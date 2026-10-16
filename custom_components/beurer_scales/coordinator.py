"""The scale coordinator: watches the scale's advertisements and syncs its readings.

One coordinator per config entry (scale). When an advertisement lists a managed
slot as pending, it connects and syncs that slot (design §5). Every reading,
whether from a sync or handed over by a flow that consented, goes through
`async_process_readings`, the single pipeline to the scale user's entities.

Scale users removed in HA are deleted on the scale at the next wake, once that
wake's weigh-in is final (design §4.3): in the same connection as a sync, or
~25 s after the wake started.
"""

import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_register_advertisement_callback,
)
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .connection import async_open_session
from .const import CONF_CONSENT_CODE, CONF_MODEL, CONF_SLOT, DOMAIN, SUBENTRY_SCALE_USER
from .pending_deletions import PendingDeletions
from .protocol.errors import ConsentRejected, ScaleError
from .protocol.frames import decode_pending_slots
from .protocol.models import (
    SWAPPED_WEIGHT_SCALE_UUID,
    WEIGHT_SCALE_SERVICE_UUID,
    model_by_name,
)
from .protocol.readings import Reading
from .protocol.session import ScaleSession
from .wake import Wake, WakeTracker

_LOGGER = logging.getLogger(__name__)

# After a failed sync, ignore pending slots for this long instead of reconnecting
# at every advertisement (~4 per second) of the same wake.
RETRY_DELAY = 15.0
# Seconds after a wake starts when pending deletions run if no sync came first.
# A weigh-in is final ~14 s after waking; connecting earlier could cut it short,
# because the scale sleeps ~1 s after HA disconnects (ADR 0008, design §4.3).
DELETION_DELAY = 25.0

_HANDOVER = "handover"

type BeurerConfigEntry = ConfigEntry[ScaleCoordinator]


def pending_slots(service_data: Mapping[str, bytes]) -> tuple[int, ...]:
    """The pending-slot list of an advertisement; the BF450 byte-swaps the UUID."""
    for uuid in (SWAPPED_WEIGHT_SCALE_UUID, WEIGHT_SCALE_SERVICE_UUID):
        if (payload := service_data.get(uuid)) is not None:
            return decode_pending_slots(payload)
    return ()


@callback
def async_hand_over_readings(
    hass: HomeAssistant, entry_id: str, slot: int, readings: Iterable[Reading]
) -> None:
    """Keep readings of a slot that has no entities yet for the next setup.

    A flow that consents (adopt) receives the scale user's readings before the
    subentry exists; the reload that adds the subentry then processes them.
    """
    handover: dict[str, dict[int, list[Reading]]] = hass.data.setdefault(
        DOMAIN, {}
    ).setdefault(_HANDOVER, {})
    handover.setdefault(entry_id, {}).setdefault(slot, []).extend(readings)


class ScaleCoordinator:
    """Syncs one scale and keeps what its entities show."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        """Set up for a scale entry; nothing runs until `async_start`."""
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.model = model_by_name(entry.data[CONF_MODEL])
        self.subentries_at_setup = dict(entry.subentries)
        self.battery_percent: int | None = None
        self.last_sync: datetime | None = None
        self._latest: dict[int, Reading] = {}
        self._listeners: list[CALLBACK_TYPE] = []
        self._syncing = False
        self._retry_after = 0.0
        self.pending_deletions = PendingDeletions(hass, entry.entry_id)
        self.wakes = WakeTracker(hass, self._async_on_wake)

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Watch every advertisement; process readings handed over by a flow.

        Load `pending_deletions` first.
        """
        handover = self.hass.data.get(DOMAIN, {}).get(_HANDOVER, {})
        for slot, readings in handover.pop(self.entry.entry_id, {}).items():
            self.async_process_readings(slot, readings)
        cancel_advertisements = async_register_advertisement_callback(
            self.hass, self._async_on_advertisement, self.address
        )

        def stop() -> None:
            cancel_advertisements()
            self.wakes.async_stop()

        return stop

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call `update` whenever a reading or a sync arrived."""
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    def latest_reading(self, slot: int) -> Reading | None:
        """The newest reading of a slot since setup."""
        return self._latest.get(slot)

    @callback
    def async_process_readings(self, slot: int, readings: Iterable[Reading]) -> None:
        """Send a slot's readings, oldest first, to its scale user's entities."""
        readings = [r for r in readings if r.slot == slot]
        if not readings:
            return
        self._latest[slot] = readings[-1]
        self._async_update_listeners()

    def _managed_slots(self) -> dict[int, ConfigSubentry]:
        return {
            int(subentry.data[CONF_SLOT]): subentry
            for subentry in self.entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_SCALE_USER
        }

    @callback
    def _async_on_advertisement(self, info: BluetoothServiceInfoBleak) -> None:
        self.wakes.async_advertisement()
        if self._syncing or self.hass.loop.time() < self._retry_after:
            return
        managed = self._managed_slots()
        pending = pending_slots(info.service_data)
        due = [slot for slot in pending if slot in managed]
        if ignored := [slot for slot in pending if slot not in managed]:
            _LOGGER.debug("Ignoring pending slots HA doesn't manage: %s", ignored)
        if not due:
            return
        self._syncing = True
        self.entry.async_create_background_task(
            self.hass,
            self._async_sync(
                managed[due[0]], info.device if info.connectable else None
            ),
            f"{DOMAIN} sync {self.address}",
        )

    async def _async_sync(
        self, subentry: ConfigSubentry, device: BLEDevice | None
    ) -> None:
        slot = int(subentry.data[CONF_SLOT])
        connected = False
        try:
            async with async_open_session(
                self.hass, self.address, self.model, device
            ) as session:
                connected = True
                result = await session.sync(
                    slot, int(subentry.data[CONF_CONSENT_CODE]), dt_util.now()
                )
                # The weigh-in is final once its slot was pending.
                await self._async_run_pending_deletions(session)
        except ScaleError as err:
            _LOGGER.warning("Syncing slot %s of %s failed: %r", slot, self.address, err)
            self._retry_after = self.hass.loop.time() + RETRY_DELAY
            return
        finally:
            self._syncing = False
            if connected:
                self.wakes.async_asleep()
        self.battery_percent = result.battery_percent
        self.last_sync = dt_util.utcnow()
        self._async_update_listeners()
        self.async_process_readings(slot, result.readings)

    @callback
    def _async_update_listeners(self) -> None:
        for update in list(self._listeners):
            update()

    # --- Pending deletions (design §4.3) ---

    @callback
    def _async_on_wake(self, wake: Wake) -> None:
        if self.pending_deletions:
            wake.async_call_at(DELETION_DELAY, self._async_delete_after_weigh_in)

    @callback
    def _async_delete_after_weigh_in(self) -> None:
        # A sync in progress runs the deletions itself.
        if self._syncing or not self.pending_deletions:
            return
        self._syncing = True
        self.entry.async_create_background_task(
            self.hass,
            self._async_delete_session(),
            f"{DOMAIN} delete scale users {self.address}",
        )

    async def _async_delete_session(self) -> None:
        connected = False
        try:
            async with async_open_session(
                self.hass, self.address, self.model
            ) as session:
                connected = True
                await self._async_run_pending_deletions(session)
        except ScaleError as err:
            _LOGGER.warning(
                "Connecting to %s to delete scale users failed: %r", self.address, err
            )
        finally:
            self._syncing = False
            if connected:
                self.wakes.async_asleep()

    async def _async_run_pending_deletions(self, session: ScaleSession) -> None:
        """Consent as each removed scale user and delete it. Never raises
        ScaleError: a failure keeps the remaining deletions for a later wake."""
        managed = self._managed_slots()
        for deletion in self.pending_deletions.all():
            if deletion.slot in managed:
                # The slot was adopted again: the scale user is wanted after all.
                await self.pending_deletions.async_discard(deletion)
                continue
            try:
                await session.consent(deletion.slot, deletion.consent_code)
                await session.delete_user()
            except ConsentRejected:
                _LOGGER.debug(
                    "Dropping the deletion of slot %s on %s: consent rejected",
                    deletion.slot,
                    self.address,
                )
                await self.pending_deletions.async_discard(deletion)
                continue
            except ScaleError as err:
                _LOGGER.warning(
                    "Deleting slot %s on %s failed: %r",
                    deletion.slot,
                    self.address,
                    err,
                )
                return
            _LOGGER.debug("Deleted slot %s on %s", deletion.slot, self.address)
            await self.pending_deletions.async_discard(deletion)

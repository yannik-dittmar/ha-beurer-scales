"""The scale coordinator: watches the scale's advertisements and syncs its readings.

One coordinator per config entry (scale). When an advertisement lists a managed
slot as pending, it connects, syncs the newest one first and drains the other
managed pending slots in the same connection (design §5). Every reading,
whether from a sync or handed over by a flow that consented, goes through
`async_process_readings`, the single pipeline to the scale user's entities.
"""

import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
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
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .connection import async_open_session
from .const import CONF_CONSENT_CODE, CONF_MODEL, CONF_SLOT, DOMAIN, SUBENTRY_SCALE_USER
from .protocol.errors import ScaleError
from .protocol.frames import decode_pending_slots
from .protocol.models import (
    SWAPPED_WEIGHT_SCALE_UUID,
    WEIGHT_SCALE_SERVICE_UUID,
    model_by_name,
)
from .protocol.readings import Reading
from .protocol.session import ScaleSession

_LOGGER = logging.getLogger(__name__)

# After a failed sync, ignore pending slots for this long instead of reconnecting
# at every advertisement (~4 per second) of the same wake.
RETRY_DELAY = 15.0
# A wake that lists only slots left pending by earlier wakes is probably a
# weigh-in still in progress: connecting would cut it short (the scale sleeps ~1 s
# after HA disconnects). Wait this long for its slot to be appended (ADR 0008, V7).
WEIGH_IN_FINAL_DELAY = 25.0

_HANDOVER = "handover"

type BeurerConfigEntry = ConfigEntry[ScaleCoordinator]


def pending_slots(service_data: Mapping[str, bytes]) -> tuple[int, ...]:
    """The pending-slot list of an advertisement; the BF450 byte-swaps the UUID."""
    for uuid in (SWAPPED_WEIGHT_SCALE_UUID, WEIGHT_SCALE_SERVICE_UUID):
        if (payload := service_data.get(uuid)) is not None:
            return decode_pending_slots(payload)
    return ()


def sync_order(pending: Sequence[int], leftovers: Collection[int]) -> list[int]:
    """The order to sync managed pending slots in: the newest weigh-in first.

    `leftovers` are the slots HA already saw pending and hasn't synced yet. The
    others just became pending. The scale appends a slot to its list when that
    weigh-in is final (protocol doc §2), so among several of either kind the
    last one listed is taken as the newest. Unverified on hardware (V2): adjust
    here only.
    """
    fresh = [slot for slot in pending if slot not in leftovers]
    rest = [slot for slot in pending if slot in leftovers]
    return [*reversed(fresh), *reversed(rest)]


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
        # Managed slots seen pending and not synced yet (see `sync_order`).
        self._leftovers: set[int] = set()
        self._ignored: list[int] = []
        self._due: list[int] = []
        self._device: BLEDevice | None = None
        self._cancel_leftover_sync: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Watch every advertisement; process readings handed over by a flow."""
        handover = self.hass.data.get(DOMAIN, {}).get(_HANDOVER, {})
        for slot, readings in handover.pop(self.entry.entry_id, {}).items():
            self.async_process_readings(slot, readings)
        stop_watching = async_register_advertisement_callback(
            self.hass, self._async_on_advertisement, self.address
        )

        @callback
        def stop() -> None:
            stop_watching()
            self._async_cancel_leftover_sync()

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
        if self._syncing or self.hass.loop.time() < self._retry_after:
            return
        managed = self._managed_slots()
        pending = pending_slots(info.service_data)
        ignored = [slot for slot in pending if slot not in managed]
        if ignored and ignored != self._ignored:
            _LOGGER.debug("Ignoring pending slots HA doesn't manage: %s", ignored)
        self._ignored = ignored
        self._due = [slot for slot in pending if slot in managed]
        self._device = info.device if info.connectable else None
        if not self._due:
            return
        if any(slot not in self._leftovers for slot in self._due):
            self._async_start_sync()
        elif self._cancel_leftover_sync is None:
            self._cancel_leftover_sync = async_call_later(
                self.hass, WEIGH_IN_FINAL_DELAY, self._async_sync_leftovers
            )

    @callback
    def _async_sync_leftovers(self, _now: datetime) -> None:
        self._cancel_leftover_sync = None
        if not self._syncing:
            self._async_start_sync()

    @callback
    def _async_cancel_leftover_sync(self) -> None:
        if self._cancel_leftover_sync is not None:
            self._cancel_leftover_sync()
            self._cancel_leftover_sync = None

    @callback
    def _async_start_sync(self) -> None:
        """Sync the slots of the last advertisement that are still managed."""
        self._async_cancel_leftover_sync()
        managed = self._managed_slots()
        order = sync_order([s for s in self._due if s in managed], self._leftovers)
        self._leftovers.update(order)
        if not order:
            return
        self._syncing = True
        self.entry.async_create_background_task(
            self.hass,
            self._async_sync([managed[slot] for slot in order], self._device),
            f"{DOMAIN} sync {self.address}",
        )

    async def _async_sync(
        self, subentries: list[ConfigSubentry], device: BLEDevice | None
    ) -> None:
        """Sync the first scale user, then drain the others in the same connection.

        Readings are processed as soon as each slot is collected, so a refused
        second consent (design §8, V2) never loses the first slot's readings.
        """
        first, *others = subentries
        slot = int(first.data[CONF_SLOT])
        try:
            async with async_open_session(
                self.hass, self.address, self.model, device
            ) as session:
                result = await session.sync(
                    slot, int(first.data[CONF_CONSENT_CODE]), dt_util.now()
                )
                self.battery_percent = result.battery_percent
                self.last_sync = dt_util.utcnow()
                self._async_update_listeners()
                self._leftovers.discard(slot)
                self.async_process_readings(slot, result.readings)
                await self._async_drain(session, others)
        except ScaleError as err:
            _LOGGER.warning("Syncing slot %s of %s failed: %r", slot, self.address, err)
            self._retry_after = self.hass.loop.time() + RETRY_DELAY
        finally:
            self._syncing = False

    async def _async_drain(
        self, session: ScaleSession, subentries: list[ConfigSubentry]
    ) -> None:
        """Consent and collect further slots until the scale refuses one."""
        for subentry in subentries:
            slot = int(subentry.data[CONF_SLOT])
            try:
                readings = await session.drain(
                    slot, int(subentry.data[CONF_CONSENT_CODE])
                )
            except ScaleError as err:
                # The remaining slots stay pending and are synced at a later wake.
                _LOGGER.debug(
                    "%s refused slot %s in the same connection: %r",
                    self.address,
                    slot,
                    err,
                )
                return
            self._leftovers.discard(slot)
            self.async_process_readings(slot, readings)

    @callback
    def _async_update_listeners(self) -> None:
        for update in list(self._listeners):
            update()

"""The scale coordinator: watches the scale's advertisements and syncs its readings.

One coordinator per config entry (scale). When an advertisement lists a managed
slot as pending, it connects and syncs that slot (design §5). Every reading,
whether from a sync or handed over by a flow that consented, goes through
`async_process_readings`, the single pipeline to the scale user's entities. The
reading policy sits in that pipeline: only fresh readings reach the entities,
backlog and guessed readings are reported, duplicates are dropped (design §5.4).
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
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .connection import async_open_session
from .discards import async_notify_discarded
from .const import CONF_CONSENT_CODE, CONF_MODEL, CONF_SLOT, DOMAIN, SUBENTRY_SCALE_USER
from .protocol.errors import ScaleError
from .protocol.frames import decode_pending_slots
from .protocol.models import (
    SWAPPED_WEIGHT_SCALE_UUID,
    WEIGHT_SCALE_SERVICE_UUID,
    model_by_name,
)
from .protocol.policy import ReadingPolicy
from .protocol.readings import Reading

_LOGGER = logging.getLogger(__name__)

# After a failed sync, ignore pending slots for this long instead of reconnecting
# at every advertisement (~4 per second) of the same wake.
RETRY_DELAY = 15.0

_HANDOVER = "handover"

# The de-duplication log, one store per scale (design §5.4).
READING_LOG_VERSION = 1
READING_LOG_SAVE_DELAY = 10.0


def reading_log_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """The store that keeps a scale's de-duplication log across restarts."""
    return Store(hass, READING_LOG_VERSION, f"{DOMAIN}.{entry_id}.reading_log")


type BeurerConfigEntry = ConfigEntry[ScaleCoordinator]


def pending_slots(service_data: Mapping[str, bytes]) -> tuple[int, ...]:
    """The pending-slot list of an advertisement; the BF450 byte-swaps the UUID."""
    for uuid in (SWAPPED_WEIGHT_SCALE_UUID, WEIGHT_SCALE_SERVICE_UUID):
        if (payload := service_data.get(uuid)) is not None:
            return decode_pending_slots(payload)
    return ()


@callback
def async_hand_over_readings(
    hass: HomeAssistant,
    entry_id: str,
    slot: int,
    readings: Iterable[Reading],
    sync_start: datetime | None = None,
) -> None:
    """Keep readings of a slot that has no entities yet for the next setup.

    A flow that consents (adopt) receives the scale user's readings before the
    subentry exists; the reload that adds the subentry then processes them.
    `sync_start` is when the flow connected (default: now), for the backlog rule.
    """
    handover: dict[str, dict[int, list[tuple[datetime, list[Reading]]]]] = (
        hass.data.setdefault(DOMAIN, {}).setdefault(_HANDOVER, {})
    )
    handover.setdefault(entry_id, {}).setdefault(slot, []).append(
        (sync_start or dt_util.now(), list(readings))
    )


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
        self._policy = ReadingPolicy()
        self._reading_log = reading_log_store(hass, entry.entry_id)

    async def async_load(self) -> None:
        """Restore the de-duplication log; call before `async_start`."""
        if (stored := await self._reading_log.async_load()) is not None:
            self._policy = ReadingPolicy(tuple(e) for e in stored["readings"])

    async def async_save(self) -> None:
        """Write the de-duplication log now, e.g. when the scale unloads."""
        await self._reading_log.async_save(self._reading_log_data())

    def _reading_log_data(self) -> dict[str, Any]:
        return {"readings": self._policy.log()}

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Watch every advertisement; process readings handed over by a flow."""
        handover = self.hass.data.get(DOMAIN, {}).get(_HANDOVER, {})
        for slot, batches in handover.pop(self.entry.entry_id, {}).items():
            for sync_start, readings in batches:
                self.async_process_readings(slot, readings, sync_start)
        return async_register_advertisement_callback(
            self.hass, self._async_on_advertisement, self.address
        )

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call `update` whenever a reading or a sync arrived."""
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    def latest_reading(self, slot: int) -> Reading | None:
        """The newest reading of a slot since setup."""
        return self._latest.get(slot)

    @callback
    def async_process_readings(
        self,
        slot: int,
        readings: Iterable[Reading],
        sync_start: datetime | None = None,
    ) -> None:
        """Send a slot's fresh readings, oldest first, to its scale user's entities.

        `sync_start` (aware, default now) is when the sync that collected the
        readings began; readings much older than it are backlog (ADR 0005).
        """
        readings = [r for r in readings if r.slot == slot]
        if not readings:
            return
        start = dt_util.as_local(sync_start or dt_util.now()).replace(tzinfo=None)
        result = self._policy.apply(readings, start)
        self._reading_log.async_delay_save(
            self._reading_log_data, READING_LOG_SAVE_DELAY
        )
        if result.discarded:
            subentry = self._managed_slots().get(slot)
            async_notify_discarded(
                self.hass,
                self.address,
                slot,
                subentry.title if subentry else f"slot {slot}",
                result.discarded,
            )
        if not result.fresh:
            return
        self._latest[slot] = result.fresh[-1]
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
        sync_start = dt_util.now()
        try:
            async with async_open_session(
                self.hass, self.address, self.model, device
            ) as session:
                result = await session.sync(
                    slot, int(subentry.data[CONF_CONSENT_CODE]), sync_start
                )
        except ScaleError as err:
            _LOGGER.warning("Syncing slot %s of %s failed: %r", slot, self.address, err)
            self._retry_after = self.hass.loop.time() + RETRY_DELAY
            return
        finally:
            self._syncing = False
        self.battery_percent = result.battery_percent
        self.last_sync = dt_util.utcnow()
        self._async_update_listeners()
        self.async_process_readings(slot, result.readings, sync_start)

    @callback
    def _async_update_listeners(self) -> None:
        for update in list(self._listeners):
            update()

"""The scale coordinator: watches the scale's advertisements and syncs its readings.

One coordinator per config entry (scale). When an advertisement lists a managed
slot as pending, it connects and syncs that slot (design §5). Every reading,
whether from a sync or handed over by a flow that consented, goes through
`async_process_readings`, the single pipeline to the scale user's entities.
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
from .protocol.errors import ScaleError
from .protocol.frames import decode_pending_slots
from .protocol.models import (
    SWAPPED_WEIGHT_SCALE_UUID,
    WEIGHT_SCALE_SERVICE_UUID,
    model_by_name,
)
from .protocol.readings import Reading

_LOGGER = logging.getLogger(__name__)

# After a failed sync, ignore pending slots for this long instead of reconnecting
# at every advertisement (~4 per second) of the same wake.
RETRY_DELAY = 15.0

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

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Watch every advertisement; process readings handed over by a flow."""
        handover = self.hass.data.get(DOMAIN, {}).get(_HANDOVER, {})
        for slot, readings in handover.pop(self.entry.entry_id, {}).items():
            self.async_process_readings(slot, readings)
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
        try:
            async with async_open_session(
                self.hass, self.address, self.model, device
            ) as session:
                result = await session.sync(
                    slot, int(subentry.data[CONF_CONSENT_CODE]), dt_util.now()
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
        self.async_process_readings(slot, result.readings)

    @callback
    def _async_update_listeners(self) -> None:
        for update in list(self._listeners):
            update()

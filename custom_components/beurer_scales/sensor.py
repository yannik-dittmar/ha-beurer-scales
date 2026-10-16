"""Sensors: per scale user the reading's metrics, per scale its battery and last sync."""

from datetime import datetime

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfMass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_SLOT, DOMAIN, SUBENTRY_SCALE_USER
from .coordinator import BeurerConfigEntry, ScaleCoordinator
from .protocol.readings import Reading


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BeurerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the scale's sensors and each scale user's sensors."""
    coordinator = entry.runtime_data
    async_add_entities([BatterySensor(coordinator), LastSyncSensor(coordinator)])
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_SCALE_USER:
            continue
        async_add_entities(
            [
                WeightSensor(coordinator, subentry),
                LastReadingSensor(coordinator, subentry),
            ],
            config_subentry_id=subentry.subentry_id,
        )


class _ScaleSensor(RestoreSensor):
    """A sensor of the scale device that restores its value after a restart."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: ScaleCoordinator, key: str) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.address)})

    async def async_added_to_hass(self) -> None:
        """Restore the last value, then follow the coordinator."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last.native_value
        self.async_on_remove(self.coordinator.async_add_listener(self._async_update))

    @callback
    def _async_update(self) -> None:
        raise NotImplementedError


class BatterySensor(_ScaleSensor):
    """The scale's battery level, read at every sync."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ScaleCoordinator) -> None:
        """Create the battery sensor."""
        super().__init__(coordinator, "battery")

    @callback
    def _async_update(self) -> None:
        if self.coordinator.battery_percent is not None:
            self._attr_native_value = self.coordinator.battery_percent
            self.async_write_ha_state()


class LastSyncSensor(_ScaleSensor):
    """When HA last synced the scale."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "last_sync"

    def __init__(self, coordinator: ScaleCoordinator) -> None:
        """Create the last sync sensor."""
        super().__init__(coordinator, "last_sync")

    @callback
    def _async_update(self) -> None:
        if self.coordinator.last_sync is not None:
            self._attr_native_value = self.coordinator.last_sync
            self.async_write_ha_state()


class _ScaleUserSensor(_ScaleSensor):
    """A sensor of a scale user's device, fed by that slot's readings."""

    def __init__(
        self, coordinator: ScaleCoordinator, subentry: ConfigSubentry, key: str
    ) -> None:
        self._slot = int(subentry.data[CONF_SLOT])
        super().__init__(coordinator, f"slot_{self._slot}_{key}")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.address}_slot_{self._slot}")}
        )
        self._reading: Reading | None = None

    @callback
    def _async_update(self) -> None:
        reading = self.coordinator.latest_reading(self._slot)
        if reading is None or reading is self._reading:
            return
        self._reading = reading
        self._attr_native_value = self._value(reading)
        self.async_write_ha_state()

    def _value(self, reading: Reading) -> float | datetime | None:
        raise NotImplementedError


class WeightSensor(_ScaleUserSensor):
    """The scale user's weight."""

    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_native_unit_of_measurement = UnitOfMass.KILOGRAMS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: ScaleCoordinator, subentry: ConfigSubentry) -> None:
        """Create the weight sensor of a scale user."""
        super().__init__(coordinator, subentry, "weight")

    def _value(self, reading: Reading) -> float:
        return reading.weight_kg


class LastReadingSensor(_ScaleUserSensor):
    """When the scale user's newest reading was taken, by the scale's clock."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_reading"

    def __init__(self, coordinator: ScaleCoordinator, subentry: ConfigSubentry) -> None:
        """Create the last reading sensor of a scale user."""
        super().__init__(coordinator, subentry, "last_reading")

    def _value(self, reading: Reading) -> datetime | None:
        if reading.timestamp is None:
            return None
        # The scale's clock runs on HA's local time, set at every sync.
        return reading.timestamp.replace(tzinfo=dt_util.get_default_time_zone())

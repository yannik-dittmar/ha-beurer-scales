"""Binary sensor: whether the scale is awake, for automations on stepping on."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import BeurerConfigEntry, ScaleCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BeurerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the scale's Awake sensor."""
    async_add_entities([AwakeBinarySensor(entry.runtime_data)])


class AwakeBinarySensor(BinarySensorEntity):
    """On from the scale's first advertisement (someone tapped or stepped on it)
    until it has been silent for a while, HA's own sessions included.

    It turns on before the weigh-in is final and before anyone is recognised,
    so it suits a routine started by stepping on; the readings follow later.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "awake"

    def __init__(self, coordinator: ScaleCoordinator) -> None:
        """Create the Awake sensor."""
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_awake"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.address)})

    async def async_added_to_hass(self) -> None:
        """Follow the coordinator."""
        self.async_on_remove(self.coordinator.async_add_listener(self._async_update))

    @property
    def is_on(self) -> bool:
        """Whether the scale is awake."""
        return self.coordinator.awake

    @callback
    def _async_update(self) -> None:
        self.async_write_ha_state()

"""The Beurer Scales integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_MODEL, CONF_SLOT, DOMAIN, MANUFACTURER, SUBENTRY_SCALE_USER


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a scale and its scale users. This never connects to the scale."""
    address: str = entry.data[CONF_ADDRESS]
    device_registry = dr.async_get(hass)
    scale_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, address)},
        identifiers={(DOMAIN, address)},
        manufacturer=MANUFACTURER,
        model=entry.data[CONF_MODEL],
        name=entry.title,
    )
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_SCALE_USER:
            continue
        # Each scale user is its own device, connected via the scale (ADR 0004).
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=subentry.subentry_id,
            identifiers={(DOMAIN, f"{address}_slot_{subentry.data[CONF_SLOT]}")},
            manufacturer=MANUFACTURER,
            model="Scale user",
            name=subentry.title,
            via_device_id=scale_device.id,
        )
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when scale users are added or removed."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a scale."""
    return True

"""The Beurer Scales integration."""

from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_MODEL, CONF_SLOT, DOMAIN, MANUFACTURER, SUBENTRY_SCALE_USER
from .coordinator import BeurerConfigEntry, ScaleCoordinator, subentries_snapshot
from .repairs import async_clear_orphaned_scale_user_issues

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: BeurerConfigEntry) -> bool:
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
    # A removed scale user takes its repair issue along.
    async_clear_orphaned_scale_user_issues(hass, entry)
    entry.runtime_data = ScaleCoordinator(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.runtime_data.async_start())
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: BeurerConfigEntry) -> None:
    """Reload when scale users are added, changed or removed.

    Other updates, such as HA recording the discovery key at an advertisement,
    must not reload: that would cut off a sync in progress.
    """
    if subentries_snapshot(entry) != entry.runtime_data.subentries_at_setup:
        hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: BeurerConfigEntry) -> bool:
    """Unload a scale."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: BeurerConfigEntry) -> None:
    """Close the repair issues of a removed scale."""
    async_clear_orphaned_scale_user_issues(hass, entry, all_of_entry=True)

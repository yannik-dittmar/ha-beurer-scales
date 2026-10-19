"""The Beurer Scales integration."""

from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_CONSENT_CODE,
    CONF_MODEL,
    CONF_SLOT,
    DOMAIN,
    MANUFACTURER,
    SUBENTRY_SCALE_USER,
    scale_user_id,
)
from .coordinator import (
    BeurerConfigEntry,
    ScaleCoordinator,
    reading_log_store,
    subentries_snapshot,
)
from .repairs import (
    async_clear_orphaned_scale_user_issues,
    async_delete_obsolete_unit_issue,
)
from .pending_deletions import PendingDeletion, async_remove_pending_deletions

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
            identifiers={(DOMAIN, scale_user_id(address, subentry.data[CONF_SLOT]))},
            manufacturer=MANUFACTURER,
            model="Scale user",
            name=subentry.title,
            via_device_id=scale_device.id,
        )
    # A removed scale user takes its repair issue along.
    async_clear_orphaned_scale_user_issues(hass, entry)
    async_delete_obsolete_unit_issue(hass, entry)
    entry.runtime_data = ScaleCoordinator(hass, entry)
    await entry.runtime_data.async_load()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.runtime_data.async_start())
    entry.async_on_unload(entry.runtime_data.async_save)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: BeurerConfigEntry) -> None:
    """Reload when scale users are added, changed or removed.

    Other updates, such as HA recording the discovery key at an advertisement,
    must not reload: that would cut off a sync in progress.
    """
    coordinator = entry.runtime_data
    for subentry_id, (_title, data) in coordinator.subentries_at_setup.items():
        if subentry_id not in entry.subentries and CONF_CONSENT_CODE in data:
            # HA has already removed its device and entities; the scale user
            # is deleted on the scale at the next wake (design §4.3).
            await coordinator.pending_deletions.async_add(
                PendingDeletion(
                    slot=int(data[CONF_SLOT]), consent_code=int(data[CONF_CONSENT_CODE])
                )
            )
    if subentries_snapshot(entry) != coordinator.subentries_at_setup:
        hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: BeurerConfigEntry) -> None:
    """Forget the scale's stored data and close its repairs when it is removed."""
    await reading_log_store(hass, entry.entry_id).async_remove()
    async_clear_orphaned_scale_user_issues(hass, entry, all_of_entry=True)
    await async_remove_pending_deletions(hass, entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: BeurerConfigEntry) -> bool:
    """Unload a scale."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

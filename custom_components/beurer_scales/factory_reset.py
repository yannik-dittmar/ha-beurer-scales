"""Factory-resetting the scale from its configure menu (design §4.4, #30).

The reset deletes every scale user on the scale, so Home Assistant forgets its
own scale users afterwards: their slots are gone, and so are the deletions that
were waiting for a wake.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.components.bluetooth import async_ble_device_from_address

from .bond import async_remove_bond
from .connection import async_hold_scale, async_open_session, async_wait_for_wake
from .const import CONF_CONSENT_CODE, SUBENTRY_SCALE_USER
from .coordinator import ScaleCoordinator
from .repairs import async_delete_deletion_issue
from .protocol.errors import ScaleConnectionError
from .protocol.models import model_by_name
from .const import CONF_MODEL


async def async_factory_reset_scale(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Wait for a wake, reset the scale, then drop the bond of the path used.

    The scale forgets its bond keys with the reset while the path keeps its own,
    and bonding then fails (§9.6, V6): the bond is removed on the very path that
    carried the reset. Holds the scale from the wait through the end.
    """
    address: str = entry.data[CONF_ADDRESS]
    model = model_by_name(entry.data[CONF_MODEL])
    async with async_hold_scale(hass, address):
        await async_wait_for_wake(hass, address)
        device = async_ble_device_from_address(hass, address, connectable=True)
        if device is None:
            raise ScaleConnectionError(f"no connectable Bluetooth path to {address}")
        async with async_open_session(hass, address, model, device=device) as session:
            await session.factory_reset()
        # Best effort: where the path can't drop its bond (a local adapter), the
        # next connect's pairing failure explains what to do.
        await async_remove_bond(hass, device)


async def async_forget_scale_users(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: ScaleCoordinator
) -> None:
    """Remove every scale user subentry and clear the pending deletions.

    Removing a subentry would queue its deletion on the scale; the slots are gone
    already, so the consent codes are dropped from the coordinator's snapshot
    first, which is what the update listener compares against.
    """
    for deletion in coordinator.pending_deletions.all():
        await coordinator.pending_deletions.async_discard(deletion)
    async_delete_deletion_issue(hass, entry)
    coordinator.subentries_at_setup = {
        subentry_id: (title, {k: v for k, v in data.items() if k != CONF_CONSENT_CODE})
        for subentry_id, (title, data) in coordinator.subentries_at_setup.items()
    }
    for subentry in list(entry.subentries.values()):
        if subentry.subentry_type == SUBENTRY_SCALE_USER:
            hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)

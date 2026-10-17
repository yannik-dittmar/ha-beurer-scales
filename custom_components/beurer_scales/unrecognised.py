"""Telling the household that someone stepped on the scale unrecognised.

An unrecognised weigh-in matches no scale user: the scale stores nothing for it,
so the person needs to be added or taught (ADR 0008, design §5.3). The
notification is a scale option, on by default.
"""

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


@callback
def async_notify_unrecognised(hass: HomeAssistant, address: str, scale: str) -> None:
    """Say that someone wasn't recognised; a newer one replaces the last."""
    persistent_notification.async_create(
        hass,
        f"Someone stepped on {scale}, but the scale recognised them as none of "
        "its scale users, so it stored no reading. If they should be tracked, "
        "add them as a scale user of the scale. You can turn this notification "
        "off in the scale's options.",
        title=f"Weigh-in on {scale} not recognised",
        notification_id=f"{DOMAIN}_{address}_unrecognised",
    )

"""Repair issues of the Beurer Scales integration and their fix flows.

Scale user issues (design §6): a scale user's consent failed and the user list
told why. Each one is keyed by the scale user's subentry, blocks that scale
user's syncs while it is open, and is fixed through the scale user subentry
flows. It is cleared once the scale user syncs again or is removed.
"""

from enum import StrEnum

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

# --- Scale user issues: wrong code, deleted, factory reset (#12) ---


class ScaleUserIssue(StrEnum):
    """Why a scale user can't be consented; also the issue's translation key."""

    WRONG_CONSENT_CODE = "wrong_consent_code"
    SCALE_USER_DELETED = "scale_user_deleted"
    FACTORY_RESET = "factory_reset"


ISSUE_DATA_ENTRY_ID = "entry_id"
ISSUE_DATA_SUBENTRY_ID = "subentry_id"


def scale_user_issue_id(kind: ScaleUserIssue, subentry_id: str) -> str:
    """The issue id of a scale user issue."""
    return f"{kind}_{subentry_id}"


@callback
def async_raise_scale_user_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    subentry: ConfigSubentry,
    kind: ScaleUserIssue,
) -> None:
    """Open a scale user issue, replacing another kind for the same scale user."""
    for other in ScaleUserIssue:
        if other is not kind:
            ir.async_delete_issue(
                hass, DOMAIN, scale_user_issue_id(other, subentry.subentry_id)
            )
    ir.async_create_issue(
        hass,
        DOMAIN,
        scale_user_issue_id(kind, subentry.subentry_id),
        data={
            ISSUE_DATA_ENTRY_ID: entry.entry_id,
            ISSUE_DATA_SUBENTRY_ID: subentry.subentry_id,
        },
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=kind,
        translation_placeholders={"name": subentry.title, "scale": entry.title},
    )


@callback
def async_clear_scale_user_issues(hass: HomeAssistant, subentry_id: str) -> None:
    """Close every scale user issue of a scale user."""
    for kind in ScaleUserIssue:
        ir.async_delete_issue(hass, DOMAIN, scale_user_issue_id(kind, subentry_id))


@callback
def async_scale_user_issue(
    hass: HomeAssistant, subentry_id: str
) -> ScaleUserIssue | None:
    """The open scale user issue of a scale user, if any."""
    registry = ir.async_get(hass)
    for kind in ScaleUserIssue:
        if registry.async_get_issue(DOMAIN, scale_user_issue_id(kind, subentry_id)):
            return kind
    return None

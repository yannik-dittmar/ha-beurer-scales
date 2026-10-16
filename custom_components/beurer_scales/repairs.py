"""Repair issues and warnings the integration raises about a scale (design §6).

Each issue type has a create and a delete helper here; the coordinator calls them.
Issue ids carry the config entry id (scale issues) or the subentry id (scale user
issues), so every scale and scale user has its own issues.

Scale user issues (wrong consent code, scale user deleted, factory reset): a
scale user's consent failed and the user list told why. Each one blocks that
scale user's syncs while it is open, is fixed through the scale user subentry
flows (`async_create_fix_flow`, the HA repairs platform hook), and is cleared
once the scale user syncs again or is removed.
"""

from enum import StrEnum
from typing import Any

from homeassistant.components.repairs import FlowType, RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    SOURCE_REPAIR_ADOPT,
    SOURCE_REPAIR_CREATE,
    SUBENTRY_SCALE_USER,
)

ISSUE_PAIRING_IMPOSSIBLE = "pairing_impossible"
ISSUE_UNSUPPORTED_UNIT = "unsupported_unit"


def _issue_id(kind: str, entry: ConfigEntry) -> str:
    return f"{kind}_{entry.entry_id}"


@callback
def async_create_pairing_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The Bluetooth path HA picked can't bond with the scale (ADR 0002).

    Nothing in HA can fix this: the user adds an ESPHome proxy or makes the host
    adapter pairable, and the issue clears at the next successful sync.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id("pairing", entry),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_PAIRING_IMPOSSIBLE,
        translation_placeholders={"name": entry.title},
        learn_more_url="https://esphome.io/components/bluetooth_proxy/",
    )


@callback
def async_delete_pairing_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """A sync bonded and succeeded."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id("pairing", entry))


@callback
def async_create_unit_issue(hass: HomeAssistant, entry: ConfigEntry, unit: str) -> None:
    """The scale reported weights in `unit`, not kg; they were not used.

    Advisory only: v1 never writes scale settings, so the user sets the scale's
    display back to kg (design §6).
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id("unit", entry),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_UNSUPPORTED_UNIT,
        translation_placeholders={"name": entry.title, "unit": unit},
    )


@callback
def async_delete_unit_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The scale reports kg again."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id("unit", entry))


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


@callback
def async_clear_orphaned_scale_user_issues(
    hass: HomeAssistant, entry: ConfigEntry, *, all_of_entry: bool = False
) -> None:
    """Close the scale user issues of an entry whose scale user is gone.

    With `all_of_entry`, close every scale user issue of the entry (the entry
    is being removed).
    """
    registry = ir.async_get(hass)
    for issue in list(registry.issues.values()):
        data = issue.data or {}
        if (
            issue.domain == DOMAIN
            and data.get(ISSUE_DATA_ENTRY_ID) == entry.entry_id
            and (
                all_of_entry or data.get(ISSUE_DATA_SUBENTRY_ID) not in entry.subentries
            )
        ):
            ir.async_delete_issue(hass, DOMAIN, issue.issue_id)


class ScaleUserRepairFlow(RepairsFlow):
    """Fix a scale user issue: enter the code again, create again, or remove.

    Entering the code and creating continue in the scale user subentry flow;
    that flow clears the issue once it succeeds.
    """

    def __init__(self, kind: ScaleUserIssue, entry_id: str, subentry_id: str) -> None:
        """Fix the issue of one scale user."""
        self._kind = kind
        self._entry_id = entry_id
        self._subentry_id = subentry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Offer the fixes that fit the issue."""
        if (subentry := self._subentry()) is None:
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return self.async_abort(reason="scale_user_removed")
        options = ["create", "remove"]
        if self._kind is ScaleUserIssue.WRONG_CONSENT_CODE:
            options.insert(0, "adopt")
        return self.async_show_menu(
            step_id="init",
            menu_options=options,
            description_placeholders={"name": subentry.title},
        )

    async def async_step_adopt(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Enter the consent code again."""
        return await self._async_continue_in(SOURCE_REPAIR_ADOPT)

    async def async_step_create(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Create the scale user on the scale again."""
        return await self._async_continue_in(SOURCE_REPAIR_CREATE)

    async def async_step_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Remove the scale user from Home Assistant; this closes the issue."""
        entry = self.hass.config_entries.async_get_known_entry(self._entry_id)
        self.hass.config_entries.async_remove_subentry(entry, self._subentry_id)
        return self.async_create_entry(data={})

    def _subentry(self) -> ConfigSubentry | None:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        return entry.subentries.get(self._subentry_id) if entry else None

    async def _async_continue_in(self, source: str) -> RepairsFlowResult:
        result = await self.hass.config_entries.subentries.async_init(
            (self._entry_id, SUBENTRY_SCALE_USER),
            context={"source": source, "subentry_id": self._subentry_id},
        )
        # Aborting keeps the issue open until the subentry flow fixed it.
        return self.async_abort(
            reason="continue_in_scale_user_flow",
            next_flow=(FlowType.CONFIG_SUBENTRIES_FLOW, result["flow_id"]),
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the fix flow of a fixable issue."""
    data = data or {}
    for kind in ScaleUserIssue:
        if issue_id.startswith(f"{kind}_"):
            return ScaleUserRepairFlow(
                kind,
                str(data[ISSUE_DATA_ENTRY_ID]),
                str(data[ISSUE_DATA_SUBENTRY_ID]),
            )
    raise ValueError(f"No fix flow for issue {issue_id}")

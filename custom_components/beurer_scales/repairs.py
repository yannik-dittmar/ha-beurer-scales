"""Repair issues and warnings the integration raises about a scale (design §6).

Each issue type has a create and a delete helper here; the coordinator calls them.
Issue ids carry the config entry id, so every scale has its own issues.

Fixable issues (wrong consent code, scale user deleted, factory reset) also need
`async_create_fix_flow` in this module, the HA repairs platform hook.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

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

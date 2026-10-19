"""Repair issues and warnings about the scale's Bluetooth path and weight unit."""

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.beurer_scales.const import DOMAIN

from . import frames
from .conftest import stay_awake, wake_scale
from .fake_scale import FakeReading, FakeScale
from .test_sync import WEIGHT, anna, local_now, state

__all__ = ["anna"]  # a fixture


def pairing_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    """The scale's "pairing impossible" repair issue, if raised."""
    return ir.async_get(hass).async_get_issue(DOMAIN, f"pairing_{entry.entry_id}")


def weigh_in(scale: FakeScale) -> None:
    """Anna steps on the scale; her slot becomes pending."""
    scale.weigh_in(2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now()))


async def test_a_path_without_pairing_support_raises_a_repair_issue(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A proxy that can't pair is reported with what to do about it (ADR 0002)."""
    fake_scale.pairing = "unsupported"
    weigh_in(fake_scale)

    await wake_scale(hass, fake_scale)

    issue = pairing_issue(hass, anna)
    assert issue is not None
    assert issue.translation_key == "pairing_impossible"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {"name": "BF450"}


async def test_a_failed_bond_raises_the_same_repair_issue(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A path that tries to bond and fails, e.g. HA OS's local adapter."""
    fake_scale.pairing = "fails"
    weigh_in(fake_scale)

    await wake_scale(hass, fake_scale)

    issue = pairing_issue(hass, anna)
    assert issue is not None
    assert issue.translation_key == "pairing_impossible"


# The sync waits for a quiet period, so time has to run.
@pytest.mark.freeze_time(tick=True)
async def test_the_pairing_issue_clears_after_the_next_successful_sync(
    hass: HomeAssistant,
    anna: MockConfigEntry,
    fake_scale: FakeScale,
    monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Once a proxy that can pair reaches the scale, the issue goes away."""
    monkeypatch.setattr("custom_components.beurer_scales.coordinator.RETRY_DELAY", 0.0)
    fake_scale.pairing = "unsupported"
    weigh_in(fake_scale)
    await wake_scale(hass, fake_scale)
    assert pairing_issue(hass, anna) is not None

    fake_scale.pairing = "supported"
    # The slot is a leftover now: HA syncs it once the weigh-in is final.
    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 30)

    assert pairing_issue(hass, anna) is None
    assert state(hass, WEIGHT).state == "78.4"


async def test_other_connection_failures_raise_no_pairing_issue(
    hass: HomeAssistant,
    anna: MockConfigEntry,
    fake_scale: FakeScale,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sync that times out isn't blamed on the Bluetooth path."""
    monkeypatch.setattr(
        "custom_components.beurer_scales.protocol.session.SESSION_TIMEOUT", 0.05
    )
    fake_scale.answers_ucp = False
    weigh_in(fake_scale)

    await wake_scale(hass, fake_scale)

    assert pairing_issue(hass, anna) is None


async def test_a_unit_issue_from_an_older_version_is_removed_at_setup(
    hass: HomeAssistant, anna: MockConfigEntry
) -> None:
    """The lb/st check is gone (design §6); its leftover issue is cleared."""
    issue_id = f"unit_{anna.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="unsupported_unit",
    )

    await hass.config_entries.async_reload(anna.entry_id)

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None

"""Repair issues and warnings about the scale's Bluetooth path and weight unit."""

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNKNOWN
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


def unit_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    """The scale's "unit isn't kg" warning, if raised."""
    return ir.async_get(hass).async_get_issue(DOMAIN, f"unit_{entry.entry_id}")


async def test_a_reading_in_pounds_is_not_written_as_kg_and_raises_a_warning(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """With the display set to lb, HA warns instead of showing a wrong weight."""
    fake_scale.display_unit = "lb"
    weigh_in(fake_scale)

    await wake_scale(hass, fake_scale)

    assert state(hass, WEIGHT).state == STATE_UNKNOWN
    issue = unit_issue(hass, anna)
    assert issue is not None
    assert issue.translation_key == "unsupported_unit"
    assert issue.severity is ir.IssueSeverity.WARNING
    assert not issue.is_fixable
    assert issue.translation_placeholders == {"name": "BF450", "unit": "lb"}
    # The readings were collected, so the scale doesn't offer them again.
    assert fake_scale.service_data() == {"00001d18-0000-1000-8000-00805f9b34fb": b""}


async def test_the_unit_warning_clears_once_readings_are_in_kg_again(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Switching the display back to kg ends the warning."""
    fake_scale.display_unit = "lb"
    weigh_in(fake_scale)
    await wake_scale(hass, fake_scale)
    assert unit_issue(hass, anna) is not None

    fake_scale.display_unit = "kg"
    weigh_in(fake_scale)
    await wake_scale(hass, fake_scale)

    assert unit_issue(hass, anna) is None
    assert state(hass, WEIGHT).state == "78.4"


async def test_kg_readings_raise_no_unit_warning(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """The BF450 in its default kg mode."""
    weigh_in(fake_scale)

    await wake_scale(hass, fake_scale)

    assert unit_issue(hass, anna) is None

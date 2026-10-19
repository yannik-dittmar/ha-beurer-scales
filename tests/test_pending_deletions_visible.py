"""A removed scale user's pending deletion is visible, and a failing one is flagged (#29)."""

import logging

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beurer_scales.const import DOMAIN

from .conftest import stay_awake, wake_scale
from .fake_scale import FakeScale
from .test_remove_scale_user import remove_bob, scale  # noqa: F401

SENSOR = "sensor.bf450_pending_deletions"


def issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    """The scale's "deletion failing" repair issue, if raised."""
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"deletion_failing_{entry.entry_id}"
    )


async def missed_wake(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, fake_scale: FakeScale
) -> None:
    """A wake that ends before HA can connect."""
    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 6)
    fake_scale.awake = False
    await stay_awake(hass, freezer, fake_scale, 30)


async def test_no_deletion_is_pending_by_default(
    hass: HomeAssistant,
    scale: MockConfigEntry,  # noqa: F811
) -> None:
    """The sensor exists and reads 0."""
    state = hass.states.get(SENSOR)
    assert state is not None
    assert state.state == "0"
    assert state.attributes["slots"] == []


async def test_a_removed_scale_user_shows_as_pending_until_deleted(
    hass: HomeAssistant,
    scale: MockConfigEntry,  # noqa: F811
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
    caplog: logging.LogRecord,
) -> None:
    """The count and slots show the deletion, which drops to 0 and is logged."""
    caplog.set_level(logging.INFO, logger="custom_components.beurer_scales")
    await remove_bob(hass, scale)
    state = hass.states.get(SENSOR)
    assert state.state == "1"
    assert state.attributes["slots"] == [3]

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    assert 3 not in fake_scale.users
    state = hass.states.get(SENSOR)
    assert state.state == "0"
    assert state.attributes["slots"] == []
    assert "Deleted slot 3" in caplog.text


async def test_repeated_failures_raise_a_repair_issue_that_clears(
    hass: HomeAssistant,
    scale: MockConfigEntry,  # noqa: F811
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Three wakes without the deletion raise the issue; the deletion closes it."""
    await remove_bob(hass, scale)
    for _ in range(2):
        await missed_wake(hass, freezer, fake_scale)
        assert issue(hass, scale) is None
    await missed_wake(hass, freezer, fake_scale)

    raised = issue(hass, scale)
    assert raised is not None
    assert raised.translation_placeholders["wakes"] == "3"
    assert raised.translation_key == "deletion_failing"
    assert raised.translation_placeholders["name"] == "BF450"
    assert raised.is_fixable is False

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    assert 3 not in fake_scale.users
    assert issue(hass, scale) is None


async def test_a_deletion_that_works_never_raises_the_issue(
    hass: HomeAssistant,
    scale: MockConfigEntry,  # noqa: F811
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Two missed wakes, then success, leave nothing behind."""
    await remove_bob(hass, scale)
    await missed_wake(hass, freezer, fake_scale)
    await missed_wake(hass, freezer, fake_scale)
    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)
    await missed_wake(hass, freezer, fake_scale)
    assert issue(hass, scale) is None

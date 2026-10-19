"""Active scanning per wake, and wakes without any scan response (#22).

The scale lists its pending slots only in its scan response, which a passively
scanning path never receives. At each wake HA asks for an active scan; a wake in
which no scan response arrived says nothing about who stepped on.
"""

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.beurer_scales.const import DOMAIN
from custom_components.beurer_scales.wake import WAKE_WINDOW

from .conftest import (
    advertisement,
    inject_advertisement,
    stay_awake,
    wake_scale,
)
from .fake_scale import FakeScale
from .test_sync import WEIGHT, anna_scale, state
from .test_unrecognised_weigh_in import unrecognised_notifications
from .test_weigh_in_final import anna_weighs_in

pytestmark = pytest.mark.freeze_time(tick=True)


def issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    """The scale's "no scan response" repair issue, if raised."""
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"no_scan_response_{entry.entry_id}"
    )


async def tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Two seconds pass; the scale's advertising is up to the caller."""
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def passive_wake(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    scale: FakeScale,
    active_scans: list[float | None],
    seconds: float = 30,
) -> None:
    """A wake on a passive path: only plain advertisements, until an active scan
    has been requested; then the scale's scan responses come."""
    scale.wake()
    await inject_advertisement(hass, advertisement(scale))
    for _ in range(int(seconds // 2)):
        if active_scans and scale.awake:
            await wake_scale(hass, scale)
        elif scale.awake:
            await inject_advertisement(hass, advertisement(scale))
        await tick(hass, freezer)


async def test_each_wake_requests_an_active_scan_up_to_the_window(
    hass: HomeAssistant,
    fake_scale: FakeScale,
    active_scans: list[float | None],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The first advertisement of a wake asks for active scanning, once."""
    entry = anna_scale()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 4)
    assert len(active_scans) == 1
    assert active_scans[0] is not None
    assert 0 < active_scans[0] <= 35

    fake_scale.awake = False
    await stay_awake(hass, freezer, fake_scale, 14)
    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 2)
    assert len(active_scans) == 2


async def test_a_recognised_weigh_in_is_synced_once_active_scanning_is_on(
    hass: HomeAssistant,
    fake_scale: FakeScale,
    active_scans: list[float | None],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Passive at first, then the scan responses show the final weigh-in."""
    fake_scale.add_user(slot=2, consent_code=4321)
    entry = anna_scale()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    anna_weighs_in(fake_scale)

    await passive_wake(hass, freezer, fake_scale, active_scans)

    assert fake_scale.connection_attempts == 1
    assert state(hass, WEIGHT).state == "78.4"
    assert not unrecognised_notifications(hass)


async def test_a_wake_without_scan_response_is_not_an_unrecognised_weigh_in(
    hass: HomeAssistant,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Nothing is known about the pending slots: no notification."""
    entry = anna_scale()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fake_scale.add_user(slot=2, consent_code=4321)

    fake_scale.wake()
    for _ in range(int((WAKE_WINDOW // 2))):
        await inject_advertisement(hass, advertisement(fake_scale))
        await tick(hass, freezer)

    assert not unrecognised_notifications(hass)


async def test_a_scan_response_without_slots_is_still_unrecognised(
    hass: HomeAssistant,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An empty list is a list: nobody HA manages was recognised."""
    entry = anna_scale()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fake_scale.add_user(slot=2, consent_code=4321)

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    assert len(unrecognised_notifications(hass)) == 1


async def test_repeated_wakes_without_scan_response_raise_a_repair_that_clears(
    hass: HomeAssistant,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
    active_scans: list[float | None],
) -> None:
    """Three wakes are not enough, the fourth raises it; a scan response clears it."""
    entry = anna_scale()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    async def silent_wake() -> None:
        fake_scale.wake()
        for _ in range(14):
            await inject_advertisement(hass, advertisement(fake_scale))
            await tick(hass, freezer)
        fake_scale.awake = False
        await stay_awake(hass, freezer, fake_scale, 30)

    for _ in range(2):
        await silent_wake()
        assert issue(hass, entry) is None
    await silent_wake()

    raised = issue(hass, entry)
    assert raised is not None
    assert raised.translation_key == "no_scan_response"
    assert raised.is_fixable is False

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 2)
    assert issue(hass, entry) is None


async def test_wakes_with_scan_responses_never_raise_the_repair(
    hass: HomeAssistant,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Healthy wakes count nothing."""
    entry = anna_scale()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for _ in range(5):
        await wake_scale(hass, fake_scale)
        await stay_awake(hass, freezer, fake_scale, 30)
        fake_scale.awake = False
        await stay_awake(hass, freezer, fake_scale, 30)

    assert issue(hass, entry) is None

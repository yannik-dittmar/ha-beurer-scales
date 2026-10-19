"""Advertisements right after HA's disconnect aren't a new wake (#25, ADR 0008).

The scale advertises again ~1.5 s after HA disconnects and goes on for 0-20 s
(protocol doc §9.6). HA merges an address's data, so the first of those
advertisements still shows the slot list from before the sync.
"""

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from . import frames
from .conftest import (
    advertisement,
    inject_advertisement,
    scan_response,
    stay_awake,
    wake_scale,
)
from .fake_scale import FakeReading, FakeScale
from .test_sync import WEIGHT, local_now, state
from .test_create_scale_user import step_off_and_on
from .test_teach_scale_user import SLOT, start_teaching
from .test_unrecognised_weigh_in import setup_anna, unrecognised_notifications


async def sync_annas_weigh_in(hass: HomeAssistant, fake_scale: FakeScale) -> None:
    """Anna weighs in and HA syncs it: one connection."""
    fake_scale.add_user(slot=2, consent_code=4321)
    await setup_anna(hass)
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )
    await wake_scale(hass, fake_scale)
    assert state(hass, WEIGHT).state == "78.4"
    assert fake_scale.connection_attempts == 1


# The sync waits for a quiet period, so time has to run.
@pytest.mark.freeze_time(tick=True)
async def test_the_advertising_after_a_sync_causes_no_connection(
    hass: HomeAssistant, fake_scale: FakeScale, freezer: FrozenDateTimeFactory
) -> None:
    """The stale slot list once, then cleared scan responses for 26 s: nothing."""
    await sync_annas_weigh_in(hass, fake_scale)

    fake_scale.wake()
    await inject_advertisement(hass, advertisement(fake_scale, merged_slots=b"\x02"))
    await inject_advertisement(hass, scan_response(fake_scale))
    await stay_awake(hass, freezer, fake_scale, 26)

    assert fake_scale.connection_attempts == 1
    assert unrecognised_notifications(hass) == []


@pytest.mark.freeze_time(tick=True)
async def test_the_advertising_after_a_teaching_session_is_not_a_weigh_in(
    hass: HomeAssistant,
    loaded_scale: MockConfigEntry,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No notification and no user-list read, although the scale goes on for 26 s."""
    result = await start_teaching(hass, loaded_scale, fake_scale)
    await step_off_and_on(hass, result["flow_id"], fake_scale, SLOT)
    await hass.async_block_till_done()
    assert list(fake_scale.users) == [SLOT]
    attempts = fake_scale.connection_attempts

    fake_scale.wake()
    await inject_advertisement(hass, scan_response(fake_scale))
    await stay_awake(hass, freezer, fake_scale, 26)

    assert fake_scale.connection_attempts == attempts
    assert unrecognised_notifications(hass) == []


@pytest.mark.freeze_time(tick=True)
async def test_someone_stepping_on_right_after_a_sync_is_synced(
    hass: HomeAssistant, fake_scale: FakeScale, freezer: FrozenDateTimeFactory
) -> None:
    """Their slot is appended to a later scan response of the same wake."""
    await sync_annas_weigh_in(hass, fake_scale)

    fake_scale.wake()
    await inject_advertisement(hass, advertisement(fake_scale, merged_slots=b"\x02"))
    await inject_advertisement(hass, scan_response(fake_scale))
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )
    await stay_awake(hass, freezer, fake_scale, 6)
    fake_scale.finish_weigh_in()
    await stay_awake(hass, freezer, fake_scale, 6)

    assert fake_scale.connection_attempts == 2

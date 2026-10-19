"""One Bluetooth session per scale: a flow that holds the scale goes first."""

import asyncio
from datetime import timedelta

import pytest
from homeassistant.components.bluetooth import async_get_advertisement_callback
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.beurer_scales.coordinator import WEIGH_IN_FINAL_DELAY
from custom_components.beurer_scales.wake import WAKE_GAP

from . import frames
from .conftest import (
    SWAPPED_WEIGHT_SCALE_UUID,
    scale_advertisement,
    stay_awake,
    wake_scale,
)
from .fake_scale import FakeReading, FakeScale
from .test_adopt_scale_user import start_adopt
from .test_create_scale_user import CREATE_INPUT, start_create, submit_and_wake
from .test_sync import WEIGHT, anna_scale, local_now, state
from .test_teach_scale_user import until_disconnected


@pytest.fixture
async def anna(hass: HomeAssistant, fake_scale: FakeScale) -> MockConfigEntry:
    """A loaded scale with Anna in slot 2, on the fake scale too."""
    fake_scale.add_user(slot=2, consent_code=4321)
    entry = anna_scale()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# The sync waits for a quiet period, so time has to run.
@pytest.mark.freeze_time(tick=True)
async def test_the_coordinator_leaves_the_scale_to_a_teaching_flow(
    hass: HomeAssistant,
    anna: MockConfigEntry,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Anna's pending slot waits while a new scale user is being taught."""
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )
    result = await start_create(hass, anna)

    # The wake that the flow waits for also lists Anna's slot as pending.
    result = await submit_and_wake(hass, result["flow_id"], fake_scale, CREATE_INPUT)
    assert result["step_id"] == "teach", result
    # Waiting for background tasks would wait for teaching to time out.
    async_get_advertisement_callback(hass)(
        scale_advertisement(service_data=fake_scale.service_data())
    )
    await asyncio.sleep(0.05)

    assert fake_scale.connection_attempts == 1
    assert fake_scale.connected
    assert fake_scale.service_data()[SWAPPED_WEIGHT_SCALE_UUID] == b"\x02"

    hass.config_entries.subentries.async_abort(result["flow_id"])
    await until_disconnected(fake_scale)
    # The scale falls asleep; HA sees no advertisement for a while.
    freezer.tick(timedelta(seconds=WAKE_GAP + 1))
    async_fire_time_changed(hass)

    # Anna's slot was pending before this wake: it waits for the weigh-in.
    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, WEIGH_IN_FINAL_DELAY + 1)

    assert state(hass, WEIGHT).state == "78.4"
    assert fake_scale.service_data()[SWAPPED_WEIGHT_SCALE_UUID] == b""


async def test_a_flow_closed_while_waiting_for_the_wake_lets_go_of_the_scale(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Once the adopt flow is closed, Anna's weigh-in syncs as usual."""
    fake_scale.add_user(slot=3, consent_code=5678)
    result = await start_adopt(hass, anna)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Bob", "slot": 3, "consent_code": 5678}
    )
    hass.config_entries.subentries.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )
    await wake_scale(hass, fake_scale)

    assert state(hass, WEIGHT).state == "78.4"

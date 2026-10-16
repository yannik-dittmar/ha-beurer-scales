"""One Bluetooth session per scale: a flow that holds the scale goes first."""

import asyncio

import pytest
from homeassistant.components.bluetooth import async_get_advertisement_callback
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from . import frames
from .conftest import SWAPPED_WEIGHT_SCALE_UUID, scale_advertisement, wake_scale
from .fake_scale import FakeReading, FakeScale
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


async def test_the_coordinator_leaves_the_scale_to_a_teaching_flow(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
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

    await wake_scale(hass, fake_scale)

    assert state(hass, WEIGHT).state == "78.4"
    assert fake_scale.service_data()[SWAPPED_WEIGHT_SCALE_UUID] == b""

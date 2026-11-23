"""The Awake binary sensor: on from the first advertisement until the scale is silent."""

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.beurer_scales.coordinator import AWAKE_OFF_DELAY

from . import frames
from .conftest import SCALE_ADDRESS, inject_advertisement, scan_response, wake_scale
from .fake_scale import FakeReading, FakeScale
from .test_sync import WEIGHT, local_now, state
from .test_unrecognised_weigh_in import setup_anna


def awake(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", "beurer_scales", f"{SCALE_ADDRESS}_awake"
    )
    assert entity_id is not None
    found = hass.states.get(entity_id)
    assert found is not None
    return found.state


async def let_time_pass(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.freeze_time(tick=True)
async def test_on_when_the_scale_wakes_off_once_it_is_silent(
    hass: HomeAssistant,
    loaded_scale: MockConfigEntry,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A tap: on at the first advertisement, off after the silence."""
    assert awake(hass) == "off"

    fake_scale.wake()
    await inject_advertisement(hass, scan_response(fake_scale))
    assert awake(hass) == "on"

    await let_time_pass(hass, freezer, AWAKE_OFF_DELAY / 2)
    await inject_advertisement(hass, scan_response(fake_scale))
    await let_time_pass(hass, freezer, AWAKE_OFF_DELAY / 2 + 1)
    assert awake(hass) == "on"

    await let_time_pass(hass, freezer, AWAKE_OFF_DELAY)
    assert awake(hass) == "off"


@pytest.mark.freeze_time(tick=True)
async def test_stays_on_through_a_sync(
    hass: HomeAssistant, fake_scale: FakeScale, freezer: FrozenDateTimeFactory
) -> None:
    """HA's session doesn't turn it off; the silence after it does."""
    fake_scale.add_user(slot=2, consent_code=4321)
    await setup_anna(hass)
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )

    await wake_scale(hass, fake_scale)

    assert state(hass, WEIGHT).state == "78.4"
    assert awake(hass) == "on"
    await let_time_pass(hass, freezer, AWAKE_OFF_DELAY + 1)
    assert awake(hass) == "off"

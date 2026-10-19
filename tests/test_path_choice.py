"""Letting every proxy in range report a wake before connecting (#17).

Proxies report the scale's first advertisements at slightly different times.
Connecting at the very first one leaves HA a single path to choose from, which
in the field was a distant proxy whose link broke.
"""

import asyncio

import pytest
from homeassistant.components.bluetooth import async_get_advertisement_callback
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from . import frames
from .conftest import scan_response
from .fake_scale import FakeReading, FakeScale
from .test_create_scale_user import CREATE_INPUT, after_progress, start_create
from .test_sync import WEIGHT, anna, local_now, state

__all__ = ["anna"]  # a fixture


@pytest.fixture
def path_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wait a short, real time for the paths, so a test can look in between."""
    monkeypatch.setattr("custom_components.beurer_scales.connection.PATH_SETTLE", 0.1)


async def advertise(hass: HomeAssistant, scale: FakeScale) -> None:
    """Step on the scale without waiting for what HA starts in response.

    A weigh-in in progress is appended to the second scan response.
    """
    scale.wake()
    async_get_advertisement_callback(hass)(scan_response(scale))
    scale.finish_weigh_in()
    async_get_advertisement_callback(hass)(scan_response(scale))
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.usefixtures("path_settle", "anna")
async def test_a_sync_connects_once_the_paths_settled(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """Anna's pending slot shows at once; HA still waits before connecting."""
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )

    await advertise(hass, fake_scale)
    assert fake_scale.connection_attempts == 0

    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake_scale.connection_attempts == 1
    assert state(hass, WEIGHT).state == "78.4"


@pytest.mark.usefixtures("path_settle")
async def test_a_flow_connects_once_the_paths_settled(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A flow waiting for the wake doesn't connect at the first advertisement."""
    result = await start_create(hass, loaded_scale)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], CREATE_INPUT
    )

    await advertise(hass, fake_scale)
    assert fake_scale.connection_attempts == 0

    result = await after_progress(hass, result["flow_id"], "create_wake")
    assert fake_scale.connection_attempts == 1
    assert result["type"] is FlowResultType.SHOW_PROGRESS

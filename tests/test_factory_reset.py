"""Factory-resetting the scale from its configure menu (#30)."""

import asyncio
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beurer_scales.connection import WakeTimeout
from custom_components.beurer_scales.pending_deletions import PendingDeletion
from custom_components.beurer_scales.protocol.models import BF450
from custom_components.beurer_scales.protocol.session import open_session

from .conftest import wake_scale
from .fake_scale import FACTORY_RESET, FakeScale
from .test_create_scale_user import CREATE_INPUT, create_with_one_reading, start_create
from .test_sync import anna

__all__ = ["anna"]  # a fixture


async def open_reset(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Open the configure menu and choose "Factory reset the scale"."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "factory_reset"}
    )


async def after_wake_step(hass: HomeAssistant, flow_id: str) -> dict[str, Any]:
    """Let the flow's progress step run to its end; return the next result."""
    for _ in range(2000):
        step_id = hass.config_entries.options.async_get(flow_id)["step_id"]
        if step_id != "factory_reset_wake":
            break
        await asyncio.sleep(0.001)
    return await hass.config_entries.options.async_configure(flow_id)


async def reset_through(
    hass: HomeAssistant, entry: MockConfigEntry, scale: FakeScale
) -> dict[str, Any]:
    """Confirm the reset, step on the scale and run the flow to its end."""
    result = await open_reset(hass, entry)
    flow_id = result["flow_id"]
    result = await hass.config_entries.options.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "factory_reset_wake"
    await wake_scale(hass, scale)
    return await after_wake_step(hass, flow_id)


async def test_the_session_writes_zero_to_the_reset_characteristic(
    fake_scale: FakeScale,
) -> None:
    """FFF7 `00` wipes the scale users; the scale ends the link and loses its bond."""
    fake_scale.add_user(slot=1, consent_code=1234)
    fake_scale.wake()

    async with open_session(fake_scale.ble_device, BF450) as session:
        await session.factory_reset()

    assert (FACTORY_RESET, b"\x00", True) in fake_scale.writes
    assert fake_scale.users == {}
    assert fake_scale.bond_stale and fake_scale.proxy_bond_stale


async def test_the_configure_menu_offers_the_factory_reset(
    hass: HomeAssistant, loaded_scale: MockConfigEntry
) -> None:
    """The menu lists the options and the reset; the reset warns before it acts."""
    result = await hass.config_entries.options.async_init(loaded_scale.entry_id)
    assert result["menu_options"] == ["options", "factory_reset"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "factory_reset"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "factory_reset"


async def test_nothing_happens_until_the_reset_is_confirmed(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Opening the step and walking away leaves the scale alone."""
    fake_scale.add_user(slot=1, consent_code=1234)
    result = await open_reset(hass, loaded_scale)
    hass.config_entries.options.async_abort(result["flow_id"])
    await wake_scale(hass, fake_scale)
    await hass.async_block_till_done()

    assert fake_scale.resets == 0
    assert fake_scale.connection_attempts == 0


async def test_the_reset_clears_the_scale_and_the_proxy_bond(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """The scale is wiped, and the bond it forgot is removed on the path used."""
    result = await reset_through(hass, anna, fake_scale)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "factory_reset_done"
    assert fake_scale.resets == 1
    assert fake_scale.users == {}
    assert fake_scale.proxy_unpairs == 1
    assert not fake_scale.proxy_bond_stale
    assert not fake_scale.connected


async def test_the_reset_removes_the_scale_users_and_their_pending_deletions(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Their slots are gone: no subentry stays and nothing is queued for deletion."""
    await anna.runtime_data.pending_deletions.async_add(PendingDeletion(5, 1111))
    assert anna.runtime_data.pending_deletions

    await reset_through(hass, anna, fake_scale)
    await hass.async_block_till_done()

    assert anna.subentries == {}
    # The queue is empty after the reload the removal triggers, too.
    assert not anna.runtime_data.pending_deletions
    await hass.config_entries.async_reload(anna.entry_id)
    assert not anna.runtime_data.pending_deletions


async def test_a_scale_user_can_be_created_after_the_reset(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """The stale bond is gone, so the next connection bonds and creates at once."""
    await reset_through(hass, anna, fake_scale)
    await hass.async_block_till_done()
    fake_scale.sleep()

    result = await start_create(hass, anna)
    result = await create_with_one_reading(
        hass, result["flow_id"], fake_scale, CREATE_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert fake_scale.proxy_unpairs == 1
    assert list(fake_scale.users) == [1]


async def test_a_wake_that_never_comes_changes_nothing(
    hass: HomeAssistant,
    anna: MockConfigEntry,
    fake_scale: FakeScale,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If nobody steps on the scale, the flow ends and keeps the scale users."""

    async def wake_times_out(*_args: Any, **_kwargs: Any) -> None:
        raise WakeTimeout

    monkeypatch.setattr(
        "custom_components.beurer_scales.factory_reset.async_wait_for_wake",
        wake_times_out,
    )
    result = await open_reset(hass, anna)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        result = await after_wake_step(hass, result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wake_timeout"
    assert len(anna.subentries) == 1
    assert fake_scale.resets == 0

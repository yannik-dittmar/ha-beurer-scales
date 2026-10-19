"""Teaching a new scale user its weight at the end of the create flow."""

import asyncio
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beurer_scales.pending_deletions import PendingDeletion

from .conftest import SCALE_ADDRESS
from .fake_scale import FakeScale
from .test_adopt_scale_user import only_subentry
from .test_create_scale_user import (
    CREATE_INPUT,
    after_progress,
    start_create,
    step_off_and_on,
    submit_and_wake,
)
from .test_sync import state

# The fake scale has no scale users, so the new one lands in slot 1.
SLOT = 1
WEIGHT = f"{SCALE_ADDRESS}_slot_{SLOT}_weight"
LAST_READING = f"{SCALE_ADDRESS}_slot_{SLOT}_last_reading"


@pytest.fixture
def short_teaching_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give up waiting for a teaching reading after a fraction of a second."""
    monkeypatch.setattr(
        "custom_components.beurer_scales.protocol.teaching.TEACHING_TIMEOUT", 0.2
    )


async def start_teaching(
    hass: HomeAssistant, entry: MockConfigEntry, scale: FakeScale
) -> dict[str, Any]:
    """Create a scale user up to the first teaching reading."""
    result = await start_create(hass, entry)
    return await submit_and_wake(hass, result["flow_id"], scale, CREATE_INPUT)


async def choose(hass: HomeAssistant, flow_id: str, option: str) -> dict[str, Any]:
    """Pick an option of the menu after a teaching reading."""
    return await hass.config_entries.subentries.async_configure(
        flow_id, {"next_step_id": option}
    )


async def until_disconnected(scale: FakeScale) -> None:
    """HA doesn't wait for a cancelled task; let its cleanup talk to the scale."""
    for _ in range(1000):
        if not scale.connected:
            return
        await asyncio.sleep(0.001)


async def test_teaching_counts_up_to_three_readings(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Each live reading advances "reading k of 3"; the third one finishes."""
    result = await start_teaching(hass, loaded_scale, fake_scale)
    flow_id = result["flow_id"]

    for reading in ("1", "2"):
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["progress_action"] == "teach"
        assert result["description_placeholders"] == {
            "reading": reading,
            "readings": "3",
        }
        assert fake_scale.connected

        result = await step_off_and_on(hass, flow_id, fake_scale, SLOT)

        assert result["type"] is FlowResultType.MENU
        assert result["step_id"] == "teach_more"
        assert result["menu_options"] == ["teach", "teach_done"]
        assert result["description_placeholders"] == {
            "reading": reading,
            "readings": "3",
        }
        result = await choose(hass, flow_id, "teach")

    assert result["description_placeholders"] == {"reading": "3", "readings": "3"}
    result = await step_off_and_on(hass, flow_id, fake_scale, SLOT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result.get("description") is None
    assert only_subentry(loaded_scale).unique_id == f"slot_{SLOT}"
    assert list(fake_scale.users) == [SLOT]
    assert not fake_scale.connected


async def test_teaching_can_finish_after_one_reading(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """The new household member may stop after a single reading."""
    result = await start_teaching(hass, loaded_scale, fake_scale)
    await step_off_and_on(hass, result["flow_id"], fake_scale, SLOT)

    result = await choose(hass, result["flow_id"], "teach_done")

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert only_subentry(loaded_scale).unique_id == f"slot_{SLOT}"
    assert list(fake_scale.users) == [SLOT]
    assert fake_scale.deleted_while_connected == []
    assert not fake_scale.connected


async def test_teaching_readings_reach_the_new_scale_users_sensors(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A teaching reading is a fresh reading of the new scale user."""
    result = await start_teaching(hass, loaded_scale, fake_scale)
    await step_off_and_on(hass, result["flow_id"], fake_scale, SLOT)
    await choose(hass, result["flow_id"], "teach_done")
    await hass.async_block_till_done()

    assert state(hass, WEIGHT).state == "78.4"
    assert state(hass, LAST_READING).state not in ("unknown", "unavailable")


@pytest.mark.usefixtures("short_teaching_timeout")
async def test_teaching_without_a_reading_times_out_and_deletes_the_scale_user(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Nobody steps on again: the scale user is removed and the flow says why."""
    result = await start_teaching(hass, loaded_scale, fake_scale)

    result = await after_progress(hass, result["flow_id"], "teach")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "teaching_timeout"
    assert fake_scale.users == {}
    assert fake_scale.deleted_while_connected == [SLOT]
    assert loaded_scale.subentries == {}
    assert not fake_scale.connected


@pytest.mark.usefixtures("short_teaching_timeout")
async def test_a_timeout_after_one_reading_keeps_the_scale_user(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """One reading taught the scale enough; the flow ends and says so."""
    result = await start_teaching(hass, loaded_scale, fake_scale)
    await step_off_and_on(hass, result["flow_id"], fake_scale, SLOT)
    await choose(hass, result["flow_id"], "teach")

    result = await after_progress(hass, result["flow_id"], "teach")

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["description"] == "teaching_timeout"
    assert only_subentry(loaded_scale).unique_id == f"slot_{SLOT}"
    assert list(fake_scale.users) == [SLOT]
    assert not fake_scale.connected


@pytest.mark.usefixtures("short_teaching_timeout")
async def test_a_timeout_while_the_menu_is_open_keeps_the_scale_user(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Teaching ends while nobody answers the menu; closing it then keeps the user."""
    result = await start_teaching(hass, loaded_scale, fake_scale)
    await step_off_and_on(hass, result["flow_id"], fake_scale, SLOT)
    await until_disconnected(fake_scale)
    assert not fake_scale.connected

    hass.config_entries.subentries.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    assert only_subentry(loaded_scale).unique_id == f"slot_{SLOT}"
    assert list(fake_scale.users) == [SLOT]
    assert state(hass, WEIGHT).state == "78.4"


async def test_aborting_during_teaching_deletes_the_scale_user(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Closing the flow while teaching removes the new scale user from the scale."""
    result = await start_teaching(hass, loaded_scale, fake_scale)
    await step_off_and_on(hass, result["flow_id"], fake_scale, SLOT)

    hass.config_entries.subentries.async_abort(result["flow_id"])
    await until_disconnected(fake_scale)

    assert fake_scale.users == {}
    assert fake_scale.deleted_while_connected == [SLOT]
    assert loaded_scale.subentries == {}
    assert not fake_scale.connected


async def test_a_lost_link_after_one_reading_keeps_the_scale_user(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """The link breaks while waiting for the second reading: teaching ends at
    once with what it has, instead of waiting on a dead connection (#17)."""
    result = await start_teaching(hass, loaded_scale, fake_scale)
    await step_off_and_on(hass, result["flow_id"], fake_scale, SLOT)
    await choose(hass, result["flow_id"], "teach")

    fake_scale.drop_link()
    result = await after_progress(hass, result["flow_id"], "teach")

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["description"] == "teaching_connection_lost"
    assert only_subentry(loaded_scale).unique_id == f"slot_{SLOT}"
    assert list(fake_scale.users) == [SLOT]


async def test_a_lost_link_before_the_first_reading_deletes_at_the_next_wake(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Without a reading the scale user isn't wanted, but it can't be deleted
    without a link: the deletion waits for the next wake."""
    result = await start_teaching(hass, loaded_scale, fake_scale)
    consent_code = fake_scale.users[SLOT].consent_code

    fake_scale.drop_link()
    result = await after_progress(hass, result["flow_id"], "teach")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "teaching_connection_lost"
    assert loaded_scale.subentries == {}
    assert loaded_scale.runtime_data.pending_deletions.all() == [
        PendingDeletion(slot=SLOT, consent_code=consent_code)
    ]

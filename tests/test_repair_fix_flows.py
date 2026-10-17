"""Fixing a scale user's repair: enter the code again, create again, or remove."""

from datetime import date
from types import MappingProxyType
from typing import Any

import pytest
from homeassistant.components.repairs import FlowType, repairs_flow_manager
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beurer_scales.const import DOMAIN

from .conftest import SCALE_ADDRESS, wake_scale
from .fake_scale import FakeScale
from .test_create_scale_user import step_off_and_on, submit_and_wake
from .test_scale_user_repairs import (
    anna_id,
    scale_issues,
    setup_anna,
    wake_with_pending,
    weigh_in_anna,
)
from .test_sync import ANNA, WEIGHT, entity_id, state


@pytest.fixture(autouse=True)
async def repairs(hass: HomeAssistant) -> None:
    """Load the repairs integration, which runs fix flows."""
    assert await async_setup_component(hass, "repairs", {})


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let every wake try to sync, so only an open repair holds a sync back."""
    monkeypatch.setattr("custom_components.beurer_scales.coordinator.RETRY_DELAY", 0.0)


async def wrong_code(hass: HomeAssistant, scale: FakeScale) -> MockConfigEntry:
    """Anna's slot 2 now has code 9999 on the scale; a sync raised the repair."""
    scale.add_user(slot=2, consent_code=9999, height_cm=168)
    entry = await setup_anna(hass)
    weigh_in_anna(scale)
    await wake_scale(hass, scale)
    assert f"wrong_consent_code_{anna_id(entry)}" in scale_issues(ir.async_get(hass))
    return entry


async def deleted(hass: HomeAssistant, scale: FakeScale) -> MockConfigEntry:
    """Anna's slot 2 is gone from the scale; a sync raised the repair."""
    scale.add_user(slot=1, consent_code=1111)
    entry = await setup_anna(hass)
    await wake_with_pending(hass, scale, 2)
    assert f"scale_user_deleted_{anna_id(entry)}" in scale_issues(ir.async_get(hass))
    return entry


async def factory_reset(hass: HomeAssistant, scale: FakeScale) -> MockConfigEntry:
    """The scale was reset; a sync raised the repair."""
    entry = await setup_anna(hass)
    scale.factory_reset()
    await wake_with_pending(hass, scale, 2)
    assert f"factory_reset_{anna_id(entry)}" in scale_issues(ir.async_get(hass))
    return entry


async def start_fix(hass: HomeAssistant, issue_id: str) -> dict[str, Any]:
    """Open the fix flow of a repair issue."""
    manager = repairs_flow_manager(hass)
    assert manager is not None
    result: dict[str, Any] = await manager.async_init(
        DOMAIN, context={"issue_id": issue_id}
    )
    return result


async def choose(hass: HomeAssistant, flow_id: str, option: str) -> dict[str, Any]:
    """Pick an option in a fix flow's menu."""
    manager = repairs_flow_manager(hass)
    assert manager is not None
    result: dict[str, Any] = await manager.async_configure(
        flow_id, {"next_step_id": option}
    )
    return result


async def forwarded_flow(
    hass: HomeAssistant, issue_id: str, option: str
) -> dict[str, Any]:
    """Choose a fix option that continues in a scale user subentry flow."""
    result = await choose(hass, (await start_fix(hass, issue_id))["flow_id"], option)
    assert result["type"] is FlowResultType.ABORT
    flow_type, flow_id = result["next_flow"]
    assert flow_type is FlowType.CONFIG_SUBENTRIES_FLOW
    return hass.config_entries.subentries.async_get(flow_id)


async def test_wrong_code_repair_offers_to_reenter_the_code_create_or_remove(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """The scale user still exists, so the code can be entered again."""
    entry = await wrong_code(hass, fake_scale)

    result = await start_fix(hass, f"wrong_consent_code_{anna_id(entry)}")

    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["adopt", "create", "remove"]


@pytest.mark.parametrize(
    ("situation", "kind"),
    [(deleted, "scale_user_deleted"), (factory_reset, "factory_reset")],
)
async def test_a_scale_user_gone_from_the_scale_can_be_created_or_removed(
    hass: HomeAssistant, fake_scale: FakeScale, situation: Any, kind: str
) -> None:
    """Without the scale user on the scale there is no code to enter."""
    entry = await situation(hass, fake_scale)

    result = await start_fix(hass, f"{kind}_{anna_id(entry)}")

    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["create", "remove"]


async def test_remove_removes_the_scale_user_and_clears_the_repair(
    hass: HomeAssistant, fake_scale: FakeScale, device_registry: dr.DeviceRegistry
) -> None:
    """Removing drops the subentry, its device and the repair."""
    entry = await deleted(hass, fake_scale)
    result = await start_fix(hass, f"scale_user_deleted_{anna_id(entry)}")

    result = await choose(hass, result["flow_id"], "remove")
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.subentries == {}
    assert scale_issues(ir.async_get(hass)) == {}
    assert (
        device_registry.async_get_devices(
            identifiers={(DOMAIN, f"{SCALE_ADDRESS}_slot_2")}
        )
        == []
    )


async def test_removing_the_scale_user_elsewhere_clears_the_repair(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """A scale user removed from the scale's entry takes its repair along."""
    entry = await deleted(hass, fake_scale)

    hass.config_entries.async_remove_subentry(entry, anna_id(entry))
    await hass.async_block_till_done()

    assert scale_issues(ir.async_get(hass)) == {}


async def test_reentering_the_right_code_fixes_the_scale_user(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """The adopt path verifies the new code at a wake and takes the readings."""
    entry = await wrong_code(hass, fake_scale)
    subentry_id = anna_id(entry)
    flow = await forwarded_flow(hass, f"wrong_consent_code_{subentry_id}", "adopt")
    assert flow["step_id"] == "repair_adopt"

    result = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"consent_code": 9999}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "wake"
    await wake_scale(hass, fake_scale)
    result = await hass.config_entries.subentries.async_configure(flow["flow_id"])
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "scale_user_repaired"
    subentry = entry.subentries[subentry_id]
    assert subentry.data["consent_code"] == 9999
    assert subentry.data["profile"]["height_cm"] == 168
    assert scale_issues(ir.async_get(hass)) == {}
    # The reading the scale delivered on consent reaches Anna's sensors.
    assert state(hass, WEIGHT).state == "78.4"


async def test_reentering_a_wrong_code_again_shows_an_error(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """A rejected code goes back to the form; the repair stays open."""
    entry = await wrong_code(hass, fake_scale)
    flow = await forwarded_flow(hass, f"wrong_consent_code_{anna_id(entry)}", "adopt")

    await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"consent_code": 1234}
    )
    await wake_scale(hass, fake_scale)
    result = await hass.config_entries.subentries.async_configure(flow["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "repair_adopt"
    assert result["errors"] == {"consent_code": "invalid_consent_code"}
    assert len(scale_issues(ir.async_get(hass))) == 1
    assert entry.subentries[anna_id(entry)].data["consent_code"] == 4321


async def recreate_and_teach(
    hass: HomeAssistant, flow_id: str, scale: FakeScale
) -> dict[str, Any]:
    """Confirm creating again, wake the scale, teach once and finish."""
    result = await submit_and_wake(hass, flow_id, scale, {})
    assert result["step_id"] == "teach", result
    assert scale.client is not None
    assert scale.client.consented_slot is not None
    result = await step_off_and_on(hass, flow_id, scale, scale.client.consented_slot)
    assert result["step_id"] == "teach_more", result
    result = await hass.config_entries.subentries.async_configure(
        flow_id, {"next_step_id": "teach_done"}
    )
    await hass.async_block_till_done()
    return result


async def test_create_again_recreates_and_teaches_the_scale_user(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """The scale user is registered again with the stored profile and taught."""
    entry = await deleted(hass, fake_scale)
    subentry_id = anna_id(entry)
    flow = await forwarded_flow(hass, f"scale_user_deleted_{subentry_id}", "create")
    assert flow["step_id"] == "repair_create"

    result = await recreate_and_teach(hass, flow["flow_id"], fake_scale)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "scale_user_repaired"
    subentry = entry.subentries[subentry_id]
    assert subentry.title == "Anna"
    # Slot 1 is taken, so the scale gives Anna slot 2 again.
    assert subentry.data["slot"] == 2
    created = fake_scale.users[2]
    assert subentry.data["consent_code"] == created.consent_code
    assert created.birth_date == date(1985, 4, 12)
    assert created.height_cm == 180
    assert scale_issues(ir.async_get(hass)) == {}
    assert not fake_scale.connected
    # The teaching reading reaches Anna's sensors.
    assert state(hass, WEIGHT).state == "78.4"


async def test_create_again_into_another_slot_keeps_the_entities(
    hass: HomeAssistant, fake_scale: FakeScale, device_registry: dr.DeviceRegistry
) -> None:
    """After a reset Anna lands in slot 1; her device and sensors move along."""
    entry = await factory_reset(hass, fake_scale)
    subentry_id = anna_id(entry)
    weight_entity = entity_id(hass, WEIGHT)
    flow = await forwarded_flow(hass, f"factory_reset_{subentry_id}", "create")

    await recreate_and_teach(hass, flow["flow_id"], fake_scale)

    subentry = entry.subentries[subentry_id]
    assert subentry.data["slot"] == 1
    assert subentry.unique_id == "slot_1"
    assert set(fake_scale.users) == {1}
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{SCALE_ADDRESS}_slot_1_weight")
        == weight_entity
    )
    [device] = [
        d
        for d in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        if d.config_subentry_id == subentry_id
    ]
    assert device.identifiers == {(DOMAIN, f"{SCALE_ADDRESS}_slot_1")}


async def test_create_again_into_a_slot_another_scale_user_holds_is_undone(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """The new scale user is deleted again rather than clashing with Ben's slot."""
    entry = await factory_reset(hass, fake_scale)
    anna = anna_id(entry)
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType({**ANNA, "slot": 1, "name": "Ben"}),
            subentry_type="scale_user",
            title="Ben",
            unique_id="slot_1",
        ),
    )
    await hass.async_block_till_done()
    flow = await forwarded_flow(hass, f"factory_reset_{anna}", "create")

    result = await submit_and_wake(hass, flow["flow_id"], fake_scale, {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "slot_held_by_other_scale_user"
    assert fake_scale.users == {}
    assert fake_scale.deleted_while_connected == [1]
    assert f"factory_reset_{anna}" in scale_issues(ir.async_get(hass))

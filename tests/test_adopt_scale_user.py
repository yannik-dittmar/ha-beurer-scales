"""Adding a scale user by adopting an existing slot."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.components.bluetooth import async_get_advertisement_callback
from homeassistant.config_entries import SOURCE_USER, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.beurer_scales.const import SUBENTRY_SCALE_USER

from .conftest import SCALE_ADDRESS, scale_advertisement, wake_scale
from .fake_scale import FakeScale

ADOPT_INPUT = {"name": "Anna", "slot": 2, "consent_code": 4321}


async def start_flow(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Open "Add scale user" on the scale's entry."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_SCALE_USER), context={"source": SOURCE_USER}
    )


async def start_adopt(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Open "Add scale user" and choose "Adopt existing slot"."""
    result = await start_flow(hass, entry)
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "adopt"}
    )


async def submit_and_wake(
    hass: HomeAssistant, flow_id: str, scale: FakeScale, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Submit the adopt form, step on the scale and return the next flow result."""
    result = await hass.config_entries.subentries.async_configure(flow_id, user_input)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "wake"
    await wake_scale(hass, scale)
    return await hass.config_entries.subentries.async_configure(flow_id)


def only_subentry(entry: MockConfigEntry) -> ConfigSubentry:
    """The single scale user of a scale."""
    [subentry] = entry.subentries.values()
    return subentry


async def test_add_scale_user_offers_create_and_adopt(
    hass: HomeAssistant, loaded_scale: MockConfigEntry
) -> None:
    """The flow opens with a menu of the two ways to add a scale user."""
    result = await start_flow(hass, loaded_scale)

    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["create", "adopt"]


async def test_adopt_reads_the_profile_and_creates_the_scale_user(
    hass: HomeAssistant,
    loaded_scale: MockConfigEntry,
    fake_scale: FakeScale,
    device_registry: dr.DeviceRegistry,
) -> None:
    """With the right code, the scale user is created with the profile from the scale."""
    fake_scale.add_user(slot=1, consent_code=1111)
    fake_scale.add_user(slot=2, consent_code=4321, height_cm=168, gender="female")
    result = await start_adopt(hass, loaded_scale)
    assert result["type"] is FlowResultType.FORM

    result = await submit_and_wake(hass, result["flow_id"], fake_scale, ADOPT_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = only_subentry(loaded_scale)
    assert subentry.unique_id == "slot_2"
    assert subentry.title == "Anna"
    assert dict(subentry.data) == {
        "slot": 2,
        "consent_code": 4321,
        "name": "Anna",
        "person": None,
        "profile": {
            "birth_date": "1985-04-12",
            "height_cm": 168,
            "gender": "female",
            "activity_level": 3,
        },
    }
    # The scale's clock was set while connected.
    assert fake_scale.clock is not None
    assert not fake_scale.connected


async def test_scale_user_device_is_linked_to_the_scale(
    hass: HomeAssistant,
    loaded_scale: MockConfigEntry,
    fake_scale: FakeScale,
    device_registry: dr.DeviceRegistry,
) -> None:
    """The scale user gets its own device, connected via the scale device."""
    fake_scale.add_user(slot=2, consent_code=4321)
    result = await start_adopt(hass, loaded_scale)
    await submit_and_wake(hass, result["flow_id"], fake_scale, ADOPT_INPUT)
    await hass.async_block_till_done()

    [scale_device] = device_registry.async_get_devices(
        connections={(dr.CONNECTION_BLUETOOTH, SCALE_ADDRESS)}
    )
    subentry = only_subentry(loaded_scale)
    [user_device] = [
        device
        for device in dr.async_entries_for_config_entry(
            device_registry, loaded_scale.entry_id
        )
        if device.id != scale_device.id
    ]
    assert user_device.name == "Anna"
    assert user_device.via_device_id == scale_device.id
    assert user_device.config_subentry_id == subentry.subentry_id


async def test_name_falls_back_to_the_persons_name(
    hass: HomeAssistant,
    loaded_scale: MockConfigEntry,
    fake_scale: FakeScale,
    entity_registry: er.EntityRegistry,
) -> None:
    """With a person and no name, the scale user is named after the person."""
    assert await async_setup_component(
        hass, "person", {"person": [{"id": "anna", "name": "Anna Example"}]}
    )
    person = entity_registry.async_get("person.anna_example")
    assert person is not None
    fake_scale.add_user(slot=2, consent_code=4321)
    result = await start_adopt(hass, loaded_scale)

    result = await submit_and_wake(
        hass,
        result["flow_id"],
        fake_scale,
        {"person": "person.anna_example", "slot": 2, "consent_code": 4321},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = only_subentry(loaded_scale)
    assert subentry.title == "Anna Example"
    assert subentry.data["name"] == "Anna Example"
    assert subentry.data["person"] == person.id


async def test_person_or_name_is_required(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Leaving both the person and the name empty shows an error."""
    result = await start_adopt(hass, loaded_scale)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": " ", "slot": 2, "consent_code": 4321}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "name_required"}
    assert fake_scale.connection_attempts == 0


async def test_name_must_be_unique_on_this_scale(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A name another scale user of this scale already has shows an error."""
    fake_scale.add_user(slot=1, consent_code=1111)
    fake_scale.add_user(slot=2, consent_code=4321)
    result = await start_adopt(hass, loaded_scale)
    await submit_and_wake(
        hass,
        result["flow_id"],
        fake_scale,
        {**ADOPT_INPUT, "slot": 1, "consent_code": 1111},
    )

    result = await start_adopt(hass, loaded_scale)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**ADOPT_INPUT, "name": "anna"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"name": "name_in_use"}


async def test_slot_already_managed_shows_an_error(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A slot that already is a scale user of this scale can't be adopted twice."""
    fake_scale.add_user(slot=2, consent_code=4321)
    result = await start_adopt(hass, loaded_scale)
    await submit_and_wake(hass, result["flow_id"], fake_scale, ADOPT_INPUT)

    result = await start_adopt(hass, loaded_scale)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**ADOPT_INPUT, "name": "Ben"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"slot": "slot_in_use"}


async def test_wrong_consent_code_shows_an_error(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A code the scale rejects returns to the form and creates nothing."""
    fake_scale.add_user(slot=2, consent_code=4321)
    result = await start_adopt(hass, loaded_scale)

    result = await submit_and_wake(
        hass, result["flow_id"], fake_scale, {**ADOPT_INPUT, "consent_code": 1234}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "adopt"
    assert result["errors"] == {"consent_code": "invalid_consent_code"}
    assert loaded_scale.subentries == {}
    assert not fake_scale.connected


async def test_missing_slot_shows_an_error(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A slot that holds no scale user returns to the form and creates nothing."""
    fake_scale.add_user(slot=1, consent_code=1111)
    result = await start_adopt(hass, loaded_scale)

    result = await submit_and_wake(hass, result["flow_id"], fake_scale, ADOPT_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"slot": "slot_not_found"}
    assert loaded_scale.subentries == {}
    assert not fake_scale.connected


async def test_corrected_input_after_an_error_creates_the_scale_user(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """After an error the user can fix the code and wake the scale again."""
    fake_scale.add_user(slot=2, consent_code=4321)
    result = await start_adopt(hass, loaded_scale)
    result = await submit_and_wake(
        hass, result["flow_id"], fake_scale, {**ADOPT_INPUT, "consent_code": 1234}
    )

    result = await submit_and_wake(hass, result["flow_id"], fake_scale, ADOPT_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert only_subentry(loaded_scale).unique_id == "slot_2"


async def test_scale_that_does_not_wake_aborts(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Nobody steps on the scale for three minutes: the flow gives up."""
    result = await start_adopt(hass, loaded_scale)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], ADOPT_INPUT
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=3, seconds=1))
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wake_timeout"
    assert fake_scale.connection_attempts == 0


async def test_path_without_pairing_aborts(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A Bluetooth path that can't bond ends the flow with a clear reason."""
    fake_scale.add_user(slot=2, consent_code=4321)
    fake_scale.pairing = "unsupported"
    result = await start_adopt(hass, loaded_scale)

    result = await submit_and_wake(hass, result["flow_id"], fake_scale, ADOPT_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pairing_unsupported"
    assert loaded_scale.subentries == {}


async def test_silent_scale_aborts_and_disconnects(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A scale that stops answering ends the flow and the connection."""
    fake_scale.add_user(slot=2, consent_code=4321)
    fake_scale.answers_ucp = False
    result = await start_adopt(hass, loaded_scale)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.beurer_scales.protocol.session.RESPONSE_TIMEOUT", 0.01
        )
        result = await submit_and_wake(hass, result["flow_id"], fake_scale, ADOPT_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
    assert not fake_scale.connected


async def test_aborting_while_connected_disconnects(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Closing the flow mid-session still closes the connection."""
    fake_scale.add_user(slot=2, consent_code=4321)
    fake_scale.answers_ucp = False
    result = await start_adopt(hass, loaded_scale)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], ADOPT_INPUT
    )
    fake_scale.wake()
    # Don't wait for the session: it hangs on the consent the scale never answers.
    async_get_advertisement_callback(hass)(scale_advertisement())
    for _ in range(100):
        if fake_scale.ucp_requests:
            break
        await asyncio.sleep(0)
    assert fake_scale.connected

    hass.config_entries.subentries.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    assert not fake_scale.connected
    assert loaded_scale.subentries == {}


async def test_aborting_while_waiting_never_connects(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Closing the flow before the wake stops waiting for the scale."""
    fake_scale.add_user(slot=2, consent_code=4321)
    result = await start_adopt(hass, loaded_scale)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], ADOPT_INPUT
    )

    hass.config_entries.subentries.async_abort(result["flow_id"])
    await wake_scale(hass, fake_scale)

    assert fake_scale.connection_attempts == 0

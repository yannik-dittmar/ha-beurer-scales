"""Changing a scale user's name, person or profile (reconfigure)."""

from datetime import date, timedelta
from typing import Any

import pytest
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

from . import frames
from .conftest import (
    SCALE_ADDRESS,
    inject_advertisement,
    scale_advertisement,
    wake_scale,
)
from .fake_scale import ACTIVITY_LEVEL, FakeReading, FakeScale
from .test_adopt_scale_user import only_subentry
from .test_sync import ANNA, WEIGHT, anna_scale, local_now, state

ANNA_FORM = {
    "name": "Anna",
    "birth_date": "1985-04-12",
    "height_cm": 180,
    "gender": "male",
    "activity_level": "3",
}


@pytest.fixture
async def anna(hass: HomeAssistant, fake_scale: FakeScale) -> MockConfigEntry:
    """A loaded scale with Anna in slot 2, on the fake scale too."""
    fake_scale.add_user(slot=2, consent_code=4321)
    entry = anna_scale()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def start_reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry
) -> dict[str, Any]:
    """Open "Reconfigure" on the scale's only scale user."""
    return await entry.start_subentry_reconfigure_flow(
        hass, only_subentry(entry).subentry_id
    )


async def submit(
    hass: HomeAssistant, flow_id: str, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Submit the reconfigure form."""
    return await hass.config_entries.subentries.async_configure(flow_id, user_input)


async def submit_and_wake(
    hass: HomeAssistant, flow_id: str, scale: FakeScale, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Submit a profile change, step on the scale and return the next result."""
    result = await submit(hass, flow_id, user_input)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "reconfigure"
    await wake_scale(hass, scale)
    return await hass.config_entries.subentries.async_configure(flow_id)


def user_device(hass: HomeAssistant) -> dr.DeviceEntry:
    """Anna's device."""
    [device] = dr.async_get(hass).async_get_devices(
        identifiers={("beurer_scales", f"{SCALE_ADDRESS}_slot_2")}
    )
    return device


async def test_form_shows_the_current_name_and_profile(
    hass: HomeAssistant, anna: MockConfigEntry
) -> None:
    """The form starts from what the subentry holds."""
    result = await start_reconfigure(hass, anna)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    suggested = {
        key.schema: key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }
    assert suggested == ANNA_FORM


async def test_renaming_needs_no_scale_and_renames_the_device(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A name-only change is saved at once; the device name follows."""
    result = await start_reconfigure(hass, anna)

    result = await submit(hass, result["flow_id"], {**ANNA_FORM, "name": "Annie"})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    subentry = only_subentry(anna)
    assert subentry.title == "Annie"
    assert dict(subentry.data) == {**ANNA, "name": "Annie"}
    assert user_device(hass).name == "Annie"
    assert fake_scale.connection_attempts == 0


async def test_linking_a_person_needs_no_scale(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Choosing a person and leaving the name empty takes the person's name."""
    assert await async_setup_component(
        hass,
        "person",
        {"person": [{"id": "lena", "name": "Lena"}]},
    )
    person = er.async_get(hass).async_get("person.lena")
    assert person is not None
    result = await start_reconfigure(hass, anna)

    result = await submit(
        hass, result["flow_id"], {**ANNA_FORM, "name": "", "person": "person.lena"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    subentry = only_subentry(anna)
    assert subentry.title == "Lena"
    assert subentry.data["person"] == person.id
    assert user_device(hass).name == "Lena"
    assert fake_scale.connection_attempts == 0


async def test_keeping_the_name_is_not_a_name_conflict(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Submitting the unchanged form doesn't clash with the scale user itself."""
    result = await start_reconfigure(hass, anna)

    result = await submit(hass, result["flow_id"], ANNA_FORM)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert dict(only_subentry(anna).data) == ANNA
    assert fake_scale.connection_attempts == 0


async def test_another_scale_users_name_is_refused(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """Names stay unique per scale."""
    entry = MockConfigEntry(
        domain="beurer_scales",
        title="BF450",
        unique_id=SCALE_ADDRESS,
        data={"address": SCALE_ADDRESS, "model": "BF450"},
        subentries_data=[
            {
                "data": ANNA,
                "subentry_type": "scale_user",
                "title": "Anna",
                "unique_id": "slot_2",
            },
            {
                "data": {**ANNA, "slot": 3, "name": "Ben"},
                "subentry_type": "scale_user",
                "title": "Ben",
                "unique_id": "slot_3",
            },
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    [anna_entry] = [s for s in entry.subentries.values() if s.title == "Anna"]
    result = await entry.start_subentry_reconfigure_flow(hass, anna_entry.subentry_id)

    result = await submit(hass, result["flow_id"], {**ANNA_FORM, "name": "ben"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"name": "name_in_use"}


async def test_profile_change_wakes_the_scale_and_writes_the_profile(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A changed height reaches the scale, then the subentry."""
    result = await start_reconfigure(hass, anna)

    result = await submit_and_wake(
        hass,
        result["flow_id"],
        fake_scale,
        {**ANNA_FORM, "height_cm": 182, "activity_level": "4", "gender": "female"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    user = fake_scale.users[2]
    assert (user.birth_date, user.height_cm, user.gender, user.activity_level) == (
        date(1985, 4, 12),
        182,
        "female",
        4,
    )
    assert only_subentry(anna).data["profile"] == {
        "birth_date": "1985-04-12",
        "height_cm": 182,
        "gender": "female",
        "activity_level": 4,
    }
    assert fake_scale.clock is not None
    assert not fake_scale.connected


async def test_profile_and_name_change_together(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A rename that comes with a profile change is saved with it."""
    result = await start_reconfigure(hass, anna)

    await submit_and_wake(
        hass,
        result["flow_id"],
        fake_scale,
        {**ANNA_FORM, "name": "Annie", "birth_date": "1986-01-02"},
    )
    await hass.async_block_till_done()

    subentry = only_subentry(anna)
    assert subentry.title == "Annie"
    assert subentry.data["name"] == "Annie"
    assert subentry.data["profile"]["birth_date"] == "1986-01-02"
    assert fake_scale.users[2].birth_date == date(1986, 1, 2)
    assert user_device(hass).name == "Annie"


async def test_rejected_consent_code_changes_nothing(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """If the scale rejects the stored code, the flow says so and keeps everything."""
    fake_scale.users[2].consent_code = 9999
    result = await start_reconfigure(hass, anna)

    result = await submit_and_wake(
        hass, result["flow_id"], fake_scale, {**ANNA_FORM, "height_cm": 182}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "consent_rejected"
    assert dict(only_subentry(anna).data) == ANNA
    assert fake_scale.users[2].height_cm == 180
    assert not fake_scale.connected


async def test_failed_profile_write_keeps_the_subentry(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A write the scale doesn't take leaves the subentry as it was."""
    fake_scale.write_failures[ACTIVITY_LEVEL] = "error"
    result = await start_reconfigure(hass, anna)

    result = await submit_and_wake(
        hass, result["flow_id"], fake_scale, {**ANNA_FORM, "activity_level": "5"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
    assert dict(only_subentry(anna).data) == ANNA
    assert not fake_scale.connected


async def test_scale_that_does_not_wake_aborts(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Nobody steps on the scale for three minutes: the flow gives up."""
    result = await start_reconfigure(hass, anna)
    result = await submit(hass, result["flow_id"], {**ANNA_FORM, "height_cm": 182})

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=3, seconds=1))
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wake_timeout"
    assert dict(only_subentry(anna).data) == ANNA
    assert fake_scale.connection_attempts == 0


async def test_readings_released_by_consent_reach_the_sensors(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Consent makes the scale deliver stored readings; they aren't lost."""
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )
    result = await start_reconfigure(hass, anna)
    result = await submit(hass, result["flow_id"], {**ANNA_FORM, "height_cm": 182})

    # An advertisement without the pending-slot list, so only the flow connects.
    fake_scale.wake()
    await inject_advertisement(hass, scale_advertisement())
    result = await hass.config_entries.subentries.async_configure(result["flow_id"])
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert state(hass, WEIGHT).state == "78.4"


async def test_invalid_birth_date_is_refused(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """The same age range as at creation applies."""
    result = await start_reconfigure(hass, anna)

    result = await submit(
        hass, result["flow_id"], {**ANNA_FORM, "birth_date": "2030-01-01"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"birth_date": "invalid_birth_date"}
    assert fake_scale.connection_attempts == 0

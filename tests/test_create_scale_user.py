"""Adding a scale user by creating a new scale user on the scale."""

import asyncio
from datetime import date, timedelta
from typing import Any

import pytest
from homeassistant.components.bluetooth import async_get_advertisement_callback
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from . import frames
from .conftest import SCALE_ADDRESS, scale_advertisement
from .fake_scale import ACTIVITY_LEVEL, FakeReading, FakeScale
from .test_adopt_scale_user import only_subentry, start_flow

CREATE_INPUT = {
    "name": "Mia",
    "birth_date": "2012-09-30",
    "height_cm": 152,
    "gender": "female",
    "activity_level": "4",
}


async def start_create(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Open "Add scale user" and choose "Create new scale user"."""
    result = await start_flow(hass, entry)
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "create"}
    )


async def after_progress(
    hass: HomeAssistant, flow_id: str, step_id: str
) -> dict[str, Any]:
    """Let the scale and HA run until the progress step `step_id` is done.

    Creating holds the connection for teaching in a background task, which
    `async_block_till_done` can't wait for. Returns the flow's next result.
    """
    for _ in range(2000):
        if hass.config_entries.subentries.async_get(flow_id)["step_id"] != step_id:
            return await hass.config_entries.subentries.async_configure(flow_id)
        await asyncio.sleep(0.001)
    raise AssertionError(f"the flow is still at {step_id}")


async def through_progress(
    hass: HomeAssistant, flow_id: str, step_id: str, seen: list[str] | None = None
) -> dict[str, Any]:
    """Run the flow on from progress step `step_id` to "measuring" or its end.

    `seen` collects the progress steps the flow showed on the way.
    """
    result = await after_progress(hass, flow_id, step_id)
    while (
        result["type"] is FlowResultType.SHOW_PROGRESS
        and result["step_id"] != "create_measure"
    ):
        if seen is not None:
            seen.append(result["step_id"])
        result = await after_progress(hass, flow_id, result["step_id"])
    if seen is not None and result["type"] is FlowResultType.SHOW_PROGRESS:
        seen.append(result["step_id"])
    return result


async def submit_and_wake(
    hass: HomeAssistant,
    flow_id: str,
    scale: FakeScale,
    user_input: dict[str, Any],
    seen: list[str] | None = None,
) -> dict[str, Any]:
    """Submit the create form, step on the scale and run the flow on.

    Returns the flow's result once it is measuring, or when it ended earlier.
    """
    result = await hass.config_entries.subentries.async_configure(flow_id, user_input)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "create_wake"
    scale.wake()
    async_get_advertisement_callback(hass)(
        scale_advertisement(service_data=scale.service_data())
    )
    return await through_progress(hass, flow_id, "create_wake", seen)


def teaching_reading() -> FakeReading:
    """A live reading taken now, on the scale's clock."""
    now = dt_util.now().replace(tzinfo=None, microsecond=0)
    return FakeReading.from_capture(frames.READING_SLOT_2, at=now)


async def step_off_and_on(
    hass: HomeAssistant, flow_id: str, scale: FakeScale, slot: int
) -> dict[str, Any]:
    """The person on the scale gets their reading; return the flow's next result."""
    scale.weigh_in(slot, teaching_reading())
    return await after_progress(hass, flow_id, "create_measure")


async def create_with_one_reading(
    hass: HomeAssistant, flow_id: str, scale: FakeScale, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Create a scale user with the one step-on; return the flow's final result."""
    result = await submit_and_wake(hass, flow_id, scale, user_input)
    assert result["step_id"] == "create_measure", result
    assert scale.client is not None
    assert scale.client.consented_slot is not None
    return await step_off_and_on(hass, flow_id, scale, scale.client.consented_slot)


async def test_create_registers_the_scale_user_and_writes_its_profile(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """The new scale user lands in the first free slot with the entered profile."""
    fake_scale.add_user(slot=1, consent_code=1111)
    result = await start_create(hass, loaded_scale)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "create"

    result = await create_with_one_reading(
        hass, result["flow_id"], fake_scale, CREATE_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = only_subentry(loaded_scale)
    assert subentry.unique_id == "slot_2"
    assert subentry.title == "Mia"
    consent_code = subentry.data["consent_code"]
    assert 1111 <= consent_code <= 9998
    assert dict(subentry.data) == {
        "slot": 2,
        "consent_code": consent_code,
        "name": "Mia",
        "person": None,
        "profile": {
            "birth_date": "2012-09-30",
            "height_cm": 152,
            "gender": "female",
            "activity_level": 4,
        },
    }
    # The scale holds the scale user with HA's code and the profile.
    user = fake_scale.users[2]
    assert user.consent_code == consent_code
    assert (user.birth_date, user.height_cm, user.gender, user.activity_level) == (
        date(2012, 9, 30),
        152,
        "female",
        4,
    )
    assert fake_scale.clock is not None
    assert not fake_scale.connected


async def test_new_scale_user_device_is_linked_to_the_scale(
    hass: HomeAssistant,
    loaded_scale: MockConfigEntry,
    fake_scale: FakeScale,
    device_registry: dr.DeviceRegistry,
) -> None:
    """The created scale user gets its own device, connected via the scale device."""
    result = await start_create(hass, loaded_scale)
    await create_with_one_reading(hass, result["flow_id"], fake_scale, CREATE_INPUT)
    await hass.async_block_till_done()

    [scale_device] = device_registry.async_get_devices(
        connections={(dr.CONNECTION_BLUETOOTH, SCALE_ADDRESS)}
    )
    [user_device] = [
        device
        for device in dr.async_entries_for_config_entry(
            device_registry, loaded_scale.entry_id
        )
        if device.id != scale_device.id
    ]
    assert user_device.name == "Mia"
    assert user_device.via_device_id == scale_device.id
    assert user_device.config_subentry_id == only_subentry(loaded_scale).subentry_id


async def test_full_scale_aborts_before_registering(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """With every slot taken the flow says so and registers nothing."""
    for slot in range(1, 9):
        fake_scale.add_user(slot=slot, consent_code=1110 + slot)
    result = await start_create(hass, loaded_scale)

    result = await submit_and_wake(hass, result["flow_id"], fake_scale, CREATE_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_free_slot"
    assert fake_scale.ucp_requests == []
    assert loaded_scale.subentries == {}
    assert not fake_scale.connected


async def test_failure_after_registration_deletes_the_scale_user(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A profile write that fails removes the new scale user from the scale."""
    fake_scale.add_user(slot=1, consent_code=1111)
    fake_scale.write_failures[ACTIVITY_LEVEL] = "error"
    result = await start_create(hass, loaded_scale)

    result = await submit_and_wake(hass, result["flow_id"], fake_scale, CREATE_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
    assert list(fake_scale.users) == [1]
    assert fake_scale.deleted_while_connected == [2]
    assert loaded_scale.subentries == {}
    assert not fake_scale.connected


async def test_aborting_after_registration_deletes_the_scale_user(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Closing the flow while the new scale user exists removes it from the scale."""
    fake_scale.write_failures[ACTIVITY_LEVEL] = "stall"
    result = await start_create(hass, loaded_scale)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], CREATE_INPUT
    )
    fake_scale.wake()
    # Don't wait for the session: it hangs on the profile write.
    async_get_advertisement_callback(hass)(scale_advertisement())
    for _ in range(100):
        if fake_scale.stalled:
            break
        await asyncio.sleep(0)
    assert fake_scale.users

    hass.config_entries.subentries.async_abort(result["flow_id"])
    # HA doesn't wait for a cancelled task; let its cleanup talk to the scale.
    for _ in range(100):
        if not fake_scale.connected:
            break
        await asyncio.sleep(0)

    assert fake_scale.users == {}
    assert fake_scale.deleted_while_connected == [1]
    assert loaded_scale.subentries == {}
    assert not fake_scale.connected


async def test_scale_that_does_not_wake_aborts(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Nobody steps on the scale for three minutes: the flow gives up."""
    result = await start_create(hass, loaded_scale)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], CREATE_INPUT
    )

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=3, seconds=1))
    result = await after_progress(hass, result["flow_id"], "create_wake")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wake_timeout"
    assert fake_scale.connection_attempts == 0


async def test_person_or_name_is_required(
    hass: HomeAssistant, loaded_scale: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Leaving both the person and the name empty shows an error."""
    result = await start_create(hass, loaded_scale)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**CREATE_INPUT, "name": ""}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "name_required"}
    assert fake_scale.connection_attempts == 0


def years_ago(years: int, days: int = 0) -> str:
    """The ISO date `years` years (and `days` days) before today."""
    today = dt_util.now().date()
    return (today.replace(year=today.year - years) - timedelta(days=days)).isoformat()


@pytest.mark.parametrize(
    ("birth_date", "error"),
    [
        (years_ago(-1), "invalid_birth_date"),  # in the future
        (years_ago(10, days=-1), "invalid_birth_date"),  # not yet 10
        (years_ago(10), None),
        (years_ago(100), None),
        (years_ago(101), "invalid_birth_date"),  # older than 100
    ],
)
async def test_birth_date_must_give_an_age_of_10_to_100(
    hass: HomeAssistant,
    loaded_scale: MockConfigEntry,
    fake_scale: FakeScale,
    birth_date: str,
    error: str | None,
) -> None:
    """The scale computes body composition for ages 10 to 100 only."""
    result = await start_create(hass, loaded_scale)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**CREATE_INPUT, "birth_date": birth_date}
    )

    if error is None:
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        hass.config_entries.subentries.async_abort(result["flow_id"])
    else:
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"birth_date": error}
    assert fake_scale.connection_attempts == 0


@pytest.mark.parametrize(
    "bad_input",
    [
        {"height_cm": 99},
        {"height_cm": 221},
        {"activity_level": "0"},
        {"activity_level": "6"},
        {"gender": "other"},
    ],
)
async def test_out_of_range_profile_is_refused(
    hass: HomeAssistant,
    loaded_scale: MockConfigEntry,
    fake_scale: FakeScale,
    bad_input: dict[str, Any],
) -> None:
    """Height outside 100-220 cm, activity outside 1-5 or another gender is refused."""
    result = await start_create(hass, loaded_scale)

    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"], {**CREATE_INPUT, **bad_input}
        )

    assert fake_scale.connection_attempts == 0

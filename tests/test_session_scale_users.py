"""Creating and deleting scale users through the scale session (no Home Assistant)."""

import asyncio
from datetime import date

import pytest

from custom_components.beurer_scales.protocol.errors import (
    NoFreeSlot,
    ScaleError,
)
from custom_components.beurer_scales.protocol.frames import Gender, UserListEntry
from custom_components.beurer_scales.protocol.models import BF450
from custom_components.beurer_scales.protocol.profile import Profile
from custom_components.beurer_scales.protocol.scale_user import create_scale_user
from custom_components.beurer_scales.protocol.session import open_session

from .fake_scale import ACTIVITY_LEVEL, FakeScale

PROFILE = Profile(
    birth_date=date(2012, 9, 30), height_cm=152, gender=Gender.FEMALE, activity_level=4
)


@pytest.fixture
def scale(fake_scale: FakeScale) -> FakeScale:
    """An awake BF450 that the session connects to."""
    fake_scale.wake()
    return fake_scale


async def test_register_takes_the_lowest_free_slot(scale: FakeScale) -> None:
    """Registering stores a scale user with the code and returns its slot."""
    scale.add_user(slot=1, consent_code=1111)

    async with open_session(scale.ble_device, BF450) as session:
        slot = await session.register_user(consent_code=4321)

    assert slot == 2
    assert scale.users[2].consent_code == 4321


async def test_register_on_a_full_scale_raises_no_free_slot(scale: FakeScale) -> None:
    """All eight slots taken: the scale refuses to register another scale user."""
    for slot in range(1, 9):
        scale.add_user(slot=slot, consent_code=1110 + slot)

    async with open_session(scale.ble_device, BF450) as session:
        with pytest.raises(NoFreeSlot):
            await session.register_user(consent_code=4321)


async def test_written_profile_reads_back_unchanged(scale: FakeScale) -> None:
    """After consent, the profile is written and the user list shows it."""
    async with open_session(scale.ble_device, BF450) as session:
        slot = await session.register_user(consent_code=4321)
        await session.consent(slot, 4321)
        await session.write_profile(PROFILE)
        users = await session.read_user_list()

    assert users == [
        UserListEntry(
            slot=1,
            initials=None,
            birth_date=date(2012, 9, 30),
            height_cm=152,
            gender=Gender.FEMALE,
            activity_level=4,
        )
    ]
    # The app always bumps the database change increment to 1 (§4).
    assert scale.users[1].database_change_increment == 1


async def test_profile_write_needs_consent(scale: FakeScale) -> None:
    """Without consent the scale refuses profile writes."""
    scale.add_user(slot=1, consent_code=1111)

    async with open_session(scale.ble_device, BF450) as session:
        with pytest.raises(ScaleError):
            await session.write_profile(PROFILE)

    assert scale.users[1].height_cm == 180


async def test_delete_removes_the_consented_scale_user(scale: FakeScale) -> None:
    """Deleting while consented frees the slot; other scale users stay."""
    scale.add_user(slot=1, consent_code=1111)
    scale.add_user(slot=2, consent_code=4321)

    async with open_session(scale.ble_device, BF450) as session:
        await session.consent(2, 4321)
        await session.delete_user()
        users = await session.read_user_list()

    assert [u.slot for u in users] == [1]


async def test_delete_without_consent_fails(scale: FakeScale) -> None:
    """The scale deletes only the consented scale user."""
    scale.add_user(slot=1, consent_code=1111)

    async with open_session(scale.ble_device, BF450) as session:
        with pytest.raises(ScaleError):
            await session.delete_user()

    assert list(scale.users) == [1]


async def test_create_registers_consents_and_writes_the_profile(
    scale: FakeScale,
) -> None:
    """A new scale user gets a random consent code and the profile."""
    scale.add_user(slot=1, consent_code=1111)

    async with (
        open_session(scale.ble_device, BF450) as session,
        create_scale_user(session, PROFILE) as created,
    ):
        assert scale.client is not None
        assert scale.client.consented_slot == created.slot

    assert created.slot == 2
    assert 1111 <= created.consent_code <= 9998
    user = scale.users[2]
    assert user.consent_code == created.consent_code
    assert (user.birth_date, user.height_cm, user.gender, user.activity_level) == (
        date(2012, 9, 30),
        152,
        "female",
        4,
    )


async def test_create_on_a_full_scale_never_registers(scale: FakeScale) -> None:
    """A full user list raises NoFreeSlot before anything is registered."""
    for slot in range(1, 9):
        scale.add_user(slot=slot, consent_code=1110 + slot)

    async with open_session(scale.ble_device, BF450) as session:
        with pytest.raises(NoFreeSlot):
            async with create_scale_user(session, PROFILE):
                pass

    assert scale.ucp_requests == []


async def test_failure_after_registration_deletes_the_scale_user(
    scale: FakeScale,
) -> None:
    """An error while the new scale user exists deletes it before disconnecting."""
    scale.add_user(slot=1, consent_code=1111)

    with pytest.raises(RuntimeError):
        async with (
            open_session(scale.ble_device, BF450) as session,
            create_scale_user(session, PROFILE),
        ):
            raise RuntimeError("teaching failed")

    assert list(scale.users) == [1]
    assert scale.deleted_while_connected == [2]
    assert not scale.connected


async def test_failed_profile_write_deletes_the_scale_user(scale: FakeScale) -> None:
    """A profile write the scale refuses leaves no scale user behind."""
    scale.write_failures[ACTIVITY_LEVEL] = "error"

    with pytest.raises(ScaleError):
        async with (
            open_session(scale.ble_device, BF450) as session,
            create_scale_user(session, PROFILE),
        ):
            pass

    assert scale.users == {}
    assert scale.deleted_while_connected == [1]


async def test_cancelling_after_registration_deletes_the_scale_user(
    scale: FakeScale,
) -> None:
    """Cancelling (an aborted flow) mid-create deletes the new scale user."""
    scale.write_failures[ACTIVITY_LEVEL] = "stall"

    async def create() -> None:
        async with (
            open_session(scale.ble_device, BF450) as session,
            create_scale_user(session, PROFILE),
        ):
            pass

    task = asyncio.create_task(create())
    while not scale.stalled:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert scale.users == {}
    assert scale.deleted_while_connected == [1]
    assert not scale.connected

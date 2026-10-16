"""The scale session, run directly against the fake scale (no Home Assistant)."""

from collections.abc import Iterator
from datetime import date, datetime
from unittest.mock import patch

import pytest

from custom_components.beurer_scales.protocol.errors import (
    ConsentRejected,
    PairingFailed,
    PairingUnsupported,
    ScaleConnectionError,
    ScaleTimeout,
)
from custom_components.beurer_scales.protocol.frames import Gender, UserListEntry
from custom_components.beurer_scales.protocol.models import BF450
from custom_components.beurer_scales.protocol.session import open_session

from .fake_scale import FakeScale


@pytest.fixture
def scale() -> Iterator[FakeScale]:
    """An awake BF450 that the session connects to."""
    scale = FakeScale()
    scale.wake()
    with patch(
        "custom_components.beurer_scales.protocol.session.establish_connection",
        scale.establish_connection,
    ):
        yield scale


async def test_user_list_holds_every_scale_user(scale: FakeScale) -> None:
    """Reading the user list needs no consent and returns each slot's profile."""
    scale.add_user(slot=1, consent_code=1234)
    scale.add_user(
        slot=3,
        consent_code=4321,
        birth_date=date(2012, 9, 30),
        height_cm=152,
        gender="female",
        activity_level=4,
    )

    async with open_session(scale.ble_device, BF450) as session:
        users = await session.read_user_list()

    assert users == [
        UserListEntry(
            slot=1,
            initials=None,
            birth_date=date(1985, 4, 12),
            height_cm=180,
            gender=Gender.MALE,
            activity_level=3,
        ),
        UserListEntry(
            slot=3,
            initials=None,
            birth_date=date(2012, 9, 30),
            height_cm=152,
            gender=Gender.FEMALE,
            activity_level=4,
        ),
    ]


async def test_user_list_of_a_scale_without_users_is_empty(scale: FakeScale) -> None:
    """A factory-new scale lists nobody."""
    async with open_session(scale.ble_device, BF450) as session:
        assert await session.read_user_list() == []


async def test_set_time_sets_the_scale_clock(scale: FakeScale) -> None:
    """The scale's clock is set to the given local time."""
    async with open_session(scale.ble_device, BF450) as session:
        await session.set_time(datetime(2026, 10, 16, 7, 12, 30))

    assert scale.clock == datetime(2026, 10, 16, 7, 12, 30)


async def test_consent_with_the_right_code_succeeds(scale: FakeScale) -> None:
    """The right consent code is accepted."""
    scale.add_user(slot=2, consent_code=4321)

    async with open_session(scale.ble_device, BF450) as session:
        await session.consent(slot=2, consent_code=4321)

    assert scale.client is not None
    assert scale.client.consented_slot == 2


@pytest.mark.parametrize(("slot", "code"), [(2, 1234), (5, 4321)])
async def test_consent_is_rejected_for_a_wrong_code_or_missing_slot(
    scale: FakeScale, slot: int, code: int
) -> None:
    """A wrong code and a missing slot both raise ConsentRejected."""
    scale.add_user(slot=2, consent_code=4321)

    async with open_session(scale.ble_device, BF450) as session:
        with pytest.raises(ConsentRejected):
            await session.consent(slot=slot, consent_code=code)


async def test_session_requests_pairing(scale: FakeScale) -> None:
    """The scale drops unbonded links, so the session always pairs."""
    scale.add_user(slot=1, consent_code=1234)

    async with open_session(scale.ble_device, BF450) as session:
        assert len(await session.read_user_list()) == 1


async def test_session_always_disconnects(scale: FakeScale) -> None:
    """The connection is closed when the session ends, also after an error."""
    with pytest.raises(ConsentRejected):
        async with open_session(scale.ble_device, BF450) as session:
            await session.consent(slot=1, consent_code=1234)

    assert scale.connection_attempts == 1
    assert not scale.connected


async def test_a_silent_scale_times_out(scale: FakeScale) -> None:
    """A UCP request that gets no answer raises ScaleTimeout and disconnects."""
    scale.add_user(slot=1, consent_code=1234)
    scale.answers_ucp = False

    with pytest.raises(ScaleTimeout):
        async with open_session(
            scale.ble_device, BF450, response_timeout=0.01
        ) as session:
            await session.consent(slot=1, consent_code=1234)

    assert not scale.connected


async def test_the_whole_session_is_capped(scale: FakeScale) -> None:
    """A session that outlasts its time budget raises ScaleTimeout."""
    scale.answers_ucp = False

    with pytest.raises(ScaleTimeout):
        async with open_session(scale.ble_device, BF450, session_timeout=0.01) as s:
            await s.consent(slot=1, consent_code=1234)

    assert not scale.connected


async def test_a_sleeping_scale_cannot_be_reached() -> None:
    """Connecting to a scale that isn't awake raises ScaleConnectionError."""
    scale = FakeScale()
    with (
        patch(
            "custom_components.beurer_scales.protocol.session.establish_connection",
            scale.establish_connection,
        ),
        pytest.raises(ScaleConnectionError),
    ):
        async with open_session(scale.ble_device, BF450):
            pass


async def test_a_path_without_pairing_support_is_reported(scale: FakeScale) -> None:
    """A Bluetooth path that can't bond raises PairingUnsupported."""
    scale.pairing = "unsupported"

    with pytest.raises(PairingUnsupported):
        async with open_session(scale.ble_device, BF450):
            pass


@pytest.mark.parametrize(
    "path", ["bluez", "kernel"], ids=["bluez agentless", "habluetooth mgmt"]
)
async def test_a_failed_bond_is_reported(scale: FakeScale, path: str) -> None:
    """A Bluetooth path that tries to bond and fails raises PairingFailed."""
    scale.pairing = "fails"
    scale.pairing_failure = path

    with pytest.raises(PairingFailed):
        async with open_session(scale.ble_device, BF450):
            pass


async def test_a_connection_failure_is_not_a_pairing_failure(scale: FakeScale) -> None:
    """A sleeping scale is a connection error, not a pairing problem."""
    scale.awake = False

    with pytest.raises(ScaleConnectionError) as caught:
        async with open_session(scale.ble_device, BF450):
            pass

    assert not isinstance(caught.value, PairingFailed | PairingUnsupported)

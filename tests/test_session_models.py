"""Per-model differences in what the scale session writes (protocol doc §3, §4).

The fake scale uses the BF450 numbering (FFF1/FFF2), which the BF500 and the
Wi-Fi BF880 share.
"""

from collections.abc import Iterator
from datetime import date, datetime
from unittest.mock import patch

import pytest

from custom_components.beurer_scales.protocol.frames import Gender
from custom_components.beurer_scales.protocol.models import BF450, model_by_name
from custom_components.beurer_scales.protocol.profile import Profile
from custom_components.beurer_scales.protocol.session import open_session

from .fake_scale import DATABASE_CHANGE_INCREMENT, HEIGHT, FakeScale

PROFILE = Profile(
    birth_date=date(2012, 9, 30), height_cm=152, gender=Gender.FEMALE, activity_level=4
)


@pytest.fixture
def scale() -> Iterator[FakeScale]:
    """An awake scale that the session connects to."""
    scale = FakeScale()
    scale.wake()
    with patch(
        "custom_components.beurer_scales.protocol.session.establish_connection",
        scale.establish_connection,
    ):
        yield scale


def written(scale: FakeScale, uuid: str) -> list[bytes]:
    """What the session wrote to one characteristic."""
    return [data for char, data, _ in scale.writes if char == uuid]


async def test_bf450_profile_writes(scale: FakeScale) -> None:
    """The BF450 takes `[cm, 0]` and the change increment 01 00 00 00 (§9.2)."""
    async with open_session(scale.ble_device, BF450) as session:
        slot = await session.register_user(consent_code=4321)
        await session.consent(slot, 4321)
        await session.write_profile(PROFILE)

    assert written(scale, HEIGHT) == [bytes([152, 0])]
    assert written(scale, DATABASE_CHANGE_INCREMENT) == [bytes([1, 0, 0, 0])]


async def test_bf500_writes_the_short_profile(scale: FakeScale) -> None:
    """The BF500 takes a one-byte height and an all-ff change increment (§4)."""
    async with open_session(scale.ble_device, model_by_name("BF500")) as session:
        slot = await session.register_user(consent_code=4321)
        await session.consent(slot, 4321)
        await session.write_profile(PROFILE)

    assert written(scale, HEIGHT) == [bytes([152])]
    assert written(scale, DATABASE_CHANGE_INCREMENT) == [b"\xff\xff\xff\xff"]
    assert scale.users[slot].height_cm == 152


@pytest.mark.parametrize(("name", "response"), [("BF450", True), ("BF880", False)])
async def test_wifi_models_write_without_response(
    scale: FakeScale, name: str, response: bool
) -> None:
    """Wi-Fi models take every write without response (§3)."""
    async with open_session(scale.ble_device, model_by_name(name)) as session:
        await session.set_time(datetime(2026, 10, 16, 8, 0, 0))
        await session.read_user_list()

    assert scale.writes
    assert {r for _, _, r in scale.writes} == {response}

"""Teaching a new scale user its weight, run directly against the fake scale (no HA)."""

import asyncio

import pytest

from custom_components.beurer_scales.protocol.errors import (
    ConnectionLost,
    TeachingTimeout,
)
from custom_components.beurer_scales.protocol.models import BF450
from custom_components.beurer_scales.protocol.session import ScaleSession, open_session
from custom_components.beurer_scales.protocol.teaching import Teaching

from . import frames
from .fake_scale import FakeReading, FakeScale

READING = FakeReading.from_capture(frames.READING_SLOT_2)
TIMEOUT = 0.1


@pytest.fixture
def scale(fake_scale: FakeScale) -> FakeScale:
    """An awake BF450 with a consentable scale user in slot 2."""
    fake_scale.add_user(slot=2, consent_code=4321)
    fake_scale.wake()
    return fake_scale


async def test_waiting_for_a_live_reading_returns_it_once_the_scale_is_done(
    scale: FakeScale,
) -> None:
    """A live reading counts when FFF4 01 says it is complete."""
    async with open_session(scale.ble_device, BF450) as session:
        await session.subscribe_readings()
        await session.consent(2, 4321)
        waiting = asyncio.ensure_future(session.wait_for_live_reading())
        await asyncio.sleep(0.01)
        assert not waiting.done()

        scale.weigh_in(2, READING)
        reading = await asyncio.wait_for(waiting, 1)

    assert (reading.slot, reading.weight_kg) == (2, 78.4)


async def test_a_reading_that_finished_before_waiting_is_not_lost(
    scale: FakeScale,
) -> None:
    """Someone who steps on before HA asks still gets their reading counted."""
    async with open_session(scale.ble_device, BF450) as session:
        await session.subscribe_readings()
        await session.consent(2, 4321)
        scale.weigh_in(2, READING)
        await asyncio.sleep(0.01)
        scale.weigh_in(2, READING)
        await asyncio.sleep(0.01)

        first = await asyncio.wait_for(session.wait_for_live_reading(), 1)
        second = await asyncio.wait_for(session.wait_for_live_reading(), 1)

    assert first.weight_kg == second.weight_kg == 78.4


async def consented(session: ScaleSession) -> ScaleSession:
    """Prepare a session for teaching the scale user in slot 2."""
    await session.subscribe_readings()
    await session.consent(2, 4321)
    return session


async def test_teaching_takes_one_reading_and_ends(scale: FakeScale) -> None:
    """The first live reading is the teaching reading; teaching then ends."""
    teaching = Teaching()
    async with open_session(scale.ble_device, BF450) as session:
        running = asyncio.ensure_future(teaching.run(await consented(session)))
        await asyncio.sleep(0.01)
        assert not running.done()
        scale.weigh_in(2, READING)
        await asyncio.wait_for(running, 1)

    assert teaching.reading is not None
    assert teaching.reading.weight_kg == 78.4


async def test_teaching_without_any_reading_times_out(scale: FakeScale) -> None:
    """Nobody steps on: teaching fails, so the caller can undo the scale user."""
    teaching = Teaching()
    async with open_session(scale.ble_device, BF450) as session:
        with pytest.raises(TeachingTimeout):
            await teaching.run(await consented(session), timeout=TIMEOUT)

    assert teaching.reading is None


async def test_a_lost_link_before_the_reading_raises(scale: FakeScale) -> None:
    """Without a reading the caller must undo the scale user."""
    teaching = Teaching()
    async with open_session(scale.ble_device, BF450) as session:
        running = asyncio.ensure_future(teaching.run(await consented(session)))
        await asyncio.sleep(0.01)
        scale.drop_link()
        with pytest.raises(ConnectionLost):
            await asyncio.wait_for(running, 1)

    assert teaching.reading is None

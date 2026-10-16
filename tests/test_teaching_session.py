"""Teaching a new scale user its weight, run directly against the fake scale (no HA)."""

import asyncio
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from custom_components.beurer_scales.protocol.errors import TeachingTimeout
from custom_components.beurer_scales.protocol.models import BF450
from custom_components.beurer_scales.protocol.session import ScaleSession, open_session
from custom_components.beurer_scales.protocol.teaching import Teaching

from . import frames
from .fake_scale import FakeReading, FakeScale

READING = FakeReading.from_capture(frames.READING_SLOT_2)
TIMEOUT = 0.1


@pytest.fixture
def scale() -> Iterator[FakeScale]:
    """An awake BF450 with a consentable scale user in slot 2."""
    scale = FakeScale()
    scale.add_user(slot=2, consent_code=4321)
    scale.wake()
    with patch(
        "custom_components.beurer_scales.protocol.session.establish_connection",
        scale.establish_connection,
    ):
        yield scale


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


async def step_on(scale: FakeScale, teaching: Teaching, times: int = 1) -> None:
    """Weigh in `times` times; return once teaching counted the readings."""
    for _ in range(times):
        expected = len(teaching.readings) + 1
        scale.weigh_in(2, READING)
        await asyncio.wait_for(teaching.wait_for_readings(expected), 1)


async def test_teaching_ends_after_three_readings(scale: FakeScale) -> None:
    """Teaching counts each live reading and stops at the third."""
    teaching = Teaching()
    async with open_session(scale.ble_device, BF450) as session:
        running = asyncio.ensure_future(teaching.run(await consented(session)))
        await step_on(scale, teaching, times=2)
        assert not running.done()
        await step_on(scale, teaching)
        await asyncio.wait_for(running, 1)

    assert [r.weight_kg for r in teaching.readings] == [78.4, 78.4, 78.4]
    assert not teaching.timed_out


async def test_teaching_can_be_finished_after_one_reading(scale: FakeScale) -> None:
    """The household member doesn't have to take all three readings."""
    teaching = Teaching()
    async with open_session(scale.ble_device, BF450) as session:
        running = asyncio.ensure_future(teaching.run(await consented(session)))
        await step_on(scale, teaching)
        teaching.finish()
        await asyncio.wait_for(running, 1)

    assert len(teaching.readings) == 1
    assert not teaching.timed_out


async def test_teaching_without_any_reading_times_out(scale: FakeScale) -> None:
    """Nobody steps on: teaching fails, so the caller can undo the scale user."""
    teaching = Teaching()
    async with open_session(scale.ble_device, BF450) as session:
        with pytest.raises(TeachingTimeout):
            await teaching.run(await consented(session), timeout=TIMEOUT)

    assert teaching.readings == []


async def test_a_timeout_after_a_reading_keeps_what_was_taught(
    scale: FakeScale,
) -> None:
    """One reading taught the scale enough; a timeout then just ends teaching."""
    teaching = Teaching()
    async with open_session(scale.ble_device, BF450) as session:
        running = asyncio.ensure_future(
            teaching.run(await consented(session), timeout=TIMEOUT)
        )
        await step_on(scale, teaching)
        await asyncio.wait_for(running, 1)

    assert len(teaching.readings) == 1
    assert teaching.timed_out
    assert teaching.ended


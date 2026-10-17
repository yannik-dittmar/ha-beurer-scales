"""Syncing a scale user's readings, run directly against the fake scale (no HA)."""

from datetime import datetime

import pytest

from custom_components.beurer_scales.protocol.errors import (
    ConsentRejected,
    ScaleConnectionError,
)
from custom_components.beurer_scales.protocol.models import BF450
from custom_components.beurer_scales.protocol.session import open_session

from . import frames
from .fake_scale import (
    SWAPPED_WEIGHT_SCALE_UUID,
    FakeReading,
    FakeScale,
    SecondConsent,
)

QUIET = 0.05
NOW = datetime(2024, 3, 10, 8, 0, 0)


@pytest.fixture
def scale(fake_scale: FakeScale) -> FakeScale:
    """An awake BF450 that the session connects to."""
    fake_scale.wake()
    return fake_scale


def pending_slots(scale: FakeScale) -> bytes:
    """The pending-slot list the scale advertises."""
    return scale.service_data()[SWAPPED_WEIGHT_SCALE_UUID]


async def test_sync_delivers_stored_readings_oldest_first(scale: FakeScale) -> None:
    """Consent releases the stored readings; the slot is no longer pending."""
    scale.add_user(slot=1, consent_code=1234)
    scale.weigh_in(1, FakeReading.from_capture(frames.STORED_READING_SLOT_1_A))
    scale.weigh_in(1, FakeReading.from_capture(frames.STORED_READING_SLOT_1_B))
    assert pending_slots(scale) == b"\x01"

    async with open_session(scale.ble_device, BF450) as session:
        result = await session.sync(1, 1234, NOW, quiet_period=QUIET)

    assert [(r.slot, r.timestamp, r.weight_kg) for r in result.readings] == [
        (1, datetime(2024, 3, 10, 7, 54, 0), 77.9),
        (1, datetime(2024, 3, 10, 7, 58, 34), 77.9),
    ]
    assert result.battery_percent == 71
    assert scale.clock == NOW
    assert pending_slots(scale) == b""
    assert not scale.connected


async def test_a_live_reading_is_collected(scale: FakeScale) -> None:
    """A weigh-in while consented arrives live (then FFF4 01) and is collected."""
    scale.add_user(slot=2, consent_code=4321)

    async with open_session(scale.ble_device, BF450) as session:
        await session.subscribe_readings()
        await session.consent(2, 4321)
        scale.weigh_in(2, FakeReading.from_capture(frames.READING_SLOT_2))
        readings = await session.collect_readings(quiet_period=QUIET)

    assert [(r.slot, r.weight_kg) for r in readings] == [(2, 78.4)]


async def test_collecting_returns_only_new_readings(scale: FakeScale) -> None:
    """Each collect returns the readings since the last one."""
    scale.add_user(slot=1, consent_code=1234)
    scale.weigh_in(1, FakeReading.from_capture(frames.STORED_READING_SLOT_1_A))

    async with open_session(scale.ble_device, BF450) as session:
        await session.subscribe_readings()
        await session.consent(1, 1234)
        first = await session.collect_readings(quiet_period=QUIET)
        second = await session.collect_readings(quiet_period=QUIET)

    assert len(first) == 1
    assert second == []


async def test_sync_with_a_wrong_code_is_rejected(scale: FakeScale) -> None:
    """A rejected consent delivers nothing and keeps the slot pending."""
    scale.add_user(slot=1, consent_code=1234)
    scale.weigh_in(1, FakeReading.from_capture(frames.STORED_READING_SLOT_1_A))

    with pytest.raises(ConsentRejected):
        async with open_session(scale.ble_device, BF450) as session:
            await session.sync(1, 9999, NOW, quiet_period=QUIET)

    assert pending_slots(scale) == b"\x01"
    assert not scale.connected


async def test_another_scale_user_is_drained_in_the_same_connection(
    scale: FakeScale,
) -> None:
    """After one sync, a second consent collects the next slot's readings."""
    scale.add_user(slot=1, consent_code=1234)
    scale.add_user(slot=2, consent_code=4321)
    scale.weigh_in(1, FakeReading.from_capture(frames.STORED_READING_SLOT_1_A))
    scale.weigh_in(2, FakeReading.from_capture(frames.READING_SLOT_2))

    async with open_session(scale.ble_device, BF450) as session:
        first = await session.sync(2, 4321, NOW, quiet_period=QUIET)
        drained = await session.drain(1, 1234, quiet_period=QUIET)

    assert [(r.slot, r.weight_kg) for r in first.readings] == [(2, 78.4)]
    assert [(r.slot, r.timestamp, r.weight_kg) for r in drained] == [
        (1, datetime(2024, 3, 10, 7, 54, 0), 77.9)
    ]
    assert pending_slots(scale) == b""
    assert scale.connection_attempts == 1


@pytest.mark.parametrize(
    ("refusal", "error"),
    [("rejected", ConsentRejected), ("dropped", ScaleConnectionError)],
)
async def test_a_refused_second_consent_keeps_the_slot_pending(
    scale: FakeScale, refusal: SecondConsent, error: type[Exception]
) -> None:
    """A scale that allows one consent per connection (V2) keeps the rest pending."""
    scale.second_consent = refusal
    scale.add_user(slot=1, consent_code=1234)
    scale.add_user(slot=2, consent_code=4321)
    scale.weigh_in(1, FakeReading.from_capture(frames.STORED_READING_SLOT_1_A))
    scale.weigh_in(2, FakeReading.from_capture(frames.READING_SLOT_2))

    with pytest.raises(error):
        async with open_session(scale.ble_device, BF450) as session:
            first = await session.sync(2, 4321, NOW, quiet_period=QUIET)
            await session.drain(1, 1234, quiet_period=QUIET)

    assert [(r.slot, r.weight_kg) for r in first.readings] == [(2, 78.4)]
    assert pending_slots(scale) == b"\x01"
    assert not scale.connected

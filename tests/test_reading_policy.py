"""The reading policy: fresh, backlog or guessed, and duplicates (ADRs 0005, 0006)."""

from datetime import datetime, timedelta

from custom_components.beurer_scales.protocol.models import BF450, model_by_name
from custom_components.beurer_scales.protocol.policy import (
    Discard,
    ReadingPolicy,
)
from custom_components.beurer_scales.protocol.readings import (
    Reading,
    ReadingAssembler,
)

from . import frames
from .fake_scale import FakeReading

SYNC_START = datetime(2026, 10, 16, 7, 30, 0)


def reading(
    capture: tuple[tuple[int, bytes], ...] = frames.READING_SLOT_2,
    *,
    at: datetime,
    slot: int = 2,
) -> Reading:
    """Assemble a captured reading as the session would return it."""
    assembler = ReadingAssembler()
    for short_uuid, frame in (
        FakeReading.from_capture(capture, at=at).for_slot(slot).frames
    ):
        assembler.add(f"0000{short_uuid:04x}-0000-1000-8000-00805f9b34fb", frame)
    (result,) = assembler.readings()
    return result


def test_a_reading_from_just_before_the_sync_is_fresh() -> None:
    """A reading taken during this wake window reaches the sensors."""
    taken = reading(at=SYNC_START - timedelta(seconds=20))

    result = ReadingPolicy(model=BF450).apply([taken], SYNC_START)

    assert result.fresh == [taken]
    assert result.discarded == []


def test_a_reading_more_than_5_minutes_old_is_backlog() -> None:
    """ADR 0005: older than 5 minutes before the sync start means backlog."""
    old = reading(at=SYNC_START - timedelta(minutes=5, seconds=1))
    borderline = reading(at=SYNC_START - timedelta(minutes=5))

    result = ReadingPolicy(model=BF450).apply([old, borderline], SYNC_START)

    assert result.fresh == [borderline]
    assert [(d.reading, d.reason) for d in result.discarded] == [(old, Discard.BACKLOG)]


def test_a_reading_the_scale_guessed_is_discarded() -> None:
    """ADR 0006: custom1 byte 4 != 0 (display U--) means guessed (§9.3)."""
    guessed = reading(frames.GUESSED_READING_SLOT_2, at=SYNC_START)

    result = ReadingPolicy(model=BF450).apply([guessed], SYNC_START)

    assert result.fresh == []
    assert [(d.reading, d.reason) for d in result.discarded] == [
        (guessed, Discard.GUESSED)
    ]


def test_only_models_with_custom1_on_fff5_flag_a_guess() -> None:
    """A heart-rate model's FFF5 frame has no guess byte, whatever its length."""
    heart_rate_model = model_by_name("BF500")
    taken = reading(frames.GUESSED_READING_SLOT_2, at=SYNC_START)

    result = ReadingPolicy(model=heart_rate_model).apply([taken], SYNC_START)

    assert result.fresh == [taken]


def test_a_reading_without_custom1_is_not_guessed() -> None:
    """Models without the custom1 frame can't flag a guess."""
    weight_only = reading(frames.READING_SLOT_2[:1], at=SYNC_START)

    assert ReadingPolicy(model=BF450).apply([weight_only], SYNC_START).fresh == [
        weight_only
    ]


def test_a_reading_seen_before_is_not_emitted_again() -> None:
    """Same slot, timestamp and weight: a re-delivery after an interrupted sync."""
    policy = ReadingPolicy(model=BF450)
    first = reading(at=SYNC_START)
    policy.apply([first], SYNC_START)

    again = policy.apply([reading(at=SYNC_START)], SYNC_START + timedelta(seconds=30))

    assert again.fresh == []
    assert again.discarded == []


def test_discarded_readings_are_remembered_too() -> None:
    """A backlog reading delivered twice is reported once."""
    policy = ReadingPolicy(model=BF450)
    old = reading(at=SYNC_START - timedelta(hours=1))
    policy.apply([old], SYNC_START)

    assert policy.apply([old], SYNC_START).discarded == []


def test_another_slot_or_weight_is_not_a_duplicate() -> None:
    """Only (slot, timestamp, weight) together identify a reading."""
    policy = ReadingPolicy(model=BF450)
    policy.apply([reading(at=SYNC_START)], SYNC_START)
    other_slot = reading(at=SYNC_START, slot=3)
    other_weight = reading(frames.STORED_READING_SLOT_1_A, at=SYNC_START)

    result = policy.apply([other_slot, other_weight], SYNC_START)

    assert result.fresh == [other_slot, other_weight]


def test_the_log_survives_a_round_trip_through_storage() -> None:
    """The log is plain data, so HA can persist it and restore it after a restart."""
    policy = ReadingPolicy(model=BF450)
    policy.apply([reading(at=SYNC_START)], SYNC_START)

    restored = ReadingPolicy(policy.log(), model=BF450)

    assert restored.apply([reading(at=SYNC_START)], SYNC_START).fresh == []


def test_the_log_keeps_only_recent_readings() -> None:
    """The log is short: the oldest entries make room for new ones."""
    policy = ReadingPolicy(model=BF450, max_log=2)
    first, second, third = (
        reading(at=SYNC_START - timedelta(seconds=s)) for s in (30, 20, 10)
    )
    policy.apply([first, second, third], SYNC_START)

    assert policy.apply([first], SYNC_START).fresh == [first]
    assert policy.apply([third], SYNC_START).fresh == []

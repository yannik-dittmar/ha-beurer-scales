"""Grouping the frame stream into readings (§9.4b: 2A9D starts every reading)."""

from collections.abc import Iterable
from datetime import datetime

from custom_components.beurer_scales.protocol.models import uuid16
from custom_components.beurer_scales.protocol.readings import ReadingAssembler

from . import frames


def feed(assembler: ReadingAssembler, group: Iterable[tuple[int, bytes]]) -> None:
    """Hand a captured group of frames to the assembler."""
    for short_uuid, frame in group:
        assembler.add(uuid16(short_uuid), frame)


def test_back_to_back_groups_become_separate_readings() -> None:
    """Two stored readings without a separator are split at each 2A9D frame."""
    assembler = ReadingAssembler()
    feed(assembler, frames.STORED_READING_SLOT_1_A)
    feed(assembler, frames.STORED_READING_SLOT_1_B)

    readings = assembler.readings()

    assert [(r.slot, r.timestamp, r.weight_kg) for r in readings] == [
        (1, datetime(2024, 3, 10, 7, 54, 0), 77.9),
        (1, datetime(2024, 3, 10, 7, 58, 34), 77.9),
    ]


def test_a_reading_keeps_its_other_frames() -> None:
    """The frames after 2A9D belong to that reading, by characteristic."""
    assembler = ReadingAssembler()
    feed(assembler, frames.READING_SLOT_2)

    [reading] = assembler.readings()

    assert reading.frames == {
        uuid16(short_uuid): frame for short_uuid, frame in frames.READING_SLOT_2[1:]
    }


def test_a_weight_frame_starts_a_new_reading_even_if_the_last_is_incomplete() -> None:
    """A group cut short is still a reading; the next 2A9D starts the next one."""
    assembler = ReadingAssembler()
    feed(assembler, frames.STORED_READING_SLOT_1_A[:2])
    feed(assembler, frames.STORED_READING_SLOT_1_B)

    first, second = assembler.readings()

    assert set(first.frames) == {uuid16(0x2A9C)}
    assert second.timestamp == datetime(2024, 3, 10, 7, 58, 34)
    assert len(second.frames) == 3


def test_frames_before_any_weight_frame_are_dropped() -> None:
    """Without a 2A9D frame there is no reading to attach a frame to."""
    assembler = ReadingAssembler()
    feed(assembler, frames.READING_SLOT_2[1:])

    assert assembler.readings() == []

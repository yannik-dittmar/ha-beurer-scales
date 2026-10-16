"""The metrics of a reading, as the scale's model defines them."""

from dataclasses import replace

from custom_components.beurer_scales.protocol.metrics import Metric
from custom_components.beurer_scales.protocol.models import BF450, uuid16
from custom_components.beurer_scales.protocol.readings import (
    Reading,
    ReadingAssembler,
    reading_metrics,
)

from . import frames


def assemble(group: tuple[tuple[int, bytes], ...]) -> Reading:
    """The one reading a captured group of frames makes."""
    assembler = ReadingAssembler()
    for short_uuid, frame in group:
        assembler.add(uuid16(short_uuid), frame)
    [reading] = assembler.readings()
    return reading


def test_bf450_reading_metrics() -> None:
    """A BF450 reading yields every metric of its model, in display units (§9.3)."""
    metrics = reading_metrics(assemble(frames.READING_SLOT_2), BF450)

    assert metrics == {
        Metric.BMI: 24.2,
        Metric.BODY_FAT: 18.4,
        Metric.BODY_WATER: 58.5,
        Metric.MUSCLE: 41.2,
        Metric.BONE_MASS: 3.35,
        Metric.BMR: 1758,
        Metric.AMR: 2637,
        Metric.VISCERAL_FAT: 7,
        Metric.METABOLIC_AGE: 34,
        Metric.LEAN_BODY_MASS: 63.97,
        Metric.PROTEIN_MASS: 14.4,
        Metric.TOTAL_MUSCLE_MASS: 43.1,
        Metric.SOFT_LEAN_MASS: 60.6,
        Metric.SUBCUTANEOUS_FAT: 16.1,
        Metric.IDEAL_WEIGHT: 70.47,
        Metric.IMPEDANCE: 512.0,
    }


def test_the_model_decides_which_metrics_a_reading_has() -> None:
    """A model without a metric never reports it."""
    model = replace(BF450, metrics=(Metric.BMI, Metric.BODY_FAT))

    metrics = reading_metrics(assemble(frames.READING_SLOT_2), model)

    assert metrics == {Metric.BMI: 24.2, Metric.BODY_FAT: 18.4}


def test_missing_frames_leave_their_metrics_out() -> None:
    """Without custom2, body water falls back to water mass / weight (§5.3)."""
    group = tuple(f for f in frames.READING_SLOT_2 if f[0] != 0xFFF6)

    metrics = reading_metrics(assemble(group), BF450)

    assert metrics[Metric.BODY_WATER] == 58.5
    assert Metric.BONE_MASS not in metrics
    assert metrics[Metric.AMR] == 2637


def test_a_malformed_frame_leaves_its_metrics_out() -> None:
    """A frame with the wrong length is skipped; the others still count."""
    group = tuple(
        (short_uuid, frame[:-1] if short_uuid == 0xFFF5 else frame)
        for short_uuid, frame in frames.READING_SLOT_2
    )

    metrics = reading_metrics(assemble(group), BF450)

    assert Metric.AMR not in metrics
    assert Metric.IDEAL_WEIGHT not in metrics
    assert metrics[Metric.BMR] == 1758

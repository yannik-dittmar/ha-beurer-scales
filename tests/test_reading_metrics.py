"""The metrics of a reading, as the scale's model defines them."""

from dataclasses import replace

from custom_components.beurer_scales.protocol.metrics import Metric
from custom_components.beurer_scales.protocol.models import BF450, model_by_name, uuid16
from custom_components.beurer_scales.protocol.readings import (
    Reading,
    ReadingAssembler,
    reading_metrics,
)

from . import frames, test_segment_frames


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


# The §9.3 weight and body composition frames, as any BF600-family model sends them.
WEIGHT_AND_BODY = frames.READING_SLOT_2[:2]


def test_segmental_frames_add_their_metrics() -> None:
    """A BF1000 reading carries the upper/lower and segmental analyses (§5.4)."""
    group = (
        *WEIGHT_AND_BODY,
        (0x0008, test_segment_frames.SEGMENT_BODY),
        (0x0009, test_segment_frames.SEGMENT_FAT),
        (0x000A, test_segment_frames.SEGMENT_MUSCLE),
    )

    metrics = reading_metrics(assemble(group), model_by_name("BF1000"))

    assert metrics[Metric.BODY_FAT] == 18.4
    assert metrics[Metric.UPPER_BODY_FAT] == 21.3
    assert metrics[Metric.LOWER_BODY_MUSCLE] == 44.1
    assert metrics[Metric.VISCERAL_FAT] == 8.5
    assert metrics[Metric.TORSO_FAT] == 19.8
    assert metrics[Metric.TORSO_MUSCLE] == 28.4
    # Body masses are in 0.005 kg on models other than the BF450 type (§5.2).
    assert metrics[Metric.SOFT_LEAN_MASS] == 3.03


def test_fff5_is_a_heart_rate_frame_on_heart_rate_models() -> None:
    """The BF500 sends a heart-rate frame on FFF5, not the BF450's custom1."""
    group = (*WEIGHT_AND_BODY, (0xFFF5, test_segment_frames.HEART_RATE))

    metrics = reading_metrics(assemble(group), model_by_name("BF500"))

    assert metrics[Metric.HEART_RATE] == 72
    assert metrics[Metric.VISCERAL_FAT] == 9.5
    assert metrics[Metric.METABOLIC_AGE] == 41
    assert Metric.AMR not in metrics


def test_a_weight_only_model_has_just_the_bmi() -> None:
    """The GS435 measures no body composition; stray frames are ignored."""
    metrics = reading_metrics(assemble(frames.READING_SLOT_2), model_by_name("GS435"))

    assert metrics == {Metric.BMI: 24.2}


def test_a_malformed_segment_frame_leaves_its_metrics_out() -> None:
    """A wrong segment frame drops only its own metrics."""
    group = (
        *WEIGHT_AND_BODY,
        (0x0008, test_segment_frames.SEGMENT_BODY[:-1]),
        (0x0009, test_segment_frames.SEGMENT_FAT),
    )

    metrics = reading_metrics(assemble(group), model_by_name("BF1000"))

    assert Metric.UPPER_BODY_FAT not in metrics
    assert metrics[Metric.LEFT_ARM_FAT] == 24.1

"""The BF990's frames (protocol doc §5.4, §9.7).

The layout was verified on a real BF990 (firmware V02.13); the frames below
re-encode one captured weigh-in with the fictional person of §9.3 (born
1985-04-12, 180 cm, male, activity 3). Frame layout and the relations between
fields (BMI, LBM = weight × (1 − fat), AMR = BMR × 1.5) are as captured.
"""

from datetime import datetime

import pytest

from custom_components.beurer_scales.protocol.frames import FrameError
from custom_components.beurer_scales.protocol.metrics import (
    Metric,
    decode_full_body_frame,
    decode_heart_rate_extended_frame,
    decode_measurement_status_frame,
)
from custom_components.beurer_scales.protocol.models import model_by_name, uuid16
from custom_components.beurer_scales.protocol.policy import Discard, classify, guessed
from custom_components.beurer_scales.protocol.readings import (
    Reading,
    ReadingAssembler,
    reading_metrics,
)

BF990 = model_by_name("BF990")

# 78.4 kg, 2024-03-10 07:12:30, slot 1, BMI 24.2, height in mm (1800).
WEIGHT = bytes.fromhex("0e 40 3d e8 07 03 0a 07 0c 1e 01 f2 00 08 07")
# Fat 18.4 %, BMR 7360 kJ, muscle 41.2 %, soft lean 60.6 kg and water 45.9 kg
# (× 0.005 kg), 512.0 Ω.
BODY = bytes.fromhex("98 03 b8 00 c0 1c 9c 01 58 2f dc 23 00 14")
# Pulse 72, visceral 7.0, metabolic age 34, BMR 7360 kJ, AMR 11040 kJ, bone
# 3.35 kg, LBM 63.97 kg, protein 14.4 kg, total muscle 43.1 kg, water 58.5 %.
HEART_RATE_EXTENDED = bytes.fromhex(
    "0e 48 00 46 00 22 c0 1c 20 2b 9e 02 fa 31 40 0b ac 21 49 02"
)
# Fat: legs 20.0/19.8, arms 28.0/27.8, torso 18.0; muscle: legs 41.6/41.8,
# arms 9.0/9.2, torso 30.0 (left before right).
FULL_BODY = bytes.fromhex("c8 00 c6 00 18 01 16 01 b4 00 a0 01 a2 01 5a 00 5c 00 2c 01")
# Impedances 512 Ω and 530 Ω, user recognised.
STATUS = bytes.fromhex("00 02 12 02 00 00")

# A weigh-in without bioimpedance (feet not in contact): weight only, BMR/AMR
# from the profile, no pulse (ff ff), segmental frame and status all zeros.
BODY_NO_BIA = bytes.fromhex("98 03 00 00 c0 1c 00 00 00 00 00 00 00 00")
HEART_RATE_NO_BIA = bytes.fromhex(
    "0e ff ff 00 00 00 c0 1c 20 2b 00 00 00 00 00 00 00 00 00 00"
)
ZEROS_20 = bytes(20)
ZEROS_6 = bytes(6)

SEGMENTS = {
    Metric.LEFT_LEG_FAT: 20.0,
    Metric.RIGHT_LEG_FAT: 19.8,
    Metric.LEFT_ARM_FAT: 28.0,
    Metric.RIGHT_ARM_FAT: 27.8,
    Metric.TORSO_FAT: 18.0,
    Metric.LEFT_LEG_MUSCLE: 41.6,
    Metric.RIGHT_LEG_MUSCLE: 41.8,
    Metric.LEFT_ARM_MUSCLE: 9.0,
    Metric.RIGHT_ARM_MUSCLE: 9.2,
    Metric.TORSO_MUSCLE: 30.0,
}


def assemble(*frames: tuple[int, bytes]) -> Reading:
    """The one reading a group of frames makes, in the scale's order."""
    assembler = ReadingAssembler()
    for short_uuid, frame in frames:
        assembler.add(uuid16(short_uuid), frame)
    [reading] = assembler.readings()
    return reading


def full_reading(status: bytes = STATUS) -> Reading:
    return assemble(
        (0x2A9D, WEIGHT),
        (0x2A9C, BODY),
        (0xFFF5, HEART_RATE_EXTENDED),
        (0xFFF7, FULL_BODY),
        (0xFFF8, status),
    )


def test_heart_rate_extended_frame() -> None:
    """FFF5 carries the pulse and the extended analysis."""
    assert decode_heart_rate_extended_frame(HEART_RATE_EXTENDED) == {
        Metric.HEART_RATE: 72,
        Metric.VISCERAL_FAT: 7.0,
        Metric.METABOLIC_AGE: 34,
        Metric.BMR: 1758,
        Metric.AMR: 2637,
        Metric.BONE_MASS: 3.35,
        Metric.LEAN_BODY_MASS: 63.97,
        Metric.PROTEIN_MASS: 14.4,
        Metric.TOTAL_MUSCLE_MASS: 43.1,
        Metric.BODY_WATER: 58.5,
    }


def test_no_pulse_and_no_bioimpedance() -> None:
    """`ff ff` means no pulse; zeros mean the value wasn't measured."""
    decoded = decode_heart_rate_extended_frame(HEART_RATE_NO_BIA)

    assert {m: v for m, v in decoded.items() if v is not None} == {
        Metric.BMR: 1758,
        Metric.AMR: 2637,
    }


def test_full_body_frame() -> None:
    """FFF7 has fat, then muscle, of each leg, each arm and the torso."""
    assert decode_full_body_frame(FULL_BODY) == SEGMENTS


def test_measurement_status_frame() -> None:
    """FFF8 has two impedances and the unknown-user flag."""
    status = decode_measurement_status_frame(STATUS)

    assert (status.impedance_ohm, status.second_impedance_ohm) == (512, 530)
    assert not status.guessed


@pytest.mark.parametrize(
    ("decode", "frame"),
    [
        (decode_heart_rate_extended_frame, HEART_RATE_EXTENDED[:-1]),
        (decode_full_body_frame, FULL_BODY[:-1]),
        (decode_measurement_status_frame, STATUS[:-1]),
    ],
)
def test_short_frames_are_rejected(decode: object, frame: bytes) -> None:
    with pytest.raises(FrameError):
        decode(frame)  # type: ignore[operator]


def test_bf990_reading_metrics() -> None:
    """A full BF990 reading yields every metric, the segments included."""
    metrics = reading_metrics(full_reading(), BF990)

    assert metrics == {
        Metric.BMI: 24.2,
        Metric.BODY_FAT: 18.4,
        Metric.BODY_WATER: 58.5,
        Metric.MUSCLE: 41.2,
        Metric.BONE_MASS: 3.35,
        Metric.BMR: 1758,
        Metric.AMR: 2637,
        Metric.VISCERAL_FAT: 7.0,
        Metric.METABOLIC_AGE: 34,
        Metric.LEAN_BODY_MASS: 63.97,
        Metric.PROTEIN_MASS: 14.4,
        Metric.TOTAL_MUSCLE_MASS: 43.1,
        Metric.SOFT_LEAN_MASS: 60.6,
        Metric.IMPEDANCE: 512.0,
        Metric.HEART_RATE: 72,
        **SEGMENTS,
    }


def test_a_weigh_in_without_bioimpedance_has_no_body_metrics() -> None:
    """Zeroed frames leave their metrics out instead of reporting 0."""
    reading = assemble(
        (0x2A9D, WEIGHT),
        (0x2A9C, BODY_NO_BIA),
        (0xFFF5, HEART_RATE_NO_BIA),
        (0xFFF7, ZEROS_20),
        (0xFFF8, ZEROS_6),
    )

    assert reading_metrics(reading, BF990) == {
        Metric.BMI: 24.2,
        Metric.BMR: 1758,
        Metric.AMR: 2637,
    }


def test_the_unknown_user_flag_marks_a_guessed_reading() -> None:
    """FFF8 bytes 4..5 > 0: the scale wasn't sure whose reading it is."""
    reading = full_reading(status=bytes.fromhex("00 02 12 02 01 00"))
    sync_start = datetime(2024, 3, 10, 7, 13)

    assert guessed(reading, BF990)
    assert classify(reading, sync_start, BF990) is Discard.GUESSED
    assert classify(full_reading(), sync_start, BF990) is None

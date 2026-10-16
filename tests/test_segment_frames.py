"""Decoders for the frames of models other than the BF450 (protocol doc §5.4).

No captures exist for these models: the frames below are built by hand from the
layouts in §5.4.
"""

import pytest

from custom_components.beurer_scales.protocol.frames import FrameError
from custom_components.beurer_scales.protocol.metrics import (
    Metric,
    decode_heart_rate_frame,
    decode_segment_body_frame,
    decode_segment_fat_frame,
    decode_segment_muscle_frame,
)

# 0x0008: flags 1e, upper fat 21.3 %, lower fat 25.0 %, upper muscle 38.4 %,
# lower muscle 44.1 %.
SEGMENT_BODY = bytes.fromhex("1e d5 00 fa 00 80 01 b9 01")
# 0x0009: flags 7e, visceral 8.5, left arm 24.1 %, right arm 23.9 %, left leg
# 27.2 %, right leg 27.0 %, torso 19.8 %.
SEGMENT_FAT = bytes.fromhex("7e 55 f1 00 ef 00 10 01 0e 01 c6 00")
# 0x000A: flags 3e, left arm 3.2, right arm 3.3, left leg 10.1, right leg 10.2,
# torso 28.4.
SEGMENT_MUSCLE = bytes.fromhex("3e 20 00 21 00 65 00 66 00 1c 01")
# FFF5 on BF500/BF880/BF980: pulse 72 bpm, visceral 9.5, metabolic age 41.
HEART_RATE = bytes.fromhex("00 48 00 5f 00 29")


def test_upper_and_lower_body_analysis() -> None:
    """0x0008 has fat and muscle of the upper and lower body."""
    assert decode_segment_body_frame(SEGMENT_BODY) == {
        Metric.UPPER_BODY_FAT: 21.3,
        Metric.LOWER_BODY_FAT: 25.0,
        Metric.UPPER_BODY_MUSCLE: 38.4,
        Metric.LOWER_BODY_MUSCLE: 44.1,
    }


def test_segmental_fat_analysis() -> None:
    """0x0009 has the visceral fat and the fat of each limb and the torso."""
    assert decode_segment_fat_frame(SEGMENT_FAT) == {
        Metric.VISCERAL_FAT: 8.5,
        Metric.LEFT_ARM_FAT: 24.1,
        Metric.RIGHT_ARM_FAT: 23.9,
        Metric.LEFT_LEG_FAT: 27.2,
        Metric.RIGHT_LEG_FAT: 27.0,
        Metric.TORSO_FAT: 19.8,
    }


def test_segmental_muscle_analysis() -> None:
    """0x000A has the muscle of each limb and the torso."""
    assert decode_segment_muscle_frame(SEGMENT_MUSCLE) == {
        Metric.LEFT_ARM_MUSCLE: 3.2,
        Metric.RIGHT_ARM_MUSCLE: 3.3,
        Metric.LEFT_LEG_MUSCLE: 10.1,
        Metric.RIGHT_LEG_MUSCLE: 10.2,
        Metric.TORSO_MUSCLE: 28.4,
    }


def test_heart_rate_frame() -> None:
    """The heart-rate frame has the pulse, visceral fat and metabolic age."""
    assert decode_heart_rate_frame(HEART_RATE) == {
        Metric.HEART_RATE: 72,
        Metric.VISCERAL_FAT: 9.5,
        Metric.METABOLIC_AGE: 41,
    }


@pytest.mark.parametrize("pulse", ["ff 00", "ff ff", "00 00"])
def test_heart_rate_frame_without_a_pulse(pulse: str) -> None:
    """0xFF (or 0) means the scale measured no pulse."""
    frame = bytes.fromhex(f"00 {pulse} 5f 00 29")

    assert decode_heart_rate_frame(frame)[Metric.HEART_RATE] is None


def test_zero_means_not_measured() -> None:
    """As in the other frames, a raw 0 is an absent value."""
    frame = bytes.fromhex("1e 00 00 fa 00 80 01 b9 01")

    assert decode_segment_body_frame(frame)[Metric.UPPER_BODY_FAT] is None


@pytest.mark.parametrize(
    ("decode", "frame"),
    [
        (decode_segment_body_frame, SEGMENT_BODY[:-1]),
        (decode_segment_body_frame, b"\x00" + SEGMENT_BODY[1:]),
        (decode_segment_fat_frame, SEGMENT_FAT + b"\x00"),
        (decode_segment_fat_frame, b"\x3e" + SEGMENT_FAT[1:]),
        (decode_segment_muscle_frame, SEGMENT_MUSCLE[:-1]),
        (decode_segment_muscle_frame, b"\x7e" + SEGMENT_MUSCLE[1:]),
        (decode_heart_rate_frame, HEART_RATE[:-1]),
    ],
    ids=[
        "body-short",
        "body-flags",
        "fat-long",
        "fat-flags",
        "muscle-short",
        "muscle-flags",
        "heart-rate-short",
    ],
)
def test_malformed_frames_are_rejected(decode: object, frame: bytes) -> None:
    """A wrong length or flags byte is not decoded (the app checks both)."""
    with pytest.raises(FrameError):
        decode(frame)  # type: ignore[operator]

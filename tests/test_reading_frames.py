"""Decoding the pending-slot list and readings, on frames captured from a BF450."""

from datetime import datetime

import pytest

from custom_components.beurer_scales.protocol.frames import (
    FrameError,
    WeightUnit,
    decode_pending_slots,
    decode_weight_frame,
)

from . import frames


@pytest.mark.parametrize(
    ("payload", "slots"),
    [(b"", ()), (b"\x01", (1,)), (b"\x01\x02", (1, 2))],
    ids=["none", "one", "several"],
)
def test_pending_slot_list(payload: bytes, slots: tuple[int, ...]) -> None:
    """The 0x1D18 service data is one byte per pending slot (§2)."""
    assert decode_pending_slots(payload) == slots


def test_weight_frame() -> None:
    """2A9D holds the weight, the scale's local timestamp and the slot (§9.3)."""
    weight = decode_weight_frame(dict(frames.READING_SLOT_2)[0x2A9D])

    assert weight.weight_kg == 78.4
    assert weight.timestamp == datetime(2024, 3, 10, 7, 12, 30)
    assert weight.slot == 2
    assert weight.bmi == 24.2
    assert weight.height_cm == 180


def test_offline_weight_frame() -> None:
    """Another captured weight decodes with the 0.005 kg resolution."""
    weight = decode_weight_frame(frames.OFFLINE_WEIGHT_SLOT_2)

    assert weight.weight_kg == 78.2
    assert weight.timestamp == datetime(2024, 3, 10, 7, 17, 9)


@pytest.mark.parametrize(
    "frame", [b"", frames.OFFLINE_WEIGHT_SLOT_2[:-1]], ids=["empty", "short"]
)
def test_malformed_weight_frames_are_rejected(frame: bytes) -> None:
    """A weight frame that isn't 15 bytes raises FrameError (§9.4)."""
    with pytest.raises(FrameError):
        decode_weight_frame(frame)


def test_the_bf450_weight_frame_is_in_kg() -> None:
    """The BF450 sends flags 0x02: bit 0 clear means SI units (§5.1)."""
    weight = decode_weight_frame(frames.OFFLINE_WEIGHT_SLOT_2)

    assert weight.unit is WeightUnit.KG


def test_a_weight_frame_with_the_imperial_flag_is_in_pounds() -> None:
    """Flags bit 0 set means imperial units (0x2A9D spec; unverified on BF450, V5)."""
    frame = (
        bytes([frames.OFFLINE_WEIGHT_SLOT_2[0] | 0x01])
        + frames.OFFLINE_WEIGHT_SLOT_2[1:]
    )

    assert decode_weight_frame(frame).unit is WeightUnit.LB

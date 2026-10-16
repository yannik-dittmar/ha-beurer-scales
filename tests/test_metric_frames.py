"""Decoding the body-composition and BF450 custom frames captured from a BF450."""

import pytest

from custom_components.beurer_scales.protocol.frames import FrameError
from custom_components.beurer_scales.protocol.metrics import (
    decode_body_composition_frame,
    decode_custom1_frame,
    decode_custom2_frame,
)

from . import frames

READING = dict(frames.READING_SLOT_2)
# FFF5 of the §9.3 weigh-in whose display showed U-- before settling on U-2.
GUESSED_CUSTOM1 = bytes.fromhex("0e 37 20 2b 02")


def test_body_composition_frame_with_bf450_scaling() -> None:
    """2A9C on the BF450: soft lean and water mass in 0.1 kg, BMR in kJ (§5.2, §9.3)."""
    body = decode_body_composition_frame(READING[0x2A9C], mass_resolution_kg=0.1)

    assert body.body_fat_percent == 18.4
    assert body.bmr_kcal == 1758
    assert body.muscle_percent == 41.2
    assert body.soft_lean_mass_kg == 60.6
    assert body.body_water_mass_kg == 45.9
    assert body.impedance_ohm == 512.0


def test_body_composition_frame_with_default_scaling() -> None:
    """Other models send soft lean and water mass in 0.005 kg (§5.2)."""
    body = decode_body_composition_frame(READING[0x2A9C], mass_resolution_kg=0.005)

    assert body.soft_lean_mass_kg == 3.03
    assert body.body_water_mass_kg == 2.295


def test_absent_body_composition_values_are_none() -> None:
    """A raw 0 means the scale didn't measure that value (§5.2)."""
    body = decode_body_composition_frame(bytes(14), mass_resolution_kg=0.1)

    assert body.body_fat_percent is None
    assert body.bmr_kcal is None
    assert body.muscle_percent is None
    assert body.soft_lean_mass_kg is None
    assert body.body_water_mass_kg is None
    assert body.impedance_ohm is None


@pytest.mark.parametrize("frame", [b"", READING[0x2A9C][:-1]], ids=["empty", "short"])
def test_malformed_body_composition_frames_are_rejected(frame: bytes) -> None:
    """A body-composition frame that isn't 14 bytes raises FrameError."""
    with pytest.raises(FrameError):
        decode_body_composition_frame(frame, mass_resolution_kg=0.1)


def test_custom1_frame() -> None:
    """FFF5 holds the ideal weight and the AMR in kJ (§5.3, §9.3)."""
    custom1 = decode_custom1_frame(READING[0xFFF5])

    assert custom1.ideal_weight_kg == 70.47
    assert custom1.amr_kcal == 2637
    assert custom1.guess == 0
    assert not custom1.guessed


def test_custom1_frame_of_a_guessed_reading() -> None:
    """Byte 4 is not 0 when the scale had to guess the scale user (§9.4)."""
    custom1 = decode_custom1_frame(GUESSED_CUSTOM1)

    assert custom1.guess == 2
    assert custom1.guessed


@pytest.mark.parametrize("frame", [b"", READING[0xFFF5][:-1]], ids=["empty", "short"])
def test_malformed_custom1_frames_are_rejected(frame: bytes) -> None:
    """A custom1 frame that isn't 5 bytes raises FrameError."""
    with pytest.raises(FrameError):
        decode_custom1_frame(frame)


def test_custom2_frame() -> None:
    """FFF6 holds the extended body-composition values (§5.3, §9.3)."""
    custom2 = decode_custom2_frame(READING[0xFFF6])

    assert custom2.visceral_fat == 7
    assert custom2.metabolic_age_years == 34
    assert custom2.lean_body_mass_kg == 63.97
    assert custom2.protein_mass_kg == 14.4
    assert custom2.total_muscle_mass_kg == 43.1
    assert custom2.subcutaneous_fat_percent == 16.1
    assert custom2.bone_mass_kg == 3.35
    assert custom2.body_water_percent == 58.5


def test_absent_custom2_values_are_none() -> None:
    """A raw 0 is absent, and so is a metabolic age outside 1..99 (§5.3)."""
    frame = bytearray(16)
    frame[2] = 120
    custom2 = decode_custom2_frame(bytes(frame))

    assert custom2.visceral_fat is None
    assert custom2.metabolic_age_years is None
    assert custom2.lean_body_mass_kg is None
    assert custom2.bone_mass_kg is None
    assert custom2.body_water_percent is None


@pytest.mark.parametrize("frame", [b"", READING[0xFFF6][:-1]], ids=["empty", "short"])
def test_malformed_custom2_frames_are_rejected(frame: bytes) -> None:
    """A custom2 frame that isn't 16 bytes raises FrameError."""
    with pytest.raises(FrameError):
        decode_custom2_frame(frame)

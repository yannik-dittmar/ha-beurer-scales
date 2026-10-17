"""Body-composition metrics: the 2A9C and BF450 custom frames (protocol doc §5.2, §5.3).

A raw value of 0 means the scale didn't measure that value; it decodes to None.
"""

from dataclasses import dataclass
from enum import StrEnum

from .frames import FrameError


class Metric(StrEnum):
    """A body-composition value a reading can carry besides the weight."""

    BMI = "bmi"
    BODY_FAT = "body_fat"
    BODY_WATER = "body_water"
    MUSCLE = "muscle"
    BONE_MASS = "bone_mass"
    BMR = "bmr"
    AMR = "amr"
    VISCERAL_FAT = "visceral_fat"
    METABOLIC_AGE = "metabolic_age"
    LEAN_BODY_MASS = "lean_body_mass"
    PROTEIN_MASS = "protein_mass"
    TOTAL_MUSCLE_MASS = "total_muscle_mass"
    SOFT_LEAN_MASS = "soft_lean_mass"
    SUBCUTANEOUS_FAT = "subcutaneous_fat"
    IDEAL_WEIGHT = "ideal_weight"
    IMPEDANCE = "impedance"
    # Heart-rate frame (BF500/BF880/BF980, §5.4).
    HEART_RATE = "heart_rate"
    # Upper/lower body analysis 0x0008 (BF105/BF1000, §5.4).
    UPPER_BODY_FAT = "upper_body_fat"
    LOWER_BODY_FAT = "lower_body_fat"
    UPPER_BODY_MUSCLE = "upper_body_muscle"
    LOWER_BODY_MUSCLE = "lower_body_muscle"
    # Segmental fat 0x0009 and muscle 0x000A analysis (BF1000, §5.4).
    LEFT_ARM_FAT = "left_arm_fat"
    RIGHT_ARM_FAT = "right_arm_fat"
    LEFT_LEG_FAT = "left_leg_fat"
    RIGHT_LEG_FAT = "right_leg_fat"
    TORSO_FAT = "torso_fat"
    LEFT_ARM_MUSCLE = "left_arm_muscle"
    RIGHT_ARM_MUSCLE = "right_arm_muscle"
    LEFT_LEG_MUSCLE = "left_leg_muscle"
    RIGHT_LEG_MUSCLE = "right_leg_muscle"
    TORSO_MUSCLE = "torso_muscle"


# The app converts the scale's kJ to kcal with this factor (§5.2).
_KJ_PER_KCAL = 4.1868


def _uint16(frame: bytes, offset: int) -> int:
    return int.from_bytes(frame[offset : offset + 2], "little")


def _scaled(raw: int, factor: float, digits: int) -> float | None:
    return round(raw * factor, digits) if raw else None


def _kcal(raw_kj: int) -> int | None:
    return round(raw_kj / _KJ_PER_KCAL) if raw_kj else None


def _check_length(name: str, frame: bytes, length: int) -> None:
    if len(frame) != length:
        raise FrameError(f"{name} frame of {len(frame)} bytes: {frame.hex()}")


@dataclass(frozen=True, slots=True)
class BodyCompositionFrame:
    """A Body Composition (0x2A9C) frame."""

    body_fat_percent: float | None
    bmr_kcal: int | None
    muscle_percent: float | None
    soft_lean_mass_kg: float | None
    body_water_mass_kg: float | None
    impedance_ohm: float | None


def decode_body_composition_frame(
    frame: bytes, *, mass_resolution_kg: float
) -> BodyCompositionFrame:
    """Decode 0x2A9C. The flags are ignored, as the app does (§5.2).

    `mass_resolution_kg` is the model's unit for soft lean and body water mass
    (BF450: 0.1 kg, others 0.005 kg).
    """
    _check_length("body composition", frame, 14)
    return BodyCompositionFrame(
        body_fat_percent=_scaled(_uint16(frame, 2), 0.1, 1),
        bmr_kcal=_kcal(_uint16(frame, 4)),
        muscle_percent=_scaled(_uint16(frame, 6), 0.1, 1),
        soft_lean_mass_kg=_scaled(_uint16(frame, 8), mass_resolution_kg, 3),
        body_water_mass_kg=_scaled(_uint16(frame, 10), mass_resolution_kg, 3),
        impedance_ohm=_scaled(_uint16(frame, 12), 0.1, 1),
    )


@dataclass(frozen=True, slots=True)
class Custom1Frame:
    """The BF450's custom1 (FFFF/FFF5) frame."""

    ideal_weight_kg: float | None
    amr_kcal: int | None
    # Byte 4: 0 when the scale recognised the scale user directly, otherwise
    # (working hypothesis) the slot it guessed (§9.4).
    guess: int

    @property
    def guessed(self) -> bool:
        """Whether the scale only guessed whose reading this is (ADR 0006)."""
        return self.guess != 0


def decode_custom1_frame(frame: bytes) -> Custom1Frame:
    """Decode FFF5 (§5.3)."""
    _check_length("custom1", frame, 5)
    return Custom1Frame(
        ideal_weight_kg=_scaled(_uint16(frame, 0), 0.005, 3),
        amr_kcal=_kcal(_uint16(frame, 2)),
        guess=frame[4],
    )


@dataclass(frozen=True, slots=True)
class Custom2Frame:
    """The BF450's custom2 (FFFF/FFF6) frame. Body shape index is not decoded."""

    visceral_fat: int | None
    metabolic_age_years: int | None
    lean_body_mass_kg: float | None
    protein_mass_kg: float | None
    total_muscle_mass_kg: float | None
    subcutaneous_fat_percent: float | None
    bone_mass_kg: float | None
    body_water_percent: float | None


def decode_custom2_frame(frame: bytes) -> Custom2Frame:
    """Decode FFF6 (§5.3)."""
    _check_length("custom2", frame, 16)
    metabolic_age = frame[2]
    return Custom2Frame(
        visceral_fat=_uint16(frame, 0) or None,
        metabolic_age_years=metabolic_age if 1 <= metabolic_age <= 99 else None,
        lean_body_mass_kg=_scaled(_uint16(frame, 3), 0.005, 3),
        protein_mass_kg=_scaled(_uint16(frame, 5), 0.005, 3),
        total_muscle_mass_kg=_scaled(_uint16(frame, 7), 0.005, 3),
        subcutaneous_fat_percent=_scaled(_uint16(frame, 10), 0.1, 1),
        bone_mass_kg=_scaled(_uint16(frame, 12), 0.005, 3),
        body_water_percent=_scaled(_uint16(frame, 14), 0.1, 1),
    )


# --- Frames of other BF600-family models (§5.4) ---
#
# Decoded from the app's parsers only; no model sending them has been tested.
# They decode straight to metrics. Fat and muscle values are × 0.1 (assumed %).

_SEGMENT_BODY_METRICS = (
    Metric.UPPER_BODY_FAT,
    Metric.LOWER_BODY_FAT,
    Metric.UPPER_BODY_MUSCLE,
    Metric.LOWER_BODY_MUSCLE,
)
_SEGMENT_FAT_LIMBS = (
    Metric.LEFT_ARM_FAT,
    Metric.RIGHT_ARM_FAT,
    Metric.LEFT_LEG_FAT,
    Metric.RIGHT_LEG_FAT,
    Metric.TORSO_FAT,
)
_SEGMENT_MUSCLE_METRICS = (
    Metric.LEFT_ARM_MUSCLE,
    Metric.RIGHT_ARM_MUSCLE,
    Metric.LEFT_LEG_MUSCLE,
    Metric.RIGHT_LEG_MUSCLE,
    Metric.TORSO_MUSCLE,
)


def _check_frame(name: str, frame: bytes, length: int, flags: int) -> None:
    _check_length(name, frame, length)
    if frame[0] != flags:
        raise FrameError(f"{name} frame with flags {frame[0]:02x}: {frame.hex()}")


def _tenths(
    frame: bytes, offset: int, metrics: tuple[Metric, ...]
) -> dict[Metric, float | None]:
    return {
        metric: _scaled(_uint16(frame, offset + 2 * i), 0.1, 1)
        for i, metric in enumerate(metrics)
    }


def decode_segment_body_frame(frame: bytes) -> dict[Metric, float | None]:
    """Decode the upper/lower body analysis 0x0008: 9 bytes, flags 1e."""
    _check_frame("upper/lower body", frame, 9, 0x1E)
    return _tenths(frame, 1, _SEGMENT_BODY_METRICS)


def decode_segment_fat_frame(frame: bytes) -> dict[Metric, float | None]:
    """Decode the segmental fat analysis 0x0009: 12 bytes, flags 7e.

    The visceral fat is one byte, the five segments two bytes each.
    """
    _check_frame("segmental fat", frame, 12, 0x7E)
    return {
        Metric.VISCERAL_FAT: _scaled(frame[1], 0.1, 1),
        **_tenths(frame, 2, _SEGMENT_FAT_LIMBS),
    }


def decode_segment_muscle_frame(frame: bytes) -> dict[Metric, float | None]:
    """Decode the segmental muscle analysis 0x000A: 11 bytes, flags 3e."""
    _check_frame("segmental muscle", frame, 11, 0x3E)
    return _tenths(frame, 1, _SEGMENT_MUSCLE_METRICS)


def decode_heart_rate_frame(frame: bytes) -> dict[Metric, float | None]:
    """Decode the heart-rate frame on FFF5 of the BF500/BF880/BF980: 6 bytes."""
    _check_length("heart rate", frame, 6)
    pulse = _uint16(frame, 1)
    metabolic_age = frame[5]
    return {
        Metric.HEART_RATE: pulse if pulse not in (0, 0xFF, 0xFFFF) else None,
        Metric.VISCERAL_FAT: _scaled(_uint16(frame, 3), 0.1, 1),
        Metric.METABOLIC_AGE: metabolic_age if 1 <= metabolic_age <= 99 else None,
    }

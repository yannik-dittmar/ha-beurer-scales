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

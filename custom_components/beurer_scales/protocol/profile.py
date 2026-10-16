"""A scale user's profile and how the BF600 family stores it (protocol doc §4)."""

from dataclasses import dataclass
from datetime import date

from .frames import Gender


@dataclass(frozen=True, slots=True)
class Profile:
    """What the scale needs to compute body composition for a scale user."""

    birth_date: date
    height_cm: int
    gender: Gender
    # 1 (none) to 5 (very high).
    activity_level: int


def encode_birth_date(profile: Profile) -> bytes:
    """Date of Birth 0x2A85: `[year_lo, year_hi, month, day]`."""
    birth = profile.birth_date
    return bytes([*birth.year.to_bytes(2, "little"), birth.month, birth.day])


def encode_gender(profile: Profile) -> bytes:
    """Gender 0x2A8C: `[0]` male, `[1]` female."""
    return bytes([1 if profile.gender is Gender.FEMALE else 0])


def encode_height(profile: Profile) -> bytes:
    """Height 0x2A8E: `[cm, 0]` (BF450, §9.2 step 7)."""
    return bytes([profile.height_cm, 0])


def encode_activity_level(profile: Profile) -> bytes:
    """The model's custom activity characteristic: `[level]`."""
    return bytes([profile.activity_level])


# Database Change Increment 0x2A99 as the app writes it after a profile change.
DATABASE_CHANGE_INCREMENT = bytes([0x01, 0x00, 0x00, 0x00])

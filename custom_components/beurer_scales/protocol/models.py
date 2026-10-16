"""Supported scale models and how to recognise them in an advertisement.

The model table covers the BF600 (UDS-based) family of protocol doc §1 and §7.
Only the BF450/BF451 are verified on real hardware; the rest follows the app.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .metrics import Metric

# The BF450 advertises the Weight Scale service (0x181D) byte-swapped, as 0x1D18;
# its service data holds the pending-slot list (protocol doc §2).
SWAPPED_WEIGHT_SCALE_UUID = "00001d18-0000-1000-8000-00805f9b34fb"
WEIGHT_SCALE_SERVICE_UUID = "0000181d-0000-1000-8000-00805f9b34fb"


def uuid16(short: int) -> str:
    """Expand a 16-bit UUID with the Bluetooth SIG base."""
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


# Characteristics every BF600-family model shares (protocol doc §1).
CURRENT_TIME_UUID = uuid16(0x2A2B)
USER_CONTROL_POINT_UUID = uuid16(0x2A9F)
# User Data Service profile characteristics; writable only while consented.
DATE_OF_BIRTH_UUID = uuid16(0x2A85)
GENDER_UUID = uuid16(0x2A8C)
HEIGHT_UUID = uuid16(0x2A8E)
DATABASE_CHANGE_INCREMENT_UUID = uuid16(0x2A99)
BATTERY_LEVEL_UUID = uuid16(0x2A19)
# Starts every reading (§9.4b).
WEIGHT_MEASUREMENT_UUID = uuid16(0x2A9D)
BODY_COMPOSITION_UUID = uuid16(0x2A9C)
# The BF450/BF451 custom measurement frames (§5.3). Other models send other
# frames on FFF5 (§5.4); the model's reading frames say how to decode them.
CUSTOM1_UUID = uuid16(0xFFF5)
CUSTOM2_UUID = uuid16(0xFFF6)
# Segmental analysis frames (BF105/BF1000, §5.4).
SEGMENT_BODY_UUID = uuid16(0x0008)
SEGMENT_FAT_UUID = uuid16(0x0009)
SEGMENT_MUSCLE_UUID = uuid16(0x000A)


class FrameKind(StrEnum):
    """How a measurement characteristic's frames are decoded (protocol doc §5)."""

    WEIGHT = "weight"
    BODY_COMPOSITION = "body_composition"
    # BF450/BF451 custom frames (§5.3).
    CUSTOM1 = "custom1"
    CUSTOM2 = "custom2"
    # Heart-rate frame on FFF5 (BF500/BF880/BF980, §5.4).
    HEART_RATE = "heart_rate"
    # Segmental analysis (BF105/BF1000, §5.4).
    SEGMENT_BODY = "segment_body"
    SEGMENT_FAT = "segment_fat"
    SEGMENT_MUSCLE = "segment_muscle"


# The metrics each kind of frame carries; a model measures those of its frames.
FRAME_METRICS: dict[FrameKind, tuple[Metric, ...]] = {
    FrameKind.WEIGHT: (Metric.BMI,),
    FrameKind.BODY_COMPOSITION: (
        Metric.BODY_FAT,
        Metric.BMR,
        Metric.MUSCLE,
        Metric.SOFT_LEAN_MASS,
        # From the water mass, unless another frame has the percentage.
        Metric.BODY_WATER,
        Metric.IMPEDANCE,
    ),
    FrameKind.CUSTOM1: (Metric.IDEAL_WEIGHT, Metric.AMR),
    FrameKind.CUSTOM2: (
        Metric.VISCERAL_FAT,
        Metric.METABOLIC_AGE,
        Metric.LEAN_BODY_MASS,
        Metric.PROTEIN_MASS,
        Metric.TOTAL_MUSCLE_MASS,
        Metric.SUBCUTANEOUS_FAT,
        Metric.BONE_MASS,
        Metric.BODY_WATER,
    ),
    FrameKind.HEART_RATE: (
        Metric.HEART_RATE,
        Metric.VISCERAL_FAT,
        Metric.METABOLIC_AGE,
    ),
    FrameKind.SEGMENT_BODY: (
        Metric.UPPER_BODY_FAT,
        Metric.LOWER_BODY_FAT,
        Metric.UPPER_BODY_MUSCLE,
        Metric.LOWER_BODY_MUSCLE,
    ),
    FrameKind.SEGMENT_FAT: (
        Metric.VISCERAL_FAT,
        Metric.LEFT_ARM_FAT,
        Metric.RIGHT_ARM_FAT,
        Metric.LEFT_LEG_FAT,
        Metric.RIGHT_LEG_FAT,
        Metric.TORSO_FAT,
    ),
    FrameKind.SEGMENT_MUSCLE: (
        Metric.LEFT_ARM_MUSCLE,
        Metric.RIGHT_ARM_MUSCLE,
        Metric.LEFT_LEG_MUSCLE,
        Metric.RIGHT_LEG_MUSCLE,
        Metric.TORSO_MUSCLE,
    ),
}

# What the app writes to Database Change Increment 0x2A99 after a profile change.
DATABASE_CHANGE_INCREMENT = bytes([0x01, 0x00, 0x00, 0x00])

ReadingFrames = tuple[tuple[str, FrameKind], ...]

_WEIGHT_FRAMES: ReadingFrames = ((WEIGHT_MEASUREMENT_UUID, FrameKind.WEIGHT),)
_STANDARD_FRAMES: ReadingFrames = (
    *_WEIGHT_FRAMES,
    (BODY_COMPOSITION_UUID, FrameKind.BODY_COMPOSITION),
)


@dataclass(frozen=True, slots=True)
class ScaleModel:
    """One Beurer scale model of the BF600 family and its per-model differences."""

    name: str
    slot_count: int
    # The custom characteristic that lists the scale users (BF450: FFFF/FFF1).
    user_list_uuid: str
    # The custom characteristic holding the activity level (BF450: FFFF/FFF2).
    activity_level_uuid: str
    # Verified on real hardware; other models get an "untested" warning.
    verified: bool = False
    # The service the app filters on when searching for the model (§1).
    advertised_service_uuid: str = WEIGHT_SCALE_SERVICE_UUID
    # The service holding the custom characteristics (§1, marker `bi`).
    custom_service_uuid: str = uuid16(0xFFFF)
    # The characteristics a reading arrives on, in the order the scale sends
    # them, with how to decode each; the first is always the weight (§5).
    reading_frames: ReadingFrames = _STANDARD_FRAMES
    # Notifies 01 when a live reading has finished (§9.4), or None. It is the
    # model's take-measurement characteristic.
    live_reading_done_uuid: str | None = None
    # The body-composition metrics the model measures; each gets an entity.
    metrics: tuple[Metric, ...] = ()
    # The unit of soft lean mass and body water mass in 0x2A9C (§5.2).
    body_mass_resolution_kg: float = 0.005
    # Wi-Fi models (marker `kj`) take writes without response (§3).
    write_with_response: bool = True
    # Profile quirks of marker `ig` (§4): Height 0x2A8E as one byte instead of
    # `[cm, 0]`, and its own Database Change Increment value.
    one_byte_height: bool = False
    database_change_increment: bytes = DATABASE_CHANGE_INCREMENT

    @property
    def reading_uuids(self) -> tuple[str, ...]:
        """The characteristics a reading arrives on, the weight first."""
        return tuple(uuid for uuid, _ in self.reading_frames)


def _model(name: str, *rows: dict[str, Any], **details: Any) -> ScaleModel:
    """A table row; later dicts win. The metric set follows from the frames."""
    fields: dict[str, Any] = {"reading_frames": _STANDARD_FRAMES}
    for row in (*rows, details):
        fields |= row
    measured = {m for _, kind in fields["reading_frames"] for m in FRAME_METRICS[kind]}
    return ScaleModel(
        name=name, metrics=tuple(m for m in Metric if m in measured), **fields
    )


# The three custom characteristic numberings of the family (§1). The slot count
# is known for the BF450 (8) and BF105/BF1000 (10) only (§4); others assume 8.
_GATT_0001: dict[str, Any] = {
    "advertised_service_uuid": uuid16(0xFFFF),
    "user_list_uuid": uuid16(0x0001),
    "activity_level_uuid": uuid16(0x0004),
    "live_reading_done_uuid": uuid16(0x0006),
    "slot_count": 8,
}
# Marker `jj`, the BF450 numbering.
_GATT_FFF1: dict[str, Any] = {
    "advertised_service_uuid": uuid16(0xFFFF),
    "user_list_uuid": uuid16(0xFFF1),
    "activity_level_uuid": uuid16(0xFFF2),
    "live_reading_done_uuid": uuid16(0xFFF4),
    "slot_count": 8,
}
# Custom service 0xFFF0 (no marker `bi`).
_GATT_FFF2: dict[str, Any] = {
    "advertised_service_uuid": uuid16(0xFFF0),
    "custom_service_uuid": uuid16(0xFFF0),
    "user_list_uuid": uuid16(0xFFF2),
    "activity_level_uuid": uuid16(0xFFF3),
    "live_reading_done_uuid": uuid16(0xFFF4),
    "slot_count": 8,
}
_ADVERTISES_181D: dict[str, Any] = {
    "advertised_service_uuid": WEIGHT_SCALE_SERVICE_UUID
}
_TEN_SLOTS: dict[str, Any] = {"slot_count": 10}
_WIFI: dict[str, Any] = {"write_with_response": False}

# Marker `od`: custom1/custom2 frames, body masses in tenths of a kg.
_BF450_TYPE: dict[str, Any] = _ADVERTISES_181D | {
    "reading_frames": (
        *_STANDARD_FRAMES,
        (CUSTOM1_UUID, FrameKind.CUSTOM1),
        (CUSTOM2_UUID, FrameKind.CUSTOM2),
    ),
    "body_mass_resolution_kg": 0.1,
    "verified": True,
}
# Marker `nh`.
_HEART_RATE: dict[str, Any] = {
    "reading_frames": (*_STANDARD_FRAMES, (CUSTOM1_UUID, FrameKind.HEART_RATE))
}
# Marker `nd` (upper/lower) and `md` (full segmental analysis).
_UPPER_LOWER_FRAMES: ReadingFrames = (
    *_STANDARD_FRAMES,
    (SEGMENT_BODY_UUID, FrameKind.SEGMENT_BODY),
)
_SEGMENTAL_FRAMES: ReadingFrames = (
    *_UPPER_LOWER_FRAMES,
    (SEGMENT_FAT_UUID, FrameKind.SEGMENT_FAT),
    (SEGMENT_MUSCLE_UUID, FrameKind.SEGMENT_MUSCLE),
)

BF450 = _model("BF450", _GATT_FFF1, _BF450_TYPE)
MODELS: tuple[ScaleModel, ...] = (
    _model("BF105", _GATT_0001, _TEN_SLOTS, reading_frames=_UPPER_LOWER_FRAMES),
    _model("BF1000", _GATT_0001, _TEN_SLOTS, reading_frames=_SEGMENTAL_FRAMES),
    _model("BF405", _GATT_0001),
    BF450,
    _model("BF451", _GATT_FFF1, _BF450_TYPE),
    _model(
        "BF500",
        _GATT_FFF1,
        _HEART_RATE,
        one_byte_height=True,
        database_change_increment=b"\xff\xff\xff\xff",
    ),
    _model("BF600", _GATT_FFF2, _ADVERTISES_181D),
    _model("BF720", _GATT_0001),
    _model("BF722", _GATT_0001),
    _model("BF850", _GATT_FFF2),
    _model("BF880", _GATT_FFF1, _HEART_RATE, _WIFI),
    _model("BF915", _GATT_0001),
    _model("BF950", _GATT_0001),
    _model("BF980", _GATT_FFF1, _HEART_RATE, _WIFI),
    # Its 20-byte FFF5/FFF7/FFF8 frames aren't decoded yet (§5.4).
    _model("BF990", _GATT_FFF1, _WIFI),
    # Weight only (marker `ji`).
    _model(
        "GS435",
        _GATT_0001,
        _ADVERTISES_181D,
        activity_level_uuid=uuid16(0xFFF3),
        reading_frames=_WEIGHT_FRAMES,
    ),
    _model("SRBF1", _GATT_FFF2),
)


def model_by_name(name: str) -> ScaleModel:
    """Return the model stored in a config entry."""
    return next(m for m in MODELS if m.name == name)


def model_from_advertisement(
    local_name: str | None, service_data_uuids: Iterable[str]
) -> ScaleModel | None:
    """Return the supported model that sent this advertisement, if any.

    The local name decides when present; the longest model name it starts with
    wins. Without one (passive scanning may not see it), the byte-swapped Weight
    Scale UUID identifies a BF450: no other known scale advertises it that way.
    """
    if local_name:
        matches = [m for m in MODELS if local_name.startswith(m.name)]
        return max(matches, key=lambda m: len(m.name), default=None)
    if SWAPPED_WEIGHT_SCALE_UUID in service_data_uuids:
        return BF450
    return None

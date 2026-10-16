"""Supported scale models and how to recognise them in an advertisement."""

from collections.abc import Iterable
from dataclasses import dataclass

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
# The BF450/BF451 custom measurement frames (§5.3).
CUSTOM1_UUID = uuid16(0xFFF5)
CUSTOM2_UUID = uuid16(0xFFF6)


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
    # The characteristics a reading arrives on, in the order the scale sends them;
    # the first is always the weight (§5).
    reading_uuids: tuple[str, ...] = (WEIGHT_MEASUREMENT_UUID, BODY_COMPOSITION_UUID)
    # Notifies 01 when a live reading has finished (§9.4), or None.
    live_reading_done_uuid: str | None = None
    # The body-composition metrics the model measures; each gets an entity.
    metrics: tuple[Metric, ...] = ()
    # The unit of soft lean mass and body water mass in 0x2A9C (§5.2).
    body_mass_resolution_kg: float = 0.005


BF450 = ScaleModel(
    name="BF450",
    slot_count=8,
    user_list_uuid=uuid16(0xFFF1),
    activity_level_uuid=uuid16(0xFFF2),
    verified=True,
    reading_uuids=(
        WEIGHT_MEASUREMENT_UUID,
        BODY_COMPOSITION_UUID,
        CUSTOM1_UUID,
        CUSTOM2_UUID,
    ),
    live_reading_done_uuid=uuid16(0xFFF4),
    metrics=tuple(Metric),
    body_mass_resolution_kg=0.1,
)
MODELS: tuple[ScaleModel, ...] = (BF450,)


def model_by_name(name: str) -> ScaleModel:
    """Return the model stored in a config entry."""
    return next(m for m in MODELS if m.name == name)


def model_from_advertisement(
    local_name: str | None, service_data_uuids: Iterable[str]
) -> ScaleModel | None:
    """Return the supported model that sent this advertisement, if any.

    The local name decides when present. Without one (passive scanning may not
    see it), the byte-swapped Weight Scale UUID identifies a BF450: no other
    known scale advertises it that way.
    """
    if local_name:
        return next((m for m in MODELS if local_name.startswith(m.name)), None)
    if SWAPPED_WEIGHT_SCALE_UUID in service_data_uuids:
        return BF450
    return None

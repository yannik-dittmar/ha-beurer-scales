"""Supported scale models and how to recognise them in an advertisement."""

from collections.abc import Iterable
from dataclasses import dataclass

# The BF450 advertises the Weight Scale service (0x181D) byte-swapped, as 0x1D18;
# its service data holds the pending-slot list (protocol doc §2).
SWAPPED_WEIGHT_SCALE_UUID = "00001d18-0000-1000-8000-00805f9b34fb"


def uuid16(short: int) -> str:
    """Expand a 16-bit UUID with the Bluetooth SIG base."""
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


# Characteristics every BF600-family model shares (protocol doc §1).
CURRENT_TIME_UUID = uuid16(0x2A2B)
USER_CONTROL_POINT_UUID = uuid16(0x2A9F)


@dataclass(frozen=True, slots=True)
class ScaleModel:
    """One Beurer scale model of the BF600 family and its per-model differences."""

    name: str
    slot_count: int
    # The custom characteristic that lists the scale users (BF450: FFFF/FFF1).
    user_list_uuid: str
    # Verified on real hardware; other models get an "untested" warning.
    verified: bool = False


BF450 = ScaleModel(
    name="BF450", slot_count=8, user_list_uuid=uuid16(0xFFF1), verified=True
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

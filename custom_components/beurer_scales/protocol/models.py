"""Supported scale models and how to recognise them in an advertisement."""

from collections.abc import Iterable
from dataclasses import dataclass

# The BF450 advertises the Weight Scale service (0x181D) byte-swapped, as 0x1D18;
# its service data holds the pending-slot list (protocol doc §2).
SWAPPED_WEIGHT_SCALE_UUID = "00001d18-0000-1000-8000-00805f9b34fb"


@dataclass(frozen=True, slots=True)
class ScaleModel:
    """One Beurer scale model."""

    name: str


BF450 = ScaleModel(name="BF450")
MODELS: tuple[ScaleModel, ...] = (BF450,)


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

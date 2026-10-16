"""Assembling readings from the frames a scale sends (protocol doc §5, §9.4b).

The scale sends each reading as a group of frames, one per measurement
characteristic, with no separator or end marker between groups. A weight frame
(2A9D) always starts a new reading.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from .frames import FrameError, WeightFrame, WeightUnit, decode_weight_frame
from .models import WEIGHT_MEASUREMENT_UUID

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Reading:
    """One weigh-in as the scale recorded it for a slot."""

    slot: int
    # The scale's local time; it has no time zone.
    timestamp: datetime | None
    weight_kg: float
    # The reading's other frames by characteristic UUID (body composition,
    # custom frames), decoded by whoever needs them.
    frames: Mapping[str, bytes] = field(default_factory=dict)
    # The unit the scale reported `weight_kg` in; only kg may be used as kg.
    unit: WeightUnit = WeightUnit.KG


@dataclass(slots=True)
class _Group:
    weight: WeightFrame
    frames: dict[str, bytes] = field(default_factory=dict)


class ReadingAssembler:
    """Collects the frames of a sync and groups them into readings."""

    def __init__(self) -> None:
        """Start with no readings."""
        self._groups: list[_Group] = []
        self._current: _Group | None = None

    def add(self, uuid: str, frame: bytes) -> None:
        """Add one notification of a measurement characteristic."""
        if uuid == WEIGHT_MEASUREMENT_UUID:
            try:
                self._current = _Group(decode_weight_frame(frame))
            except FrameError as err:
                _LOGGER.warning("Dropping a reading: %s", err)
                self._current = None
                return
            self._groups.append(self._current)
        elif self._current is None:
            _LOGGER.debug("Dropping %s frame outside a reading: %s", uuid, frame.hex())
        else:
            self._current.frames[uuid] = frame

    def readings(self) -> list[Reading]:
        """The readings so far, in the order the scale sent them."""
        return [
            Reading(
                slot=group.weight.slot,
                timestamp=group.weight.timestamp,
                weight_kg=group.weight.weight_kg,
                frames=dict(group.frames),
                unit=group.weight.unit,
            )
            for group in self._groups
        ]

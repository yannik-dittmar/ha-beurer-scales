"""Assembling readings from the frames a scale sends (protocol doc §5, §9.4b).

The scale sends each reading as a group of frames, one per measurement
characteristic, with no separator or end marker between groups. A weight frame
(2A9D) always starts a new reading.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from .frames import FrameError, WeightFrame, decode_weight_frame
from .metrics import (
    Metric,
    decode_body_composition_frame,
    decode_custom1_frame,
    decode_custom2_frame,
    decode_heart_rate_frame,
    decode_segment_body_frame,
    decode_segment_fat_frame,
    decode_segment_muscle_frame,
)
from .models import WEIGHT_MEASUREMENT_UUID, FrameKind, ScaleModel

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
    # From the weight frame; None when the scale didn't compute it.
    bmi: float | None = None


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
                bmi=group.weight.bmi or None,
            )
            for group in self._groups
        ]


def reading_metrics(reading: Reading, model: ScaleModel) -> dict[Metric, float]:
    """The body-composition metrics of a reading that its model measures.

    Each frame is decoded as the model's table says. A metric is left out when
    the scale didn't measure it or its frame is missing or malformed. When two
    frames carry a metric (body water), the later frame's value wins.
    """
    values: dict[Metric, float] = {}
    if reading.bmi is not None:
        values[Metric.BMI] = reading.bmi
    for uuid, kind in model.reading_frames:
        if (frame := reading.frames.get(uuid)) is None:
            continue
        try:
            decoded = _decode_frame(kind, frame, reading, model)
        except FrameError as err:
            _LOGGER.warning("Ignoring a %s frame: %s", kind, err)
            continue
        values |= {metric: v for metric, v in decoded.items() if v is not None}
    return {
        metric: value
        for metric in model.metrics
        if (value := values.get(metric)) is not None
    }


def _decode_frame(
    kind: FrameKind, frame: bytes, reading: Reading, model: ScaleModel
) -> Mapping[Metric, float | None]:
    match kind:
        case FrameKind.BODY_COMPOSITION:
            body = decode_body_composition_frame(
                frame, mass_resolution_kg=model.body_mass_resolution_kg
            )
            water_mass_kg = body.body_water_mass_kg
            return {
                Metric.BODY_FAT: body.body_fat_percent,
                Metric.BMR: body.bmr_kcal,
                Metric.MUSCLE: body.muscle_percent,
                Metric.SOFT_LEAN_MASS: body.soft_lean_mass_kg,
                Metric.IMPEDANCE: body.impedance_ohm,
                # The app's fallback when custom2 has no water % (§5.3).
                Metric.BODY_WATER: (
                    round(water_mass_kg / reading.weight_kg * 100, 1)
                    if water_mass_kg and reading.weight_kg
                    else None
                ),
            }
        case FrameKind.CUSTOM1:
            custom1 = decode_custom1_frame(frame)
            return {
                Metric.IDEAL_WEIGHT: custom1.ideal_weight_kg,
                Metric.AMR: custom1.amr_kcal,
            }
        case FrameKind.CUSTOM2:
            custom2 = decode_custom2_frame(frame)
            return {
                Metric.VISCERAL_FAT: custom2.visceral_fat,
                Metric.METABOLIC_AGE: custom2.metabolic_age_years,
                Metric.LEAN_BODY_MASS: custom2.lean_body_mass_kg,
                Metric.PROTEIN_MASS: custom2.protein_mass_kg,
                Metric.TOTAL_MUSCLE_MASS: custom2.total_muscle_mass_kg,
                Metric.SUBCUTANEOUS_FAT: custom2.subcutaneous_fat_percent,
                Metric.BONE_MASS: custom2.bone_mass_kg,
                Metric.BODY_WATER: custom2.body_water_percent,
            }
        case FrameKind.HEART_RATE:
            return decode_heart_rate_frame(frame)
        case FrameKind.SEGMENT_BODY:
            return decode_segment_body_frame(frame)
        case FrameKind.SEGMENT_FAT:
            return decode_segment_fat_frame(frame)
        case FrameKind.SEGMENT_MUSCLE:
            return decode_segment_muscle_frame(frame)
        case FrameKind.WEIGHT:
            # Not in `reading.frames`: the weight frame makes the reading itself.
            return {}

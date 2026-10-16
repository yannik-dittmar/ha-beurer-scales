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
from .metrics import (
    Metric,
    decode_body_composition_frame,
    decode_custom1_frame,
    decode_custom2_frame,
)
from .models import (
    BODY_COMPOSITION_UUID,
    CUSTOM1_UUID,
    CUSTOM2_UUID,
    WEIGHT_MEASUREMENT_UUID,
    ScaleModel,
)

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
                unit=group.weight.unit,
                bmi=group.weight.bmi or None,
            )
            for group in self._groups
        ]


def reading_metrics(reading: Reading, model: ScaleModel) -> dict[Metric, float]:
    """The body-composition metrics of a reading that its model measures.

    A metric is left out when the scale didn't measure it or its frame is
    missing or malformed.
    """
    values: dict[Metric, float | None] = {Metric.BMI: reading.bmi}
    water_mass_kg: float | None = None
    try:
        if (frame := reading.frames.get(BODY_COMPOSITION_UUID)) is not None:
            body = decode_body_composition_frame(
                frame, mass_resolution_kg=model.body_mass_resolution_kg
            )
            water_mass_kg = body.body_water_mass_kg
            values |= {
                Metric.BODY_FAT: body.body_fat_percent,
                Metric.BMR: body.bmr_kcal,
                Metric.MUSCLE: body.muscle_percent,
                Metric.SOFT_LEAN_MASS: body.soft_lean_mass_kg,
                Metric.IMPEDANCE: body.impedance_ohm,
            }
    except FrameError as err:
        _LOGGER.warning("Ignoring a body composition frame: %s", err)
    try:
        if (frame := reading.frames.get(CUSTOM1_UUID)) is not None:
            custom1 = decode_custom1_frame(frame)
            values |= {
                Metric.IDEAL_WEIGHT: custom1.ideal_weight_kg,
                Metric.AMR: custom1.amr_kcal,
            }
    except FrameError as err:
        _LOGGER.warning("Ignoring a custom1 frame: %s", err)
    try:
        if (frame := reading.frames.get(CUSTOM2_UUID)) is not None:
            custom2 = decode_custom2_frame(frame)
            values |= {
                Metric.VISCERAL_FAT: custom2.visceral_fat,
                Metric.METABOLIC_AGE: custom2.metabolic_age_years,
                Metric.LEAN_BODY_MASS: custom2.lean_body_mass_kg,
                Metric.PROTEIN_MASS: custom2.protein_mass_kg,
                Metric.TOTAL_MUSCLE_MASS: custom2.total_muscle_mass_kg,
                Metric.SUBCUTANEOUS_FAT: custom2.subcutaneous_fat_percent,
                Metric.BONE_MASS: custom2.bone_mass_kg,
                Metric.BODY_WATER: custom2.body_water_percent,
            }
    except FrameError as err:
        _LOGGER.warning("Ignoring a custom2 frame: %s", err)
    if values.get(Metric.BODY_WATER) is None and water_mass_kg and reading.weight_kg:
        # The app's fallback when custom2 has no water % (§5.3).
        values[Metric.BODY_WATER] = round(water_mass_kg / reading.weight_kg * 100, 1)
    return {
        metric: value
        for metric in model.metrics
        if (value := values.get(metric)) is not None
    }

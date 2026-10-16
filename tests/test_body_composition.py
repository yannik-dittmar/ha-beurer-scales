"""A reading updates the scale user's body-composition sensors."""

from dataclasses import replace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beurer_scales.const import DOMAIN
from custom_components.beurer_scales.protocol import models
from custom_components.beurer_scales.protocol.metrics import Metric

from . import frames
from .conftest import SCALE_ADDRESS, wake_scale
from .fake_scale import FakeReading, FakeScale
from .test_sync import anna_scale, local_now, state

# (metric, state, unit, device class) for the §9.3 reading of Anna (slot 2).
ENABLED = [
    ("bmi", "24.2", None, None),
    ("body_fat", "18.4", "%", None),
    ("body_water", "58.5", "%", None),
    ("muscle", "41.2", "%", None),
    ("bone_mass", "3.35", "kg", "weight"),
    ("bmr", "1758", "kcal", None),
    ("amr", "2637", "kcal", None),
    ("visceral_fat", "7", None, None),
    ("metabolic_age", "34", "y", None),
    ("lean_body_mass", "63.97", "kg", "weight"),
    ("protein_mass", "14.4", "kg", "weight"),
    ("total_muscle_mass", "43.1", "kg", "weight"),
    ("soft_lean_mass", "60.6", "kg", "weight"),
    ("subcutaneous_fat", "16.1", "%", None),
]
DISABLED = [
    ("ideal_weight", "70.47", "kg", "weight"),
    ("impedance", "512.0", "Ω", None),
]


def unique_id(metric: str) -> str:
    """The unique id of one of Anna's metric sensors."""
    return f"{SCALE_ADDRESS}_slot_2_{metric}"


async def set_up(hass: HomeAssistant, fake_scale: FakeScale) -> MockConfigEntry:
    """A loaded scale with Anna adopted in slot 2."""
    fake_scale.add_user(slot=2, consent_code=4321)
    entry = anna_scale()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def weigh_in_anna(hass: HomeAssistant, fake_scale: FakeScale) -> None:
    """Anna steps on the scale and HA syncs the reading."""
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )
    await wake_scale(hass, fake_scale)


@pytest.mark.parametrize(("metric", "value", "unit", "device_class"), ENABLED)
async def test_a_reading_updates_the_body_composition_sensors(
    hass: HomeAssistant,
    fake_scale: FakeScale,
    metric: str,
    value: str,
    unit: str | None,
    device_class: str | None,
) -> None:
    """Every enabled metric shows the reading's value in its unit."""
    await set_up(hass, fake_scale)

    await weigh_in_anna(hass, fake_scale)

    sensor = state(hass, unique_id(metric))
    assert sensor.state == value
    assert sensor.attributes.get("unit_of_measurement") == unit
    assert sensor.attributes.get("device_class") == device_class
    assert sensor.attributes["state_class"] == "measurement"


async def test_ideal_weight_and_impedance_are_disabled_by_default(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """The niche metrics exist but start disabled."""
    await set_up(hass, fake_scale)

    registry = er.async_get(hass)
    for metric, *_ in DISABLED:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id(metric))
        assert entity_id is not None
        registry_entry = registry.async_get(entity_id)
        assert registry_entry is not None
        assert registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_enabled_ideal_weight_and_impedance_show_the_reading(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """Once enabled, ideal weight and impedance follow the readings too."""
    entry = await set_up(hass, fake_scale)
    registry = er.async_get(hass)
    for metric, *_ in DISABLED:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id(metric))
        assert entity_id is not None
        registry.async_update_entity(entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    await weigh_in_anna(hass, fake_scale)

    for metric, value, unit, device_class in DISABLED:
        sensor = state(hass, unique_id(metric))
        assert sensor.state == value
        assert sensor.attributes.get("unit_of_measurement") == unit
        assert sensor.attributes.get("device_class") == device_class
        assert sensor.attributes["state_class"] == "measurement"


async def test_a_model_without_a_metric_gets_no_entity(
    hass: HomeAssistant, fake_scale: FakeScale, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model table decides which metric entities exist."""
    monkeypatch.setattr(
        models, "MODELS", (replace(models.BF450, metrics=(Metric.BODY_FAT,)),)
    )

    await set_up(hass, fake_scale)

    registry = er.async_get(hass)
    present = {
        metric
        for metric, *_ in ENABLED + DISABLED
        if registry.async_get_entity_id("sensor", DOMAIN, unique_id(metric))
    }
    assert present == {"body_fat"}

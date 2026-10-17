"""Each model gets entities for its own metric set (protocol doc §1, §5.4)."""

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beurer_scales.const import CONF_MODEL, DOMAIN

from . import frames, test_segment_frames
from .conftest import SCALE_ADDRESS, wake_scale
from .fake_scale import FakeReading, FakeScale
from .test_sync import ANNA, local_now, state

# Every per-user sensor key the integration knows, beyond weight/last reading.
ALL_METRICS = [
    "bmi",
    "body_fat",
    "body_water",
    "muscle",
    "bone_mass",
    "bmr",
    "amr",
    "visceral_fat",
    "metabolic_age",
    "lean_body_mass",
    "protein_mass",
    "total_muscle_mass",
    "soft_lean_mass",
    "subcutaneous_fat",
    "ideal_weight",
    "impedance",
    "heart_rate",
    "upper_body_fat",
    "lower_body_fat",
    "upper_body_muscle",
    "lower_body_muscle",
    "left_arm_fat",
    "right_arm_fat",
    "left_leg_fat",
    "right_leg_fat",
    "torso_fat",
    "left_arm_muscle",
    "right_arm_muscle",
    "left_leg_muscle",
    "right_leg_muscle",
    "torso_muscle",
]
STANDARD = {
    "bmi",
    "body_fat",
    "body_water",
    "muscle",
    "bmr",
    "soft_lean_mass",
    "impedance",
}


def unique_id(metric: str) -> str:
    """The unique id of one of Anna's metric sensors."""
    return f"{SCALE_ADDRESS}_slot_2_{metric}"


async def set_up(hass: HomeAssistant, model: str) -> MockConfigEntry:
    """A loaded scale of `model` with Anna in slot 2."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=model,
        unique_id=SCALE_ADDRESS,
        data={CONF_ADDRESS: SCALE_ADDRESS, CONF_MODEL: model},
        subentries_data=[
            {
                "data": ANNA,
                "subentry_type": "scale_user",
                "title": "Anna",
                "unique_id": "slot_2",
            }
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def present_metrics(hass: HomeAssistant) -> set[str]:
    """The metric sensors Anna has."""
    registry = er.async_get(hass)
    return {
        metric
        for metric in ALL_METRICS
        if registry.async_get_entity_id("sensor", DOMAIN, unique_id(metric))
    }


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (
            "BF1000",
            STANDARD
            | {
                "visceral_fat",
                "upper_body_fat",
                "lower_body_fat",
                "upper_body_muscle",
                "lower_body_muscle",
                "left_arm_fat",
                "right_arm_fat",
                "left_leg_fat",
                "right_leg_fat",
                "torso_fat",
                "left_arm_muscle",
                "right_arm_muscle",
                "left_leg_muscle",
                "right_leg_muscle",
                "torso_muscle",
            },
        ),
        ("BF500", STANDARD | {"heart_rate", "visceral_fat", "metabolic_age"}),
        ("BF600", STANDARD),
        ("GS435", {"bmi"}),
    ],
)
async def test_entities_follow_the_models_metric_set(
    hass: HomeAssistant, model: str, expected: set[str]
) -> None:
    """Only the metrics the model measures get an entity."""
    await set_up(hass, model)

    assert present_metrics(hass) == expected


async def test_segment_sensors_are_enabled_percentages(hass: HomeAssistant) -> None:
    """Segment metrics are percentages and enabled by default."""
    await set_up(hass, "BF1000")

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id("torso_fat"))
    assert entity_id is not None
    registry_entry = registry.async_get(entity_id)
    assert registry_entry is not None
    assert registry_entry.disabled_by is None
    assert registry_entry.unit_of_measurement == "%"


async def test_a_heart_rate_reading_reaches_the_sensors(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """A BF500 sends its heart-rate frame on FFF5; HA decodes it as such."""
    fake_scale.local_name = "BF500"
    fake_scale.add_user(slot=2, consent_code=4321)
    await set_up(hass, "BF500")
    capture = (*frames.READING_SLOT_2[:2], (0xFFF5, test_segment_frames.HEART_RATE))

    fake_scale.weigh_in(2, FakeReading.from_capture(capture, at=local_now()))
    await wake_scale(hass, fake_scale)

    pulse = state(hass, unique_id("heart_rate"))
    assert pulse.state == "72"
    assert pulse.attributes["unit_of_measurement"] == "bpm"
    assert pulse.attributes["state_class"] == "measurement"
    assert state(hass, unique_id("visceral_fat")).state == "9.5"
    assert state(hass, unique_id("metabolic_age")).state == "41"
    assert state(hass, unique_id("body_fat")).state == "18.4"

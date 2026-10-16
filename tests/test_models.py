"""The model table: every BF600-family model's GATT map and metric set (protocol doc §1, §7)."""

import pytest

from custom_components.beurer_scales.protocol.metrics import Metric
from custom_components.beurer_scales.protocol.models import (
    MODELS,
    FrameKind,
    ScaleModel,
    model_by_name,
    model_from_advertisement,
    uuid16,
)

# Per model, straight from the §1 table: advertised service, custom service,
# user list, activity level, take measurement, and the extra measurement
# characteristics after 2A9D (+ 2A9C).
GATT_MAP = {
    "BF105": (0xFFFF, 0xFFFF, 0x0001, 0x0004, 0x0006),
    "BF1000": (0xFFFF, 0xFFFF, 0x0001, 0x0004, 0x0006),
    "BF405": (0xFFFF, 0xFFFF, 0x0001, 0x0004, 0x0006),
    "BF450": (0x181D, 0xFFFF, 0xFFF1, 0xFFF2, 0xFFF4),
    "BF451": (0x181D, 0xFFFF, 0xFFF1, 0xFFF2, 0xFFF4),
    "BF500": (0xFFFF, 0xFFFF, 0xFFF1, 0xFFF2, 0xFFF4),
    "BF600": (0x181D, 0xFFF0, 0xFFF2, 0xFFF3, 0xFFF4),
    "BF720": (0xFFFF, 0xFFFF, 0x0001, 0x0004, 0x0006),
    "BF722": (0xFFFF, 0xFFFF, 0x0001, 0x0004, 0x0006),
    "BF850": (0xFFF0, 0xFFF0, 0xFFF2, 0xFFF3, 0xFFF4),
    "BF880": (0xFFFF, 0xFFFF, 0xFFF1, 0xFFF2, 0xFFF4),
    "BF915": (0xFFFF, 0xFFFF, 0x0001, 0x0004, 0x0006),
    "BF950": (0xFFFF, 0xFFFF, 0x0001, 0x0004, 0x0006),
    "BF980": (0xFFFF, 0xFFFF, 0xFFF1, 0xFFF2, 0xFFF4),
    "BF990": (0xFFFF, 0xFFFF, 0xFFF1, 0xFFF2, 0xFFF4),
    "GS435": (0x181D, 0xFFFF, 0x0001, 0xFFF3, 0x0006),
    "SRBF1": (0xFFF0, 0xFFF0, 0xFFF2, 0xFFF3, 0xFFF4),
}

W = (0x2A9D, FrameKind.WEIGHT)
B = (0x2A9C, FrameKind.BODY_COMPOSITION)
STANDARD_FRAMES = (W, B)
READING_FRAMES = {
    "BF105": (W, B, (0x0008, FrameKind.SEGMENT_BODY)),
    "BF1000": (
        W,
        B,
        (0x0008, FrameKind.SEGMENT_BODY),
        (0x0009, FrameKind.SEGMENT_FAT),
        (0x000A, FrameKind.SEGMENT_MUSCLE),
    ),
    "BF450": (W, B, (0xFFF5, FrameKind.CUSTOM1), (0xFFF6, FrameKind.CUSTOM2)),
    "BF451": (W, B, (0xFFF5, FrameKind.CUSTOM1), (0xFFF6, FrameKind.CUSTOM2)),
    "BF500": (W, B, (0xFFF5, FrameKind.HEART_RATE)),
    "BF880": (W, B, (0xFFF5, FrameKind.HEART_RATE)),
    "BF980": (W, B, (0xFFF5, FrameKind.HEART_RATE)),
    # The BF990's 20-byte frames aren't decoded yet (§5.4).
    "GS435": (W,),
}

STANDARD_METRICS = {
    Metric.BMI,
    Metric.BODY_FAT,
    Metric.BODY_WATER,
    Metric.MUSCLE,
    Metric.BMR,
    Metric.SOFT_LEAN_MASS,
    Metric.IMPEDANCE,
}
BF450_METRICS = STANDARD_METRICS | {
    Metric.BONE_MASS,
    Metric.AMR,
    Metric.VISCERAL_FAT,
    Metric.METABOLIC_AGE,
    Metric.LEAN_BODY_MASS,
    Metric.PROTEIN_MASS,
    Metric.TOTAL_MUSCLE_MASS,
    Metric.SUBCUTANEOUS_FAT,
    Metric.IDEAL_WEIGHT,
}
HEART_RATE_METRICS = STANDARD_METRICS | {
    Metric.HEART_RATE,
    Metric.VISCERAL_FAT,
    Metric.METABOLIC_AGE,
}
UPPER_LOWER_METRICS = {
    Metric.UPPER_BODY_FAT,
    Metric.LOWER_BODY_FAT,
    Metric.UPPER_BODY_MUSCLE,
    Metric.LOWER_BODY_MUSCLE,
}
METRICS = {
    "BF105": STANDARD_METRICS | UPPER_LOWER_METRICS,
    "BF1000": STANDARD_METRICS
    | UPPER_LOWER_METRICS
    | {
        Metric.VISCERAL_FAT,
        Metric.LEFT_ARM_FAT,
        Metric.RIGHT_ARM_FAT,
        Metric.LEFT_LEG_FAT,
        Metric.RIGHT_LEG_FAT,
        Metric.TORSO_FAT,
        Metric.LEFT_ARM_MUSCLE,
        Metric.RIGHT_ARM_MUSCLE,
        Metric.LEFT_LEG_MUSCLE,
        Metric.RIGHT_LEG_MUSCLE,
        Metric.TORSO_MUSCLE,
    },
    "BF450": BF450_METRICS,
    "BF451": BF450_METRICS,
    "BF500": HEART_RATE_METRICS,
    "BF880": HEART_RATE_METRICS,
    "BF980": HEART_RATE_METRICS,
    # A weight-only scale: only the BMI of the weight frame.
    "GS435": {Metric.BMI},
}

WIFI_MODELS = {"BF880", "BF980", "BF990"}


def test_the_table_holds_the_whole_bf600_family() -> None:
    """All 17 BF600-family models, and only BF450/BF451 are verified."""
    assert {m.name for m in MODELS} == set(GATT_MAP)
    assert {m.name for m in MODELS if m.verified} == {"BF450", "BF451"}


@pytest.mark.parametrize("name", GATT_MAP)
def test_gatt_map(name: str) -> None:
    """Each model talks to the characteristics the app uses for it."""
    advertised, custom, user_list, activity, take = GATT_MAP[name]
    model = model_by_name(name)

    assert model.advertised_service_uuid == uuid16(advertised)
    assert model.custom_service_uuid == uuid16(custom)
    assert model.user_list_uuid == uuid16(user_list)
    assert model.activity_level_uuid == uuid16(activity)
    assert model.live_reading_done_uuid == uuid16(take)
    assert model.write_with_response is (name not in WIFI_MODELS)


@pytest.mark.parametrize("name", GATT_MAP)
def test_reading_frames(name: str) -> None:
    """A reading arrives on the weight frame first, then the model's other frames."""
    model = model_by_name(name)
    expected = READING_FRAMES.get(name, STANDARD_FRAMES)

    assert model.reading_frames == tuple(
        (uuid16(short), kind) for short, kind in expected
    )
    assert model.reading_uuids == tuple(uuid16(short) for short, _ in expected)


@pytest.mark.parametrize("name", GATT_MAP)
def test_metric_set(name: str) -> None:
    """Each model gets entities for exactly the metrics its frames carry."""
    assert set(model_by_name(name).metrics) == METRICS.get(name, STANDARD_METRICS)


@pytest.mark.parametrize(
    ("name", "slots"), [("BF450", 8), ("BF451", 8), ("BF105", 10), ("BF1000", 10)]
)
def test_slot_count(name: str, slots: int) -> None:
    """The scale holds as many scale users as the app allows (§4)."""
    assert model_by_name(name).slot_count == slots


def test_only_the_bf450_type_stores_body_masses_in_tenths() -> None:
    """Soft lean and water mass are × 0.1 kg on the BF450/BF451, else × 0.005 (§5.2)."""
    tenths = {m.name for m in MODELS if m.body_mass_resolution_kg == 0.1}
    assert tenths == {"BF450", "BF451"}
    assert all(
        m.body_mass_resolution_kg == 0.005 for m in MODELS if m.name not in tenths
    )


def test_only_the_bf500_writes_the_short_profile() -> None:
    """The BF500 takes a 1-byte height and an all-ff change increment (§4)."""
    assert {m.name for m in MODELS if m.one_byte_height} == {"BF500"}
    assert {
        m.name
        for m in MODELS
        if m.database_change_increment != bytes([0x01, 0x00, 0x00, 0x00])
    } == {"BF500"}
    assert model_by_name("BF500").database_change_increment == b"\xff\xff\xff\xff"


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.name)
def test_each_model_is_recognised_by_its_local_name(model: ScaleModel) -> None:
    """The advertised name picks the model, also with a suffix."""
    assert model_from_advertisement(model.name, []) is model
    assert model_from_advertisement(f"{model.name}-1234", []) is model

"""Sending fresh readings to Garmin Connect (ADR 0011)."""

import json
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.beurer_scales.const import (
    CONF_MODEL,
    CONF_SCALE_USER,
    DOMAIN,
    SUBENTRY_GARMIN,
    SUBENTRY_SCALE_USER,
)
from custom_components.beurer_scales.garmin import (
    GarminAuthError,
    GarminRateLimited,
    GarminReading,
    PushResult,
    body_composition_args,
)
from custom_components.beurer_scales.garmin_push import (
    RETRY_DELAYS,
    START_DELAY,
    garmin_reading,
)
from custom_components.beurer_scales.protocol.metrics import Metric
from custom_components.beurer_scales.protocol.models import model_by_name
from custom_components.beurer_scales.protocol.readings import Reading

from . import frames
from .conftest import SCALE_ADDRESS, wake_scale
from .fake_scale import FakeReading, FakeScale
from .test_sync import ANNA, local_now

PUSH = "custom_components.beurer_scales.garmin_push.push"
ANNA_ID = "anna-subentry"
STORE_KEY = "beurer_scales.garmin-scale.garmin"


def scale_with_link() -> MockConfigEntry:
    """A BF450 whose Anna sends her readings to Garmin."""
    return MockConfigEntry(
        entry_id="garmin-scale",
        domain=DOMAIN,
        title="BF450",
        unique_id=SCALE_ADDRESS,
        data={CONF_ADDRESS: SCALE_ADDRESS, CONF_MODEL: "BF450"},
        subentries_data=[
            {
                "subentry_id": ANNA_ID,
                "data": ANNA,
                "subentry_type": SUBENTRY_SCALE_USER,
                "title": "Anna",
                "unique_id": "slot_2",
            },
            {
                "subentry_id": "garmin-link",
                "data": {CONF_SCALE_USER: ANNA_ID},
                "subentry_type": SUBENTRY_GARMIN,
                "title": "Anna",
                "unique_id": ANNA_ID,
            },
        ],
    )


def stored_link(**overrides: Any) -> dict[str, Any]:
    """A link as the store keeps it."""
    return {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {
            ANNA_ID: {
                "tokens": "tokens-1",
                "paused": False,
                "last_sent": None,
                "queue": [],
                **overrides,
            }
        },
    }


def pushed(sent: int, tokens: str = "tokens-2") -> PushResult:
    """An upload run that sent `sent` readings."""
    return PushResult(sent=sent, tokens=tokens)


async def set_up(
    hass: HomeAssistant, hass_storage: dict[str, Any], **link: Any
) -> MockConfigEntry:
    """Load the scale with Anna's Garmin link."""
    hass_storage[STORE_KEY] = stored_link(**link)
    entry = scale_with_link()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def weigh_in(fake_scale: FakeScale) -> None:
    fake_scale.add_user(slot=2, consent_code=4321)
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )


def link_data(hass_storage: dict[str, Any]) -> dict[str, Any]:
    return hass_storage[STORE_KEY]["data"][ANNA_ID]


def test_the_upload_carries_the_values_garmin_has_a_field_for() -> None:
    """Weight and body composition map to the FIT weight scale fields."""
    taken = dt_util.now()
    args = body_composition_args(
        GarminReading(
            timestamp=taken,
            weight_kg=78.4,
            metrics={
                Metric.BODY_FAT: 20.5,
                Metric.BODY_WATER: 55.0,
                Metric.BONE_MASS: 3.1,
                Metric.TOTAL_MUSCLE_MASS: 58.2,
                Metric.BMR: 1800,
                Metric.AMR: 2700,
                Metric.METABOLIC_AGE: 35,
                Metric.VISCERAL_FAT: 7,
                Metric.BMI: 24.2,
                Metric.PROTEIN_MASS: 12.0,
                Metric.IMPEDANCE: 500,
            },
        )
    )

    assert args == {
        "timestamp": taken.isoformat(),
        "weight": 78.4,
        "percent_fat": 20.5,
        "percent_hydration": 55.0,
        "bone_mass": 3.1,
        "muscle_mass": 58.2,
        "basal_met": 1800,
        "active_met": 2700,
        "metabolic_age": 35,
        "visceral_fat_rating": 7,
        "bmi": 24.2,
    }


def test_muscle_mass_falls_back_to_the_percentage() -> None:
    """A model without the muscle mass in kg gets it from the percentage."""
    args = body_composition_args(
        GarminReading(
            timestamp=dt_util.now(), weight_kg=80.0, metrics={Metric.MUSCLE: 40.0}
        )
    )

    assert args["muscle_mass"] == 32.0


def test_the_scales_clock_is_taken_as_home_assistants_local_time() -> None:
    """The reading's naive time gets HA's zone; a reading without one is now."""
    taken = local_now()
    reading = Reading(slot=2, timestamp=taken, weight_kg=78.4)

    garmin = garmin_reading(reading, model_by_name("BF450"))

    assert garmin.timestamp == taken.replace(tzinfo=dt_util.get_default_time_zone())
    assert garmin.timestamp.utcoffset() is not None


async def test_a_fresh_reading_is_sent_and_the_new_tokens_are_kept(
    hass: HomeAssistant, hass_storage: dict[str, Any], fake_scale: FakeScale
) -> None:
    """Anna weighs in: her reading goes up with her tokens; the newest are stored."""
    weigh_in(fake_scale)
    with patch(PUSH, return_value=pushed(1)) as push:
        await set_up(hass, hass_storage)
        await wake_scale(hass, fake_scale)
        await hass.async_block_till_done()

    push.assert_called_once()
    tokens, [sent] = push.call_args.args
    assert tokens == "tokens-1"
    assert sent.weight_kg == 78.4
    assert sent.timestamp.tzinfo is not None
    stored = link_data(hass_storage)
    assert stored["tokens"] == "tokens-2"
    assert stored["queue"] == []
    assert stored["last_sent"] == sent.timestamp.isoformat()


async def test_a_scale_user_without_a_link_sends_nothing(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """Garmin is opt-in per scale user."""
    weigh_in(fake_scale)
    entry = scale_with_link()
    entry.add_to_hass(hass)
    # No stored link: the subentry alone does nothing.
    with patch(PUSH) as push:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await wake_scale(hass, fake_scale)
        await hass.async_block_till_done()

    push.assert_not_called()


async def test_a_rejected_login_pauses_pushing_and_raises_an_issue(
    hass: HomeAssistant, hass_storage: dict[str, Any], fake_scale: FakeScale
) -> None:
    """Garmin refuses the tokens: the reading is kept, a repair issue appears."""
    weigh_in(fake_scale)
    failed = PushResult(error=GarminAuthError("revoked"), tokens="tokens-1")
    with patch(PUSH, return_value=failed) as push:
        entry = await set_up(hass, hass_storage)
        await wake_scale(hass, fake_scale)
        await hass.async_block_till_done()

        push.assert_called_once()
        stored = link_data(hass_storage)
        assert stored["paused"] is True
        assert len(stored["queue"]) == 1
        issue = ir.async_get(hass).async_get_issue(DOMAIN, f"garmin_auth_{ANNA_ID}")
        assert issue is not None
        assert issue.translation_placeholders == {"name": "Anna"}

        # A new login resumes the link and sends what waited.
        push.return_value = pushed(1)
        await entry.runtime_data.garmin.async_set_tokens(ANNA_ID, "fresh")
        await hass.async_block_till_done()

    assert push.call_args.args[0] == "fresh"
    assert link_data(hass_storage)["queue"] == []
    assert link_data(hass_storage)["paused"] is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"garmin_auth_{ANNA_ID}") is None


async def test_a_paused_link_keeps_queueing_without_calling_garmin(
    hass: HomeAssistant, hass_storage: dict[str, Any], fake_scale: FakeScale
) -> None:
    """Nothing is sent, and nothing logs in, while the link is paused."""
    weigh_in(fake_scale)
    with patch(PUSH) as push:
        await set_up(hass, hass_storage, paused=True)
        await wake_scale(hass, fake_scale)
        await hass.async_block_till_done()

    push.assert_not_called()
    assert len(link_data(hass_storage)["queue"]) == 1


async def test_a_throttled_login_is_retried_later(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    fake_scale: FakeScale,
) -> None:
    """Garmin says 429: the reading waits, the tokens stay, a retry follows."""
    weigh_in(fake_scale)
    throttled = PushResult(error=GarminRateLimited("429"), tokens="tokens-1")
    with patch(PUSH, return_value=throttled) as push:
        await set_up(hass, hass_storage)
        await wake_scale(hass, fake_scale)
        await hass.async_block_till_done()
        assert push.call_count == 1
        assert link_data(hass_storage)["paused"] is False
        assert len(link_data(hass_storage)["queue"]) == 1
        assert (
            ir.async_get(hass).async_get_issue(DOMAIN, f"garmin_auth_{ANNA_ID}") is None
        )

        push.return_value = pushed(1)
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=RETRY_DELAYS[0] + 1)
        )
        await hass.async_block_till_done()

    assert push.call_count == 2
    assert link_data(hass_storage)["queue"] == []


async def test_readings_left_by_a_restart_are_sent_after_a_pause(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """The queue survives a restart; setup doesn't wait for Garmin."""
    taken = dt_util.now().isoformat()
    queued = [{"timestamp": taken, "weight_kg": 70.0, "metrics": {"bmi": 22.0}}]
    with patch(PUSH, return_value=pushed(1)) as push:
        await set_up(hass, hass_storage, queue=queued)
        push.assert_not_called()

        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=START_DELAY + 1)
        )
        await hass.async_block_till_done()

    [(tokens, [sent])] = [call.args for call in push.call_args_list]
    assert (tokens, sent.weight_kg, sent.metrics) == (
        "tokens-1",
        70.0,
        {Metric.BMI: 22.0},
    )
    assert link_data(hass_storage)["queue"] == []


async def test_a_reading_older_than_the_last_sent_one_is_not_sent_again(
    hass: HomeAssistant, hass_storage: dict[str, Any], fake_scale: FakeScale
) -> None:
    """A reading that went up but wasn't recorded as sent (a crash) isn't doubled."""
    weigh_in(fake_scale)
    future = (dt_util.now() + timedelta(days=1)).isoformat()
    with patch(PUSH) as push:
        await set_up(hass, hass_storage, last_sent=future)
        await wake_scale(hass, fake_scale)
        await hass.async_block_till_done()

    push.assert_not_called()
    assert link_data(hass_storage)["queue"] == []


async def test_removing_the_scale_user_removes_its_garmin_link(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """The link goes with its scale user, tokens included."""
    entry = await set_up(hass, hass_storage)
    assert entry.runtime_data.garmin.has_link(ANNA_ID)

    hass.config_entries.async_remove_subentry(entry, ANNA_ID)
    await hass.async_block_till_done()

    assert [s.subentry_type for s in entry.subentries.values()] == []
    assert not entry.runtime_data.garmin.has_link(ANNA_ID)
    assert json.dumps(hass_storage.get(STORE_KEY, {})).count("tokens-1") == 0

"""Syncing a fresh reading: a weigh-in reaches the scale user's sensors."""

from datetime import datetime, timedelta

import pytest
from homeassistant.const import CONF_ADDRESS, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.beurer_scales.const import CONF_MODEL, DOMAIN

from . import frames
from .conftest import SCALE_ADDRESS, wake_scale
from .fake_scale import FakeReading, FakeScale

ANNA = {
    "slot": 2,
    "consent_code": 4321,
    "name": "Anna",
    "person": None,
    "profile": {
        "birth_date": "1985-04-12",
        "height_cm": 180,
        "gender": "male",
        "activity_level": 3,
    },
}


def local_now() -> datetime:
    """HA's local time without a time zone, as the scale's clock shows it."""
    return dt_util.now().replace(tzinfo=None, microsecond=0)


def as_state(scale_time: datetime) -> str:
    """The state of a timestamp sensor for a time on the scale's clock."""
    aware = scale_time.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_utc(aware).isoformat()


def anna_scale() -> MockConfigEntry:
    """A BF450 entry whose scale user Anna sits in slot 2."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="BF450",
        unique_id=SCALE_ADDRESS,
        data={CONF_ADDRESS: SCALE_ADDRESS, CONF_MODEL: "BF450"},
        subentries_data=[
            {
                "data": ANNA,
                "subentry_type": "scale_user",
                "title": "Anna",
                "unique_id": "slot_2",
            }
        ],
    )


@pytest.fixture
async def anna(hass: HomeAssistant, fake_scale: FakeScale) -> MockConfigEntry:
    """A loaded scale with Anna adopted, and her slot on the fake scale."""
    fake_scale.add_user(slot=2, consent_code=4321)
    entry = anna_scale()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def entity_id(hass: HomeAssistant, unique_id: str) -> str:
    """The entity id of one of the integration's sensors."""
    found = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    assert found is not None, unique_id
    return found


def state(hass: HomeAssistant, unique_id: str) -> State:
    """The state of one of the integration's sensors."""
    found = hass.states.get(entity_id(hass, unique_id))
    assert found is not None
    return found


WEIGHT = f"{SCALE_ADDRESS}_slot_2_weight"
LAST_READING = f"{SCALE_ADDRESS}_slot_2_last_reading"
BATTERY = f"{SCALE_ADDRESS}_battery"
LAST_SYNC = f"{SCALE_ADDRESS}_last_sync"


async def test_a_fresh_reading_updates_the_scale_users_sensors(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Anna weighs in; HA syncs within the wake window and shows her weight."""
    taken = local_now()
    fake_scale.weigh_in(2, FakeReading.from_capture(frames.READING_SLOT_2, at=taken))

    await wake_scale(hass, fake_scale)

    weight = state(hass, WEIGHT)
    assert weight.state == "78.4"
    assert weight.attributes["unit_of_measurement"] == "kg"
    assert weight.attributes["device_class"] == "weight"
    assert weight.attributes["state_class"] == "measurement"
    assert state(hass, LAST_READING).state == as_state(taken)
    assert state(hass, LAST_READING).attributes["device_class"] == "timestamp"
    assert not fake_scale.connected
    # The slot is no longer pending once its readings are delivered.
    assert fake_scale.service_data() == {"00001d18-0000-1000-8000-00805f9b34fb": b""}


async def test_the_scale_device_shows_battery_and_last_sync(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A sync reads the battery and records when it happened."""
    fake_scale.battery_level = 64
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )

    await wake_scale(hass, fake_scale)

    battery = state(hass, BATTERY)
    assert battery.state == "64"
    assert battery.attributes["unit_of_measurement"] == "%"
    assert battery.attributes["device_class"] == "battery"
    last_sync = state(hass, LAST_SYNC)
    assert last_sync.attributes["device_class"] == "timestamp"
    assert dt_util.parse_datetime(last_sync.state) is not None
    registry_entry = er.async_get(hass).async_get(entity_id(hass, LAST_SYNC))
    assert registry_entry is not None
    assert registry_entry.entity_category == "diagnostic"


async def test_the_newest_of_several_readings_wins(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Stored readings arrive oldest first; the sensors end on the newest."""
    newest = local_now()
    older = newest - timedelta(minutes=1)
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.STORED_READING_SLOT_1_A, at=older)
    )
    fake_scale.weigh_in(2, FakeReading.from_capture(frames.READING_SLOT_2, at=newest))

    await wake_scale(hass, fake_scale)

    assert state(hass, WEIGHT).state == "78.4"
    assert state(hass, LAST_READING).state == as_state(newest)


async def test_no_connection_without_a_pending_slot(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A wake with an empty pending-slot list doesn't connect."""
    await wake_scale(hass, fake_scale)

    assert fake_scale.connection_attempts == 0
    assert state(hass, WEIGHT).state == STATE_UNKNOWN


async def test_no_connection_when_only_an_unmanaged_slot_is_pending(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Readings of a slot HA doesn't manage are left alone."""
    fake_scale.add_user(slot=1, consent_code=1111)
    fake_scale.weigh_in(
        1, FakeReading.from_capture(frames.STORED_READING_SLOT_1_A, at=local_now())
    )

    await wake_scale(hass, fake_scale)

    assert fake_scale.connection_attempts == 0


async def test_a_stuck_sync_is_cut_off_and_disconnects(
    hass: HomeAssistant,
    anna: MockConfigEntry,
    fake_scale: FakeScale,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole sync runs under a time budget and always disconnects."""
    monkeypatch.setattr(
        "custom_components.beurer_scales.protocol.session.SESSION_TIMEOUT", 0.05
    )
    fake_scale.answers_ucp = False
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )

    await wake_scale(hass, fake_scale)

    assert not fake_scale.connected
    assert state(hass, WEIGHT).state == STATE_UNKNOWN


async def test_a_failed_sync_is_not_retried_at_once(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """After a failed sync, the next advertisements don't reconnect right away."""
    fake_scale.pairing = "unsupported"
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )
    await wake_scale(hass, fake_scale)
    attempts = fake_scale.connection_attempts
    assert attempts >= 1

    await wake_scale(hass, fake_scale)

    assert fake_scale.connection_attempts == attempts


async def test_sensors_restore_their_values_after_a_restart(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """After an HA restart the sensors show their last values again."""
    taken = dt_util.parse_datetime("2026-10-16T05:12:30+00:00")
    synced = dt_util.parse_datetime("2026-10-16T05:12:50+00:00")
    stored = [
        ("sensor.anna_weight", 78.4, "kg"),
        ("sensor.anna_last_reading", taken, None),
        ("sensor.bf450_battery", 64, "%"),
        ("sensor.bf450_last_sync", synced, None),
    ]
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(eid, "restored"),
                {
                    "native_value": (
                        {"__type": str(datetime), "isoformat": value.isoformat()}
                        if isinstance(value, datetime)
                        else value
                    ),
                    "native_unit_of_measurement": unit,
                },
            )
            for eid, value, unit in stored
        ],
    )
    entry = anna_scale()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entity_id(hass, WEIGHT) == "sensor.anna_weight"
    assert entity_id(hass, BATTERY) == "sensor.bf450_battery"
    assert state(hass, WEIGHT).state == "78.4"
    assert state(hass, LAST_READING).state == "2026-10-16T05:12:30+00:00"
    assert state(hass, BATTERY).state == "64"
    assert state(hass, LAST_SYNC).state == "2026-10-16T05:12:50+00:00"

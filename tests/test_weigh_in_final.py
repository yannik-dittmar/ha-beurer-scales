"""When a wake's pending slots are synced (ADR 0008, #24).

The scale appends a weigh-in's slot to its pending-slot list once the weigh-in
is final, ~10 s after stepping on. Connecting earlier and consenting someone
else aborts the weigh-in (protocol doc §9.6, V7). The list comes only in scan
responses: the first one of a wake shows the slots pending before it, which
wait until the weigh-in is final; a slot appended later is synced at once.
"""

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from . import frames
from .conftest import (
    SWAPPED_WEIGHT_SCALE_UUID,
    advertisement,
    inject_advertisement,
    scale_advertisement,
    scan_response,
    stay_awake,
    wake_scale,
)
from .fake_scale import FakeReading, FakeScale
from .test_sync import WEIGHT, anna_scale, local_now, state

# The sync waits for a quiet period, so time has to run.
pytestmark = pytest.mark.freeze_time(tick=True)


def anna_weighs_in(scale: FakeScale) -> None:
    """Anna (slot 2) weighs in while HA isn't connected."""
    scale.weigh_in(2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now()))


async def load_anna(hass: HomeAssistant) -> MockConfigEntry:
    """Start HA's side of Anna's scale afresh, as after a restart or reload."""
    entry = anna_scale()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
def anna_pending_before(fake_scale: FakeScale) -> FakeScale:
    """Anna weighed in at an earlier wake that nobody synced."""
    fake_scale.add_user(slot=2, consent_code=4321)
    anna_weighs_in(fake_scale)
    fake_scale.sleep()
    return fake_scale


async def wait(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    """Let time pass without the scale advertising."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_slots_pending_before_the_wake_wait_after_a_restart(
    hass: HomeAssistant,
    anna_pending_before: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """HA remembers nothing of earlier wakes; the first scan response tells."""
    scale = anna_pending_before
    await load_anna(hass)

    await wake_scale(hass, scale)
    await stay_awake(hass, freezer, scale, 22)
    assert scale.connection_attempts == 0

    await stay_awake(hass, freezer, scale, 4)
    assert state(hass, WEIGHT).state == "78.4"
    assert scale.connection_attempts == 1


async def test_a_late_first_scan_response_lists_slots_pending_before(
    hass: HomeAssistant,
    anna_pending_before: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A first scan response 12 s into the wake still waits for the weigh-in.

    Until then HA sees only advertisements, which still show the empty list of
    the last wake's scan response.
    """
    scale = anna_pending_before
    await load_anna(hass)
    scale.wake()
    for _ in range(6):
        await inject_advertisement(hass, advertisement(scale, merged_slots=b""))
        await wait(hass, freezer, 2)

    await inject_advertisement(hass, scan_response(scale))
    assert scale.connection_attempts == 0

    await stay_awake(hass, freezer, scale, 14)
    assert state(hass, WEIGHT).state == "78.4"


async def test_a_slot_appended_to_a_later_scan_response_syncs_at_once(
    hass: HomeAssistant,
    anna_pending_before: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Anna's new weigh-in is final when her slot is appended, so HA connects."""
    scale = anna_pending_before
    scale.users[2].stored_readings.clear()
    await load_anna(hass)
    await wake_scale(hass, scale)
    await stay_awake(hass, freezer, scale, 10)

    anna_weighs_in(scale)
    await wake_scale(hass, scale)

    assert state(hass, WEIGHT).state == "78.4"
    assert scale.connection_attempts == 1


async def test_a_slot_list_merged_into_an_advertisement_is_not_trusted(
    hass: HomeAssistant, fake_scale: FakeScale, freezer: FrozenDateTimeFactory
) -> None:
    """HA's merged data still lists Anna from before her last sync; nothing to do."""
    fake_scale.add_user(slot=2, consent_code=4321)
    await load_anna(hass)
    fake_scale.wake()

    await inject_advertisement(hass, advertisement(fake_scale, merged_slots=b"\x02"))
    await inject_advertisement(hass, scan_response(fake_scale))
    await stay_awake(hass, freezer, fake_scale, 20)

    assert fake_scale.connection_attempts == 0


async def test_without_raw_packets_the_service_data_is_the_slot_list(
    hass: HomeAssistant,
    anna_pending_before: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A BlueZ adapter reports no raw packet; its service data is taken as is."""
    scale = anna_pending_before
    scale.users[2].stored_readings.clear()
    await load_anna(hass)
    scale.wake()
    await inject_advertisement(hass, scale_advertisement())

    anna_weighs_in(scale)
    scale.finish_weigh_in()
    await inject_advertisement(
        hass, scale_advertisement(service_data={SWAPPED_WEIGHT_SCALE_UUID: b"\x02"})
    )

    assert state(hass, WEIGHT).state == "78.4"
    assert scale.connection_attempts == 1

"""Several pending slots in one wake: the fresh weigh-in first, then the others."""

import logging

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import CONF_ADDRESS, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.beurer_scales.const import CONF_MODEL, DOMAIN

from . import frames
from .conftest import SCALE_ADDRESS, SWAPPED_WEIGHT_SCALE_UUID, stay_awake, wake_scale
from .fake_scale import FakeReading, FakeScale, SecondConsent
from .test_sync import ANNA, local_now, state

BEN = {**ANNA, "slot": 1, "consent_code": 1234, "name": "Ben"}

ANNA_WEIGHT = f"{SCALE_ADDRESS}_slot_2_weight"
BEN_WEIGHT = f"{SCALE_ADDRESS}_slot_1_weight"


@pytest.fixture
async def household(hass: HomeAssistant, fake_scale: FakeScale) -> MockConfigEntry:
    """A loaded scale with Ben in slot 1 and Anna in slot 2."""
    fake_scale.add_user(slot=1, consent_code=1234)
    fake_scale.add_user(slot=2, consent_code=4321)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="BF450",
        unique_id=SCALE_ADDRESS,
        data={CONF_ADDRESS: SCALE_ADDRESS, CONF_MODEL: "BF450"},
        subentries_data=[
            {
                "data": user,
                "subentry_type": "scale_user",
                "title": user["name"],
                "unique_id": f"slot_{user['slot']}",
            }
            for user in (BEN, ANNA)
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def weigh_in(scale: FakeScale, slot: int) -> None:
    """Someone the scale recognises as `slot` weighs in while HA isn't connected."""
    capture = frames.STORED_READING_SLOT_1_A if slot == 1 else frames.READING_SLOT_2
    scale.weigh_in(slot, FakeReading.from_capture(capture, at=local_now()))


def pending(scale: FakeScale) -> bytes:
    """The pending-slot list the scale advertises."""
    return scale.service_data()[SWAPPED_WEIGHT_SCALE_UUID]


def consents(scale: FakeScale) -> list[int]:
    """The slots HA asked the scale to consent, in order."""
    return [request[1] for request in scale.ucp_requests if request[0] == 0x02]


async def test_other_pending_slots_are_drained_in_the_same_connection(
    hass: HomeAssistant, household: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Both scale users' readings arrive through one connection."""
    weigh_in(fake_scale, 1)
    weigh_in(fake_scale, 2)

    await wake_scale(hass, fake_scale)

    assert state(hass, ANNA_WEIGHT).state == "78.4"
    assert state(hass, BEN_WEIGHT).state == "77.9"
    assert fake_scale.connection_attempts == 1
    assert pending(fake_scale) == b""
    assert not fake_scale.connected


# The sync waits for a quiet period, so time has to run.
@pytest.mark.freeze_time(tick=True)
@pytest.mark.parametrize("refusal", ["rejected", "dropped"])
async def test_a_refused_second_consent_leaves_the_rest_for_a_later_wake(
    hass: HomeAssistant,
    household: MockConfigEntry,
    fake_scale: FakeScale,
    refusal: SecondConsent,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One consent per connection: the newest slot now, the other one next wake."""
    fake_scale.second_consent = refusal
    weigh_in(fake_scale, 1)
    weigh_in(fake_scale, 2)

    await wake_scale(hass, fake_scale)

    # Two slots listed at once: the last one listed is taken as the newest.
    assert state(hass, ANNA_WEIGHT).state == "78.4"
    assert state(hass, BEN_WEIGHT).state == STATE_UNKNOWN
    assert pending(fake_scale) == b"\x01"
    assert not fake_scale.connected

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 30)

    assert state(hass, BEN_WEIGHT).state == "77.9"
    assert pending(fake_scale) == b""
    assert not fake_scale.connected


async def test_the_newly_pending_slot_is_synced_first(
    hass: HomeAssistant,
    household: MockConfigEntry,
    fake_scale: FakeScale,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slot left pending by an earlier wake waits for the fresh weigh-in."""
    monkeypatch.setattr("custom_components.beurer_scales.coordinator.RETRY_DELAY", 0.0)
    fake_scale.pairing = "unsupported"
    weigh_in(fake_scale, 2)
    await wake_scale(hass, fake_scale)
    assert pending(fake_scale) == b"\x02"
    fake_scale.pairing = "supported"
    ucp_before = len(fake_scale.ucp_requests)

    # The next wake first lists only Anna's leftover slot: HA waits.
    await wake_scale(hass, fake_scale)
    assert len(fake_scale.ucp_requests) == ucp_before

    # Ben's weigh-in is final; the scale appends his slot.
    weigh_in(fake_scale, 1)
    await wake_scale(hass, fake_scale)

    assert consents(fake_scale)[-2:] == [1, 2]
    assert state(hass, BEN_WEIGHT).state == "77.9"
    assert state(hass, ANNA_WEIGHT).state == "78.4"
    assert pending(fake_scale) == b""


@pytest.mark.freeze_time(tick=True)
async def test_unmanaged_pending_slots_are_ignored_quietly(
    hass: HomeAssistant,
    household: MockConfigEntry,
    fake_scale: FakeScale,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Another client's slot is never consented and never causes a sync."""
    caplog.set_level(logging.DEBUG, logger="custom_components.beurer_scales")
    fake_scale.add_user(slot=3, consent_code=5555)
    weigh_in(fake_scale, 3)

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 30)
    # Nobody HA manages was recognised: HA only checks the user list (ADR 0008).
    assert consents(fake_scale) == []

    weigh_in(fake_scale, 2)
    await wake_scale(hass, fake_scale)

    assert consents(fake_scale) == [2]
    assert pending(fake_scale) == b"\x03"
    about_slot_3 = [r for r in caplog.records if "[3]" in r.getMessage()]
    assert about_slot_3
    assert {r.levelno for r in about_slot_3} == {logging.DEBUG}

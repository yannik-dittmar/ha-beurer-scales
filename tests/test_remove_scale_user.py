"""Removing a scale user: gone from HA at once, deleted on the scale at the next wake."""

import logging
from datetime import timedelta
from types import MappingProxyType

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.beurer_scales.const import CONF_MODEL, DOMAIN

from . import frames
from .conftest import SCALE_ADDRESS, stay_awake, wake_scale
from .fake_scale import FakeReading, FakeScale
from .test_sync import ANNA, WEIGHT, local_now, state

BOB = {
    "slot": 3,
    "consent_code": 5678,
    "name": "Bob",
    "person": None,
    "profile": {
        "birth_date": "1990-01-02",
        "height_cm": 175,
        "gender": "male",
        "activity_level": 2,
    },
}
BOB_WEIGHT = f"{SCALE_ADDRESS}_slot_3_weight"


def household() -> MockConfigEntry:
    """A BF450 entry with Anna in slot 2 and Bob in slot 3."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="BF450",
        unique_id=SCALE_ADDRESS,
        data={CONF_ADDRESS: SCALE_ADDRESS, CONF_MODEL: "BF450"},
        subentries_data=[
            {
                "data": data,
                "subentry_type": "scale_user",
                "title": data["name"],
                "unique_id": f"slot_{data['slot']}",
            }
            for data in (ANNA, BOB)
        ],
    )


@pytest.fixture
async def scale(hass: HomeAssistant, fake_scale: FakeScale) -> MockConfigEntry:
    """A loaded scale with Anna and Bob, both also on the fake scale."""
    fake_scale.add_user(slot=2, consent_code=4321)
    fake_scale.add_user(slot=3, consent_code=5678)
    entry = household()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def remove_bob(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Remove Bob's scale user subentry, as the UI does."""
    [bob] = [s for s in entry.subentries.values() if s.unique_id == "slot_3"]
    assert hass.config_entries.async_remove_subentry(entry, bob.subentry_id)
    await hass.async_block_till_done()


async def test_removing_a_scale_user_removes_its_device_and_entities(
    hass: HomeAssistant,
    scale: MockConfigEntry,
    fake_scale: FakeScale,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Bob's device and sensors go right away, without the scale."""
    await remove_bob(hass, scale)

    assert not device_registry.async_get_devices(
        identifiers={(DOMAIN, f"{SCALE_ADDRESS}_slot_3")}
    )
    assert device_registry.async_get_devices(
        identifiers={(DOMAIN, f"{SCALE_ADDRESS}_slot_2")}
    )
    assert entity_registry.async_get_entity_id("sensor", DOMAIN, BOB_WEIGHT) is None
    assert state(hass, WEIGHT) is not None
    assert fake_scale.connection_attempts == 0
    assert 3 in fake_scale.users


async def test_the_scale_user_is_deleted_once_the_weigh_in_is_final(
    hass: HomeAssistant,
    scale: MockConfigEntry,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """At a wake without a managed slot pending, HA waits ~25 s, then deletes."""
    await remove_bob(hass, scale)

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 20)
    assert fake_scale.connection_attempts == 0
    assert 3 in fake_scale.users

    await stay_awake(hass, freezer, fake_scale, 6)

    assert 3 not in fake_scale.users
    assert 2 in fake_scale.users
    assert not fake_scale.connected


async def test_the_pending_deletion_is_cleared_once_done(
    hass: HomeAssistant,
    scale: MockConfigEntry,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After the deletion, later wakes don't consent as Bob again.

    They still connect once to check the user list (ADR 0008).
    """
    await remove_bob(hass, scale)
    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)
    requests = len(fake_scale.ucp_requests)

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 30)

    assert len(fake_scale.ucp_requests) == requests


# The sync waits for a quiet period, so time has to run.
@pytest.mark.freeze_time(tick=True)
async def test_the_deletion_rides_along_with_a_sync(
    hass: HomeAssistant,
    scale: MockConfigEntry,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """When Anna's weigh-in syncs, Bob is deleted in the same connection."""
    await remove_bob(hass, scale)
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )

    await wake_scale(hass, fake_scale)

    assert state(hass, WEIGHT).state == "78.4"
    assert 3 not in fake_scale.users
    assert fake_scale.connection_attempts == 1
    assert not fake_scale.connected

    await stay_awake(hass, freezer, fake_scale, 30)
    assert fake_scale.connection_attempts == 1


async def test_the_pending_deletion_survives_a_restart(
    hass: HomeAssistant,
    scale: MockConfigEntry,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A deletion still waiting when HA stops runs after the next start."""
    await remove_bob(hass, scale)
    assert await hass.config_entries.async_unload(scale.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(scale.entry_id)
    await hass.async_block_till_done()

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    assert 3 not in fake_scale.users


async def test_a_rejected_consent_drops_the_pending_deletion(
    hass: HomeAssistant,
    scale: MockConfigEntry,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the scale no longer takes Bob's code, HA gives up on deleting him."""
    caplog.set_level(logging.DEBUG, logger="custom_components.beurer_scales")
    await remove_bob(hass, scale)
    fake_scale.users[3].consent_code = 1234

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    assert 3 in fake_scale.users
    assert "Dropping the deletion of slot 3" in caplog.text
    requests = len(fake_scale.ucp_requests)
    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 30)
    assert len(fake_scale.ucp_requests) == requests


async def test_a_wake_that_ends_early_keeps_the_pending_deletion(
    hass: HomeAssistant,
    scale: MockConfigEntry,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """If the scale stops advertising before ~25 s, HA tries at the next wake."""
    await remove_bob(hass, scale)
    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 6)
    fake_scale.awake = False
    await stay_awake(hass, freezer, fake_scale, 30)
    assert fake_scale.connection_attempts == 0

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    assert 3 not in fake_scale.users


async def test_adopting_the_slot_again_cancels_the_pending_deletion(
    hass: HomeAssistant,
    scale: MockConfigEntry,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Bob removed by mistake and adopted again before a wake stays on the scale."""
    await remove_bob(hass, scale)
    hass.config_entries.async_add_subentry(
        scale,
        ConfigSubentry(
            data=MappingProxyType(BOB),
            subentry_type="scale_user",
            title="Bob",
            unique_id="slot_3",
        ),
    )
    await hass.async_block_till_done()

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    assert 3 in fake_scale.users


@pytest.mark.freeze_time(tick=True)
async def test_a_leftover_slot_and_the_deletion_share_one_connection(
    hass: HomeAssistant,
    scale: MockConfigEntry,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A wake listing only a slot left pending earlier syncs it, then deletes."""
    await remove_bob(hass, scale)
    fake_scale.pairing = "unsupported"
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )
    await wake_scale(hass, fake_scale)
    fake_scale.awake = False
    await stay_awake(hass, freezer, fake_scale, 30)
    fake_scale.pairing = "supported"
    attempts = fake_scale.connection_attempts

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 20)
    assert fake_scale.connection_attempts == attempts
    await stay_awake(hass, freezer, fake_scale, 6)

    assert state(hass, WEIGHT).state == "78.4"
    assert 3 not in fake_scale.users
    assert fake_scale.connection_attempts == attempts + 1


# The sync waits for a quiet period, so time has to run.
@pytest.mark.freeze_time(tick=True)
async def test_a_refused_second_consent_keeps_the_pending_deletion(
    hass: HomeAssistant,
    scale: MockConfigEntry,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Refused after Anna's sync, Bob's deletion runs alone at a later wake.

    A scale may refuse every consent after the first in a connection (design
    §8, V2): that says nothing about Bob's code.
    """
    fake_scale.second_consent = "rejected"
    await remove_bob(hass, scale)
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )

    await wake_scale(hass, fake_scale)
    assert state(hass, WEIGHT).state == "78.4"
    assert 3 in fake_scale.users

    # A new wake: advertising within 5 s of HA's disconnect is the tail of the
    # sync's session (#25).
    freezer.tick(timedelta(seconds=10))
    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    assert 3 not in fake_scale.users
    assert not fake_scale.connected

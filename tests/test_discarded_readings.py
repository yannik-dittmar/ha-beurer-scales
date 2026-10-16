"""Backlog and guessed readings never reach the sensors; duplicates count once."""

from datetime import timedelta

import pytest
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_persistent_notifications,
)

from . import frames
from .conftest import (
    SWAPPED_WEIGHT_SCALE_UUID,
    inject_advertisement,
    scale_advertisement,
    wake_scale,
)
from .fake_scale import FakeReading, FakeScale
from .test_sync import LAST_READING, WEIGHT, anna, as_state, local_now, state

__all__ = ["anna"]  # the fixture


def discard_notifications(hass: HomeAssistant) -> list[str]:
    """The messages of the integration's discard notifications."""
    return [
        n["message"]
        for key, n in async_get_persistent_notifications(hass).items()
        if key.startswith("beurer_scales_") and "discarded" in key
    ]


async def weigh_in_while_awake(
    hass: HomeAssistant, scale: FakeScale, reading: FakeReading
) -> None:
    """Step on the scale again: its first adverts list no slot until the weigh-in
    is final (§9.4b), then the slot turns pending and HA syncs."""
    scale.wake()
    await inject_advertisement(
        hass,
        scale_advertisement(service_data={SWAPPED_WEIGHT_SCALE_UUID: b""}),
    )
    scale.weigh_in(2, reading)
    await wake_scale(hass, scale)


async def test_backlog_then_fresh_only_the_fresh_one_updates_the_sensors(
    hass: HomeAssistant,
    anna: MockConfigEntry,
    fake_scale: FakeScale,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reading from while HA was down is collected but discarded (ADR 0005)."""
    now = local_now()
    backlog = now - timedelta(hours=2)
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.STORED_READING_SLOT_1_A, at=backlog)
    )
    fake_scale.weigh_in(2, FakeReading.from_capture(frames.READING_SLOT_2, at=now))

    await wake_scale(hass, fake_scale)

    assert state(hass, WEIGHT).state == "78.4"
    assert state(hass, LAST_READING).state == as_state(now)
    (message,) = discard_notifications(hass)
    assert "Anna" in message
    assert "1 backlog reading" in message
    assert backlog.strftime("%Y-%m-%d %H:%M") in message
    assert "backlog" in message
    assert "discarded" in caplog.text.lower()


async def test_a_backlog_reading_alone_leaves_the_sensors_unchanged(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """The slot's pending flag clears, but its sensors stay as they were."""
    fake_scale.weigh_in(
        2,
        FakeReading.from_capture(
            frames.READING_SLOT_2, at=local_now() - timedelta(minutes=6)
        ),
    )

    await wake_scale(hass, fake_scale)

    assert state(hass, WEIGHT).state == STATE_UNKNOWN
    assert fake_scale.service_data() == {"00001d18-0000-1000-8000-00805f9b34fb": b""}


async def test_guessed_readings_are_discarded_with_a_notification(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A U-- reading never lands on the guessed scale user's sensors (ADR 0006)."""
    first = local_now() - timedelta(minutes=2)
    second = local_now() - timedelta(minutes=1)
    for at in (first, second):
        fake_scale.weigh_in(
            2, FakeReading.from_capture(frames.GUESSED_READING_SLOT_2, at=at)
        )

    await wake_scale(hass, fake_scale)

    assert state(hass, WEIGHT).state == STATE_UNKNOWN
    (message,) = discard_notifications(hass)
    assert "2 guessed readings" in message
    assert first.strftime("%H:%M") in message
    assert second.strftime("%H:%M") in message
    assert "U--" in message


async def test_each_sync_that_discards_gets_its_own_notification(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """A later discard doesn't overwrite the notice of an earlier one."""
    for seconds_ago in (20, 10):
        await weigh_in_while_awake(
            hass,
            fake_scale,
            FakeReading.from_capture(
                frames.GUESSED_READING_SLOT_2,
                at=local_now() - timedelta(seconds=seconds_ago),
            ),
        )

    assert len(discard_notifications(hass)) == 2


async def test_a_sync_with_only_fresh_readings_notifies_nobody(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """Fresh readings need no notice."""
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )

    await wake_scale(hass, fake_scale)

    assert discard_notifications(hass) == []


async def test_a_reading_delivered_again_after_a_restart_is_ignored(
    hass: HomeAssistant, anna: MockConfigEntry, fake_scale: FakeScale
) -> None:
    """The de-duplication log is persisted: a re-delivered older reading after
    a restart doesn't push the sensors back to it."""
    older = FakeReading.from_capture(
        frames.STORED_READING_SLOT_1_A, at=local_now() - timedelta(minutes=1)
    )
    newer_at = local_now()
    await weigh_in_while_awake(hass, fake_scale, older)
    await weigh_in_while_awake(
        hass, fake_scale, FakeReading.from_capture(frames.READING_SLOT_2, at=newer_at)
    )
    assert state(hass, WEIGHT).state == "78.4"

    assert await hass.config_entries.async_reload(anna.entry_id)
    await hass.async_block_till_done()
    # An interrupted sync left the older reading on the scale.
    await weigh_in_while_awake(hass, fake_scale, older)

    assert fake_scale.connection_attempts == 3
    assert state(hass, WEIGHT).state == "78.4"
    assert state(hass, LAST_READING).state == as_state(newer_at)

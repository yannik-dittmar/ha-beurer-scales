"""The check after an unrecognised weigh-in (ADR 0008, design §5.3).

A wake that reaches ~25 s without a managed slot pending makes HA connect, read
the user list and disconnect: a factory reset or a deletion on the scale would
otherwise go unnoticed, because the scale never lists those slots again.
"""

from datetime import timedelta
from types import MappingProxyType

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_get_persistent_notifications,
)

from . import frames
from .fake_scale import USER_LIST, FakeReading, FakeScale
from .test_remove_scale_user import BOB, remove_bob
from .test_scale_user_repairs import anna_id, scale_issues, setup_anna
from .test_sync import WEIGHT, anna_scale, local_now, state
from .conftest import stay_awake, wake_scale


def unrecognised_notifications(hass: HomeAssistant) -> list[dict[str, str]]:
    """The integration's "someone wasn't recognised" notifications."""
    return [
        n
        for key, n in async_get_persistent_notifications(hass).items()
        if key.startswith("beurer_scales_") and "unrecognised" in key
    ]


async def setup_anna_with_options(
    hass: HomeAssistant, options: dict[str, bool]
) -> MockConfigEntry:
    """Load Anna's scale with these scale options."""
    entry = anna_scale()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_a_factory_reset_is_found_after_an_unrecognised_weigh_in(
    hass: HomeAssistant,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Nobody is pending; at ~25 s HA reads the empty user list and raises a repair."""
    entry = await setup_anna(hass)
    fake_scale.factory_reset()

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    assert f"factory_reset_{anna_id(entry)}" in scale_issues(issue_registry)
    assert fake_scale.connection_attempts == 1
    assert fake_scale.rebonds == 1
    assert not fake_scale.connected


async def test_the_check_waits_until_the_weigh_in_is_final(
    hass: HomeAssistant, fake_scale: FakeScale, freezer: FrozenDateTimeFactory
) -> None:
    """Connecting earlier would cut the weigh-in short (the scale sleeps ~1 s later)."""
    await setup_anna(hass)
    fake_scale.add_user(slot=2, consent_code=4321)

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 22)
    assert fake_scale.connection_attempts == 0

    await stay_awake(hass, freezer, fake_scale, 4)
    assert fake_scale.connection_attempts == 1
    assert not fake_scale.connected


async def test_a_deleted_scale_user_is_found_after_an_unrecognised_weigh_in(
    hass: HomeAssistant,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Others remain on the scale, Anna's slot is gone: the same repair as at consent."""
    fake_scale.add_user(slot=1, consent_code=1111)
    entry = await setup_anna(hass)

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    assert set(scale_issues(issue_registry)) == {f"scale_user_deleted_{anna_id(entry)}"}
    assert fake_scale.rebonds == 0


async def test_the_check_and_a_pending_deletion_share_one_connection(
    hass: HomeAssistant,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Bob, removed in HA, is deleted while HA is connected for the check."""
    fake_scale.add_user(slot=3, consent_code=5678)
    entry = await setup_anna(hass)
    await remove_bob_later(hass, entry)

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    assert 3 not in fake_scale.users
    assert f"scale_user_deleted_{anna_id(entry)}" in scale_issues(issue_registry)
    assert fake_scale.connection_attempts == 1


async def remove_bob_later(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add Bob (slot 3) to Anna's scale, then remove him again in HA."""
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType(BOB),
            subentry_type="scale_user",
            title="Bob",
            unique_id="slot_3",
        ),
    )
    await hass.async_block_till_done()
    await remove_bob(hass, entry)


async def test_an_unrecognised_weigh_in_is_notified(
    hass: HomeAssistant, fake_scale: FakeScale, freezer: FrozenDateTimeFactory
) -> None:
    """By default HA says that someone stepped on the scale and wasn't recognised."""
    fake_scale.add_user(slot=2, consent_code=4321)
    await setup_anna(hass)

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    (notification,) = unrecognised_notifications(hass)
    assert "BF450" in notification["message"]
    assert "not recognised" in notification["title"]


async def test_the_notification_can_be_turned_off(
    hass: HomeAssistant, fake_scale: FakeScale, freezer: FrozenDateTimeFactory
) -> None:
    """With the scale option off, the check still runs but says nothing."""
    fake_scale.add_user(slot=2, consent_code=4321)
    await setup_anna_with_options(hass, {"notify_unrecognised": False})

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    assert fake_scale.connection_attempts == 1
    assert unrecognised_notifications(hass) == []


async def test_the_scale_options_offer_the_notification_on_by_default(
    hass: HomeAssistant, fake_scale: FakeScale, freezer: FrozenDateTimeFactory
) -> None:
    """Turning it off in the options flow silences the next unrecognised weigh-in."""
    fake_scale.add_user(slot=2, consent_code=4321)
    entry = await setup_anna(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    [key] = result["data_schema"].schema
    assert key == "notify_unrecognised"
    assert key.default() is True

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"notify_unrecognised": False}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"notify_unrecognised": False}

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)
    assert unrecognised_notifications(hass) == []


async def test_a_weigh_in_of_another_clients_scale_user_is_not_notified(
    hass: HomeAssistant, fake_scale: FakeScale, freezer: FrozenDateTimeFactory
) -> None:
    """The scale recognised someone HA doesn't manage: their slot turns pending."""
    fake_scale.add_user(slot=2, consent_code=4321)
    fake_scale.add_user(slot=3, consent_code=5555)
    await setup_anna(hass)

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 14)
    fake_scale.weigh_in(
        3, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )
    await stay_awake(hass, freezer, fake_scale, 12)

    assert fake_scale.connection_attempts == 1
    assert unrecognised_notifications(hass) == []


# The sync waits for a quiet period, so time has to run.
@pytest.mark.freeze_time(tick=True)
async def test_a_recognised_weigh_in_needs_no_extra_connection(
    hass: HomeAssistant, fake_scale: FakeScale, freezer: FrozenDateTimeFactory
) -> None:
    """Anna's slot turns pending: HA syncs it and connects no second time."""
    fake_scale.add_user(slot=2, consent_code=4321)
    await setup_anna(hass)

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 14)
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )
    await stay_awake(hass, freezer, fake_scale, 30)

    assert state(hass, WEIGHT).state == "78.4"
    assert fake_scale.connection_attempts == 1
    assert unrecognised_notifications(hass) == []


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: int
) -> None:
    """Let time pass without waiting for background tasks."""
    for _ in range(seconds // 2):
        freezer.tick(timedelta(seconds=2))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def test_the_check_ends_with_the_wake_window(
    hass: HomeAssistant, fake_scale: FakeScale, freezer: FrozenDateTimeFactory
) -> None:
    """Started at ~25 s, the check gets what is left of the ~47 s wake, not 40 s."""
    await setup_anna(hass)
    fake_scale.add_user(slot=2, consent_code=4321)
    fake_scale.write_failures[USER_LIST] = "stall"
    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 24)

    # Not waiting for background tasks: the check is stuck on the scale.
    await tick(hass, freezer, 6)
    assert fake_scale.stalled

    await tick(hass, freezer, 18)
    stalled_at_48_s = fake_scale.stalled
    await tick(hass, freezer, 30)

    # Gave up before the scale fell asleep, ~47 s after waking.
    assert not stalled_at_48_s
    assert not fake_scale.connected

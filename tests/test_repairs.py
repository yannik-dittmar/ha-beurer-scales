"""Repairs when a scale user's consent fails: wrong code, deleted, factory reset."""

from types import MappingProxyType

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beurer_scales.const import DOMAIN

from . import frames
from .conftest import (
    SCALE_ADDRESS,
    SWAPPED_WEIGHT_SCALE_UUID,
    inject_advertisement,
    scale_advertisement,
    wake_scale,
)
from .fake_scale import FakeReading, FakeScale
from .test_sync import ANNA, WEIGHT, anna_scale, local_now, state


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let every wake try to sync, so only an open repair holds a sync back."""
    monkeypatch.setattr("custom_components.beurer_scales.coordinator.RETRY_DELAY", 0.0)


async def setup_anna(hass: HomeAssistant) -> MockConfigEntry:
    """Load a scale whose scale user Anna is slot 2 with consent code 4321."""
    entry = anna_scale()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def anna_id(entry: MockConfigEntry) -> str:
    """The subentry id of Anna."""
    [subentry_id] = entry.subentries
    return subentry_id


def weigh_in_anna(scale: FakeScale) -> None:
    """Anna steps on the scale; the scale stores the reading for slot 2."""
    scale.weigh_in(2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now()))


def scale_issues(issue_registry: ir.IssueRegistry) -> dict[str, ir.IssueEntry]:
    """The integration's open repair issues by id."""
    return {
        issue_id: issue
        for (domain, issue_id), issue in issue_registry.issues.items()
        if domain == DOMAIN
    }


async def test_rejected_code_for_a_listed_slot_raises_a_wrong_code_repair(
    hass: HomeAssistant, fake_scale: FakeScale, issue_registry: ir.IssueRegistry
) -> None:
    """The slot still exists on the scale, so the consent code must be wrong."""
    fake_scale.add_user(slot=2, consent_code=9999)
    entry = await setup_anna(hass)
    weigh_in_anna(fake_scale)

    await wake_scale(hass, fake_scale)

    issue = scale_issues(issue_registry)[f"wrong_consent_code_{anna_id(entry)}"]
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == "wrong_consent_code"
    assert issue.translation_placeholders == {"name": "Anna", "scale": "BF450"}
    assert state(hass, WEIGHT).state == STATE_UNKNOWN
    assert not fake_scale.connected


async def wake_with_pending(hass: HomeAssistant, scale: FakeScale, *slots: int) -> None:
    """Wake the scale with an advertisement that lists `slots` as pending.

    The scale only lists slots that hold readings, so this stands in for an
    advertisement that raced a deletion or a reset.
    """
    scale.wake()
    await inject_advertisement(
        hass,
        scale_advertisement(service_data={SWAPPED_WEIGHT_SCALE_UUID: bytes(slots)}),
    )


async def test_slot_missing_from_the_list_raises_a_deleted_repair(
    hass: HomeAssistant, fake_scale: FakeScale, issue_registry: ir.IssueRegistry
) -> None:
    """Other scale users remain on the scale, Anna's slot is gone."""
    fake_scale.add_user(slot=1, consent_code=1111)
    entry = await setup_anna(hass)

    await wake_with_pending(hass, fake_scale, 2)

    issue_id = f"scale_user_deleted_{anna_id(entry)}"
    assert set(scale_issues(issue_registry)) == {issue_id}
    issue = scale_issues(issue_registry)[issue_id]
    assert issue.is_fixable
    assert issue.translation_key == "scale_user_deleted"
    assert issue.translation_placeholders == {"name": "Anna", "scale": "BF450"}


async def test_empty_user_list_raises_a_factory_reset_repair_and_rebonds_once(
    hass: HomeAssistant, fake_scale: FakeScale, issue_registry: ir.IssueRegistry
) -> None:
    """A reset scale lists nobody; HA renews the bond once and raises a repair."""
    entry = await setup_anna(hass)
    fake_scale.factory_reset()

    await wake_with_pending(hass, fake_scale, 2)

    issue = scale_issues(issue_registry)[f"factory_reset_{anna_id(entry)}"]
    assert issue.is_fixable
    assert issue.translation_key == "factory_reset"
    assert fake_scale.rebonds == 1
    assert not fake_scale.bond_stale
    assert not fake_scale.connected

    await wake_with_pending(hass, fake_scale, 2)

    assert fake_scale.rebonds == 1


async def test_no_sync_is_attempted_while_the_repair_is_open(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """Anna's slot stays pending, but HA doesn't reconnect for it."""
    fake_scale.add_user(slot=2, consent_code=9999)
    await setup_anna(hass)
    weigh_in_anna(fake_scale)
    await wake_scale(hass, fake_scale)
    assert fake_scale.connection_attempts == 1

    await wake_scale(hass, fake_scale)

    assert fake_scale.connection_attempts == 1


async def test_the_other_scale_users_keep_syncing(
    hass: HomeAssistant, fake_scale: FakeScale
) -> None:
    """A wrong code for Anna doesn't hold back Ben's sync."""
    fake_scale.add_user(slot=2, consent_code=9999)
    fake_scale.add_user(slot=3, consent_code=3333)
    entry = anna_scale()
    entry.add_to_hass(hass)
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType(
                {**ANNA, "slot": 3, "consent_code": 3333, "name": "Ben"}
            ),
            subentry_type="scale_user",
            title="Ben",
            unique_id="slot_3",
        ),
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    weigh_in_anna(fake_scale)
    await wake_scale(hass, fake_scale)
    fake_scale.weigh_in(
        3, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )

    await wake_scale(hass, fake_scale)

    assert state(hass, f"{SCALE_ADDRESS}_slot_3_weight").state == "78.4"
    assert state(hass, WEIGHT).state == STATE_UNKNOWN

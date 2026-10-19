"""A proxy that still holds the bond of a factory-reset scale (#23, protocol doc §9.6).

Bonding fails, the scale ends the link, and the error that says why is in an
earlier connect attempt, not in the final "Disconnected".
"""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beurer_scales.const import DOMAIN
from custom_components.beurer_scales.protocol.errors import (
    PairingFailed,
    ScaleConnectionError,
)
from custom_components.beurer_scales.protocol.models import BF450
from custom_components.beurer_scales.protocol.session import open_session

from . import frames
from .conftest import wake_scale
from .fake_scale import FakeReading, FakeScale
from .test_sync import WEIGHT, anna, local_now, state

__all__ = ["anna"]  # a fixture


@pytest.fixture
def scale(fake_scale: FakeScale) -> FakeScale:
    """An awake BF450 whose proxy holds a bond the scale forgot."""
    fake_scale.wake()
    fake_scale.proxy_bond_stale = True
    return fake_scale


# --- The session ---


async def test_a_stale_bond_is_removed_and_the_connect_repeated(
    scale: FakeScale,
) -> None:
    """The pairing error of an earlier attempt tells a stale bond from other failures."""
    async with open_session(
        scale.ble_device, BF450, remove_bond=scale.remove_proxy_bond
    ) as session:
        assert await session.read_user_list() == []

    assert scale.proxy_unpairs == 1


async def test_without_a_way_to_remove_the_bond_pairing_fails(scale: FakeScale) -> None:
    """Callers that can't remove a bond get the failure as before."""
    with pytest.raises(PairingFailed):
        async with open_session(scale.ble_device, BF450):
            pass

    assert scale.connection_attempts == 1


async def test_a_bond_that_cannot_be_removed_fails_pairing(scale: FakeScale) -> None:
    """The path can't unpair (not an ESPHome proxy): the failure stands."""

    async def cannot() -> bool:
        return False

    with pytest.raises(PairingFailed):
        async with open_session(scale.ble_device, BF450, remove_bond=cannot):
            pass

    assert scale.connection_attempts == 1


async def test_pairing_that_fails_again_after_the_removal_fails_pairing(
    scale: FakeScale,
) -> None:
    """The bond was removed once; a second failure is not retried again."""
    removed = 0

    async def remove_but_stay_stale() -> bool:
        nonlocal removed
        removed += 1
        return True

    with pytest.raises(PairingFailed):
        async with open_session(
            scale.ble_device, BF450, remove_bond=remove_but_stay_stale
        ):
            pass

    assert removed == 1
    assert scale.connection_attempts == 2


async def test_other_connect_failures_do_not_touch_the_bond(scale: FakeScale) -> None:
    """A scale that is not advertising says nothing about the bond."""
    scale.awake = False

    with pytest.raises(ScaleConnectionError):
        async with open_session(
            scale.ble_device, BF450, remove_bond=scale.remove_proxy_bond
        ):
            pass

    assert scale.proxy_unpairs == 0


# --- The coordinator ---


async def test_the_next_wake_syncs_after_the_proxy_bond_is_renewed(
    hass: HomeAssistant,
    anna: MockConfigEntry,
    fake_scale: FakeScale,
) -> None:
    """After a factory reset the sync works again without the user doing anything."""
    fake_scale.proxy_bond_stale = True
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )

    await wake_scale(hass, fake_scale)

    assert fake_scale.proxy_unpairs == 1
    assert state(hass, WEIGHT).state == "78.4"
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"pairing_{anna.entry_id}") is None


async def test_an_unremovable_stale_bond_raises_the_pairing_repair(
    hass: HomeAssistant,
    anna: MockConfigEntry,
    fake_scale: FakeScale,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No way to remove the bond: the repair mentions the factory reset."""

    async def cannot(*_args: object) -> bool:
        return False

    monkeypatch.setattr(
        "custom_components.beurer_scales.connection.async_remove_bond", cannot
    )
    fake_scale.proxy_bond_stale = True
    fake_scale.weigh_in(
        2, FakeReading.from_capture(frames.READING_SLOT_2, at=local_now())
    )

    await wake_scale(hass, fake_scale)

    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"pairing_{anna.entry_id}")
    assert issue is not None
    assert issue.translation_key == "pairing_impossible"

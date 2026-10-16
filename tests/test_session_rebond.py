"""Renewing the bond after a factory reset (protocol doc §4), without HA."""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from custom_components.beurer_scales.protocol.errors import ScaleConnectionError
from custom_components.beurer_scales.protocol.models import BF450
from custom_components.beurer_scales.protocol.session import open_session

from .fake_scale import FakeScale


@pytest.fixture
def scale() -> Iterator[FakeScale]:
    """An awake BF450 that the session connects to."""
    scale = FakeScale()
    scale.wake()
    with patch(
        "custom_components.beurer_scales.protocol.session.establish_connection",
        scale.establish_connection,
    ):
        yield scale


async def test_a_factory_reset_scale_lists_no_users(scale: FakeScale) -> None:
    """A reset wipes every scale user; the user list is empty afterwards."""
    scale.add_user(slot=1, consent_code=1234)
    scale.factory_reset()
    scale.wake()

    async with open_session(scale.ble_device, BF450) as session:
        assert await session.read_user_list() == []


async def test_rebond_replaces_the_stale_bond(scale: FakeScale) -> None:
    """After a reset the scale lost its bond keys; unpair and pair renew them."""
    scale.factory_reset()
    scale.wake()

    async with open_session(scale.ble_device, BF450) as session:
        await session.rebond()

    assert not scale.bond_stale
    assert scale.rebonds == 1
    assert not scale.connected


async def test_a_failed_rebond_is_a_connection_error(scale: FakeScale) -> None:
    """Pairing that fails on the way surfaces as ScaleConnectionError."""
    scale.factory_reset()
    scale.wake()
    scale.pairing_fails = True

    with pytest.raises(ScaleConnectionError):
        async with open_session(scale.ble_device, BF450) as session:
            await session.rebond()

    assert scale.bond_stale
    assert not scale.connected

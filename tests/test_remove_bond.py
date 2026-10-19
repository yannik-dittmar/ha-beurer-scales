"""Removing a proxy's bond through ESPHome's API (#23, reused by #30)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beurer_scales.bond import async_remove_bond

PROXY = "11:22:33:44:55:66"
SCALE = "AA:BB:CC:DD:EE:01"


def proxy_entry(hass: HomeAssistant, client: AsyncMock) -> None:
    """An ESPHome device entry whose Bluetooth MAC is `PROXY`."""
    entry = MockConfigEntry(domain="esphome", unique_id="proxy")
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        client=client,
        device_info=SimpleNamespace(
            bluetooth_mac_address=PROXY, mac_address="11:22:33:44:55:60"
        ),
    )


def device(source: str | None) -> BLEDevice:
    """The scale as the path `source` saw it."""
    return BLEDevice(SCALE, "BF450", {"source": source} if source else {})


async def test_the_proxy_the_scale_was_found_through_unpairs(
    hass: HomeAssistant,
) -> None:
    """The unpair request names the scale's address and goes to that proxy."""
    client = AsyncMock()
    client.bluetooth_device_unpair.return_value = SimpleNamespace(success=True, error=0)
    proxy_entry(hass, client)

    assert await async_remove_bond(hass, device(PROXY.lower())) is True

    client.bluetooth_device_unpair.assert_awaited_once_with(0xAABBCCDDEE01)


async def test_a_proxy_that_refuses_is_not_a_removed_bond(hass: HomeAssistant) -> None:
    """The proxy answers success=False."""
    client = AsyncMock()
    client.bluetooth_device_unpair.return_value = SimpleNamespace(
        success=False, error=-1
    )
    proxy_entry(hass, client)

    assert await async_remove_bond(hass, device(PROXY)) is False


async def test_a_proxy_that_fails_is_not_a_removed_bond(hass: HomeAssistant) -> None:
    """API errors are reported as False, never raised."""
    client = AsyncMock()
    client.bluetooth_device_unpair.side_effect = OSError("connection reset")
    proxy_entry(hass, client)

    assert await async_remove_bond(hass, device(PROXY)) is False


@pytest.mark.parametrize("source", [None, "local", "99:99:99:99:99:99"])
async def test_a_path_that_is_no_known_proxy_cannot_unpair(
    hass: HomeAssistant, source: str | None
) -> None:
    """A local adapter, or an unknown source, has no way to unpair without a link."""
    client = AsyncMock()
    proxy_entry(hass, client)

    assert await async_remove_bond(hass, device(source)) is False
    client.bluetooth_device_unpair.assert_not_called()

"""Shared fixtures for Beurer Scales tests."""

import time

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_get_advertisement_callback,
)
from homeassistant.core import HomeAssistant

# HA mounts the test plugin's config dir, which ships its own `custom_components`
# package. Importing ours first makes HA find this repo's integrations.
import custom_components  # noqa: F401

SCALE_ADDRESS = "AA:BB:CC:DD:EE:01"
PENDING_SLOTS_UUID = "00001d18-0000-1000-8000-00805f9b34fb"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components in every test."""


@pytest.fixture(autouse=True)
def auto_enable_bluetooth(
    auto_enable_custom_integrations: None, enable_bluetooth: None
) -> None:
    """Run every test with a mocked Bluetooth stack.

    Bluetooth reads the discovery matchers once at setup, so custom integrations
    must be enabled before it starts.
    """


def scale_advertisement(
    address: str = SCALE_ADDRESS,
    local_name: str | None = "BF450",
    pending_slots: bytes | None = b"",
    rssi: int = -60,
) -> BluetoothServiceInfoBleak:
    """Build an advertisement as a BF450 sends it after waking up."""
    service_data = {} if pending_slots is None else {PENDING_SLOTS_UUID: pending_slots}
    service_uuids = [PENDING_SLOTS_UUID] if pending_slots is not None else []
    advertisement = AdvertisementData(
        local_name=local_name,
        manufacturer_data={0x1106: b"\x00\x01\x00"},
        service_data=service_data,
        service_uuids=service_uuids,
        tx_power=None,
        rssi=rssi,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=local_name or address,
        address=address,
        rssi=rssi,
        manufacturer_data=advertisement.manufacturer_data,
        service_data=service_data,
        service_uuids=service_uuids,
        source="local",
        device=BLEDevice(address, local_name, {}),
        advertisement=advertisement,
        connectable=True,
        time=time.monotonic(),
        tx_power=None,
    )


async def inject_advertisement(
    hass: HomeAssistant, info: BluetoothServiceInfoBleak
) -> None:
    """Feed an advertisement into HA's Bluetooth manager and let discovery run."""
    async_get_advertisement_callback(hass)(info)
    # Discovery flows start as background tasks.
    await hass.async_block_till_done(wait_background_tasks=True)

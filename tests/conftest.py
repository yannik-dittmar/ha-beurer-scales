"""Shared fixtures for Beurer Scales tests."""

import time
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_get_advertisement_callback,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

# HA mounts the test plugin's config dir, which ships its own `custom_components`
# package. Importing ours first makes HA find this repo's integrations.
import custom_components  # noqa: F401
from custom_components.beurer_scales.const import CONF_MODEL, DOMAIN
from custom_components.beurer_scales.protocol.models import (
    SWAPPED_WEIGHT_SCALE_UUID as SWAPPED_WEIGHT_SCALE_UUID,
)

from .fake_scale import FakeScale

SCALE_ADDRESS = "AA:BB:CC:DD:EE:01"
WEIGHT_SCALE_UUID = "0000181d-0000-1000-8000-00805f9b34fb"


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


@pytest.fixture
def configured_scale(hass: HomeAssistant) -> MockConfigEntry:
    """A BF450 that is already set up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SCALE_ADDRESS,
        data={CONF_ADDRESS: SCALE_ADDRESS, CONF_MODEL: "BF450"},
    )
    entry.add_to_hass(hass)
    return entry


def scale_advertisement(
    address: str = SCALE_ADDRESS,
    local_name: str | None = "BF450",
    service_data: dict[str, bytes] | None = None,
    rssi: int = -60,
) -> BluetoothServiceInfoBleak:
    """Build an advertisement as a BF450 sends it after waking up.

    By default it carries an empty pending-slot list under the byte-swapped
    0x181D UUID, like a BF450 with no unsynced readings.
    """
    if service_data is None:
        service_data = {SWAPPED_WEIGHT_SCALE_UUID: b""}
    advertisement = AdvertisementData(
        local_name=local_name,
        manufacturer_data={0x1106: b"\x00\x01\x00"},
        service_data=service_data,
        service_uuids=list(service_data),
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
        service_uuids=list(service_data),
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


@pytest.fixture
def fake_scale() -> Iterator[FakeScale]:
    """A sleeping BF450 behind the Bluetooth boundary; HA connects to it."""
    scale = FakeScale(address=SCALE_ADDRESS)
    with patch(
        "custom_components.beurer_scales.protocol.session.establish_connection",
        scale.establish_connection,
    ):
        yield scale


@pytest.fixture
async def loaded_scale(
    hass: HomeAssistant, configured_scale: MockConfigEntry
) -> MockConfigEntry:
    """A BF450 that is set up and loaded."""
    assert await hass.config_entries.async_setup(configured_scale.entry_id)
    await hass.async_block_till_done()
    return configured_scale


async def wake_scale(hass: HomeAssistant, scale: FakeScale) -> None:
    """Step on the fake scale: it wakes up and advertises."""
    scale.wake()
    await inject_advertisement(
        hass,
        scale_advertisement(
            address=scale.address,
            local_name=scale.local_name,
            service_data=scale.service_data(),
        ),
    )

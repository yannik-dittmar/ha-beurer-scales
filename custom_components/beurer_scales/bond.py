"""Removing the bond a Bluetooth path holds for a scale, without a connection.

A factory reset makes the scale forget its bond keys, while an ESPHome proxy
keeps its own. Bonding then fails until the proxy drops its bond, and a proxy
can do that without a connection (protocol doc §9.6, V6). Bleak's `unpair()`
can't: it needs a connected client, and the scale ends an unbonded link after
~0.5 s. So this asks the proxy directly, with ESPHome's
`bluetooth_device_unpair` over the API connection Home Assistant already holds.
"""

import asyncio
import logging
from typing import Any

from bleak.backends.device import BLEDevice

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

ESPHOME_DOMAIN = "esphome"
UNPAIR_TIMEOUT = 5.0


async def async_remove_bond(hass: HomeAssistant, device: BLEDevice) -> bool:
    """Remove the bond with `device` on the proxy it was found through.

    Returns whether the bond is gone. False when the path isn't an ESPHome
    proxy (a local adapter can't unpair without a connection) or the proxy
    refused or didn't answer; nothing is raised.
    """
    source = _source(device)
    client = _esphome_client(hass, source) if source else None
    if client is None:
        _LOGGER.debug(
            "Cannot remove the bond with %s: %s is not an ESPHome proxy",
            device.address,
            source or "the path",
        )
        return False
    try:
        async with asyncio.timeout(UNPAIR_TIMEOUT):
            response = await client.bluetooth_device_unpair(
                int(device.address.replace(":", ""), 16)
            )
    except Exception as err:  # noqa: BLE001 - a proxy, an API and a timeout can fail
        _LOGGER.warning(
            "Removing the bond with %s on proxy %s failed: %r",
            device.address,
            source,
            err,
        )
        return False
    if not response.success:
        _LOGGER.warning(
            "Proxy %s refused to remove the bond with %s: error %s",
            source,
            device.address,
            response.error,
        )
        return False
    _LOGGER.info("Removed the bond with %s on proxy %s", device.address, source)
    return True


def _source(device: BLEDevice) -> str | None:
    details = device.details
    if isinstance(details, dict) and (source := details.get("source")):
        return str(source)
    return None


def _esphome_client(hass: HomeAssistant, source: str) -> Any | None:
    """The API client of the ESPHome device whose Bluetooth MAC is `source`."""
    for entry in hass.config_entries.async_entries(ESPHOME_DOMAIN):
        data = getattr(entry, "runtime_data", None)
        info = getattr(data, "device_info", None)
        client = getattr(data, "client", None)
        if info is None or client is None:
            continue
        macs = {info.bluetooth_mac_address, info.mac_address}
        if source.upper() in {mac.upper() for mac in macs if mac}:
            return client
    return None

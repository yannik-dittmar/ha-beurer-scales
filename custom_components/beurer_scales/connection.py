"""Reaching a scale from Home Assistant: holding it, waiting for a wake and
opening a session.

The scale takes one connection at a time, so whoever talks to it (a flow, or
the coordinator) holds it first with `async_hold_scale`. Flows wait their turn
and keep the scale from the wait for the wake through the end of their session;
the coordinator never waits behind a flow: it leaves the scale alone while
`async_scale_held` and syncs at a later wake.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial

from bleak.backends.device import BLEDevice

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_register_advertisement_callback,
    async_scanner_devices_by_address,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from .bond import async_remove_bond
from .const import DOMAIN
from .protocol.errors import ScaleConnectionError
from .protocol.models import ScaleModel
from .protocol.session import ScaleSession, open_session

_LOGGER = logging.getLogger(__name__)

# How long a flow waits for someone to step on the scale.
WAKE_TIMEOUT = 180
# Proxies report a wake's first advertisements at slightly different times.
# Connecting at the very first one leaves HA a single path to choose from,
# which in the field was a distant proxy whose link broke (#17).
PATH_SETTLE = 1.5


ESPHOME_DOMAIN = "esphome"
BLUETOOTH_DOMAIN = "bluetooth"


def esphome_proxy_sources(hass: HomeAssistant) -> set[str]:
    """Return the set of known ESPHome Bluetooth proxy MAC addresses."""
    sources: set[str] = set()
    for entry in hass.config_entries.async_entries(ESPHOME_DOMAIN):
        data = getattr(entry, "runtime_data", None)
        info = getattr(data, "device_info", None)
        if info is not None:
            for mac in (info.bluetooth_mac_address, info.mac_address):
                if mac:
                    sources.add(mac.strip().upper())
    return sources


def is_local_source(hass: HomeAssistant | None, source: str | None) -> bool:
    """Return True when source is a local Bluetooth adapter on the HA host."""
    if not source:
        return True
    s = source.strip()
    s_lower = s.lower()
    # Common local adapter names/prefixes on Linux, Raspberry Pi, macOS, Windows, USB:
    # "local", "hci0", "hci1", "usb...", "/dev/...", etc.
    if (
        s_lower == "local"
        or s_lower.startswith("hci")
        or s_lower.startswith("usb")
        or s_lower.startswith("/dev/")
    ):
        return True
    if hass is not None:
        for entry in hass.config_entries.async_entries(BLUETOOTH_DOMAIN):
            if entry.unique_id and s_lower == entry.unique_id.strip().lower():
                return True
            adapter = entry.data.get("adapter")
            if adapter and s_lower == str(adapter).strip().lower():
                return True
    return False


def is_proxy_source(hass: HomeAssistant, source: str | None) -> bool:
    """Return True when source is an authorized remote proxy (e.g. ESPHome).

    Local host adapters (hci*, local, usb) are strictly excluded.
    If ESPHome proxies are registered in Home Assistant, the source
    must match one of them.
    """
    if not source or is_local_source(hass, source):
        return False
    esphome_macs = esphome_proxy_sources(hass)
    if esphome_macs:
        return source.strip().upper() in esphome_macs
    return True


def _is_local_adapter(info: BluetoothServiceInfoBleak) -> bool:
    """Return True when the advertisement came from a local Bluetooth adapter."""
    return is_local_source(None, info.source)


def _device_source(device: BLEDevice) -> str | None:
    """The source string from a BLEDevice's details dict, if present."""
    details = device.details
    if isinstance(details, dict) and (source := details.get("source")):
        return str(source)
    return None


@callback
def async_ble_device_for_scale(
    hass: HomeAssistant, address: str
) -> BLEDevice | None:
    """Return the best connectable BLEDevice for *address*, strictly requiring a
    remote proxy over any local adapter.

    For scales like the SBF77 that require bonding on an ESPHome proxy, the local
    adapter's path (hci*, local, usb) is strictly ignored to prevent triggering
    PIN prompts or invalidating the existing bond.
    """
    for scanner_device in async_scanner_devices_by_address(
        hass, address, connectable=True
    ):
        source = getattr(scanner_device.scanner, "source", None) or ""
        device = scanner_device.ble_device
        if device is None:
            continue
        if is_proxy_source(hass, source):
            return device

    _LOGGER.debug(
        "No connectable proxy path found for %s (local adapters like hci* are excluded)",
        address,
    )
    return None


_LOCKS = "scale_locks"
_SESSION_ENDED = "session_ended_at"


def session_ended_signal(address: str) -> str:
    """The dispatcher signal sent when any HA session with this scale has ended.

    The scale sleeps ~1 s after HA disconnects, whoever held the session.
    """
    return f"{DOMAIN}_session_ended_{address}"


@callback
def session_ended_at(hass: HomeAssistant, address: str) -> float | None:
    """When the last HA session with the scale ended, in event loop time.

    Kept outside the coordinator, which a flow's new scale user reloads.
    """
    return _session_ends(hass).get(address)


def _session_ends(hass: HomeAssistant) -> dict[str, float]:
    ends: dict[str, float] = hass.data.setdefault(DOMAIN, {}).setdefault(
        _SESSION_ENDED, {}
    )
    return ends


class WakeTimeout(Exception):
    """Nobody woke the scale in time."""


def _scale_lock(hass: HomeAssistant, address: str) -> asyncio.Lock:
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(DOMAIN, {}).setdefault(
        _LOCKS, {}
    )
    return locks.setdefault(address, asyncio.Lock())


@callback
def async_scale_held(hass: HomeAssistant, address: str) -> bool:
    """Whether a flow or the coordinator holds the scale right now."""
    return _scale_lock(hass, address).locked()


@asynccontextmanager
async def async_hold_scale(hass: HomeAssistant, address: str) -> AsyncIterator[None]:
    """Hold the scale, after whoever holds it now; nobody else connects meanwhile."""
    async with _scale_lock(hass, address):
        yield


async def async_wait_for_wake(
    hass: HomeAssistant, address: str, timeout: float = WAKE_TIMEOUT
) -> None:
    """Return at the scale's next advertisement from a proxy; raise WakeTimeout
    after `timeout`.

    A sleeping scale doesn't advertise, so any proxy advertisement means it is
    awake. Advertisements arriving via local adapters (hci*, local, usb) are
    ignored so that the scale's bond on the proxy is not disturbed.
    """
    woke: asyncio.Future[None] = hass.loop.create_future()

    @callback
    def on_advertisement(info: BluetoothServiceInfoBleak) -> None:
        if not woke.done() and is_proxy_source(hass, info.source):
            woke.set_result(None)

    @callback
    def on_timeout(_now: datetime) -> None:
        if not woke.done():
            woke.set_exception(WakeTimeout())

    cancel_advertisements = async_register_advertisement_callback(
        hass, on_advertisement, address
    )
    cancel_timer = async_call_later(hass, timeout, on_timeout)
    try:
        await woke
    finally:
        cancel_advertisements()
        cancel_timer()
    await async_let_paths_settle(hass, hass.loop.time())


async def async_let_paths_settle(hass: HomeAssistant, woke_at: float) -> None:
    """Wait until `PATH_SETTLE` after the wake, so every proxy in range has
    reported the scale and HA connects through the best one.

    `woke_at` is in event loop time.
    """
    if (delay := woke_at + PATH_SETTLE - hass.loop.time()) > 0:
        await asyncio.sleep(delay)


@asynccontextmanager
async def async_open_session(
    hass: HomeAssistant,
    address: str,
    model: ScaleModel,
    device: BLEDevice | None = None,
    session_timeout: float | None = None,
    deadline: float | None = None,
) -> AsyncIterator[ScaleSession]:
    """Connect to an awake scale through an authorized proxy path; always disconnect.

    Pass `device` from a connectable proxy advertisement that is being handled right
    now. If device came from a local adapter (hci*, local, usb), it is rejected
    and a proxy path is resolved instead.

    For the SBF77, automatic unpairing on failed connection is disabled so that
    a transient connection error cannot wipe the manual passkey bond on the ESP.
    """
    if device is None or is_local_source(hass, _device_source(device)):
        device = async_ble_device_for_scale(hass, address)
    if device is None:
        raise ScaleConnectionError(f"no connectable proxy path to {address}")
    opened = False
    try:
        remove_bond = (
            None
            if model.name == "SBF77"
            else partial(async_remove_bond, hass, device)
        )
        async with open_session(
            device,
            model,
            session_timeout=session_timeout,
            deadline=deadline,
            remove_bond=remove_bond,
        ) as session:
            opened = True
            yield session
    finally:
        if opened:
            _session_ends(hass)[address] = hass.loop.time()
            async_dispatcher_send(hass, session_ended_signal(address))

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


def _is_local_adapter(info: BluetoothServiceInfoBleak) -> bool:
    """Return True when the advertisement came from the HA host's built-in
    Bluetooth adapter (source == "local"), as opposed to a remote proxy.

    ESPHome proxy sources identify themselves by their Bluetooth MAC address;
    the local hci0 adapter uses the fixed string "local".  No device MACs are
    hardcoded here.
    """
    return (info.source or "").strip().lower() == "local"


@callback
def async_ble_device_for_scale(
    hass: HomeAssistant, address: str
) -> BLEDevice | None:
    """Return the best connectable BLEDevice for *address*, preferring a
    remote ESPHome proxy over the local adapter.

    When the scale is reachable through at least one proxy, the local adapter's
    path is ignored, because only the proxy holds the BLE bond for the SBF77.
    If no proxy path exists the local path is returned as a fallback (edge case:
    proxy offline) with a warning, so other scales that don't need bonding
    still work.
    """
    # Gather every currently-known connectable path for this address.
    proxy_device: BLEDevice | None = None
    local_device: BLEDevice | None = None

    for scanner_device in async_scanner_devices_by_address(
        hass, address, connectable=True
    ):
        # scanner_device.ble_device is the BLEDevice seen by this scanner.
        # scanner_device.scanner.source identifies the scanner:
        #   "local"           → the host's built-in Bluetooth adapter (hci0)
        #   "<MAC address>"   → an ESPHome Bluetooth proxy
        source = getattr(scanner_device.scanner, "source", None) or ""
        device = scanner_device.ble_device
        if device is None:
            continue
        if source.strip().lower() == "local":
            if local_device is None:
                local_device = device
        else:
            if proxy_device is None:
                proxy_device = device

    if proxy_device is not None:
        return proxy_device
    if local_device is not None:
        _LOGGER.warning(
            "No ESPHome proxy path found for %s; falling back to local adapter. "
            "The scale may request a new pairing code if the bond is on a proxy.",
            address,
        )
        return local_device
    # Last resort: use HA's standard lookup (may be None).
    return async_ble_device_from_address(hass, address, connectable=True)


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
    awake.  Advertisements arriving only via the local adapter are ignored so
    that the scale's bond on the proxy is not disturbed by a local connection.
    """
    woke: asyncio.Future[None] = hass.loop.create_future()

    @callback
    def on_advertisement(info: BluetoothServiceInfoBleak) -> None:
        if not woke.done() and not _is_local_adapter(info):
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
    """Connect to an awake scale through any connectable path; always disconnect.

    Pass `device` from a connectable advertisement that is being handled right
    now: HA hasn't recorded it yet. `session_timeout` replaces the default budget
    that fits the wake window, for sessions that keep the scale awake (teaching).
    `deadline` (event loop time) ends the session earlier, for work that starts
    late in a wake.

    A failed bond (a stale one after a factory reset) is cleared on the proxy
    and the connect repeated once (#23).
    """
    if device is None:
        device = async_ble_device_for_scale(hass, address)
    if device is None:
        raise ScaleConnectionError(f"no connectable Bluetooth path to {address}")
    opened = False
    try:
        async with open_session(
            device,
            model,
            session_timeout=session_timeout,
            deadline=deadline,
            remove_bond=partial(async_remove_bond, hass, device),
        ) as session:
            opened = True
            yield session
    finally:
        if opened:
            _session_ends(hass)[address] = hass.loop.time()
            async_dispatcher_send(hass, session_ended_signal(address))

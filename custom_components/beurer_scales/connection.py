"""Reaching a scale from Home Assistant: holding it, waiting for a wake and
opening a session.

The scale takes one connection at a time, so whoever talks to it (a flow, or
the coordinator) holds it first with `async_hold_scale`. Flows wait their turn
and keep the scale from the wait for the wake through the end of their session;
the coordinator never waits behind a flow: it leaves the scale alone while
`async_scale_held` and syncs at a later wake.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from bleak.backends.device import BLEDevice

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_register_advertisement_callback,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN
from .protocol.errors import ScaleConnectionError
from .protocol.models import ScaleModel
from .protocol.session import ScaleSession, open_session

# How long a flow waits for someone to step on the scale.
WAKE_TIMEOUT = 180


_LOCKS = "scale_locks"


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
    """Return at the scale's next advertisement; raise WakeTimeout after `timeout`.

    A sleeping scale doesn't advertise, so any advertisement means it is awake.
    """
    woke: asyncio.Future[None] = hass.loop.create_future()

    @callback
    def on_advertisement(_info: BluetoothServiceInfoBleak) -> None:
        if not woke.done():
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


@asynccontextmanager
async def async_open_session(
    hass: HomeAssistant,
    address: str,
    model: ScaleModel,
    device: BLEDevice | None = None,
    session_timeout: float | None = None,
) -> AsyncIterator[ScaleSession]:
    """Connect to an awake scale through any connectable path; always disconnect.

    Pass `device` from a connectable advertisement that is being handled right
    now: HA hasn't recorded it yet. `session_timeout` replaces the default budget
    that fits the wake window, for sessions that keep the scale awake (teaching).
    """
    if device is None:
        device = async_ble_device_from_address(hass, address, connectable=True)
    if device is None:
        raise ScaleConnectionError(f"no connectable Bluetooth path to {address}")
    async with open_session(device, model, session_timeout=session_timeout) as session:
        yield session

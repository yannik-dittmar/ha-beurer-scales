"""Reaching a scale from Home Assistant: waiting for a wake and opening a session."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_register_advertisement_callback,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .protocol.errors import ScaleConnectionError
from .protocol.models import ScaleModel
from .protocol.session import ScaleSession, open_session

# How long a flow waits for someone to step on the scale.
WAKE_TIMEOUT = 180


class WakeTimeout(Exception):
    """Nobody woke the scale in time."""


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
    hass: HomeAssistant, address: str, model: ScaleModel
) -> AsyncIterator[ScaleSession]:
    """Connect to an awake scale through any connectable path; always disconnect."""
    device = async_ble_device_from_address(hass, address, connectable=True)
    if device is None:
        raise ScaleConnectionError(f"no connectable Bluetooth path to {address}")
    async with open_session(device, model) as session:
        yield session

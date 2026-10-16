"""A connection to a scale and the operations it supports (protocol doc §3, §4).

`open_session` connects (always requesting a bond), caps the whole session with a
time budget that fits the scale's ~47 s wake window, and always disconnects.
Inside it, `ScaleSession` runs the GATT operations over any `BleakClient`-shaped
client and turns failures into the typed errors of `.errors`.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Protocol

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .errors import (
    ConsentRejected,
    PairingUnsupported,
    ScaleConnectionError,
    ScaleTimeout,
    UnexpectedResponse,
)
from .frames import (
    FrameError,
    UcpOpcode,
    UcpResponse,
    UcpResult,
    UserListEnd,
    UserListEntry,
    decode_ucp_response,
    decode_user_list_frame,
)
from .models import CURRENT_TIME_UUID, USER_CONTROL_POINT_UUID, ScaleModel

_LOGGER = logging.getLogger(__name__)

# Must end inside the wake window: ~47 s after waking, minus the time to notice it.
SESSION_TIMEOUT = 40.0
# A single request/response round trip; GATT ops take ~0.4 s on a BF450.
RESPONSE_TIMEOUT = 8.0
CONNECT_ATTEMPTS = 2
_DISCONNECT_TIMEOUT = 5.0


class GattClient(Protocol):
    """The part of `BleakClient` a session uses."""

    @property
    def is_connected(self) -> bool:
        """Whether the link is up."""
        ...

    async def write_gatt_char(
        self, char_specifier: str, data: bytes, response: bool | None = None
    ) -> None:
        """Write to a characteristic."""
        ...

    async def start_notify(
        self, char_specifier: str, callback: Callable[[Any, bytearray], None]
    ) -> None:
        """Subscribe to notifications or indications of a characteristic."""
        ...

    async def disconnect(self) -> Any:
        """Close the link."""
        ...


@asynccontextmanager
async def open_session(
    device: BLEDevice,
    model: ScaleModel,
    *,
    session_timeout: float | None = None,
    response_timeout: float | None = None,
) -> AsyncIterator["ScaleSession"]:
    """Connect with bonding and yield a session; always disconnect afterwards."""
    if session_timeout is None:
        session_timeout = SESSION_TIMEOUT
    if response_timeout is None:
        response_timeout = RESPONSE_TIMEOUT
    try:
        async with asyncio.timeout(session_timeout):
            client = await _connect(device)
            session = ScaleSession(client, model, response_timeout=response_timeout)
            try:
                yield session
            finally:
                await session.disconnect()
    except TimeoutError as err:
        raise ScaleTimeout("the session ran out of time") from err


async def _connect(device: BLEDevice) -> GattClient:
    try:
        client: GattClient = await establish_connection(
            BleakClientWithServiceCache,
            device,
            device.name or device.address,
            max_attempts=CONNECT_ATTEMPTS,
            # The scale drops unbonded links after ~0.5 s (ADR 0002).
            pair=True,
        )
    except NotImplementedError as err:
        # bleak-esphome raises this for a proxy without pairing support.
        raise PairingUnsupported(str(err)) from err
    except BleakError as err:
        raise ScaleConnectionError(str(err)) from err
    return client


class ScaleSession:
    """Operations on one connected scale."""

    def __init__(
        self,
        client: GattClient,
        model: ScaleModel,
        *,
        response_timeout: float = RESPONSE_TIMEOUT,
    ) -> None:
        """Wrap a connected client."""
        self._client = client
        self._model = model
        self._response_timeout = response_timeout
        self._notifications: dict[str, asyncio.Queue[bytes]] = {}

    async def set_time(self, now: datetime) -> None:
        """Set the scale's clock. Pass local time: the scale has no time zone."""
        frame = bytes(
            [
                *now.year.to_bytes(2, "little"),
                now.month,
                now.day,
                now.hour,
                now.minute,
                now.second,
                0,
                0,
                0,
            ]
        )
        await self._write(CURRENT_TIME_UUID, frame)

    async def read_user_list(self) -> list[UserListEntry]:
        """List every scale user on the scale. Needs no consent."""
        uuid = self._model.user_list_uuid
        queue = await self._subscribe(uuid)
        await self._write(uuid, b"\x00")
        users: list[UserListEntry] = []
        while len(users) < self._model.slot_count:
            match self._decode(decode_user_list_frame, await self._receive(queue)):
                case UserListEnd():
                    break
                case UserListEntry() as entry:
                    users.append(entry)
        return users

    async def consent(self, slot: int, consent_code: int) -> None:
        """Log in as the scale user in `slot`. Raises ConsentRejected."""
        response = await self._ucp(
            UcpOpcode.CONSENT,
            bytes([slot, *consent_code.to_bytes(2, "little")]),
        )
        if response.result is not UcpResult.SUCCESS:
            raise ConsentRejected(f"slot {slot}: {response.result.name}")

    async def disconnect(self) -> None:
        """Close the connection. Never raises."""
        try:
            async with asyncio.timeout(_DISCONNECT_TIMEOUT):
                await self._client.disconnect()
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("Disconnect failed: %s", err)

    async def _ucp(self, opcode: UcpOpcode, parameters: bytes) -> UcpResponse:
        queue = await self._subscribe(USER_CONTROL_POINT_UUID)
        _drain(queue)
        await self._write(USER_CONTROL_POINT_UUID, bytes([opcode, *parameters]))
        while True:
            response = self._decode(decode_ucp_response, await self._receive(queue))
            if response.request is opcode:
                return response
            _LOGGER.debug("Ignoring UCP response %s", response)

    async def _subscribe(self, uuid: str) -> asyncio.Queue[bytes]:
        if (queue := self._notifications.get(uuid)) is not None:
            return queue
        queue = asyncio.Queue()

        def on_notify(_sender: Any, data: bytearray) -> None:
            queue.put_nowait(bytes(data))

        try:
            await self._client.start_notify(uuid, on_notify)
        except BleakError as err:
            raise ScaleConnectionError(f"subscribe {uuid}: {err}") from err
        self._notifications[uuid] = queue
        return queue

    async def _write(self, uuid: str, data: bytes) -> None:
        try:
            await self._client.write_gatt_char(uuid, data, response=True)
        except BleakError as err:
            raise ScaleConnectionError(f"write {uuid}: {err}") from err

    async def _receive(self, queue: asyncio.Queue[bytes]) -> bytes:
        try:
            async with asyncio.timeout(self._response_timeout):
                return await queue.get()
        except TimeoutError as err:
            raise ScaleTimeout("the scale didn't answer") from err

    @staticmethod
    def _decode[T](decoder: Callable[[bytes], T], frame: bytes) -> T:
        try:
            return decoder(frame)
        except FrameError as err:
            raise UnexpectedResponse(str(err)) from err


def _drain(queue: asyncio.Queue[bytes]) -> None:
    with contextlib.suppress(asyncio.QueueEmpty):
        while True:
            queue.get_nowait()

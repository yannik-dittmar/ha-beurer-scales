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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .errors import (
    ConsentRejected,
    NoFreeSlot,
    PairingFailed,
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
from .models import (
    BATTERY_LEVEL_UUID,
    CURRENT_TIME_UUID,
    DATABASE_CHANGE_INCREMENT_UUID,
    DATE_OF_BIRTH_UUID,
    GENDER_UUID,
    HEIGHT_UUID,
    USER_CONTROL_POINT_UUID,
    ScaleModel,
)
from .profile import (
    Profile,
    encode_activity_level,
    encode_birth_date,
    encode_gender,
    encode_height,
)
from .readings import Reading, ReadingAssembler

_LOGGER = logging.getLogger(__name__)

# Must end inside the wake window: ~47 s after waking, minus the time to notice it.
SESSION_TIMEOUT = 40.0
# A single request/response round trip; GATT ops take ~0.4 s on a BF450.
RESPONSE_TIMEOUT = 8.0
CONNECT_ATTEMPTS = 2
# Readings are complete once the scale has sent nothing for this long. Frames of a
# reading arrive ~0.4 s apart, the first ~0.4 s after consent (§9.2, §9.4b).
QUIET_PERIOD = 2.5
_DISCONNECT_TIMEOUT = 5.0
# Bleak has no typed pairing error; these appear in the messages of failed bonds:
# BlueZ's Pair (org.bluez.Error.Authentication*) and habluetooth's kernel client.
# Unverified on real hardware: extend this when a failure shows up unrecognised.
# Matched case-insensitively, so keep them lower case.
PAIRING_FAILURE_MARKERS = (
    "org.bluez.error.authentication",
    "pairing failed",
    "pairing requires",
)


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

    async def read_gatt_char(self, char_specifier: str) -> bytearray:
        """Read a characteristic."""
        ...


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What one sync of a scale user collected."""

    readings: list[Reading]
    battery_percent: int | None


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
        message = str(err)
        if any(marker in message.lower() for marker in PAIRING_FAILURE_MARKERS):
            raise PairingFailed(message) from err
        raise ScaleConnectionError(message) from err
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
        self._readings = ReadingAssembler()
        self._readings_returned = 0
        self._reading_frame = asyncio.Event()
        self._reading_subscriptions: set[str] = set()
        # The reading each FFF4 `01` completed, until teaching takes it.
        self._live_readings: asyncio.Queue[Reading] = asyncio.Queue()

    @property
    def model(self) -> ScaleModel:
        """The model of the connected scale."""
        return self._model

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

    # --- Creating and deleting scale users (§4) ---

    async def register_user(self, consent_code: int) -> int:
        """Register a new scale user with `consent_code`; return its slot.

        Raises NoFreeSlot when the scale refuses, which a full scale does.
        """
        response = await self._ucp(
            UcpOpcode.REGISTER_NEW_USER, consent_code.to_bytes(2, "little")
        )
        if response.result is not UcpResult.SUCCESS or response.slot is None:
            raise NoFreeSlot(f"registration refused: {response.result.name}")
        return response.slot

    async def write_profile(self, profile: Profile) -> None:
        """Write the consented scale user's profile, in the app's order (§4)."""
        await self._write(DATE_OF_BIRTH_UUID, encode_birth_date(profile))
        await self._write(
            DATABASE_CHANGE_INCREMENT_UUID, self._model.database_change_increment
        )
        await self._write(GENDER_UUID, encode_gender(profile))
        await self._write(
            HEIGHT_UUID,
            encode_height(profile, one_byte=self._model.one_byte_height),
        )
        await self._write(
            self._model.activity_level_uuid, encode_activity_level(profile)
        )

    async def delete_user(self) -> None:
        """Delete the consented scale user on the scale (UCP `03`)."""
        response = await self._ucp(UcpOpcode.DELETE_USER_DATA, b"")
        if response.result is not UcpResult.SUCCESS:
            raise UnexpectedResponse(f"delete refused: {response.result.name}")

    # --- Syncing readings (protocol doc §3, §9.4b) ---

    async def sync(
        self,
        slot: int,
        consent_code: int,
        now: datetime,
        *,
        quiet_period: float | None = None,
    ) -> SyncResult:
        """Collect a scale user's readings: set time, subscribe, consent, collect,
        read the battery. Raises ConsentRejected."""
        await self.set_time(now)
        await self.subscribe_readings()
        await self.consent(slot, consent_code)
        readings = await self.collect_readings(quiet_period=quiet_period)
        return SyncResult(readings=readings, battery_percent=await self.read_battery())

    async def drain(
        self, slot: int, consent_code: int, *, quiet_period: float | None = None
    ) -> list[Reading]:
        """Consent another scale user after `sync` and collect its readings.

        Whether the scale allows a second consent in one connection is unverified
        (design §8, V2); a refusal raises ConsentRejected or ScaleConnectionError.
        """
        await self.consent(slot, consent_code)
        return await self.collect_readings(quiet_period=quiet_period)

    async def subscribe_readings(self) -> None:
        """Subscribe to the measurement characteristics. Do this before consent:
        the scale pushes the stored readings right after it (§3)."""
        uuids = list(self._model.reading_uuids)
        if self._model.live_reading_done_uuid:
            uuids.append(self._model.live_reading_done_uuid)
        for uuid in uuids:
            if uuid in self._reading_subscriptions:
                continue

            def on_notify(_sender: Any, data: bytearray, uuid: str = uuid) -> None:
                # FFF4 `01` only says a live reading has finished.
                if uuid != self._model.live_reading_done_uuid:
                    self._readings.add(uuid, bytes(data))
                elif readings := self._readings.readings():
                    self._live_readings.put_nowait(readings[-1])
                self._reading_frame.set()

            try:
                await self._client.start_notify(uuid, on_notify)
            except BleakError as err:
                raise ScaleConnectionError(f"subscribe {uuid}: {err}") from err
            self._reading_subscriptions.add(uuid)

    async def collect_readings(
        self, *, quiet_period: float | None = None
    ) -> list[Reading]:
        """Wait until the scale goes quiet; return the readings since the last call."""
        if quiet_period is None:
            quiet_period = QUIET_PERIOD
        while True:
            self._reading_frame.clear()
            try:
                async with asyncio.timeout(quiet_period):
                    await self._reading_frame.wait()
            except TimeoutError:
                break
        readings = self._readings.readings()
        new, self._readings_returned = (
            readings[self._readings_returned :],
            len(readings),
        )
        return new

    async def wait_for_live_reading(
        self, *, quiet_period: float | None = None
    ) -> Reading:
        """Wait for the next live reading of the consented scale user (teaching).

        Call `subscribe_readings` first. A reading counts once the scale says it is
        complete (FFF4 `01`); readings that finished before this call are returned
        one by one, oldest first. Models without that signal count a reading once
        the scale is quiet. Has no timeout of its own.
        """
        if self._model.live_reading_done_uuid:
            return await self._live_readings.get()
        while True:
            await self._reading_frame.wait()
            if new := await self.collect_readings(quiet_period=quiet_period):
                return new[-1]

    async def read_battery(self) -> int | None:
        """Read the battery level in percent; None if the scale doesn't say."""
        try:
            data = await self._client.read_gatt_char(BATTERY_LEVEL_UUID)
        except BleakError as err:
            raise ScaleConnectionError(f"read battery: {err}") from err
        return data[0] if len(data) == 1 and data[0] <= 100 else None

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
            await self._client.write_gatt_char(
                uuid, data, response=self._model.write_with_response
            )
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

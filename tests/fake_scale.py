"""A fake BF450: the single test seam at the Bluetooth boundary.

`FakeScale.establish_connection` stands in for `bleak_retry_connector.
establish_connection` and returns a `FakeBleakClient` that behaves like a BF450 at
GATT level (protocol doc §9). Nothing here imports Home Assistant, so protocol
tests use it directly; HA tests patch it in (see `conftest.fake_scale`).

The fake keeps its own GATT map and frame encoders, taken from the captures in
§9, so it is an independent source of truth for the code under test.

Implemented so far: waking and sleeping, the required bond, setting the time,
the user list, consent, stored and live readings, the pending-slot list in the
advertisement, the battery level, registering and deleting scale users, profile
writes, injected write failures, and a factory reset with the stale bond it
leaves behind (renewed by unpair + pair).
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakNotFoundError


def _uuid(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


CURRENT_TIME = _uuid(0x2A2B)
USER_CONTROL_POINT = _uuid(0x2A9F)
USER_LIST = _uuid(0xFFF1)
DATE_OF_BIRTH = _uuid(0x2A85)
GENDER = _uuid(0x2A8C)
HEIGHT = _uuid(0x2A8E)
DATABASE_CHANGE_INCREMENT = _uuid(0x2A99)
ACTIVITY_LEVEL = _uuid(0xFFF2)
PROFILE_CHARACTERISTICS = (
    DATE_OF_BIRTH,
    GENDER,
    HEIGHT,
    DATABASE_CHANGE_INCREMENT,
    ACTIVITY_LEVEL,
)
BATTERY_LEVEL = _uuid(0x2A19)
TAKE_MEASUREMENT = _uuid(0xFFF4)

SWAPPED_WEIGHT_SCALE_UUID = _uuid(0x1D18)
SLOT_COUNT = 8

Pairing = Literal["supported", "unsupported"]
# "error": the write fails; "stall": the write never completes.
WriteFailure = Literal["error", "stall"]
NotifyCallback = Callable[[Any, bytearray], None]


@dataclass
class FakeScaleUser:
    """A scale user stored in one slot of the fake scale."""

    consent_code: int
    birth_date: date | None = date(1985, 4, 12)
    height_cm: int = 180
    gender: Literal["male", "female"] = "male"
    activity_level: int = 3
    database_change_increment: int = 0
    # Readings not yet delivered, oldest first; they make the slot pending.
    stored_readings: list["FakeReading"] = field(default_factory=list)

    def user_list_entry(self, slot: int) -> bytes:
        """Encode this scale user as the BF450 lists it (§9.2: no initials)."""
        return bytes(
            [
                0x00,
                slot,
                0xFF,
                0xFF,
                0xFF,
                *(
                    [
                        *self.birth_date.year.to_bytes(2, "little"),
                        self.birth_date.month,
                        self.birth_date.day,
                    ]
                    if self.birth_date
                    else [0xFF, 0xFF, 0xFF, 0xFF]
                ),
                self.height_cm,
                1 if self.gender == "female" else 0,
                self.activity_level,
            ]
        )


@dataclass
class FakeScale:
    """A BF450 with its slots, bond behaviour and wake state."""

    address: str = "AA:BB:CC:DD:EE:01"
    local_name: str = "BF450"
    users: dict[int, FakeScaleUser] = field(default_factory=dict)
    # Whether the Bluetooth path HA connects through can bond at all.
    pairing: Pairing = "supported"
    # A scale that stops answering on the User Control Point.
    answers_ucp: bool = True

    awake: bool = False
    # The last time HA set, as the scale decoded it.
    clock: datetime | None = None
    connection_attempts: int = 0
    # Every User Control Point request written to the scale.
    ucp_requests: list[bytes] = field(default_factory=list)
    battery_level: int = 71
    client: "FakeBleakClient | None" = None
    # Writes to these characteristics misbehave.
    write_failures: dict[str, WriteFailure] = field(default_factory=dict)
    # Whether a write is stuck on a "stall" failure right now.
    stalled: bool = False
    # Slots whose scale user was deleted over a live connection (UCP 03).
    deleted_while_connected: list[int] = field(default_factory=list)
    # After a factory reset the scale has lost its bond keys while the host
    # still holds a bond (§4); unpair + pair renews it.
    bond_stale: bool = False
    # How often a client renewed the bond (unpair, then pair).
    rebonds: int = 0
    # Pairing on an existing connection fails.
    pairing_fails: bool = False

    def add_user(self, slot: int, consent_code: int, **profile: Any) -> FakeScaleUser:
        """Store a scale user in a slot."""
        user = FakeScaleUser(consent_code=consent_code, **profile)
        self.users[slot] = user
        return user

    def factory_reset(self) -> None:
        """Wipe every scale user (FFF7 `00`); the scale forgets its bond (§4)."""
        self.users.clear()
        self.bond_stale = True
        self.awake = False

    def wake(self) -> None:
        """Someone stepped on the scale: it advertises and accepts connections."""
        self.awake = True

    @property
    def connected(self) -> bool:
        """Whether a client currently holds a connection."""
        return self.client is not None and self.client.is_connected

    @property
    def ble_device(self) -> BLEDevice:
        """The BLEDevice a scanner would report for this scale."""
        return BLEDevice(self.address, self.local_name, {})

    def service_data(self) -> dict[str, bytes]:
        """Advertisement service data: the slots with undelivered readings (§2)."""
        pending = [slot for slot, u in sorted(self.users.items()) if u.stored_readings]
        return {SWAPPED_WEIGHT_SCALE_UUID: bytes(pending)}

    def weigh_in(self, slot: int, reading: "FakeReading") -> None:
        """Someone the scale recognises as `slot` weighs in.

        While that scale user is consented the reading is pushed live, followed
        by FFF4 `01` (§9.4); otherwise the scale stores it and lists the slot as
        pending.
        """
        reading = reading.for_slot(slot)
        if self.client is not None and self.client.consented_slot == slot:
            self.client.send_reading(reading)
            self.client.notify(TAKE_MEASUREMENT, b"\x01")
        else:
            self.users[slot].stored_readings.append(reading)

    async def establish_connection(
        self,
        client_class: type[Any],
        device: BLEDevice,
        name: str,
        *args: Any,
        pair: bool = False,
        **kwargs: Any,
    ) -> "FakeBleakClient":
        """Stand-in for `bleak_retry_connector.establish_connection`."""
        self.connection_attempts += 1
        if not self.awake:
            raise BleakNotFoundError(f"{name} - {device.address}: not advertising")
        if pair and self.pairing == "unsupported":
            # An ESPHome proxy without pairing support (bleak-esphome).
            raise NotImplementedError("Pairing is not available in this version")
        self.client = FakeBleakClient(self, bonded=pair)
        return self.client


class FakeBleakClient:
    """The connected end of a FakeScale, shaped like a `BleakClient`."""

    def __init__(self, scale: FakeScale, *, bonded: bool) -> None:
        """Connect; an unbonded link is dropped by the scale (§3)."""
        self._scale = scale
        self._bonded = bonded
        self._connected = True
        self._subscriptions: dict[str, NotifyCallback] = {}
        self.consented_slot: int | None = None
        self._unpaired = False

    @property
    def is_connected(self) -> bool:
        """Whether the link is up."""
        return self._connected

    async def read_gatt_char(self, char_specifier: str, **kwargs: Any) -> bytearray:
        """Read a characteristic."""
        self._check_link()
        uuid = str(char_specifier).lower()
        if uuid == BATTERY_LEVEL:
            return bytearray([self._scale.battery_level])
        raise BleakError(f"Read of unsupported characteristic {uuid}")

    def _check_link(self) -> None:
        if not self._bonded:
            # The BF450 fails the first ATT request and drops an unbonded link.
            self._connected = False
        if not self._connected:
            raise BleakError("Not connected")

    async def start_notify(
        self, char_specifier: str, callback: NotifyCallback, **kwargs: Any
    ) -> None:
        """Subscribe to a characteristic."""
        self._check_link()
        self._subscriptions[str(char_specifier).lower()] = callback

    async def write_gatt_char(
        self, char_specifier: str, data: bytes, response: bool | None = None
    ) -> None:
        """Write to a characteristic and let the scale react."""
        self._check_link()
        uuid = str(char_specifier).lower()
        data = bytes(data)
        await self._misbehave(uuid)
        if uuid == CURRENT_TIME:
            self._scale.clock = datetime(
                int.from_bytes(data[0:2], "little"), *data[2:7]
            )
        elif uuid == USER_LIST and data == b"\x00":
            self._send_user_list()
        elif uuid == USER_CONTROL_POINT:
            self._handle_ucp(data)
        elif uuid in PROFILE_CHARACTERISTICS:
            self._write_profile(uuid, data)
        else:
            raise BleakError(f"Write to unsupported characteristic {uuid}")

    async def unpair(self) -> bool:
        """Remove the host's bond with the scale."""
        self._check_link()
        self._unpaired = True
        return True

    async def pair(self, *args: Any, **kwargs: Any) -> bool:
        """Bond with the scale again."""
        if not self._connected:
            raise BleakError("Not connected")
        if self._scale.pairing_fails:
            raise BleakError("Pairing failed")
        if self._unpaired:
            self._unpaired = False
            self._scale.bond_stale = False
            self._scale.rebonds += 1
        return True

    async def disconnect(self) -> bool:
        """Close the link. The scale goes back to sleep about 1 s later."""
        self._connected = False
        self._scale.awake = False
        return True

    def send_reading(self, reading: "FakeReading") -> None:
        """Push a reading's frames, 2A9D first (§9.2 step 8)."""
        for short_uuid, frame in reading.frames:
            self._notify(_uuid(short_uuid), frame)

    def notify(self, uuid: str, frame: bytes) -> None:
        """Send a notification on a subscribed characteristic."""
        self._notify(uuid, frame)

    def _notify(self, uuid: str, frame: bytes) -> None:
        if callback := self._subscriptions.get(uuid):
            asyncio.get_running_loop().call_soon(
                lambda: self._connected and callback(uuid, bytearray(frame))
            )

    def _send_user_list(self) -> None:
        users = sorted(self._scale.users.items())
        for slot, user in users:
            self._notify(USER_LIST, user.user_list_entry(slot))
        self._notify(USER_LIST, b"\x01" if users else b"\x02")

    def _handle_ucp(self, request: bytes) -> None:
        self._scale.ucp_requests.append(request)
        if not self._scale.answers_ucp:
            return
        opcode = request[0]
        if opcode in (0x01, 0x03):
            self._handle_scale_user_ucp(request)
        elif opcode == 0x02:
            slot = request[1]
            code = int.from_bytes(request[2:4], "little")
            user = self._scale.users.get(slot)
            if user is not None and user.consent_code == code:
                self.consented_slot = slot
                self._notify(USER_CONTROL_POINT, b"\x20\x02\x01")
                # Stored readings follow the consent, oldest first (§9.4b),
                # and the slot is no longer pending.
                for reading in user.stored_readings:
                    self.send_reading(reading)
                user.stored_readings.clear()
            else:
                # A wrong code and a missing slot look the same (§9.4b).
                self._notify(USER_CONTROL_POINT, b"\x20\x02\x04")
        else:
            self._notify(USER_CONTROL_POINT, bytes([0x20, opcode, 0x02]))

    # --- Creating and deleting scale users (§4, §9.2 steps 5-7, §9.4b) ---

    def _handle_scale_user_ucp(self, request: bytes) -> None:
        opcode = request[0]
        if opcode == 0x01:
            free = [s for s in range(1, SLOT_COUNT + 1) if s not in self._scale.users]
            if not free:
                # Assumed: UDS "Operation Failed" for a full database [verify].
                self._notify(USER_CONTROL_POINT, b"\x20\x01\x04")
                return
            code = int.from_bytes(request[1:3], "little")
            # A new scale user has no profile until one is written [verify].
            self._scale.users[free[0]] = FakeScaleUser(
                consent_code=code, birth_date=None, height_cm=0, activity_level=0
            )
            self._notify(USER_CONTROL_POINT, bytes([0x20, 0x01, 0x01, free[0]]))
        elif self.consented_slot in self._scale.users:
            # Delete User Data removes the consented scale user (§9.4b).
            del self._scale.users[self.consented_slot]
            self._scale.deleted_while_connected.append(self.consented_slot)
            self.consented_slot = None
            self._notify(USER_CONTROL_POINT, b"\x20\x03\x01")
        else:
            # UDS "User Not Authorized" [verify].
            self._notify(USER_CONTROL_POINT, b"\x20\x03\x05")

    def _write_profile(self, uuid: str, data: bytes) -> None:
        user = self._scale.users.get(self.consented_slot or 0)
        if user is None:
            # UDS: "User Data Access Not Permitted" before consent.
            raise BleakError(f"Write to {uuid} without consent")
        if uuid == DATE_OF_BIRTH:
            user.birth_date = date(int.from_bytes(data[0:2], "little"), *data[2:4])
        elif uuid == GENDER:
            user.gender = "female" if data[0] == 1 else "male"
        elif uuid == HEIGHT:
            user.height_cm = int.from_bytes(data[0:2], "little")
        elif uuid == DATABASE_CHANGE_INCREMENT:
            user.database_change_increment = int.from_bytes(data, "little")
        elif uuid == ACTIVITY_LEVEL:
            user.activity_level = data[0]

    async def _misbehave(self, uuid: str) -> None:
        failure = self._scale.write_failures.get(uuid)
        if failure == "error":
            raise BleakError(f"Write to {uuid} failed")
        if failure == "stall":
            self._scale.stalled = True
            try:
                await asyncio.get_running_loop().create_future()
            finally:
                self._scale.stalled = False


@dataclass(frozen=True)
class FakeReading:
    """A reading as the scale sends it: frames keyed by short characteristic UUID."""

    frames: tuple[tuple[int, bytes], ...]

    @classmethod
    def from_capture(
        cls, capture: tuple[tuple[int, bytes], ...], *, at: datetime | None = None
    ) -> "FakeReading":
        """A captured reading (tests/frames.py), optionally taken at another time."""
        reading = cls(capture)
        return reading if at is None else reading._with_weight(timestamp=at)

    def for_slot(self, slot: int) -> "FakeReading":
        """The same reading, assigned to `slot`."""
        return self._with_weight(slot=slot)

    def _with_weight(
        self, *, timestamp: datetime | None = None, slot: int | None = None
    ) -> "FakeReading":
        frames = []
        for short_uuid, frame in self.frames:
            if short_uuid == 0x2A9D:
                weight = bytearray(frame)
                if timestamp is not None:
                    weight[3:10] = bytes(
                        [
                            *timestamp.year.to_bytes(2, "little"),
                            timestamp.month,
                            timestamp.day,
                            timestamp.hour,
                            timestamp.minute,
                            timestamp.second,
                        ]
                    )
                if slot is not None:
                    weight[10] = slot
                frame = bytes(weight)
            frames.append((short_uuid, frame))
        return FakeReading(tuple(frames))

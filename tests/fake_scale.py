"""A fake BF450: the single test seam at the Bluetooth boundary.

`FakeScale.establish_connection` stands in for `bleak_retry_connector.
establish_connection` and returns a `FakeBleakClient` that behaves like a BF450 at
GATT level (protocol doc §9). Nothing here imports Home Assistant, so protocol
tests use it directly; HA tests patch it in (see `conftest.fake_scale`).

The fake keeps its own GATT map and frame encoders, taken from the captures in
§9, so it is an independent source of truth for the code under test.

Implemented so far: waking and sleeping, the required bond, setting the time,
the user list, and consent. Extend it as later behaviour needs it (stored and
live readings, register and delete, factory reset, pending-slot adverts).
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

SWAPPED_WEIGHT_SCALE_UUID = _uuid(0x1D18)

Pairing = Literal["supported", "unsupported"]
NotifyCallback = Callable[[Any, bytearray], None]


@dataclass
class FakeScaleUser:
    """A scale user stored in one slot of the fake scale."""

    consent_code: int
    birth_date: date = date(1985, 4, 12)
    height_cm: int = 180
    gender: Literal["male", "female"] = "male"
    activity_level: int = 3

    def user_list_entry(self, slot: int) -> bytes:
        """Encode this scale user as the BF450 lists it (§9.2: no initials)."""
        return bytes(
            [
                0x00,
                slot,
                0xFF,
                0xFF,
                0xFF,
                *self.birth_date.year.to_bytes(2, "little"),
                self.birth_date.month,
                self.birth_date.day,
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
    client: "FakeBleakClient | None" = None

    def add_user(self, slot: int, consent_code: int, **profile: Any) -> FakeScaleUser:
        """Store a scale user in a slot."""
        user = FakeScaleUser(consent_code=consent_code, **profile)
        self.users[slot] = user
        return user

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
        """Advertisement service data: the pending-slot list (none yet)."""
        return {SWAPPED_WEIGHT_SCALE_UUID: b""}

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

    @property
    def is_connected(self) -> bool:
        """Whether the link is up."""
        return self._connected

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
        if uuid == CURRENT_TIME:
            self._scale.clock = datetime(
                int.from_bytes(data[0:2], "little"), *data[2:7]
            )
        elif uuid == USER_LIST and data == b"\x00":
            self._send_user_list()
        elif uuid == USER_CONTROL_POINT:
            self._handle_ucp(data)
        else:
            raise BleakError(f"Write to unsupported characteristic {uuid}")

    async def disconnect(self) -> bool:
        """Close the link. The scale goes back to sleep about 1 s later."""
        self._connected = False
        self._scale.awake = False
        return True

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
        if opcode == 0x02:
            slot = request[1]
            code = int.from_bytes(request[2:4], "little")
            user = self._scale.users.get(slot)
            if user is not None and user.consent_code == code:
                self.consented_slot = slot
                self._notify(USER_CONTROL_POINT, b"\x20\x02\x01")
            else:
                # A wrong code and a missing slot look the same (§9.4b).
                self._notify(USER_CONTROL_POINT, b"\x20\x02\x04")
        else:
            self._notify(USER_CONTROL_POINT, bytes([0x20, opcode, 0x02]))

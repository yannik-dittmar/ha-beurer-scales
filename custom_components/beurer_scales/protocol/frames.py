"""Pure decoders for the frames a BF600-family scale sends (protocol doc §4, §5, §9).

All multi-byte integers are little-endian.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum, IntEnum, StrEnum


class FrameError(ValueError):
    """A frame doesn't have the layout its characteristic promises."""


class Gender(StrEnum):
    """A scale user's gender as the scale stores it."""

    MALE = "male"
    FEMALE = "female"


@dataclass(frozen=True, slots=True)
class UserListEntry:
    """One scale user as listed by the user-list characteristic (no consent needed)."""

    slot: int
    initials: str | None
    birth_date: date | None
    height_cm: int
    gender: Gender
    activity_level: int


class UserListEnd(Enum):
    """The one-byte frame that ends a user list."""

    COMPLETE = 0x01
    EMPTY = 0x02


_USER_LIST_ENTRY_LENGTH = 12
_WIFI_USER_LIST_ENTRY_LENGTH = 13


def decode_user_list_frame(frame: bytes) -> UserListEntry | UserListEnd:
    """Decode one notification of the user-list characteristic (BF450: FFF1)."""
    if len(frame) == 1:
        try:
            return UserListEnd(frame[0])
        except ValueError as err:
            raise FrameError(f"unknown user list marker {frame.hex()}") from err
    if len(frame) not in (_USER_LIST_ENTRY_LENGTH, _WIFI_USER_LIST_ENTRY_LENGTH):
        raise FrameError(f"user list entry of {len(frame)} bytes: {frame.hex()}")
    # Wi-Fi models have one extra byte before the gender (§4).
    gender_at = len(frame) - 2
    raw_initials = frame[2:5]
    initials = (
        raw_initials.decode("ascii").rstrip("\x00 ")
        if all(0x20 <= b < 0x7F or b == 0 for b in raw_initials)
        else None
    )
    return UserListEntry(
        slot=frame[1],
        initials=initials or None,
        birth_date=_date_or_none(
            int.from_bytes(frame[5:7], "little"), frame[7], frame[8]
        ),
        height_cm=frame[9],
        gender=Gender.FEMALE if frame[gender_at] == 1 else Gender.MALE,
        activity_level=frame[gender_at + 1],
    )


def _date_or_none(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


class UcpOpcode(IntEnum):
    """User Control Point (0x2A9F) requests."""

    REGISTER_NEW_USER = 0x01
    CONSENT = 0x02
    DELETE_USER_DATA = 0x03


class UcpResult(IntEnum):
    """UDS response values. The BF450 answers a wrong code, a missing slot and a
    deleted scale user alike with OPERATION_FAILED (§9.4b)."""

    SUCCESS = 0x01
    OP_CODE_NOT_SUPPORTED = 0x02
    INVALID_PARAMETER = 0x03
    OPERATION_FAILED = 0x04
    USER_NOT_AUTHORIZED = 0x05


@dataclass(frozen=True, slots=True)
class UcpResponse:
    """A User Control Point response: `20 <request> <result> [slot]`."""

    request: UcpOpcode
    result: UcpResult
    # Only a successful registration names the new slot.
    slot: int | None = None


_UCP_RESPONSE_CODE = 0x20


def decode_ucp_response(frame: bytes) -> UcpResponse:
    """Decode an indication of the User Control Point."""
    if len(frame) < 3 or frame[0] != _UCP_RESPONSE_CODE:
        raise FrameError(f"not a UCP response: {frame.hex()}")
    try:
        request = UcpOpcode(frame[1])
        result = UcpResult(frame[2])
    except ValueError as err:
        raise FrameError(f"unknown UCP response {frame.hex()}") from err
    slot = (
        frame[3]
        if request is UcpOpcode.REGISTER_NEW_USER
        and result is UcpResult.SUCCESS
        and len(frame) >= 4
        else None
    )
    return UcpResponse(request=request, result=result, slot=slot)


# --- Advertisements and readings (protocol doc §2, §5) ---


def decode_pending_slots(payload: bytes) -> tuple[int, ...]:
    """Decode the 0x1D18 service data: one byte per slot with unsynced readings."""
    return tuple(payload)


# AD type "Service Data - 16-bit UUID", and the UUIDs the pending-slot list
# comes under, little-endian: 0x1D18 (BF450, byte-swapped) and 0x181D.
_SERVICE_DATA_16 = 0x16
_SLOT_LIST_UUIDS = (b"\x18\x1d", b"\x1d\x18")


def decode_slot_list_in_packet(raw: bytes) -> tuple[int, ...] | None:
    """The pending-slot list in a raw advertising packet, or None without one.

    Only the scale's scan response carries the list (protocol doc §9.6).
    """
    i = 0
    while i < len(raw) and (length := raw[i]):
        structure = raw[i + 1 : i + 1 + length]
        if structure[:1] == bytes([_SERVICE_DATA_16]) and (
            structure[1:3] in _SLOT_LIST_UUIDS
        ):
            return decode_pending_slots(structure[3:])
        i += 1 + length
    return None


@dataclass(frozen=True, slots=True)
class WeightFrame:
    """A Weight Measurement (0x2A9D) frame. It always starts a new reading."""

    weight_kg: float
    # The scale's local time; it has no time zone.
    timestamp: datetime | None
    slot: int
    bmi: float
    height_cm: int


_WEIGHT_FRAME_LENGTH = 15


def decode_weight_frame(frame: bytes) -> WeightFrame:
    """Decode 0x2A9D. Always kg, whatever the display unit; the flags are ignored (§5.1, §9.6)."""
    if len(frame) != _WEIGHT_FRAME_LENGTH:
        raise FrameError(f"weight frame of {len(frame)} bytes: {frame.hex()}")
    try:
        timestamp: datetime | None = datetime(
            int.from_bytes(frame[3:5], "little"), *frame[5:10]
        )
    except ValueError:
        timestamp = None
    return WeightFrame(
        weight_kg=round(int.from_bytes(frame[1:3], "little") * 0.005, 3),
        timestamp=timestamp,
        slot=frame[10],
        bmi=round(int.from_bytes(frame[11:13], "little") * 0.1, 1),
        # Raw cm on the BF450 (§9.3).
        height_cm=int.from_bytes(frame[13:15], "little"),
    )

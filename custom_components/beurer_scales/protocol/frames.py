"""Pure decoders for the frames a BF600-family scale sends (protocol doc §4, §5, §9).

All multi-byte integers are little-endian.
"""

from dataclasses import dataclass
from datetime import date
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


def decode_user_list_frame(frame: bytes) -> UserListEntry | UserListEnd:
    """Decode one notification of the user-list characteristic (BF450: FFF1)."""
    if len(frame) == 1:
        try:
            return UserListEnd(frame[0])
        except ValueError as err:
            raise FrameError(f"unknown user list marker {frame.hex()}") from err
    if len(frame) != _USER_LIST_ENTRY_LENGTH:
        raise FrameError(f"user list entry of {len(frame)} bytes: {frame.hex()}")
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
        gender=Gender.FEMALE if frame[10] == 1 else Gender.MALE,
        activity_level=frame[11],
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

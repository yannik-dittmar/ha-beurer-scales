"""Frame decoders, tested on frames captured from a real BF450."""

from datetime import date

import pytest

from custom_components.beurer_scales.protocol.frames import (
    FrameError,
    Gender,
    UcpOpcode,
    UcpResult,
    UserListEnd,
    decode_ucp_response,
    decode_user_list_frame,
)

from . import frames


def test_user_list_entry_holds_the_scale_users_profile() -> None:
    """A 12-byte entry decodes to the slot and its profile."""
    entry = decode_user_list_frame(frames.USER_LIST_ENTRY_SLOT_1)

    assert entry.slot == 1
    assert entry.birth_date == date(1985, 4, 12)
    assert entry.height_cm == 180
    assert entry.gender is Gender.MALE
    assert entry.activity_level == 3
    # The BF450 never stores initials; it reports ff ff ff.
    assert entry.initials is None


def test_user_list_entry_with_gender_byte_1_is_female() -> None:
    """Gender byte 1 means female."""
    frame = bytearray(frames.USER_LIST_ENTRY_SLOT_1)
    frame[10] = 1

    assert decode_user_list_frame(bytes(frame)).gender is Gender.FEMALE


def test_user_list_entry_with_ascii_initials() -> None:
    """Initials, where a scale stores them, are ASCII."""
    frame = bytearray(frames.USER_LIST_ENTRY_SLOT_1)
    frame[2:5] = b"ABC"

    assert decode_user_list_frame(bytes(frame)).initials == "ABC"


def test_user_list_entry_without_a_valid_birth_date() -> None:
    """An unset birth date decodes to None instead of failing."""
    frame = bytearray(frames.USER_LIST_ENTRY_SLOT_1)
    frame[5:9] = b"\xff\xff\xff\xff"

    assert decode_user_list_frame(bytes(frame)).birth_date is None


@pytest.mark.parametrize(
    ("frame", "end"),
    [
        (frames.USER_LIST_COMPLETE, UserListEnd.COMPLETE),
        (frames.USER_LIST_EMPTY, UserListEnd.EMPTY),
    ],
)
def test_user_list_end_markers(frame: bytes, end: UserListEnd) -> None:
    """One-byte frames end the list: 01 after the entries, 02 when there are none."""
    assert decode_user_list_frame(frame) is end


@pytest.mark.parametrize(
    "frame", [b"", b"\x03", frames.USER_LIST_ENTRY_SLOT_1[:-1]], ids=repr
)
def test_malformed_user_list_frames_are_rejected(frame: bytes) -> None:
    """Frames that are neither an end marker nor a 12-byte entry raise FrameError."""
    with pytest.raises(FrameError):
        decode_user_list_frame(frame)


@pytest.mark.parametrize(
    ("frame", "opcode", "result", "slot"),
    [
        (frames.UCP_CONSENT_OK, UcpOpcode.CONSENT, UcpResult.SUCCESS, None),
        (
            frames.UCP_CONSENT_FAILED,
            UcpOpcode.CONSENT,
            UcpResult.OPERATION_FAILED,
            None,
        ),
        (
            frames.UCP_REGISTERED_SLOT_2,
            UcpOpcode.REGISTER_NEW_USER,
            UcpResult.SUCCESS,
            2,
        ),
        (frames.UCP_DELETE_OK, UcpOpcode.DELETE_USER_DATA, UcpResult.SUCCESS, None),
    ],
    ids=["consent-ok", "consent-failed", "registered", "deleted"],
)
def test_ucp_responses(
    frame: bytes, opcode: UcpOpcode, result: UcpResult, slot: int | None
) -> None:
    """UCP responses name the request, its result and, for a registration, the slot."""
    response = decode_ucp_response(frame)

    assert response.request is opcode
    assert response.result is result
    assert response.slot == slot


@pytest.mark.parametrize(
    "frame", [b"", b"\x20\x02", b"\x21\x02\x01", b"\x20\x7f\x01"], ids=repr
)
def test_malformed_ucp_responses_are_rejected(frame: bytes) -> None:
    """A UCP frame that isn't a response to a known request raises FrameError."""
    with pytest.raises(FrameError):
        decode_ucp_response(frame)


def test_wifi_user_list_entry_has_an_extra_byte_before_gender() -> None:
    """Wi-Fi models (BF880/BF980/BF990) send 13-byte entries (§4)."""
    frame = bytes.fromhex("00 03 ff ff ff c1 07 04 0c b4 7f 01 02")

    entry = decode_user_list_frame(frame)

    assert entry.slot == 3
    assert entry.birth_date == date(1985, 4, 12)
    assert entry.height_cm == 180
    assert entry.gender is Gender.FEMALE
    assert entry.activity_level == 2

"""What the user list says about a slot whose consent failed (protocol doc §9.4b)."""

from datetime import date

from custom_components.beurer_scales.protocol.frames import Gender, UserListEntry
from custom_components.beurer_scales.protocol.scale_user import (
    SlotState,
    explain_slot,
)


def entry(slot: int) -> UserListEntry:
    """A user list entry for a slot."""
    return UserListEntry(
        slot=slot,
        initials=None,
        birth_date=date(1985, 4, 12),
        height_cm=180,
        gender=Gender.MALE,
        activity_level=3,
    )


def test_a_listed_slot_is_present() -> None:
    """The slot still holds a scale user: a rejected consent means a wrong code."""
    assert explain_slot(2, [entry(1), entry(2)]) is SlotState.PRESENT


def test_a_slot_missing_from_the_list_was_deleted() -> None:
    """Other scale users remain, this one is gone."""
    assert explain_slot(2, [entry(1), entry(3)]) is SlotState.DELETED


def test_an_empty_list_means_the_scale_was_factory_reset() -> None:
    """A reset wipes every scale user (protocol doc §4)."""
    assert explain_slot(2, []) is SlotState.FACTORY_RESET

"""Creating a scale user on the scale as one all-or-nothing step (protocol doc §4).

A scale user that is registered but not tracked by its client would steal
recognition from the others, so `create_scale_user` deletes it again on any
failure, or cancellation, while still consented and connected (ADR 0003).
"""

import logging
import random
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum

from .errors import NoFreeSlot, ScaleError
from .frames import UserListEntry
from .profile import Profile
from .session import ScaleSession

_LOGGER = logging.getLogger(__name__)

# The app's range (§4); HA picks the code and the administrator never sees it.
CONSENT_CODE_MIN = 1111
CONSENT_CODE_MAX = 9998


@dataclass(frozen=True, slots=True)
class CreatedScaleUser:
    """A scale user that exists on the scale and is consented on this session."""

    slot: int
    consent_code: int


def new_consent_code() -> int:
    """Pick a random consent code."""
    return random.SystemRandom().randint(CONSENT_CODE_MIN, CONSENT_CODE_MAX)


@asynccontextmanager
async def create_scale_user(
    session: ScaleSession, profile: Profile
) -> AsyncIterator[CreatedScaleUser]:
    """Register a scale user, consent and write its profile; yield it.

    The body of the `async with` runs while the new scale user is consented, so
    later steps that need it (teaching) go there. If anything fails, here or in
    the body, the scale user is deleted on the scale before the error propagates.
    Raises NoFreeSlot, without registering, when every slot is taken.
    """
    users = await session.read_user_list()
    if len(users) >= session.model.slot_count:
        raise NoFreeSlot(f"all {session.model.slot_count} slots are taken")

    consent_code = new_consent_code()
    slot = await session.register_user(consent_code)
    # Deleting needs consent; a scale user that couldn't be consented stays behind.
    await session.consent(slot, consent_code)
    try:
        await session.write_profile(profile)
        yield CreatedScaleUser(slot=slot, consent_code=consent_code)
    except BaseException:
        try:
            await session.delete_user()
        except ScaleError as err:
            _LOGGER.warning(
                "Couldn't delete the new scale user in slot %s: %s", slot, err
            )
        raise


class SlotState(Enum):
    """What the user list says about a slot."""

    # The scale user is still there: after a rejected consent, the code is wrong.
    PRESENT = "present"
    # Other scale users remain, but this slot is empty.
    DELETED = "deleted"
    # The list is empty: the scale was factory-reset.
    FACTORY_RESET = "factory_reset"


def explain_slot(slot: int, users: Sequence[UserListEntry]) -> SlotState:
    """Tell why a slot can't be consented, from the user list (§9.4b).

    The scale answers a wrong code, a missing slot and a deleted scale user alike;
    the user list, which needs no consent, tells them apart.
    """
    if not users:
        return SlotState.FACTORY_RESET
    if any(user.slot == slot for user in users):
        return SlotState.PRESENT
    return SlotState.DELETED

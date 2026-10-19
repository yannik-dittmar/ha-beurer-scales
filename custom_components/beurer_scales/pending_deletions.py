"""Scale users removed in HA that still have to be deleted on the scale (design §4.3).

Removing a scale user's subentry takes effect in HA at once; deleting the scale
user on the scale needs a wake. Until then, the slot and consent code are kept
in a `Store`, so the deletion survives a restart.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_STORAGE_VERSION = 1


class _StoredDeletion(TypedDict):
    slot: int
    consent_code: int


class _StoredData(TypedDict):
    deletions: list[_StoredDeletion]


@dataclass(frozen=True, slots=True)
class PendingDeletion:
    """A scale user to delete on the scale: consent with the code, then delete."""

    slot: int
    consent_code: int


def _store(hass: HomeAssistant, entry_id: str) -> Store[_StoredData]:
    return Store(hass, _STORAGE_VERSION, f"{DOMAIN}.{entry_id}.pending_deletions")


class PendingDeletions:
    """The pending deletions of one scale."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """Use the scale entry's store; call `async_load` before reading.

        `on_change` is called after a deletion is added or forgotten.
        """
        self._store = _store(hass, entry_id)
        self._on_change = on_change
        self._deletions: list[PendingDeletion] = []

    async def async_load(self) -> None:
        """Read the stored pending deletions."""
        data: Any = await self._store.async_load()
        self._deletions = [
            PendingDeletion(slot=int(d["slot"]), consent_code=int(d["consent_code"]))
            for d in (data or {}).get("deletions", [])
        ]

    def __bool__(self) -> bool:
        """Whether anything is waiting to be deleted."""
        return bool(self._deletions)

    @property
    def slots(self) -> list[int]:
        """The slots waiting to be deleted, oldest first."""
        return [d.slot for d in self._deletions]

    def all(self) -> list[PendingDeletion]:
        """Every pending deletion, oldest first."""
        return list(self._deletions)

    async def async_add(self, deletion: PendingDeletion) -> None:
        """Remember a scale user to delete; saved before this returns."""
        # A slot holds one scale user; a newer request for it replaces the older.
        self._deletions = [d for d in self._deletions if d.slot != deletion.slot]
        self._deletions.append(deletion)
        await self._async_save()
        self._changed()

    async def async_discard(self, deletion: PendingDeletion) -> None:
        """Forget a deletion: done, or impossible."""
        if deletion in self._deletions:
            self._deletions.remove(deletion)
            await self._async_save()
            self._changed()

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "deletions": [
                    {"slot": d.slot, "consent_code": d.consent_code}
                    for d in self._deletions
                ]
            }
        )


async def async_remove_pending_deletions(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the store of a scale entry that is being removed."""
    await _store(hass, entry_id).async_remove()

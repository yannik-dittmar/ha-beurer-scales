"""The scale coordinator: watches the scale's advertisements and syncs its readings.

One coordinator per config entry (scale). When an advertisement lists a managed
slot as pending, it connects, syncs the newest one first and drains the other
managed pending slots in the same connection (design §5). Every reading,
whether from a sync or handed over by a flow that consented, goes through
`async_process_readings`, the single pipeline to the scale user's entities. The
reading policy sits in that pipeline: only fresh readings reach the entities,
backlog and guessed readings are reported, duplicates are dropped (design §5.4).

Scale users removed in HA are deleted on the scale at the next wake, once that
wake's weigh-in is final (design §4.3): in the same connection as a sync, or
`WEIGH_IN_FINAL_DELAY` after the wake started.
"""

import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_register_advertisement_callback,
)
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .connection import async_open_session
from .discards import async_notify_discarded
from .pending_deletions import PendingDeletions
from .const import CONF_CONSENT_CODE, CONF_MODEL, CONF_SLOT, DOMAIN, SUBENTRY_SCALE_USER
from .protocol.errors import (
    ConsentRejected,
    PairingFailed,
    PairingUnsupported,
    ScaleError,
)
from .protocol.frames import WeightUnit, decode_pending_slots
from .protocol.models import (
    SWAPPED_WEIGHT_SCALE_UUID,
    WEIGHT_SCALE_SERVICE_UUID,
    model_by_name,
)
from .protocol.policy import ReadingPolicy
from .protocol.readings import Reading
from .protocol.session import ScaleSession
from .repairs import (
    async_create_pairing_issue,
    async_create_unit_issue,
    async_delete_pairing_issue,
    async_delete_unit_issue,
)
from .wake import Wake, WakeTracker

_LOGGER = logging.getLogger(__name__)

# After a failed sync, ignore pending slots for this long instead of reconnecting
# at every advertisement (~4 per second) of the same wake.
RETRY_DELAY = 15.0
# A wake that lists only slots left pending by earlier wakes is probably a
# weigh-in still in progress: connecting would cut it short (the scale sleeps ~1 s
# after HA disconnects). Wait this long for its slot to be appended (ADR 0008, V7).
WEIGH_IN_FINAL_DELAY = 25.0

_HANDOVER = "handover"

# The de-duplication log, one store per scale (design §5.4).
READING_LOG_VERSION = 1
READING_LOG_SAVE_DELAY = 10.0


def reading_log_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """The store that keeps a scale's de-duplication log across restarts."""
    return Store(hass, READING_LOG_VERSION, f"{DOMAIN}.{entry_id}.reading_log")


type BeurerConfigEntry = ConfigEntry[ScaleCoordinator]


def pending_slots(service_data: Mapping[str, bytes]) -> tuple[int, ...]:
    """The pending-slot list of an advertisement; the BF450 byte-swaps the UUID."""
    for uuid in (SWAPPED_WEIGHT_SCALE_UUID, WEIGHT_SCALE_SERVICE_UUID):
        if (payload := service_data.get(uuid)) is not None:
            return decode_pending_slots(payload)
    return ()


def sync_order(pending: Sequence[int], leftovers: Collection[int]) -> list[int]:
    """The order to sync managed pending slots in: the newest weigh-in first.

    `leftovers` are the slots HA already saw pending and hasn't synced yet. The
    others just became pending. The scale appends a slot to its list when that
    weigh-in is final (protocol doc §2), so among several of either kind the
    last one listed is taken as the newest. Unverified on hardware (V2): adjust
    here only.
    """
    fresh = [slot for slot in pending if slot not in leftovers]
    rest = [slot for slot in pending if slot in leftovers]
    return [*reversed(fresh), *reversed(rest)]


def subentries_snapshot(entry: ConfigEntry[Any]) -> dict[str, Any]:
    """The scale users' titles and data, to tell whether any changed.

    HA updates a subentry in place, so comparing the objects misses changes.
    """
    return {
        subentry_id: (subentry.title, dict(subentry.data))
        for subentry_id, subentry in entry.subentries.items()
    }


@callback
def async_hand_over_readings(
    hass: HomeAssistant,
    entry_id: str,
    slot: int,
    readings: Iterable[Reading],
    sync_start: datetime | None = None,
) -> None:
    """Keep readings of a slot that has no entities yet for the next setup.

    A flow that consents (adopt) receives the scale user's readings before the
    subentry exists; the reload that adds the subentry then processes them.
    `sync_start` is when the flow connected (default: now), for the backlog rule.
    """
    handover: dict[str, dict[int, list[tuple[datetime, list[Reading]]]]] = (
        hass.data.setdefault(DOMAIN, {}).setdefault(_HANDOVER, {})
    )
    handover.setdefault(entry_id, {}).setdefault(slot, []).append(
        (sync_start or dt_util.now(), list(readings))
    )


class ScaleCoordinator:
    """Syncs one scale and keeps what its entities show."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        """Set up for a scale entry; nothing runs until `async_start`."""
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.model = model_by_name(entry.data[CONF_MODEL])
        self.subentries_at_setup = subentries_snapshot(entry)
        self.battery_percent: int | None = None
        self.last_sync: datetime | None = None
        self._latest: dict[int, Reading] = {}
        self._listeners: list[CALLBACK_TYPE] = []
        self._syncing = False
        self._retry_after = 0.0
        # Managed slots seen pending and not synced yet (see `sync_order`).
        self._leftovers: set[int] = set()
        self._ignored: list[int] = []
        self._due: list[int] = []
        self._device: BLEDevice | None = None
        self._cancel_leftover_sync: CALLBACK_TYPE | None = None
        self._policy = ReadingPolicy()
        self._reading_log = reading_log_store(hass, entry.entry_id)
        self.pending_deletions = PendingDeletions(hass, entry.entry_id)
        self.wakes = WakeTracker(hass, self._async_on_wake)

    async def async_load(self) -> None:
        """Restore the de-duplication log and the pending deletions; call
        before `async_start`."""
        if (stored := await self._reading_log.async_load()) is not None:
            self._policy = ReadingPolicy(tuple(e) for e in stored["readings"])
        await self.pending_deletions.async_load()

    async def async_save(self) -> None:
        """Write the de-duplication log now, e.g. when the scale unloads."""
        await self._reading_log.async_save(self._reading_log_data())

    def _reading_log_data(self) -> dict[str, Any]:
        return {"readings": self._policy.log()}

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Watch every advertisement; process readings handed over by a flow."""
        handover = self.hass.data.get(DOMAIN, {}).get(_HANDOVER, {})
        for slot, batches in handover.pop(self.entry.entry_id, {}).items():
            for sync_start, readings in batches:
                self.async_process_readings(slot, readings, sync_start)
        stop_watching = async_register_advertisement_callback(
            self.hass, self._async_on_advertisement, self.address
        )

        @callback
        def stop() -> None:
            stop_watching()
            self._async_cancel_leftover_sync()
            self.wakes.async_stop()

        return stop

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call `update` whenever a reading or a sync arrived."""
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    def latest_reading(self, slot: int) -> Reading | None:
        """The newest reading of a slot since setup."""
        return self._latest.get(slot)

    @callback
    def async_process_readings(
        self,
        slot: int,
        readings: Iterable[Reading],
        sync_start: datetime | None = None,
    ) -> None:
        """Send a slot's fresh readings, oldest first, to its scale user's entities.

        `sync_start` (aware, default now) is when the sync that collected the
        readings began; readings much older than it are backlog (ADR 0005).
        """
        readings = self._async_kg_only(r for r in readings if r.slot == slot)
        if not readings:
            return
        start = dt_util.as_local(sync_start or dt_util.now()).replace(tzinfo=None)
        result = self._policy.apply(readings, start)
        self._reading_log.async_delay_save(
            self._reading_log_data, READING_LOG_SAVE_DELAY
        )
        if result.discarded:
            subentry = self._managed_slots().get(slot)
            async_notify_discarded(
                self.hass,
                self.address,
                slot,
                subentry.title if subentry else f"slot {slot}",
                result.discarded,
            )
        if not result.fresh:
            return
        self._latest[slot] = result.fresh[-1]
        self._async_update_listeners()

    @callback
    def _async_kg_only(self, readings: Iterable[Reading]) -> list[Reading]:
        """Drop readings in another unit than kg and warn about them.

        v1 never writes scale settings, so the user switches the display back.
        """
        readings = list(readings)
        kg = [r for r in readings if r.unit is WeightUnit.KG]
        if other := [r for r in readings if r.unit is not WeightUnit.KG]:
            _LOGGER.warning(
                "Discarding %s reading(s) of %s in %s; only kg is supported",
                len(other),
                self.address,
                other[-1].unit,
            )
            async_create_unit_issue(self.hass, self.entry, str(other[-1].unit))
        elif kg:
            async_delete_unit_issue(self.hass, self.entry)
        return kg

    def _managed_slots(self) -> dict[int, ConfigSubentry]:
        return {
            int(subentry.data[CONF_SLOT]): subentry
            for subentry in self.entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_SCALE_USER
        }

    @callback
    def _async_on_advertisement(self, info: BluetoothServiceInfoBleak) -> None:
        self.wakes.async_advertisement()
        if self._syncing or self.hass.loop.time() < self._retry_after:
            return
        managed = self._managed_slots()
        pending = pending_slots(info.service_data)
        ignored = [slot for slot in pending if slot not in managed]
        if ignored and ignored != self._ignored:
            _LOGGER.debug("Ignoring pending slots HA doesn't manage: %s", ignored)
        self._ignored = ignored
        self._due = [slot for slot in pending if slot in managed]
        self._device = info.device if info.connectable else None
        if not self._due:
            return
        if any(slot not in self._leftovers for slot in self._due):
            self._async_start_sync()
        elif self._cancel_leftover_sync is None:
            self._cancel_leftover_sync = async_call_later(
                self.hass, WEIGH_IN_FINAL_DELAY, self._async_sync_leftovers
            )

    @callback
    def _async_sync_leftovers(self, _now: datetime) -> None:
        self._cancel_leftover_sync = None
        if not self._syncing:
            self._async_start_sync()

    @callback
    def _async_cancel_leftover_sync(self) -> None:
        if self._cancel_leftover_sync is not None:
            self._cancel_leftover_sync()
            self._cancel_leftover_sync = None

    @callback
    def _async_start_sync(self) -> None:
        """Sync the slots of the last advertisement that are still managed."""
        self._async_cancel_leftover_sync()
        managed = self._managed_slots()
        order = sync_order([s for s in self._due if s in managed], self._leftovers)
        self._leftovers.update(order)
        if not order:
            return
        self._syncing = True
        self.entry.async_create_background_task(
            self.hass,
            self._async_sync([managed[slot] for slot in order], self._device),
            f"{DOMAIN} sync {self.address}",
        )

    async def _async_sync(
        self, subentries: list[ConfigSubentry], device: BLEDevice | None
    ) -> None:
        """Sync the first scale user, then drain the others in the same connection.

        Readings are processed as soon as each slot is collected, so a refused
        second consent (design §8, V2) never loses the first slot's readings.
        Every slot's readings are judged against the time this sync started.
        """
        first, *others = subentries
        slot = int(first.data[CONF_SLOT])
        sync_start = dt_util.now()
        connected = False
        try:
            async with async_open_session(
                self.hass, self.address, self.model, device
            ) as session:
                connected = True
                result = await session.sync(
                    slot, int(first.data[CONF_CONSENT_CODE]), sync_start
                )
                async_delete_pairing_issue(self.hass, self.entry)
                self.battery_percent = result.battery_percent
                self.last_sync = dt_util.utcnow()
                self._async_update_listeners()
                self._leftovers.discard(slot)
                self.async_process_readings(slot, result.readings, sync_start)
                await self._async_drain(session, others, sync_start)
                # The weigh-in is final once its slot is listed as pending.
                await self._async_run_pending_deletions(session)
        except ScaleError as err:
            _LOGGER.warning("Syncing slot %s of %s failed: %r", slot, self.address, err)
            if isinstance(err, PairingUnsupported | PairingFailed):
                async_create_pairing_issue(self.hass, self.entry)
            self._retry_after = self.hass.loop.time() + RETRY_DELAY
        finally:
            self._syncing = False
            if connected:
                # The scale sleeps ~1 s after HA disconnects.
                self.wakes.async_asleep()

    async def _async_drain(
        self,
        session: ScaleSession,
        subentries: list[ConfigSubentry],
        sync_start: datetime,
    ) -> None:
        """Consent and collect further slots until the scale refuses one."""
        for subentry in subentries:
            slot = int(subentry.data[CONF_SLOT])
            try:
                readings = await session.drain(
                    slot, int(subentry.data[CONF_CONSENT_CODE])
                )
            except ScaleError as err:
                # The remaining slots stay pending and are synced at a later wake.
                _LOGGER.debug(
                    "%s refused slot %s in the same connection: %r",
                    self.address,
                    slot,
                    err,
                )
                return
            self._leftovers.discard(slot)
            self.async_process_readings(slot, readings, sync_start)

    @callback
    def _async_update_listeners(self) -> None:
        for update in list(self._listeners):
            update()

    # --- Pending deletions (design §4.3) ---

    @callback
    def _async_on_wake(self, wake: Wake) -> None:
        if self.pending_deletions:
            wake.async_call_at(WEIGH_IN_FINAL_DELAY, self._async_delete_after_weigh_in)

    @callback
    def _async_delete_after_weigh_in(self) -> None:
        # A sync runs the deletions itself, after its slots.
        if self._syncing or not self.pending_deletions:
            return
        if self._due:
            self._async_start_sync()
            return
        self._syncing = True
        self.entry.async_create_background_task(
            self.hass,
            self._async_delete_session(),
            f"{DOMAIN} delete scale users {self.address}",
        )

    async def _async_delete_session(self) -> None:
        connected = False
        try:
            async with async_open_session(
                self.hass, self.address, self.model
            ) as session:
                connected = True
                await self._async_run_pending_deletions(session)
        except ScaleError as err:
            _LOGGER.warning(
                "Connecting to %s to delete scale users failed: %r", self.address, err
            )
        finally:
            self._syncing = False
            if connected:
                self.wakes.async_asleep()

    async def _async_run_pending_deletions(self, session: ScaleSession) -> None:
        """Consent as each removed scale user and delete it. Never raises
        ScaleError: a failure keeps the remaining deletions for a later wake."""
        managed = self._managed_slots()
        for deletion in self.pending_deletions.all():
            if deletion.slot in managed:
                # The slot was adopted again: the scale user is wanted after all.
                await self.pending_deletions.async_discard(deletion)
                continue
            try:
                await session.consent(deletion.slot, deletion.consent_code)
                await session.delete_user()
            except ConsentRejected:
                _LOGGER.debug(
                    "Dropping the deletion of slot %s on %s: consent rejected",
                    deletion.slot,
                    self.address,
                )
                await self.pending_deletions.async_discard(deletion)
                continue
            except ScaleError as err:
                _LOGGER.warning(
                    "Deleting slot %s on %s failed: %r",
                    deletion.slot,
                    self.address,
                    err,
                )
                return
            _LOGGER.debug("Deleted slot %s on %s", deletion.slot, self.address)
            await self.pending_deletions.async_discard(deletion)

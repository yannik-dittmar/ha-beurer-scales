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

A wake that reaches `WEIGH_IN_FINAL_DELAY` without a managed slot pending was an
unrecognised weigh-in: HA reads the user list, which shows a deletion or a
factory reset the scale would never report as pending (ADR 0008, design §5.3),
and may notify about it. The leftover sync, the deletions and this check share
that one moment per wake, so only one of them connects.
"""

import logging
from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Mapping,
    Sequence,
)
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
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .connection import async_hold_scale, async_open_session, async_scale_held
from .discards import async_notify_discarded
from .pending_deletions import PendingDeletions
from .const import (
    CONF_CONSENT_CODE,
    CONF_MODEL,
    CONF_NOTIFY_UNRECOGNISED,
    CONF_SLOT,
    DEFAULT_NOTIFY_UNRECOGNISED,
    DOMAIN,
    SUBENTRY_SCALE_USER,
)
from .protocol.errors import (
    ConsentRejected,
    PairingFailed,
    PairingUnsupported,
    ScaleError,
)
from .protocol.frames import UserListEntry, WeightUnit, decode_pending_slots
from .protocol.models import (
    SWAPPED_WEIGHT_SCALE_UUID,
    WEIGHT_SCALE_SERVICE_UUID,
    model_by_name,
)
from .protocol.policy import ReadingPolicy
from .protocol.readings import Reading
from .protocol.scale_user import SlotState, explain_slot
from .protocol.session import ScaleSession
from .repairs import (
    ScaleUserIssue,
    async_clear_scale_user_issues,
    async_create_pairing_issue,
    async_create_unit_issue,
    async_delete_pairing_issue,
    async_delete_unit_issue,
    async_raise_scale_user_issue,
    async_scale_user_issue,
)
from .unrecognised import async_notify_unrecognised
from .wake import Wake, WakeTracker

_LOGGER = logging.getLogger(__name__)

# After a failed sync, ignore pending slots for this long instead of reconnecting
# at every advertisement (~4 per second) of the same wake.
RETRY_DELAY = 15.0
# A wake that lists only slots left pending by earlier wakes is probably a
# weigh-in still in progress: connecting would cut it short (the scale sleeps ~1 s
# after HA disconnects). Wait this long for its slot to be appended (ADR 0008, V7).
WEIGH_IN_FINAL_DELAY = 25.0
# Slots listed this early in a wake were pending before it; a slot listed later
# is the wake's weigh-in, final ~14 s after stepping on (protocol doc §2).
ALREADY_PENDING_WINDOW = 5.0
# HA's sessions end this long before the scale would fall asleep by itself, so
# work started late in a wake (at `WEIGH_IN_FINAL_DELAY`) still disconnects
# cleanly.
WAKE_END_MARGIN = 2.0

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


def sync_order(
    pending: Sequence[int],
    leftovers: Collection[int],
    refused: Collection[int] = (),
) -> list[int]:
    """The order to sync managed pending slots in: the newest weigh-in first.

    `leftovers` are the slots HA already saw pending and hasn't synced yet. The
    others just became pending. The scale appends a slot to its list when that
    weigh-in is final (protocol doc §2), so among several of either kind the
    last one listed is taken as the newest. `refused` are slots the scale
    refused as a later consent of a connection: they go first, because only a
    connection's first consent tells a wrong code apart. Unverified on hardware
    (V2): adjust here only.
    """
    first = [slot for slot in pending if slot in refused]
    others = [slot for slot in pending if slot not in refused]
    fresh = [slot for slot in others if slot not in leftovers]
    rest = [slot for slot in others if slot in leftovers]
    return [*first, *reversed(fresh), *reversed(rest)]


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
        # Slots (managed, or pending deletion) the scale refused as a later
        # consent of a connection: their next consent must be a connection's
        # first (design §8, V2).
        self._refused_later: set[int] = set()
        self._ignored: list[int] = []
        # Slots pending since before this wake, and whether one was appended.
        self._pending_before_wake: set[int] = set()
        self._recognised = False
        self._due: list[int] = []
        self._device: BLEDevice | None = None
        self._policy = ReadingPolicy(model=self.model)
        self._reading_log = reading_log_store(hass, entry.entry_id)
        self.pending_deletions = PendingDeletions(hass, entry.entry_id)
        self.wakes = WakeTracker(hass, self._async_on_wake)

    async def async_load(self) -> None:
        """Restore the de-duplication log and the pending deletions; call
        before `async_start`."""
        if (stored := await self._reading_log.async_load()) is not None:
            self._policy = ReadingPolicy(
                (tuple(e) for e in stored["readings"]), model=self.model
            )
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
            subentry = self._scale_users().get(slot)
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

    @callback
    def async_check_user_list(
        self, users: Sequence[UserListEntry], rejected_slot: int | None = None
    ) -> bool:
        """Raise repairs for the scale users the user list shows are broken.

        A managed slot missing from the list was deleted on the scale; an empty
        list means a factory reset. `rejected_slot` is a slot whose consent just
        failed: if it is still listed, its consent code is wrong. Returns True
        when a factory reset is newly detected, so the caller renews the bond
        (once, see `async_rebond`) while still connected.
        """
        newly_reset = False
        for slot, subentry in self._scale_users().items():
            match explain_slot(slot, users):
                case SlotState.FACTORY_RESET:
                    kind = ScaleUserIssue.FACTORY_RESET
                case SlotState.DELETED:
                    kind = ScaleUserIssue.SCALE_USER_DELETED
                case SlotState.PRESENT if slot == rejected_slot:
                    kind = ScaleUserIssue.WRONG_CONSENT_CODE
                case _:
                    continue
            if async_scale_user_issue(self.hass, subentry.subentry_id) is kind:
                continue
            _LOGGER.warning(
                "Scale user %s (slot %s) of %s needs a repair: %s",
                subentry.title,
                slot,
                self.address,
                kind,
            )
            newly_reset |= kind is ScaleUserIssue.FACTORY_RESET
            async_raise_scale_user_issue(self.hass, self.entry, subentry, kind)
        return newly_reset

    async def async_rebond(self, session: ScaleSession) -> None:
        """Renew the bond after a factory reset; a failure only logs."""
        try:
            await session.rebond()
        except ScaleError as err:
            _LOGGER.warning("Renewing the bond with %s failed: %r", self.address, err)

    def _scale_users(self) -> dict[int, ConfigSubentry]:
        return {
            int(subentry.data[CONF_SLOT]): subentry
            for subentry in self.entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_SCALE_USER
        }

    def _managed_slots(self) -> dict[int, ConfigSubentry]:
        """The scale users to sync: those without an open repair issue."""
        return {
            slot: subentry
            for slot, subentry in self._scale_users().items()
            if async_scale_user_issue(self.hass, subentry.subentry_id) is None
        }

    @callback
    def _async_on_advertisement(self, info: BluetoothServiceInfoBleak) -> None:
        wake = self.wakes.async_advertisement()
        pending = pending_slots(info.service_data)
        if wake.elapsed < ALREADY_PENDING_WINDOW:
            self._pending_before_wake.update(pending)
        elif any(slot not in self._pending_before_wake for slot in pending):
            self._recognised = True
        if (
            self._syncing
            or self.hass.loop.time() < self._retry_after
            # A flow holding the scale goes first; the slots wait for a wake.
            or async_scale_held(self.hass, self.address)
        ):
            return
        managed = self._managed_slots()
        ignored = [slot for slot in pending if slot not in managed]
        if ignored and ignored != self._ignored:
            _LOGGER.debug("Ignoring pending slots HA doesn't manage: %s", ignored)
        self._ignored = ignored
        self._due = [slot for slot in pending if slot in managed]
        self._device = info.device if info.connectable else None
        if not self._due:
            return
        # Only leftovers listed: the weigh-in may still be in progress, so they
        # wait for `_async_after_weigh_in`.
        if any(slot not in self._leftovers for slot in self._due):
            self._async_start_sync()

    @callback
    def _async_start_sync(self) -> None:
        """Sync the slots of the last advertisement that are still managed."""
        managed = self._managed_slots()
        order = sync_order(
            [s for s in self._due if s in managed],
            self._leftovers,
            self._refused_later,
        )
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
        self._refused_later.discard(slot)

        async def sync(session: ScaleSession) -> None:
            try:
                result = await session.sync(
                    slot, int(first.data[CONF_CONSENT_CODE]), sync_start
                )
            except ConsentRejected:
                # The user list, which needs no consent, tells why (§9.4b).
                users = await session.read_user_list()
                if self.async_check_user_list(users, rejected_slot=slot):
                    await self.async_rebond(session)
                raise
            async_delete_pairing_issue(self.hass, self.entry)
            async_clear_scale_user_issues(self.hass, first.subentry_id)
            self.battery_percent = result.battery_percent
            self.last_sync = dt_util.utcnow()
            self._async_update_listeners()
            self._leftovers.discard(slot)
            self.async_process_readings(slot, result.readings, sync_start)
            await self._async_drain(session, others, sync_start)
            # The weigh-in is final once its slot is listed as pending.
            await self._async_run_pending_deletions(session, consented=True)

        try:
            await self._async_in_session(sync, device)
        except ScaleError as err:
            _LOGGER.warning("Syncing slot %s of %s failed: %r", slot, self.address, err)
            if isinstance(err, PairingUnsupported | PairingFailed):
                async_create_pairing_issue(self.hass, self.entry)
            self._retry_after = self.hass.loop.time() + RETRY_DELAY

    async def _async_in_session(
        self,
        work: Callable[[ScaleSession], Awaitable[None]],
        device: BLEDevice | None = None,
    ) -> None:
        """Hold the scale and run `work` in a session; the scale sleeps after it.

        The session ends before the wake does, however late in it the work
        starts. Leaves the scale alone if a flow holds it: the work waits for a
        later wake. Raises ScaleError.
        """
        connected = False
        wake = self.wakes.current
        deadline = wake.falls_asleep_at - WAKE_END_MARGIN if wake else None
        try:
            if async_scale_held(self.hass, self.address):
                return
            async with (
                async_hold_scale(self.hass, self.address),
                async_open_session(
                    self.hass, self.address, self.model, device, deadline=deadline
                ) as session,
            ):
                connected = True
                await work(session)
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
                # The remaining slots stay pending and are synced at a later
                # wake, this one first: a refusal here says nothing about its code.
                self._refused_later.add(slot)
                _LOGGER.debug(
                    "%s refused slot %s in the same connection: %r",
                    self.address,
                    slot,
                    err,
                )
                return
            self._leftovers.discard(slot)
            async_clear_scale_user_issues(self.hass, subentry.subentry_id)
            self.async_process_readings(slot, readings, sync_start)

    @callback
    def _async_update_listeners(self) -> None:
        for update in list(self._listeners):
            update()

    # --- Once the weigh-in is final (ADR 0008, design §4.3, §5.3) ---

    @callback
    def _async_on_wake(self, wake: Wake) -> None:
        self._pending_before_wake = set()
        self._recognised = False
        # One moment per wake for the leftover sync, the pending deletions and
        # the user-list check, so they share one connection.
        wake.async_call_at(WEIGH_IN_FINAL_DELAY, self._async_after_weigh_in)

    @callback
    def _async_after_weigh_in(self) -> None:
        """The wake's weigh-in is final and HA hasn't connected yet.

        Leftover slots are synced now (the sync also runs the deletions);
        otherwise nobody HA manages was recognised: read the user list and run
        the deletions in one connection.
        """
        if self._syncing or async_scale_held(self.hass, self.address):
            return
        if self._due:
            self._async_start_sync()
            return
        if not self._recognised and self.entry.options.get(
            CONF_NOTIFY_UNRECOGNISED, DEFAULT_NOTIFY_UNRECOGNISED
        ):
            async_notify_unrecognised(self.hass, self.address, self.entry.title)
        if not self._scale_users() and not self.pending_deletions:
            return
        self._syncing = True
        self.entry.async_create_background_task(
            self.hass,
            self._async_check_session(),
            f"{DOMAIN} check {self.address}",
        )

    async def _async_check_session(self) -> None:
        async def check(session: ScaleSession) -> None:
            users = await session.read_user_list()
            if self.async_check_user_list(users):
                await self.async_rebond(session)
            await self._async_run_pending_deletions(session, consented=False)

        try:
            await self._async_in_session(check)
        except ScaleError as err:
            _LOGGER.warning(
                "Checking the scale users of %s failed: %r", self.address, err
            )

    async def _async_run_pending_deletions(
        self, session: ScaleSession, *, consented: bool
    ) -> None:
        """Consent as each removed scale user and delete it. Never raises
        ScaleError: a failure keeps the remaining deletions for a later wake.

        `consented` says whether the session has consented before. Only a
        refused first consent of a connection means a wrong code and drops the
        deletion; a refused later one may just be the scale allowing one
        consent per connection (design §8, V2), so the deletion waits to be
        the first consent of a later connection.
        """
        managed = self._scale_users()
        first = not consented
        deletions = sorted(
            self.pending_deletions.all(),
            key=lambda d: d.slot not in self._refused_later,
        )
        for deletion in deletions:
            if deletion.slot in managed:
                # The slot was adopted again: the scale user is wanted after all.
                await self.pending_deletions.async_discard(deletion)
                continue
            if not first and deletion.slot in self._refused_later:
                continue
            try:
                await session.consent(deletion.slot, deletion.consent_code)
                await session.delete_user()
            except ConsentRejected:
                if not first:
                    _LOGGER.debug(
                        "%s refused deleting slot %s in the same connection",
                        self.address,
                        deletion.slot,
                    )
                    self._refused_later.add(deletion.slot)
                    return
                _LOGGER.debug(
                    "Dropping the deletion of slot %s on %s: consent rejected",
                    deletion.slot,
                    self.address,
                )
                first = False
                self._refused_later.discard(deletion.slot)
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
            first = False
            self._refused_later.discard(deletion.slot)
            await self.pending_deletions.async_discard(deletion)

"""The scale coordinator: watches the scale's advertisements and syncs its readings.

One coordinator per config entry (scale). When the scale appends a managed slot
to its pending-slot list, that weigh-in is final: it connects, syncs that slot
first and drains the other managed pending slots in the same connection (design
§5). The first slot list of a wake shows the slots pending before it; they wait
until the wake's weigh-in is final, since connecting during a weigh-in aborts it
(ADR 0008).

Every reading, whether from a sync or handed over by a flow that consented, goes
through `async_process_readings`, the single pipeline to the scale user's
entities. The reading policy sits in that pipeline: only fresh readings reach
the entities, backlog and guessed readings are reported, duplicates are dropped
(design §5.4).

Scale users removed in HA are deleted on the scale at the next wake, once that
wake's weigh-in is final (design §4.3): in the same connection as a sync, or
`WEIGH_IN_FINAL_DELAY` after the wake started.

A wake without any scan response is not an unrecognised weigh-in: nothing is known
about its pending slots. HA requests an active scan at each wake's start; wake
after wake without a scan response raises a repair issue (#22).

A wake that reaches `WEIGH_IN_FINAL_DELAY` without a managed slot pending was an
unrecognised weigh-in: HA reads the user list, which shows a deletion or a
factory reset the scale would never report as pending (ADR 0008, design §5.3),
and may notify about it. The sync of slots pending before the wake, the
deletions and this check share that one moment per wake, so only one of them
connects.
"""

import logging
from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Sequence,
)
from datetime import datetime
from typing import Any

from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_register_advertisement_callback,
    async_request_active_scan,
)
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .connection import (
    session_ended_at,
    session_ended_signal,
    async_hold_scale,
    async_let_paths_settle,
    async_open_session,
    async_scale_held,
)
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
from .protocol.frames import (
    decode_pending_slots,
    decode_slot_list_in_packet,
)
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
    async_create_deletion_issue,
    async_create_no_scan_response_issue,
    async_create_pairing_issue,
    async_delete_deletion_issue,
    async_delete_no_scan_response_issue,
    async_delete_pairing_issue,
    async_raise_scale_user_issue,
    async_scale_user_issue,
)
from .unrecognised import async_notify_unrecognised
from .wake import Wake, WakeTracker

_LOGGER = logging.getLogger(__name__)

# After a failed sync, ignore pending slots for this long instead of reconnecting
# at every advertisement (~4 per second) of the same wake.
RETRY_DELAY = 15.0
# A wake that lists only slots pending before it is probably a weigh-in still in
# progress: connecting would abort it. The scale appends its slot 4–10 s after
# stepping on; wait this long for that (ADR 0008, V7).
WEIGH_IN_FINAL_DELAY = 25.0

# Wakes with a deletion still pending before a repair issue is raised (#29).
DELETION_FAILING_WAKES = 3
# HA's sessions end this long before the scale would fall asleep by itself, so
# work started late in a wake (at `WEIGH_IN_FINAL_DELAY`) still disconnects
# cleanly.
WAKE_END_MARGIN = 2.0

# The active scan asked for at each wake; habluetooth caps a window at 35 s (#22).
ACTIVE_SCAN_WINDOW = 35.0
# Wakes in a row that reached `WEIGH_IN_FINAL_DELAY` without any scan response
# before a repair issue names the passive scanning path (#22).
NO_SCAN_RESPONSE_WAKES = 3

_HANDOVER = "handover"

# The de-duplication log, one store per scale (design §5.4).
READING_LOG_VERSION = 1
READING_LOG_SAVE_DELAY = 10.0


def reading_log_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """The store that keeps a scale's de-duplication log across restarts."""
    return Store(hass, READING_LOG_VERSION, f"{DOMAIN}.{entry_id}.reading_log")


type BeurerConfigEntry = ConfigEntry[ScaleCoordinator]


def pending_slots(info: BluetoothServiceInfoBleak) -> tuple[int, ...] | None:
    """The pending-slot list an advertisement carries, or None if it has none.

    The BF450 sends the list only in its scan response (protocol doc §9.6). HA
    merges an address's service data, so an advertisement still shows the list
    of an earlier scan response, even one of an earlier wake: only the raw
    packet tells. Without one (a BlueZ adapter) the service data is taken as
    is; the BF450 byte-swaps the UUID.
    """
    if info.raw is not None:
        return decode_slot_list_in_packet(info.raw)
    for uuid in (SWAPPED_WEIGHT_SCALE_UUID, WEIGHT_SCALE_SERVICE_UUID):
        if (payload := info.service_data.get(uuid)) is not None:
            return decode_pending_slots(payload)
    return None


def sync_order(
    pending: Sequence[int],
    fresh: Collection[int],
    refused: Collection[int] = (),
) -> list[int]:
    """The order to sync managed pending slots in: the newest weigh-in first.

    `fresh` are the slots the scale just appended to its list: their weigh-in
    is final now (protocol doc §2). The others were pending before. Among
    several of either kind the last one listed is taken as the newest.
    `refused` are slots the scale refused as a later consent of a connection:
    they go first, because only a connection's first consent tells a wrong code
    apart. Unverified on hardware (V2): adjust here only.
    """
    first = [slot for slot in pending if slot in refused]
    others = [slot for slot in pending if slot not in refused]
    new = [slot for slot in others if slot in fresh]
    rest = [slot for slot in others if slot not in fresh]
    return [*first, *reversed(new), *reversed(rest)]


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
        # Slots (managed, or pending deletion) the scale refused as a later
        # consent of a connection: their next consent must be a connection's
        # first (design §8, V2).
        self._refused_later: set[int] = set()
        # Wakes that began with a deletion pending, since one last ran (#29).
        self._deletion_wakes = 0
        # Wakes in a row without a scan response, as of their weigh-in check (#22).
        self._silent_wakes = 0
        # What this wake's last slot list showed as pending, managed or not.
        self._managed_pending: list[int] = []
        self._unmanaged_pending: list[int] = []
        # The slots this wake's slot lists showed so far (None before the
        # first), and whether the scale appended one (it recognised someone,
        # ADR 0008).
        self._listed_this_wake: set[int] | None = None
        self._slot_appended_this_wake = False
        self._device: BLEDevice | None = None
        # Advertisement contents logged this wake, per path (design §8, V4).
        self._logged_advertisements: set[tuple[Any, ...]] = set()
        self._policy = ReadingPolicy(model=self.model)
        self._reading_log = reading_log_store(hass, entry.entry_id)
        self.pending_deletions = PendingDeletions(
            hass, entry.entry_id, self._async_pending_deletions_changed
        )
        self.wakes = WakeTracker(
            hass,
            self._async_on_wake,
            lambda: session_ended_at(hass, self.address),
        )

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

        # Any HA session ends the wake, not only the coordinator's own: the
        # scale sleeps ~1 s after HA disconnects.
        stop_listening = async_dispatcher_connect(
            self.hass,
            session_ended_signal(self.address),
            self.wakes.async_disconnected,
        )

        @callback
        def stop() -> None:
            stop_watching()
            stop_listening()
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
        readings = [r for r in readings if r.slot == slot]
        if not readings:
            return
        start = dt_util.as_local(sync_start or dt_util.now()).replace(tzinfo=None)
        result = self._policy.apply(readings, start)
        _LOGGER.debug(
            "Slot %s of %s: %s reading(s), %s fresh, %s discarded",
            slot,
            self.address,
            len(readings),
            len(result.fresh),
            len(result.discarded),
        )
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

    async def _async_check_user_list(
        self, session: ScaleSession, rejected_slot: int | None = None
    ) -> None:
        """Read the user list and raise repairs for the scale users it shows
        are broken; renew the bond after a factory reset.

        A managed slot missing from the list was deleted on the scale; an empty
        list means a factory reset. `rejected_slot` is a slot whose consent just
        failed: if it is still listed, its consent code is wrong. The bond is
        renewed once, when the reset is newly detected, while still connected;
        a failure to renew it only logs.
        """
        users = await session.read_user_list()
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
        if not newly_reset:
            return
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
        self._async_log_advertisement(info, wake.elapsed)
        pending = pending_slots(info)
        appended: list[int] = []
        if pending is not None:
            # The first list of a wake shows the slots pending before it, even
            # if it comes late; a slot added later is this wake's weigh-in.
            if self._listed_this_wake is None:
                self._listed_this_wake = set(pending)
            else:
                appended = [s for s in pending if s not in self._listed_this_wake]
                self._listed_this_wake.update(appended)
            self._slot_appended_this_wake |= bool(appended)
            if self._silent_wakes:
                self._silent_wakes = 0
                async_delete_no_scan_response_issue(self.hass, self.entry)
        if (
            self._syncing
            or self.hass.loop.time() < self._retry_after
            # A flow holding the scale goes first; the slots wait for a wake.
            or async_scale_held(self.hass, self.address)
        ):
            return
        self._device = info.device if info.connectable else None
        if pending is None:
            return
        managed = self._managed_slots()
        ignored = [slot for slot in pending if slot not in managed]
        if ignored and ignored != self._unmanaged_pending:
            _LOGGER.debug("Ignoring pending slots HA doesn't manage: %s", ignored)
        self._unmanaged_pending = ignored
        self._managed_pending = [slot for slot in pending if slot in managed]
        # Slots pending before the wake wait for `_async_after_weigh_in`: the
        # weigh-in may still be in progress.
        if any(slot in managed for slot in appended):
            self._async_start_sync(appended)

    @callback
    def _async_log_advertisement(
        self, info: BluetoothServiceInfoBleak, elapsed: float
    ) -> None:
        """Log each advertisement content once per wake and path.

        The raw packet tells an advertisement from a scan response, which shows
        whether passive scanning would see the name and service data (V4).
        """
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        raw = info.raw
        key = (
            info.source,
            info.name,
            tuple(sorted((k, bytes(v)) for k, v in info.service_data.items())),
            tuple(sorted((k, bytes(v)) for k, v in info.manufacturer_data.items())),
            tuple(info.service_uuids),
            info.connectable,
            raw,
        )
        if key in self._logged_advertisements:
            return
        self._logged_advertisements.add(key)
        _LOGGER.debug(
            "Advertisement at %.1f s via %s (RSSI %s, connectable %s): name %r, "
            "service data %s, manufacturer data %s, service UUIDs %s, raw %s",
            elapsed,
            info.source,
            info.rssi,
            info.connectable,
            info.name,
            {uuid: bytes(data).hex() for uuid, data in info.service_data.items()},
            {cid: bytes(data).hex() for cid, data in info.manufacturer_data.items()},
            info.service_uuids,
            raw.hex() if raw else None,
        )

    @callback
    def _async_start_sync(self, fresh: Collection[int] = ()) -> None:
        """Sync the slots of the last slot list that are still managed;
        `fresh` were just appended to it."""
        managed = self._managed_slots()
        order = sync_order(
            [s for s in self._managed_pending if s in managed],
            fresh,
            self._refused_later,
        )
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
        _LOGGER.debug(
            "Syncing slots %s of %s at %s",
            [int(s.data[CONF_SLOT]) for s in subentries],
            self.address,
            self._wake_time(),
        )
        self._refused_later.discard(slot)

        async def sync(session: ScaleSession) -> None:
            try:
                result = await session.sync(
                    slot, int(first.data[CONF_CONSENT_CODE]), sync_start
                )
            except ConsentRejected:
                # The user list, which needs no consent, tells why (§9.4b).
                await self._async_check_user_list(session, rejected_slot=slot)
                raise
            async_delete_pairing_issue(self.hass, self.entry)
            async_clear_scale_user_issues(self.hass, first.subentry_id)
            self.battery_percent = result.battery_percent
            self.last_sync = dt_util.utcnow()
            self._async_update_listeners()
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
        wake = self.wakes.current
        deadline = wake.falls_asleep_at - WAKE_END_MARGIN if wake else None
        try:
            if async_scale_held(self.hass, self.address):
                _LOGGER.debug(
                    "%s is held by a flow; trying at a later wake", self.address
                )
                return
            async with async_hold_scale(self.hass, self.address):
                if wake is not None:
                    await async_let_paths_settle(self.hass, wake.started)
                async with async_open_session(
                    self.hass, self.address, self.model, device, deadline=deadline
                ) as session:
                    await work(session)
        finally:
            # The end of the session ends the wake (see `async_start`).
            self._syncing = False

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
            async_clear_scale_user_issues(self.hass, subentry.subentry_id)
            self.async_process_readings(slot, readings, sync_start)

    def _wake_time(self) -> str:
        """How far into the current wake we are, for the logs."""
        wake = self.wakes.current
        return f"{wake.elapsed:.1f} s into the wake" if wake else "no wake"

    @callback
    def _async_update_listeners(self) -> None:
        for update in list(self._listeners):
            update()

    # --- Once the weigh-in is final (ADR 0008, design §4.3, §5.3) ---

    @callback
    def _async_on_wake(self, wake: Wake) -> None:
        self._logged_advertisements.clear()
        self._listed_this_wake = None
        self._slot_appended_this_wake = False
        self._managed_pending = []
        # The scale lists its pending slots only in its scan response, which a
        # passive path never hears: ask for an active scan for the wake's rest.
        self.entry.async_create_background_task(
            self.hass,
            async_request_active_scan(self.hass, ACTIVE_SCAN_WINDOW),
            f"{DOMAIN} active scan {self.address}",
        )
        if wake.after_disconnect:
            # The scale's tail of advertising after HA's own session: nothing
            # to check (ADR 0008). A slot appended to a later scan response
            # still syncs in `_async_on_advertisement`.
            return
        self._async_count_deletion_wake()
        # One moment per wake for the sync of slots pending before it, the
        # pending deletions and the user-list check, so they share one
        # connection.
        wake.async_call_at(WEIGH_IN_FINAL_DELAY, self._async_after_weigh_in)

    @callback
    def _async_count_deletion_wake(self) -> None:
        """Count a wake that starts with a deletion still pending (#29).

        The `DELETION_FAILING_WAKES`th such wake without the deletion having
        run raises a repair issue names the likely causes.
        """
        if not self.pending_deletions:
            return
        self._deletion_wakes += 1
        if self._deletion_wakes >= DELETION_FAILING_WAKES:
            async_create_deletion_issue(
                self.hass,
                self.entry,
                self.pending_deletions.slots,
                self._deletion_wakes,
            )

    async def async_forget_slots(self) -> None:
        """The scale's slots are gone (factory reset): forget what refers to them.

        Drops the pending deletions and the consent codes of the setup snapshot,
        which the update listener compares against, so removing the scale user
        subentries afterwards queues no deletion.
        """
        for deletion in self.pending_deletions.all():
            await self.pending_deletions.async_discard(deletion)
        self.subentries_at_setup = {
            subentry_id: (
                title,
                {k: v for k, v in data.items() if k != CONF_CONSENT_CODE},
            )
            for subentry_id, (title, data) in self.subentries_at_setup.items()
        }

    @callback
    def _async_pending_deletions_changed(self) -> None:
        """A deletion was added or has run: update the entities and the issue."""
        if not self.pending_deletions:
            self._deletion_wakes = 0
            async_delete_deletion_issue(self.hass, self.entry)
        self._async_update_listeners()

    @callback
    def _async_after_weigh_in(self) -> None:
        """The wake's weigh-in is final and HA hasn't connected yet.

        Slots pending before the wake are synced now (the sync also runs the
        deletions); otherwise nobody HA manages was recognised: read the user
        list and run the deletions in one connection.
        """
        _LOGGER.debug(
            "Weigh-in on %s final at %s: managed pending %s, slot appended %s, "
            "syncing %s",
            self.address,
            self._wake_time(),
            self._managed_pending,
            self._slot_appended_this_wake,
            self._syncing,
        )
        no_scan_response = self._listed_this_wake is None
        if no_scan_response:
            self._async_count_silent_wake()
        if self._syncing or async_scale_held(self.hass, self.address):
            return
        if self._managed_pending:
            self._async_start_sync()
            return
        # Without any scan response HA knows nothing about the pending slots,
        # so nobody was shown to be unrecognised.
        if (
            not no_scan_response
            and not self._slot_appended_this_wake
            and self.entry.options.get(
                CONF_NOTIFY_UNRECOGNISED, DEFAULT_NOTIFY_UNRECOGNISED
            )
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

    @callback
    def _async_count_silent_wake(self) -> None:
        """A wake reached its weigh-in check without any scan response (#22).

        Active scanning was asked for at the wake's start; if wake after wake
        still brings none, the path scans passively and a repair issue says how
        to change that. It clears with the next scan response.
        """
        self._silent_wakes += 1
        _LOGGER.debug(
            "No scan response from %s in %s wake(s) in a row",
            self.address,
            self._silent_wakes,
        )
        if self._silent_wakes >= NO_SCAN_RESPONSE_WAKES:
            async_create_no_scan_response_issue(
                self.hass, self.entry, self._silent_wakes
            )

    async def _async_check_session(self) -> None:
        """Check the user list and run the pending deletions in one session."""

        async def check(session: ScaleSession) -> None:
            await self._async_check_user_list(session)
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
            _LOGGER.info("Deleted slot %s on %s", deletion.slot, self.address)
            first = False
            self._refused_later.discard(deletion.slot)
            await self.pending_deletions.async_discard(deletion)

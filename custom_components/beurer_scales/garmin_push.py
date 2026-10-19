"""Sending a scale user's fresh readings to Garmin Connect (ADR 0011).

A scale user with a Garmin link has its tokens, a queue of readings still to
send and a few bookkeeping values in a private `Store`: never in the subentry,
whose data is world-readable in the config entry file and whose update would
reload the scale. Each fresh reading is queued before it is sent, so a Garmin
outage, a restart or a throttled login loses nothing.

The tokens are all that is kept of the login. When Garmin rejects them, pushing
pauses and a repair issue asks for a new login (reconfigure the link); nothing
ever logs in by itself, since a login is what Garmin throttles and mails MFA
codes for. A throttled or unreachable Garmin is retried with a growing delay.
"""

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CONF_SCALE_USER, DOMAIN, SUBENTRY_GARMIN
from .garmin import (
    GarminAuthError,
    GarminError,
    GarminRateLimited,
    GarminReading,
    PushResult,
    push,
)
from .protocol.metrics import Metric
from .protocol.models import ScaleModel
from .protocol.readings import Reading, reading_metrics

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
SAVE_DELAY = 10
# Queued readings per scale user; the oldest go first when Garmin stays away.
MAX_QUEUE = 100
# After a restart, wait before the first upload so setup stays quick.
START_DELAY = 60
# Seconds to wait after the 1st, 2nd, ... failure in a row; the last repeats.
RETRY_DELAYS = (300, 1800, 7200, 21600)

ISSUE_GARMIN_AUTH = "garmin_auth"


def garmin_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """The private store of an entry's Garmin links."""
    return Store(hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.garmin", private=True)


def _issue_id(scale_user_id: str) -> str:
    return f"{ISSUE_GARMIN_AUTH}_{scale_user_id}"


@dataclass(slots=True)
class _Link:
    """What is kept for one scale user's Garmin link."""

    tokens: str
    paused: bool = False
    last_sent: datetime | None = None
    queue: list[dict[str, Any]] = field(default_factory=list)
    failures: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    retry: CALLBACK_TYPE | None = None

    def as_data(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens,
            "paused": self.paused,
            "last_sent": self.last_sent.isoformat() if self.last_sent else None,
            "queue": self.queue,
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "_Link":
        last_sent = data.get("last_sent")
        return cls(
            tokens=data["tokens"],
            paused=bool(data.get("paused")),
            last_sent=datetime.fromisoformat(last_sent) if last_sent else None,
            queue=list(data.get("queue", [])),
        )


def _queued(reading: GarminReading) -> dict[str, Any]:
    return {
        "timestamp": reading.timestamp.isoformat(),
        "weight_kg": reading.weight_kg,
        "metrics": {str(metric): value for metric, value in reading.metrics.items()},
    }


def _unqueued(item: dict[str, Any]) -> GarminReading:
    return GarminReading(
        timestamp=datetime.fromisoformat(item["timestamp"]),
        weight_kg=item["weight_kg"],
        metrics={Metric(key): value for key, value in item["metrics"].items()},
    )


def garmin_reading(reading: Reading, model: ScaleModel) -> GarminReading:
    """A reading as the upload takes it.

    The scale's clock has no time zone but runs on HA's local time (set at every
    sync), so the zone is HA's. A reading without a time was taken just now.
    """
    timestamp = (
        reading.timestamp.replace(tzinfo=dt_util.get_default_time_zone())
        if reading.timestamp
        else dt_util.now()
    )
    return GarminReading(
        timestamp=timestamp,
        weight_kg=reading.weight_kg,
        metrics=reading_metrics(reading, model),
    )


class GarminPusher:
    """The Garmin links of one scale and their upload queues."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        """Set up for a scale entry; nothing runs until `async_start`."""
        self.hass = hass
        self.entry = entry
        self._store = garmin_store(hass, entry.entry_id)
        # By the scale user's subentry id.
        self._links: dict[str, _Link] = {}

    def _linked_scale_users(self) -> set[str]:
        return {
            subentry.data[CONF_SCALE_USER]
            for subentry in self.entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_GARMIN
        }

    def _data(self) -> dict[str, Any]:
        return {id_: link.as_data() for id_, link in self._links.items()}

    async def async_load(self) -> None:
        """Restore the links of the scale users that still have one."""
        stored = await self._store.async_load() or {}
        linked = self._linked_scale_users()
        for scale_user_id, data in stored.items():
            if scale_user_id in linked:
                self._links[scale_user_id] = _Link.from_data(data)
            else:
                # The link or the scale user was removed.
                ir.async_delete_issue(self.hass, DOMAIN, _issue_id(scale_user_id))
        for scale_user_id, link in self._links.items():
            if link.paused:
                self._async_create_issue(scale_user_id)
        if self._links.keys() != stored.keys():
            self._store.async_delay_save(self._data, SAVE_DELAY)

    async def async_save(self) -> None:
        """Write the links now, e.g. when the scale unloads."""
        await self._store.async_save(self._data())

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Send what a restart left in the queues, after a pause."""
        cancels = [
            async_call_later(self.hass, START_DELAY, self._async_start_flush(id_))
            for id_, link in self._links.items()
            if link.queue and not link.paused
        ]

        @callback
        def stop() -> None:
            for cancel in cancels:
                cancel()
            for link in self._links.values():
                if link.retry:
                    link.retry()
                    link.retry = None

        return stop

    def _async_start_flush(self, scale_user_id: str) -> Any:
        @callback
        def start(_now: datetime) -> None:
            self._async_flush_soon(scale_user_id)

        return start

    def has_link(self, scale_user_id: str) -> bool:
        """Whether a scale user sends its readings to Garmin."""
        return scale_user_id in self._links

    def is_paused(self, scale_user_id: str) -> bool:
        """Whether Garmin rejected the tokens, so nothing is sent until a new login."""
        return (link := self._links.get(scale_user_id)) is not None and link.paused

    def pending(self, scale_user_id: str) -> int:
        """How many readings wait to be sent."""
        return len(link.queue) if (link := self._links.get(scale_user_id)) else 0

    @callback
    def async_enqueue(
        self, scale_user_id: str, readings: Iterable[Reading], model: ScaleModel
    ) -> None:
        """Queue a scale user's fresh readings, oldest first, and send them."""
        if (link := self._links.get(scale_user_id)) is None:
            return
        queued = {item["timestamp"] for item in link.queue}
        for reading in readings:
            garmin = garmin_reading(reading, model)
            if link.last_sent and garmin.timestamp <= link.last_sent:
                continue
            item = _queued(garmin)
            if item["timestamp"] not in queued:
                link.queue.append(item)
                queued.add(item["timestamp"])
        del link.queue[:-MAX_QUEUE]
        self.entry.async_create_background_task(
            self.hass,
            self._async_keep_and_flush(scale_user_id, link),
            f"{DOMAIN} garmin queue {scale_user_id}",
        )

    async def _async_keep_and_flush(self, scale_user_id: str, link: _Link) -> None:
        """Write the queue first, so a crash can't lose a reading, then send."""
        await self.async_save()
        # A retry timer holds the next attempt back after a failure.
        if not link.paused and link.retry is None:
            await self._async_flush(scale_user_id)

    async def async_set_tokens(self, scale_user_id: str, tokens: str) -> None:
        """Start, or after a new login resume, a scale user's link."""
        if link := self._links.get(scale_user_id):
            link.tokens = tokens
            link.paused = False
            link.failures = 0
        else:
            self._links[scale_user_id] = _Link(tokens=tokens)
        ir.async_delete_issue(self.hass, DOMAIN, _issue_id(scale_user_id))
        await self.async_save()
        if self._links[scale_user_id].queue:
            self._async_flush_soon(scale_user_id)

    async def async_remove(self) -> None:
        """Forget every link and its tokens, when the scale is removed."""
        for scale_user_id in self._links:
            ir.async_delete_issue(self.hass, DOMAIN, _issue_id(scale_user_id))
        self._links.clear()
        await self._store.async_remove()

    @callback
    def _async_flush_soon(self, scale_user_id: str) -> None:
        link = self._links[scale_user_id]
        if link.retry:
            link.retry()
            link.retry = None
        self.entry.async_create_background_task(
            self.hass,
            self._async_flush(scale_user_id),
            f"{DOMAIN} garmin push {scale_user_id}",
        )

    async def _async_flush(self, scale_user_id: str) -> None:
        """Send the queue in one login; one flush per scale user at a time."""
        link = self._links.get(scale_user_id)
        if link is None:
            return
        async with link.lock:
            if link.paused or not link.queue:
                return
            batch = list(link.queue)
            result: PushResult = await self.hass.async_add_executor_job(
                push, link.tokens, [_unqueued(item) for item in batch]
            )
            self._async_apply(scale_user_id, link, batch, result)
            await self.async_save()

    @callback
    def _async_apply(
        self,
        scale_user_id: str,
        link: _Link,
        batch: list[dict[str, Any]],
        result: PushResult,
    ) -> None:
        done = result.sent + result.rejected
        if result.sent:
            link.last_sent = max(
                link.last_sent or datetime.min.replace(tzinfo=dt_util.UTC),
                *(datetime.fromisoformat(item["timestamp"]) for item in batch[:done]),
            )
        if result.rejected:
            _LOGGER.warning(
                "Garmin refused %s reading(s) of %s; dropped them",
                result.rejected,
                self._name(scale_user_id),
            )
        del link.queue[:done]
        if result.tokens:
            link.tokens = result.tokens
        if result.error is None:
            link.failures = 0
            return
        self._async_handle_error(scale_user_id, link, result.error)

    @callback
    def _async_handle_error(
        self, scale_user_id: str, link: _Link, error: GarminError
    ) -> None:
        name = self._name(scale_user_id)
        if isinstance(error, GarminAuthError):
            link.paused = True
            _LOGGER.warning(
                "Garmin Connect rejected the login of %s; pushing is paused: %s",
                name,
                error,
            )
            self._async_create_issue(scale_user_id)
            return
        link.failures += 1
        delay = RETRY_DELAYS[min(link.failures, len(RETRY_DELAYS)) - 1]
        (_LOGGER.warning if link.failures == 1 else _LOGGER.debug)(
            "Garmin Connect is %s for %s; trying again in %s: %s",
            "throttling" if isinstance(error, GarminRateLimited) else "unavailable",
            name,
            timedelta(seconds=delay),
            error,
        )

        @callback
        def retry(_now: datetime) -> None:
            link.retry = None
            self._async_flush_soon(scale_user_id)

        link.retry = async_call_later(self.hass, delay, retry)

    def _name(self, scale_user_id: str) -> str:
        subentry = self.entry.subentries.get(scale_user_id)
        return subentry.title if subentry else scale_user_id

    @callback
    def _async_create_issue(self, scale_user_id: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            _issue_id(scale_user_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_GARMIN_AUTH,
            translation_placeholders={"name": self._name(scale_user_id)},
        )

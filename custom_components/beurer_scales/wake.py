"""Grouping the scale's advertisements into wakes.

The scale advertises only while awake: from someone stepping on it until about
47 s later, or until about 1 s after HA disconnects. A wake starts at the first
advertisement after the scale slept and ends when HA's connection put it back
to sleep or when it stops advertising. Work that must wait for a point in the
wake (a weigh-in is final 4–10 s after waking; ADR 0008 checks at ~25 s) is
scheduled relative to the wake's start and dropped if the wake ends first.

After HA disconnects the scale advertises again ~1.5 s later and goes on for
0-20 s (protocol doc §9.6). Advertisements that start a wake within
`AFTER_DISCONNECT_WINDOW` of HA's disconnect, whoever held the session (a sync
or a flow), continue that session: the wake is marked `after_disconnect` and
the coordinator schedules none of the ADR 0008 work for it.
"""

import asyncio
import logging
from collections.abc import Callable

from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)

# Without an advertisement for this long the scale is asleep again. A BF450
# advertises several times a second while awake.
WAKE_GAP = 10.0
# Unless a connection keeps it awake, the scale sleeps this long after waking.
WAKE_WINDOW = 47.0
# A wake starting this soon after HA disconnected is the scale's tail of
# advertising after that session, not someone stepping on.
AFTER_DISCONNECT_WINDOW = 5.0


class Wake:
    """One wake of the scale."""

    def __init__(self, hass: HomeAssistant, after_disconnect: bool = False) -> None:
        """Start a wake now."""
        self._hass = hass
        self.after_disconnect = after_disconnect
        self.started = hass.loop.time()
        self.last_advertisement = self.started
        self._timers: set[asyncio.TimerHandle] = set()

    @property
    def falls_asleep_at(self) -> float:
        """When the scale goes to sleep by itself, in event loop time."""
        return self.started + WAKE_WINDOW

    @property
    def elapsed(self) -> float:
        """Seconds since the wake started."""
        return self._hass.loop.time() - self.started

    @callback
    def async_call_at(
        self, seconds_since_start: float, action: Callable[[], None]
    ) -> None:
        """Run `action` once the wake is that old, unless it has ended by then."""

        def run() -> None:
            self._timers.discard(timer)
            action()

        timer = self._hass.loop.call_at(self.started + seconds_since_start, run)
        self._timers.add(timer)

    @callback
    def _async_end(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()


class WakeTracker:
    """Follows the scale's wakes from its advertisements."""

    def __init__(
        self,
        hass: HomeAssistant,
        on_wake: Callable[[Wake], None],
        last_disconnect: Callable[[], float | None] = lambda: None,
    ) -> None:
        """Call `on_wake` with each new wake as it starts.

        `last_disconnect` tells when HA's last session with the scale ended (event
        loop time); it outlives the tracker.
        """
        self._hass = hass
        self._last_disconnect = last_disconnect
        self._on_wake = on_wake
        self._wake: Wake | None = None
        self._gap_timer: asyncio.TimerHandle | None = None

    @property
    def current(self) -> Wake | None:
        """The wake in progress, if the scale is awake."""
        return self._wake

    @callback
    def async_advertisement(self) -> Wake:
        """Note an advertisement of the scale; return the wake it belongs to."""
        if self._gap_timer is not None:
            self._gap_timer.cancel()
        self._gap_timer = self._hass.loop.call_later(
            WAKE_GAP, self.async_asleep, "it stopped advertising"
        )
        if self._wake is None:
            disconnected_at = self._last_disconnect()
            after_disconnect = (
                disconnected_at is not None
                and self._hass.loop.time() - disconnected_at <= AFTER_DISCONNECT_WINDOW
            )
            _LOGGER.debug(
                "The scale woke up%s",
                " (still advertising after HA disconnected)"
                if after_disconnect
                else "",
            )
            self._wake = Wake(self._hass, after_disconnect)
            self._on_wake(self._wake)
        self._wake.last_advertisement = self._hass.loop.time()
        return self._wake

    @callback
    def async_asleep(self, reason: str) -> None:
        """The scale went to sleep; end the wake."""
        if self._gap_timer is not None:
            self._gap_timer.cancel()
            self._gap_timer = None
        if self._wake is not None:
            _LOGGER.debug(
                "Wake ended at %.1f s (%s); last advertisement at %.1f s",
                self._wake.elapsed,
                reason,
                self._wake.last_advertisement - self._wake.started,
            )
            self._wake._async_end()
            self._wake = None

    @callback
    def async_disconnected(self) -> None:
        """An HA session with the scale ended: it sleeps soon; end the wake."""
        self.async_asleep("HA disconnected")

    @callback
    def async_stop(self) -> None:
        """Stop following wakes."""
        self.async_asleep("HA stopped watching")

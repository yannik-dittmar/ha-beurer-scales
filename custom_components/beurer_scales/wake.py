"""Grouping the scale's advertisements into wakes.

The scale advertises only while awake: from someone stepping on it until about
47 s later, or until about 1 s after HA disconnects. A wake starts at the first
advertisement after the scale slept and ends when HA's connection put it back
to sleep or when it stops advertising. Work that must wait for a point in the
wake (a weigh-in is final ~14 s after waking; ADR 0008 checks at ~25 s) is
scheduled relative to the wake's start and dropped if the wake ends first.
"""

import asyncio
from collections.abc import Callable

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

# Without an advertisement for this long the scale is asleep again. A BF450
# advertises several times a second while awake.
WAKE_GAP = 10.0


class Wake:
    """One wake of the scale."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Start a wake now."""
        self._hass = hass
        self.started = hass.loop.time()
        self.ended = False
        self._timers: set[asyncio.TimerHandle] = set()

    @property
    def elapsed(self) -> float:
        """Seconds since the wake started."""
        return self._hass.loop.time() - self.started

    @callback
    def async_call_at(
        self, seconds_since_start: float, action: Callable[[], None]
    ) -> CALLBACK_TYPE:
        """Run `action` once the wake is that old, unless it has ended by then."""

        def run() -> None:
            self._timers.discard(timer)
            action()

        timer = self._hass.loop.call_at(self.started + seconds_since_start, run)
        self._timers.add(timer)

        def cancel() -> None:
            timer.cancel()
            self._timers.discard(timer)

        return cancel

    @callback
    def _async_end(self) -> None:
        self.ended = True
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()


class WakeTracker:
    """Follows the scale's wakes from its advertisements."""

    def __init__(self, hass: HomeAssistant, on_wake: Callable[[Wake], None]) -> None:
        """Call `on_wake` with each new wake as it starts."""
        self._hass = hass
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
        self._gap_timer = self._hass.loop.call_later(WAKE_GAP, self.async_asleep)
        if self._wake is None:
            self._wake = Wake(self._hass)
            self._on_wake(self._wake)
        return self._wake

    @callback
    def async_asleep(self) -> None:
        """The scale went to sleep, e.g. HA disconnected; end the wake."""
        if self._gap_timer is not None:
            self._gap_timer.cancel()
            self._gap_timer = None
        if self._wake is not None:
            self._wake._async_end()
            self._wake = None

    @callback
    def async_stop(self) -> None:
        """Stop following wakes."""
        self.async_asleep()

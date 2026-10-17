"""Teaching: taking live readings so the scale learns a scale user's weight.

Right after it is created, the scale doesn't recognise a scale user yet. While
that scale user is consented, every weigh-in arrives as a live reading and the
scale learns their weight from it (CONTEXT.md, design §4.1 step 4).
"""

import asyncio

from .errors import TeachingTimeout
from .readings import Reading
from .session import ScaleSession

# How many readings teaching asks for; one is enough to finish.
TEACHING_READINGS = 3
# How long teaching waits for each reading: stepping off, back on and standing
# still until the reading is final (~14 s) takes a while.
TEACHING_TIMEOUT = 90.0


class Teaching:
    """Teaching one consented scale user, observable while it runs."""

    def __init__(self, readings_wanted: int = TEACHING_READINGS) -> None:
        """Prepare to take up to `readings_wanted` readings."""
        self.readings_wanted = readings_wanted
        self._readings: list[Reading] = []
        self._changed = asyncio.Event()
        self._finish = asyncio.Event()
        self.timed_out = False
        self.ended = False

    @property
    def readings(self) -> list[Reading]:
        """The readings taken so far, oldest first."""
        return list(self._readings)

    def finish(self) -> None:
        """Stop teaching before all readings were taken."""
        self._finish.set()

    async def wait_for_readings(self, count: int) -> None:
        """Return once `count` readings were taken or teaching ended."""
        while len(self._readings) < count and not self.ended:
            self._changed.clear()
            await self._changed.wait()

    async def run(self, session: ScaleSession, *, timeout: float | None = None) -> None:
        """Take live readings until enough were taken, `finish` or a timeout.

        The session must be subscribed to readings and consented as the scale user.
        `timeout` applies to each reading. A timeout after at least one reading
        just ends teaching (`timed_out`); before the first one it raises
        TeachingTimeout.
        """
        if timeout is None:
            timeout = TEACHING_TIMEOUT
        try:
            while len(self._readings) < self.readings_wanted:
                reading = asyncio.ensure_future(session.wait_for_live_reading())
                finish = asyncio.ensure_future(self._finish.wait())
                try:
                    done, _ = await asyncio.wait(
                        {reading, finish},
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    reading.cancel()
                    finish.cancel()
                if reading in done:
                    self._readings.append(reading.result())
                    self._changed.set()
                elif finish in done:
                    return
                elif self._readings:
                    self.timed_out = True
                    return
                else:
                    raise TeachingTimeout("no reading while teaching")
        finally:
            self.ended = True
            self._changed.set()

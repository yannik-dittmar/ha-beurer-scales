"""Teaching: taking the one live reading that makes the scale learn a scale user.

Right after it is created, the scale doesn't recognise a scale user yet. While
that scale user is consented, a weigh-in arrives as a live reading and the scale
learns their weight from it. One reading is enough (ADR 0010, CONTEXT.md,
design §4.1 step 4).
"""

import asyncio

from .errors import TeachingTimeout
from .readings import Reading
from .session import ScaleSession

# How long teaching waits for the reading. The person stands on the scale from
# the step-on that woke it, so the reading is final within about 14 s of that;
# connecting and creating the scale user come first.
TEACHING_TIMEOUT = 90.0


class Teaching:
    """Teaching one consented scale user, observable while it runs."""

    def __init__(self) -> None:
        """Prepare to take one reading."""
        self.reading: Reading | None = None

    @property
    def readings(self) -> list[Reading]:
        """The readings taken: none, or the one teaching reading."""
        return [] if self.reading is None else [self.reading]

    async def run(self, session: ScaleSession, *, timeout: float | None = None) -> None:
        """Take the live reading of the consented scale user.

        The session must be subscribed to readings and consented as the scale user.
        Raises TeachingTimeout if no reading is final within `timeout`, and
        ConnectionLost if the link breaks first.
        """
        if timeout is None:
            timeout = TEACHING_TIMEOUT
        try:
            async with asyncio.timeout(timeout):
                self.reading = await session.wait_for_live_reading()
        except TimeoutError as err:
            raise TeachingTimeout("no reading while teaching") from err

"""The reading policy: which readings reach a scale user's entities.

A reading is fresh, a backlog reading (ADR 0005) or a guessed reading
(ADR 0006); only fresh readings are emitted. A reading seen before (same slot,
timestamp and weight) is dropped silently, whatever it was the first time, so
a re-delivery after an interrupted sync neither updates the entities twice nor
reports a discard twice.
"""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from .frames import FrameError
from .metrics import decode_custom1_frame
from .models import CUSTOM1_UUID, FrameKind, ScaleModel
from .readings import Reading

# Readings older than this, measured back from the sync start, are backlog.
BACKLOG_AGE = timedelta(minutes=5)
# How many readings the de-duplication log remembers.
LOG_SIZE = 100

# One log entry: slot, ISO timestamp, weight in kg.
type LogEntry = tuple[int, str, float]


class Discard(StrEnum):
    """Why a reading never reaches the entities."""

    BACKLOG = "backlog"
    GUESSED = "guessed"


@dataclass(frozen=True, slots=True)
class Discarded:
    """A reading the policy discarded, and why."""

    reading: Reading
    reason: Discard


@dataclass(slots=True)
class PolicyResult:
    """The readings of one sync, sorted by the policy; both lists oldest first."""

    fresh: list[Reading] = field(default_factory=list)
    discarded: list[Discarded] = field(default_factory=list)


def guessed(reading: Reading, model: ScaleModel) -> bool:
    """Whether the scale only guessed the slot (custom1 byte 4 != 0, §9.4).

    Only models whose FFF5 frame is custom1 flag a guess; FFF5 is a heart-rate
    frame on others (§5.4). A reading without a custom1 frame, or with a
    malformed one, counts as not guessed.
    """
    if (CUSTOM1_UUID, FrameKind.CUSTOM1) not in model.reading_frames:
        return False
    if (frame := reading.frames.get(CUSTOM1_UUID)) is None:
        return False
    try:
        return decode_custom1_frame(frame).guessed
    except FrameError:
        return False


def classify(
    reading: Reading, sync_start: datetime, model: ScaleModel
) -> Discard | None:
    """Why to discard a reading, or None if it is fresh.

    `sync_start` is on the scale's clock: local time without a time zone. A
    reading without a timestamp can't be dated and counts as fresh.
    """
    if reading.timestamp is not None and reading.timestamp < sync_start - BACKLOG_AGE:
        return Discard.BACKLOG
    if guessed(reading, model):
        return Discard.GUESSED
    return None


class ReadingPolicy:
    """Sorts each sync's readings and remembers the recent ones."""

    def __init__(
        self,
        log: Iterable[LogEntry] = (),
        *,
        model: ScaleModel,
        max_log: int = LOG_SIZE,
    ):
        """Start from a log restored from storage, or an empty one, for a
        scale of `model`."""
        self._model = model
        self._log: deque[LogEntry] = deque(
            (_entry(*entry) for entry in log), maxlen=max_log
        )

    def apply(self, readings: Iterable[Reading], sync_start: datetime) -> PolicyResult:
        """Sort a sync's readings into fresh and discarded, dropping duplicates."""
        result = PolicyResult()
        for reading in readings:
            # Without a timestamp, two readings of the same weight can't be
            # told apart; such a reading is never treated as a duplicate.
            if reading.timestamp is not None:
                key = _entry(
                    reading.slot, reading.timestamp.isoformat(), reading.weight_kg
                )
                if key in self._log:
                    continue
                self._log.append(key)
            if (reason := classify(reading, sync_start, self._model)) is None:
                result.fresh.append(reading)
            else:
                result.discarded.append(Discarded(reading, reason))
        return result

    def log(self) -> list[LogEntry]:
        """The log as plain data, oldest first, for storage."""
        return list(self._log)


def _entry(slot: int, timestamp: str, weight_kg: float) -> LogEntry:
    return (int(slot), str(timestamp), round(float(weight_kg), 3))

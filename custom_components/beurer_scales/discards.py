"""Telling the household which readings the reading policy discarded.

Every sync that discards readings logs a warning and creates one persistent
notification per scale user, giving the count, the time range and the reason
(design §5.4).
"""

import logging
from collections.abc import Sequence
from datetime import datetime

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.ulid import ulid_now

from .const import DOMAIN, scale_user_id
from .protocol.policy import Discard, Discarded

_LOGGER = logging.getLogger(__name__)

_REASONS = {
    Discard.BACKLOG: (
        "backlog",
        "taken while Home Assistant wasn't syncing the scale, for example while "
        "it was down or out of range. Home Assistant can't record them at the "
        "time they were taken.",
    ),
    Discard.GUESSED: (
        "guessed",
        "the scale wasn't sure who stepped on it (the display showed U--), so "
        "they may belong to someone else.",
    ),
}


@callback
def async_notify_discarded(
    hass: HomeAssistant,
    address: str,
    slot: int,
    name: str,
    discarded: Sequence[Discarded],
) -> None:
    """Warn about one sync's discarded readings of one scale user."""
    if not discarded:
        return
    lines = []
    for reason, (label, explanation) in _REASONS.items():
        readings = [d.reading for d in discarded if d.reason is reason]
        if not readings:
            continue
        count = f"{len(readings)} {label} reading{'s' if len(readings) > 1 else ''}"
        span = _time_range([r.timestamp for r in readings])
        _LOGGER.warning(
            "Discarded %s of %s (slot %s of %s, %s)", count, name, slot, address, span
        )
        lines.append(f"- **{count}** ({span}): {explanation}")
    persistent_notification.async_create(
        hass,
        f"Home Assistant discarded readings of {name}:\n\n" + "\n".join(lines),
        title=f"Readings of {name} discarded",
        notification_id=(
            f"{DOMAIN}_{scale_user_id(address, slot)}_discarded_{ulid_now()}"
        ),
    )


def _time_range(timestamps: Sequence[datetime | None]) -> str:
    """The span of the readings on the scale's clock, e.g. 2026-10-16 07:12–07:15."""
    known = sorted(t for t in timestamps if t is not None)
    if not known:
        return "time unknown"
    first, last = known[0], known[-1]
    start = first.strftime("%Y-%m-%d %H:%M")
    if last.strftime("%Y-%m-%d %H:%M") == start:
        return start
    end_format = "%H:%M" if last.date() == first.date() else "%Y-%m-%d %H:%M"
    return f"{start}–{last.strftime(end_format)}"

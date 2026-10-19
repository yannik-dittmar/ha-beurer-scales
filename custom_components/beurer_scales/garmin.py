"""The blocking half of the Garmin Connect push: login, MFA and upload (ADR 0011).

`garminconnect` is synchronous and slow (a login sleeps on purpose, to get past
Garmin's bot checks), so every function here runs in the executor. The library
is imported inside them: it loads a native extension (`curl_cffi`), which must
not happen in the event loop.

Only the tokens persist, never the password: it is used once, at setup, and a
Garmin login is never retried in the background (research, §6).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .protocol.metrics import Metric

_LOGGER = logging.getLogger(__name__)

MFA_REQUIRED = "needs_mfa"


class GarminError(Exception):
    """A Garmin Connect call failed."""


class GarminAuthError(GarminError):
    """Garmin rejected the credentials, the MFA code or the stored tokens."""


class GarminRateLimited(GarminError):
    """Garmin throttled the call (429); the tokens are fine, try again later."""


class GarminUnavailable(GarminError):
    """Garmin could not be reached or failed; try again later."""


class GarminRejectedReading(GarminError):
    """A reading can't be uploaded, whatever Garmin's state: drop it."""


@dataclass(frozen=True, slots=True)
class GarminReading:
    """What one upload carries; `timestamp` is aware."""

    timestamp: datetime
    weight_kg: float
    metrics: dict[Metric, float] = field(default_factory=dict)


def body_composition_args(reading: GarminReading) -> dict[str, Any]:
    """The `add_body_composition` arguments of a reading.

    Garmin's FIT weight scale message knows fewer values than the scale
    measures; the others (lean body mass, protein, ...) have no field and are
    left out. The mass of the muscles is in kg there, so the percentage is
    converted when the scale reports only that. The visceral fat is the scale's
    index, which Garmin keeps as a rating (its `visceral_fat_mass` is kg).
    """
    metrics = reading.metrics
    muscle_kg = metrics.get(Metric.TOTAL_MUSCLE_MASS)
    if muscle_kg is None and (percent := metrics.get(Metric.MUSCLE)) is not None:
        muscle_kg = round(reading.weight_kg * percent / 100, 1)
    values: dict[str, float | None] = {
        "percent_fat": metrics.get(Metric.BODY_FAT),
        "percent_hydration": metrics.get(Metric.BODY_WATER),
        "bone_mass": metrics.get(Metric.BONE_MASS),
        "muscle_mass": muscle_kg,
        "basal_met": metrics.get(Metric.BMR),
        "active_met": metrics.get(Metric.AMR),
        "metabolic_age": metrics.get(Metric.METABOLIC_AGE),
        "visceral_fat_rating": metrics.get(Metric.VISCERAL_FAT),
        "bmi": metrics.get(Metric.BMI),
    }
    return {
        "timestamp": reading.timestamp.isoformat(),
        "weight": reading.weight_kg,
        **{key: value for key, value in values.items() if value is not None},
    }


@dataclass(slots=True)
class PendingLogin:
    """A login that waits for the MFA code; the library keeps the state."""

    client: Any


@dataclass(slots=True)
class PushResult:
    """How far an upload run got.

    `sent` readings went up (the first ones of the batch). `error` is what
    stopped the run, or None; a reading Garmin can never take is skipped and
    counted in `rejected`. `tokens` are the tokens to keep.
    """

    sent: int = 0
    rejected: int = 0
    tokens: str | None = None
    error: GarminError | None = None


def _library() -> Any:
    import garminconnect  # noqa: PLC0415

    return garminconnect


def _translate(err: Exception) -> GarminError:
    """Map the library's exceptions to ours."""
    lib = _library()
    if isinstance(err, GarminError):
        return err
    if isinstance(err, lib.GarminConnectAuthenticationError):
        return GarminAuthError(str(err))
    if isinstance(err, lib.GarminConnectTooManyRequestsError):
        return GarminRateLimited(str(err))
    if isinstance(err, ValueError):
        return GarminRejectedReading(str(err))
    return GarminUnavailable(f"{type(err).__name__}: {err}")


def login(email: str, password: str) -> tuple[PendingLogin | None, str | None]:
    """Log in with the credentials; blocking.

    Returns `(pending, None)` when Garmin wants an MFA code, else `(None,
    tokens)`.
    """
    lib = _library()
    client = lib.Garmin(email, password, return_on_mfa=True)
    try:
        status, _ = client.login()
    except Exception as err:
        raise _translate(err) from err
    if status == MFA_REQUIRED:
        return PendingLogin(client), None
    return None, client.client.dumps()


def finish_mfa(pending: PendingLogin, code: str) -> str:
    """Finish a login with the MFA code and return the tokens; blocking.

    A wrong code leaves the login pending: the caller may try again.
    """
    try:
        pending.client.resume_login(None, code)
    except Exception as err:
        raise _translate(err) from err
    return pending.client.client.dumps()  # type: ignore[no-any-return]


def push(tokens: str, readings: list[GarminReading]) -> PushResult:
    """Upload readings in order with the stored tokens; blocking.

    One login serves the whole batch, and the run stops at the first failure
    that would hit the next reading too.
    """
    result = PushResult(tokens=tokens)
    try:
        client = _library().Garmin()
        client.login(tokenstore=tokens)
    except Exception as err:
        result.error = _translate(err)
        return result
    try:
        for reading in readings:
            try:
                client.add_body_composition(**body_composition_args(reading))
            except Exception as err:
                error = _translate(err)
                if not isinstance(error, GarminRejectedReading):
                    result.error = error
                    break
                _LOGGER.debug("Garmin refused a reading: %s", error)
                result.rejected += 1
            else:
                result.sent += 1
    finally:
        # The refresh token rotates: keep the newest.
        result.tokens = client.client.dumps()
    return result

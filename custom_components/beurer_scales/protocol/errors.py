"""Typed failures of a scale session. Callers decide what each one means to the user."""


class ScaleError(Exception):
    """Base class for everything a scale session can fail with."""


class ScaleConnectionError(ScaleError):
    """The scale couldn't be reached, or it dropped the connection."""


class PairingUnsupported(ScaleConnectionError):
    """The Bluetooth path (adapter or proxy) can't bond, and the scale requires it."""


class PairingFailed(ScaleConnectionError):
    """The Bluetooth path tried to bond with the scale and failed.

    For example a local BlueZ adapter without a pairing agent (ADR 0002).
    """


class ScaleTimeout(ScaleError):
    """The scale didn't answer in time, or the session ran out of its time budget."""


class ConsentRejected(ScaleError):
    """The scale refused consent: wrong code, missing slot or deleted scale user.

    The scale gives the same answer for all three; read the user list to tell them
    apart.
    """


class UnexpectedResponse(ScaleError):
    """The scale answered with a frame the session can't make sense of."""

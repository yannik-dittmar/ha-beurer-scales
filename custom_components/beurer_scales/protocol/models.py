"""Supported scale models, identified by their advertised local name."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScaleModel:
    """One Beurer scale model."""

    name: str
    verified: bool


MODELS: tuple[ScaleModel, ...] = (ScaleModel(name="BF450", verified=True),)


def model_from_local_name(local_name: str | None) -> ScaleModel | None:
    """Return the model a scale advertises with this local name, if supported."""
    if not local_name:
        return None
    for model in MODELS:
        if local_name.startswith(model.name):
            return model
    return None

"""Constants for the Beurer Scales integration."""

DOMAIN = "beurer_scales"
MANUFACTURER = "Beurer"

CONF_MODEL = "model"

# Scale options.
CONF_NOTIFY_UNRECOGNISED = "notify_unrecognised"
DEFAULT_NOTIFY_UNRECOGNISED = True

# A config subentry per scale user (ADR 0004).
SUBENTRY_SCALE_USER = "scale_user"

# Subentry flow sources of the repair fix flows (#12), with context["subentry_id"].
SOURCE_REPAIR_ADOPT = "repair_adopt"
SOURCE_REPAIR_CREATE = "repair_create"

# Scale user subentry data.
CONF_SLOT = "slot"
CONF_CONSENT_CODE = "consent_code"
CONF_NAME = "name"
# The linked person's entity registry id, or None.
CONF_PERSON = "person"
CONF_PROFILE = "profile"
# Keys of CONF_PROFILE.
PROFILE_BIRTH_DATE = "birth_date"  # ISO date
PROFILE_HEIGHT_CM = "height_cm"
PROFILE_GENDER = "gender"  # "male" / "female"
PROFILE_ACTIVITY_LEVEL = "activity_level"  # 1-5


def slot_unique_id(slot: int) -> str:
    """Return the unique id of the scale user subentry for a slot."""
    return f"slot_{slot}"


def scale_user_id(address: str, slot: int | str) -> str:
    """Return the id of a scale user's device, which prefixes its entities' ids."""
    return f"{address}_slot_{slot}"

"""Frames captured from a real BF450 (protocol doc §9, anonymised values)."""

# User list (FFF1), §9.2 step 4: slot 1, no initials, born 1985-04-12, 180 cm,
# male, activity 3.
USER_LIST_ENTRY_SLOT_1 = bytes.fromhex("00 01 ff ff ff c1 07 04 0c b4 00 03")
USER_LIST_COMPLETE = bytes.fromhex("01")
USER_LIST_EMPTY = bytes.fromhex("02")

# User Control Point (2A9F) responses, §9.2 and §9.4b.
UCP_REGISTERED_SLOT_2 = bytes.fromhex("20 01 01 02")
UCP_CONSENT_OK = bytes.fromhex("20 02 01")
UCP_CONSENT_FAILED = bytes.fromhex("20 02 04")
UCP_DELETE_OK = bytes.fromhex("20 03 01")

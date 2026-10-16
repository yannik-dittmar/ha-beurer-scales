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

# Readings, §9.3 and §9.4b. Each reading is a group of frames as the BF450 sends
# them, keyed by the characteristic's short UUID.

# Weigh-in with take-measurement, slot 2: 78.4 kg at 2024-03-10 07:12:30.
READING_SLOT_2 = (
    (0x2A9D, bytes.fromhex("02 40 3d e8 07 03 0a 07 0c 1e 02 f2 00 b4 00")),
    (0x2A9C, bytes.fromhex("98 03 b8 00 c0 1c 9c 01 5e 02 cb 01 00 14")),
    (0xFFF5, bytes.fromhex("0e 37 20 2b 00")),
    (0xFFF6, bytes.fromhex("07 00 22 fa 31 40 0b ac 21 08 a1 00 9e 02 49 02")),
)
# The FFF4 notification that ends a live reading.
LIVE_READING_DONE = bytes.fromhex("01")

# Two stored readings of slot 1, delivered on consent oldest first (§9.4b):
# 77.9 kg at 2024-03-10 07:54:00, then 77.9 kg at 07:58:34.
STORED_READING_SLOT_1_A = (
    (0x2A9D, bytes.fromhex("02 dc 3c e8 07 03 0a 07 36 00 01 f0 00 b4 00")),
    (0x2A9C, bytes.fromhex("98 03 b7 00 b0 1c 9a 01 5b 02 c8 01 dc 13")),
    (0xFFF5, bytes.fromhex("0e 37 08 2b 00")),
    (0xFFF6, bytes.fromhex("07 00 21 b8 31 34 0b 88 21 08 a0 00 9e 02 4a 02")),
)
STORED_READING_SLOT_1_B = (
    (0x2A9D, bytes.fromhex("02 dc 3c e8 07 03 0a 07 3a 22 01 f0 00 b4 00")),
    (0x2A9C, bytes.fromhex("98 03 b7 00 b0 1c 9a 01 5b 02 c8 01 02 14")),
    (0xFFF5, bytes.fromhex("0e 37 08 2b 00")),
    (0xFFF6, bytes.fromhex("07 00 21 b8 31 34 0b 88 21 08 a0 00 9e 02 4a 02")),
)

# Offline weigh-in of slot 2: 78.2 kg at 2024-03-10 07:17:09 (§9.3).
OFFLINE_WEIGHT_SLOT_2 = bytes.fromhex("02 18 3d e8 07 03 0a 07 11 09 02 f1 00 b4 00")

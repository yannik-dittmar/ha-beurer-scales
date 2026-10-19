# Beurer scale BLE protocol (reverse engineered)

Source: Beurer HealthManager Pro Android app `com.beurer.healthmanager` 1.22.0
(versionCode 2026080603), `base.apk` sha256
`dca2439855201799d469499039c53a42621763652ab2076456afcab1e77ae177`.
Static analysis (JADX 0.7.1 decompilation via REA), then verified on a real **BF450**
on 2026-10-15/16 with BlueZ + bleak 3.0.2 (see §9). Items still marked **[verify]** have
not been observed yet. Where the scale and the app code disagree, §9 wins.

Kotlin source file names survive R8 (`/* compiled from: Xyz.kt */`), which is how the
obfuscated classes below were named. Class references are `obfuscated.Class` →
original `SourceFile.kt`.

All 16-bit UUIDs use the Bluetooth SIG base `0000xxxx-0000-1000-8000-00805f9b34fb`
(`lk.a` → `BTUUIDHelperImpl.kt`, `br.a` → `AdvertisementServiceUuidProviderImpl.kt`).
All multi-byte integers are little-endian unless noted.

---

## 1. Supported scales and protocol families

The app knows 19 scales (`entities/device/Scale.kt` sealed interface). They split into
two protocol families:

| Family | Models | Basis |
|---|---|---|
| **"BF600" (UDS-based)** | BF105, BF1000, BF405, **BF450**, BF451, BF500, BF600, BF720, BF722, BF850, BF880, BF915, BF950, BF980, BF990, GS435, SRBF1 | Bluetooth SIG Weight Scale (0x181D), Body Composition (0x181B), User Data Service (0x181C) + Beurer custom service |
| **"BF700" (proprietary)** | BF700, BF710 | Single service 0xFFE0 / char 0xFFE1, `0xF7`/`0xE7`-prefixed command protocol (same family as Sanitas SBF70) |

Model capabilities are expressed as ~90 empty marker interfaces in package `jp.*`
(names stripped). The ones that matter were resolved by usage; see §7.

### Per-model GATT map (BF600 family)

"Custom svc" is `0xFFFF` for models with marker `bi`, else `0xFFF0`.
Rules from `vi.f` (`ScaleGeneralServiceConfigurationImpl.kt`) and `vi.g`
(`ScaleMeasurementsServiceConfigurationImpl.kt`).

| Model | Advertised svc | Custom svc | User list | Settings | Activity | Initials | Take meas. | Guest meas. | Measurement listener |
|---|---|---|---|---|---|---|---|---|---|
| BF105 | FFFF | FFFF | 0001 | 0000 | 0004 | 0002 | 0006 | 0007 | 2A9D+2A9C + 0008 (upper/lower) |
| BF1000 | FFFF | FFFF | 0001 | 0000 | 0004 | 0002 | 0006 | 0007 | 2A9D+2A9C + 0008/0009/000A (segmental) |
| BF405 | FFFF | FFFF | 0001 | 0000 | 0004 | 0002 | 0006 | 0007 | 2A9D+2A9C |
| **BF450** | **181D** | **FFFF** | **FFF1** | **FFF0** | **FFF2** | **FFF6** | **FFF4** | **0007** | **2A9D+2A9C + FFF5 + FFF6** |
| BF451 | 181D | FFFF | FFF1 | FFF0 | FFF2 | FFF6 | FFF4 | 0007 | same as BF450 |
| BF500 | FFFF | FFFF | FFF1 | FFF0 | FFF2 | FFF6 | FFF4 | 0007 | 2A9D+2A9C + FFF5 (pulse etc.) |
| BF600 | 181D | FFF0 | FFF2 | FFF1 | FFF3 | FFF6 | FFF4 | 0007 | 2A9D+2A9C |
| BF720 / BF722 | FFFF | FFFF | 0001 | 0000 | 0004 | 0002 | 0006 | 0007 | 2A9D+2A9C |
| BF850 | FFF0 | FFF0 | FFF2 | FFF1 | FFF3 | FFF6 | FFF4 | 0007 | 2A9D+2A9C |
| BF880 (wifi) | FFFF | FFFF | FFF1 | FFF0 | FFF2 | 0002 | FFF4 | 0007 | 2A9D+2A9C + FFF5 |
| BF915 / BF950 | FFFF | FFFF | 0001 | 0000 | 0004 | 0002 | 0006 | 0007 | 2A9D+2A9C |
| BF980 (wifi) | FFFF | FFFF | FFF1 | FFF0 | FFF2 | 0002 | FFF4 | 0007 | 2A9D+2A9C + FFF5 |
| BF990 (wifi) | FFFF | FFFF | FFF1 | FFF0 | FFF2 | 0002 | FFF4 | FFF9 | 2A9D+2A9C + FFF5/FFF7/FFF8 (20-byte frames) |
| GS435 | 181D | FFFF | 0001 | 0000 | FFF3 | FFF6 | 0006 | 0007 | 2A9D only (weight-only scale) |
| SRBF1 | FFF0 | FFF0 | FFF2 | FFF1 | FFF3 | FFF6 | FFF4 | 0007 | 2A9D+2A9C |

Standard characteristics common to the family:

| Purpose | Service | Char |
|---|---|---|
| Weight Measurement | 0x181D | 0x2A9D (notify/indicate) |
| Body Composition Measurement | 0x181B | 0x2A9C (notify/indicate) |
| User Control Point (UCP) | 0x181C | 0x2A9F (write + indicate) |
| User Index | 0x181C | 0x2A9A (read) |
| Database Change Increment | 0x181C | 0x2A99 (write) |
| Date of Birth | 0x181C | 0x2A85 |
| Gender | 0x181C | 0x2A8C |
| Height | 0x181C | 0x2A8E |
| Current Time | 0x1805 | 0x2A2B |
| Battery Level | 0x180F | 0x2A19 |
| Unique user identifier (37 B) | custom | 0xFFFA |
| Target weight | custom | 0x0003 |
| Pregnancy/tare mode | custom | 0x000C |
| Wi-Fi config / OTA (wifi models) | 0xFFEF | 0xFFE0–0xFFE5 |
| Display settings (BF990) | 0xFFCF | 0xFFC0/0xFFC1 |

---

## 2. Discovery and "is there new data?" detection

### Identifying the scale
- `fk.c` (`DeviceSearchExtenstions.kt`): the device type is chosen **by advertised
  local name** (`DeviceType.fromDiscoverName`). If the OS gives no name, the app parses
  AD type `0x09` (Complete Local Name) out of the raw scan record itself.
- Names: `BF450`, `BF451`, `BF600`, …; BF700 accepts `BF700` or `Beurer BF700`; BF710
  advertises `Beurer BF710`.
- Discover service UUIDs (table above) are used for scan filtering during pairing.
- Once paired, background scanning (`ik.b`, `KableBackgroundDeviceSearch.kt`) filters
  on the stored MAC (`qk.b`, `BTDeviceSearchFilter.kt`). The scan restarts every 4 min.
- Fallback MAC match: `ck.a` (`BTScaleMissingDataHelper.kt`) reads the manufacturer
  data AD (type `0xFF`), takes the following 11 bytes, and uses bytes 4..9 **reversed**
  as the MAC. This covers scales whose advertised address differs from the paired one.
  **[verify]** which models do this. The BF450 does **not**: its manufacturer data is
  company `0x1106`, payload `00 01 00` (3 bytes, no MAC), and it advertises its public
  address directly.

### "New measurement pending" flag in the advertisement (key for HA)
`uj.z` (`BTDeviceSyncLogic.kt`, log `"Check if user has new measurements"`):

- The scale advertises **Service Data** (AD type `0x16`) for UUID **0x181D**.
  **Observed on BF450:** the UUID is byte-swapped in the advert, i.e. it shows up as
  **`0x1D18`** (`0000 1d18-…`) in BlueZ/bleak, both in the service-UUID list and as the
  service-data key. A matcher must accept both `181d` and `1d18`.
- The payload bytes after the UUID are a list of **user slots (1..10) that have
  unsynced measurements**.
- The app connects only if its user's slot is in that list (unless the connection is
  whitelisted from a settings screen). Otherwise it logs
  `"user id/slot N has no new measurements, no need to connect"`.

**For HA:** a passive BLE listener can see "slot N has new data" without connecting, then
connect only when needed.

**Observed on BF450:** payload = one byte per pending slot, ascending, no length/header
byte. Examples: `` (nothing pending), `01`, `01 02`. Timeline of an offline weigh-in:
wake → first advert sometimes has no service data at all or an empty payload → ~0.5 s
later the existing pending list (`01`) → when the new reading is final (~14 s after
stepping on on BlueZ, 4–10 s through a proxy, §9.6) the slot is appended (`01 02`). The
list is only in the scan response (§9.6). Slots stay listed until that user consents
and receives the data; readings that were already delivered are not sent again.

No weight values are sent in advertisements. A connection is always needed for data.

---

## 3. Connection sequence (BF600 family, background sync)

Reconstructed from `tj.a0` (`ScaleBackgroundSyncServiceImpl.kt`), `zj.u0`
(`ScaleInitialLoginUseCaseImpl.kt`), `zj.h3` (`ScaleUserLoginUseCaseImpl.kt`),
`zj.z1` (`ScaleSyncMeasurementUseCaseImpl.kt`):

1. Advertisement seen for the paired MAC with own slot in 0x181D service data → connect
   (Kable, `hk.d` `KableDeviceConnection.kt`), discover services.
2. **Set time**: write Current Time 0x1805/0x2A2B (`ol.e.e`):
   `[year_lo, year_hi, month, day, hour, min, sec, 0, 0, 0]` in **local time**.
3. **Subscribe to the measurement characteristics first** (the model's listener set,
   §5). The app sets up the listener before logging in.
4. **Consent/login**: subscribe to UCP 0x2A9F indications, write
   `[0x02, user_index, consent_lo, consent_hi]` (`q2`, `BTScaleBF600UserLoginRepoImpl.kt`).
   Response `[0x20, 0x02, result]`: `result == 0x01` means success.
   Retries up to 3×. A login timeout is tolerated (`"Ignore login timeout scale not
   responding but doing right"`).
5. The scale then pushes stored measurements for that user on the subscribed
   characteristics. The listener (`o0`, `BTScaleBF600MeasurementListenerRepoImpl.kt`)
   joins the per-characteristic frames into one measurement and keeps them in a map
   keyed by timestamp. It flushes the batch after a quiet-period timer (the base repo is
   created with `super(3, 10, 4)`; units are unclear).
6. Optional: read settings / user list, update target weight (BF950-type), disconnect.

Write type: `WITH_RESPONSE` for everything except wifi models (`kj`) and some BF990
commands, which use `WITHOUT_RESPONSE` (`vi.f.l`).

### Bonding / security — **verified: bonding is mandatory**
The app never calls `createBond()` explicitly. The connection layer does handle
`GATT_AUTH_FAIL` (`"We are not bonded error"`).

**Observed on BF450:** right after connecting, the scale asks for bonding. If the central
does not bond (BlueZ adapter `Pairable: no` → kernel answers "pairing not supported"),
the **very first ATT request fails with `0x0E Unlikely Error` and the scale drops the
link ~0.5 s after connecting**, even for unprotected reads like Device Information or a
Current Time write. With the adapter pairable and a `NoInputNoOutput` agent, **Just
Works** bonding completes in ~1 s and everything works. After a scale reset the old bond
must be removed on the host; fresh pairing then works again. The HA side must therefore
be able to bond (BlueZ with pairing enabled; ESPHome proxies need pairing support).

---

## 4. Pairing / user management (BF600 family)

### Create a user (`zj.f` `ScaleBF600UserCreateUseCaseImpl.kt`, `e2`/`b2` `BTScaleBF600UserCreateRepoImpl.kt`)
1. Pick a random **consent code 1111–9998**.
2. UCP write `[0x01, consent_lo, consent_hi]` (Register New User).
   Response `[0x20, 0x01, 0x01, user_index]` (validity: length 4, first byte 0x20).
3. Login with `[0x02, user_index, consent_lo, consent_hi]` (as above).
4. Write the user profile (`v2`, `BTScaleBF600UserUpdateRepoImpl.kt`), each step optional:
   - Date of birth 0x2A85: `[year_lo, year_hi, month, day]`
   - Database Change Increment 0x2A99: `[0x01,0,0,0]` (`[FF,FF,FF,FF]` for BF500 `ig`)
   - Gender 0x2A8C: `[0]` male / `[1]` female
   - Height 0x2A8E: `[cm, 0]` (one byte `[cm]` for BF500 `ig`)
   - Activity level (custom, BF450 = FFF2): `[code]`; codes NONE→1, LOW 2, MIDDLE 3, HIGH 4, VERY_HIGH 5
   - Initials (custom, BF450 = FFF6): 3 ASCII bytes, NUL→space (skipped for marker `qh`)
   - Pregnancy/tare (0x000C): 1 byte (skipped for marker `oi`)
   - Target weight (0x0003): empty payload
5. Wi-Fi models also write a 37-byte unique identifier to 0xFFFA in two 20-byte chunks:
   `[0x00, bytes 0..18]` then `[0x01, bytes 19..36]` (`w1`).

The app stores `(user_index, consent_code)` per scale (Realm `DBWeightScaleUserEntity`).
**For HA we must store the same pair per person.**

### List users (`k2`/`l2` `BTScaleBF600UserListRepoImpl.kt`)
Write `[0x00]` to the user-list char (BF450: FFFF/FFF1) and collect notifications:
- `[0x01]` = list complete, `[0x02]` = list empty
- 12-byte entry (13 on wifi models, which have one extra byte before gender):

| Offset | Field |
|---|---|
| 0 | 0x00 |
| 1 | user index |
| 2..4 | initials (ASCII) |
| 5..6 | birth year (LE) |
| 7 | birth month |
| 8 | birth day |
| 9 | height (cm) |
| 10 (11 wifi) | gender (1 = female) |
| 11 (12 wifi) | activity level |

Collection stops after `allowedMaxUsers` entries (8 for BF450, 10 for BF105/BF1000).

### Delete user (`i2`, `BTScaleBF600UserDeleteRepoImpl.kt`)
UCP write `[0x03]` (UDS "Delete User Data"). It deletes the **currently consented** user,
so the consent code of that user is needed.

### Factory reset (`r0`, `BTScaleBF600ResetRepoImpl.kt`)
Write `[0x00]` to the "reset" characteristic: custom svc / **FFF7** (BF990: FFCF/FFC2).
This wipes all users. No consent is needed. On the BF450 the user list was empty
afterwards, and the host had to remove its stale bond and pair again.

### Scale settings (`h1` read / `i1` update, `BTScaleBF600Settings*RepoImpl.kt`)
Characteristic: BF450 FFFF/FFF0 (§1). Two layouts:

| Byte | 5-byte layout (marker `yg`, incl. BF450) | ≥6-byte layout (other models) |
|---|---|---|
| 0 | language (`BTScaleBF600Language`) | language |
| 1 | unit: 1 = lb, 2 = st, else kg | unit |
| 2 | time format | time format |
| 3..4 | weight threshold, uint16 LE | guest mode |
| 5 | | weight threshold low (with 4: bytes 4..5 LE) |
| 6..7 | | extra uint16 (bytes 6..7), only if length ≥ 8 |

Weight threshold = raw ÷ 10 (÷ 1000 for marker `mg`, ÷ 1 for `lg`). It is most likely the
user-recognition tolerance. BF450 read `ff 00 ff 1e 00`: language unset, **kg**, time format
unset, **threshold 3.0 kg**.

### Take measurement on demand (`s1`/`p1`/`r1`)
Subscribe to the take-measurement char (BF450: FFF4), write `[0x00]`.
Notification `[status]`: `1` = OK, `2` = error (`ok.a.m.q`), `3`/`4` = other errors.

### Guest measurement (`o1`/`l1`)
Write to 0x0007 (BF990: FFF9):
`[year_lo, year_hi, month, day, gender, height_lo, height_hi, activity]` for BF450/451/990
(marker `hj`), else `[year_lo, year_hi, month, day, gender, height, activity, 0]`.
Note: gender here uses enum codes **MALE=1, FEMALE=0** (opposite of 0x2A8C).

---

## 5. Measurement data formats

### 5.1 Weight Measurement 0x2A9D (`ej.c`, `BTScaleBF600MeasurementWeightModelParser.kt`)
The app **ignores the flags byte** and always parses the fixed 15-byte layout below
(timestamp + user ID + BMI/height, SI units). It has **no** length or flags check in
1.22.0 (an earlier version of this doc wrongly claimed `data[0] == 0x0E`). The BF450 sends
flags `0x02`.

| Offset | Field | Scale |
|---|---|---|
| 0 | flags — **BF450 sends `0x02`**, see note | |
| 1..2 | weight uint16 | × 0.005 kg |
| 3..4 | year | |
| 5,6,7,8,9 | month, day, hour, min, sec (scale **local time**) | |
| 10 | user index | |
| 11..12 | BMI | × 0.1 |
| 13..14 | height | **raw = cm** on BF450 (`b4 00` = 180 cm, §9.3). The app multiplies by 0.1 and truncates (→ 17), which is wrong but unused for the BF450 |

Firmware workaround (marker `oe` = BF720/BF722/BF950): if bytes 0..2 == `02 00 55`
(the "108.8 kg error"), weight = BMI × (height/100)².

### 5.2 Body Composition 0x2A9C (`ej.a`, `BTScaleBF600MeasurementBodyModelParser.kt`)
The app also ignores flags here. Validity: length 14, `data[0] == 0x98` (flags 0x0398 =
basal metabolism, muscle %, soft lean mass, body water mass, impedance). A raw value of 0
means absent.

| Offset | Field | Scale |
|---|---|---|
| 0..1 | flags | |
| 2..3 | body fat % | × 0.1 |
| 4..5 | basal metabolism (kJ) | ÷ 4.1868 → kcal |
| 6..7 | muscle % | × 0.1 |
| 8..9 | soft lean mass | **BF450/451: × 0.1 kg**; others × 0.005 kg |
| 10..11 | body water mass | **BF450: × 0.1 kg** (observed e.g. `cb 01` = 45.9 kg = 58.5 % of 78.4 kg, anonymised values from §9.3). The app uses × 0.005 kg for every model (→ 2.3 kg, an app bug); it takes water % from FFF6 instead. Other models: × 0.005 kg [verify] |
| 12..13 | impedance | × 0.1 Ω |

### 5.3 BF450/BF451 custom frames (`ej.b.e`, `q` = `initDeviceListenerForBF450`)
The listener zips **weight + body + custom1**. If body impedance is non-null, it waits for
**custom2** as well.

**Custom1 — FFFF/FFF5** (`xh.a`, `BTScaleBF450Custom1Model.kt`), 5 bytes:

| Offset | Field | Scale |
|---|---|---|
| 0..1 | ideal weight | × 0.005 kg |
| 2..3 | AMR (active metabolic rate, kJ) | ÷ 4.1868 → kcal |
| 4 | isUserUnknown (>0 = true) | |

**Custom2 — FFFF/FFF6** (`xh.b`, `BTScaleBF450Custom2Model.kt`), 16 bytes, 0 = absent:

| Offset | Field | Scale |
|---|---|---|
| 0..1 | visceral fat | raw |
| 2 | metabolic age (valid 1..99) | years |
| 3..4 | lean body mass | × 0.005 kg |
| 5..6 | protein mass | × 0.005 kg |
| 7..8 | total muscle mass | × 0.005 kg |
| 9 | body shape index | raw |
| 10..11 | subcutaneous fat | × 0.1 (%) |
| 12..13 | bone mass | × 0.005 kg |
| 14..15 | water % | × 0.1 (falls back to water mass / weight) |

**Observed:** FFF6 on the BF450 is **indicate-only** (no write), so the "initials = FFF6"
entry in the app config cannot be a working write on this model. The scale's user list
reports initials as `ff ff ff`, i.e. the app never sets them on the BF450.

Custom1 byte 4 is not a plain boolean: it was `00` when the user was identified directly
(`U-2` on the display) and **`02`** when the display first showed `U--` (ambiguous) and
then settled on `U-2`. [verify] other values.

### 5.4 Other model frames (survey depth)
- **Analysis body 0x0008** (BF105/BF1000), 9 bytes, `[0]==0x1E`: upperBodyFat, lowerBodyFat, upperMuscle, lowerMuscle (int16 × 0.1).
- **Analysis fat 0x0009** (BF1000), 12 bytes, `[0]==0x7E`: visceral, leftArm, rightArm, leftLeg, rightLeg, torso (× 0.1).
- **Analysis muscle 0x000A** (BF1000), 11 bytes, `[0]==0x3E`: leftArm, rightArm, leftLeg, rightLeg, torso (× 0.1).
- **Heart-rate frame FFF5** (BF500/880/980), 6 bytes: pulse at [1..2] (0xFF = absent), metabolic age [5], visceral [3..4] × 0.1.
- **BF990**: 20-byte frames on FFF5/FFF7/FFF8 (heart-rate-extended: metabolicAge, pulse, visceral, bmr, amr, boneMass, leanBodyMass, proteinMass, totalMuscleMass, water%; full-body segmental fat/muscle ×10 values; "unknown measurement"). Not decoded byte-by-byte yet.

### 5.5 Unified app model (`jq.h`, `WeightMeasurement.kt`)
`weight, boneWeight, bodyWaterPercentage, muscleMassPercentage, bodyFatPercentage, amr,
bmr, bmi, softLeanMass, userMode`, plus type 1–4 analyses
(type 3 = metabolicAge/pulse/visceral; type 4 = leanBodyMass/proteinMass/totalMuscleMass/
subcutaneousFat/idealWeight/bodyProfile). This is a useful target for HA sensor
entities.

---

## 6. BF700 / BF710 protocol (survey depth)

Service 0xFFE0, characteristic 0xFFE1 (write + notify). Each command is
`[prefix, opcode, args…]`. Prefix/series (`BTScaleBF700SeriesType`):
TYPE_1 (name contains "BF700") request/response `0xF7`; TYPE_2_3 `0xE7`;
TYPE_710 requests with `0xF7` and gets responses with `0xE7`.

Opcodes (`BTScaleBF700Commands`): init 0xF6/0xE6 (+0x01), set time 0xF9/0xE9,
set unit 0x4D, first user 0x33, next user F1 34, user info 0x36, create 0x31,
update 0x35, delete 0x32, take measurement 0x40, get measurements 0x41,
next measurement F1 42, scale status 0x4F, unknown measurements 0x46/next F1 47,
delete unknown 0x49, assign unknown 0x4B, delete user measurements 0x43,
threshold 0x4E. Live weight is 0x58; saved measurement is 0x59 (two 13-byte notifications).

Saved measurement (`fj.a`, big-endian fields split over two frames):
timestamp uint32 (unix seconds), weight × 0.05 kg, impedance, fat % /10, water % /10,
muscle % /10, bone /20 kg, BMR, AMR, BMI × 0.1.

---

## 7. Resolved marker interfaces (obfuscated `jp.*`)

| Marker | Meaning (inferred from usage) | Models |
|---|---|---|
| `pd` | BF600 (UDS) protocol family | all except BF700/710 |
| `qd` | BF700 protocol family | BF700, BF710 |
| `bi` | custom service is 0xFFFF (else 0xFFF0) | most |
| `od` | BF450-type listener (custom1/custom2), soft lean × 0.1 | BF450, BF451 |
| `jj` | BF450 char numbering (user list FFF1, settings FFF0, activity FFF2) | BF450, BF451 |
| `kj` | Wi-Fi scale (13-byte user entries, write-without-response) | BF880, BF980, BF990 |
| `hj` | 16-bit height in guest measurement | BF450, BF451, BF990 |
| `nh` | heart-rate frame on FFF5 | BF500, BF880, BF980, BF990 |
| `nd` / `md` | upper/lower / full segmental analysis | BF105+BF1000 / BF1000 |
| `ji` | weight only (no 2A9C) | GS435 |
| `oe` | 108.8 kg firmware-bug workaround | BF720, BF722, BF950 |
| `yh` | `allowedMaxUsers` | all |

---

## 8. Open questions for runtime verification

1. ~~GATT dump of a real BF450~~: done, see §9.1.
2. ~~Is bonding required?~~ Yes, see §3 and §9.2.
3. ~~0x181D service-data layout~~: done, see §2.
4. Does the scale send anything for an unregistered person (isUserUnknown), or only
   store it on the scale?
5. Timing: how long does the scale stay connectable after a weigh-in, and how long after
   consent do the stored measurements arrive?
6. Is the weight flags byte ever not 0x0E (for example in lb/st display mode)?

Suggested method: nRF Connect / `bleak` GATT dump plus an advertisement capture on the
HA host while weighing, and optionally Android HCI snoop log with the official app.

---

## 9. Runtime observations on a real BF450 (2026-10-15/16)

Setup: host BlueZ via D-Bus from a container, bleak 3.0.2, scripts in the session
scratchpad (not kept). Scale: BF450, public address (not recorded here), appearance
`0x0C80`, battery 71 %.

**Anonymised values.** All frames, profiles, consent codes and timestamps in this section
were re-encoded with a fictional person (born 1985-04-12, 180 cm, male, activity 3) and
fictional times. Frame layout, flags, user indices, the custom1 "guessed" byte, frame
order, and the relationships between fields (BMI, LBM, water %, AMR = BMR × 1.5, ideal
weight) are exactly as captured; only the measured values were replaced.

### 9.1 GATT table (handles as reported by BlueZ)

| Service | Char | Handle | Properties | Notes |
|---|---|---|---|---|
| 1800 | 2A00/2A01/2A02/2A04 | 2–8 | read | |
| 1801 | 2A05 | 11 | indicate | |
| 180A | 2A29, 2A26, 2A28, 2A24, 2A27, 2A25 | 15–25 | read | |
| 180F | 2A19 | 28 | read | `47` = 71 % |
| 1805 | 2A2B | 31 | read, write, notify | echoes the time it was set to (+ elapsed s) |
| 181D | 2A9E | 35 | read | Weight Scale Feature `31 00 00 00` |
| 181D | 2A9D | 37 | indicate | weight |
| 181B | 2A9C | 41 | indicate | body composition |
| 181B | 2A9B | 44 | read | Body Composition Feature `cf 01 00 00` |
| 181C | 2A8C, 2A85, 2A8E | 47–51 | read, write | gender, DOB, height |
| 181C | 2A9A | 53 | read | user index (`ff` before consent) |
| 181C | 2A99 | 55 | read, write, notify | DB change increment |
| 181C | 2A9F | 58 | write, indicate | UCP |
| FFFF | FFF0 | 62 | read, write | settings, read `ff 00 ff 1e 00` |
| FFFF | FFF1 | 64 | write, notify | user list |
| FFFF | FFF2 | 67 | read, write | activity level |
| FFFF | FFF3 | 69 | read | `ff ff`. The app uses FFF3 only as the activity char of other models (BF600/BF850/…); never on the BF450 |
| FFFF | FFF4 | 71 | write, notify | take measurement |
| FFFF | FFF5 | 74 | indicate | custom1 |
| FFFF | FFF6 | 77 | indicate | custom2 (**no write**) |
| FFFF | FFF7 | 80 | write | **factory reset** (`00`), see §4 |
| FFFF | FFF8 | 82 | write, notify | the app uses FFF8 only as a BF990 measurement char; unused on the BF450 |

All UDS characteristics read as `ff…` until a user has consented.

### 9.2 Verified sequence (works)
1. Connect **with bonding** (Just Works). ~2 s fresh, ~5–7 s for a reconnect incl. service
   resolution.
2. Write Current Time 2A2B (local time) → OK.
3. Subscribe 2A9D, 2A9C, FFF5, FFF6, FFF1, FFF4, 2A99, 2A2B, 2A9F.
4. User list: write `00` to FFF1 → one 12-byte notification per user, then `01`; with no
   users just `02`. Layout as in §4 (e.g. `00 01 ff ff ff c1 07 04 0c b4 00 03` = idx 1,
   no initials, born 1985-04-12, 180 cm, male, activity 3).
5. Register: UCP `01 e1 10` (code 4321) → `20 01 01 02` (new index 2).
6. Consent: UCP `02 02 e1 10` → `20 02 01`.
7. Profile writes 2A85 / 2A99 `01 00 00 00` / 2A8C / 2A8E `[cm, 0]` / FFF2 all succeed and
   read back unchanged.
8. Pending measurements for the consented user arrive **~0.4 s after the consent
   response**, in the order 2A9D → 2A9C → FFF5 → FFF6, ~0.4 s apart. No end marker.
9. Take measurement: FFF4 `00` → the next weigh-in goes to the logged-in user; frames
   arrive as above, then FFF4 notifies `01`.

The scale **never drops a bonded connection by itself** (connections held for 40 s and
90 s were closed by the host). Unbonded links are dropped after ~0.5 s.
When nobody connects, the scale was still advertising 30 s after wake-up (the end of the
awake window was not measured).

### 9.3 Captured frames (test fixtures)

Weigh-in with take-measurement, user 2 (display `U-2`):
```
2A9D 02 40 3d e8 07 03 0a 07 0c 1e 02 f2 00 b4 00   78.4 kg, 2024-03-10 07:12:30, user 2, BMI 24.2, 180 cm
2A9C 98 03 b8 00 c0 1c 9c 01 5e 02 cb 01 00 14      fat 18.4 %, BMR 7360 kJ (1758 kcal), muscle 41.2 %,
                                                    soft lean 60.6 kg, water 45.9 kg, 512.0 Ω
FFF5 0e 37 20 2b 00                                 ideal 70.47 kg, AMR 11040 kJ (2637 kcal), user byte 0
FFF6 07 00 22 fa 31 40 0b ac 21 08 a1 00 9e 02 49 02 visceral 7, met. age 34, LBM 63.97 kg, protein 14.40 kg,
                                                    total muscle 43.10 kg, shape 8, subcut. 16.1 %,
                                                    bone 3.35 kg, water 58.5 %
FFF4 01
```
Delivered on consent after weigh-in with display `U--` → `U-2`:
```
2A9D 02 40 3d e8 07 03 0a 07 0f 0b 02 f2 00 b4 00
2A9C 98 03 b9 00 c0 1c 9c 01 5d 02 cb 01 20 14
FFF5 0e 37 20 2b 02
FFF6 07 00 22 ec 31 30 0b ac 21 08 a1 00 9e 02 49 02
```
Offline weigh-in (no connection), delivered ~36 s later on consent:
```
2A9D 02 18 3d e8 07 03 0a 07 11 09 02 f1 00 b4 00   78.2 kg, 2024-03-10 07:17:09
2A9C 98 03 b8 00 b8 1c 9b 01 5d 02 c9 01 40 14
FFF5 0e 37 14 2b 02
FFF6 07 00 22 da 31 38 0b 98 21 08 a1 00 9e 02 49 02
```
Cross-checks that pin down the scales (they held on the real captures; the anonymised
values above keep them): BMI = 78.4 / 1.80² = 24.2; LBM = 78.4 × (1 − 0.184) = 63.97;
soft lean = LBM − bone; water 45.9 kg / 78.4 kg = 58.5 % ≈ FFF6 58.5 %;
AMR = BMR × 1.5 (activity 3); ideal weight = 21.75 × 1.80².

### 9.4 Differences from the app code / §1–§5
- Weight flags byte is `0x02` although the frame contains user ID, BMI and height. The
  app never checks the flags (re-checked in 1.22.0). Validate on length 15 instead.
- Height in 2A9D is raw cm; body water mass in 2A9C is × 0.1 kg on the BF450.
- Advert service UUID is byte-swapped (`0x1D18`).
- FFF6 is indicate-only; initials are never written.
- Two users with identical profiles and the same weight make the scale show `U--`
  before it picks one. **Consequence for HA:** the official app and HA should not both
  register a user for the same person.

Unknown person (scale had **no users**, display `U--`): the advert slot list stayed empty
for the whole 30 s observed (108 adverts), and a user registered right afterwards
received nothing after consent. Unassigned readings are therefore invisible to BLE
clients: no advert flag, no later delivery. The scale stayed awake and advertising for
at least 30 s with nobody connected.

First weigh-ins of a freshly created user (only user on the scale, slot 1):
- Weigh-in 1, display `U--`: still stored for slot 1. The advert slot list went from empty
  to `01` at the moment the reading was final (~14 s after wake). Custom1 byte 4 = `01`.
- Weigh-ins 2 and 3 happened while connected and consented: each was pushed live right
  away (2A9D → 2A9C → FFF5 → FFF6), followed by **FFF4 `01` even though no
  take-measurement command was sent**. FFF4 `01` therefore means "a live reading just
  finished", not only "your take-measurement request finished". Custom1 byte 4 = `00`.
- After those readings the display showed `U-1`: the scale learns the user's weight from
  the first readings.

Custom1 byte 4, all observations: `00` when the scale recognised the user directly, `02`
for `U--` → slot 2, `01` for `U--` → slot 1. Working hypothesis: **0 = confident match,
otherwise the slot the scale guessed**. The app only uses it as a boolean
(`isUserUnknown = byte4 > 0`) and stores it in the measurement metadata (`yp.b`).

### 9.4b Multi-reading sync, error codes, delete, awake window (second session)

**Awake window without a connection:** the scale advertised for **47 s** after waking
(183 adverts, about 4 per second, no gaps), then went silent. After a **host-initiated
disconnect** it stopped advertising about 1 s later.

**Pending flag survives sleep:** after an offline weigh-in at 07:54:00 (never synced) the
next wake-up advertised `01` from the very first advert. The flag stays the same when a
second reading for the same slot is added.

**Several stored readings in one sync:** consent delivered both readings, **oldest
first**, as back-to-back groups of 4 frames (2A9D → 2A9C → FFF5 → FFF6, ~0.4 s apart).
There is no separator or end marker between groups. Group boundaries come from the 2A9D
frame, which always starts a group:
```
2A9D 02 dc 3c e8 07 03 0a 07 36 00 01 f0 00 b4 00   77.9 kg, 07:54:00 (offline reading)
2A9C 98 03 b7 00 b0 1c 9a 01 5b 02 c8 01 dc 13
FFF5 0e 37 08 2b 00
FFF6 07 00 21 b8 31 34 0b 88 21 08 a0 00 9e 02 4a 02
2A9D 02 dc 3c e8 07 03 0a 07 3a 22 01 f0 00 b4 00   77.9 kg, 07:58:34 (reading of this wake-up)
2A9C 98 03 b7 00 b0 1c 9a 01 5b 02 c8 01 02 14
FFF5 0e 37 08 2b 00
FFF6 07 00 21 b8 31 34 0b 88 21 08 a0 00 9e 02 4a 02
```
**After the sync the flag is cleared:** the adverts after the disconnect showed an empty
slot list.

**UCP error responses:**

| Request | Response |
|---|---|
| Consent, correct code | `20 02 01` (success) |
| Consent, wrong code | `20 02 04` |
| Consent, slot that does not exist | `20 02 04` (same) |
| Consent for a just-deleted user | `20 02 04` |
| Delete (`03`) while consented | `20 03 01`; the slot disappears from the user list |

`0x04` is UDS "Operation Failed". A wrong code and a missing slot give the same answer.
To tell "scale was reset / user deleted" apart from "wrong code", read the user list
(FFF1): it needs no consent. Two wrong attempts in a row did **not** lock out the correct
code that followed.

Timing with an existing bond: connect + service resolution ~5.5 s; set time + 10
subscriptions ~4.6 s (each GATT op ~0.4 s); consent → first frame 0.4 s.

### 9.5 Still open
- Exact length of the awake/advertising window after a weigh-in.
- FFF3 and FFF8 on the BF450: the app never uses them here (§9.1); their purpose is unknown.
- Confirm the custom1 byte 4 hypothesis (more users, real strangers).
- Whether lb/st mode never flags the pending slot, or only did so once (§9.6).

### 9.6 Through an ESPHome Bluetooth proxy (2026-10-19, design §8 V1–V8)

Setup: a test HA 2026.10.2 whose only Bluetooth path was an ESP32-C3 proxy (ESPHome
2026.9.1, `bluetooth_proxy: active: true`), driving the integration; one earlier run
went through an Everything Presence Lite (ESP32, ESPHome 2025.5.1) at RSSI −88. Same
BF450 as above, factory-reset first. Two scale users with the fictional profiles of
§9.3; the second user's weight was raised ~11 kg by holding a load, so the scale told
them apart (`U-1` / `U-2`). No measured values are recorded here.

**Advertisement vs scan response (V4).** The advertisement (ADV_IND) carries only:
```
02 01 05                      flags
03 02 18 1d                   16-bit UUID list: 0x1D18 (0x181D byte-swapped)
06 09 42 46 34 35 30          complete local name "BF450"
08 1b xx xx xx xx xx xx 00    LE Bluetooth device address (public)
03 19 80 0c                   appearance 0x0C80 (generic weight scale)
```
27 of 31 bytes, so the pending-slot list can't fit. It comes only in the **scan
response**, together with manufacturer data for company ID `0x1106` (`00 01 00`):
`06 ff 06 11 00 01 00` plus the `0x16` service data for `0x1D18` with the slot list. A
passive scanner therefore sees every wake (name, UUID) but **never which slots are
pending**. With the proxy scanning actively, the first scan response usually arrived
within 0.3 s of the wake, but once only after 11 s.

HA (habluetooth) merges an address's advertisement data: an ADV_IND keeps the service
data of the last scan response. Right after HA disconnects, the first advertisement
therefore still shows the slot list from before the sync; the next scan response
(~0.4 s later) has the cleared list.

**Bonding through the proxy (V1, V8).** The ESP32 proxy bonds with the BF450 well before
the 0.5 s no-bond drop; ESPHome 2025.5.1 and 2026.9.1 both work.
- First connection with bonding and a service discovery (cache miss): 0.6–6.0 s.
- Later connections (bond and GATT cache on the proxy): 0.6–3.9 s.
- One GATT request through the proxy: ~0.4 s, up to ~1.5 s on the first session or at
  a weak signal.
- One slot synced (time, subscriptions, consent, 1 reading, battery): 9.8 s.
- Two slots in one connection: 25 s.
- At RSSI −88 the link broke after 23 s (HCI error 8, connection timeout).

**Two consents in one connection (V2).** Works: consent slot 2 → its stored readings →
consent slot 1 (`20 02 01`) → its stored readings, all in one connection.

**Teaching (V3).** While a scale user is consented, the scale stays awake (154 s in one
session) and every step-off/step-on sends a live reading (2A9D → 2A9C → FFF5 → FFF6 →
FFF4 `01`). The first reading after creating a scale user can arrive while HA is still
subscribing: subscribe to the reading characteristics first. **One reading is enough:** a
scale user taught with a single reading was recognised at the next weigh-in. The
step-on that wakes the scale for the create flow can be that reading. The scale takes a
new weigh-in only after it has shown all results and the display has gone dark (the
Bluetooth icon may stay on); stepping on earlier isn't measured.

**Deleting a scale user.** In the same connection as a sync: consent as the removed
scale user (a second consent), then UCP `03` → `20 03 01`; the slot is gone.

**Display unit (V5).** Settings (FFF0) can be written **without consent**:
`ff 00 ff 1e 00` → `ff 01 ff 1e 00` switches the display to lb at once, and writing the
old value back restores kg. In lb mode the frames are **unchanged**: the weight is still
in kg (flags `0x02`, 0.005 kg resolution). The display unit is display-only. But the one
weigh-in taken in lb mode was stored for its slot **without the slot being flagged
pending** in the scan response for the rest of the wake; it was delivered at the next
consent.

**Factory reset with an existing bond (V6).** The proxy still held the old bond, the
scale didn't. Bonding failed with ESP-IDF auth error 97 (most likely
`ESP_AUTH_SMP_ENC_FAIL`: encryption with the stored key failed); the scale then
terminated the link (`ESP_GATT_CONN_TERMINATE_PEER_USER`) and slept ~3 s later. The
integration never got a connection, so its "renew the bond after a reset" step (§4)
couldn't run. Removing the bond on the proxy (ESPHome `bluetooth_device_unpair`, which
works without a connection) fixed it: the next connection bonded again.
The error text is only in the first connect attempt ("Pairing failed due to error: 97");
bleak-retry-connector's final exception says just "Disconnected". The integration therefore
records the error of every attempt, removes the proxy's bond through the ESPHome API and
connects again (design §3, §6). Bleak's own `unpair()` is no way out: ESPHome's client
requires a connection, and the scale ends an unbonded link after ~0.5 s.

**Timing (V7).**
- A new weigh-in's slot appeared in the scan response ~10 s after stepping on (kg mode);
  in two later runs after 4.1 s and 7.5 s (#24).
- Connecting *during* a weigh-in and consenting another scale user first aborted it. The
  display showed each consented user with a sync icon (`U-2`, then `U-1`) and went dark
  after the disconnect, although the person was still standing on the scale. No reading
  was stored.
- Connecting during a weigh-in and consenting the person standing on the scale let the
  reading complete live.
- Without a connection the scale advertised for up to ~25 s after a weigh-in.
- After a host-initiated disconnect it advertised again for 0–20 s (observed: one
  advertisement, 1.7 s, 17 s, 20 s), not always ~1 s as on BlueZ (§9.4b).

**Display.** After the reset, before any scale user existed, a weigh-in showed only the
weight. With a scale user and its profile, the display shows `U-n` and the full body
composition.

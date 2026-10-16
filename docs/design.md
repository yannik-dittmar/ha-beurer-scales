# Design: Beurer Scales for Home Assistant (v1)

Result of the design session on 2026-10-16. Terms are defined in [`CONTEXT.md`](../CONTEXT.md). The decisions with real trade-offs live in [`docs/adr/`](adr/); this document links them instead of repeating the reasoning. Inputs: the [protocol doc](beurer-ble-protocol.md) (§9 = observed BF450 behaviour) and the [HA capabilities research](research/home-assistant-integration-capabilities.md).

## 1. Shape

- A single HACS custom integration. Domain `beurer_scales`, display name "Beurer Scales", MIT license, English UI only. ([ADR 0001](adr/0001-single-integration-no-protocol-library.md), [ADR 0009](adr/0009-own-integration-not-ha-beurer-bf600.md))
- The protocol modules (frame decoders, UCP, per-model tables, sync session over a `BleakClient`) live inside the integration but never import Home Assistant. They are tested against the §9 fixtures without HA.
- Minimum Home Assistant version: 2026.10. ([ADR 0007](adr/0007-minimum-home-assistant-2026-10.md))
- Model scope: the BF600 (UDS-based) family, described by a per-model table (protocol doc §1, §7). BF450/BF451 are "verified". All other family models are discovered automatically, and the flow warns that they are untested. The BF700/BF710 family is out of scope for v1; the sync session interface leaves room for a second protocol implementation.

## 2. Configuration structure

- **One config entry per scale.** unique_id = the scale's Bluetooth address. ([ADR 0004](adr/0004-config-entry-per-scale-subentry-per-scale-user.md))
  - The Bluetooth discovery step only asks the user to confirm. It does not connect.
  - The manual `user` step lists Beurer scales HA has seen nearby.
  - Manifest matchers: `service_data_uuid` for both byte orders of 0x181D, plus `local_name` patterns per model family.
- **One config subentry per scale user.** unique_id = `slot_<n>`. Subentry data: slot, consent code, profile, optional person (entity registry id).
- **Devices.** Each scale user has its own device, linked to the scale device with `via_device_id`. Removing a subentry removes its device and entities.
- **Scale options.** "Notify about unrecognised weigh-ins" (on by default).

## 3. Bluetooth

- Any connectable path (local adapter or ESPHome proxy) may be used. HA always requests pairing (`pair=True`), and we do not try to pin the scale to one proxy. ([ADR 0002](adr/0002-no-own-bluez-pairing-agent.md))
- The primary, tested path is the ESPHome ESP32 proxy.
- No integration-owned BlueZ agent. When pairing fails, a repair issue explains the options: add an ESPHome proxy, or make the host adapter pairable.
  - Shelly proxies can't connect at all.
  - A proxy without pairing support raises `NotImplementedError` on pair. Show it as its own clear error.
- Every session sits inside the ~47 s wake window: cap the whole session with a timeout, limit connection attempts, and always disconnect in `finally`.

## 4. Scale user lifecycle

The scale user is the core of the design. HA is the only client for the scale users it manages, and the user documentation must say so. ([ADR 0003](adr/0003-home-assistant-is-the-only-client.md))

### 4.1 Add scale user (subentry flow)

The flow opens with a menu: **Create new scale user** or **Adopt existing slot**.

The form shared by both paths:
- **Person** (optional HA `person` picker) and **Name** (optional).
  - An empty name falls back to the person's name. If both are empty, the form shows an error.
  - The name must be unique per scale.
  - The person link is stored, but renaming the person later does not rename the scale user.
- **Profile** (create only): birth date, height, gender, activity level. Initials are not writable on the BF450.

**Create:**
1. Form as above.
2. Progress step "Step on the scale to wake it". It waits for the wake through the per-advert callback, with its own timeout (~3 min), because the flow framework has none.
3. Connect and bond. Read the user list and abort if there is no free slot. Register the scale user with a random consent code (1111–9998). Consent and write the profile.
4. **Teaching**, in the same connection. A progress step says "Step off and on again: reading k of 3". Each live reading counts (2A9D → … → FFF4 `01`). The user can finish after 1 reading, and the step has its own timeout. After each reading a menu offers another reading or finishing (a progress step can't have buttons); the per-reading timeout keeps running while it is open. A timeout before the first reading deletes the scale user; after one, it keeps the scale user and the flow says teaching ended early.
5. Disconnect and create the subentry.

**Abort or failure after step 3:** delete the scale user on the scale (UCP `03`, while still consented) before disconnecting. A half-created scale user that HA doesn't track would steal recognition from the others.

**Adopt:**
1. The user enters slot and consent code (only for slots whose code they know, e.g. one HA created before a reinstall).
2. A progress step waits for the next wake, checks the consent, and reads the profile back from the user list.

### 4.2 Change profile

Subentry reconfigure flow:
1. Form, then the progress step "Step on the scale to wake it".
2. Write the profile.

A name-only change does not need the scale.

### 4.3 Remove scale user

HA removes the subentry immediately. The integration remembers `(slot, consent code)` in `Store` as a pending deletion. At the next wake of any kind, after the weigh-in is final (same timing rule as §5.3), it connects, consents and deletes the scale user. If consent fails, the request is dropped.

## 5. Syncing

### 5.1 Trigger

- Watch every advertisement of the scale (per-advert callback, HA ≥ 2026.10).
- Connect when the 0x1D18 service data lists a **managed** slot.
- Pending slots without a subentry are ignored and logged at debug level.

### 5.2 Session

The order follows protocol doc §3:
1. Set the time (HA local time).
2. Subscribe to the measurement characteristics.
3. Consent.
4. Collect readings until a quiet period.
5. Read the battery level.
6. Disconnect.

Several managed slots pending:
- Sync the slot that just became pending first.
- Then try to consent the other managed pending slots in the same connection, to drain them.
- If the scale refuses the second consent, drain them at later wakes. *(Unverified, §8.)*

### 5.3 Unrecognised weigh-ins

If a wake reaches ~25 s and no managed slot is pending, connect, read the user list (no consent needed) and disconnect. ([ADR 0008](adr/0008-connect-after-unrecognised-weigh-ins.md))
- This detects a factory reset or a deleted scale user (§6).
- If the option is on, a persistent notification says that someone was weighed but not recognised (with the weight, if the advert or user list provides it; otherwise without).

### 5.4 Reading policy

- **Fresh reading:** written to the scale user's entities right away. The entity state time is the sync time, seconds after the weigh-in.
- **Backlog reading** (timestamp more than 5 min older than the sync start): collected so the pending flag clears, then discarded. ([ADR 0005](adr/0005-backlog-readings-are-discarded.md))
- **Guessed reading** (custom1 byte 4 ≠ 0): discarded. ([ADR 0006](adr/0006-guessed-readings-are-discarded.md))
- Every discard logs a warning and creates one persistent notification per scale user and sync, giving the count, the time range and the reason.
- **De-duplication:** keep a short `(slot, timestamp, weight)` log in `Store`, so a reading delivered twice (e.g. after a partial sync) is never emitted twice.

## 6. Failures and repairs

| Situation | Detected by | Reaction |
|---|---|---|
| Wrong consent code | consent `20 02 04` + slot present in the user list | Fixable repair → adopt again with the code / create again / remove scale user |
| Scale user deleted on the scale | consent `20 02 04` or §5.3 check: slot missing | Fixable repair → create again / remove scale user |
| Factory reset | user list empty | Same repair, plus one automatic `unpair()` → `pair()` (the scale lost its bond keys) |
| Pairing impossible on this path | pair error / `NotImplementedError` | Repair: add an ESPHome proxy or make the host adapter pairable |
| Scale reports a unit other than kg | settings / frames | Warning. v1 never writes scale settings. |

Repair fix flows forward to the scale user's subentry flows via `next_flow`.

## 7. Entities

**Per scale user device:**

| Entity | Unit | Device class | Default |
|---|---|---|---|
| Weight | kg | `weight` | enabled |
| BMI | — | — | enabled |
| Body fat | % | — | enabled |
| Body water | % | — | enabled |
| Muscle | % | — | enabled |
| Bone mass | kg | `weight` | enabled |
| BMR, AMR | kcal | — (ENERGY would require `total`) | enabled |
| Visceral fat | — | — | enabled |
| Metabolic age | years | — | enabled |
| Lean body mass, protein mass, total muscle mass, soft lean mass | kg | `weight` | enabled |
| Subcutaneous fat | % | — | enabled |
| Last reading | — | `timestamp` | enabled |
| Ideal weight | kg | `weight` | disabled |
| Impedance | Ω | — | disabled |

- Metric sensors use `state_class: measurement` and are `RestoreSensor`s.
- A model that doesn't provide a metric doesn't get that entity.

**Scale device:**
- Battery (%), read at every sync.
- Last sync (timestamp, diagnostic).

## 8. To verify when the scale is available again

| # | Item | Affects |
|---|---|---|
| V1 | An ESP32 ESPHome proxy bonds with the BF450 before the 0.5 s no-bond drop (research U4); proxy sync latency within the 47 s window | §3, everything |
| V2 | One connection can consent two scale users in a row | §5.2 |
| V3 | The scale stays awake while HA holds the connection for the teaching step, and each step-off/step-on yields a live reading | §4.1 step 4 |
| V4 | 0x1D18 service data and local name are visible with passive scanning (research U7) | §5.1 |
| V5 | Frames are unchanged when the scale display is set to lb/st | §6 |
| V6 | Scale and proxy behaviour after a factory reset with an existing bond (research U5) | §6 |
| V7 | The ~14 s "reading final" and ~25 s thresholds hold, so the §5.3 connection never cuts a weigh-in short | §5.3, §4.3 |
| V8 | The minimum ESPHome version needed for pairing: 2023.3.0 (research) vs 2024.3.0 (ha-beurer-bf600 README) | docs |

## 9. Follow-ups (not v1)

- An opt-in BlueZ pairing agent for HA OS local adapters (ADR 0002).
- Holding guessed readings and letting the user keep, move or discard each one (ADR 0006).
- A "Teach" button on the scale user device for re-teaching later.
- The BF700/BF710 protocol family.
- Long-term statistics import for backlog readings (if ADR 0005 is ever revisited).

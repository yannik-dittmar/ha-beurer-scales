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
- Connect no earlier than 1.5 s after the wake starts. Proxies report the first advertisements at different times, and HA picks the path from the proxies that have reported the scale. Connecting at the very first advertisement left HA a single, distant proxy whose link broke (#17).
- A failed bond is recognised from the pairing error of any connect attempt: bleak-retry-connector raises only the last error, which is just "Disconnected" because the scale ends the link after a bond error. `open_session` then calls its `remove_bond` hook and connects again once. `connection.async_open_session` passes `bond.async_remove_bond(hass, device)`, which asks the ESPHome proxy the scale was found through to drop its bond. A local adapter can't unpair without a connection, so there the failure stands and the repair explains the manual removal. The factory reset in the configure menu (#30) calls the same `async_remove_bond`.
- A session watches the link. When it closes, every wait in the session (a response, a live reading) ends at once with `ConnectionLost` instead of running into its timeout.

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
4. **Teaching**, in the same connection, from one step-on (ADR 0010). The form is followed by progress steps "connecting" (the instruction: step on and stay on until the display goes dark; that step-on wakes the scale), "creating the scale user" and "measuring". The flow waits for one live reading (2A9D → … → FFF4 `01`), with a timeout. There is no reading counter and no menu. The flow ends with the measured weight (create entry description, or the abort text of the repair fix flows). A timeout before the reading deletes the scale user. A broken link does the same, except that the scale user can't be deleted without a link: the deletion is queued like a removed scale user's (§4.3) and runs at the next wake. The texts tell the user to wait for the dark display before stepping on again.
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

HA removes the subentry immediately. The integration remembers `(slot, consent code)` in `Store` as a pending deletion. At the next wake of any kind, after the weigh-in is final (same timing rule as §5.3), it connects, consents and deletes the scale user. If consent fails as the first consent of the connection, the code is wrong and the request is dropped. If it fails as a later consent (after a sync, or after another deletion), the scale may only allow one consent per connection *(V2, §8)*: the request is kept and waits for a wake where it can be the connection's first consent, i.e. one without a managed slot pending.

While deletions wait, the **Pending deletions** diagnostic sensor of the scale device shows their count and slots, and drops to 0 after the deletion, which is logged at info level. The coordinator counts the wakes that began with a deletion pending (in memory; a restart resets it). When three wakes passed without the deletion, the next wake raises the `deletion_failing` repair issue; it clears once nothing is pending.

## 5. Syncing

### 5.1 Trigger

- Watch every advertisement of the scale (per-advert callback, HA ≥ 2026.10).
- The pending-slot list (0x1D18 service data) comes only in the scan response (protocol doc §9.6). HA merges service data, so only a packet whose raw bytes hold the list counts; without raw bytes (BlueZ) the service data is taken as is.
- The wake's first slot list shows the slots pending before the wake. They wait until the weigh-in is final (§5.3): connecting during a weigh-in aborts it.
- Connect when a later slot list appends a **managed** slot: that weigh-in is final now, 4–10 s after stepping on. ([ADR 0008](adr/0008-connect-after-unrecognised-weigh-ins.md))
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
- If the scale refuses the second consent, drain them at later wakes. *(Unverified, §8.)* A refused slot goes first at its next sync: only a connection's first consent tells a wrong code apart (§6).

Every session ends before the wake does: ~47 s after the wake started, minus a small margin, even when it starts late (at ~25 s, §5.3). Only the create flow's session, which keeps the scale awake while teaching, gets a longer budget.

One session per scale at a time. A flow (create, adopt, reconfigure, repair fix flows) holds the scale from waiting for the wake to the end of its session; the coordinator doesn't queue behind it but leaves pending slots, the §5.3 check and deletions for a later wake.

### 5.3 Unrecognised weigh-ins

At ~25 s into a wake the weigh-in is final (the scale appends its slot 4–10 s after stepping on). If HA hasn't connected yet:
- Managed slots pending before the wake are synced now.
- If no managed slot is pending, connect, read the user list (no consent needed) and disconnect. ([ADR 0008](adr/0008-connect-after-unrecognised-weigh-ins.md))
- This detects a factory reset or a deleted scale user (§6).
- If the option is on, a persistent notification says that someone was weighed but not recognised (with the weight, if the advert or user list provides it; otherwise without).

**After HA's own disconnect** the scale advertises again ~1.5 s later, for 0-20 s (protocol doc §9.6). Any HA session ending counts, a flow's teaching session as much as a sync. Advertisements within 5 s of a session's end are not a wake: the 25 s moment above is not scheduled for them, so there is no second connection, user-list check, notification or deletion run. HA's merged slot list may still show the slots from before the sync; only a slot appended to a later scan response syncs, so someone stepping on right after a sync is still handled.

### 5.4 Reading policy

- **Fresh reading:** written to the scale user's entities right away. The entity state time is the sync time, seconds after the weigh-in.
- **Backlog reading** (timestamp more than 5 min older than the sync start): collected so the pending flag clears, then discarded. ([ADR 0005](adr/0005-backlog-readings-are-discarded.md))
- **Guessed reading** (custom1 byte 4 ≠ 0, on models whose FFF5 frame is custom1): discarded. ([ADR 0006](adr/0006-guessed-readings-are-discarded.md))
- Every discard logs a warning and creates one persistent notification per scale user and sync, giving the count, the time range and the reason.
- **De-duplication:** keep a short `(slot, timestamp, weight)` log in `Store`, so a reading delivered twice (e.g. after a partial sync) is never emitted twice.

## 6. Failures and repairs

| Situation | Detected by | Reaction |
|---|---|---|
| Wrong consent code | consent `20 02 04` + slot present in the user list | Fixable repair → adopt again with the code / create again / remove scale user |
| Scale user deleted on the scale | consent `20 02 04` or §5.3 check: slot missing | Fixable repair → create again / remove scale user |
| Factory reset | user list empty | Same repair, plus one automatic `unpair()` → `pair()` (the scale lost its bond keys) |
| Stale bond on the path (the scale was factory-reset) | a pairing error in any connect attempt, not only the final message | Remove the path's bond (ESPHome `bluetooth_device_unpair`, no connection needed) and connect again once; if that fails too, the pairing repair below |
| Pairing impossible on this path | pair error / `NotImplementedError` | Repair: add an ESPHome proxy or make the host adapter pairable; the text mentions a stale bond after a factory reset |
| Removed scale user not deleted for several wakes (#29) | a wake starts with the deletion still pending, three wakes in a row | Warning repair (not fixable): likely causes are passive scanning, a weak proxy or a stale bond (#22, #23). Clears when the deletions have run |
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
- Pending deletions (count, diagnostic; attribute `slots`): removed scale users not yet deleted on the scale (§4.3).

## 8. Hardware verification (done 2026-10-19, #17)

Results are in the protocol doc §9.6. Where a result breaks this design, the follow-up issue is named.

| # | Item | Result |
|---|---|---|
| V1 | An ESP32 ESPHome proxy bonds with the BF450 before the 0.5 s no-bond drop; proxy sync latency within the 47 s window | ✅ Bonds; a one-slot sync takes ~10 s, two slots ~25 s |
| V2 | One connection can consent two scale users in a row | ✅ |
| V3 | The scale stays awake while HA holds the connection for teaching, and a step-on yields a live reading; one reading teaches the scale user | ✅ |
| V4 | 0x1D18 service data and local name are visible with passive scanning | ❌ Name and UUID yes; the pending-slot list is only in the scan response (#22) |
| V5 | Frames are unchanged when the scale display is set to lb/st | ✅ Frames stay in kg, but an lb weigh-in wasn't flagged pending (#26) |
| V6 | Scale and proxy behaviour after a factory reset with an existing bond | ❌ The proxy's stale bond makes bonding fail; HA can't recover by itself (#23) |
| V7 | The ~14 s "reading final" and ~25 s thresholds hold, so no connection cuts a weigh-in short | ⚠️ The slot appears after ~10 s; connecting earlier (after a restart) aborted a weigh-in (#24); post-disconnect advertisements count as a wake (#25) |
| V8 | The minimum ESPHome version needed for pairing | ESPHome 2025.5.1 and 2026.9.1 work; the true minimum wasn't tested |

## 9. Follow-ups (not v1)

- An opt-in BlueZ pairing agent for HA OS local adapters (ADR 0002).
- Holding guessed readings and letting the user keep, move or discard each one (ADR 0006).
- A "Teach" button on the scale user device for re-teaching later.
- The BF700/BF710 protocol family.
- Long-term statistics import for backlog readings (if ADR 0005 is ever revisited).

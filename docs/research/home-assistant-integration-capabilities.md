# Home Assistant integration capabilities for Beurer BLE scales

Research date: 2026-10-16. Scope: the project's HA research brief (topics 1-11), applied to the BF450 facts in `docs/beurer-ble-protocol.md` §2-§4 and §9.

Source snapshots read (all primary; code was read only, never executed):
- home-assistant/core `dev` @ `c1fa339` (2026-10-16; `2026.11.0.dev0`). Latest release 2026.10.1. Tagged files were checked for "introduced in" versions.
- habluetooth 7.1.2 @ `b41bfe7`, bleak 3.0.2 @ `c7ca1b0`, bleak-retry-connector 4.7.1 @ `0bdeaf1`, bleak-esphome 4.1.0 @ `b2fb6bf` (HA pins 4.0.0), aioesphomeapi 46.12.1 @ `8d86534` (HA pins 46.12.0).
- esphome `dev` @ `3298988` (2026.11.0-dev), esphome-docs @ `4b5ee14`.
- BlueZ @ `fa6ba55`, Linux @ `2f2f7ee`, ESP-IDF master @ `1f8366c`.
- home-assistant/operating-system `dev` @ `7522632`, os-agent and supervisor `main` (grep only).
- developers.home-assistant @ `50b5188`, home-assistant.io @ `4d4539b`, hacs/documentation @ `d8800f6`, bodymiscale @ `c3e02d1`, xiaomi-ble @ `4dbc9f1`.
- PyPI sdists/wheels of the BLE libraries pinned by core (grep only).

Labels:
- "T*n*" refers to Topic *n* below.
- **UNVERIFIED** marks a claim not confirmed by a primary source or by a test; each one says what would confirm it.

## 1. Answers at a glance

| # | Question | Short answer | Confidence | Source |
|---|---|---|---|---|
| 1a | Can a manifest matcher match `service_data_uuid` 0x1D18? | Yes. Use the full lowercase 128-bit string `00001d18-0000-1000-8000-00805f9b34fb`, matched by key presence, not content. Add a second matcher for `0000181d-…` and/or `local_name` for other models. | High (code + docs) | [manifest docs](https://developers.home-assistant.io/docs/creating_integration_manifest#bluetooth), [match.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/match.py#L252-L275) |
| 1b | Does `async_register_callback` fire for every advert or only on changes? | Only when manufacturer data, service data, service UUIDs or name change. `async_register_advertisement_callback` (new in **HA 2026.10**) fires for every packet of one address. Discovery fires once per new field *keys*. | High | [dev docs](https://developers.home-assistant.io/docs/core/bluetooth/api#subscribing-to-every-advertisement-of-a-device), [habluetooth manager.py](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/manager.py#L1315-L1349) |
| 1c | Passive vs active scanning, scan responses? | Per adapter: Auto (default since **HA 2026.6**), Active, Passive. Integrations can request per-address active windows (default 5 min/10 s). ESPHome proxies scan actively by default. Passive receives no scan responses. Whether the BF450 puts the name and 0x1D18 data in the ADV_IND is **UNVERIFIED**. | High (HA side) / **UNVERIFIED** (BF450 side) | [HA bluetooth docs](https://www.home-assistant.io/integrations/bluetooth/#scanning-modes), [dev docs](https://developers.home-assistant.io/docs/core/bluetooth/api#requesting-on-demand-active-scanning) |
| 2a | How does `establish_connection` pick an adapter/proxy? | Every scanner that sees the address, sorted by RSSI and a path score; the first with a free slot wins. ESPHome proxies have 3 slots by default. No API to pin a path. | High / pinning **UNVERIFIED** (none found) | [wrappers.py](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/wrappers.py#L600-L686), [ESPHome proxy docs](https://esphome.io/components/bluetooth_proxy/) |
| 2b | Latency vs the 47 s window? | BlueZ measured ~5.5 s connect + ~4.6 s setup (protocol §9.4b). Default retry budget (4 attempts × 20 s connect timeout, 60 s per-attempt safety cap) exceeds the window, so cap it. Proxy latency **UNVERIFIED**. | Medium | [bleak-retry-connector](https://github.com/Bluetooth-Devices/bleak-retry-connector/blob/0bdeaf11d91aa954ee0973ba06c9aba52ddb9971/src/bleak_retry_connector/__init__.py#L104-L116) |
| 2c | Indications supported? | Yes. BlueZ (verified on BF450), and ESPHome proxies via CCCD `02 00`. | High | [bleak-esphome client.py](https://github.com/Bluetooth-Devices/bleak-esphome/blob/b2fb6bf4cb2ea4bf52284ce8664d11e3de46fdf4/src/bleak_esphome/backend/client.py#L888-L975) |
| 3a | `pair()` on local BlueZ under **HA OS**? | Expected **not** to work out of the box. HA, HA OS, os-agent and Supervisor register no BlueZ agent, and `AlwaysPairable` defaults to false. BlueZ then rejects Just Works confirmation ("No agent available") and the adapter is not bondable, so the kernel answers `PAIRING_NOTSUPP` to the scale's security request. Bonds that are created persist (`/var/lib/bluetooth` bind-mounted). Workarounds: integration-registered default agent; future habluetooth mgmt path. | Medium-high (code path) / **UNVERIFIED on a real HA OS box** | [bluez device.c](https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/device.c#L7934-L7970), [linux smp.c](https://github.com/torvalds/linux/blob/2f2f7eeef8b0a1a9b9ca998231419873ccae8d4e/net/bluetooth/smp.c#L2326-L2328), [HA OS main.conf](https://github.com/home-assistant/operating-system/blob/75226325226a46d5f567f6595f8c3c51fe9a1875/buildroot-external/rootfs-overlay/etc/bluetooth/main.conf) |
| 3b | `pair()` on local BlueZ under **Container/Core**? | Depends on the host: works with a default agent (desktop session, or one registered by the integration) or `AlwaysPairable=true` / `Pairable` on. That is the setup used for the protocol verification, proven with probe.py. Bonds persist in the host BlueZ store. | High (matches §9 observation) | [bluez main.conf](https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/main.conf#L16-L19), [bluez adapter.c](https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/adapter.c#L9508-L9524) |
| 3c | `pair()` through an **ESPHome proxy**? | Yes when the proxy advertises `FEATURE_PAIRING`: ESP32 with `active: true` (pairing since **ESPHome 2023.3.0**), RP2040/RP2350 since **2026.8.0**. Uses `bluetooth_device_pair`. ESP32 also auto-accepts the peer's security request (Just Works, IDF default auth includes bonding), and bonds persist in NVS. Not tested with BF450. | High (code) / **UNVERIFIED** (BF450 + proxy) | [bleak-esphome client.py](https://github.com/Bluetooth-Devices/bleak-esphome/blob/b2fb6bf4cb2ea4bf52284ce8664d11e3de46fdf4/src/bleak_esphome/backend/client.py#L615-L636), [bluetooth_connection_bluedroid.cpp](https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/bluetooth_connection/bluetooth_connection_bluedroid.cpp#L828-L848), [ESPHome 2023.3.0](https://esphome.io/changelog/2023.3.0/) |
| 3d | Through a **Shelly** proxy? | No. Shelly scanners are passive-only (`can_connect=False`), with no GATT, so no pairing. | High | [HA shelly docs](https://www.home-assistant.io/integrations/shelly/#bluetooth-support) |
| 3e | Do bonds persist? Per adapter? | Yes on BlueZ (HA OS bind mount) and ESP32 NVS. Bonds are **per controller**, and HA chooses the path per connection by RSSI and slots (hue_ble/automower docs document the limitation). | High | [hue_ble docs](https://www.home-assistant.io/integrations/hue_ble/), [var-lib-bluetooth.mount](https://github.com/home-assistant/operating-system/blob/75226325226a46d5f567f6595f8c3c51fe9a1875/buildroot-external/rootfs-overlay/usr/lib/systemd/system/var-lib-bluetooth.mount) |
| 3f | Which core integrations pair? | Through their libraries: `hue_ble` (HueBLE, registers its own BlueZ agent), `husqvarna_automower_ble`, `dormakaba_dkey` (docs: ESPHome proxies only), `specialized_turbo`. | High | [dormakaba_dkey docs](https://www.home-assistant.io/integrations/dormakaba_dkey/), [HueBLE 2.2.3](https://pypi.org/project/HueBLE/2.2.3/) |
| 4 | Do the BT coordinators fit "connect only when the advert lists our slot"? | Yes. `needs_poll_method(service_info, …)` inspects the 0x1D18 payload, and `poll_method` runs the session (debounced, immediate, 10 s cooldown). Processor coordinators model latest values only, so for batches use the plain Active coordinator or a custom session manager. | High | [active_update_coordinator.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/active_update_coordinator.py#L80-L170) |
| 5a | Bluetooth discovery step? | `async_step_bluetooth(BluetoothServiceInfoBleak)`: set unique_id, abort if configured, confirm with the user. Fires once per address. | High | [config flow docs](https://developers.home-assistant.io/docs/core/integration/config_flow#discovery-steps) |
| 5b | Multi-step forms? | Yes: forms, menus, chaining, `next_flow`. | High | [data entry flow docs](https://developers.home-assistant.io/docs/data_entry_flow_index) |
| 5c | `async_show_progress` for "step on the scale", wait limits? | Yes, with `progress_task` (HA ≥ 2024.2) and `async_update_progress`. **No framework timeout.** The task is cancelled when the flow is removed, but a closed browser tab doesn't remove it, so add your own timeout and never hold the BLE link indefinitely. `teslemetry`'s BLE subentry flow is the model. | High | [data_entry_flow.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/data_entry_flow.py#L809-L880), [teslemetry config_flow.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/teslemetry/config_flow.py#L231-L540) |
| 5d | Options and reconfigure flows? | Both, for entries and for subentries. A config-entry update listener combined with reload helpers is deprecated from 2026.6 and errors in 2026.12. | High | [config flow docs § Reconfigure](https://developers.home-assistant.io/docs/core/integration/config_flow#reconfigure), [blog](https://developers.home-assistant.io/blog/2026/05/07/config-entry-listener-together-with-reloading-methods) |
| 5e | Config subentries: since when, custom OK, devices/entities? | Since **HA 2025.3**, usable by custom integrations. No discovery source; `user`/`reconfigure` only. Entities via `async_add_entities(..., config_subentry_id=…)`. Since **2026.8** a device belongs to one entry and at most one subentry, so use one device per person-subentry, linked with `via_device_id`. Child devices (2026.9) must share the subentry; `via_device_id` may cross subentries. Removing a subentry removes its devices and entities. | High | [blog 2025-02-16](https://developers.home-assistant.io/blog/2025/02/16/config-subentries), [blog 2026-07-21](https://developers.home-assistant.io/blog/2026/07/21/device-registry-single-config-entry), [device registry docs](https://developers.home-assistant.io/docs/device_registry_index) |
| 5f | Start a flow from code or an action? | `flow.async_init` / `subentries.async_init` work, but the user only sees them when chained via `next_flow` (from a flow or a **repair fix flow**). `discovery_flow.async_create_flow` raises a visible *config* flow. Opening a dialog from an action: none found (**UNVERIFIED**). | High / partial | [config flow docs](https://developers.home-assistant.io/docs/core/integration/config_flow#use-async_on_create_entry), [repairs docs](https://developers.home-assistant.io/docs/core/platform/repairs) |
| 6a | Devices vs entities, `via_device`? | One device per person-subentry plus the scale device. `via_device` is deprecated (2026.8) in favour of `via_device_id`. Child devices are for parts of one product. | High | [device registry docs](https://developers.home-assistant.io/docs/device_registry_index) |
| 6b | Device classes/units/state classes per metric? | WEIGHT/kg/MEASUREMENT for masses. `%` with no class for fat/water/muscle. No class for BMI, visceral fat and metabolic age. BMR/AMR `kcal` with **no** class (ENERGY demands TOTAL*). Impedance `Ω` with no class. TIMESTAMP for measured-at. | High | [sensor/const.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/sensor/const.py#L669-L892) |
| 6c | Link entities to HA `person`? | No built-in relation and no person selector. Use `EntitySelector(domain="person")` and store the entity-registry id in subentry data. | High | [selector.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/selector.py) |
| 7a | Record states with a past timestamp? | Not via the entity API (always "now"). The low-level `states.async_set(timestamp=…)` exists but is unsupported for this; effects **UNVERIFIED**. | High (API) | [entity.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/entity.py#L1335-L1344) |
| 7b | Long-term statistics import? | `async_add_external_statistics` (`domain:id`, used by opower, tibber and 8 others) or `async_import_statistics` (entity id, source `recorder`, only kitchen_sink). Hourly, top-of-hour, upsert. `unit_class` + `mean_type` required by 2026.11. | High | [recorder/statistics.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/recorder/statistics.py#L2860-L2951), [blog](https://developers.home-assistant.io/blog/2025/10/16/recorder-statistics-api-changes) |
| 7c | EventEntity / bus events with the original timestamp? | EventEntity state time = now, but strictly increasing per event since **2026.8**. Put `measured_at` in event attributes. Bus `async_fire(time_fired=…)` exists; back-dating it is undocumented (**UNVERIFIED**). | High | [event/__init__.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/event/__init__.py#L154-L168) |
| 7d | `measured_at` attributes? | Fine. A changing attribute forces a new state row even for an identical weight. Plus a per-person TIMESTAMP sensor. | High | [core.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/core.py#L2465-L2500) |
| 7e | Several readings in the same second? | EventEntity keeps them distinct (+1 ms). Sensor writes are separate states at sync time. Statistics collapse per hour. | High | as 7b/7c |
| 8a | Actions with response data? | `SupportsResponse.OPTIONAL/ONLY`, JSON dict. Register platform entity services in `async_setup` (2025-09 change). | High | [services docs](https://developers.home-assistant.io/docs/dev_101_services#response-data) |
| 8b | Buttons, select/number/text for profile? | All available (also `date`). Writes must be queued until the next consented session. | High | [button docs](https://developers.home-assistant.io/docs/core/entity/button) |
| 8c | Persistent notifications? | `persistent_notification.async_create(hass, message, title, notification_id)`. | High | [helpers.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/persistent_notification/helpers.py#L53) |
| 8d | Repair issues with fix flows? | `ir.async_create_issue(is_fixable=True, data=…)` plus `RepairsFlow`. Can forward to subentry reconfigure via `next_flow`. Good for reset / deleted slot / bad code / bond lost. | High | [repairs docs](https://developers.home-assistant.io/docs/core/platform/repairs) |
| 9 | Storage for codes, state, restore? | Codes in subentry data (plaintext `.storage/core.config_entries`, normal for credentials). `Store` (`private=True`) for user-list snapshot, de-dup log, queued writes and pending guesses. `RestoreSensor` for per-person metrics (restore keyed by registry id from 2026.11). | High | [storage.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/storage.py#L292-L320), [sensor docs](https://developers.home-assistant.io/docs/core/entity/sensor) |
| 10 | HACS / manifest / quality scale? | HACS: one `custom_components/<domain>`, manifest keys incl. `version`, `brand/icon.png`, `hacs.json`, HACS + hassfest actions and a release for the default store. Core path: put the protocol in a PyPI library (`dependency-transparency`, Bronze); Gold needs discovery, repairs, reconfigure, stale-devices, diagnostics. | High | [HACS publish](https://hacs.xyz/docs/publish/integration), [quality scale rules](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules) |
| 11a | How do `xiaomi_ble` and other core BLE scales separate users? | They don't. `xiaomi_ble` exposes latest weight/impedance plus a `profile_id` sensor on newer scales. `eufylife_ble` has no users. | High | [xiaomi_ble sensor.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/xiaomi_ble/sensor.py#L195-L220) |
| 11b | Withings? | One config entry per Withings account (unique_id = userid). The cloud assigns users. Latest-value sensors; first sync looks back 14 days; no history import. | High | [withings config_flow.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/withings/config_flow.py#L59-L62) |
| 11c | bodymiscale? | One config entry per person. Assignment by weight range, nearest weight ± tolerance, interactive push notification, scale profile ID, or manual. | High | [bodymiscale README](https://github.com/dckiller51/bodymiscale/blob/c3e02d196f885474f5c5e937f18f0e818d8da36c/README.md#configuration) |

## 2. Topics

### Topic 1: Passive listening and discovery

**Manifest matchers.** A `bluetooth` matcher in `manifest.json` may contain `connectable`, `local_name`, `service_uuid`, `service_data_uuid`, `manufacturer_id` and `manufacturer_data_start`. All keys of one matcher must match, and any matcher in the list can trigger discovery ([dev docs, manifest § Bluetooth](https://developers.home-assistant.io/docs/creating_integration_manifest#bluetooth); schema in [hassfest manifest.py L256-L270](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/script/hassfest/manifest.py#L256-L270), which also requires UUID strings to be lowercase). `service_data_uuid` is matched by **key membership** in `service_info.service_data` ([match.py L252-L275](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/match.py#L252-L275), [L399-L420](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/match.py#L399-L420)). The key is the full 128-bit string that bleak reports, so for the BF450 the matcher would be `{"service_data_uuid": "00001d18-0000-1000-8000-00805f9b34fb"}`. To cover models that advertise it correctly, add a second matcher with `0000181d-…`. Matching on `local_name` (`"BF4*"`, `"BF6*"` and so on, fnmatch patterns) also works ([match.py L440-L450](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/match.py#L440-L450)). The matcher can't look at the *content* of the service data.

**Discovery fires once per new field set, not once per change.** The integration matcher remembers for each address which manufacturer-data, service-data and service-UUID *keys* and which name it has seen. It skips the matchers when nothing new has appeared ([match.py L72-L96, L123-L161](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/match.py#L72-L161)). The dev docs say the same: "It only checks if the UUIDs have been seen before, not whether their content has changed" ([dev docs, bluetooth API](https://developers.home-assistant.io/docs/core/bluetooth/api#clearing-match-history-for-rediscovery)). So a config-flow discovery for the scale fires once, not on every weigh-in.

**Runtime callbacks.**
- `bluetooth.async_register_callback(hass, cb, BluetoothCallbackMatcher, mode)` accepts the same keys as the manifest plus `address` ([match.py L36-L46](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/match.py#L36-L46)). It fires **only when the advertisement data changes**. habluetooth returns early when manufacturer data, service data, service UUIDs and name all equal the previous advert ([habluetooth manager.py L1315-L1349](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/manager.py#L1315-L1349); [dev docs](https://developers.home-assistant.io/docs/core/bluetooth/api#subscribing-to-every-advertisement-of-a-device): "`bluetooth.async_register_callback` only fires when the advertisement data of a device changes"). For the BF450 that's enough: the payload of 0x1D18 changes from empty to `01 02` when a reading becomes final, and that change fires the callback.
  - Caveat: the flag survives sleep. If the scale wakes with exactly the payload of its last advert before sleeping, nothing has "changed" until that history entry is evicted. habluetooth runs the unavailable sweep every 5 min (`UNAVAILABLE_TRACK_SECONDS`). It evicts an address once it has dropped out of the scanners' device lists and, for the non-connectable pass, has been silent for longer than its learned advert interval + 5 s (the interval is learned after 16 adverts), or 15 min as a fallback ([habluetooth manager.py L532-L600](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/manager.py#L532-L600); [const.py L27, L36, L104](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/const.py#L27-L36); [advertisement_tracker.py L10-L16](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/advertisement_tracker.py#L10-L16)). A scale that slept for a few minutes therefore usually comes back as "new". A quick re-wake (a second family member right after the first) may not, until the slot list actually changes, which it does when a new reading becomes final. That is enough for "new reading pending". It is not enough for "a flow is waiting for the scale to wake", which should use the per-packet callback below. **UNVERIFIED in practice**; this is from code reading only.
- **New in HA 2026.10:** `bluetooth.async_register_advertisement_callback(hass, cb, address)` fires on **every** advertisement from one address, unchanged ones included ([api.py L222-L233](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/api.py#L222-L233); [dev docs](https://developers.home-assistant.io/docs/core/bluetooth/api#subscribing-to-every-advertisement-of-a-device)). It comes from habluetooth 7.1.0 ("per address advertisement callbacks that fire for every packet", [CHANGELOG](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/CHANGELOG.md#v710-2026-09-22)). It is absent in 2026.9.0 and present in 2026.10.0 ([2026.9.0 api.py](https://github.com/home-assistant/core/blob/2026.9.0/homeassistant/components/bluetooth/api.py), [2026.10.0 api.py](https://github.com/home-assistant/core/blob/2026.10.0/homeassistant/components/bluetooth/api.py)). Use it to detect "the scale is awake" reliably, independent of the change-only semantics.
- `bluetooth.async_process_advertisements(...)` waits until a predicate is true or a timeout expires ([api.py L174-L205](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/api.py#L174-L205)). It is useful inside a config flow for "wait until the scale wakes up".

**Passive vs active scanning.**
- Since **HA 2026.6** (habluetooth ≥ 6.3.0) each adapter has three modes: **Auto** (recommended default: passive, with short on-demand active windows), **Active** and **Passive** ([HA bluetooth docs § Scanning modes](https://www.home-assistant.io/integrations/bluetooth/#scanning-modes); [habluetooth v6.3.0 "Add auto scanning mode with on-demand active windows"](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/CHANGELOG.md#v630-2026-05-22); the `AUTO` option first appears in [2026.6.0 config_flow.py](https://github.com/home-assistant/core/blob/2026.6.0/homeassistant/components/bluetooth/config_flow.py)). Passive and Auto on Linux need BlueZ ≥ 5.63 with experimental features (HA OS ≥ 9.4) ([HA bluetooth docs](https://www.home-assistant.io/integrations/bluetooth/#scanning-modes)). HA OS ships `Experimental=true` in `/etc/bluetooth/main.conf` ([HA OS main.conf](https://github.com/home-assistant/operating-system/blob/75226325226a46d5f567f6595f8c3c51fe9a1875/buildroot-external/rootfs-overlay/etc/bluetooth/main.conf)).
- An integration can opt one **address** into Auto-mode active windows: call `async_register_callback` with `{"address": …}`, a mode other than PASSIVE, and optional `scan_interval` / `scan_duration`. The defaults are 5 min / 10 s ([dev docs § Requesting on-demand active scanning](https://developers.home-assistant.io/docs/core/bluetooth/api#requesting-on-demand-active-scanning); [api.py L142-L171](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/api.py#L142-L171)). habluetooth caps an Auto active window at 35 s ([v6.13.0](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/CHANGELOG.md#v6130-2026-06-23)). `bluetooth.async_request_active_scan(hass, duration)` runs a one-shot sweep, intended for config flows ([api.py L327-L340](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/api.py#L327-L340)).
- **Scan responses.** In active mode the scanner requests scan-response data. In passive mode only the primary advertising PDU arrives ([HA docs § About Bluetooth terminology](https://www.home-assistant.io/integrations/bluetooth/#scanning-mode); [ESPHome bluetooth_proxy § Passive vs Active Scanning](https://esphome.io/components/bluetooth_proxy/#passive-vs-active-scanning)). habluetooth merges the latest data per address into one `BluetoothServiceInfoBleak`.
- ESPHome proxies scan **actively by default** (`esp32_ble_tracker: scan_parameters: active: true`) ([esp32_ble_tracker docs](https://esphome.io/components/esp32_ble_tracker/)). HA can switch the mode at runtime on proxies that advertise `FEATURE_STATE_AND_MODE` ([bluetooth_proxy.h L48-L57, L208-L222](https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/bluetooth_proxy/bluetooth_proxy.h#L48-L57)). Shelly scripts scan passively or actively according to the Shelly integration's option; Auto mode toggles the script through `Script.Eval` ([HA shelly bluetooth/__init__.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/shelly/bluetooth/__init__.py); [aioshelly 13.35.0 `async_set_active_mode`](https://pypi.org/project/aioshelly/13.35.0/)).
- **BF450 specifics, UNVERIFIED.** All protocol captures used active scanning (protocol doc §2). Two things are unknown: whether the local name (`BF450`, AD 0x09) is in the ADV_IND or only in the SCAN_RSP, and whether the 0x16 service data is in the ADV_IND. If either sits only in the scan response, a passive or Auto adapter misses it outside an active window. To check: a passive bleak scan on the host (BlueZ AdvertisementMonitor), or `btmon` on a host with root, while weighing; compare the AD structures of the ADV_IND and SCAN_RSP PDUs.

### Topic 2: Active connections

**Getting a connectable device and connecting.** Use `bluetooth.async_ble_device_from_address(hass, addr, connectable=True)` and then `bleak_retry_connector.establish_connection(BleakClientWithServiceCache, device, name, …)` ([dev docs, bluetooth API § Fetching the bleak BLEDevice](https://developers.home-assistant.io/docs/core/bluetooth/api#fetching-the-bleak-bledevice-from-the-address)). Inside HA, `bleak.BleakClient` is patched to `HaBleakClientWrapper`. If it is not called through `establish_connection`, it logs a warning ([wrappers.py L431-L447](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/wrappers.py#L431-L447)).

**Adapter/proxy selection and slots.** At connect time the wrapper takes every scanner that currently sees the address and sorts them by RSSI, then by a connection-path score (failures, in-progress connections, free slots). It uses the first scanner with a free slot ([wrappers.py L600-L686](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/wrappers.py#L600-L686)).
- Local adapters allocate a slot in the manager's slot manager ([wrappers.py L567-L598](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/wrappers.py#L567-L598)).
- Remote scanners must have a `connector` whose `can_connect()` is true.
- ESPHome proxies default to **3** connection slots (`connection_slots`) ([ESPHome bluetooth_proxy docs § How Active Connections Work](https://esphome.io/components/bluetooth_proxy/#how-active-connections-work)). bleak-esphome waits for a free slot before it connects ([bleak-esphome client.py L455-L475](https://github.com/Bluetooth-Devices/bleak-esphome/blob/b2fb6bf4cb2ea4bf52284ce8664d11e3de46fdf4/src/bleak_esphome/backend/client.py#L455-L475)).
- **No public API pins a connection to one specific adapter or proxy.** The choice is per attempt and RSSI-driven. **UNVERIFIED**: no supported pinning API was found. This matters for bonding (Topic 3).

**Timeouts vs the 47 s window.** `establish_connection` defaults to `max_attempts=4`, `BLEAK_TIMEOUT=20 s` per connect call and a per-attempt `BLEAK_SAFETY_TIMEOUT` of 60 s, plus backoff between attempts ([bleak-retry-connector `__init__.py` L104-L116, L451-L560](https://github.com/Bluetooth-Devices/bleak-retry-connector/blob/0bdeaf11d91aa954ee0973ba06c9aba52ddb9971/src/bleak_retry_connector/__init__.py#L104-L116)). The worst case is far longer than the scale's 47 s awake window, so pass a lower `max_attempts` and wrap the whole session in `asyncio.timeout(~40)`.
- Measured on the BF450 over local BlueZ (protocol doc §9.4b): connect plus service resolution takes ~5.5 s with an existing bond (~2 s fresh); set time plus 10 subscriptions ~4.6 s; consent → first frame 0.4 s. That leaves roughly 30 s of margin when the connect starts within a few seconds of the "slot appended" advert.
- With `use_services_cache` / `BleakClientWithServiceCache` the GATT table is cached ([bleak-retry-connector usage docs](https://github.com/Bluetooth-Devices/bleak-retry-connector/blob/0bdeaf11d91aa954ee0973ba06c9aba52ddb9971/docs/source/usage.md)), and ESPHome proxies cache services on the host (`REMOTE_CACHING`) ([bleak-esphome client.py L477-L484](https://github.com/Bluetooth-Devices/bleak-esphome/blob/b2fb6bf4cb2ea4bf52284ce8664d11e3de46fdf4/src/bleak_esphome/backend/client.py#L477-L484)).
- Proxy latency for the BF450 is **UNVERIFIED** (not measured).

**Indications.** bleak's `start_notify` covers notify and indicate. On BlueZ it uses `StartNotify`/`AcquireNotify`, and the protocol doc §9.2 confirms that all BF450 indicate-only characteristics worked. bleak-esphome writes CCCD `02 00` when the characteristic has `indicate` but not `notify` ([bleak-esphome client.py L68-L71, L888-L975](https://github.com/Bluetooth-Devices/bleak-esphome/blob/b2fb6bf4cb2ea4bf52284ce8664d11e3de46fdf4/src/bleak_esphome/backend/client.py#L888-L975)), so indications work through ESPHome proxies.

**Shelly cannot connect.** The Shelly scanner is registered with `can_connect=lambda: False` and `connectable=False` ([aioshelly 13.35.0 `ble/__init__.py` `create_scanner`](https://pypi.org/project/aioshelly/13.35.0/)). The HA Shelly docs say "Shelly devices do **not** support proxying active (GATT) connections" ([HA shelly docs § Bluetooth Support](https://www.home-assistant.io/integrations/shelly/#bluetooth-support)). If only Shelly scanners see the device, habluetooth raises "Shelly devices are passive-only and cannot connect" ([wrappers.py L655-L670](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/wrappers.py#L655-L670)). Shelly's own script API has `BLE.Scanner`, `BLE.GAP` parsing and advertising helpers, but no GATT client ([Shelly Script BLE API](https://shelly-api-docs.shelly.cloud/gen2/Scripts/APIs/BLE)).

### Topic 3: Bonding / pairing (critical)

#### T3.1 The API surface
- `BleakClient(..., pair=True)` (bleak ≥ 1.0) pairs **before** connecting, if the device isn't paired yet. `BleakClient.pair()` pairs an already connected client ([bleak `__init__.py` L568-L611, L758-L784](https://github.com/hbldh/bleak/blob/c7ca1b04b00b0bda5f98ccf9493e24784cfe869d/bleak/__init__.py#L568-L611)). HA pins bleak 3.0.2 ([bluetooth manifest](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/manifest.json)).
- `establish_connection(..., pair=False)` forwards `pair` to the client constructor ([bleak-retry-connector L451-L524](https://github.com/Bluetooth-Devices/bleak-retry-connector/blob/0bdeaf11d91aa954ee0973ba06c9aba52ddb9971/src/bleak_retry_connector/__init__.py#L451-L524)).
- `HaBleakClientWrapper` stores `pair` and passes it to whichever backend it picks: BlueZ D-Bus for local adapters, `ESPHomeClient` for proxies ([wrappers.py L308-L333, L503-L508](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/wrappers.py#L308-L333)). `pair()` and `unpair()` are inherited from `BleakClient` and go to that backend.

#### T3.2 Local BlueZ adapter (HA OS, Container, Core)
HA core still builds the **bleak/D-Bus** `HaScanner` for local adapters ([bluetooth/__init__.py L427](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/__init__.py#L427)). Pairing on a local adapter therefore means BlueZ's `Device1.Pair()`.

Code path, verified in source:
1. bleak BlueZ `connect(pair=True)` calls `Device1.Pair` instead of `Connect` when the device isn't paired. `_pair()` first sets `Trusted=true` and then calls `Pair` ([bleak bluezdbus/client.py L250-L275, L478-L516](https://github.com/hbldh/bleak/blob/c7ca1b04b00b0bda5f98ccf9493e24784cfe869d/bleak/backends/bluezdbus/client.py#L250-L275)). bleak registers **no** BlueZ agent: a grep for `Agent1`/`RegisterAgent` in bleak returns nothing.
2. BlueZ `pair_device()` uses the caller's agent, else the **default** agent. With no agent it uses IO capability `NoInputNoOutput` ([bluez src/device.c L3410-L3468](https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/device.c#L3410-L3468); [agent.c `agent_get` L247-L261](https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/agent.c#L247-L261)).
3. The kernel asks user space to confirm every Just Works pairing (`mgmt_user_confirm_request(..., confirm_hint=1)`) ([linux net/bluetooth/smp.c L846-L877](https://github.com/torvalds/linux/blob/2f2f7eeef8b0a1a9b9ca998231419873ccae8d4e/net/bluetooth/smp.c#L846-L877)). bluetoothd handles this in `device_confirm_passkey()`. It calls `new_auth()` first, which **fails with "No agent available" when there is neither a bonding agent nor a default agent**. Only after that does it auto-accept, because "the client has indicated the intent to pair" ([device.c L7858-L7883](https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/device.c#L7858-L7883), [L7934-L7970](https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/device.c#L7934-L7970)). A failed `new_auth` makes bluetoothd send a negative confirm reply ([adapter.c L8437-L8471](https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/adapter.c#L8437-L8471)).
4. Bonding (as opposed to pairing without key storage) also needs the controller to be **bondable**.
   - With the mgmt interface the kernel clears `HCI_BONDABLE` and user space must enable it ([linux mgmt.c L1149-L1154](https://github.com/torvalds/linux/blob/2f2f7eeef8b0a1a9b9ca998231419873ccae8d4e/net/bluetooth/mgmt.c#L1149-L1154)).
   - Without `HCI_BONDABLE` the kernel strips the bonding flag from our own pairing request ([smp.c L634-L641](https://github.com/torvalds/linux/blob/2f2f7eeef8b0a1a9b9ca998231419873ccae8d4e/net/bluetooth/smp.c#L634-L641)). It also answers a peer's Security Request that asks for bonding with **`SMP_PAIRING_NOTSUPP`** ([smp.c L2326-L2328](https://github.com/torvalds/linux/blob/2f2f7eeef8b0a1a9b9ca998231419873ccae8d4e/net/bluetooth/smp.c#L2326-L2328)). This is exactly the "pairing not supported → scale drops link after 0.5 s" behaviour in protocol doc §3.
   - bluetoothd makes the adapter bondable in three cases: `AlwaysPairable=true` in main.conf (default **false**, "Always allow pairing even if there are no agent registered") ([bluez main.conf L16-L19](https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/main.conf#L16-L19); [adapter.c L10922-L10924](https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/adapter.c#L10922-L10924)); a **default agent** is registered ([adapter.c `adapter_set_io_capability` L9508-L9524](https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/adapter.c#L9508-L9524), called through `add_default_agent` in [agent.c L139-L154](https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/agent.c#L139-L154)); or someone sets `Adapter1.Pairable=true` ([org.bluez.Adapter.rst L389-L395](https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/doc/org.bluez.Adapter.rst)).
   - When the last default agent goes away, bondable is switched off again.
   - A plain `RegisterAgent` without `RequestDefaultAgent` does **not** make an agent the default ([agent.c `register_agent` L947-L977](https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/agent.c#L947-L977)).

What each install type provides:
- **HA OS.** `/etc/bluetooth/main.conf` contains only `Experimental=true`, `TemporaryTimeout=195` and `AutoEnable=true`. There is no `AlwaysPairable` ([HA OS main.conf](https://github.com/home-assistant/operating-system/blob/75226325226a46d5f567f6595f8c3c51fe9a1875/buildroot-external/rootfs-overlay/etc/bluetooth/main.conf)). No component of HA OS, os-agent, Supervisor, HA core `bluetooth`, habluetooth, bleak-retry-connector or bluetooth-auto-recovery registers a BlueZ agent or sets `Pairable`: a grep of each source tree for `RegisterAgent`/`AgentManager1`/`Pairable` was empty. Only the experimental habluetooth mgmt path below handles confirmations.
  - **Verdict for stock HA OS:** `pair=True` on a local adapter is expected to **fail** for a Just Works peer (no agent → confirm rejected; adapter not bondable). The BF450 additionally drops unbonded links. Confidence: medium-high, from code reading plus the matching §3/§9 observation on a desktop BlueZ host. **UNVERIFIED on a real HA OS box**: needs a test install.
  - Bonds that do get created persist across reboots. HA OS bind-mounts `/var/lib/bluetooth` from the overlay partition ([var-lib-bluetooth.mount](https://github.com/home-assistant/operating-system/blob/75226325226a46d5f567f6595f8c3c51fe9a1875/buildroot-external/rootfs-overlay/usr/lib/systemd/system/var-lib-bluetooth.mount)). The weekly `haos-bt-cache` job deletes only `…/cache` entries, not bond `info` files ([haos-bt-cache.service](https://github.com/home-assistant/operating-system/blob/75226325226a46d5f567f6595f8c3c51fe9a1875/buildroot-external/rootfs-overlay/usr/lib/systemd/system/haos-bt-cache.service)).
- **HA Container / Core.** These use the host's bluetoothd over `/run/dbus` ([HA bluetooth docs § Requirements for Linux systems](https://www.home-assistant.io/integrations/bluetooth/#requirements-for-linux-systems)). Whether pairing works depends on the host. A desktop session agent (GNOME/KDE) makes the adapter bondable and acts as the default agent. A headless host needs `AlwaysPairable=true` or an agent. This matches the setup used for the protocol verification: host `Pairable: yes`, plus `probe.py`, which registers a `NoInputNoOutput` agent and calls `RequestDefaultAgent` (`tools/probe.py`, lines 70-102). Bonds live in the host's `/var/lib/bluetooth` and persist.
- **Workaround inside the integration (prior art in core).** The `hue_ble` library HueBLE 2.2.3 detects the BlueZ backend and registers its own `org.bluez.Agent1` over a separate D-Bus connection for the duration of `pair()` ("Workaround for bluez authentication issues"). It calls `RegisterAgent(path, "KeyboardDisplay")` but **not** `RequestDefaultAgent` ([HueBLE 2.2.3 on PyPI](https://pypi.org/project/HueBLE/2.2.3/), `HueBLE.py` `_pair_bluez`). By the BlueZ code above, a non-default agent on a different bus connection is not found by `agent_get(sender)`, so it is unclear how this helps on HA OS. **UNVERIFIED**: check the HueBLE repo/issues or test. A Beurer integration could do the same thing more completely: register a `NoInputNoOutput` agent **and** call `RequestDefaultAgent` while pairing, then release it. That is what probe.py does successfully. Side effects: it temporarily becomes the system's default agent and makes the adapter bondable (global settings), it needs write access to the system bus (HA already has it for bleak), and it is a BlueZ-only code path a core reviewer may question.
- **Future: habluetooth's DBus-free mgmt/L2CAP client** (habluetooth 6.10-6.26, June 2026). It does its own `PAIR_DEVICE` over the mgmt socket, auto-confirms Just Works ("confirm_hint != 0 … accept it") and bonds on connect ([client_mgmt.py L467-L500](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/client_mgmt.py#L467-L500); [CHANGELOG v6.26.0 "Bond on connect, encrypt bonded reconnects, and auto-confirm just works pairing"](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/CHANGELOG.md#v6260-2026-06-25)). It is explicitly **experimental and not wired into HA**: "Home Assistant still constructs the bleak-backed `HaScanner` until this wiring lands". Its LTKs are held in memory and must be persisted by HA with a `Store`. It must not co-manage an `hciN` that bluetoothd manages ([habluetooth docs/home_assistant_integration.md](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/docs/home_assistant_integration.md), [docs/bond_persistence.md](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/docs/bond_persistence.md)). Not usable today. Watch it, because it would fix local bonding on HA OS.

#### T3.3 ESPHome Bluetooth proxy
- **Protocol/API.** `bleak-esphome` `ESPHomeClient.pair()` sends `bluetooth_device_pair` (aioesphomeapi `BluetoothDeviceRequestType.PAIR`, 30 s default timeout) ([bleak-esphome client.py L615-L636](https://github.com/Bluetooth-Devices/bleak-esphome/blob/b2fb6bf4cb2ea4bf52284ce8664d11e3de46fdf4/src/bleak_esphome/backend/client.py#L615-L636); [aioesphomeapi client.py L1442-L1465](https://github.com/esphome/aioesphomeapi/blob/8d86534f49089f9bd80d50884be012c119700273/aioesphomeapi/client.py#L1442-L1465)). It raises `NotImplementedError` unless the proxy advertises the `PAIRING` feature flag (bit 3). With `pair=True`, bleak-esphome connects first and then pairs, **before** service discovery ([client.py L555-L560](https://github.com/Bluetooth-Devices/bleak-esphome/blob/b2fb6bf4cb2ea4bf52284ce8664d11e3de46fdf4/src/bleak_esphome/backend/client.py#L555-L560)). `unpair()` exists too, but bleak-esphome only lets you call it **while connected** ([client.py L638-L655](https://github.com/Bluetooth-Devices/bleak-esphome/blob/b2fb6bf4cb2ea4bf52284ce8664d11e3de46fdf4/src/bleak_esphome/backend/client.py#L638-L655)).
- **Firmware.** Proxy pairing came with **ESPHome 2023.3.0** ("implement pairing for bluetooth proxy", [esphome#4475](https://github.com/esphome/esphome/pull/4475); [ESPHome 2023.3.0 changelog](https://esphome.io/changelog/2023.3.0/)). The bleak-esphome docstring says "ESPHome 2024.3.0 or newer". The two primary sources disagree; the feature flag is what actually counts. In current ESPHome the flag is set when `active: true` and the platform backend supports pairing: **ESP32 (Bluedroid) yes; RP2040/RP2350 (Pico W, BTstack) yes since ESPHome 2026.8.0** ("Explicit pairing for rp2", esphome#18166); other platforms no ([bluetooth_connection.h L51-L65](https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/bluetooth_connection/bluetooth_connection.h#L51-L65); [bluetooth_proxy.h L208-L236](https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/bluetooth_proxy/bluetooth_proxy.h#L208-L236); [ESPHome 2026.8.0 changelog](https://esphome.io/changelog/2026.8.0/)). HA 2026.10 ships aioesphomeapi 46.12.0 and bleak-esphome 4.0.0 ([esphome manifest](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/esphome/manifest.json)).
- **Peer-initiated security is auto-accepted on ESP32.** On `ESP_GAP_BLE_SEC_REQ_EVT` the Bluedroid backend "Always accept[s]" by calling `esp_ble_gap_security_rsp(addr, true)` ([bluetooth_connection_bluedroid.cpp L828-L848](https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/bluetooth_connection/bluetooth_connection_bluedroid.cpp#L828-L848)). The default IO capability is `none` ([esp32_ble docs](https://esphome.io/components/esp32_ble/); [ble.h L244](https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/esp32_ble/ble.h#L244)), so pairing is Just Works. ESPHome leaves `auth_req_mode` at the ESP-IDF default unless configured, and that default is `BTA_LE_AUTH_REQ_SC_MITM_BOND`, which includes the **bond** flag ([esp-idf bta_dm_co.c L41-L53](https://github.com/espressif/esp-idf/blob/1f8366c26e56b8066ce243624f94905be175f7ae/components/bt/host/bluedroid/bta/dm/bta_dm_co.c#L41-L53)). The RP2 BTstack backend auto-confirms Just Works too (`sm_just_works_confirm`) ([bluetooth_connection_rp2.cpp L216-L235](https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/bluetooth_connection/bluetooth_connection_rp2.cpp#L216-L235)). The BF450 asks for bonding right after connecting, so an ESP32 proxy should bond **automatically, even without HA calling `pair()`**. Calling `pair()` afterwards returns "paired" because `is_paired()` short-circuits it ([bluetooth_proxy.cpp L388-L405](https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/bluetooth_proxy/bluetooth_proxy.cpp#L388-L405)). **UNVERIFIED against the real scale**, including whether the proxy's own service discovery finishes before the scale's 0.5 s no-bond drop (it should not apply once encryption starts).
- **Persistence.** ESPHome's recommended framework is **ESP-IDF 5.5.5** ([esphome esp32/__init__.py L1209-L1213](https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/esp32/__init__.py#L1209-L1213)). In that release, Bluedroid stores SMP bond keys in NVS by default (`BT_BLE_SMP_BOND_NVS_FLASH`, default y) and keeps up to 15 bonds (`BT_SMP_MAX_BONDS`) ([esp-idf v5.5.5 Kconfig.in L478-L481, L1353-L1357](https://github.com/espressif/esp-idf/blob/v5.5.5/components/bt/host/bluedroid/Kconfig.in#L478-L481)). The default auth request is `BTA_LE_AUTH_REQ_SC_MITM_BOND` ([v5.5.5 bta_dm_co.c L41-L43](https://github.com/espressif/esp-idf/blob/v5.5.5/components/bt/host/bluedroid/bta/dm/bta_dm_co.c#L41-L43)). ESPHome's `esp32_ble`, `bluetooth_proxy` and `bluetooth_connection` components set none of these options; a grep for `SMP`/`BOND` sdkconfig keys returns nothing. So bonds survive proxy reboots, as long as the user does not erase flash.
- **Re-pair after a scale factory reset.** The scale forgets its key and the proxy still holds the old LTK. ESP-IDF *master* (not 5.5.5) adds `BT_BLE_SMP_HARDENED_REPAIRING` (default y), which refuses a re-pair that is *weaker* than the stored bond. A Just Works → Just Works re-pair is not weaker ([esp-idf master Kconfig.in L560-L580](https://github.com/espressif/esp-idf/blob/1f8366c26e56b8066ce243624f94905be175f7ae/components/bt/host/bluedroid/Kconfig.in#L560-L580)). How Bluedroid 5.5.5 reacts when the peer has lost the key (encryption failure on reconnect) is **UNVERIFIED**. The safe recipe: detect the reset (FFF1, see §3), connect, call `unpair()` (bleak-esphome requires an active link) and pair again.
- **Prior art.** The HA `dormakaba_dkey` docs say the lock "is currently not working with USB dongles or built-in Bluetooth radios, only ESPHome Bluetooth proxies configured to allow active connections work reliably". Its library calls `client.pair()` after `establish_connection` ([HA dormakaba_dkey docs](https://www.home-assistant.io/integrations/dormakaba_dkey/); [py-dormakaba-dkey 1.0.6](https://pypi.org/project/py-dormakaba-dkey/1.0.6/) `dkey.py`). This is consistent with the local-BlueZ verdict above.

#### T3.4 Shelly proxy
Shelly proxies can't connect at all (Topic 2), so **pairing is impossible** through them. Shelly's own `BLE.StartPairing`/bond list API applies to Shelly acting as a BLE *peripheral* for RPC-over-BLE, not to proxying ([Shelly BLE component docs](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/BLE)). Shelly proxies are still useful for the **passive** part: they see the 0x1D18 flag, as long as the data is in the primary advert or the Shelly scans actively.

#### T3.5 Bonds are per adapter/proxy
A bond lives in one controller: BlueZ's `/var/lib/bluetooth/<adapter>/…`, the ESP32's NVS, or the habluetooth mgmt store "keyed by adapter" ([bond_persistence.md](https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/docs/bond_persistence.md)). HA picks the connection path per attempt by RSSI and free slots (Topic 2). Core integrations document the result:
- `hue_ble`: "only the adapter/proxy which was used to pair to the light during setup will be able to control the light" ([HA hue_ble docs](https://www.home-assistant.io/integrations/hue_ble/)).
- `husqvarna_automower_ble`: pairing "only needs to be done once per BLE controller (so changing the ESPHome device will require a repair)" ([HA husqvarna_automower_ble docs](https://www.home-assistant.io/integrations/husqvarna_automower_ble/)).

How many centrals the BF450 can keep bonds for is **UNVERIFIED**. If it keeps only one, pairing via proxy B would invalidate proxy A's bond. Test by pairing from two hosts and checking whether the first still connects.

#### T3.6 Core integrations that pair today
The integration code itself never calls pair (a grep of `homeassistant/components` for `.pair(` / `pair=True` finds only non-BLE matches). The calls happen in their libraries (grep of the pinned requirements of every integration that depends on `bluetooth`):

| Integration | Library | How it pairs |
|---|---|---|
| `hue_ble` | HueBLE 2.2.3 | `client.pair()`, with its own BlueZ agent on the BlueZ backend |
| `husqvarna_automower_ble` | automower-ble 0.2.9 | `client.pair()` after `establish_connection`; errors ignored |
| `dormakaba_dkey` | py-dormakaba-dkey 1.0.6 | `client.pair()` after `establish_connection` |
| `specialized_turbo` | specialized-turbo 0.8.4 | `client.pair(protection_level=2)`; its HA docs say "Some ESPHome proxies cannot complete operating-system pairing requests" and recommend pairing through a local adapter first ([HA specialized_turbo docs](https://www.home-assistant.io/integrations/specialized_turbo/)) |

`teslemetry` "pairs" at the application layer (key whitelisting confirmed on the car), not by BLE bonding, but its config flow is a good `async_show_progress` model (Topic 5).

### Topic 4: Coordinators

HA ships four Bluetooth coordinator flavours ([dev docs, Fetching Bluetooth data](https://developers.home-assistant.io/docs/core/bluetooth/bluetooth_fetching_data)):
- `PassiveBluetoothProcessorCoordinator` and `ActiveBluetoothProcessorCoordinator`: for sensor/binary_sensor/event integrations whose data comes from adverts, optionally with polling.
- `PassiveBluetoothDataUpdateCoordinator` and `ActiveBluetoothDataUpdateCoordinator`: for everything else.

All of them register an `async_register_callback` on one `address` plus an unavailable tracker ([update_coordinator.py L89-L112](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/update_coordinator.py#L89-L112)). They therefore inherit the "fires on change" semantics from Topic 1.

**`needs_poll_method` / `poll_method`.**
- On every (changed) advert the Active variants call `needs_poll_method(service_info, seconds_since_last_poll)`. If it returns true, they schedule `poll_method(service_info)` through a `Debouncer` (default `cooldown=10 s`, `immediate=True`, `background=True`) ([active_update_coordinator.py L19-L23, L80-L118, L154-L170](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/active_update_coordinator.py#L80-L170); processor variant [active_update_processor.py L23-L172](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/active_update_processor.py#L23-L172)).
- `BleakError`s and other exceptions from the poll are caught and logged once ([active_update_coordinator.py L120-L145](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/active_update_coordinator.py#L120-L145)).
- This maps directly onto "connect only when the advert lists one of our slots". `needs_poll_method` parses `service_info.service_data.get("00001d18-…")` and returns true when it contains a slot that has a configured person (subentry). `poll_method` then runs the whole session: connect+bond, set time, subscribe, consent per pending slot, collect, disconnect.
- The debouncer's `immediate=True` means the first matching advert triggers the poll with no delay. The 10 s cooldown blocks a second poll in the same wake window; that suits a scale which falls asleep ~1 s after disconnect.

**Fit and misfit.**
- The *processor* coordinators are designed around "latest value per entity key". They support several devices per update (`PassiveBluetoothDataUpdate.devices` keyed by `device_id`), and they persist the last data per config entry in the `bluetooth.passive_update_processor` Store ([passive_update_processor.py L128-L250, L289-L322](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/passive_update_processor.py#L128-L322)).
- They do **not** model a *batch of historical readings with their own timestamps*. If you push several readings in a row, only the last one would end up as entity state (each update overwrites).
- The *non-processor* `ActiveBluetoothDataUpdateCoordinator` is a thinner base. `poll_method` returns arbitrary data and `_async_handle_bluetooth_poll` notifies listeners. That makes it a better fit when the integration itself routes readings per person and writes history (Topic 7).
- A third option is a **plain callback plus a custom session manager**: `async_register_callback` or `async_register_advertisement_callback` feeding a `asyncio.Lock`-guarded session task. This avoids the debouncer semantics and lets a config flow share the same session object (Topic 5).
- Either way, set `connectable=True` so the coordinator only acts on adverts that have a connectable path. With `connectable=False`, adverts arriving only through Shelly would trigger polls that cannot connect.

### Topic 5: Config flows

**Bluetooth discovery step.**
- A manifest match starts the config flow at `async_step_bluetooth(discovery_info)`. The flow should set `unique_id` (address), abort if already configured, and always confirm with the user ([dev docs, config flow § Discovery steps](https://developers.home-assistant.io/docs/core/integration/config_flow#discovery-steps)). Custom integrations' manifest matchers are loaded too ([loader.py `async_get_bluetooth` L618-L631](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/loader.py#L618-L631)).
- Discovery happens once per address (Topic 1). Within the 47 s window the user rarely has time to finish the flow, so the confirm step should not need the scale. Do anything that needs the scale in a later step that *waits* for the next wake-up.

**Multi-step forms and menus.** These are standard: `async_show_form`, `async_show_menu`, and step chaining ([dev docs, data entry flow](https://developers.home-assistant.io/docs/data_entry_flow_index)).

**`async_show_progress` for "step on the scale now".**
- The flow starts an `asyncio.Task` and returns `async_show_progress(progress_action=…, progress_task=task)`. The `FlowManager` re-invokes the step when the task finishes, and the step then returns `async_show_progress_done(next_step_id=…)` ([dev docs § Show progress](https://developers.home-assistant.io/docs/data_entry_flow_index#show-progress--show-progress-done); [data_entry_flow.py L525-L548, L809-L880](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/data_entry_flow.py#L525-L548)).
- `progress_task` arrived in **HA 2024.2** (blog of 2024-01-11). It was announced as mandatory from 2024.8; in the current code a missing task only logs a warning and sets `deprecated_show_progress` ([blog](https://developers.home-assistant.io/blog/2024/01/11/async-show-progress-changes); [data_entry_flow.py L818-L834](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/data_entry_flow.py#L818-L834)).
- `async_update_progress(0..1)` sends a progress fraction to the UI ([L849-L855](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/data_entry_flow.py#L849-L855)). There is also an undocumented `async_notify_flow_changed()` that makes the frontend reload the flow ([L857-L866](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/data_entry_flow.py#L857-L866)), which could refresh placeholders such as "2 readings received". **UNVERIFIED** that the frontend re-renders the placeholders; no core integration uses it.
- **Wait limits.** The flow framework has **no timeout** on a progress task: there is no timeout in `data_entry_flow.py`, and the task is only cancelled when the flow is removed ([L463-L474](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/data_entry_flow.py#L463-L474)). Closing the dialog cancels the task (blog above). The `teslemetry` code notes, though, that "a closed tab never removes the flow" ([teslemetry config_flow.py L425-L427](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/teslemetry/config_flow.py#L429-L432)). So the integration must put its own `asyncio.timeout` around the wait (for example 2-5 min for "step on the scale") and must never hold a BLE link while waiting for a human.
- **Core prior art.** `teslemetry`'s `VehicleSubentryFlowHandler` is a *subentry* flow that:
  - finds a BLE device with `async_discovered_service_info`, `async_request_active_scan` and `async_register_advertisement_callback` (5 s timeout);
  - connects;
  - runs a long pairing task under `async_show_progress` while the user confirms on the car's screen, and maps each failure to a form error ([config_flow.py L231-L540](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/teslemetry/config_flow.py#L231-L540)).
  
  That is structurally the "create a person on the scale and do the first weigh-ins" flow.

**Options and reconfigure.** An options flow (`OptionsFlow`) and a `reconfigure` step on the config entry are supported, and subentries can have their own `reconfigure` step ([dev docs § Reconfigure](https://developers.home-assistant.io/docs/core/integration/config_flow#reconfigure), [§ Subentry reconfigure](https://developers.home-assistant.io/docs/core/integration/config_flow#subentry-reconfigure), [options flow](https://developers.home-assistant.io/docs/core/integration/options_flow)). Since **2026.6**, combining a config-entry update listener with the flow's reload helpers is deprecated, and it errors from **2026.12** ([blog 2026-05-07](https://developers.home-assistant.io/blog/2026/05/07/config-entry-listener-together-with-reloading-methods)).

**Config subentries.**
- **Introduced in HA 2025.3.** `ConfigSubentryFlow` is absent in 2025.2.0 and present in 2025.3.0 ([2025.2.0 config_entries.py](https://github.com/home-assistant/core/blob/2025.2.0/homeassistant/config_entries.py), [2025.3.0](https://github.com/home-assistant/core/blob/2025.3.0/homeassistant/config_entries.py); [blog 2025-02-16](https://developers.home-assistant.io/blog/2025/02/16/config-subentries)). `_get_reconfigure_entry` was renamed to `_get_entry` shortly after ([blog 2025-03-24](https://developers.home-assistant.io/blog/2025/03/24/config-subentry-flow-changes)).
- **Usable by custom integrations.** It is plain ConfigFlow API: `async_get_supported_subentry_types()` returns `{type: FlowHandlerClass}`, and translations live under `config_subentries` ([dev docs § Subentry flows](https://developers.home-assistant.io/docs/core/integration/config_flow#subentry-flows)). Nothing in the docs restricts it to core.
- **Limits.** Subentry flows "don't support reauthentication or discovery; a subentry flow can only be initiated via the `user` or `reconfigure` steps" (same page). Subentry `unique_id` only has to be unique within the entry.
- **Lifecycle.**
  - Finishing a subentry flow adds the subentry and fires the entry's update listeners. It does **not** reload the entry by itself ([config_entries.py L3933-L3968, L2898-L2905](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/config_entries.py#L3933-L3968)).
  - `async_update_reload_and_abort` on a subentry flow schedules a reload ([L4073-L4113](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/config_entries.py#L4073-L4113)).
  - Removing a subentry (from the UI) automatically clears the devices and entities that belong to it from the registries ([L2870-L2877](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/config_entries.py#L2870-L2877)).
  - The integration can also add subentries from code with `hass.config_entries.async_add_subentry(entry, ConfigSubentry(...))` ([L2898](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/config_entries.py#L2898)). That suits "adopt existing slots found in the user list".
- **Attaching devices and entities.**
  - Entities: `async_add_entities(entities, config_subentry_id=subentry_id)` ([entity_platform.py L140-L152](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/entity_platform.py#L140-L152)).
  - Devices carry `config_subentry_id`.
  - **Since HA 2026.8, a device belongs to exactly one config entry and at most one subentry**, so "one device per subentry" is mandatory ([blog 2026-07-21](https://developers.home-assistant.io/blog/2026/07/21/device-registry-single-config-entry); [device registry docs](https://developers.home-assistant.io/docs/device_registry_index)). The `telegram_bot` migration in that blog is the reference pattern: one device per subentry, linked to the main device.
  - `DeviceInfo["via_device"]` is deprecated in favour of `via_device_id` (2026.8; removal 2027.8).
  - **Child devices** (`ChildDeviceInfo`, `parent_device_id`) exist since **HA 2026.9**: absent in 2026.8.0 and present in 2026.9.0 ([2026.9.0 device_registry.py](https://github.com/home-assistant/core/blob/2026.9.0/homeassistant/helpers/device_registry.py)). A child must be in the **same subentry** as its parent, and children "describe *composition* — the logical parts of a single product" ([device registry docs § Child devices](https://developers.home-assistant.io/docs/device_registry_index#child-devices)). A per-person subentry device therefore cannot be a child of a scale device that sits outside that subentry. A `via_device_id` link across subentries **is allowed**: the registry only checks that the id is a registered, non-child device. It applies no config-entry or subentry constraint ([device_registry.py L2391-L2420, L2632-L2652](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/device_registry.py#L2632-L2652)). This matches the `telegram_bot` pattern (chat devices in subentries via the bot device).
- **Starting flows from code or an action.**
  - `hass.config_entries.flow.async_init(domain, context={"source": …})` and `hass.config_entries.subentries.async_init((entry_id, subentry_type), context=SubentryFlowContext(source=SOURCE_USER))` start flows programmatically ([dev docs § async_on_create_entry](https://developers.home-assistant.io/docs/core/integration/config_flow#use-async_on_create_entry)). A user-source flow started from code is not shown to anyone unless it is chained as `next_flow` from a flow the user is already in. That chaining works from a config flow, and from a **repair fix flow**, which can forward to a subentry or reconfigure flow ([repairs docs](https://developers.home-assistant.io/docs/core/platform/repairs#issues-that-can-be-repaired-via-config-entry-options-or-subentry-reconfiguration)).
  - A *discovery* config flow can be raised from code with `helpers.discovery_flow.async_create_flow(..., context={"source": SOURCE_INTEGRATION_DISCOVERY})` ([discovery_flow.py L38-L60](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/discovery_flow.py#L38-L60)). It appears as "Discovered", but only for *config* flows, not subentry flows.
  - An action (service) cannot open a flow dialog in a browser. **UNVERIFIED**: no API was found for it.

### Topic 6: Per-person data model

**Devices vs entities.** The device registry docs define a device as "a physical device that has its own control unit, or a service". Separate physical products are linked by `via_device_id` (connectivity); parts of one product are child devices (composition) ([device registry docs](https://developers.home-assistant.io/docs/device_registry_index)). There is no dedicated concept for "a person measured by a device". Two options:
- **One device per person (subentry)**, holding all metrics for that person, with `via_device_id` → the scale device. This is the `telegram_bot` pattern, and the only way to get per-person entity grouping, areas and labels, given one device per subentry.
- **Entities only, on the scale device**, named per person. This doesn't work with subentries, since entities in a subentry need a device in that subentry or none.

**Sensor classes, units, state classes.** Allowed combinations come from `DEVICE_CLASS_UNITS` and `DEVICE_CLASS_STATE_CLASSES` ([sensor/const.py L669-L680, L759, L816-L892](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/sensor/const.py#L669-L892); dev docs [sensor entity](https://developers.home-assistant.io/docs/core/entity/sensor)).

| Metric (BF450 source) | Device class | Unit | State class | Notes |
|---|---|---|---|---|
| Weight (2A9D) | `WEIGHT` | `kg` (`UnitOfMass`) | `MEASUREMENT` | WEIGHT allows MEASUREMENT/TOTAL/TOTAL_INCREASING. HA converts to the user's unit system |
| BMI | none | none | `MEASUREMENT` | no BMI device class |
| Body fat %, muscle %, water % | none | `%` | `MEASUREMENT` | there is no body-composition % class; `HUMIDITY`/`MOISTURE` would be wrong |
| Water mass, soft lean mass, LBM, protein, total muscle, bone mass, ideal weight | `WEIGHT` | `kg` | `MEASUREMENT` | |
| BMR / AMR (kJ → kcal) | **none** | `kcal` | `MEASUREMENT` | `ENERGY` only allows `TOTAL`/`TOTAL_INCREASING` ([L831-L834](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/sensor/const.py#L831-L834)), so a daily rate can't use it |
| Impedance | none | `Ω` | `MEASUREMENT` | no impedance device class; `xiaomi_ble` does the same ([xiaomi_ble sensor.py L75-L82](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/xiaomi_ble/sensor.py#L75-L82)) |
| Visceral fat, body shape index | none | none | `MEASUREMENT` | |
| Metabolic age | none | `y`, plain string | `MEASUREMENT` | `DURATION` doesn't allow years ([L669-L676](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/sensor/const.py#L669-L676)) |
| Measured at | `TIMESTAMP` | none | none | `native_value` must be a tz-aware `datetime` |
| Guess flag (custom1 byte 4) | binary_sensor or enum sensor | | | diagnostic |

The 2025-10 statistics API change requires `unit_class` / `mean_type` on statistics metadata (Topic 7).

**Linking to HA `person`.** There is no person selector. The selector list in [helpers/selector.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/selector.py) has `EntitySelector` but no `PersonSelector`. Use `EntitySelector(EntitySelectorConfig(domain="person"))`. A person entity carries only `user_id` and `device_trackers` ([person/__init__.py L59-L60](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/person/__init__.py#L59-L60)); HA has no built-in "this sensor belongs to person X" relation. Store the person's entity-registry id (stable across renames) in the subentry data and use it for naming and in automations. Whether to also expose it, for example as an attribute, is a design choice.

### Topic 7: Readings from the past (backfill)

**States with a past timestamp.** `hass.states.async_set(..., timestamp=…)` accepts a timestamp ([core.py L2399-L2429](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/core.py#L2399-L2429)). But `Entity.async_write_ha_state` always writes `time_now` ([entity.py L1335-L1344](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/entity.py#L1335-L1344)), and there is no supported entity API for back-dated states. Writing out-of-order states through the low-level API bypasses the entity model, and how history and the recorder behave with them is **UNVERIFIED**. Not recommended.

**Long-term statistics import** ([recorder/statistics.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/recorder/statistics.py)):

| | `async_import_statistics` | `async_add_external_statistics` |
|---|---|---|
| statistic_id | an **entity_id** (`sensor.x`) | `domain:object_id`, e.g. `beurer_scales:anna_weight` |
| source | must be `"recorder"` | must equal the domain |
| Code | [L2860-L2892](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/recorder/statistics.py#L2860-L2892) | [L2895-L2928](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/recorder/statistics.py#L2895-L2928) |
| Core users | only `kitchen_sink` (demo) | `opower`, `tibber`, `elvia`, `ista_ecotrend`, `mill`, `solaredge`, `srp_energy`, `suez_water`, `anglian_water`, `waterfurnace`, `kitchen_sink` (grep of `homeassistant/components`) |

- Both require tz-aware `start` timestamps **at the top of the hour** (hourly granularity) ([L2832-L2843](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/recorder/statistics.py#L2832-L2843)). They **upsert** by (statistic, hour) ([L2931-L2951](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/recorder/statistics.py#L2931-L2951)).
- **Since 2025.11 / 2026.11**, metadata must carry `unit_class` and `mean_type` (`StatisticMeanType`); omitting them stops working in 2026.11 ([blog 2025-10-16](https://developers.home-assistant.io/blog/2025/10/16/recorder-statistics-api-changes)).
- Consequence for scales: several readings in the same hour collapse into one statistics row. You compute mean/min/max yourself, and a later import for the same hour overwrites the earlier one. Statistics are graph data (statistics graph card, energy-style). They do not replace states: no automation trigger and no `last_changed`.
- `async_import_statistics` into a *recorder-tracked* sensor's own id competes with the hourly statistics the recorder compiles from that sensor's states. Importing into an external id (`domain:…`) avoids the clash. **UNVERIFIED** how the compiler treats a past hour that was later imported.

**EventEntity / bus events carrying the original timestamp.**
- `EventEntity._trigger_event(event_type, attributes)` sets the state to *now*. Since **HA 2026.8** it forces strictly increasing timestamps (+1 ms), "so multiple events fired within the same millisecond stay distinct state changes" ([event/__init__.py L154-L168](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/event/__init__.py#L154-L168); absent in [2026.7.0](https://github.com/home-assistant/core/blob/2026.7.0/homeassistant/components/event/__init__.py), present in [2026.8.0](https://github.com/home-assistant/core/blob/2026.8.0/homeassistant/components/event/__init__.py)).
- The original measurement time travels as an event attribute, for example `measured_at`, with all metrics. Each backfilled reading becomes its own recorded state change, in order, triggerable from automations.
- Raw bus events: `hass.bus.async_fire(event_type, data, time_fired=…)` accepts a `time_fired` ([core.py L1575-L1590](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/core.py#L1575-L1590)). Using a past `time_fired` is not documented. **UNVERIFIED** effects on the logbook and recorder.

**`measured_at` attribute / sensor.**
- A per-person `TIMESTAMP` sensor ("last measured") plus a `measured_at` attribute on the metric sensors keeps the *latest* reading's real time visible.
- Sensors with an unchanged value and unchanged attributes only update `last_reported` ([core.py L2465-L2500](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/core.py#L2465-L2500)). A changing `measured_at` attribute therefore guarantees a new state for every reading, even when the weight is identical.

**Several readings in the same second / batch.** These options can be combined:
1. Write sensors only for the newest reading. Fire one EventEntity event per reading, oldest first, which yields distinct ms-separated states since 2026.8. Optionally upsert hourly external statistics per metric.
2. Write each reading to the sensors in sequence. Each write becomes a state row with "now" timestamps a few µs apart. History then shows a burst at sync time instead of the real measurement times, which is misleading for multi-day backfills.
3. Statistics only for backfill, states for live readings. A reading is "live" when `measured_at` is within ~2 min of now.

### Topic 8: Actions, entities and notifications

**Actions with response data.** Register them with `supports_response=SupportsResponse.OPTIONAL|ONLY` and return a JSON-serialisable dict ([dev docs § Response data](https://developers.home-assistant.io/docs/dev_101_services#response-data); [core.py `SupportsResponse` L2549-L2563](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/core.py#L2549-L2563)). The docs say response data is "meant for cases that do not fit the Home Assistant state", for example "a response stream of objects". `beurer_scales.get_readings(person, since)` or `beurer_scales.list_users` (the scale's user list from the last sync) fit that description.
- Platform entity actions are registered from `async_setup` with `service.async_register_platform_entity_service` since the 2025-09 API change ([blog 2025-09-25](https://developers.home-assistant.io/blog/2025/09/25/entity-services-api-changes); [service.py L1267](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/service.py#L1267)).
- An action can't do anything with the scale until it wakes up. Actions that change the scale therefore have to **queue** work for the next session, or block with a timeout until the next wake-up.

**Entities for control and profile.**
- [`button`](https://developers.home-assistant.io/docs/core/entity/button): "take measurement" or "assign next weigh-in to this person". On the BF450 this is FFF4 `00` *while connected and consented* (protocol §4). Because the scale only accepts the command while awake and connected, a button press can only *arm* the behaviour for the next session.
- [`number`](https://developers.home-assistant.io/docs/core/entity/number) (height), [`date`](https://developers.home-assistant.io/docs/core/entity/date) (birth date), [`select`](https://developers.home-assistant.io/docs/core/entity/select) (gender, activity level) and [`text`](https://developers.home-assistant.io/docs/core/entity/text) entities can hold the per-person UDS profile. HA keeps the desired value and the integration writes it on the next consented session.
- Alternatively the profile lives in subentry data and is edited through subentry `reconfigure`. That gives fewer entities and is closer to "configuration".

**Persistent notifications.** `persistent_notification.async_create(hass, message, title, notification_id)` ([persistent_notification/helpers.py L53](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/persistent_notification/helpers.py#L53)). Good for "step on the scale now" outside a flow, or "3 readings synced for Anna".

**Repair issues with fix flows.** `ir.async_create_issue(..., is_fixable=True, data={...})` plus `async_create_fix_flow` in `repairs.py` ([dev docs, repairs](https://developers.home-assistant.io/docs/core/platform/repairs); [issue_registry.py L409](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/issue_registry.py#L409)).
- A fix flow can forward to a **subentry reconfigure** flow through `next_flow=(FlowType.CONFIG_SUBENTRIES_FLOW, flow_id)` (same docs page).
- `is_persistent` controls whether the issue survives restarts. Ignored issues stay ignored until they are deleted and re-created.
- Candidates: "scale was reset / slot N gone" (fix: re-create or re-adopt the user), "consent rejected for Anna" (fix: re-enter code), "bond lost" (fix: re-pair). The "guessed reading for Anna" case (fix: confirm / reassign / discard) is a weaker fit, because it is a per-reading decision rather than a configuration issue.

### Topic 9: Storage

- **Config entry and subentry data** are stored as plain JSON in `.storage/core.config_entries` ([config_entries.py L143-L145](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/config_entries.py#L143-L145)). HA has no secret encryption for config entries; it is the normal place for credentials (the dev docs' flow example stores a `password` in entry data, [config flow docs](https://developers.home-assistant.io/docs/core/integration/config_flow#defining-steps)). Consent codes (4 digits) and slot numbers belong in **subentry data**, one subentry per person/slot. The scale address belongs in entry data.
- **`helpers.storage.Store`** keeps integration-owned JSON under `.storage/<key>`. It has versioned migrations, `async_delay_save`, and `private=True` for mode-600 files ([storage.py L292-L320](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/storage.py#L292-L320)). Since the change of 2025-11-25, `serialize_in_event_loop` defaults to `True` ([blog](https://developers.home-assistant.io/blog/2025/11/25/storage-helper-opt-in-serialize-in-executor)). Use it for: the last-seen user list (reset detection), a de-dup log of delivered reading timestamps per slot, pending profile writes and queued "take measurement" intents, and unassigned/guessed readings awaiting a decision.
- **`RestoreSensor`** restores `native_value` and unit after a restart via `async_get_last_sensor_data` ([sensor entity docs](https://developers.home-assistant.io/docs/core/entity/sensor)). The scale only reports while awake, so per-person metric sensors should restore. Otherwise they are `unknown` after every restart until the next weigh-in. `eufylife_ble` does this for its weight sensor ([eufylife_ble sensor.py L116-L160](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/eufylife_ble/sensor.py#L116-L160)). From **2026.11**, restore state is keyed by entity-registry id, so it survives entity_id renames ([blog 2026-10-06](https://developers.home-assistant.io/blog/2026/10/06/restore-state-last-states-removal)).
- **Runtime objects** (session manager, coordinator) go in `entry.runtime_data` (quality-scale rule `runtime-data`, Bronze; [rules](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/runtime-data)). A subentry flow reaches them through `self._get_entry().runtime_data`, as `teslemetry` does.

### Topic 10: Custom integration constraints

- **Manifest.**
  - Custom integrations must have `version` (AwesomeVersion) ([manifest docs](https://developers.home-assistant.io/docs/creating_integration_manifest#version)).
  - BLE integrations should depend on `bluetooth_adapters` ([dev docs, Building a Bluetooth integration](https://developers.home-assistant.io/docs/bluetooth)).
  - `bluetooth` matchers in custom manifests are honoured (Topic 5).
  - Pin requirements, for example a separate protocol library.
- **HACS.**
  - Public GitHub repo with exactly one `custom_components/<domain>/`, and a manifest with `domain`, `documentation`, `issue_tracker`, `codeowners`, `name`, `version`.
  - Brand assets: a `brand/` directory with at least `icon.png`.
  - A `hacs.json` with `name`; optional `homeassistant` minimum version.
  - Repo description, topics and README ([HACS publish: integration](https://hacs.xyz/docs/publish/integration), [general](https://hacs.xyz/docs/publish/start)).
  - For the default store: HACS Action and hassfest Action passing, and a full GitHub release ([HACS include](https://hacs.xyz/docs/publish/include)).
  - Custom integrations can ship `brand/icon.png` / `logo.png` themselves since **HA 2026.3** ([blog 2026-02-24](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api)).
  - Pin `"homeassistant": "2026.10.0"` in `hacs.json` if `async_register_advertisement_callback` is used. 2026.8 is the floor for single-config-entry devices and EventEntity ms-ordering, and 2025.3 the floor for subentries.
- **Quality scale (for a later core submission)** ([tiers.json](https://github.com/home-assistant/developers.home-assistant/blob/50b518898989b67bb76b69827f227aa6f0acd32a/docs/core/integration-quality-scale/_includes/tiers.json); [rules](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules)):
  - **Bronze:** `config-flow`, `config-flow-test-coverage`, `unique-config-entry`, `entity-unique-id`, `has-entity-name`, `runtime-data`, `test-before-configure`, `test-before-setup`, `dependency-transparency`, `action-setup`, `entity-event-setup`, `appropriate-polling`, `common-modules`, `brands`, `docs-*`.
    - **`dependency-transparency`** requires the protocol code to be a separate OSI-licensed **PyPI library built by public CI**, with tagged releases ([rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/dependency-transparency)). Keep the frame decoders and session logic in a standalone package from day one.
    - `test-before-setup`/`test-before-configure` have exemptions where validation is impossible. A sleeping scale qualifies for setup, but the flow can still validate during the "step on the scale" step.
  - **Silver:** `config-entry-unloading`, `entity-unavailable`, `log-when-unavailable`, `parallel-updates`, `action-exceptions`, `reauthentication-flow` (here: consent code rejected), `test-coverage`, `integration-owner`.
  - **Gold:** `devices`, `discovery`, `discovery-update-info`, `dynamic-devices`, `stale-devices` (remove a person device when its slot is deleted), `repair-issues`, `reconfiguration-flow`, `diagnostics`, `entity-translations`, `icon-translations`, `exception-translations`, `entity-category`, `entity-device-class`, `entity-disabled-by-default`, `docs-*`.
  - **Platinum:** `async-dependency`, `inject-websession` (N/A), `strict-typing`.
- **Core-review risk, UNVERIFIED.** A core integration that registers its own BlueZ default agent over D-Bus (the local-adapter bonding workaround) may not be accepted. HueBLE does register an agent and is in core (Topic 3), but that agent is not made the default.

### Topic 11: Prior art, how others separate users

- **`xiaomi_ble` (core).**
  - Scales are passive advertisement devices. One device per scale exposes the latest `weight`, `impedance` (and for the S400 low/high impedance, heart rate) and, on newer models, a **`profile_id` sensor**: the scale's own user slot from the advert ([xiaomi_ble sensor.py L75-L82, L195-L220](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/xiaomi_ble/sensor.py#L195-L220); [xiaomi-ble parser.py `obj4e16`/`obj6e16` L1820-L1900](https://github.com/Bluetooth-Devices/xiaomi-ble/blob/4dbc9f143a67fed1b113032f5dd3ed703c34f4ae/src/xiaomi_ble/parser.py#L1820-L1900)).
  - **Core does no per-person separation.** Users are left to templates or HACS (bodymiscale).
  - It uses `PassiveBluetoothProcessorCoordinator`, so data restores after a restart.
- **`eufylife_ble` (core).** One scale device with real-time and final weight; weight and heart rate are `RestoreSensor`s ([eufylife_ble sensor.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/eufylife_ble/sensor.py)). There are no users.
- **Withings (core, cloud).**
  - Multi-user through **one config entry per Withings account**: `unique_id = userid` from the OAuth token, and the device identifier is that userid ([withings config_flow.py L59-L62](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/withings/config_flow.py#L59-L62); [withings `__init__.py` L104-L111, L150](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/withings/__init__.py#L104-L150)).
  - The cloud attributes measurements to users, and HA receives them already separated.
  - Backfill: the first update fetches 14 days, later ones `get_measurement_since(last)`. Readings are aggregated into **latest-value sensors**; nothing is imported as history ([withings coordinator.py L123-L140](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/withings/coordinator.py#L123-L140)).
  - Accounts created after 2026-10-11 need a Withings+ subscription ([HA withings docs](https://www.home-assistant.io/integrations/withings/)).
- **bodymiscale (HACS, v2026.10.2).**
  - **One config entry per person**: name, date of birth, gender, calculation mode, and a source weight sensor (plus optional impedance and "stabilized" sensors).
  - Since v2026.5.x it can share one scale sensor across profiles. It assigns each reading by a per-person **user identification method** ([bodymiscale README § Configuration](https://github.com/dckiller51/bodymiscale/blob/c3e02d196f885474f5c5e937f18f0e818d8da36c/README.md#configuration)):
    - weight range;
    - nearest current weight with a tolerance (default ±5 kg, initial weight required);
    - **interactive push notification** (the person taps their name);
    - **profile ID** from the scale;
    - manual.
  - It also notes that `xiaomi_ble` re-publishes the last weight on every advert, so a "stabilized" signal is needed to count each weigh-in once.
- **Beurer BF450 comparison.** The BF450 does the assignment *on the scale* (UDS user index + consent), which is stronger than any of the above: readings arrive already tagged with `user index` in 2A9D byte 10. HA only has to map slot → person. bodymiscale's notification and nearest-weight methods are still useful prior art for the scale's leftovers: `U--`/guessed readings (custom1 byte 4 ≠ 0), and readings taken by *nobody-matched* persons, which the scale never delivers (protocol §9.4).

## 3. Implications for the Beurer design

This section maps the findings onto the scale facts from the handoff and onto the open design questions. It lists options and trade-offs; the decisions belong to the design session.

### 3.1 Scale facts → HA mechanisms

| Scale fact (protocol doc) | Relevant HA findings | Consequence / options |
|---|---|---|
| **~47 s awake window**, sleeps ~1 s after host disconnect | Default `establish_connection` worst case is well over 60 s (4 attempts × 20 s) (T2). BF450 reconnect plus setup measured ~10 s on BlueZ. Auto-mode active windows default to every 5 min for 10 s (T1). | Cap the session with `asyncio.timeout(~40 s)` and `max_attempts≈2`. Start connecting on the first "slot appended" advert. Disconnect explicitly when done, so the scale sleeps and the "pending" flag is re-evaluated. A config-flow "wait for wake" needs its *own* timeout because the flow framework has none (T5). |
| **0x1D18 service data lists pending slots** | Manifest `service_data_uuid` matches by key, which is enough for discovery (T1). `async_register_callback` fires on payload change; `async_register_advertisement_callback` (HA ≥ 2026.10) fires on every packet. Active coordinators' `needs_poll_method` sees each changed advert (T4). | Connect only if `set(payload) ∩ configured_slots ≠ ∅`. Slots that are pending but unconfigured, i.e. users of another app or a person not added yet, can be surfaced, for example as a diagnostic or repair ("slot 3 has data but no person in HA"). **Open:** passive vs active visibility of the 0x16 AD and the name, which needs a capture (T1). |
| **Mandatory Just Works bonding** | Local BlueZ: the `pair=True` path is fine, but stock HA OS has no agent and the adapter is not bondable, so it is expected to fail. Container/Core depend on the host. ESP32 ESPHome proxy: the security request is auto-accepted, bond flag set, NVS-persisted. RP2040 since ESPHome 2026.8. Shelly: impossible. Bonds are per controller, and HA does not pin the path (T3). | See 3.2 (adapter choice). Always pass `pair=True`: it is a no-op when already bonded on BlueZ, and `is_paired()` short-circuits on ESPHome. Handle `NotImplementedError` (proxy without PAIRING) and auth failures as distinct, user-visible errors (repair issue). |
| **Consent codes (4 digits), one per slot** | Subentry data is plaintext JSON in `.storage`, the normal place for credentials (T9). Subentry `unique_id` must be unique within the entry (T5). | One subentry per person = `{slot, consent_code, profile, person_entity_reg_id, created_marker}`, `unique_id = f"slot_{n}"`. `20 02 04` on consent is ambiguous (T8/§9.4b), so pair it with the FFF1 user list to decide "wrong code" (reauth-style subentry reconfigure) vs "slot gone" (reset/delete issue). |
| **Backfilled multi-day readings with their own timestamps (scale local time), oldest first** | No supported back-dated entity states. EventEntity gives ordered, distinct states (ms-separated since 2026.8) with `measured_at` in attributes. External statistics are hourly and upserted. A sensor with a changing `measured_at` attribute always writes a new state (T7). | Options: (A) sensors = latest reading, plus one event per reading; (B) A plus `domain:person_metric` external statistics for long-term graphs at real hours; (C) sensors only, which loses intermediate readings. Convert scale local time with HA's time zone. The scale clock is set each session (§3 step 2), so readings taken after a clock drift or battery swap can carry wrong times. Keep a per-slot de-dup log in `Store` (T9). |
| **`U--` / guessed readings** (custom1 byte 4 ≠ 0 = guessed slot), and unmatched persons never delivered | No HA primitive for "pending attribution". Repairs are per issue, not per reading. bodymiscale uses actionable push notifications or weight-proximity (T11). | Options: (1) accept the guess and mark it (`guessed: true` attribute/event field, diagnostic binary sensor); (2) hold guessed readings in `Store` and ask via a persistent notification, mobile actionable notification or repair flow (confirm / move to another person / discard); (3) apply a proximity check against the person's last weight before accepting. Moving a reading to another slot is HA-side only: the scale has already removed it from the source slot's queue. |
| **Reset detection via FFF1 (no consent needed)** | FFF1 read costs ~1 write plus notifications (~0.4 s per GATT op, §9.4b). Repairs can forward to subentry flows. Removing a subentry auto-removes its devices and entities (T5, T8). | Read FFF1 at the start of each session (cheap insurance), or only after a `20 02 04`. Compare with the configured slots, and with a stored profile fingerprint (DOB/height/gender) to catch a *reused* slot number after reset. On mismatch, raise a fixable repair → re-create or re-adopt. Also handle bond loss after a factory reset: BlueZ needs `unpair()` (RemoveDevice) then re-pair; ESPHome `unpair()` needs an active link (T3). |
| **Up to 8 per-slot users; the scale assigns by weight (3 kg threshold)** | One device per subentry since 2026.8. `via_device_id` links the person device to the scale device. Child devices must share the parent's subentry (T5/T6). | Scale device on the entry (battery, last sync, diagnostics), and one person device per subentry with metric sensors and an event entity. `via_device_id` across subentries is accepted by the registry ([device_registry.py](https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/device_registry.py#L2632-L2652)). |

### 3.2 Open design questions

**A. Local BlueZ vs ESPHome proxy (bonding decides this).**
- *ESPHome ESP32 proxy (ESPHome ≥ 2023.3, preferably current).*
  - Strongest evidence of working bonding without host changes: auto-accepts the security request, Just Works, keys in NVS.
  - Downsides: one more device; bonds tied to that proxy; HA may choose a different connectable path (another proxy or a local adapter) by RSSI. Mitigations: document "one connectable path near the scale"; bond on each path (hue_ble/automower docs accept this limitation); or detect a non-bonded path and retry.
  - Not tested with the BF450 (**UNVERIFIED**).
- *Local adapter, HA Container/Core.* Works if the host has an agent or `AlwaysPairable=true` (the setup used for the protocol verification, already proven with probe.py). Bonds in host `/var/lib/bluetooth`.
- *Local adapter, HA OS.* Expected to fail out of the box. Options:
  1. The integration registers a `NoInputNoOutput` agent and calls `RequestDefaultAgent` around `pair()`, as probe.py does. This is intrusive, but there is precedent for an integration registering an agent (HueBLE).
  2. Ask users to configure the host. Hard on HA OS: no supported way to edit `main.conf`. **UNVERIFIED**: whether the SSH add-on can run `bluetoothctl`.
  3. Wait for the habluetooth DBus-free mgmt path, which already auto-confirms Just Works and persists LTKs, but is experimental and not wired into HA.
- *Shelly:* useful only for wake detection.
- A design that accepts any connectable path with `pair=True`, and turns pairing errors into a clear repair, keeps all options open.

**B. Mapping an HA person to a scale slot and consent code.**
- Config entry = one scale (unique_id = BLE address). Subentry = one person/slot. Subentry data holds slot, code, profile, and the `person` entity's registry id (picked with `EntitySelector(domain="person")`, optional).
- Alternatives:
  - Config entry per person (bodymiscale/Withings style). This duplicates the scale connection across entries; a single scale session must serve all pending slots, so this needs a shared runtime object.
  - A single entry with a slot table in options. Simpler, but no per-person devices in the subentry sense.

**C. Create-new vs adopt-existing slot flow.** A subentry flow (`user` step = menu: "Create new user on the scale" / "Use existing slot").
- *Create:* collect profile (DOB, height, gender, activity) → `async_show_progress` "Step on the scale to wake it" (task: wait for advert via `async_register_advertisement_callback`, own timeout of a few minutes) → connect + bond → read FFF1 (free slot? max 8) → UCP `01` with random code 1111–9998 → consent → write profile → teach phase (D) → create the subentry.
  - Risk: the user closes the dialog after the scale user is created. The progress task is cancelled, but the slot exists on the scale. Options: delete it in the cancellation path while still consented (UCP `03`); or record "orphan slot N, code X" in `Store` and offer adoption/cleanup later.
- *Adopt:* enter slot plus code (or pick from the last FFF1 list the integration saw) → validate on the next wake (progress step) → create the subentry.
  - Warn against also using the Beurer app for the same person (§9.4: duplicate profiles cause `U--`).
- Auto-adopt from code (`async_add_subentry`) is possible, but a slot without its code can't be consented. So "adopt" always needs the user to supply the code, unless HA created the slot.

**D. "Teach the scale" first-reading procedure.**
- The scale learns a new user only from readings taken while that user is connected and consented (§9.4: 2–3 readings → `U-1`). This needs the link held *while a human acts*.
- Inside the flow: a second `async_show_progress` step ("Step on the scale now, reading k of 3"). It keeps the session open, collects live frames (2A9D…FFF6, then FFF4 `01`), and finishes after N readings or a timeout. Progress can be shown with `async_update_progress(k/N)`.
- Rules from the evidence: give the task its own timeout; disconnect in `finally` and in `async_remove()` (the `teslemetry` pattern); cap the hold time. The scale never drops a bonded link itself, so the host must end it.
- Alternative outside the flow: create the subentry first, then a per-person "Teach / take measurement" `button` arms "next session: consent as this person and send FFF4 `00`". This is usable later too (a family member who becomes `U--`), but needs a notification to tell the user when to step on.
- **Check before relying on it** (protocol §9.5): how long the scale stays awake with a held connection, and whether a step-off/step-on cycle during a held connection always produces a new live reading. Weigh-ins 2 and 3 in §9.4 suggest it does.

**E. Guessed and stale (backfilled) readings.** See row "U--" above. Additional trade-offs:
- For stale readings, decide whether sensors show the *newest* reading even if it is days old (state `last_changed` = sync time, `measured_at` = real time), or whether readings older than X are shown only as events/statistics.
- Readings are delivered oldest first, so the final sensor state is always the newest reading.
- Keep idempotency: never re-emit a reading whose `(slot, timestamp, weight)` is in the de-dup log, for example after a partial session.

**F. Reset / deleted-slot detection.** See the FFF1 row above.
- Distinguish: (1) wrong code (slot present in FFF1, consent `04`) → subentry reconfigure; (2) slot missing → "user deleted on scale" repair (re-create / re-adopt / remove person); (3) empty user list → "scale was factory-reset" repair, which also needs a bond reset (local `unpair()`, ESPHome `unpair()` while connected).
- `stale-devices` (Gold) suggests removing a person device when the user removes the subentry; HA does this automatically (T5).

**G. Multi-model config tables.**
- The manifest can list one matcher per family: `local_name` patterns (`BF4*`, `BF6*`, `BF7*`, `BF8*`, `BF9*`, `BF1*`, `GS435`, `SRBF1`, `Beurer BF7*`), plus `service_data_uuid` for both byte orders of 0x181D.
- The custom-service/characteristic map (protocol §1 table, §7 markers) can be a per-model dataclass table in the *library* (needed anyway for `dependency-transparency`). The integration then only knows "capabilities", for example which metrics and which profile fields exist.
- Entities can be created per capability set (`entity-disabled-by-default` for niche metrics such as segmental analysis).
- The BF700/710 family (`0xFFE0/FFE1` protocol) is a separate protocol implementation behind the same interface. Its bonding needs are **UNVERIFIED**.

### 3.3 UNVERIFIED items and how to settle them

| # | Item | How to verify |
|---|---|---|
| U1 | Stock HA OS local adapter cannot bond with the BF450 (no agent, not bondable) | Fresh HA OS test install with a USB adapter: `establish_connection(..., pair=True)` against the scale, then check the logs and `bluetoothctl show` (Pairable) |
| U2 | An integration-registered default agent (`RegisterAgent` + `RequestDefaultAgent`, NoInputNoOutput) makes bonding work on HA OS | Same test install, agent code from probe.py moved into HA |
| U3 | How HueBLE's non-default agent helps on HA OS | Read the HueBLE repo/issues, or test with D-Bus monitoring |
| U4 | ESP32 ESPHome proxy auto-bonds with the BF450 before the 0.5 s no-bond drop; proxy session latency | ESP32 proxy (ESPHome ≥ 2023.3, ideally current) next to the scale; time connect → consent → frames |
| U5 | Bluedroid 5.5.5 behaviour on reconnect after the scale lost its key (factory reset) | Reset the scale with the proxy bonded; observe; then `unpair()` + re-pair |
| U6 | Number of centrals the BF450 keeps bonds for (does bonding via proxy B break proxy A / the host?) | Bond from two controllers alternately; check whether the first still connects without re-pairing |
| U7 | Whether the 0x1D18 service data and the local name are in ADV_IND (passive-visible) or only in SCAN_RSP | Passive scan on the host (BlueZ AdvertisementMonitor / bleak passive) or `btmon` with root while weighing |
| U8 | Real-world re-wake behaviour of change-only callbacks (history eviction) | Log `async_register_callback` vs `async_register_advertisement_callback` events across two quick wakes |
| U9 | No API pins a connection to a specific proxy/adapter | Ask upstream / watch habluetooth; none found in `wrappers.py` |
| U10 | Frontend re-renders progress placeholders after `async_notify_flow_changed()` | Small custom flow test |
| U11 | Effects of back-dated `states.async_set(timestamp=…)` / `bus.async_fire(time_fired=…)` on recorder, history and logbook | Test instance; not recommended anyway |
| U12 | Recorder's hourly compile vs a later `async_import_statistics` into an entity's own statistic id | Test instance, or read `recorder/statistics.py` compile path in depth; avoid by using external ids |
| U13 | No way for an action to open a flow dialog in the browser | Frontend/WS API review |
| U14 | Whether the SSH add-on (protection mode off) can run `bluetoothctl` on HA OS to set `pairable on` / register an agent | Test on HA OS; check add-on docs |
| U15 | Core reviewers' acceptance of an integration-registered BlueZ default agent | Ask on the HA developer channels / architecture discussion before a core submission |
| U16 | BF700/BF710 bonding requirements | Hardware test (no device available) |

## 4. Sources

Deduplicated list of all 199 URLs cited above, grouped by origin. Local files referenced: `docs/beurer-ble-protocol.md`, `tools/probe.py`.

### Home Assistant developer docs and dev blog

- <https://developers.home-assistant.io/blog/2024/01/11/async-show-progress-changes>
- <https://developers.home-assistant.io/blog/2025/02/16/config-subentries>
- <https://developers.home-assistant.io/blog/2025/03/24/config-subentry-flow-changes>
- <https://developers.home-assistant.io/blog/2025/09/25/entity-services-api-changes>
- <https://developers.home-assistant.io/blog/2025/10/16/recorder-statistics-api-changes>
- <https://developers.home-assistant.io/blog/2025/11/25/storage-helper-opt-in-serialize-in-executor>
- <https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api>
- <https://developers.home-assistant.io/blog/2026/05/07/config-entry-listener-together-with-reloading-methods>
- <https://developers.home-assistant.io/blog/2026/07/21/device-registry-single-config-entry>
- <https://developers.home-assistant.io/blog/2026/10/06/restore-state-last-states-removal>
- <https://developers.home-assistant.io/docs/bluetooth>
- <https://developers.home-assistant.io/docs/core/bluetooth/api#clearing-match-history-for-rediscovery>
- <https://developers.home-assistant.io/docs/core/bluetooth/api#fetching-the-bleak-bledevice-from-the-address>
- <https://developers.home-assistant.io/docs/core/bluetooth/api#requesting-on-demand-active-scanning>
- <https://developers.home-assistant.io/docs/core/bluetooth/api#subscribing-to-every-advertisement-of-a-device>
- <https://developers.home-assistant.io/docs/core/bluetooth/bluetooth_fetching_data>
- <https://developers.home-assistant.io/docs/core/entity/button>
- <https://developers.home-assistant.io/docs/core/entity/date>
- <https://developers.home-assistant.io/docs/core/entity/number>
- <https://developers.home-assistant.io/docs/core/entity/select>
- <https://developers.home-assistant.io/docs/core/entity/sensor>
- <https://developers.home-assistant.io/docs/core/entity/text>
- <https://developers.home-assistant.io/docs/core/integration-quality-scale/rules>
- <https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/dependency-transparency>
- <https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/runtime-data>
- <https://developers.home-assistant.io/docs/core/integration/config_flow#defining-steps>
- <https://developers.home-assistant.io/docs/core/integration/config_flow#discovery-steps>
- <https://developers.home-assistant.io/docs/core/integration/config_flow#reconfigure>
- <https://developers.home-assistant.io/docs/core/integration/config_flow#subentry-flows>
- <https://developers.home-assistant.io/docs/core/integration/config_flow#subentry-reconfigure>
- <https://developers.home-assistant.io/docs/core/integration/config_flow#use-async_on_create_entry>
- <https://developers.home-assistant.io/docs/core/integration/options_flow>
- <https://developers.home-assistant.io/docs/core/platform/repairs>
- <https://developers.home-assistant.io/docs/core/platform/repairs#issues-that-can-be-repaired-via-config-entry-options-or-subentry-reconfiguration>
- <https://developers.home-assistant.io/docs/creating_integration_manifest#bluetooth>
- <https://developers.home-assistant.io/docs/creating_integration_manifest#version>
- <https://developers.home-assistant.io/docs/data_entry_flow_index>
- <https://developers.home-assistant.io/docs/data_entry_flow_index#show-progress--show-progress-done>
- <https://developers.home-assistant.io/docs/dev_101_services#response-data>
- <https://developers.home-assistant.io/docs/device_registry_index>
- <https://developers.home-assistant.io/docs/device_registry_index#child-devices>
- <https://github.com/home-assistant/developers.home-assistant/blob/50b518898989b67bb76b69827f227aa6f0acd32a/docs/core/integration-quality-scale/_includes/tiers.json>

### Home Assistant integration docs

- <https://www.home-assistant.io/integrations/bluetooth/#requirements-for-linux-systems>
- <https://www.home-assistant.io/integrations/bluetooth/#scanning-mode>
- <https://www.home-assistant.io/integrations/bluetooth/#scanning-modes>
- <https://www.home-assistant.io/integrations/dormakaba_dkey/>
- <https://www.home-assistant.io/integrations/hue_ble/>
- <https://www.home-assistant.io/integrations/husqvarna_automower_ble/>
- <https://www.home-assistant.io/integrations/shelly/#bluetooth-support>
- <https://www.home-assistant.io/integrations/specialized_turbo/>
- <https://www.home-assistant.io/integrations/withings/>

### Home Assistant core source

- <https://github.com/home-assistant/core/blob/2025.2.0/homeassistant/config_entries.py>
- <https://github.com/home-assistant/core/blob/2025.3.0/homeassistant/config_entries.py>
- <https://github.com/home-assistant/core/blob/2026.10.0/homeassistant/components/bluetooth/api.py>
- <https://github.com/home-assistant/core/blob/2026.6.0/homeassistant/components/bluetooth/config_flow.py>
- <https://github.com/home-assistant/core/blob/2026.7.0/homeassistant/components/event/__init__.py>
- <https://github.com/home-assistant/core/blob/2026.8.0/homeassistant/components/event/__init__.py>
- <https://github.com/home-assistant/core/blob/2026.9.0/homeassistant/components/bluetooth/api.py>
- <https://github.com/home-assistant/core/blob/2026.9.0/homeassistant/helpers/device_registry.py>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/__init__.py#L427>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/active_update_coordinator.py#L120-L145>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/active_update_coordinator.py#L80-L170>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/active_update_processor.py#L23-L172>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/api.py#L142-L171>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/api.py#L174-L205>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/api.py#L222-L233>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/api.py#L327-L340>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/manifest.json>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/match.py#L252-L275>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/match.py#L36-L46>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/match.py#L399-L420>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/match.py#L440-L450>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/match.py#L72-L161>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/passive_update_processor.py#L128-L322>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/bluetooth/update_coordinator.py#L89-L112>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/esphome/manifest.json>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/eufylife_ble/sensor.py>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/eufylife_ble/sensor.py#L116-L160>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/event/__init__.py#L154-L168>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/persistent_notification/helpers.py#L53>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/person/__init__.py#L59-L60>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/recorder/statistics.py>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/recorder/statistics.py#L2832-L2843>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/recorder/statistics.py#L2860-L2892>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/recorder/statistics.py#L2860-L2951>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/recorder/statistics.py#L2895-L2928>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/recorder/statistics.py#L2931-L2951>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/sensor/const.py#L669-L676>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/sensor/const.py#L669-L892>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/sensor/const.py#L831-L834>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/shelly/bluetooth/__init__.py>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/teslemetry/config_flow.py#L231-L540>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/teslemetry/config_flow.py#L429-L432>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/withings/__init__.py#L104-L150>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/withings/config_flow.py#L59-L62>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/withings/coordinator.py#L123-L140>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/xiaomi_ble/sensor.py#L195-L220>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/components/xiaomi_ble/sensor.py#L75-L82>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/config_entries.py#L143-L145>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/config_entries.py#L2870-L2877>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/config_entries.py#L2898>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/config_entries.py#L3933-L3968>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/config_entries.py#L4073-L4113>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/core.py#L1575-L1590>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/core.py#L2399-L2429>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/core.py#L2465-L2500>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/core.py#L2549-L2563>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/data_entry_flow.py#L463-L474>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/data_entry_flow.py#L525-L548>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/data_entry_flow.py#L809-L880>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/data_entry_flow.py#L818-L834>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/data_entry_flow.py#L849-L855>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/data_entry_flow.py#L857-L866>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/device_registry.py#L2632-L2652>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/discovery_flow.py#L38-L60>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/entity.py#L1335-L1344>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/entity_platform.py#L140-L152>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/issue_registry.py#L409>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/selector.py>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/service.py#L1267>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/helpers/storage.py#L292-L320>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/homeassistant/loader.py#L618-L631>
- <https://github.com/home-assistant/core/blob/c1fa339167187a14aed68f98daee9c2e120463e5/script/hassfest/manifest.py#L256-L270>

### Home Assistant OS source

- <https://github.com/home-assistant/operating-system/blob/75226325226a46d5f567f6595f8c3c51fe9a1875/buildroot-external/rootfs-overlay/etc/bluetooth/main.conf>
- <https://github.com/home-assistant/operating-system/blob/75226325226a46d5f567f6595f8c3c51fe9a1875/buildroot-external/rootfs-overlay/usr/lib/systemd/system/haos-bt-cache.service>
- <https://github.com/home-assistant/operating-system/blob/75226325226a46d5f567f6595f8c3c51fe9a1875/buildroot-external/rootfs-overlay/usr/lib/systemd/system/var-lib-bluetooth.mount>

### Bluetooth-Devices libraries and bleak

- <https://github.com/Bluetooth-Devices/bleak-esphome/blob/b2fb6bf4cb2ea4bf52284ce8664d11e3de46fdf4/src/bleak_esphome/backend/client.py#L455-L475>
- <https://github.com/Bluetooth-Devices/bleak-esphome/blob/b2fb6bf4cb2ea4bf52284ce8664d11e3de46fdf4/src/bleak_esphome/backend/client.py#L477-L484>
- <https://github.com/Bluetooth-Devices/bleak-esphome/blob/b2fb6bf4cb2ea4bf52284ce8664d11e3de46fdf4/src/bleak_esphome/backend/client.py#L555-L560>
- <https://github.com/Bluetooth-Devices/bleak-esphome/blob/b2fb6bf4cb2ea4bf52284ce8664d11e3de46fdf4/src/bleak_esphome/backend/client.py#L615-L636>
- <https://github.com/Bluetooth-Devices/bleak-esphome/blob/b2fb6bf4cb2ea4bf52284ce8664d11e3de46fdf4/src/bleak_esphome/backend/client.py#L638-L655>
- <https://github.com/Bluetooth-Devices/bleak-esphome/blob/b2fb6bf4cb2ea4bf52284ce8664d11e3de46fdf4/src/bleak_esphome/backend/client.py#L888-L975>
- <https://github.com/Bluetooth-Devices/bleak-retry-connector/blob/0bdeaf11d91aa954ee0973ba06c9aba52ddb9971/docs/source/usage.md>
- <https://github.com/Bluetooth-Devices/bleak-retry-connector/blob/0bdeaf11d91aa954ee0973ba06c9aba52ddb9971/src/bleak_retry_connector/__init__.py#L104-L116>
- <https://github.com/Bluetooth-Devices/bleak-retry-connector/blob/0bdeaf11d91aa954ee0973ba06c9aba52ddb9971/src/bleak_retry_connector/__init__.py#L451-L524>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/CHANGELOG.md#v6130-2026-06-23>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/CHANGELOG.md#v6260-2026-06-25>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/CHANGELOG.md#v630-2026-05-22>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/CHANGELOG.md#v710-2026-09-22>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/docs/bond_persistence.md>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/docs/home_assistant_integration.md>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/advertisement_tracker.py#L10-L16>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/client_mgmt.py#L467-L500>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/const.py#L27-L36>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/manager.py#L1315-L1349>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/manager.py#L532-L600>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/wrappers.py#L308-L333>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/wrappers.py#L431-L447>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/wrappers.py#L567-L598>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/wrappers.py#L600-L686>
- <https://github.com/Bluetooth-Devices/habluetooth/blob/b41bfe7979223812cd50a592dda29b3ad4ea6cd4/src/habluetooth/wrappers.py#L655-L670>
- <https://github.com/Bluetooth-Devices/xiaomi-ble/blob/4dbc9f143a67fed1b113032f5dd3ed703c34f4ae/src/xiaomi_ble/parser.py#L1820-L1900>
- <https://github.com/hbldh/bleak/blob/c7ca1b04b00b0bda5f98ccf9493e24784cfe869d/bleak/__init__.py#L568-L611>
- <https://github.com/hbldh/bleak/blob/c7ca1b04b00b0bda5f98ccf9493e24784cfe869d/bleak/backends/bluezdbus/client.py#L250-L275>

### aioesphomeapi

- <https://github.com/esphome/aioesphomeapi/blob/8d86534f49089f9bd80d50884be012c119700273/aioesphomeapi/client.py#L1442-L1465>

### ESPHome source, docs and changelogs

- <https://esphome.io/changelog/2023.3.0/>
- <https://esphome.io/changelog/2026.8.0/>
- <https://esphome.io/components/bluetooth_proxy/>
- <https://esphome.io/components/bluetooth_proxy/#how-active-connections-work>
- <https://esphome.io/components/bluetooth_proxy/#passive-vs-active-scanning>
- <https://esphome.io/components/esp32_ble/>
- <https://esphome.io/components/esp32_ble_tracker/>
- <https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/bluetooth_connection/bluetooth_connection.h#L51-L65>
- <https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/bluetooth_connection/bluetooth_connection_bluedroid.cpp#L828-L848>
- <https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/bluetooth_connection/bluetooth_connection_rp2.cpp#L216-L235>
- <https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/bluetooth_proxy/bluetooth_proxy.cpp#L388-L405>
- <https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/bluetooth_proxy/bluetooth_proxy.h#L208-L236>
- <https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/bluetooth_proxy/bluetooth_proxy.h#L48-L57>
- <https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/esp32/__init__.py#L1209-L1213>
- <https://github.com/esphome/esphome/blob/3298988f5d166d68a557c469183b3b4a9d3edf30/esphome/components/esp32_ble/ble.h#L244>
- <https://github.com/esphome/esphome/pull/4475>

### BlueZ, Linux kernel, ESP-IDF

- <https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/doc/org.bluez.Adapter.rst>
- <https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/adapter.c#L10922-L10924>
- <https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/adapter.c#L8437-L8471>
- <https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/adapter.c#L9508-L9524>
- <https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/agent.c#L139-L154>
- <https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/agent.c#L247-L261>
- <https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/agent.c#L947-L977>
- <https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/device.c#L3410-L3468>
- <https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/device.c#L7858-L7883>
- <https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/device.c#L7934-L7970>
- <https://github.com/bluez/bluez/blob/fa6ba55020dee3c3183041c854c347b4582448be/src/main.conf#L16-L19>
- <https://github.com/espressif/esp-idf/blob/1f8366c26e56b8066ce243624f94905be175f7ae/components/bt/host/bluedroid/Kconfig.in#L560-L580>
- <https://github.com/espressif/esp-idf/blob/1f8366c26e56b8066ce243624f94905be175f7ae/components/bt/host/bluedroid/bta/dm/bta_dm_co.c#L41-L53>
- <https://github.com/espressif/esp-idf/blob/v5.5.5/components/bt/host/bluedroid/Kconfig.in#L478-L481>
- <https://github.com/espressif/esp-idf/blob/v5.5.5/components/bt/host/bluedroid/bta/dm/bta_dm_co.c#L41-L43>
- <https://github.com/torvalds/linux/blob/2f2f7eeef8b0a1a9b9ca998231419873ccae8d4e/net/bluetooth/mgmt.c#L1149-L1154>
- <https://github.com/torvalds/linux/blob/2f2f7eeef8b0a1a9b9ca998231419873ccae8d4e/net/bluetooth/smp.c#L2326-L2328>
- <https://github.com/torvalds/linux/blob/2f2f7eeef8b0a1a9b9ca998231419873ccae8d4e/net/bluetooth/smp.c#L634-L641>
- <https://github.com/torvalds/linux/blob/2f2f7eeef8b0a1a9b9ca998231419873ccae8d4e/net/bluetooth/smp.c#L846-L877>

### Shelly

- <https://pypi.org/project/aioshelly/13.35.0/>
- <https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/BLE>
- <https://shelly-api-docs.shelly.cloud/gen2/Scripts/APIs/BLE>

### HACS

- <https://hacs.xyz/docs/publish/include>
- <https://hacs.xyz/docs/publish/integration>
- <https://hacs.xyz/docs/publish/start>

### PyPI packages (libraries used by core integrations)

- <https://pypi.org/project/HueBLE/2.2.3/>
- <https://pypi.org/project/py-dormakaba-dkey/1.0.6/>

### bodymiscale

- <https://github.com/dckiller51/bodymiscale/blob/c3e02d196f885474f5c5e937f18f0e818d8da36c/README.md#configuration>

## Addendum (2026-10-16, design session): third-party Beurer integrations on GitHub

Found while choosing the domain name; not part of the original research run.
- [chalimov/ha-beurer-bf600](https://github.com/chalimov/ha-beurer-bf600) (domain `beurer_bf600`, no license, last push 2026-03-20): BF105-family scales (tested on Sanitas SBF73) through an ESPHome proxy with `client.pair()` Just Works bonding. Its README says ESPHome **2024.3.0+** is needed for pairing, where this research found 2023.3.0 (T3.3). It accepts readings from all scale users into one set of sensors plus a "User" sensor, so it does **not** separate people. Its README also mentions a monkey-patch for an `aioesphomeapi` bug with the SBF73's UUID format. It supports the expectation (U4) that ESPHome proxies can bond with this scale family, but the BF450 is still untested.
- [Barma-lej/ha-beurer-bf700](https://github.com/Barma-lej/ha-beurer-bf700) (domain `beurer_bf700`, MIT): BF700 family.
- [pedropombeiro/home_assistant_beurer_bf915](https://github.com/pedropombeiro/home_assistant_beurer_bf915): BF915.
- No core integration uses the domains `beurer`, `beurer_scales`, `beurer_ble` or `beurer_scale` (GitHub API, 404 on `homeassistant/components/<domain>`).

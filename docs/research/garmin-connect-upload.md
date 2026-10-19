# Pushing readings to Garmin Connect from a Home Assistant integration

Research date: 2026-10-20. Scope: an optional, per-scale-user setup where the owner enters Garmin Connect credentials (plus MFA if needed) and the integration pushes each new reading (weight plus body composition) to Garmin Connect. Constraints: asyncio, deps in `manifest.json` `requirements`, no blocking IO in the event loop.

Source snapshots read (all primary; code was read only, never executed against Garmin):
- python-garminconnect (cyberjunky) tag `0.3.19` = `b6264c4` (2026-10-16), also checked `main` @ `5ddb9ee` and its open PR #473. PyPI `garminconnect` 0.3.19 (latest, uploaded 2026-10-16).
- ha-garmin (cyberjunky) tag `v0.1.52` (2026-10-16). PyPI `ha-garmin` 0.1.52. This is the client library of the third-party HA integration `garmin_connect` 3.0.20 (`home-assistant-garmin_connect`).
- garth (matin) PyPI 0.8.0 README (final release, status "Inactive").
- garmin-fit-sdk 21.218.0 (PyPI wheel, the `garmin/fit-python-sdk` package, profile 21.218.0) and fit-tool 0.9.16 (PyPI wheel).
- home-assistant/core tag `2026.10.2` (latest release) and `dev` @ `9b4cd38` (2026-10-19). developers.home-assistant @ `50b5188`.
- PyPI metadata for curl_cffi 0.16.3 and ua-generator 2.1.7.

Labels:
- Source paths are relative to the repo named above; `path:line` refers to the snapshot named above.
- **UNVERIFIED** marks a claim not confirmed by a primary source or by a live test; each one says what would confirm it. Nothing here was tested against a real Garmin account.

## 1. Answers at a glance

| # | Question | Short answer | Confidence |
|---|---|---|---|
| 1 | Maintained library that logs in (incl. email MFA) and uploads? | `garminconnect` **0.3.19**, which dropped garth in 0.3.0 and runs its own login chain. Works headless, no browser. `garth` is dead. | High (code) / **UNVERIFIED** live today |
| 2 | How to upload body composition? | `Garmin.add_body_composition(...)`: builds a FIT `weight_scale` file and POSTs it to `/upload-service/upload`. Prefer it over hand-rolling FIT. | High (code) |
| 3 | Token persistence | Three strings (`di_token`, `di_refresh_token`, `di_client_id`), exported by `client.dumps()` and restored by `Garmin.login(tokenstore=<json string>)`. Store them in a private HA `Store`, not in subentry data. | High |
| 4 | MFA in a config flow | `Garmin(email, password, return_on_mfa=True).login()` returns `("needs_mfa", None)`; the same instance finishes with `resume_login(None, code)`. Keep the instance on the flow handler. All calls blocking, use `hass.async_add_executor_job`. | High |
| 5 | Rate limit risk | Garmin returns 429 per client fingerprint, not just per frequency. Log in rarely, reuse the refresh token. | Medium (maintainer statements; no Garmin doc) |
| 6 | Security | The library nulls the password after a successful login and writes tokens `0600`. Store no password. | High |
| - | HA gotchas | HA 2026.10 requires **Python >=3.14.2**, not 3.13. Subentry flows have **no reauth**. Updating a subentry fires this integration's update listener. | High |

## 2. Q1: Auth state of play and library choice

### 2.1 garth is dead

- The garth 0.8.0 PyPI README opens with "Garth is deprecated and no longer maintained. Garmin changed their auth flow, breaking the mobile auth approach that Garth depends on." The classifier is `Development Status :: 7 - Inactive`. It says "New logins will not work" and an existing session "may continue to work until that token expires (~1 year from when it was issued)". Source: https://pypi.org/project/garth/0.8.0/ (links https://github.com/matin/garth/discussions/222 and issue #217).
- python-garminconnect 0.3.0 removed garth. Its README says: "Since 0.3.0 this library no longer uses `garth`, which is deprecated. Sessions saved by older versions (`oauth1_token.json`/`oauth2_token.json`) cannot be converted." Source: https://github.com/cyberjunky/python-garminconnect/blob/0.3.19/README.md ("Upgrading from 0.2.x (garth)").

### 2.2 Candidates

| Library | Latest | Auth | Deps | Notes |
|---|---|---|---|---|
| **garminconnect** (cyberjunky) | 0.3.19, 2026-10-16 | DI OAuth2 bearer tokens via a 5-strategy login chain | `curl_cffi>=0.15.0`, `requests>=2.33.0`, `ua-generator>=1.0` (pyproject.toml:8-13) | Python >=3.12 (pyproject.toml:`requires-python`). Release cadence about weekly (0.3.16 on 2026-09-18, 0.3.17 on 09-29, 0.3.18 on 10-15, 0.3.19 on 10-16, per PyPI). 3,172 GitHub stars; about 548k PyPI downloads last month (pypistats). Synchronous API. |
| **ha-garmin** (cyberjunky) | 0.1.52, 2026-10-16 | Same login chain, forked into `GarminAuth` | `pydantic>=2.0.0`, `curl_cffi>=0.7.4`, `requests>=2.28.0` | Python >=3.11. Built for HA: `GarminAuth.login()` raises `GarminMFARequired`, then `complete_mfa(code)`; `GarminClient` has async methods that use `asyncio.to_thread`. Used by the `garmin_connect` HA integration (manifest `"requirements": ["ha-garmin==0.1.52"]`). Smaller user base; same author. |
| garth | 0.8.0 | dead | - | Do not use. |

Both live libraries share the same login design: `ha-garmin` auth.py duplicates python-garminconnect's `client/sso.py` and `client/session.py`, and the python-garminconnect source comments say "(ha-garmin order)" (`garminconnect/client/sso.py` `LoginMixin.login` docstring). So they break and get fixed together.

### 2.3 python-garminconnect 0.3.19 auth mechanism

- `Garmin.login()` is documented as "Authentication uses the same mobile SSO flow as the official Garmin Connect mobile apps. No browser is needed." (README.md "Authentication"). First login hits `sso.garmin.com/mobile/api/login` with iOS client id `GCM_IOS_DARK`. The service ticket is exchanged for DI OAuth bearer tokens (`access_token` plus `refresh_token`) at `diauth.garmin.com`.
- Strategy chain, in order: `mobile+cffi`, `mobile+requests`, `widget+cffi`, `portal+cffi`, `portal+requests` (`garminconnect/client/sso.py:157-167`). Only wrong credentials (`GarminConnectAuthenticationError`) and MFA stop the chain; 429s and Cloudflare HTML challenges fall through to the next strategy (sso.py:215-245). When every strategy returns 429 it raises `GarminConnectTooManyRequestsError`; otherwise it raises `GarminConnectConnectionError("All login strategies exhausted ...")`.
- With `verify_login=True` (default) each strategy's token is checked against the API tier (`_verify_token`) before it is accepted (`garminconnect/client/session.py:87`).
- It sleeps deliberately inside login: 3-8 s in the widget flow, 10-20 s in the portal flow, to avoid Cloudflare bot heuristics (`garminconnect/client/constants.py` `LOGIN_DELAY_*`, `WIDGET_DELAY_*`; `sso.py:494-496`, `711-716`). A single `login()` can therefore take tens of seconds. Blocking, so executor only.
- `curl_cffi` is a hard dependency. Without it the library warns that "the plain-requests fallbacks are usually blocked by Cloudflare" (sso.py:`login`).

### 2.4 Is it working right now? (open issues)

Checked via the GitHub API on 2026-10-20 (`gh api repos/cyberjunky/python-garminconnect/issues`):
- There is **1 open item and it is a PR** (#473, see 4.3). No open bug issues. Recently closed, relevant:
  - #453 "0.3.17 mobile+requests: first SSO request returns 429 without Retry-After", closed 2026-10-16. The maintainer reply: Garmin's login endpoints "return 429 based on the client fingerprint, not only request frequency, including from IPs that have never been used before"; a wrapper that keeps only `mobile+requests` is not the supported configuration (the default chain tries `mobile+cffi` first). https://github.com/cyberjunky/python-garminconnect/issues/453
  - #444 "Valid DI token gets 403 on every connectapi call", closed 2026-10-16. The reporter found the cause was a `default` entry in `~/.netrc` that `requests` applied over the bearer header; removing it fixed the issue on 0.3.18. Lesson: `requests` honours `~/.netrc`, which can corrupt the Authorization header (on HA OS there is normally no netrc; UNVERIFIED for container installs). https://github.com/cyberjunky/python-garminconnect/issues/444
  - #386 "Widget MFA detects email MFA but no OTP is delivered", closed. A later comment (2026-08-20, 0.3.10) reports the email MFA flow works: `mobile+cffi` 429, `mobile+requests` 429, `widget+cffi` MFA challenge, OTP delivered, `resume_login` completed, then 24 consecutive token resumes with no MFA. This is a user report, not a test. https://github.com/cyberjunky/python-garminconnect/issues/386
  - #369 "socialProfile returns 401 Token is not active since ~2026-06-01", closed 2026-07-09 (fixed by token validation plus fallback strategies, README "Cached-token gotcha").
  - #350, #344, #337 (429 and Cloudflare during login, spring 2026), all closed.
- Conclusion: login breakage happens in bursts (April 2026, June 2026, early October 2026), each fixed within days by new releases. The library is maintained, but the login path is a moving target. Whether a login from the owner's network succeeds today is **UNVERIFIED**; only a live run confirms it. Plan for 429 and fingerprint blocks (section 6).

### 2.5 Pin recommendation

- `"requirements": ["garminconnect==0.3.19"]` in `manifest.json`. HA custom integrations should pin exact versions (HACS and the hassfest check expect `==` pins for requirements; hassfest rule: UNVERIFIED here, not read). Pinning transitive `curl_cffi` is not necessary; `garminconnect` pulls `curl_cffi>=0.15.0`, and 0.16.3 satisfies it.
- Bump deliberately: because Garmin changes break login, expect to bump the pin a few times a year. Re-test the MFA flow on each bump.
- Fallback if the maintainer abandons it: `ha-garmin==0.1.52` has the same engine with an HA-shaped two-step API. Keep the Garmin code behind one small module (`garmin_client.py` in the integration) so the library can be swapped.

## 3. Q2: Upload method

### 3.1 `add_body_composition` (python-garminconnect 0.3.19)

Source: `garminconnect/api/body.py:57-105` (https://github.com/cyberjunky/python-garminconnect/blob/0.3.19/garminconnect/api/body.py).

```python
def add_body_composition(
    self,
    timestamp: str | None,          # ISO 8601 string, e.g. "2026-10-20T07:31:00+02:00"; None means now
    weight: float,                  # kg, must be positive and finite
    percent_fat: float | None = None,        # %
    percent_hydration: float | None = None,  # % (body water)
    visceral_fat_mass: float | None = None,  # kg
    bone_mass: float | None = None,          # kg
    muscle_mass: float | None = None,        # kg
    basal_met: float | None = None,          # kcal/day
    active_met: float | None = None,         # kcal/day
    physique_rating: float | None = None,    # unitless (uint8)
    metabolic_age: float | None = None,      # years (uint8)
    visceral_fat_rating: float | None = None,# index (uint8)
    bmi: float | None = None,                # kg/m^2
) -> dict[str, Any]
```

- Timestamp handling: `datetime.fromisoformat(timestamp)`; a naive string is localised to the user's Garmin profile timezone (`_localize`, `_user_tzinfo`, using `get_user_timezone()`); an aware string is used as is. The FIT timestamp is then `dt.timestamp() - 631065600` (UTC seconds since 1989-12-31, `garminconnect/fit.py:409-415`), so an **aware** timestamp is unambiguous. The method needs `str`, not `datetime`. Passing a naive local time from the host is how 0.3.18 and earlier produced wrong days; 0.3.19 CHANGELOG "Timestamps you pass in without an offset are now also read as your profile's local time rather than the host's". Always pass an aware ISO string.
- Units and scaling are applied by the encoder: weight, percent_fat, percent_hydration, visceral_fat_mass, bone_mass, muscle_mass are written as `uint16` with scale 100 (2 decimals); basal_met and active_met scale 4; bmi scale 10 (1 decimal); physique_rating, metabolic_age, visceral_fat_rating `uint8` (`garminconnect/fit.py:472-503`). These match the official FIT profile for message 30 `weight_scale` (section 3.3). `None` is written as the FIT "invalid" value, i.e. field absent.
- Nit: `FitBaseType.pack` does `int(value * scale)`, which truncates (`fit.py:177-182`, `240-252`), so `4.35` can become `434` (4.34 kg) through float error. A 0.01 kg error is irrelevant here, but pre-round values with a small epsilon if exactness matters.
- Upload: `self.client.post("connectapi", "/upload-service/upload", files={"file": ("body_composition.fit", bytes)}, api=True)` (body.py:`add_body_composition` tail; `garmin_connect_upload` is set at `api/base.py:295`). It returns the parsed JSON body. HTTP >=400 raises `GarminConnectConnectionError` whose message is `API Error <status> - <Garmin text>` and whose `.response` holds the response (`garminconnect/client/transport.py:244-277`). 401 triggers one token refresh and a retry with the file rewound (transport.py:228-241).
- Which fields Garmin keeps: the FIT file carries all 13 fields above, but what the Garmin backend actually stores and displays is **UNVERIFIED** (no Garmin doc, not tested live). Confirm by uploading once and reading back with `Garmin.get_daily_weigh_ins(date)` (returns `dateWeightList`) in a throwaway account.
- The library also has `add_weigh_in(weight, unitKey="kg", timestamp="")`, a JSON POST to `/weight-service/user-weight` with `sourceType: "MANUAL"` (body.py:107-140). That endpoint carries weight only, no body composition, and is marked MANUAL. Not suitable.
- Mapping from the integration's readings (field names in `CONTEXT.md` and the issue brief):

| Reading | `add_body_composition` argument |
|---|---|
| `weight_kg` | `weight` |
| `body_fat` % | `percent_fat` |
| `body_water` % | `percent_hydration` |
| `bone_mass` kg | `bone_mass` |
| `total_muscle_mass` kg | `muscle_mass` |
| `visceral_fat` (index) | `visceral_fat_rating` (**not** `visceral_fat_mass`, which is kg) |
| `metabolic_age` years | `metabolic_age` |
| `bmi` | `bmi` |
| reading timestamp | `timestamp` (aware ISO string) |

  Beurer BMR (`basal_met`) if the reading has it: kcal/day; `active_met` has no Beurer equivalent. `physique_rating` has none either.

### 3.2 Alternative: build the FIT yourself and call `upload_activity`

- `Garmin.upload_activity(activity_path)` takes a **file path**, checks the extension is FIT, GPX or TCX, and posts to the same `/upload-service/upload` endpoint (`garminconnect/api/activities.py:200-243`). It is therefore the same HTTP call as `add_body_composition`, just fed by a file on disk. It forces a temp file and gives no benefit.
- There is also `import_activity(path)`, which posts to `/upload-service/upload/<EXT>` with mobile headers and maps HTTP 409 to a "duplicate" error (activities.py:300-350). That is for activities, not weigh-ins; whether the weight FIT is accepted on that URL is **UNVERIFIED**.
- Building the FIT yourself with Garmin's SDK is now feasible: `garmin-fit-sdk` 21.218.0 (Garmin's own `fit-python-sdk`, license "FIT Protocol License Agreement", `Requires-Python >=3.6`, no runtime deps) **has an `Encoder`** (`garmin_fit_sdk/encoder.py`, README "Encoder" section). Earlier versions were decode-only; verified in the 21.218.0 wheel. `fit-tool` 0.9.16 (pure Python, no runtime deps) also writes FIT and has a `weight_scale_message`.
- Official FIT `weight_scale` (global message 30) fields, read from `garmin_fit_sdk/profile.py` 21.218.0: `253 timestamp date_time s`, `0 weight uint16 x100 kg`, `1 percent_fat uint16 x100 %`, `2 percent_hydration uint16 x100 %`, `3 visceral_fat_mass uint16 x100 kg`, `4 bone_mass uint16 x100 kg`, `5 muscle_mass uint16 x100 kg`, `7 basal_met uint16 x4 kcal/day`, `8 physique_rating uint8`, `9 active_met uint16 x4 kcal/day`, `10 metabolic_age uint8 years`, `11 visceral_fat_rating uint8`, `12 user_profile_index message_index`, `13 bmi uint16 x10 kg/m^2`. The python-garminconnect encoder matches this table field for field (except it does not write `user_profile_index`).

### 3.3 Recommendation

Use `add_body_composition`. Reasons: it is the maintained, already field-compatible path; it produces the identical upload request as the do-it-yourself route; it avoids a new dependency and a temp file; the FIT layout matches Garmin's published profile. Revisit a Garmin-SDK encoder only if the hand-written encoder is found to lose data (for example the `int()` truncation, or `file_id`/`device_info` choices that make Garmin classify the source oddly, **UNVERIFIED**). If a FIT is built independently, upload bytes with the same POST (not via `upload_activity`, which needs a path).

### 3.4 Duplicate handling

- For **activity** FIT uploads Garmin returns HTTP 409 for a duplicate, with `detailedImportResult.failures[].messages[].content` text and code 202; python-garminconnect surfaces it as `API Error 409 - Duplicate Activity.` (CHANGELOG 0.3.19 "Upload errors show Garmin's message text"; `activities.py:338-341`; `transport.py:244-277`).
- For a **weight_scale** FIT uploaded twice with the same timestamp: **UNVERIFIED**. No primary source states the behaviour (the library neither detects nor documents it; `body.py` returns the raw JSON). Two possible outcomes: a 409 or a `failures` entry (idempotent), or a second weigh-in row. Confirm with a live test: upload the same FIT twice and read `get_daily_weigh_ins`. Until then, make the integration idempotent itself: persist the last pushed reading timestamp per scale user and never re-send the same one; treat `GarminConnectConnectionError` with "409" or "Duplicate" in the message as success.
- Timestamps use second resolution; the Beurer reading timestamp is the scale's own timestamp, so a retry of the same reading reuses the same value, which is what makes dedupe possible.

## 4. Q3: Token persistence, lifetime, refresh, storage in HA

### 4.1 What is stored and its format

- Token file / string is JSON with exactly three keys: `di_token` (access token, a JWT), `di_refresh_token`, `di_client_id` (`garminconnect/client/tokens.py:117-124`, `dumps()`). Default location when a path is given: `<dir>/garmin_tokens.json`; README: `~/.garminconnect/garmin_tokens.json`, mode `0600`, directory `0700` (README "Token storage"; `tokens.py:126-168`).
- The format is **not** garth's: old `oauth1_token.json`/`oauth2_token.json` stores are rejected with a clear error (`api/base.py:503-512`).
- `Garmin.login(tokenstore=...)` accepts either a directory/`.json` path or **an inline JSON string** (`_looks_like_json`, `api/base.py:454-456`: `self.client.loads(tokenstore)`). So no file on disk is required, which suits HA. `GARMINTOKENS` env var is also read (base.py:443); do not rely on it in HA.
- `Client.dumps()` returns the JSON string; `Client.loads(str)` restores it.

### 4.2 Lifetime and refresh

- The access token is a JWT; `_token_expires_soon()` decodes `exp` without verifying and treats it as expiring 900 s early (`tokens.py:95-115`). Before every API request `_run_request` refreshes if close to expiry, and on a 401 it refreshes and retries once (`transport.py:210-241`).
- Refresh = `POST https://diauth.garmin.com/di-oauth2-service/oauth/token` with `grant_type=refresh_token` (`client/session.py:246-280`). A new refresh token returned by Garmin replaces the old one (`session.py`: `self.di_refresh_token = data.get("refresh_token", self.di_refresh_token)`). **So the stored token string changes after refreshes and must be re-persisted**; losing a rotated refresh token could force a new login.
- `Garmin.login(tokenstore=path)` also refreshes proactively on load when the token is about to expire, "to avoid hitting the SSO login endpoint (which may be Cloudflare-blocked)" (base.py:472-482).
- Lifetime: README says "DI tokens auto-refresh indefinitely as long as the refresh token remains valid. A full re-login with credentials (and possibly MFA) is only needed if the refresh token itself expires or is revoked." The refresh token's actual lifetime is **UNVERIFIED** (no number in any primary source; garth's README gives "~1 year" for its different OAuth1 token). Confirm by decoding `exp` of the access token (short) and observing the refresh over months.
- Persistence hook: when a path is set, the library writes refreshed tokens back itself (`_persist_tokens`, `session.py:281-285`, errors suppressed). With an inline JSON string nothing is written back, so the integration must call `client.dumps()` after each operation and save when it changed.

### 4.3 Where to store them in HA

Facts:
- Config entry data and subentry data are stored in `.storage/core.config_entries` (`ConfigEntryStore`, `homeassistant/config_entries.py:137,2170` @ 2026.10.2) via a `Store` created without `private=True`. `Store(..., private=False)` is the default and `private=False` makes the written file mode `0o644` (`homeassistant/helpers/storage.py:229-235`; `homeassistant/util/file.py:19-39,47-62`). So tokens in entry data are plaintext, world-readable inside the HA config directory, and are included in backups. This is how all HA integrations store secrets today (the third-party `garmin_connect` integration stores `token`, `refresh_token`, `client_id` in config entry data: `config_flow.py:78-86`, `_token_data()`; UNVERIFIED for its security rationale).
- `hass.config_entries.async_update_subentry(entry, subentry, data=...)` exists and "If the subentry was changed, the update_listeners are fired" (`config_entries.py:2840-2878` @ 2026.10.2). It must run in the event loop thread.
- **This repo's update listener reloads when subentry data change.** `custom_components/beurer_scales/__init__.py:64` registers `_async_reload`, which compares `subentries_snapshot(entry)` (`coordinator.py:185-193`, which includes `dict(subentry.data)`) with the snapshot at setup and calls `async_schedule_reload` on any difference. Writing a rotated Garmin token into subentry data would therefore reload the whole entry and cut off a sync in progress. The same file already warns about this ("must not reload: that would cut off a sync in progress").
- The repo already uses `Store` helpers per entry (`coordinator.py:141`, `pending_deletions.py:38`).

Recommendation:
- Keep subentry data limited to **non-secret settings**: a boolean "push to Garmin enabled", maybe the Garmin display name for the title. Keep refreshable tokens in a dedicated `Store(hass, 1, f"{DOMAIN}.{entry_id}.garmin_tokens", private=True, atomic_writes=True)` with one record per subentry id (`private=True` gives `0600`). Use `async_delay_save` after refreshes to avoid write storms. Delete the record in `async_remove_entry` and when a subentry is removed or Garmin push is disabled. Do not write tokens into entry or subentry data, and redact any token fields in diagnostics (`async_redact_data`, https://developers.home-assistant.io/docs/core/integration/diagnostics).
- Alternative if a single file is wanted: put tokens in subentry data and make `subentries_snapshot` ignore the token key. Worse: the plaintext copy lands in `core.config_entries` (`0644`) and in every HA backup and diagnostics download.

### 4.4 Reauth in HA

- Primary doc: https://developers.home-assistant.io/docs/integration_setup_failures#handling-expired-credentials (`docs/integration_setup_failures.md:74-96`). Raise `ConfigEntryAuthFailed` from `async_setup_entry` or from a `DataUpdateCoordinator`, and HA puts the entry in a failure state and starts a reauth flow. "If your integration does not use a `DataUpdateCoordinator`, calling `entry.async_start_reauth()` can be used as an alternative." Reauth flow context: `source=SOURCE_REAUTH`, `entry_id`, `unique_id`. Flow handler pattern: `async_step_reauth` then `async_step_reauth_confirm`, finish with `async_update_reload_and_abort(self._get_reauth_entry(), data_updates=...)` (https://developers.home-assistant.io/docs/core/integration/config_flow#reauthentication, `config_flow.md:334-403`).
- **Limitation for this design:** "Subentry flows don't support reauthentication or discovery; a subentry flow can only be initiated via the `user` or `reconfigure` steps." (`docs/core/integration/config_flow.md:438`). `async_start_reauth` exists only on `ConfigEntry` (`config_entries.py:1389`). Garmin credentials belong to one scale user (a subentry), so a Garmin auth failure for one user cannot start a subentry reauth. Also, raising `ConfigEntryAuthFailed` would block the whole scale entry for one user's Garmin problem, which is wrong for this integration (BLE sync must keep working).
- Pattern that fits: on a Garmin auth failure, **do not** raise `ConfigEntryAuthFailed`. Disable pushing for that scale user, create a repair issue (`ir.async_create_issue(..., is_fixable=True, severity=ERROR, translation_key=..., data={"subentry_id": ...})`, https://developers.home-assistant.io/docs/core/platform/repairs) and offer a `RepairsFlow` or the subentry **reconfigure** step to run the login and MFA again. The repo already has a `repairs.py` and per-scale-user repair issues (`__init__.py:async_clear_orphaned_scale_user_issues`), so the pattern exists. Clear the issue when new tokens are stored.
- Distinguish errors: 401/invalid refresh = reauth needed; 429, 403, timeouts, 5xx = transient, keep the tokens, retry later, do not alarm the user.

## 5. Q4: MFA in an HA config flow and blocking calls

### 5.1 MFA API (python-garminconnect 0.3.19)

- `Garmin(email, password, prompt_mfa=None, return_on_mfa=False, ...)` (`api/base.py:58-66`).
  - `prompt_mfa`: a synchronous callable returning the code. Usable in a CLI; not usable in a config flow (blocks the thread while waiting for UI input).
  - `return_on_mfa=True`: `login()` returns `("needs_mfa", None)` instead of prompting; "caller must call `resume_login()`" (`client/sso.py:102-124`; `api/base.py:516-523`).
  - `Garmin.resume_login(client_state, mfa_code)` completes the login and then loads profile and settings (`api/base.py:687-698`). In 0.3.19 `client_state` is **ignored**; the pending MFA state (session cookies, CSRF, flow name) lives on the instance that started the login (`client/sso.py:248-267`, `session.py:62-79`). A wrong code leaves the pending session intact so the user can retry (docstring of `resume_login`); a second `login()` on the same instance while MFA is pending raises `GarminConnectAuthenticationError`.
  - In the `return_on_mfa` path, `Garmin.login` returns before dumping tokens (`base.py:516-523`), so after `resume_login` call `garmin.client.dumps()` yourself.
- Open PR #473 (2026-10-19, not merged, UNVERIFIED for release) would make `client_state` a JSON-serialisable dict so a different instance or process can finish the login. Not needed in HA because a config flow handler instance lives across steps; do not depend on it.
- Email MFA works through the widget or portal flow; the library requests the code delivery itself (`_widget_request_mfa_code`, sso.py:622-668). Delivery was fixed in 0.3.10 (issue #386 above). SMS/authenticator-app MFA: the library reads `mfaMethod` from the response (`_mfa_method`, `sso.py:753-757`), but only email was seen working in the issue tracker; others **UNVERIFIED**.

### 5.2 Flow shape

1. Subentry flow step `user` (or a "Garmin" step in the scale-user subentry flow): form with `email` and `password` (password in a `TextSelector(type=PASSWORD)`).
2. In the handler: `self._garmin = Garmin(email, password, return_on_mfa=True)`; `status, _ = await hass.async_add_executor_job(self._garmin.login)`.
3. If `status == "needs_mfa"`: `return await self.async_step_garmin_mfa()`; a form with one field `mfa_code`. On submit: `await hass.async_add_executor_job(self._garmin.resume_login, None, code)`. On `GarminConnectAuthenticationError` re-show with `errors={"base": "invalid_mfa"}` (the instance keeps its pending state, so retrying works).
4. On success: `tokens = self._garmin.client.dumps()`, save into the `Store` under the subentry id, discard the password and the instance (`self._garmin = None`), create/update the subentry with `{"garmin_enabled": True, "garmin_name": <display name>}`.
5. If no MFA: tokens are on the instance right after step 2; go straight to step 4.

The `garmin_connect` third-party integration uses the same shape (`config_flow.py:205-258`: `async_step_user` then `async_step_mfa`, both via `hass.async_add_executor_job`; `ha-garmin` raises `GarminMFARequired` instead). That is evidence the pattern is accepted in a gold-quality-scale integration, not a Garmin guarantee.

Consequences of the flow living in memory: if the user abandons the form or HA restarts, the pending MFA session is lost and the user simply starts again (a fresh email code is sent). Do not log the code, email or password.

### 5.3 Blocking IO

- Primary doc: https://developers.home-assistant.io/docs/asyncio_blocking_operations ("In Home Assistant this is usually accomplished by calling `await hass.async_add_executor_job`", `docs/asyncio_blocking_operations.md:18,30`). Every python-garminconnect call is synchronous (`requests` and `curl_cffi.requests` sessions, `time.sleep` in login, `threading.RLock`), so every call must go through `hass.async_add_executor_job` (login, resume_login, add_body_composition, token refresh through any API call).
- Library-internal threading is safe for this use: token state is guarded by an `RLock` (`client/__init__.py` `_token_lock`). Serialise Garmin calls per scale user with an `asyncio.Lock` so two readings do not race on one token.
- A login may take 10-40 s (sleeps plus up to five strategies with 10-30 s timeouts: `transport.py` default timeout 15 s; sso `timeout=30`). Config flow steps may take that long, but show a progress/loading step only if it exceeds a few seconds in testing (UNVERIFIED). For the readings, push in a background task (`entry.async_create_background_task`) so BLE processing never waits on Garmin.
- The first `Garmin` use also does lazy imports of `curl_cffi` (native extension); doing that in the executor (it is, since the first call is `login`) avoids the "import in event loop" warning (https://developers.home-assistant.io/docs/asyncio_imports, `docs/asyncio_imports.md:26`). Do not `import garminconnect` at module top level in `config_flow.py`/`__init__.py` of the integration unless tested: the import of `curl_cffi` happens at `garminconnect.client` import time (`client/__init__.py` try-import). Import inside the executor function, or verify HA's blocking-import detector stays silent.

### 5.4 HTTP stack and dependency conflicts

- Stack: `requests` for the API session and for `+requests` strategies, `curl_cffi` (libcurl with browser TLS impersonation) for `+cffi` strategies, `ua-generator` for User-Agents. `requests.Session` mounts `HTTPAdapter` (`client/__init__.py`).
- Home Assistant pins (core tag 2026.10.2, `homeassistant/package_constraints.txt`, `pyproject.toml`): `requests==2.34.2` (pyproject.toml:72; constraints:66), `pydantic==2.13.5` (constraints:144), `cryptography==50.0.1` (constraints:32), `cffi==2.0.0` (constraints:285), `certifi>=2021.5.30`, `urllib3>=2.0`. `dev` @ `9b4cd38` moves to `requests==2.34.2`, `pydantic==2.14.1`, `cryptography==50.0.2`, `cffi==2.0.0`.
- garminconnect 0.3.19 `requires_dist`: `curl_cffi>=0.15.0`, `requests>=2.33.0`, `ua-generator>=1.0`, `tzdata` (Windows only). Extras (`pydantic>=2.4.0` for `typed`/`workout`) are not installed by default. Result: **no conflict** with `requests==2.34.2` (>=2.33 satisfied).
- curl_cffi 0.16.3 `requires_dist`: `cffi>=2.0.0`, `certifi>=2024.2.2`. HA pins `cffi==2.0.0` (satisfies `>=2.0.0`), `certifi` is only a lower bound in HA's constraints (HA OS ships a recent one). Not conflicting. It does **not** require `cryptography` or `pydantic` at runtime (`cryptography<47` appears only in its dev/test extras). **However** HA constraints pin `cryptography==50.0.1`, which is outside curl_cffi's *dev/test* extra `cryptography<47.0,>=46.0.4`; irrelevant for installs without those extras.
- curl_cffi wheels (PyPI 0.16.3): `cp310-abi3` for manylinux x86_64/aarch64/armv7l/i686 and **musllinux_1_2 x86_64/aarch64** (HA OS and HA container images are Alpine/musl), so a binary wheel exists for every Python >=3.10 including 3.14. HA 32-bit arm/old CPUs: armv7l manylinux only; musl armv7 wheel is absent in the file list (HA dropped 32-bit support; UNVERIFIED for this project).
- `curl_cffi` is **not** a dependency of any core integration (not in `requirements_all.txt` 2026.10.2), so installing it into HA's venv by a custom integration is possible but unusual; each HA update that bumps `cffi`, `requests` or `certifi` can trigger a re-resolve. UNVERIFIED how HA's `pip` resolution behaves if a future core pin conflicts: HA installs custom requirements with the constraints file, so a conflict fails the integration's requirement install (documented behaviour, not read in this pass).
- ha-garmin: needs `pydantic>=2.0.0` (satisfied by HA's pin) in addition to the same libs.
- **Python version:** HA 2026.10.2 and dev require **Python >=3.14.2** (`pyproject.toml:24`). The brief's "Python 3.13" does not match; garminconnect (>=3.12) and curl_cffi (abi3 >=3.10) both support 3.14 (garminconnect classifiers list 3.12, 3.13, 3.14).

## 6. Q5: Rate limits and ban risk

Primary-source facts (all from the library maintainers; Garmin publishes no login rate-limit document, **UNVERIFIED** as an official policy):
- The maintainers state Garmin returns 429 on login "based on the client fingerprint, not only request frequency, including from IPs that have never been used before" (issue #453 reply; PR #449 body, https://github.com/cyberjunky/python-garminconnect/pull/449, which reworded the error text for that reason).
- Cloudflare in front of SSO flags "rapid GET to POST sequences as bot-like", hence the deliberate 3-20 s delays (`client/constants.py` comments "Anti-WAF delay bounds").
- The widget flow and the MFA verify endpoints are described as "a different rate-limit bucket" (constants.py; `sso.py:_complete_mfa` comment "may be on different rate-limit buckets"), i.e. buckets per endpoint are inferred by the maintainers, not documented by Garmin.
- 429 on token refresh: `ha-garmin` raises `GarminRateLimitError("DI token refresh rate limited (429)")` (`auth.py:~1195`), so refresh calls can be limited too. The `garminconnect` refresh treats any non-OK as `GarminConnectAuthenticationError("DI token refresh failed: <status>")` (`session.py:246-280`), **which conflates 429 with a revoked token**. Handle it: if the status is 429 do not delete the stored tokens; the library does not expose the status in that exception (the number is in the message string only).
- Account lock risk after repeated bad passwords: the library error text lists "Account locked - check https://sso.garmin.com" as a possible cause of 401 (`api/base.py` login except block). Garmin's lock thresholds are **UNVERIFIED**.
- No evidence of permanent bans for token-based API use in the primary sources read. Whether an upload volume limit exists is **UNVERIFIED**; a scale produces a handful of readings per day, far below any plausible limit.

Design consequences:
- Log in exactly once per setup (config flow). Afterwards only use stored tokens plus refresh. Never fall back to a password login automatically in the background (that is the thing that triggers 429 and MFA emails). Do not store the password, so an automatic re-login is impossible by construction.
- Do not call `login()` at HA startup. Create the `Garmin` object lazily and load the token with `Garmin().login(tokenstore=<json string>)`. Be careful: with a token string and no credentials, `login()` loads tokens, may refresh, then calls `socialProfile` and `user-settings` (two or three API calls: `base.py:537-543`, `624-686`), which is harmless but adds latency at setup; it also tries a fresh credential login only if credentials were passed (`base.py:545-570`), so pass none.
- Back off: on `GarminConnectTooManyRequestsError` or 429/403, keep the reading in a retry queue, retry with exponential backoff (for example 5 min, 30 min, 2 h, then every 6 h), cap at a day, and never re-login.
- Queue and retry readings (a small per-subentry list in the same private `Store`) so a Garmin outage or 429 does not lose weigh-ins.

## 7. Q6: Security

- Password handling: `Garmin.__init__` stores `self.password`; after a successful login it sets `self.password = None` ("Successful authentication no longer needs the plaintext password. Keeping it would enlarge the credential exposure window", `api/base.py:566-572`). The `Client` never stores it; `dumps()` has only the three token fields (`tokens.py:117-124`). The fallback path (cached tokens rejected, then re-login) needs the password and therefore only works if you passed it; with a token-only design it is not used.
- Tokens on disk, if a path is given: `0600` file in `0700` directory, atomic write with `O_CREAT|O_EXCL|O_NOFOLLOW`, symlinks in the path rejected (`tokens.py:21-62,126-168`; fix for GHSA-wjhr-76vg-2hvc per the code comment). The README: "Treat the token file like a password: the refresh token can provide persistent account access." and `SECURITY.md` says "stores your login tokens in your home folder". `logout()` deletes only the local file and "does not revoke a token that has already been issued by Garmin" (README "Authentication"). To revoke you must use Garmin account security settings. Do not pass a path in HA, pass an inline string and own the storage (section 4.3).
- Logging: the library redacts query values such as `?ticket=` (`constants.py` `_QUERY_VALUE_RE`) and does not log the response body of an unexpected SSO response (CHANGELOG 0.3.19). Keep HA loggers for `garminconnect` at INFO or higher; never log `client.dumps()`.
- Allowed hosts: the library only talks to `garmin.com` / `garmin.cn` (`ALLOWED_DOMAINS`, constants.py; `_resolve_url` rejects other hosts, transport.py).
- Recommendation: **do not store the password.** Use it once in the config flow, then drop it. Store only the three-field token JSON in a `private=True` `Store`, keyed by subentry id. The cost is that a revoked or expired refresh token requires the user to re-enter the password (and MFA), which is acceptable and avoids silent repeated logins (section 6). HA has no secrets vault for custom integrations; the tokens remain plaintext on the HA host either way (UNVERIFIED that any encrypted store exists in core 2026.10).

## 8. Open questions (need a live test or a Garmin source)

1. Does the owner's network get past login today (429 or Cloudflare)? Run the flow once on the HA host.
2. Duplicate behaviour of a `weight_scale` FIT with an identical timestamp (409, ignored, or second row).
3. Which of the 13 FIT fields Garmin displays for a body-composition entry from a non-Garmin source, and whether `visceral_fat_rating` and `metabolic_age` survive.
4. Real refresh-token lifetime.
5. Whether the account's profile timezone (`get_user_timezone`) and the aware timestamps interact correctly for readings that were taken while HA was offline (backlog readings are discarded per ADR 0005, so this applies only to live readings).
6. SMS or authenticator-app MFA through `return_on_mfa`.
7. hassfest/HACS expectations for pinned requirements and for importing a native-wheel library (not read in this pass).

## 9. Recommended design

**Library:** `garminconnect==0.3.19` in `custom_components/beurer_scales/manifest.json` `requirements` (brings `curl_cffi>=0.15.0`, `requests>=2.33.0`, `ua-generator`; compatible with HA 2026.10.2 pins; HA runs Python >=3.14.2). Wrap it in one module (`garmin.py`) exposing four coroutines: `async_login(email, password) -> LoginResult`, `async_finish_mfa(code)`, `async_push_reading(tokens, reading) -> new_tokens`, and an error mapping. Everything blocking runs in `hass.async_add_executor_job`.

**Setup flow (per scale user, optional):**
1. In the scale-user subentry flow, add a "Send readings to Garmin Connect" toggle. When on, show a credentials step (`email`, `password`).
2. Executor: `Garmin(email, password, return_on_mfa=True).login()`. Map `GarminConnectAuthenticationError` to `invalid_auth`, `GarminConnectTooManyRequestsError` to `rate_limited`, `GarminConnectConnectionError` to `cannot_connect`.
3. If it returns `"needs_mfa"`, show an MFA step; executor `resume_login(None, code)`; wrong code re-shows the form (same instance).
4. On success read `garmin.client.dumps()`, save it in `Store(f"{DOMAIN}.{entry_id}.garmin_tokens", private=True)` under the subentry id, then drop the instance and the password. Subentry data gets only `garmin_enabled: True`.
5. Also expose a reconfigure step to re-run steps 1-4 and a way to turn Garmin off (deletes the stored tokens).

**What to persist:** tokens JSON (3 keys) per subentry in the private `Store`; a small retry queue of unsent readings (timestamp plus metrics) and the timestamp of the last successfully pushed reading per subentry for idempotence. No password. No tokens in config entry or subentry data (it would trigger the entry reload listener, and land in a `0644` file).

**Push path:** when the coordinator finalises a (non-guessed, non-backlog, already-accepted) reading for a subentry with Garmin enabled, enqueue it and run a background task guarded by a per-subentry `asyncio.Lock`: load tokens (`Garmin().login(tokenstore=<json>)`), call `add_body_composition(timestamp=<aware ISO>, weight=..., percent_fat=..., percent_hydration=..., bone_mass=..., muscle_mass=..., visceral_fat_rating=..., metabolic_age=..., bmi=...)`, then compare `client.dumps()` with the stored value and re-save if changed. Never `await` Garmin inside the BLE sync path.

**Error handling:**
- Success or a "Duplicate" 409: mark the reading sent.
- Auth failure (token refresh rejected, 401, `GarminConnectAuthenticationError`): stop pushing for that scale user, keep the queue, raise a fixable repair issue with the subentry id (not `ConfigEntryAuthFailed`, because subentry flows cannot reauth and the BLE sync must keep running); the repair or reconfigure step runs the login plus MFA again and clears the issue.
- 429, 403, timeouts, 5xx (`GarminConnectTooManyRequestsError`, `GarminConnectConnectionError`): keep tokens and queue, retry with exponential backoff (cap about 6 h), log once at warning, never log in again automatically.
- Validation errors (`ValueError` for non-positive weight and similar): drop the reading from the queue and log at debug.
- Diagnostics redact tokens and e-mail.

**Before shipping:** run the open questions 1-3 against a throwaway Garmin account (login with MFA, upload the same FIT twice, read back `get_daily_weigh_ins`), and add a feature flag so the integration works unchanged when the library cannot be installed.

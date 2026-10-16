# No integration-owned BlueZ pairing agent; ESPHome proxies are the primary path

The scale only stays connected when it is bonded (Just Works). On a stock HA OS local adapter, bonding is expected to fail: HA registers no BlueZ pairing agent and the adapter is not pairable (research §T3). We do not register our own agent, as `tools/probe.py` does, because that takes over the system's default agent and is unverified on HA OS. Core reviewers may also reject it. The integration accepts any connectable path and always requests pairing. ESPHome ESP32 proxies bond automatically and are the primary, tested path. When pairing fails, a repair issue tells the user to add an ESPHome proxy or make the host adapter pairable.

## Consequences

HA OS users with only a local adapter cannot use the integration until an agent exists. Revisit this if that turns out to be a common setup.

Any connectable path may bond; we do not try to pin the scale to one proxy. Evidence that the scale keeps more than one bond: during reverse engineering, a phone that had bonded earlier stayed bonded and kept working with the Beurer app while a second host bonded too. An opt-in "register a pairing agent" option for HA OS local adapters is a tracked follow-up, not part of v1, because nobody can test it.

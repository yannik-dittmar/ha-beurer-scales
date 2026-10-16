# Protocol code lives inside the integration, not in a separate library

All code, including the BLE protocol (frame decoders, UCP, per-model tables, the sync session), lives in a single HACS custom integration under `custom_components/`. Home Assistant core requires device protocols to live in a separate PyPI library, so a later core submission would mean extracting the protocol first. We accept that cost because a core submission is not a goal now, and one package is simpler to build, release and install.

## Consequences

Keep the protocol modules free of Home Assistant imports anyway. That keeps the decoders testable against the captured fixtures without HA and leaves extraction cheap.

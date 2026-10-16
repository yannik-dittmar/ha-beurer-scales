# Beurer Scales for Home Assistant

A Home Assistant custom integration (HACS) for Beurer Bluetooth body-composition scales. It is designed to keep each person's readings separate.

> **Status: early development.** So far the integration can only discover and add a BF450 scale. It doesn't sync readings yet. Progress is tracked in the [issues](https://github.com/yannik-dittmar/ha-beurer-scales/issues).

## Requirements

- Home Assistant 2026.10 or newer
- A Bluetooth path that can bond with the scale, ideally an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy/) on an ESP32

## Documentation

- Design: [`docs/design.md`](docs/design.md)
- BLE protocol (reverse engineered): [`docs/beurer-ble-protocol.md`](docs/beurer-ble-protocol.md)

## License

[MIT](LICENSE)

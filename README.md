# Beurer Scales for Home Assistant

A Home Assistant custom integration for Beurer Bluetooth body-composition scales. Several people can share one scale: each **scale user** gets their own Home Assistant device with weight and body-composition sensors, and one person's readings never end up on another person's sensors.

After someone steps on the scale, Home Assistant connects during the scale's **wake window** (about 47 s), collects the new **readings** and sends the scale back to sleep. The values show up on that scale user's sensors seconds after they step off. You don't need the Beurer app.

> **Status: first release.** The protocol was worked out on a BF450. Some behaviour, such as pairing through an ESPHome proxy, still has to be confirmed on real hardware. Please report what works and what doesn't in the [issues](https://github.com/yannik-dittmar/ha-beurer-scales/issues).

## Supported scales

The integration supports the BF600 family of Beurer scales.

| Status | Models |
|---|---|
| Verified | BF450, BF451 |
| Untested | BF105, BF1000, BF405, BF500, BF600, BF720, BF722, BF850, BF880, BF915, BF950, BF980, BF990, GS435, SRBF1 |

Home Assistant discovers untested models too, and setup warns you that the model hasn't been tested. Each model only gets the sensors it actually measures (the GS435, for example, only measures weight). Known gaps: the BF990's extra body-composition frames aren't decoded yet, and the firmware workaround for the "108.8 kg" weight error of the BF720, BF722 and BF950 isn't implemented. The BF700/BF710 family isn't supported.

## Requirements

- **Home Assistant 2026.10 or newer.** The integration relies on a Bluetooth callback that fires for every advertisement, which arrived in 2026.10.
- **A Bluetooth path that can pair (bond) with the scale.** The scale drops any connection that isn't bonded.
  - Recommended: an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy/) on an ESP32 near the scale, with `active: true`. ESPHome proxies pair with the scale automatically.
  - Home Assistant Container or Core with a host Bluetooth adapter that is pairable also works.
  - **Not supported: Home Assistant OS with only a built-in or USB Bluetooth adapter.** Home Assistant registers no pairing agent there, so the adapter can't bond, and this integration doesn't register its own. Add an ESPHome proxy instead.
  - Shelly devices can't be used as a proxy for this scale: they can't connect to it.
- **Home Assistant must be the only client for its scale users.** Don't use a scale user that Home Assistant manages with the Beurer app (or any other app) as well. Whichever client syncs first takes the readings, so your history would be split. Duplicate scale users for the same person also make the scale unsure who is standing on it. Other people can keep using the Beurer app with their own scale users on the same scale; Home Assistant ignores those slots.

## Installation

1. In HACS, add `https://github.com/yannik-dittmar/ha-beurer-scales` as a custom repository of type **Integration**.
2. Install **Beurer Scales** and restart Home Assistant.

## Setting up the scale

Step on the scale once. Home Assistant discovers it and offers to set it up; confirm it. You can also add it under **Settings → Devices & services → Add integration → Beurer Scales**, which lists the Beurer scales Home Assistant has seen nearby.

Setting up the scale doesn't connect to it, so the scale doesn't need to be awake. The scale gets its own device with a **Battery** sensor and a **Last sync** sensor (diagnostic). Then add scale users.

## Scale users

A scale stores up to 8 scale users (10 on the BF105 and BF1000), each in a numbered **slot**. A scale user only gives its readings to a client that knows its 4-digit **consent code**. In Home Assistant, each scale user is a config subentry of the scale with its own device. Add one from the scale's entry: **Add scale user**. A menu offers **Create new scale user** and **Adopt existing slot**.

Both ask for a **person** and a **name**. Both are optional, but you need at least one: an empty name takes the person's name. You can type a name for children or guests who have no Home Assistant person. Names must be unique on a scale.

### Create a new scale user

1. Enter the person or name, then the profile: birth date (age 10 to 100), height (100 to 220 cm), gender and activity level. The scale uses the profile to compute body composition.
2. Step on the scale to wake it when asked. The flow waits up to 3 minutes. Home Assistant then connects and creates the scale user on the scale. It picks the consent code itself and stores it; you never need to know it.
3. **Teaching.** While Home Assistant stays connected, the new scale user steps off the scale and on again, standing still until the result shows ("reading 1 of 3"). Each reading teaches the scale that person's weight so it recognises them later. After each reading you can take another one or finish; after the third reading the flow finishes on its own.

If the scale is full, the flow stops and asks you to remove a scale user first. If you cancel the flow, or nobody steps on the scale for the first teaching reading, Home Assistant deletes the new scale user from the scale again so it doesn't confuse recognition. If teaching times out after at least one reading, the scale user is kept with the readings taken so far.

### Adopt an existing slot

Use this only for a slot whose consent code you know, for example a scale user Home Assistant created before you reinstalled it. Enter the slot number and the consent code, then step on the scale. Home Assistant checks the code and reads the profile from the scale. A scale user the Beurer app created can't be adopted: the app never shows its consent codes.

### Change a scale user

Use **Reconfigure** on the scale user. Changing only the name or the person takes effect at once. Changing the birth date, height, gender or activity level asks you to wake the scale, so Home Assistant can write the new profile to it.

### Remove a scale user

Delete the scale user's subentry. Its device and sensors disappear from Home Assistant right away. The scale user itself is deleted from the scale at the next wake, after the weigh-in is final, which frees the slot. If the scale refuses that deletion in the connection that synced someone's weigh-in, Home Assistant tries again at a wake where nobody it manages has new readings.

## Everyday weighing

Just step on the scale. If it recognises you, your reading appears on your sensors within seconds:

- Weight and **Last reading** (when you last weighed)
- Body composition, depending on the model: BMI, body fat, body water, muscle, bone mass, basal and active metabolic rate, visceral fat, metabolic age, lean body mass, protein mass, total muscle mass, soft lean mass, subcutaneous fat. Some models add heart rate or per-segment fat and muscle.
- **Ideal weight** and **Impedance** exist but are disabled by default.

The sensors keep their last value across restarts and have long-term statistics. If several scale users weighed in while Home Assistant wasn't connected, it collects their readings in the same connection where it can, or at later wakes.

## Notifications

Home Assistant only records readings it can attribute with certainty. It discards the others and tells you with a persistent notification, one per scale user and sync, giving the count, the time range and the reason:

- **Backlog readings**: taken more than 5 minutes before the sync, for example while Home Assistant was down or out of range. Their time can't be shown correctly in Home Assistant's history, so they are dropped.
- **Guessed readings**: the scale wasn't sure who was standing on it (the display showed `U--`). They might belong to someone else, so they are dropped.

A reading that arrives twice is recorded only once.

**Unrecognised weigh-ins**: when someone steps on the scale and it recognises them as none of its scale users, the scale stores nothing. About 25 seconds into such a wake, Home Assistant connects briefly to check the scale's user list and, by default, notifies you so you can add that person. Turn this off in the scale's options: **Notify about unrecognised weigh-ins**.

## Repairs

Problems that need your action appear under **Settings → System → Repairs**. The scale user issues stop syncing that scale user until they are fixed, and each one offers a guided fix:

| Repair | When | Fixes offered |
|---|---|---|
| The scale rejects the consent code | The slot still has a scale user, but not with the stored code, for example because another client created a new one there. | Enter the consent code again; create the scale user again; remove the scale user from Home Assistant |
| Scale user deleted on the scale | The slot is empty, for example after deleting it in the Beurer app. | Create the scale user again; remove the scale user from Home Assistant |
| Scale factory-reset | The scale lists no scale users at all. Home Assistant renews the Bluetooth bond once by itself. | Create the scale user again; remove the scale user from Home Assistant |
| Can't pair through this Bluetooth path | The adapter or proxy that reached the scale couldn't bond with it. | Add an ESPHome proxy or make the host adapter pairable. Clears after the next successful sync. |
| Scale reports weights in lb or st | The scale's display unit isn't kg. Those readings aren't recorded. | Set the scale back to kg, for example in the Beurer app. Home Assistant never changes scale settings. Clears with the next reading in kg. |

**Create the scale user again** creates it on the scale with the stored profile and a new consent code, followed by teaching. The scale may choose a different slot; the sensors and their history stay the same.

## Documentation

- Design: [`docs/design.md`](docs/design.md)
- Glossary: [`CONTEXT.md`](CONTEXT.md)
- Decisions: [`docs/adr/`](docs/adr/)
- BLE protocol (reverse engineered): [`docs/beurer-ble-protocol.md`](docs/beurer-ble-protocol.md)

## License

[MIT](LICENSE). Not affiliated with or endorsed by Beurer. The brand icon is a generic scale drawing.

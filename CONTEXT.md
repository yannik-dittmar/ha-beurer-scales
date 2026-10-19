# Beurer Scales for Home Assistant

A Home Assistant integration that collects body-weight readings from Beurer Bluetooth scales and keeps each person's readings separate.

## Language

### The scale

**Scale**:
One physical Beurer bathroom scale, identified by its Bluetooth address.
_Avoid_: device (overloaded with HA devices)

**Scale model**:
The product type of a scale, such as the BF450. It decides how the scale is recognised, its protocol details and which metrics it measures.
_Avoid_: type, variant

**Slot**:
A numbered position (1–8 on the BF450) on the scale that can hold one scale user.
_Avoid_: user index, user ID

**Scale user**:
The record stored in a slot: its consent code and profile (birth date, height, gender, activity level, initials). In Home Assistant, each scale user managed by the integration is its own config subentry. A scale user's name exists only in Home Assistant; the scale stores none.
_Avoid_: profile, account, member

**Consent code**:
The 4-digit code a client chooses when it creates a scale user. Without it nobody can read or change that scale user.
_Avoid_: PIN, password

**Person**:
A Home Assistant `person` entity. A scale user may optionally be linked to one; a person is not a scale user.

### Weighing

**Weigh-in**:
The physical act of someone stepping on the scale.

**Reading**:
The scale's record of one weigh-in: its own timestamp and the metrics (weight, body composition) assigned to one scale user.
_Avoid_: measurement (collides with HA's `state_class: measurement`)

**Backlog reading**:
A reading that was not synced right after its weigh-in, because Home Assistant was down or out of range at the time.
_Avoid_: offline reading, stale reading, historic reading

**Guessed reading**:
A reading the scale assigned to a scale user with uncertainty (the display shows `U--`).

**Unrecognised weigh-in**:
A weigh-in that matches no scale user. The scale stores nothing for it and never delivers it.

**Teaching**:
Taking readings while a scale user is connected and consented, so the scale learns that scale user's weight and recognises them afterwards.

### Synchronisation

**Wake window**:
The ~47 s after a weigh-in during which the scale advertises and accepts connections.

**Pending slot**:
A slot that the scale's scan response lists as having unsynced readings (the slot list). Slots in a wake's first slot list were pending before the wake; a slot appended later belongs to that wake's weigh-in, which is final at that moment.

**Sync**:
One connection during a wake window that collects the pending readings of one or more slots.

**Stale bond**:
A bond the host's Bluetooth path (an ESPHome proxy or adapter) still holds for a scale that has forgotten it, which happens after a factory reset. Bonding fails until the path drops it. The integration removes it from an ESPHome proxy without a connection and connects again.

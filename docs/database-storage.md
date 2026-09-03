# What the Plugin Stores in the Database

This document describes every piece of data the Bambu Lab plugin reads from or
writes to the FilaMan database, and how each is used. It complements
[consumption-calculation.md](consumption-calculation.md), which focuses on the
consumption arithmetic.

## Overview

The plugin operates in **readonly-safe mode**: it never sends write commands to
the printer. All of its persistence goes through the FilaMan database and the
FilaMan `SpoolService`. The plugin:

- **Creates and maintains `Location` rows** for printer slots.
- **Moves spools between locations** when they are assigned to or removed from
  a slot.
- **Clears `PrinterSlotAssignment` rows** owned by the FilaMan core when a
  spool is removed.
- **Writes spool consumption events** at the end of a print.

Everything else the plugin keeps in memory only and never persists.

## 1. Locations (`Location` model)

For every printer slot involved in an assignment, the plugin creates or reuses a
location row via `_resolve_slot_location()`.

| Field | Value | Purpose |
|---|---|---|
| `identifier` | `bambulab_{printer_id}_{ams_id}_{tray_id}` (e.g. `bambulab_7_0_2`) | Stable, unique identity for the slot location. Matching is identifier-first. |
| `name` | Display name, e.g. `My Printer - AMS A2`, `… - HT-A`, `… - Ext` | Human-readable slot label; normalized by `_reconcile_driver_slot_locations()`. |
| `custom_fields` | `{"managed_by": "bambulab_plugin", "printer_id": <id>}` | Marks the row as plugin-managed and records which printer owns it. |

How it is used:

- Assignment resolves the slot location (creating it if missing) and moves the
  assigned spool onto it.
- At print end, consumption finalize resolves the same location to find the
  spool currently in the slot and write its consumption event.
- On startup / reconciliation, existing plugin-managed location names are
  normalized in place.

## 2. Spools (`Spool.location_id`)

The plugin moves spools between locations using `SpoolService.move_location()`.
This updates the spool's location and writes a location-change event.

| Action | Effect |
|---|---|
| Assign a spool to a slot | `move_location(spool, slot_location.id, source="driver", note="Assigned to <location>")` |
| Replace an existing spool in the target slot | Conflicting spools are moved off (`location=None`) first, with note `"Unassigned from <location> (replaced by spool <id>)"`. |
| Auto-unassign on removal | `move_location(spool, None, source="driver", note="Removed from empty slot <location>")` |

## 3. PrinterSlot / PrinterSlotAssignment (cleared, not created)

`PrinterSlot` and `PrinterSlotAssignment` rows are owned by the FilaMan core;
the plugin never creates them. When `Auto Unassign On Removal` is enabled and a
spool is physically removed from a slot, `_clear_slot_assignment()`:

1. Resolves the core slot number (`slot_no` = `ams_id * 4 + tray_id`, or
   `1000 + tray_id` for the external tray).
2. Finds the `PrinterSlot` row for this printer and slot.
3. Clears its assignment (`assignment.spool_id = None`, `assignment.present = False`).

This keeps FilaMan's slot assignment in sync with the physical printer without
sending any command to the printer.

## 4. Consumption events (`SpoolService.record_consumption`)

At the end of a print (finished, failed, or cancelled), the plugin writes one
consumption event per slot that used filament. See
[consumption-calculation.md](consumption-calculation.md) for the calculation.

| Field | Value |
|---|---|
| `delta_weight_g` | Computed consumption in grams (minimum `1.0 g`), or `0.0` on the failure path. |
| `source` | `bambulab_measured_3mf`, or `bambulab_3mf_failed` for 0 g failure events. |
| `note` | `Bambu print consumption [<printer>] (<slot>, source=…)` — failed prints add `, failed at layer N`. |

## 5. Location-move events (`SpoolService.move_location`)

Every spool move described in section 2 also writes a location-change event with
`source="driver"` and a descriptive note, so spool history reflects slot
assignments and removals.

## What is NOT stored in the database

- Live slot state (`_current_slots`, AMS units, RFID/remain values) — kept in
  memory and re-read from MQTT on every `push_status` message.
- Virtual assignment overlays and slot→spool hints (`_slot_spool_ids`,
  `_virtual_slot_overrides`) — short-lived in-memory state with a TTL.
- Prefetch/finalize dedupe markers and downloaded 3MF temp files — stored in the
  OS temp directory, never in the database.
- Printer credentials (`host`, `serial`, `access_code`) — handled by the FilaMan
  core plugin configuration, not written by this plugin to the database.

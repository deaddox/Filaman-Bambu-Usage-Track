# MQTT Slot Naming and Driver Mapping

## Scope
This document compares Bambu MQTT slot fields with the plugin driver implementation and defines the canonical display naming used for auto-managed printer slot locations.

References:
- OpenBambu MQTT docs: https://github.com/Doridian/OpenBambuAPI/raw/refs/heads/main/mqtt.md
- Bambuddy helper conventions: https://raw.githubusercontent.com/maziggy/bambuddy/8dd4efa55540cab31bde7e96c5ef923d594657ef/frontend/src/utils/amsHelpers.ts

## MQTT Fields Used by Driver

### Regular AMS units
- Source: `print.ams.ams[]`
- Unit id: `ams[].id`
- Tray id: `ams[].tray[].id`
- Filament fields (examples): `tray_type`, `tray_info_idx`, `tray_color`, `remain`

### AMS-HT units
- Source: `print.ams.ams[]`
- Unit id range: `128..199` (single-tray style units)
- Tray id typically `0`

### External tray
- Primary source: `print.vt_tray`
- Fallback source: `print.vir_slot[]` (prefers id `254` with material)
- H2D fallback: `print.device.ext_tool` when `mount_3d == 1` and standard sources absent
- Common external ids seen in payloads: `254`, `255`

### H2D Toolhead Slots (v2.5.0+)
- Source: `print.device.nozzle.info[]` and `print.device.extruder.info[]`
- Each extruder (id 0, 1) with non-empty `filam_bak` becomes a toolhead slot
- Slot index format: `tool-{extruder_id}` (e.g., `tool-0`, `tool-1`)
- Display name: `Toolhead T{id}`

## Slot Kind Classification (v2.5.0+)

Every emitted slot dict now carries a ``slot_kind`` field that maps to the
FilaMan v1.2.19+ grouped slot UI:

| ``slot_kind`` | Source | Example |
|---------------|--------|---------|
| ``"tray"`` | AMS/AMS-HT trays | AMS A1, AMS HT-A |
| ``"external"`` | External spool (vt_tray/vir_slot/ext_tool) | External Tray |
| ``"toolhead"`` | H2D dual-extruder nozzles with direct filament | Toolhead T0 |

Slots without ``slot_kind`` (legacy) default to ``"tray"`` behavior.

## Slot Payload Compatibility (v2.5.1+)

The plugin emits `slots_update` payloads compatible with current FilaMan core behavior:

- Slot records always include location/material metadata (`slot_index`, `slot_name`, `present`, `tray_*`).
- `spool_id` is included per slot only when the plugin has authoritative ownership knowledge
	(for example after readonly assignment actions managed by this plugin).
- Empty slots do not emit `spool_id`.

This prevents accidental cross-slot assignment while still enabling newer per-slot spool handling.

## Startup Sync Hook

Driver startup supports a lightweight `refresh_status()` snapshot:

- Returns `{ "active_spool_id": <id or null> }`.
- A spool id is returned only when exactly one active spool can be resolved from known slot ownership.
- Ambiguous or unknown startup states intentionally return `null` to avoid wrong assignment fallback.

## Driver Identity Model

The driver uses a stable identifier for printer slot locations:

`bambulab_{printer_id}_{ams_id}_{tray_id}`

Example:
- `bambulab_7_0_2` means printer 7, AMS 0, tray 2

Important:
- Matching is identifier-first.
- Name is a display field and can be normalized.
- Non-driver locations (no `bambulab_...` identifier) are never auto-renamed.

## Canonical Display Naming (Aligned with Bambuddy style)

### Regular AMS
Format:
- `{Printer Name} - AMS {Letter}{UnitIndex}`

Examples:
- `H2C - AMS C1` for `ams_id=0, tray_id=2`
- `H2C - AMS A2` for `ams_id=1, tray_id=0`

### AMS-HT
Format:
- `{Printer Name} - AMS HT-{Letter}`

Letter mapping:
- `ams_id=128 -> A`
- `ams_id=129 -> B`
- etc.

Examples:
- `H2C - AMS HT-A`
- `H2C - AMS HT-B`

### External
Format:
- `{Printer Name} - Ext`

Example:
- `H2C - Ext`

## Comparison with Prior Driver Behavior

Prior behavior used:
- AMS-HT: `AMS HT 1`, `AMS HT 2`
- External: `ext. Slot N`

Current behavior uses Bambuddy-style labels:
- AMS-HT: `AMS HT-A`, `AMS HT-B`
- External: `Ext`

## Auto-Rename Strategy

To fix existing mismatches while keeping changes safe and small:

1. On startup, query only driver-managed rows for current printer:
- `identifier LIKE 'bambulab_{printer_id}_%'`

2. Parse `ams_id` and `tray_id` from identifier.

3. Recompute canonical display name.

4. If the stored name differs, rename in place.

5. Skip invalid identifiers and do not touch non-driver locations.

This guarantees deterministic renaming tied to RFID-style identifier identity, not fuzzy name matching.

## Tests

The driver test suite includes coverage for:
- AMS/AMS-HT/external classification
- naming snapshots
- external tray extraction from `vt_tray` and `vir_slot`
- slot_kind tagging on all slot types
- H2D toolhead slot parsing from `device.nozzle.info[]`
- 3MF fallback behavior for non-standard units

See:
- `tests/test_driver_location_identity.py`

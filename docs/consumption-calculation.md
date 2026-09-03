# How Filament Consumption Is Calculated

This document explains how the Bambu Lab driver measures and records filament
consumption for a print. It covers the data sources, the per-layer gcode
analysis, and the exact write path into FilaMan spool history.

For the full list of everything the plugin reads and writes in the FilaMan
database, see [database-storage.md](database-storage.md).

## Overview

Consumption tracking is triggered by the printer's MQTT `print.push_status`
messages. The driver observes `gcode_state` transitions and, at the end of a
print, writes one consumption event per spool slot that was involved.

There are three candidate data sources, in order of authority:

1. **3MF slice metadata** — the slicer's expected per-slot filament weight.
2. **Per-layer gcode analysis** — extrusion length derived from the gcode,
   used to scale down a partial (failed/cancelled) print.
3. **AMS remain/RFID sensor** — the tray fill percentage before and after,
   tracked for diagnostics and slot-usage detection.

The recorded value always comes from source 1 (completed prints) or source 2
(failed/partial prints). Source 3 is tracked but does not currently produce
the recorded gram value.

---

## 1. When tracking starts

`_process_print_tracking()` runs on every `print` MQTT payload.

- `gcode_state` values `RUNNING` and `PAUSE` are treated as *active*.
- The first time the state becomes active, `_start_consumption_tracking()`
  snapshots:
  - each slot's `remain` percentage (start value),
  - the AMS `mapping` array (filament → physical slot),
  - the print context (`subtask_name`, `gcode_file`, `job_id`, etc.).
- A background prefetch task downloads the job's 3MF file from the printer via
  FTPS after a configurable delay, so it is already local when the print ends.

## 2. What is tracked during the print

While active, each message updates:

- **Progress** — `mc_percent` (fallback `print_progress`). The cumulative
  *increase* in progress is kept as `progress_delta`.
- **Used slots** — slots referenced by the MQTT `mapping`.
- **Consumed slots** — slots whose `remain` percentage dropped.
- **Current layer (new)** — `print["3D"]["layer_num"]` and
  `print["3D"]["total_layer_num"]`. The driver keeps the maximum layer seen,
  so the finalize step is robust against message ordering.

Example payload fragment:

```json
{
  "print": {
    "3D": {
      "layer_num": 618,
      "total_layer_num": 813
    },
    "mc_percent": 76,
    "gcode_state": "RUNNING"
  }
}
```

## 3. What happens when the print ends

When `gcode_state` leaves the active states (e.g. `FINISH`, `FAILED`,
`CANCEL`), `_finalize_consumption_tracking()` runs exactly once. A temp-file
dedupe marker prevents the same print from being recorded twice (e.g. across
driver restarts).

For each tracked slot it:

1. Resolves the FilaMan location (`bambulab_{printer_id}_{ams_id}_{tray_id}`)
   and the spool currently assigned there.
2. Obtains the 3MF file (prefetched, or downloaded on demand).
3. Extracts per-slot usage, and for partial prints per-layer usage.
4. Maps the gcode/3MF filament id back to the physical slot.
5. Records the consumption event.

### 3.1 Completed prints

`_extract_per_slot_filament_usage_from_file()` reads
`Metadata/slice_info.config` from the 3MF and returns, per filament:

| Field      | Meaning                                      |
|------------|----------------------------------------------|
| `slot_id`  | 1-based filament slot id                     |
| `used_g`   | slicer-computed filament weight in grams     |
| `density`  | filament density in g/cm³                    |
| `diameter` | filament diameter in mm (usually 1.75)       |

For a completed print the full `used_g` is recorded per slot. This is the most
accurate value available because the slicer accounts for all extrusion,
priming, and wipe moves.

### 3.2 Partial (failed / cancelled) prints

For `end_state != "completed"`, a linear scale of `used_g` would over- or
under-count, so the driver instead measures how far the print actually got:

1. `_extract_layer_gcode_usage_from_file()` parses the gcode embedded in the
   3MF (`3D/*.gcode`) and builds, per filament, a list of
   `(layer, cumulative_mm)` samples:
   - `;LAYER:X` comments mark layer changes.
   - `M82` / `M83` switch between absolute and relative E-axis modes.
   - `G92 E…` resets the E baseline (handled in both modes).
   - `T<n>` tool changes route extrusion to the correct filament.
   - Retraction moves (negative E in relative mode) are ignored.
2. The current layer is resolved with `_resolve_gcode_layer()` (see below).
3. The cumulative extrusion length at that layer is converted to grams with
   `_mm_to_grams()`.

### 3.3 Layer resolution

`_resolve_gcode_layer(layer_num, total_layer_num, max_gcode_layer, progress_delta)`
maps the printer-reported layer to a 0-based gcode layer.

gcode `;LAYER:` markers are 0-based, while the printer's `layer_num` is
1-based, so the offset is derived from `total_layer_num` relative to the
highest gcode marker (`max_gcode_layer`):

| Condition                                | Mapping                                  |
|------------------------------------------|------------------------------------------|
| `total_layer_num == max_gcode_layer + 1` | `gcode_layer = layer_num - 1` (offset 1) |
| `total_layer_num == max_gcode_layer`     | `gcode_layer = layer_num` (offset 0)     |
| neither (unknown numbering)              | `int(layer_num / total_layer_num * max_gcode_layer)` |
| `layer_num` missing                      | `int(progress_delta / 100 * max_gcode_layer)` (legacy estimate) |

The result is clamped to `[0, max_gcode_layer]`.

### 3.4 Length → weight conversion

`_mm_to_grams(mm, diameter_mm, density_g_cm3)` models the filament as a
cylinder:

```
radius_cm = (diameter_mm / 2) / 10
length_cm = mm / 10
volume_cm3 = π · radius_cm² · length_cm
grams      = volume_cm3 · density_g_cm3
```

### 3.5 Slot ↔ filament id mapping

The 3MF indexes filaments with `slot_id` (1-based), while the driver tracks
physical slots as `{ams_id}-{tray_id}`. `_apply_3mf_grams_to_entries()`
resolves this:

- **Preferred**: the MQTT `mapping` array (source of truth), decoded by
  `_find_filament_id_from_mapping()`.
- **Fallback** (only when no `mapping` array is available):
  `_map_slot_index_to_3mf_id(ams_id, tray_id) = ams_id * 4 + tray_id + 1`.

For single-filament prints a single-filament fallback applies the one
available `used_g` to the single used slot.

## 4. Recording the event

`_resolve_entry_grams()` decides what to write:

- Uses the `grams_from_3mf` value produced above.
- Skips entries with no usable grams.
- Rounds anything below `1.0 g` up to `1.0 g`.

The event is written via `SpoolService.record_consumption()` with:

- **source** `bambulab_measured_3mf`
- **note** `Bambu print consumption [<printer>] (<slot>, source=…)`
- **note (failed prints)** — when the print does not finish normally, the note
  is suffixed with `, failed at layer N` using the printer-reported
  `layer_num`, e.g. `…, failed at layer 618`.

### Failure path

If the 3MF could not be obtained or parsed (and the print progressed beyond
0%), a `0.0 g` event is written with source `bambulab_3mf_failed` and a note
containing the reason, so the gap is visible in spool history instead of
silently missing. Failed jobs at 0% progress do not create events.

## 5. Configuration knobs

| Setting                        | Default | Purpose                                   |
|--------------------------------|---------|-------------------------------------------|
| `enable_consumption_tracking`  | `true`  | Master switch for the whole feature.      |
| `local_3mf_timeout_seconds`    | `8`     | FTPS timeout for fetching the 3MF.        |
| `local_3mf_ftps_verify_tls`    | `false` | Validate the printer's self-signed cert.  |

## 6. Accuracy notes

- **Completed prints** are exact (slicer `used_g`).
- **Partial prints** are exact up to the layer reached, plus the printer's
  real `layer_num` rather than a progress percentage. Remaining inaccuracy
  comes only from the 1-vs-0-based layer offset, which the auto-offset logic
  resolves from `total_layer_num`.
- The AMS `remain` sensor measurement is computed but not used as the recorded
  value; the 3MF/gcode-derived weight is authoritative.

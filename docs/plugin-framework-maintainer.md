# BambuLab Plugin Framework Maintainer Guide

This guide describes how this plugin integrates with the current `filaman-system` plugin framework and what must be preserved when updating plugin code.

## Scope

- This repository owns plugin code and plugin documentation only.
- Core app code in `filaman-system` is out of scope.

## Framework Contract

### 1) Manifest (`bambulab/plugin.json`)

Required for this driver plugin:

- `plugin_key`: `bambu_consu`
- `plugin_type`: `driver`
- `driver_key`: `bambu_consu`
- `version`: semver (for example `3.0.0`)
- `config_schema`: valid JSON schema for printer config

Expected optional sections used by this plugin:

- `page_url` (plugin navigation route)
- `show_in_nav` (toggle nav visibility)
- `capabilities` (for example `slot_kinds`, `catalog_images`)
- `printer_params`
- `printer_params.migration.legacy_renames`

### 2) Driver Class (`bambulab/driver.py`)

Required shape:

- `class Driver(..., BaseDriver)` (mixin composition is allowed)
- class attribute `driver_key = "bambu_consu"`
- `async start()`
- `async stop()`

Optional hooks now used:

- `refresh_status()` returns `{"active_spool_id": int | None}` for startup sync.
- Read-only slicer profile picker actions (called by the core `driver/*` REST
  endpoints). These must exist on the driver or the core returns
  `{"code": "unsupported"}`:
  - `list_connected_models()` → `{"models": [{model, printer_ids, representative_printer_id}], "count": N}`
  - `get_profile_coverage(spool_id, filament_id)` → per-model coverage payload
  - `list_cloud_presets(force, model, group)` → `{"presets": [...], "count": N}`

### 3) Event Contract

Primary emitted events:

- `slots_update`
- `slot_tracking_update`

`slots_update` payload expectations:

- Always includes `slots` and `ams_info`.
- Slot metadata includes `slot_index`, `slot_name`, `present`, and tray fields.
- `spool_id` is emitted per-slot only when spool identity is authoritative in the plugin.
- Catalog enrichment metadata is optional per slot: `catalog_provider`, `catalog_material_code`, `catalog_material_name`, `catalog_image_url`.

## Readonly Assignment Model

The plugin is intentionally readonly-safe for printer writes:

- Assignment actions do not send printer-side write commands.
- Assignment updates are reflected by:
  - DB spool location updates
  - short-lived virtual slot overlays
  - optional per-slot `spool_id` emission where identity is known

This behavior must remain stable unless core app contracts explicitly change.

## Startup Sync Behavior

`filaman-system` may call `refresh_status()` right after driver startup.

Current plugin behavior:

- Returns one `active_spool_id` only when a single active spool can be determined from authoritative slot ownership.
- Returns `None` when there is no known spool or when state is ambiguous.

## Packaging and Installer Validation Checklist

Before releasing a plugin ZIP:

1. Required files exist:
- `plugin.json`
- `__init__.py`
- `driver.py`

2. ZIP root layout is correct:
- Archive root must contain plugin files directly.
- Do not wrap files in a top-level plugin folder.
- Valid: `plugin.json`, `__init__.py`, `driver.py` at ZIP root.
- Invalid: `bambulab/plugin.json`, `bambulab/__init__.py`, `bambulab/driver.py`.

3. Manifest consistency:
- `plugin_type == "driver"`
- `driver_key == plugin_key == "bambu_consu"`
- version is valid semver

4. No cache artifacts:
- remove `__pycache__/`
- remove `*.pyc`

5. Allowed extensions only:
- `.py`, `.json`, `.md`, `.txt`, `.cfg`, `.ini`, `.yaml`, `.yml`, `.toml`, `.html`

6. Run test suite before packaging.

## Test Coverage Requirements

At minimum, keep coverage for:

- slot identity and naming behavior
- readonly assignment flow
- startup `refresh_status()` behavior
- per-slot `spool_id` emission/clearing semantics

## Change Classification Guidance

Use this when reviewing future compatibility updates:

- Breaking-required:
  - install/validation contract failures
  - driver lifecycle contract mismatches
  - event payload changes that break core sync behavior
- Improvements:
  - richer payload fields with safe fallback
  - diagnostics and logging enhancements
  - better tests and docs

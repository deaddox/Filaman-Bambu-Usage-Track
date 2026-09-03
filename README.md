# Bambu Lab Plugin for FilaMan

A FilaMan printer driver plugin that connects to Bambu Lab printers via MQTT, reads AMS slot data in real-time and enables automatic filament-to-tray assignment.

## Features

- MQTT communication via `bambulabs_api`
- AMS and AMS Lite support (auto-detection by printer model)
- External tray support
- RFID spool identification
- Automatic spool-to-tray matching with configurable timeout
- Readonly-safe assignment flow (no printer-side write commands)
- Process-local single-instance guard (prevents duplicate active drivers for the same printer identity)
- Readonly slot tracking events for insert/remove/material change
- DB-backed spool location assignment with virtual slot overlays for UI consistency
- Print consumption tracking with 3MF-only attribution and explicit 0g failure events
- Local 3MF print-weight prefetch with duplicate-work suppression during tracking
- 10 printer-specific parameters (material index, calibration, temperatures, flow)
- Protocol-level debug logging (viewable in admin UI)
- Auto-reconnect with configurable interval
- Spoolman legacy data migration (automatic)
- Slot kind classification (tray/external/toolhead) for FilaMan v1.2.19+ grouped UI
- H2D dual-extruder toolhead slot detection (v2.5.0+)

## Behavior Change

- Assignment actions now use a readonly-safe flow by default.
- The driver no longer sends printer-side write commands for spool assignment.
- API action names and health keys remain backward compatible.

## Supported Printers

P1S, P1P, X1 Carbon, X1E, A1, A1 Mini, H2C, H2D, H2S, P2S

## Installation

This plugin is distributed as a release ZIP for manual installation through the FilaMan settings UI.

1. Download the latest release ZIP from GitHub.
2. Open FilaMan.
3. Go to Settings → Plugins or the plugin upload area.
4. Upload the ZIP file.
5. Restart or reload FilaMan if prompted.
6. Add a printer and select the uploaded Bambu Lab driver.

> The plugin identifier is intentionally set to `bambu_consu` so it does not collide with FilaMan's built-in Bambu driver.

## Public GitHub v3 Release Readiness

This repository is being prepared for the first public GitHub release, version 3.0.0. Before making the repository public, confirm the following:

- No secrets, keys, access tokens, or captured browser sessions remain in the repo.
- No `.har`, `.env`, `.pem`, or private key files are checked in.
- No `__pycache__/` or compiled Python artifacts are present in the release ZIP.
- The plugin ZIP is built from the plugin root only, with `plugin.json`, `__init__.py`, `driver.py`, and `bambu_filaments.json` at the archive root.
- The release has passed the automated test suite.

## Quick Setup Guide

1. Install FilaMan and ensure the plugin framework is available.
2. Download the release ZIP from the GitHub Releases page.
3. In FilaMan, open Settings → Plugins and upload the ZIP file.
4. In the FilaMan admin UI, add a new printer and choose the uploaded Bambu Lab driver.
5. Enter the printer host, serial number, and access code.
6. Select the matching printer model and save the configuration.
7. Verify the plugin connects and slot data appears in the UI.

See [docs/user-setup-guide.md](docs/user-setup-guide.md) for full setup and troubleshooting steps.

## Required FilaMan Version

- Plugin v3.0.0 targets the current `filaman-system` main branch plugin framework.
- Grouped slot UI (`slot_kind`) requires FilaMan v1.2.19+.
- The plugin remains backward compatible with v1.2.16+ (older cores ignore `slot_kind`).

## Plugin Framework Compatibility

This plugin intentionally follows the newer FilaMan plugin framework contract:

- Manifest contract in `plugin.json`
	- `plugin_type: "driver"`
	- `driver_key: "bambu_consu"` and `plugin_key: "bambu_consu"`
- Driver class contract in `driver.py`
	- `class Driver(BaseDriver)`
	- class attribute `driver_key = "bambu_consu"`
- Startup sync hook
	- `refresh_status()` returns `{"active_spool_id": ...}` snapshot data when determinable.
- Slot event contract
	- `slots_update` always emits slot metadata.
	- `spool_id` is emitted per-slot only when spool identity is authoritative in the plugin.

## Release Validation Rules

FilaMan now validates plugin ZIPs strictly during install/upgrade. Keep these rules in your release process:

- Include required files for driver plugins:
	- `plugin.json`
	- `__init__.py`
	- `driver.py`
- ZIP layout requirement:
	- Plugin files must be at ZIP root (no wrapping top-level folder).
	- Correct: `plugin.json`, `__init__.py`, `driver.py` are directly in the archive root.
	- Incorrect: `bambulab/plugin.json`, `bambulab/__init__.py`, `bambulab/driver.py`.
- Keep allowed file extensions only:
	- `.py`, `.json`, `.md`, `.txt`, `.cfg`, `.ini`, `.yaml`, `.yml`, `.toml`, `.html`
- Avoid packaging cache artifacts:
	- `__pycache__/`
	- `*.pyc`
- Keep manifest semver valid (for example `3.0.0`).

## Configuration

Create a new printer in the FilaMan admin panel and select **Bambu Lab** as driver. The following fields are required:

| Field | Description |
|-------|-------------|
| Printer Model | Your Bambu Lab model (determines AMS data handling) |
| IP/Hostname | IP address or hostname of the printer |
| Serial Number | Printer serial number |
| Access Code | Printer access code |
| Reconnect Interval | Minutes between reconnection attempts (default: 5) |
| Auto Unassign On Removal | Clear slot assignment in FilaMan when filament is removed from a slot (default: enabled) |
| Enable Consumption Tracking | Records spool consumption events when a print finishes (default: enabled) |
| Local 3MF Fetch Timeout | FTPS timeout for local 3MF metadata fetch (default: 8 seconds) |
| Verify FTPS Certificate | Enables TLS certificate verification for local FTPS fetch (default: disabled) |

> **Important:** The driver operates in **readonly-safe mode** for assignment actions.
> - No printer-side write commands are sent for spool assignment.
> - AMS/slot status reading continues normally.
> - Slot tracking events (insert/remove/material change) continue.
> - Spool location assignment in FilaMan remains available and updates slot UI via short-lived virtual overlays.
> - `spool_id` is included in slot payloads only when the plugin can prove slot ownership.

See [docs/plugin-framework-maintainer.md](docs/plugin-framework-maintainer.md) for maintainer-level framework details and release checklist. See [docs/database-storage.md](docs/database-storage.md) for exactly what data the plugin writes to the FilaMan database.

Consumption tracking notes:
- When print state transitions out of `RUNNING`/`PAUSE`, the plugin finalizes a consumption snapshot.
- Consumption is recorded from 3MF per-slot usage only (`source=bambulab_measured_3mf`).
- If 3MF cannot be downloaded or parsed into usable per-slot usage, the plugin records `0g` failure events for mapped spools (`source=bambulab_3mf_failed`).
- Unknown or unmapped slot spools are skipped with warning logs.

## Printer Parameters

The plugin registers the following per-printer parameters for filaments and spools:

| Parameter | Type | Description |
|-----------|------|-------------|
| Bambu Material Index | Dropdown | Material code from `bambu_filaments.json` |
| Tray Info Index | Text | Tray info index string |
| Setting ID | Text | Bambu setting identifier |
| Calibration Index | Text | Calibration index |
| K Value | Number | Pressure advance K value |
| Flow Ratio | Number | Flow ratio multiplier |
| Bed Temperature | Number | Bed temperature (°C) |
| Nozzle Temp Min | Number | Minimum nozzle temperature (°C) |
| Nozzle Temp Max | Number | Maximum nozzle temperature (°C) |
| Max Volumetric Speed | Number | Maximum volumetric speed (mm³/s) |

Parameters can be set at filament level (shared across spools) or overridden per individual spool.

## License

This project is licensed under the [MIT License](LICENSE).

This project is a fork of the [FilaMan Bambu Lab plugin](https://github.com/Fire-Devils/filaman-bambulab-plugin), part of the [FilaMan](https://github.com/Fire-Devils/filaman-system) project (MIT).

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for upstream attribution and third-party license details.

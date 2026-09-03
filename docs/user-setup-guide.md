# User Setup Guide

This guide explains the standard installation path for end users: download the published release ZIP and install it directly from the FilaMan settings screen.

## 1) Prerequisites

Before installing the plugin:

- FilaMan is installed and running.
- You have a Bambu Lab printer reachable on the same network.
- You know the printer IP address or hostname.
- You have the printer serial number and access code.
- The printer is not blocked by a firewall or NAT rule that prevents local access.

## 2) Install the Plugin from the Release ZIP

1. Download the latest release ZIP from the GitHub Releases page.
2. Open FilaMan.
3. Go to Settings.
4. Open the plugin or extension management area.
5. Upload or install the ZIP file you downloaded.
6. Confirm the plugin is enabled after the upload completes.

> This plugin is packaged as a ZIP file for direct installation through FilaMan. You do not need to manually copy source files into a filesystem plugin folder.

> The plugin identity uses `bambu_consu` so it is distinct from FilaMan's built-in Bambu driver.

## 3) Create a Printer Entry

In the FilaMan admin panel:

1. Open the printer configuration screen.
2. Click to add a new printer.
3. Select the uploaded `Bambu Lab` driver.
4. Fill in the required fields:
   - Printer Model
   - IP/Hostname
   - Serial Number
   - Access Code
5. Save the printer configuration.

## 4) Configuration Options

The Bambu Lab driver exposes the following options on the printer
configuration screen.

| Field | Required | Default | What it does |
|---|---|---|---|
| Printer Model | Yes | — | The exact Bambu Lab model. Controls how AMS data is interpreted (regular AMS, AMS Lite, or H2D toolheads). |
| IP/Hostname | Yes | — | LAN IP address or hostname of the printer. |
| Serial Number | Yes | — | Printer serial number. Used for stable identity and to prevent two driver instances from controlling the same printer. |
| Access Code | Yes | — | The printer's LAN access code. Used to authenticate MQTT and FTPS connections. |
| Reconnect Interval | No | 5 minutes | Maximum delay between automatic reconnection attempts after a connection drop. |
| Auto Unassign On Removal | No | enabled | When a spool is physically removed from an AMS/tray slot, automatically clears that slot's spool assignment in FilaMan. |
| Enable Consumption Tracking | No | enabled | Records spool consumption events when a print finishes or fails. |
| Local 3MF Fetch Timeout | No | 8 seconds | Timeout for the local FTPS download used to read the 3MF metadata for weight estimation. |
| Verify FTPS Certificate | No | disabled | Validates the printer's FTPS TLS certificate. Disable to auto-accept the printer's self-signed certificate. |

### Auto Unassign On Removal

When enabled, the driver watches slot changes reported over MQTT. If a spool is
removed from a slot, the plugin clears that slot's spool assignment in FilaMan
and moves any spool off the slot's location, so no spool stays linked to an
empty slot.

This is plugin-local behaviour: **no write command is sent to the printer**.
Disable it if you want spools to remain assigned to a slot even after physical
removal.

### Reconnect Interval

The driver reconnects automatically with exponential backoff. This value sets
the maximum delay between attempts (1–60 minutes). Keep the default (5 minutes)
unless the printer frequently loses its connection and you want faster retries.

### Enable Consumption Tracking

When enabled, the plugin computes and records filament consumption per spool at
the end of a print. Failed and cancelled prints are scaled to the layer the
printer actually reached. See
[consumption-calculation.md](consumption-calculation.md) for the full
calculation.

### Local 3MF Fetch Timeout

Used only for the local FTPS download of the print's 3MF metadata file. Increase
it on slow networks or for very large files; decrease it to fail faster when
the printer is unreachable.

### Verify FTPS Certificate

Bambu Lab printers use self-signed certificates, so leave this disabled to
auto-accept them. Enable it only when the printer is reached through a trusted
certificate (for example a reverse proxy with a valid certificate).

## 5) Verifying the Connection

After saving the printer config:

- The driver should establish a connection to the printer.
- AMS slots should appear in the UI.
- RFID and spool information should populate when available.
- Slot tracking should begin updating live.

If the connection fails:

- Confirm the IP is correct.
- Confirm the access code matches the printer.
- Confirm the serial is correct.
- Confirm the printer is online and reachable from the FilaMan host.
- Check FilaMan logs for MQTT or FTP connection errors.

## 6) Typical Troubleshooting

### Printer not discovered

- Ensure the printer is on the same network segment.
- Check whether the printer uses a hostname instead of a raw IP.
- Verify the printer is powered on and not in offline mode.

### Access denied

- Re-enter the printer access code carefully.
- Confirm you are using the correct Bambu printer, not an AMS-only gateway or duplicate device.

### Slots remain empty

- Confirm the printer model is set correctly.
- Check whether the printer firmware exposes AMS data correctly.
- Verify the plugin is active and the driver has loaded successfully.

### Driver endpoints fail with `primary_proxy_failed`

FilaMan only runs drivers on a single "primary" Gunicorn worker. The stock
Docker image starts **4 workers**, so requests that land on a non-primary worker
are proxied to `FILAMAN_GUNICORN_URL` (default `http://127.0.0.1:8001`). Because
that loopback URL is the shared Gunicorn listener, a proxied request can bounce
between secondary workers until the hop limit is reached, producing:

```json
{"code": "primary_proxy_failed", "message": "Could not route request to primary worker"}
```

For a single-container deployment, run **one Gunicorn worker** so every request
is handled by the primary and no proxying happens. In `docker-compose.yml`
override the container command, e.g.:

```yaml
services:
  filaman-system-app:
    command:
      - gunicorn
      - -w
      - "1"
      - -k
      - uvicorn.workers.UvicornWorker
      - --bind
      - 127.0.0.1:8001
      - --timeout
      - "120"
      - --keep-alive
      - "5"
      - --pid
      - /tmp/filaman-gunicorn.pid
      - --access-logfile
      - "-"
      - app.main:app
```

This is FilaMan core routing behaviour, not a plugin issue.

## 7) Security Notes

This plugin processes printer authentication values locally. Do not store secrets in:

- Git repositories
- unsupported `.env` files
- browser HAR captures
- screenshots or support bundles containing session data

Before publishing a release, remove all local artifacts such as:

- `__pycache__/`
- `*.pyc`
- `.har` captures
- `.env` files
- private keys or certificates

## 8) Future Releases

For new releases:

1. Run the automated test suite.
2. Rebuild the plugin ZIP from the cleaned source tree.
3. Verify the archive root layout and required file list.
4. Publish the release tag or GitHub release.
5. Include a changelog and note any compatibility limits.

## 9) Further Reading

- [consumption-calculation.md](consumption-calculation.md) — how filament consumption is computed and recorded.
- [database-storage.md](database-storage.md) — what data the plugin stores in the FilaMan database and how it uses it.

# Third-Party Notices

This plugin builds on and borrows from the following projects. This file documents the
sources, licenses, and attribution obligations so they can be preserved in every release.

## FilaMan (filaman-system)

- Project: https://github.com/Fire-Devils/filaman-system
- Role: Parent application and plugin framework contract. This plugin is a fork of the
  official FilaMan Bambu Lab plugin and imports FilaMan core classes
  (`app.core.*`, `app.models.*`, `app.plugins.base.BaseDriver`, `app.services.*`).
- License: MIT
- Copyright: © 2026 Manuel Weiser and FilaMan contributors

## FilaMan Bambu Lab Plugin (filaman-bambulab-plugin)

- Project: https://github.com/Fire-Devils/filaman-bambulab-plugin
- Role: Direct upstream of this fork.
- License: MIT (deferred to the FilaMan project license)

## bambulabs_api

- Project: https://github.com/BambuTools/bambulabs_api
- Package: `bambulabs-api` on PyPI
- Role: Runtime dependency used for MQTT communication with Bambu Lab printers.
- License: MIT
- Copyright: © 2023 Chris Ioannidis

## ImplicitFTP_TLS snippet

- Source: adapted from a Stack Overflow answer
  (https://stackoverflow.com/a/36049814) and credited to
  `@WolfwithSword / ha-bambulab` in `bambulab/driver.py`.
- License: Stack Overflow content is licensed under CC BY-SA. The `ha-bambulab`
  repository (https://github.com/greghesp/ha-bambulab) does not publish a standalone
  LICENSE file.
- Obligation: attribution is retained in the source code comments. The snippet is a
  small, isolated utility and does not extend copyleft to the rest of this project.

## Bambuddy

- Project: https://github.com/maziggy/bambuddy
- Role: Reference for MQTT slot naming conventions and AMS display helpers.
- License: Appears to be MIT or similar open-source license (not explicitly stated in repo).
- Attribution: Slot naming and display logic reference the Bambuddy frontend helper conventions
  (specifically `amsHelpers.ts`) for consistent UX with other Bambu Lab tools.

## bambu_filaments.json

- Role: mapping of Bambu Lab material codes to display names.
- Nature: factual product-code data with minimal creative content.
- Source: derived from Bambu Lab material identifiers.

## General note

SQLAlchemy and the Python standard library are used as imports only and are not
redistributed with this plugin. Their licenses (MIT and PSF respectively) impose no
additional obligation on this project.

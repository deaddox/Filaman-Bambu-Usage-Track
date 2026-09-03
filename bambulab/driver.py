import asyncio
import ftplib
import hashlib
import json
import logging
import os
import ssl
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from xml.etree import ElementTree
from zipfile import ZipFile

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import async_session_maker
from app.models.location import Location
from app.models.printer import (
    Printer as PrinterModel,
    PrinterSlot,
    PrinterSlotAssignment,
)
from app.models.spool import Spool
from app.plugins.base import BaseDriver
from app.services.spool_service import SpoolService

from .catalog import CatalogMixin
from .catalog_enrichment import CatalogEnrichmentMixin
from .slots import SlotSupportMixin
from .state import PendingSpool

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60
DEFAULT_RECONNECT_INTERVAL = 5
VIRTUAL_ASSIGNMENT_TTL_SECONDS = 20
DEFAULT_LOCAL_3MF_TIMEOUT_SECONDS = 8
DEFAULT_3MF_PREFETCH_DELAY_SECONDS = 120
DEFAULT_3MF_PREFETCH_LOCK_STALE_SECONDS = 600
DEFAULT_DISCONNECT_GRACE_SECONDS = 20


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTP_TLS subclass that wraps sockets in SSL immediately on connect to
    support implicit FTPS (port 990).  Standard ftplib.FTP_TLS only supports
    explicit FTPS (port 21 + STARTTLS).
    See https://stackoverflow.com/a/36049814
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        """Wrap the raw socket in SSL as soon as it is assigned."""
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        """Override to wrap the data-channel socket in SSL (fixes reliability
        issues seen on some printer firmware versions).
        Courtesy @WolfwithSword / ha-bambulab.
        """
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            session = None
            if isinstance(self.sock, ssl.SSLSocket):
                session = self.sock.session
            conn = self.context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=session,
            )
        return conn, size


class Driver(CatalogEnrichmentMixin, CatalogMixin, SlotSupportMixin, BaseDriver):
    driver_key = "bambu_consu"
    _instance_registry_lock = threading.Lock()
    _active_instance_claims: dict[str, int] = {}

    # NOTE: This driver runs in readonly-safe assignment mode.
    # Assignment actions update FilaMan state + virtual slot overlays, but never
    # send printer-side write commands.

    def __init__(
        self,
        printer_id: int,
        config: dict[str, Any],
        emitter: Callable[[dict[str, Any]], None],
    ):
        super().__init__(printer_id, config, emitter)
        self._printer: Any = None  # bambulabs_api.Printer
        self._pending: PendingSpool | None = None
        self._timeout_seconds = (
            DEFAULT_TIMEOUT  # Can be overridden per assign_pending_spool call
        )
        self._host = config.get("host", "")
        self._serial = config.get("serial", "")
        self._access_code = config.get("access_code", "")
        self._connected = False
        self._last_connected_at: float | None = None
        self._last_disconnected_at: float | None = None
        self._last_disconnect_rc: str | None = None
        self._last_mqtt_message_at: float | None = None
        self._disconnect_grace_seconds = self._to_float(
            config.get("disconnect_grace_seconds", DEFAULT_DISCONNECT_GRACE_SECONDS),
            DEFAULT_DISCONNECT_GRACE_SECONDS,
        )
        self._reconnect_interval = (
            self._to_float(
                config.get("reconnect_interval_minutes", DEFAULT_RECONNECT_INTERVAL),
                DEFAULT_RECONNECT_INTERVAL,
            )
            * 60
        )
        self._auto_unassign_on_remove = self._to_bool(
            config.get("auto_unassign_on_remove", True), True
        )
        self._consumption_tracking_enabled = self._to_bool(
            config.get("enable_consumption_tracking", True), True
        )
        self._resolve_shop_images = self._to_bool(
            config.get("resolve_shop_images", False),
            False,
        )
        self._local_3mf_timeout_seconds = self._to_float(
            config.get("local_3mf_timeout_seconds", DEFAULT_LOCAL_3MF_TIMEOUT_SECONDS),
            DEFAULT_LOCAL_3MF_TIMEOUT_SECONDS,
        )
        self._local_3mf_ftps_verify_tls = self._to_bool(
            config.get("local_3mf_ftps_verify_tls", False),
            False,
        )
        self._current_slots: list[dict[str, Any]] = []
        self._current_ams_units: list[dict[str, Any]] = []
        self._printer_model = config.get("printer_model", "P1S")
        self._is_ams_lite = self._printer_model in ("A1", "A1_MINI")
        self._has_toolheads = self._printer_model == "H2D"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ams_serials: dict[str, str] = {}  # ams_id -> serial number
        self._printer_name: str | None = None  # Wird in start() aus DB geladen
        self._write_mode = "readonly"
        self._write_mode_source = "driver"
        self._write_mode_reason = "forced_readonly"
        self._write_mode_checked_at: float | None = None
        self._capability_lock = threading.Lock()
        self._virtual_slot_overrides: dict[str, dict[str, Any]] = {}
        self._slot_spool_ids: dict[str, int] = {}
        self._tracking_active = False
        self._tracking_started_at: datetime | None = None
        self._tracking_last_progress = 0.0
        self._tracking_progress_delta = 0.0
        self._tracking_slots: dict[str, dict[str, Any]] = {}
        self._tracking_consumed_slots: set[str] = set()
        self._tracking_used_slots: set[str] = set()
        self._tracking_mapping_array: list[int] | None = None
        self._tracking_last_result: dict[str, Any] | None = None
        self._tracking_last_print_state: str = ""
        self._current_print_state: str = ""
        self._current_print_progress: float | None = None
        self._tracking_max_layer_num: int | None = None
        self._tracking_total_layer_num: int | None = None
        self._tracking_print_context: dict[str, Any] = {}
        self._tracking_prefetched_total_grams: float | None = None
        self._tracking_prefetched_3mf_path: str | None = None
        self._tracking_finalize_queued = False
        self._tracking_finalize_in_progress = False
        self._tracking_prefetch_task: asyncio.Task | None = None
        self._tracking_prefetch_delay_seconds = DEFAULT_3MF_PREFETCH_DELAY_SECONDS
        self._tracking_prefetch_generation = 0
        self._tracking_prefetch_in_flight = False
        self._tracking_prefetch_lock_path: str | None = None
        self._tracking_prefetch_lock_stale_seconds = (
            DEFAULT_3MF_PREFETCH_LOCK_STALE_SECONDS
        )
        self._instance_claim_keys: tuple[str, ...] = ()

    def _build_prefetch_lock_path(self) -> str:
        return os.path.join(
            tempfile.gettempdir(),
            f"filaman_bambulab_prefetch_printer_{self.printer_id}.lock",
        )

    def _build_finalize_dedupe_key(
        self,
        print_context: dict[str, Any],
        started_at: datetime | None,
    ) -> str:
        """Build a stable per-print key used to dedupe finalize writes.

        Prefer printer job identifiers when available; otherwise include
        contextual metadata plus tracking start timestamp to avoid collisions.
        """
        printer_part = f"printer={self.printer_id}"

        job_id = str(print_context.get("job_id") or "").strip()
        if job_id and job_id != "0":
            return f"{printer_part}|job_id={job_id}"

        lan_task_id = str(print_context.get("lan_task_id") or "").strip()
        if lan_task_id and lan_task_id != "0":
            return f"{printer_part}|lan_task_id={lan_task_id}"

        start_part = started_at.isoformat() if started_at else "none"
        model_id = str(print_context.get("model_id") or "")
        design_id = str(print_context.get("design_id") or "")
        gcode_file = str(print_context.get("gcode_file") or "")
        subtask_name = str(print_context.get("subtask_name") or "")
        return (
            f"{printer_part}|model_id={model_id}|design_id={design_id}|"
            f"gcode_file={gcode_file}|subtask_name={subtask_name}|start={start_part}"
        )

    @staticmethod
    def _build_finalize_dedupe_marker_path(dedupe_key: str) -> str:
        key_hash = hashlib.sha1(dedupe_key.encode("utf-8")).hexdigest()
        return os.path.join(
            tempfile.gettempdir(),
            f"filaman_bambulab_finalize_{key_hash}.lock",
        )

    @staticmethod
    def _read_finalize_dedupe_marker_payload(
        marker_path: str,
    ) -> dict[str, Any] | None:
        try:
            with open(marker_path, encoding="utf-8") as marker_file:
                payload = json.load(marker_file)
            if isinstance(payload, dict):
                return payload
        except Exception:
            return None
        return None

    def _try_acquire_finalize_dedupe_marker(self, dedupe_key: str) -> bool:
        marker_path = self._build_finalize_dedupe_marker_path(dedupe_key)
        try:
            fd = os.open(marker_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing_payload = self._read_finalize_dedupe_marker_payload(marker_path)
            logger.info(
                "Finalize dedupe marker already exists for printer %s: key=%s path=%s instance=%s pid=%s existing=%s",
                self.printer_id,
                dedupe_key,
                marker_path,
                hex(id(self)),
                os.getpid(),
                existing_payload,
            )
            return False
        except OSError as error:
            logger.warning(
                "Finalize dedupe marker create failed for printer %s: %s",
                self.printer_id,
                error,
            )
            # Fail-open so consumption recording is not lost because of a temp-dir error.
            return True

        try:
            payload = {
                "printer_id": self.printer_id,
                "pid": os.getpid(),
                "instance": hex(id(self)),
                "created_at": time.time(),
                "dedupe_key": dedupe_key,
            }
            os.write(fd, json.dumps(payload).encode("utf-8"))
        finally:
            os.close(fd)

        logger.info(
            "Finalize dedupe marker acquired for printer %s: key=%s path=%s instance=%s pid=%s",
            self.printer_id,
            dedupe_key,
            marker_path,
            hex(id(self)),
            os.getpid(),
        )

        return True

    def _acquire_cross_process_prefetch_lock(self) -> bool:
        lock_path = self._build_prefetch_lock_path()
        now = time.time()

        for attempt in range(2):
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = now - os.path.getmtime(lock_path)
                except OSError:
                    age = None

                if (
                    age is not None
                    and age > float(self._tracking_prefetch_lock_stale_seconds)
                    and attempt == 0
                ):
                    try:
                        os.remove(lock_path)
                        logger.warning(
                            "Metadata prefetch lock stale for printer %s: removed %s (age=%.1fs)",
                            self.printer_id,
                            lock_path,
                            age,
                        )
                        continue
                    except OSError:
                        pass
                return False
            except OSError as error:
                logger.warning(
                    "Metadata prefetch lock create failed for printer %s: %s",
                    self.printer_id,
                    error,
                )
                return False

            try:
                payload = {
                    "pid": os.getpid(),
                    "printer_id": self.printer_id,
                    "created_at": now,
                }
                os.write(fd, json.dumps(payload).encode("utf-8"))
            finally:
                os.close(fd)

            self._tracking_prefetch_lock_path = lock_path
            return True

        return False

    def _release_cross_process_prefetch_lock(self) -> None:
        lock_path = self._tracking_prefetch_lock_path
        if not lock_path:
            return

        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass
        except OSError as error:
            logger.warning(
                "Metadata prefetch lock cleanup failed for printer %s: %s",
                self.printer_id,
                error,
            )
        finally:
            self._tracking_prefetch_lock_path = None

    def _build_instance_claim_keys(self) -> list[str]:
        keys = [f"printer_id:{self.printer_id}"]

        serial = str(self._serial or "").strip().lower()
        if serial:
            keys.append(f"serial:{serial}")

        host = str(self._host or "").strip().lower()
        # Only claim by host when serial is unavailable. Some setups route
        # multiple printers through one gateway host, where serial is the
        # true stable identity.
        if host and not serial:
            keys.append(f"host:{host}")

        return keys

    def _claim_instance_identity(self) -> None:
        claim_keys = self._build_instance_claim_keys()
        instance_token = id(self)

        with self._instance_registry_lock:
            conflicts = []
            for key in claim_keys:
                owner = self._active_instance_claims.get(key)
                if owner is not None and owner != instance_token:
                    conflicts.append(key)

            if conflicts:
                raise RuntimeError(
                    "Another Bambu driver instance is already active for "
                    f"printer {self.printer_id} ({', '.join(conflicts)})"
                )

            for key in claim_keys:
                self._active_instance_claims[key] = instance_token

        self._instance_claim_keys = tuple(claim_keys)

    def _release_instance_identity(self) -> None:
        instance_token = id(self)
        claim_keys = self._instance_claim_keys
        if not claim_keys:
            return

        with self._instance_registry_lock:
            for key in claim_keys:
                owner = self._active_instance_claims.get(key)
                if owner == instance_token:
                    self._active_instance_claims.pop(key, None)

        self._instance_claim_keys = ()

    async def start(self) -> None:
        try:
            from bambulabs_api import Printer as BambuPrinter
        except ImportError as exc:
            logger.error(
                "Bambu driver cannot start for printer %s: the 'bambulabs_api' "
                "dependency is missing. Reinstall the plugin so FilaMan installs "
                "its declared dependencies, or run `pip install bambulabs-api` manually.",
                self.printer_id,
            )
            self._running = False
            raise RuntimeError(
                f"bambulabs_api dependency missing for printer {self.printer_id}"
            ) from exc

        try:
            self._claim_instance_identity()
            self._running = True
            self._loop = asyncio.get_running_loop()

            self._printer = BambuPrinter(
                ip_address=self._host,
                access_code=self._access_code,
                serial=self._serial,
            )

            # Callbacks laufen im paho-Thread
            self._printer.mqtt_client.on_connect_handler = self._on_connect
            self._printer.mqtt_client.on_message_handler = self._on_message
            self._printer.mqtt_client.on_disconnect_handler = self._on_disconnect

            # Reconnect-Backoff konfigurieren (paho auto-reconnect via loop_start)
            self._printer.mqtt_client._client.reconnect_delay_set(
                min_delay=1,
                max_delay=self._reconnect_interval,
            )

            # MQTT starten (non-blocking: connect_async + loop_start in paho-Thread)
            # pushall wird automatisch beim Connect gesendet (pushall_on_connect=True)
            self._printer.mqtt_start()
            logger.info(
                f"Bambu driver started for printer {self.printer_id} at {self._host}"
            )

            # Run FTP connectivity diagnostic in the background only when the app
            # is in debug mode (settings.debug=True).
            if settings.debug:
                asyncio.get_event_loop().run_in_executor(None, self._run_ftp_diagnostics)

            # Printer-Namen aus DB laden fÃ¼r Location-Generierung
            try:
                async with async_session_maker() as db:
                    printer = await db.get(PrinterModel, self.printer_id)
                    self._printer_name = (
                        printer.name if printer else f"Printer {self.printer_id}"
                    )
                await self._reconcile_driver_slot_locations()
                self._register_catalog_enrichment()
            except Exception as e:
                logger.warning(f"Failed to load printer name: {e}")
                self._printer_name = f"Printer {self.printer_id}"
        except Exception:
            self._running = False
            self._printer = None
            self._connected = False
            self._release_instance_identity()
            raise

    async def stop(self) -> None:
        self._running = False
        self._unregister_catalog_enrichment()
        self._slot_spool_ids = {}
        self._tracking_active = False
        self._tracking_started_at = None
        self._tracking_last_progress = 0.0
        self._tracking_progress_delta = 0.0
        self._tracking_slots = {}
        self._tracking_consumed_slots = set()
        self._tracking_used_slots = set()
        self._tracking_last_print_state = ""
        self._current_print_state = ""
        self._current_print_progress = None
        self._tracking_print_context = {}
        if self._tracking_prefetch_task and not self._tracking_prefetch_task.done():
            self._tracking_prefetch_task.cancel()
        self._tracking_prefetch_task = None
        self._tracking_prefetched_total_grams = None
        self._tracking_prefetch_in_flight = False
        self._release_cross_process_prefetch_lock()
        self._tracking_finalize_queued = False
        self._tracking_finalize_in_progress = False
        self._tracking_prefetch_generation += 1
        if self._tracking_prefetched_3mf_path and os.path.exists(
            self._tracking_prefetched_3mf_path
        ):
            try:
                os.remove(self._tracking_prefetched_3mf_path)
            except Exception:
                pass
        self._tracking_prefetched_3mf_path = None
        if self._pending and self._pending.timer:
            self._pending.timer.cancel()
            self._pending = None
        if self._printer:
            try:
                self._printer.mqtt_client._client.disconnect()
            except Exception:
                pass
            self._printer.mqtt_stop()
            self._printer = None
        self._connected = False
        self._last_disconnected_at = time.time()
        self._release_instance_identity()

    def _generate_slot_location_name(self, ams_id: int, tray_id: int) -> str:
        """Generiert Location-Namen fÃ¼r AMS-Slot.

        Format:
        - AMS Slots: "{Drucker Name} - AMS {A..}{tray_id+1}"
        - AMS-HT Slots: "{Drucker Name} - HT-{A..}"
        - External Slot: "{Drucker Name} - Ext"

        Beispiele:
        - "Bambu P1S - AMS A2" (ams_id=0, tray_id=1)
        - "Bambu H2C - AMS B1" (ams_id=1, tray_id=0)
        - "Bambu H2C - HT-A" (ams_id=128, tray_id=0)
        - "Bambu X1C - Ext" (ams_id=255, tray_id=254)
        """
        printer_name = self._printer_name or f"Printer {self.printer_id}"

        if self._is_external_slot_ams_id(ams_id):
            return f"{printer_name} - Ext"
        if self._is_ams_ht_slot_ams_id(ams_id):
            ht_letter = chr(65 + (ams_id - 128))
            return f"{printer_name} - HT-{ht_letter}"
        else:
            # AMS slots follow printer display: AMS A1..A4, B1..B4, ...
            unit_label = chr(65 + ams_id)  # 65 = 'A' in ASCII
            return f"{printer_name} - AMS {unit_label}{tray_id + 1}"

    @staticmethod
    def _is_external_slot_ams_id(ams_id: int) -> bool:
        """Return True when an AMS id represents a true external tray slot."""
        return ams_id >= 200

    @staticmethod
    def _is_ams_ht_slot_ams_id(ams_id: int) -> bool:
        """Return True when an AMS id represents an AMS-HT unit channel."""
        return 128 <= ams_id < 200

    def _build_slot_location_identifier(self, ams_id: int, tray_id: int) -> str:
        return f"bambulab_{self.printer_id}_{ams_id}_{tray_id}"

    @staticmethod
    def _parse_driver_slot_identifier(identifier: str | None) -> tuple[int, int, int] | None:
        if not identifier or not identifier.startswith("bambulab_"):
            return None

        parts = identifier.split("_")
        if len(parts) != 4:
            return None

        try:
            printer_id = int(parts[1])
            ams_id = int(parts[2])
            tray_id = int(parts[3])
        except (ValueError, TypeError):
            return None

        return printer_id, ams_id, tray_id

    async def _resolve_slot_location(
        self,
        db: Any,
        ams_id: int,
        tray_id: int,
        *,
        create_if_missing: bool,
    ) -> Location | None:
        slot_location_name = self._generate_slot_location_name(ams_id, tray_id)
        slot_identifier = self._build_slot_location_identifier(ams_id, tray_id)

        identifier_result = await db.execute(
            select(Location).where(Location.identifier == slot_identifier)
        )
        location = identifier_result.scalar_one_or_none()
        if location:
            if location.name != slot_location_name:
                logger.info(
                    "Normalizing driver location name for %s: '%s' -> '%s'",
                    slot_identifier,
                    location.name,
                    slot_location_name,
                )
                location.name = slot_location_name
            return location

        # Legacy fallback by name for driver-managed/legacy slot locations.
        name_result = await db.execute(
            select(Location).where(func.lower(Location.name) == slot_location_name.lower())
        )
        name_matches = name_result.scalars().all()
        if name_matches:
            prefix = f"bambulab_{self.printer_id}_"
            preferred_match = None
            for match in name_matches:
                match_identifier = (match.identifier or "").strip()
                if not match_identifier or match_identifier.startswith(prefix):
                    preferred_match = match
                    break

            if preferred_match:
                location = preferred_match
                if not location.identifier:
                    location.identifier = slot_identifier
                return location

        if not create_if_missing:
            return None

        location = Location(
            name=slot_location_name,
            identifier=slot_identifier,
            custom_fields={
                "managed_by": "bambulab_plugin",
                "printer_id": self.printer_id,
            },
        )
        db.add(location)
        await db.flush()
        logger.info("Created location: %s (%s)", slot_location_name, slot_identifier)
        return location

    async def _reconcile_driver_slot_locations(self) -> None:
        """Repair mismatched driver-managed printer slot location names in-place."""
        prefix = f"bambulab_{self.printer_id}_"
        renamed = 0
        skipped = 0

        try:
            async with async_session_maker() as db:
                result = await db.execute(
                    select(Location).where(Location.identifier.like(f"{prefix}%"))
                )
                locations = result.scalars().all()

                for location in locations:
                    parsed = self._parse_driver_slot_identifier(location.identifier)
                    if not parsed:
                        skipped += 1
                        continue
                    parsed_printer_id, ams_id, tray_id = parsed
                    if parsed_printer_id != self.printer_id:
                        skipped += 1
                        continue

                    canonical_name = self._generate_slot_location_name(ams_id, tray_id)
                    if location.name != canonical_name:
                        logger.info(
                            "Repairing slot location name for %s: '%s' -> '%s'",
                            location.identifier,
                            location.name,
                            canonical_name,
                        )
                        location.name = canonical_name
                        renamed += 1

                if renamed:
                    await db.commit()
                logger.info(
                    "Driver slot location reconciliation for printer %s complete: %s renamed, %s skipped",
                    self.printer_id,
                    renamed,
                    skipped,
                )
        except Exception as exc:
            logger.warning(
                "Failed to reconcile driver slot locations for printer %s: %s",
                self.printer_id,
                exc,
            )

    @staticmethod
    def _select_external_tray_data(print_data: dict[str, Any]) -> dict[str, Any] | None:
        """Return external tray data from push_status, preferring vt_tray then vir_slot.

        Some firmware messages omit ``vt_tray`` and only provide ``vir_slot``
        entries (typically id 254/255). In those cases we still need stable
        external-slot tracking.

        H2D: Falls back to ``device.ext_tool`` when ``vt_tray`` and ``vir_slot``
        are absent and ``ext_tool.mount_3d == 1``.
        """
        vt_tray = print_data.get("vt_tray")
        if isinstance(vt_tray, dict):
            return vt_tray

        vir_slots = print_data.get("vir_slot")
        if isinstance(vir_slots, list):
            candidates = [slot for slot in vir_slots if isinstance(slot, dict)]
            if candidates:
                # Prefer real external tray slot id=254 with material, then any tray
                # that reports material, then id=254 as a stable fallback.
                for slot in candidates:
                    if str(slot.get("id", "")) == "254" and slot.get("tray_type"):
                        return slot

                for slot in candidates:
                    if slot.get("tray_type"):
                        return slot

                for slot in candidates:
                    if str(slot.get("id", "")) == "254":
                        return slot

                return candidates[0]

        # H2D: use device.ext_tool as external spool proxy
        device = print_data.get("device")
        if isinstance(device, dict):
            ext_tool = device.get("ext_tool")
            if isinstance(ext_tool, dict) and ext_tool.get("mount_3d") == 1:
                ext_type = ext_tool.get("type", "")
                return {
                    "id": "254",
                    "tray_type": ext_type if ext_type and ext_type != "F000" else "",
                    "tray_info_idx": "",
                    "tray_color": "00000000",
                    "nozzle_temp_min": "0",
                    "nozzle_temp_max": "0",
                    "remain": 0,
                    "tag_uid": "0000000000000000",
                }

        return None

    @staticmethod
    def _parse_toolhead_slots(device_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse H2D toolhead slots from device.nozzle.info[] and
        device.extruder.info[].

        Creates synthetic slot dicts with ``slot_kind: "toolhead"`` for each
        extruder/nozzle pair that has filament loaded (non-empty ``filam_bak``).

        Only called for printers where ``_has_toolheads`` is True (H2D).
        """
        slots: list[dict[str, Any]] = []
        nozzle_info = device_data.get("nozzle", {}).get("info", [])
        extruder_info = device_data.get("extruder", {}).get("info", [])

        if not isinstance(nozzle_info, list) or not isinstance(extruder_info, list):
            return slots

        nozzle_by_id: dict[int, dict[str, Any]] = {}
        for n in nozzle_info:
            if isinstance(n, dict) and "id" in n:
                nozzle_by_id[int(n["id"])] = n

        for extruder in extruder_info:
            if not isinstance(extruder, dict):
                continue
            ext_id = int(extruder.get("id", -1))
            filam_bak = extruder.get("filam_bak", [])
            if not isinstance(filam_bak, list):
                filam_bak = []
            nozzle = nozzle_by_id.get(ext_id, {})
            diameter = nozzle.get("diameter")
            nozzle_type = nozzle.get("type", "")
            has_filament = len(filam_bak) > 0

            slot: dict[str, Any] = {
                "slot_index": f"tool-{ext_id}",
                "slot_name": f"Toolhead T{ext_id}",
                "slot_kind": "toolhead",
                "tray_info_idx": "",
                "tray_type": "",
                "tray_color": "",
                "remain": None,
                "nozzle_temp_min": None,
                "nozzle_temp_max": None,
                "setting_id": "",
                "cali_idx": None,
                "present": has_filament,
            }

            # Populate filament info from filam_bak if available
            if has_filament and isinstance(filam_bak[0], dict):
                fb = filam_bak[0]
                slot["tray_type"] = fb.get("tray_type", "")
                slot["tray_color"] = fb.get("tray_color", "")
                slot["tray_info_idx"] = fb.get("tray_info_idx", "")
                slot["nozzle_temp_min"] = fb.get("nozzle_temp_min")
                slot["nozzle_temp_max"] = fb.get("nozzle_temp_max")
                slot["remain"] = fb.get("remain")
                slot["setting_id"] = fb.get("setting_id", "")
                slot["cali_idx"] = fb.get("cali_idx")

            # Attach nozzle metadata
            slot["nozzle_diameter"] = diameter
            slot["nozzle_type"] = nozzle_type

            slots.append(slot)

        return slots

    async def _update_spool_location(
        self, filaman_spool_id: int, ams_id: int, tray_id: int
    ) -> None:
        """Setzt Spulen-Standort auf AMS-Slot-Location.

        Erstellt die Location automatisch falls sie noch nicht existiert.
        Nutzt SpoolService.move_location() fÃ¼r konsistente Event-Generierung.
        """
        try:
            slot_location_name = self._generate_slot_location_name(ams_id, tray_id)

            async with async_session_maker() as db:
                location = await self._resolve_slot_location(
                    db,
                    ams_id,
                    tray_id,
                    create_if_missing=True,
                )
                if not location:
                    logger.warning(
                        "Unable to resolve/create location for spool %s slot %s-%s",
                        filaman_spool_id,
                        ams_id,
                        tray_id,
                    )
                    return

                # 3. Spule zur Location bewegen (wenn nicht bereits dort)
                spool = await db.get(Spool, filaman_spool_id)
                if not spool:
                    logger.warning(
                        f"Spool {filaman_spool_id} not found, cannot update location"
                    )
                    return

                # 3a. Slot eindeutig halten: bestehende Spulen aus dem Ziel-Slot entfernen
                conflict_result = await db.execute(
                    select(Spool).where(
                        Spool.location_id == location.id,
                        Spool.id != filaman_spool_id,
                    )
                )
                conflicting_spools = conflict_result.scalars().all()
                if conflicting_spools:
                    service = SpoolService(db)
                    event_at = datetime.now(timezone.utc)
                    for conflicting_spool in conflicting_spools:
                        await service.move_location(
                            conflicting_spool,
                            None,
                            event_at,
                            source="driver",
                            note=f"Unassigned from {slot_location_name} (replaced by spool {filaman_spool_id})",
                        )
                    logger.info(
                        "Cleared %s conflicting spool assignment(s) from '%s'",
                        len(conflicting_spools),
                        slot_location_name,
                    )

                if spool.location_id == location.id:
                    logger.debug(
                        f"Spool {filaman_spool_id} already at location '{slot_location_name}'"
                    )
                    await db.commit()
                    return

                # SpoolService fÃ¼r konsistente Event-Generierung nutzen
                await SpoolService(db).move_location(
                    spool,
                    location.id,
                    datetime.now(timezone.utc),
                    source="driver",
                    note=f"Assigned to {slot_location_name}",
                )

                # Einmaliger commit fÃ¼r beide Operationen (Location + Move)
                await db.commit()

                logger.info(
                    f"Moved spool {filaman_spool_id} to location '{slot_location_name}' "
                    f"(location_id={location.id})"
                )

        except Exception as e:
            logger.error(
                f"Failed to update location for spool {filaman_spool_id} "
                f"(slot {ams_id}-{tray_id}): {e}",
                exc_info=True,
            )

    @staticmethod
    def _map_slot_index_to_3mf_id(ams_id: int, tray_id: int) -> int | None:
        """Return the 1-based 3MF slot_id for a given (ams_id, tray_id) pair.

        Bambu Studio numbers 3MF filament slots as ams_id*4 + tray_id + 1 for
        normal AMS units.  External tray coordinates (ams_id >= 200) do not map
        to this scheme; return None so the single-filament fallback can be used.

        This mapping is a heuristic â€“ it matches Bambu Studio's current slicer
        convention but may not cover all third-party slicer output.  Missing
        slot_ids are handled by the single-filament fallback in the caller.
        """
        if (
            Driver._is_external_slot_ams_id(ams_id)
            or Driver._is_ams_ht_slot_ams_id(ams_id)
        ):  # external/AMS-HT tray â€“ no predictable 3MF slot id
            return None
        return ams_id * 4 + tray_id + 1

    @staticmethod
    def _find_filament_id_from_mapping(
        ams_id: int, tray_id: int, mapping_array: list[int]
    ) -> int | None:
        """Return the 1-based 3MF filament_id for a physical slot using the mapping array.
        
        The mapping array is the source of truth from print.mapping, where:
        - mapping[i] encodes the physical slot (ams_id, tray_id) for filament_id=i+1
        - mapping[i] // 256 = ams_id, mapping[i] % 256 = tray_id
        
        Args:
            ams_id: Physical AMS unit id
            tray_id: Physical tray id within the AMS
            mapping_array: List of encoded physical slots from print.mapping
            
        Returns:
            1-based filament_id that corresponds to this physical slot, or None if not found.
        """
        encoded_slot = ams_id * 256 + tray_id
        
        for filament_idx, encoded in enumerate(mapping_array):
            try:
                if int(encoded) == encoded_slot:
                    return filament_idx + 1  # 1-based filament id
            except (ValueError, TypeError):
                continue
        
        return None

    @staticmethod
    def _slot_index_to_no(slot_index: str) -> int:
        """Convert driver slot_index string (e.g. '0-1', '255-254') to integer slot_no.

        Mirrors filaman-system plugin manager mapping for compatibility.
        """
        parts = slot_index.split("-", 1)
        if len(parts) == 2:
            try:
                unit, tray = int(parts[0]), int(parts[1])
                if unit >= 200:  # external tray
                    return 1000 + tray
                return unit * 4 + tray
            except ValueError:
                pass
        return hash(slot_index) % 10000

    async def _clear_slot_assignment(self, slot_index: str) -> None:
        """Clear spool assignment when a slot becomes empty.

        Plugin-only behavior: keeps filaman core untouched while ensuring
        removed spools no longer stay linked to an empty printer slot.
        """
        try:
            self._set_slot_spool_id(slot_index, None)
            slot_no = self._slot_index_to_no(slot_index)
            async with async_session_maker() as db:
                old_spool_id: int | None = None
                result = await db.execute(
                    select(PrinterSlot)
                    .options(selectinload(PrinterSlot.assignment))
                    .where(
                        PrinterSlot.printer_id == self.printer_id,
                        PrinterSlot.slot_no == slot_no,
                    )
                )
                printer_slot = result.scalar_one_or_none()
                if not printer_slot or not printer_slot.assignment:
                    logger.debug(
                        "No PrinterSlotAssignment row to clear for printer %s slot %s",
                        self.printer_id,
                        slot_index,
                    )
                else:
                    assignment: PrinterSlotAssignment = printer_slot.assignment
                    old_spool_id = assignment.spool_id
                    assignment.spool_id = None
                    assignment.present = False

                # ZusÃ¤tzlich Spulen-Location aus dem AMS-Slot entfernen, damit
                # kein Spool auf einer leeren Slot-Location hÃ¤ngen bleibt.
                slot_location_name: str | None = None
                location: Location | None = None
                try:
                    ams_id, tray_id = (int(p) for p in slot_index.split("-", 1))
                    slot_location_name = self._generate_slot_location_name(ams_id, tray_id)
                    location = await self._resolve_slot_location(
                        db,
                        ams_id,
                        tray_id,
                        create_if_missing=False,
                    )
                except (ValueError, TypeError):
                    slot_location_name = None

                if slot_location_name and location:
                        spools_result = await db.execute(
                            select(Spool).where(Spool.location_id == location.id)
                        )
                        spools_in_slot = spools_result.scalars().all()
                        if spools_in_slot:
                            service = SpoolService(db)
                            event_at = datetime.now(timezone.utc)
                            for spool in spools_in_slot:
                                await service.move_location(
                                    spool,
                                    None,
                                    event_at,
                                    source="driver",
                                    note=f"Removed from empty slot {slot_location_name}",
                                )
                            logger.info(
                                "Cleared location for %s spool(s) from empty slot '%s'",
                                len(spools_in_slot),
                                slot_location_name,
                            )

                await db.commit()
                logger.info(
                    "Cleared slot assignment for printer %s slot %s (spool %s)",
                    self.printer_id,
                    slot_index,
                    old_spool_id,
                )
        except Exception as e:
            logger.warning(
                "Failed clearing slot assignment for printer %s slot %s: %s",
                self.printer_id,
                slot_index,
                e,
            )

    @staticmethod
    def _to_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("1", "true", "yes", "on"):
                return True
            if normalized in ("0", "false", "no", "off"):
                return False
        return default

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _parse_progress(print_data: dict[str, Any]) -> float | None:
        for key in ("mc_percent", "print_progress"):
            raw = print_data.get(key)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (ValueError, TypeError):
                continue
            return max(0.0, min(100.0, value))
        return None

    @staticmethod
    def _parse_layer_num(raw: Any) -> int | None:
        try:
            if raw is None:
                return None
            value = float(raw)
        except (ValueError, TypeError):
            return None
        if value < 0:
            return None
        return int(value)

    @staticmethod
    def _parse_remain_value(raw: Any) -> float | None:
        try:
            if raw is None:
                return None
            value = float(raw)
        except (ValueError, TypeError):
            return None
        if value < 0:
            return None
        return min(100.0, value)

    @staticmethod
    def _has_remain_sensor(raw: Any) -> bool:
        try:
            if raw is None:
                return False
            value = float(raw)
        except (ValueError, TypeError):
            return False
        return 0.0 <= value <= 100.0

    @staticmethod
    def _build_3mf_candidate_filenames(print_context: dict[str, Any]) -> list[str]:
        def _normalize_source(raw_value: Any) -> tuple[str, str] | None:
            value = str(raw_value).replace("\\", "/").split("/")[-1].strip()
            if not value:
                return None

            canonical = value.lower()
            if canonical.endswith(".gcode.3mf"):
                canonical = canonical[: -len(".gcode.3mf")]
            elif canonical.endswith(".3mf"):
                canonical = canonical[: -len(".3mf")]
            elif canonical.endswith(".gcode"):
                canonical = canonical[: -len(".gcode")]

            return value, canonical

        candidates: list[str] = []
        seen_sources: set[str] = set()
        for key in ("subtask_name", "gcode_file"):
            raw = print_context.get(key)
            if not raw:
                continue

            normalized = _normalize_source(raw)
            if normalized is None:
                continue

            value, source_key = normalized
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)

            if value.endswith(".3mf"):
                candidates.append(value)
            elif value.endswith(".gcode"):
                candidates.append(f"{value}.3mf")
            else:
                candidates.append(f"{value}.3mf")
                candidates.append(f"{value}.gcode.3mf")

        deduped: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    @staticmethod
    def _extract_3mf_total_used_grams_from_file(file_path: str) -> float | None:
        try:
            with ZipFile(file_path) as archive:
                data = archive.read("Metadata/slice_info.config")
            plate = ElementTree.fromstring(data).find("plate")
            if plate is None:
                return None

            total_grams = 0.0
            found = False
            for elem in plate.findall("filament"):
                used_g = elem.get("used_g")
                if used_g is None:
                    continue
                try:
                    grams = float(used_g)
                except (ValueError, TypeError):
                    continue
                if grams <= 0:
                    continue
                total_grams += grams
                found = True
            return total_grams if found and total_grams > 0 else None
        except Exception:
            return None

    @staticmethod
    def _extract_per_slot_filament_usage_from_file(
        file_path: str,
    ) -> list[dict[str, Any]] | None:
        """Extract per-slot filament usage from 3MF slice_info.config.

        Returns list of dicts with: slot_id, used_g, color, type, density, diameter.
        slot_id is 1-based (as in 3MF slicer indexing).
        """
        try:
            with ZipFile(file_path) as archive:
                data = archive.read("Metadata/slice_info.config")
            logger.info("Per-slot extraction: slice_info.config loaded, parsing XML")
            root = ElementTree.fromstring(data)
            plate = root.find("plate")

            # Support multiple known slice_info layouts:
            # 1) plate/filament with attributes
            # 2) filament entries elsewhere with child elements
            filament_nodes = (
                plate.findall("filament")
                if plate is not None
                else root.findall(".//filament")
            )
            if not filament_nodes:
                logger.warning("Per-slot extraction: no filament elements in slice_info.config")
                return None

            # Pre-scan raw slot IDs to determine correct base offset.
            # Bambu Studio emits 1-based IDs; some slicers may emit 0-based (0,1,2,â€¦).
            # If any raw ID is 0 we shift ALL IDs by +1 to normalise to 1-based,
            # avoiding the collision that a per-element conditional would cause.
            raw_ids: list[int | None] = []
            for filament in filament_nodes:
                slot_id_str = (
                    filament.get("filament_id")
                    or filament.get("slot_id")
                    or filament.get("id")
                )
                if not slot_id_str:
                    for tag in ("filament_id", "slot_id", "id"):
                        tag_node = filament.find(tag)
                        if tag_node is not None and tag_node.text:
                            slot_id_str = str(tag_node.text).strip()
                            break
                try:
                    raw_ids.append(int(slot_id_str) if slot_id_str else None)
                except (ValueError, TypeError):
                    raw_ids.append(None)

            parseable = [r for r in raw_ids if r is not None]
            slot_id_offset = 1 if (parseable and min(parseable) == 0) else 0
            if slot_id_offset:
                logger.debug(
                    "Per-slot extraction: detected 0-based slot IDs; applying +1 offset to all"
                )

            filaments = []
            for idx, filament in enumerate(filament_nodes, start=1):
                # Resolve slot id: use pre-scanned raw value with global offset,
                # or fall back to enumeration index (already 1-based).
                raw_slot_id = raw_ids[idx - 1]
                if raw_slot_id is not None:
                    slot_id = raw_slot_id + slot_id_offset
                else:
                    slot_id = idx
                    logger.debug(
                        "Per-slot extraction: invalid slot id for element %d, fallback to index=%d",
                        idx,
                        idx,
                    )

                used_g_str = filament.get("used_g")
                if not used_g_str:
                    used_g_node = filament.find("used_g")
                    if used_g_node is not None and used_g_node.text is not None:
                        used_g_str = str(used_g_node.text).strip()

                try:
                    used_g = float(used_g_str) if used_g_str else 0.0
                except (ValueError, TypeError):
                    logger.warning(
                        "Per-slot extraction: slot %d - invalid used_g value: %s, using 0.0",
                        slot_id,
                        used_g_str,
                    )
                    used_g = 0.0

                # Extract properties from attribute or child element.
                color = filament.get("color", "")
                if not color:
                    color_node = filament.find("color")
                    color = (
                        str(color_node.text).strip()
                        if color_node is not None and color_node.text is not None
                        else ""
                    )

                filament_type = filament.get("type", "")
                if not filament_type:
                    type_node = filament.find("type")
                    filament_type = (
                        str(type_node.text).strip()
                        if type_node is not None and type_node.text is not None
                        else ""
                    )

                density_str = filament.get("density")
                if not density_str:
                    density_node = filament.find("density")
                    if density_node is not None and density_node.text is not None:
                        density_str = str(density_node.text).strip()

                diameter_str = filament.get("diameter")
                if not diameter_str:
                    diameter_node = filament.find("diameter")
                    if diameter_node is not None and diameter_node.text is not None:
                        diameter_str = str(diameter_node.text).strip()

                try:
                    density = float(density_str) if density_str else 1.24
                except (ValueError, TypeError):
                    logger.warning(
                        "Per-slot extraction: slot %d - invalid density value: %s, using 1.24",
                        slot_id,
                        density_str,
                    )
                    density = 1.24

                try:
                    diameter = float(diameter_str) if diameter_str else 1.75
                except (ValueError, TypeError):
                    logger.warning(
                        "Per-slot extraction: slot %d - invalid diameter value: %s, using 1.75",
                        slot_id,
                        diameter_str,
                    )
                    diameter = 1.75

                logger.info(
                    "Per-slot extraction: slot_id=%d type=%s color=%s used_g=%.3f density=%.2f diameter=%.2f",
                    slot_id,
                    filament_type,
                    color,
                    used_g,
                    density,
                    diameter,
                )

                filaments.append(
                    {
                        "slot_id": slot_id,
                        "used_g": used_g,
                        "color": color,
                        "type": filament_type,
                        "density": density,
                        "diameter": diameter,
                    }
                )

            if filaments:
                logger.info("Per-slot extraction: successfully extracted %d slots", len(filaments))
                return filaments
            else:
                logger.warning("Per-slot extraction: no valid slots extracted")
                return None
        except Exception as e:
            logger.warning("Per-slot extraction: failed to parse slice_info.config: %s: %s", type(e).__name__, e, exc_info=True)
            return None

    @staticmethod
    def _extract_layer_gcode_usage_from_file(
        file_path: str,
    ) -> dict[int, list[tuple[int, float]]] | None:
        """Extract per-layer filament usage (mm) from 3MF gcode.

        Returns dict[filament_id] = [(layer_num, cumulative_mm), ...]
        where filament_id is 0-based (as in gcode) and layer_num is 0-based.
        """
        try:
            with ZipFile(file_path) as archive:
                # Try to find gcode file: typically "3D/model.gcode"
                gcode_path = None
                for name in archive.namelist():
                    if name.endswith(".gcode"):
                        gcode_path = name
                        break

                if gcode_path is None:
                    return None

                gcode_data = archive.read(gcode_path).decode("utf-8", errors="ignore")

            layer_usage: dict[int, list[tuple[int, float]]] = {}
            current_layer = 0
            cumulative_mm: dict[int, float] = {}  # filament_id â†’ cumulative mm
            last_e_by_filament: dict[int, float] = {}
            current_filament_id = 0
            e_mode_absolute = False

            for line in gcode_data.split("\n"):
                line = line.strip()
                # Detect layer changes: ;LAYER:X
                if line.startswith(";LAYER:"):
                    try:
                        current_layer = int(line.split(":")[1])
                    except (ValueError, IndexError):
                        pass
                    continue

                if not line or line.startswith(";"):
                    continue

                if line.startswith("M82"):
                    e_mode_absolute = True
                    continue

                if line.startswith("M83"):
                    e_mode_absolute = False
                    continue

                if line.startswith("T"):
                    token = line.split()[0]
                    try:
                        current_filament_id = int(token[1:])
                    except (ValueError, TypeError):
                        continue
                    cumulative_mm.setdefault(current_filament_id, 0.0)
                    continue

                if line.startswith("G92"):
                    parts = line.split()
                    for part in parts:
                        if part.startswith("E"):
                            try:
                                last_e_by_filament[current_filament_id] = float(part[1:])
                            except ValueError:
                                pass
                            break
                    continue

                # Parse extrusion moves: G1 X... Y... Z... E...
                if not line.startswith("G1"):
                    continue

                # Extract E value (extrusion)
                parts = line.split()
                e_value = None
                for part in parts:
                    if part.startswith("E"):
                        try:
                            e_value = float(part[1:])
                        except ValueError:
                            pass
                        break

                if e_value is None:
                    continue

                cumulative_mm.setdefault(current_filament_id, 0.0)
                extrusion_mm = 0.0
                if e_mode_absolute:
                    previous_e = last_e_by_filament.get(current_filament_id)
                    if previous_e is None:
                        extrusion_mm = max(0.0, e_value)
                    else:
                        extrusion_mm = max(0.0, e_value - previous_e)
                    last_e_by_filament[current_filament_id] = e_value
                else:
                    if e_value > 0:
                        extrusion_mm = e_value

                if extrusion_mm <= 0:
                    continue

                cumulative_mm[current_filament_id] += extrusion_mm

                # Record cumulative usage at this layer
                if current_filament_id not in layer_usage:
                    layer_usage[current_filament_id] = []
                if (
                    not layer_usage[current_filament_id]
                    or layer_usage[current_filament_id][-1][0] != current_layer
                ):
                    layer_usage[current_filament_id].append(
                        (current_layer, cumulative_mm[current_filament_id])
                    )
                else:
                    # Update last entry for this layer
                    layer_usage[current_filament_id][-1] = (
                        current_layer,
                        cumulative_mm[current_filament_id],
                    )

            return layer_usage if layer_usage else None
        except Exception:
            return None

    @staticmethod
    def _mm_to_grams(mm: float, diameter_mm: float, density_g_cm3: float) -> float:
        """Convert filament extrusion length (mm) to weight (grams).

        Assumes filament is a cylinder: volume = Ï€ * (d/2)Â² * L
        Then mass = volume * density
        """
        if mm <= 0 or diameter_mm <= 0 or density_g_cm3 <= 0:
            logger.debug("mm_to_grams: invalid input (mm=%.1f diameter=%.2f density=%.2f)", mm, diameter_mm, density_g_cm3)
            return 0.0

        radius_cm = (diameter_mm / 2) / 10.0  # Convert mm to cm
        length_cm = mm / 10.0
        volume_cm3 = 3.14159 * radius_cm * radius_cm * length_cm
        result = volume_cm3 * density_g_cm3
        logger.debug("mm_to_grams: mm=%.1f diameter=%.2f density=%.2f â†’ volume=%.3f cmÂ³ â†’ grams=%.3f",
            mm, diameter_mm, density_g_cm3, volume_cm3, result)
        return result

    def _run_ftp_diagnostics(self) -> None:
        """Run in a background thread at driver start-up.  Connects to the printer
        via implicit FTPS, lists available directories, downloads the smallest
        available file as a smoke-test, then logs a clear PASS / FAIL summary."""
        if not self._host or not self._access_code:
            logger.info(
                "FTP diagnostics skipped for printer %s: host or access_code not configured",
                self.printer_id,
            )
            return

        logger.info(
            "FTP diagnostics starting for printer %s at %s:990",
            self.printer_id,
            self._host,
        )

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        ftp: ImplicitFTP_TLS | None = None
        try:
            ftp = ImplicitFTP_TLS(context=context)
            ftp.connect(host=self._host, port=990, timeout=15)
            ftp.login(user="bblp", passwd=self._access_code)
            ftp.prot_p()
            logger.info(
                "FTP diagnostics connected for printer %s (bblp@%s:990)",
                self.printer_id,
                self._host,
            )
        except Exception as exc:
            logger.error(
                "FTP diagnostics FAILED for printer %s: could not connect â€“ %s: %s",
                self.printer_id,
                type(exc).__name__,
                exc,
            )
            return

        # --- list files in /cache/ and / -----------------------------------------------
        listed_files: list[tuple[str, int]] = []  # (remote_path, size)
        for search_path in ("/cache/", "/"):
            try:
                lines: list[str] = []
                ftp.retrlines(f"LIST {search_path}", lines.append)
                for line in lines:
                    # Typical format: -rw-r--r-- 1 user group SIZE Mon DD HH:MM filename
                    parts = line.split(None, 8)
                    if len(parts) < 9:
                        continue
                    if parts[0].startswith("d"):
                        continue  # skip directories
                    try:
                        size = int(parts[4])
                    except ValueError:
                        continue
                    filename = parts[8].strip()
                    remote_path = f"{search_path}{filename}"
                    listed_files.append((remote_path, size))
                    logger.debug(
                        "FTP diagnostics listed for printer %s: %s (%d bytes)",
                        self.printer_id,
                        remote_path,
                        size,
                    )
            except Exception as exc:
                logger.debug(
                    "FTP diagnostics list failed for printer %s at %s: %s",
                    self.printer_id,
                    search_path,
                    exc,
                )

        logger.info(
            "FTP diagnostics listed %d file(s) for printer %s",
            len(listed_files),
            self.printer_id,
        )

        # --- download smallest available file as smoke-test ----------------------------
        if listed_files:
            # Pick the smallest file to minimise test time (cap at 5 MB)
            MAX_TEST_BYTES = 5 * 1024 * 1024
            candidates = [
                (path, size)
                for path, size in listed_files
                if 0 < size <= MAX_TEST_BYTES
            ]
            if candidates:
                test_path, test_size = min(candidates, key=lambda x: x[1])
                downloaded = 0
                t_start = time.monotonic()
                try:
                    buf = bytearray()

                    def _recv(chunk: bytes) -> None:
                        nonlocal downloaded
                        buf.extend(chunk)
                        downloaded += len(chunk)

                    ftp.retrbinary(f"RETR {test_path}", _recv)
                    elapsed = max(0.001, time.monotonic() - t_start)
                    rate_kb = downloaded / elapsed / 1024.0
                    logger.info(
                        "FTP diagnostics PASS for printer %s: downloaded %s "
                        "(%d / %d bytes, %.2f s, %.1f KB/s) â€“ FTP is working",
                        self.printer_id,
                        test_path,
                        downloaded,
                        test_size,
                        elapsed,
                        rate_kb,
                    )
                except Exception as exc:
                    logger.error(
                        "FTP diagnostics FAILED for printer %s: download of %s failed â€“ %s: %s",
                        self.printer_id,
                        test_path,
                        type(exc).__name__,
                        exc,
                    )
            else:
                logger.info(
                    "FTP diagnostics PASS for printer %s: connection and listing OK "
                    "(no small test file available, skipping download test) â€“ FTP is working",
                    self.printer_id,
                )
        else:
            logger.info(
                "FTP diagnostics PASS for printer %s: connection OK, "
                "no files found in /cache/ or / â€“ FTP is working",
                self.printer_id,
            )

        try:
            ftp.quit()
        except Exception:
            pass

    def _try_download_local_3mf_total_grams(
        self,
        print_context: dict[str, Any],
    ) -> float | None:
        if not self._host or not self._access_code:
            return None

        candidates = self._build_3mf_candidate_filenames(print_context)
        if not candidates:
            return None

        timeout = max(2.0, float(self._local_3mf_timeout_seconds))
        search_paths = ("/cache/", "/")
        ftp: ImplicitFTP_TLS | None = None
        try:
            context = ssl.create_default_context()
            if not self._local_3mf_ftps_verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            logger.info(
                "FTPS metadata fetch start for printer %s: host=%s verify_tls=%s timeout=%.1fs candidates=%s",
                self.printer_id,
                self._host,
                self._local_3mf_ftps_verify_tls,
                timeout,
                len(candidates),
            )
            ftp = ImplicitFTP_TLS(context=context)
            ftp.connect(host=self._host, port=990, timeout=timeout)
            ftp.login(user="bblp", passwd=self._access_code)
            ftp.prot_p()
            logger.info(
                "FTPS metadata fetch connected for printer %s: user=bblp port=990",
                self.printer_id,
            )

            for candidate in candidates:
                for base in search_paths:
                    remote_path = f"{base}{candidate}" if not candidate.startswith("/") else candidate
                    logger.info(
                        "FTPS metadata fetch probing remote file for printer %s: %s",
                        self.printer_id,
                        remote_path,
                    )
                    try:
                        size = ftp.size(remote_path)
                        if not size or int(size) <= 0:
                            logger.info(
                                "FTPS metadata fetch skipped non-positive size for printer %s: %s size=%s",
                                self.printer_id,
                                remote_path,
                                size,
                            )
                            continue
                    except Exception:
                        logger.info(
                            "FTPS metadata fetch remote file not available for printer %s: %s",
                            self.printer_id,
                            remote_path,
                        )
                        continue

                    temp_file_path: str | None = None
                    transferred_bytes = 0
                    transfer_started = time.monotonic()
                    try:
                        with tempfile.NamedTemporaryFile(
                            prefix=f"bambu_{self.printer_id}_",
                            suffix=".3mf",
                            delete=False,
                        ) as temp_file:
                            temp_file_path = temp_file.name

                            def _write_chunk(data: bytes) -> None:
                                nonlocal transferred_bytes
                                temp_file.write(data)
                                transferred_bytes += len(data)

                            ftp.retrbinary(f"RETR {remote_path}", _write_chunk)
                    except Exception:
                        logger.warning(
                            "FTPS metadata fetch download failed for printer %s: %s",
                            self.printer_id,
                            remote_path,
                        )
                        continue

                    transfer_elapsed = max(0.001, time.monotonic() - transfer_started)
                    transfer_rate = transferred_bytes / transfer_elapsed
                    logger.info(
                        "FTPS metadata fetch transfer complete for printer %s: path=%s remote_size=%s transferred=%s bytes elapsed=%.3fs rate=%.1f KB/s",
                        self.printer_id,
                        remote_path,
                        size,
                        transferred_bytes,
                        transfer_elapsed,
                        transfer_rate / 1024.0,
                    )

                    try:
                        if not temp_file_path:
                            continue
                        grams = self._extract_3mf_total_used_grams_from_file(temp_file_path)
                        if grams is not None and grams > 0:
                            self.log_debug(
                                "event",
                                "consumption_tracking",
                                {
                                    "event": "local_3mf_weight_resolved",
                                    "printer_id": self.printer_id,
                                    "remote_path": remote_path,
                                    "total_grams": grams,
                                    "transferred_bytes": transferred_bytes,
                                    "transfer_elapsed_seconds": transfer_elapsed,
                                },
                            )
                            logger.info(
                                "FTPS metadata parse resolved for printer %s: path=%s total_grams=%.3f",
                                self.printer_id,
                                remote_path,
                                grams,
                            )
                            return grams
                        logger.info(
                            "FTPS metadata parse returned no grams for printer %s: path=%s",
                            self.printer_id,
                            remote_path,
                        )
                    finally:
                        if temp_file_path:
                            try:
                                os.remove(temp_file_path)
                                logger.info(
                                    "FTPS metadata temp file cleaned for printer %s: %s",
                                    self.printer_id,
                                    temp_file_path,
                                )
                            except Exception as cleanup_error:
                                logger.warning(
                                    "FTPS metadata temp cleanup failed for printer %s: %s (%s)",
                                    self.printer_id,
                                    temp_file_path,
                                    cleanup_error,
                                )
        except Exception as e:
            self.log_debug(
                "event",
                "consumption_tracking",
                {
                    "event": "local_3mf_weight_failed",
                    "printer_id": self.printer_id,
                    "error": str(e),
                },
            )
            logger.warning(
                "FTPS metadata fetch failed for printer %s: %s",
                self.printer_id,
                e,
            )
        finally:
            if ftp is not None:
                try:
                    ftp.quit()
                except Exception:
                    pass
            logger.info(
                "FTPS metadata fetch finished for printer %s",
                self.printer_id,
            )
        return None

    def _get_3mf_file_path(
        self,
        print_context: dict[str, Any],
    ) -> str | None:
        """Download 3MF file and return path (for per-slot/per-layer extraction).
        
        Unlike _try_download_local_3mf_total_grams, keeps the temp file around
        so caller can do additional processing. Caller must clean up the file.
        """
        if not self._host or not self._access_code:
            logger.warning("_get_3mf_file_path: host or access_code not configured")
            return None

        candidates = self._build_3mf_candidate_filenames(print_context)
        if not candidates:
            logger.warning("_get_3mf_file_path: no 3MF candidates found")
            return None

        timeout = max(2.0, float(self._local_3mf_timeout_seconds))
        search_paths = ("/cache/", "/")
        ftp: ImplicitFTP_TLS | None = None
        logger.info("_get_3mf_file_path: searching for 3MF on printer %s (candidates: %s)", self.printer_id, candidates)
        try:
            context = ssl.create_default_context()
            if not self._local_3mf_ftps_verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            ftp = ImplicitFTP_TLS(context=context)
            ftp.connect(host=self._host, port=990, timeout=timeout)
            ftp.login(user="bblp", passwd=self._access_code)
            ftp.prot_p()
            logger.info("_get_3mf_file_path: FTPS connected to printer %s:990", self.printer_id)

            for candidate in candidates:
                for base in search_paths:
                    remote_path = f"{base}{candidate}" if not candidate.startswith("/") else candidate
                    try:
                        size = ftp.size(remote_path)
                        if not size or int(size) <= 0:
                            logger.debug("_get_3mf_file_path: skipped %s (size=%s)", remote_path, size)
                            continue
                        logger.info("_get_3mf_file_path: found %s (size=%d bytes)", remote_path, int(size))
                    except Exception as e:
                        logger.debug("_get_3mf_file_path: probe failed for %s: %s", remote_path, e)
                        continue

                    temp_file_path: str | None = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            prefix="3mf_",
                            suffix=".3mf",
                            delete=False,
                        ) as f:
                            temp_file_path = f.name

                        with open(temp_file_path, "wb") as f:
                            ftp.retrbinary(f"RETR {remote_path}", f.write)

                        logger.info("_get_3mf_file_path: successfully downloaded %s to %s", remote_path, temp_file_path)
                        return temp_file_path

                    except Exception as e:
                        logger.warning("_get_3mf_file_path: download failed for %s: %s", remote_path, e)
                        if temp_file_path:
                            try:
                                os.remove(temp_file_path)
                            except Exception:
                                pass
                        continue

        except Exception as e:
            logger.warning("_get_3mf_file_path: FTPS connection failed: %s: %s", type(e).__name__, e)
        finally:
            if ftp:
                try:
                    ftp.quit()
                except Exception:
                    pass
        logger.warning("_get_3mf_file_path: no 3MF file found after searching all candidates")
        return None

    async def _prefetch_tracking_metadata_after_delay(self, generation: int) -> None:
        lock_acquired = False
        try:
            current_task = asyncio.current_task()
            await asyncio.sleep(self._tracking_prefetch_delay_seconds)

            # If a newer prefetch generation replaced this one, do not run duplicate work.
            if generation != self._tracking_prefetch_generation:
                logger.info(
                    "Metadata prefetch skipped for printer %s: stale generation superseded",
                    self.printer_id,
                )
                return

            if current_task is not None and self._tracking_prefetch_task is not current_task:
                logger.info(
                    "Metadata prefetch skipped for printer %s: stale task superseded",
                    self.printer_id,
                )
                return

            if not self._tracking_active:
                logger.info(
                    "Metadata prefetch skipped for printer %s: tracking inactive",
                    self.printer_id,
                )
                return

            context_snapshot = dict(self._tracking_print_context)
            logger.info(
                "Metadata prefetch starting for printer %s after %.0fs",
                self.printer_id,
                self._tracking_prefetch_delay_seconds,
            )

            if generation != self._tracking_prefetch_generation:
                logger.info(
                    "Metadata prefetch skipped for printer %s: stale generation superseded",
                    self.printer_id,
                )
                return

            if current_task is not None and self._tracking_prefetch_task is not current_task:
                logger.info(
                    "Metadata prefetch skipped for printer %s: stale task superseded",
                    self.printer_id,
                )
                return

            if self._tracking_prefetched_total_grams is not None or self._tracking_prefetched_3mf_path:
                logger.info(
                    "Metadata prefetch skipped for printer %s: metadata already prefetched",
                    self.printer_id,
                )
                return

            if self._tracking_prefetch_in_flight:
                logger.info(
                    "Metadata prefetch skipped for printer %s: another prefetch is already in flight",
                    self.printer_id,
                )
                return

            self._tracking_prefetch_in_flight = True

            if not self._acquire_cross_process_prefetch_lock():
                logger.info(
                    "Metadata prefetch skipped for printer %s: another process holds prefetch lock",
                    self.printer_id,
                )
                self._tracking_prefetch_in_flight = False
                return
            lock_acquired = True

            prefetched_3mf_path = await asyncio.to_thread(
                self._get_3mf_file_path,
                context_snapshot,
            )

            if generation != self._tracking_prefetch_generation:
                logger.info(
                    "Metadata prefetch ignored for printer %s: stale generation completed after supersede",
                    self.printer_id,
                )
                if prefetched_3mf_path and os.path.exists(prefetched_3mf_path):
                    try:
                        os.remove(prefetched_3mf_path)
                    except Exception:
                        pass
                return

            if current_task is not None and self._tracking_prefetch_task is not current_task:
                logger.info(
                    "Metadata prefetch ignored for printer %s: stale task completed after supersede",
                    self.printer_id,
                )
                if prefetched_3mf_path and os.path.exists(prefetched_3mf_path):
                    try:
                        os.remove(prefetched_3mf_path)
                    except Exception:
                        pass
                return

            if prefetched_3mf_path:
                # Replace any stale prefetched file with the latest one.
                old_prefetched_path = self._tracking_prefetched_3mf_path
                if (
                    old_prefetched_path
                    and old_prefetched_path != prefetched_3mf_path
                    and os.path.exists(old_prefetched_path)
                ):
                    try:
                        os.remove(old_prefetched_path)
                    except Exception:
                        pass

                self._tracking_prefetched_3mf_path = prefetched_3mf_path
                resolved_total = await asyncio.to_thread(
                    self._extract_3mf_total_used_grams_from_file,
                    prefetched_3mf_path,
                )
            else:
                resolved_total = None

            if resolved_total is not None and resolved_total > 0:
                self._tracking_prefetched_total_grams = resolved_total
                logger.info(
                    "Metadata prefetch resolved for printer %s: total_grams=%.3f (file=%s)",
                    self.printer_id,
                    resolved_total,
                    self._tracking_prefetched_3mf_path,
                )
            else:
                logger.info(
                    "Metadata prefetch finished without usable grams for printer %s",
                    self.printer_id,
                )
        except asyncio.CancelledError:
            logger.info(
                "Metadata prefetch cancelled for printer %s",
                self.printer_id,
            )
            raise
        except Exception as e:
            logger.warning(
                "Metadata prefetch failed for printer %s: %s",
                self.printer_id,
                e,
            )
        finally:
            if lock_acquired:
                self._release_cross_process_prefetch_lock()
            if generation == self._tracking_prefetch_generation:
                self._tracking_prefetch_in_flight = False
            current_task = asyncio.current_task()
            if current_task is not None and self._tracking_prefetch_task is current_task:
                self._tracking_prefetch_task = None

    def _update_tracking_print_context(self, print_data: dict[str, Any]) -> None:
        for key in (
            "subtask_name",
            "gcode_file",
            "job_id",
            "lan_task_id",
            "model_id",
            "design_id",
        ):
            val = print_data.get(key)
            if val is None:
                continue
            text = str(val).strip()
            if text:
                previous = self._tracking_print_context.get(key)
                self._tracking_print_context[key] = text
                if previous != text:
                    logger.info(
                        "Tracking print context updated for printer %s: key=%s value=%s",
                        self.printer_id,
                        key,
                        text,
                    )

    def _update_tracking_layer_numbers(self, print_data: dict[str, Any]) -> None:
        """Track the highest layer number reported by the printer during a print.

        Layer info arrives under ``print["3D"]`` as ``layer_num`` (current
        layer) and ``total_layer_num``. Keeping the maximum seen layer makes the
        per-layer gcode lookup robust against message ordering at finalize time.
        """
        three_d = print_data.get("3D")
        if not isinstance(three_d, dict):
            return

        layer_num = self._parse_layer_num(three_d.get("layer_num"))
        total_layer_num = self._parse_layer_num(three_d.get("total_layer_num"))

        if layer_num is not None and (
            self._tracking_max_layer_num is None
            or layer_num > self._tracking_max_layer_num
        ):
            self._tracking_max_layer_num = layer_num
        if total_layer_num is not None:
            self._tracking_total_layer_num = total_layer_num

    @staticmethod
    def _extract_mapping_slots(print_data: dict[str, Any]) -> set[str]:
        mapped_slots: set[str] = set()
        raw_mapping = print_data.get("mapping")
        if not isinstance(raw_mapping, list):
            return mapped_slots

        for raw in raw_mapping:
            try:
                encoded = int(raw)
            except (ValueError, TypeError):
                continue

            if encoded < 0:
                continue

            ams_id = encoded // 256
            tray_id = encoded % 256
            if tray_id < 0:
                continue

            mapped_slots.add(f"{ams_id}-{tray_id}")

        return mapped_slots

    def _start_consumption_tracking(
        self,
        progress: float | None,
        print_data: dict[str, Any],
    ) -> None:
        self._tracking_active = True
        self._tracking_started_at = datetime.now(timezone.utc)
        self._tracking_last_progress = progress or 0.0
        self._tracking_progress_delta = 0.0
        self._tracking_slots = {}
        self._tracking_consumed_slots = set()
        self._tracking_used_slots = set()
        self._tracking_mapping_array = None
        self._tracking_max_layer_num = None
        self._tracking_total_layer_num = None
        self._tracking_print_context = {}
        self._tracking_prefetched_total_grams = None
        self._tracking_finalize_queued = False
        self._tracking_finalize_in_progress = False
        self._tracking_prefetch_generation += 1
        self._tracking_prefetch_in_flight = False
        if self._tracking_prefetched_3mf_path and os.path.exists(
            self._tracking_prefetched_3mf_path
        ):
            try:
                os.remove(self._tracking_prefetched_3mf_path)
            except Exception:
                pass
        self._tracking_prefetched_3mf_path = None
        if self._tracking_prefetch_task and not self._tracking_prefetch_task.done():
            self._tracking_prefetch_task.cancel()
        self._tracking_prefetch_task = None
        self._update_tracking_print_context(print_data)

        # Capture mapping array from print_data (source of truth for filament-to-physical slot mapping)
        raw_mapping = print_data.get("mapping")
        if isinstance(raw_mapping, list):
            try:
                self._tracking_mapping_array = [int(m) for m in raw_mapping]
            except (ValueError, TypeError):
                self._tracking_mapping_array = None
        else:
            self._tracking_mapping_array = None

        for slot in self._current_slots:
            if not slot.get("present"):
                continue
            slot_index = slot.get("slot_index")
            if not slot_index:
                continue
            raw_remain = slot.get("remain")
            remain = self._parse_remain_value(slot.get("remain"))
            self._tracking_slots[slot_index] = {
                "start_remain": remain,
                "end_remain": remain,
                "slot_name": slot.get("slot_name", ""),
                "has_remain_sensor": self._has_remain_sensor(raw_remain),
            }

        logger.info(
            "Consumption tracking started for printer %s with %s slot(s)",
            self.printer_id,
            len(self._tracking_slots),
        )
        logger.info(
            "Print tracking start for printer %s: progress=%s slots=%s",
            self.printer_id,
            self._tracking_last_progress,
            sorted(self._tracking_slots.keys()),
        )
        self.log_debug(
            "event",
            "consumption_tracking",
            {
                "event": "started",
                "printer_id": self.printer_id,
                "started_at": self._tracking_started_at.isoformat()
                if self._tracking_started_at
                else None,
                "initial_progress": self._tracking_last_progress,
                "slot_count": len(self._tracking_slots),
                "slots": self._tracking_slots,
            },
        )

        if self._loop is not None:
            generation = self._tracking_prefetch_generation
            self._tracking_prefetch_task = self._loop.create_task(
                self._prefetch_tracking_metadata_after_delay(generation)
            )
            _prefetch_candidates = self._build_3mf_candidate_filenames(
                self._tracking_print_context
            )
            logger.info(
                "Scheduled metadata prefetch for printer %s in %.0fs (candidates: %s)",
                self.printer_id,
                self._tracking_prefetch_delay_seconds,
                ", ".join(_prefetch_candidates) if _prefetch_candidates else "none",
            )

    def _update_consumption_tracking_slots(self) -> None:
        for slot in self._current_slots:
            if not slot.get("present"):
                continue
            slot_index = slot.get("slot_index")
            if not slot_index:
                continue
            raw_remain = slot.get("remain")
            remain = self._parse_remain_value(slot.get("remain"))
            has_remain_sensor = self._has_remain_sensor(raw_remain)
            if slot_index not in self._tracking_slots:
                self._tracking_slots[slot_index] = {
                    "start_remain": remain,
                    "end_remain": remain,
                    "slot_name": slot.get("slot_name", ""),
                    "has_remain_sensor": has_remain_sensor,
                }
                self.log_debug(
                    "event",
                    "consumption_tracking",
                    {
                        "event": "slot_added_during_tracking",
                        "printer_id": self.printer_id,
                        "slot_index": slot_index,
                        "remain": remain,
                        "has_remain_sensor": has_remain_sensor,
                    },
                )
            else:
                previous_end = self._parse_remain_value(
                    self._tracking_slots[slot_index].get("end_remain")
                )
                self._tracking_slots[slot_index]["end_remain"] = remain
                self._tracking_slots[slot_index]["has_remain_sensor"] = bool(
                    self._tracking_slots[slot_index].get("has_remain_sensor")
                    or has_remain_sensor
                )
                if (
                    previous_end is not None
                    and remain is not None
                    and remain < previous_end
                ):
                    self._tracking_consumed_slots.add(slot_index)

    def _update_consumption_tracking_usage(self, print_data: dict[str, Any]) -> None:
        mapped_slots = self._extract_mapping_slots(print_data)
        
        # Update mapping array (captures union across all MQTT messages)
        raw_mapping = print_data.get("mapping")
        if isinstance(raw_mapping, list):
            try:
                new_mapping = [int(m) for m in raw_mapping]
                if self._tracking_mapping_array is None:
                    self._tracking_mapping_array = new_mapping
            except (ValueError, TypeError):
                pass
        
        if not mapped_slots:
            return

        current_slot_by_index = {
            str(slot.get("slot_index")): slot
            for slot in self._current_slots
            if slot.get("present") and slot.get("slot_index")
        }

        # print.mapping can contain all mapped project slots, not necessarily
        # the slots that actually extruded in this print. Marking all mapping
        # entries as "used" can cause false multi-spool attribution.
        if len(mapped_slots) != 1:
            logger.debug(
                "Consumption mapping update skipped used-slot marking for printer %s: mapped_slots=%s",
                self.printer_id,
                sorted(mapped_slots),
            )
            return

        for slot_index in mapped_slots:
            is_new_used_slot = slot_index not in self._tracking_used_slots
            self._tracking_used_slots.add(slot_index)
            if slot_index not in self._tracking_slots:
                slot = current_slot_by_index.get(slot_index, {})
                remain = self._parse_remain_value(slot.get("remain"))
                self._tracking_slots[slot_index] = {
                    "start_remain": remain,
                    "end_remain": remain,
                    "slot_name": slot.get("slot_name", ""),
                    "has_remain_sensor": self._has_remain_sensor(slot.get("remain")),
                }
                logger.info(
                    "Consumption slot detected from mapping for printer %s: slot=%s remain=%s",
                    self.printer_id,
                    slot_index,
                    remain,
                )

            if is_new_used_slot:
                logger.info(
                    "Print used-slot detected for printer %s: slot=%s",
                    self.printer_id,
                    slot_index,
                )

            self.log_debug(
                "event",
                "consumption_tracking",
                {
                    "event": "slot_marked_used",
                    "printer_id": self.printer_id,
                    "slot_index": slot_index,
                },
            )

    async def _acquire_3mf_path(
        self,
        prefetched_3mf_path: str | None,
        print_context: dict,
    ) -> str | None:
        """Return a local 3MF file path for extraction, reusing the prefetch when possible."""
        if prefetched_3mf_path and os.path.exists(prefetched_3mf_path):
            logger.info(
                "Per-slot/layer extraction: reusing prefetched 3MF file %s",
                prefetched_3mf_path,
            )
            return prefetched_3mf_path

        logger.info(
            "Per-slot/layer extraction: starting 3MF download for printer %s",
            self.printer_id,
        )
        try:
            path = await asyncio.to_thread(
                self._get_3mf_file_path,
                print_context,
            )
            if path:
                logger.info(
                    "Per-slot/layer extraction: 3MF file obtained at %s",
                    path,
                )
            else:
                logger.warning(
                    "Per-slot/layer extraction: no 3MF file path returned"
                )
            return path
        except Exception as e:
            logger.warning(
                "Per-slot/layer extraction: 3MF download failed: %s: %s",
                type(e).__name__,
                e,
                exc_info=True,
            )
            return None

    @staticmethod
    def _resolve_gcode_layer(
        layer_num: int | None,
        total_layer_num: int | None,
        max_gcode_layer: int,
        progress_delta: float,
    ) -> int:
        """Resolve the 0-based gcode layer reached for a partial print.

        Prefer the exact layer reported by the printer via MQTT
        (``print["3D"]["layer_num"]``); fall back to a progress-based estimate
        when no layer information is available.

        gcode ``;LAYER:`` markers are 0-based, while the printer layer number is
        1-based, so the offset is derived from ``total_layer_num`` relative to
        the highest gcode layer marker.
        """
        if layer_num is not None:
            if (
                total_layer_num is not None
                and total_layer_num > 0
                and max_gcode_layer > 0
            ):
                if total_layer_num == max_gcode_layer + 1:
                    # gcode markers 0..N-1, printer layers 1..N
                    offset = 1
                elif total_layer_num == max_gcode_layer:
                    # gcode markers and printer layers share numbering
                    offset = 0
                else:
                    # Unknown numbering: use fractional position.
                    fraction = max(0.0, min(1.0, layer_num / total_layer_num))
                    return max(
                        0, min(max_gcode_layer, int(fraction * max_gcode_layer))
                    )
                return max(0, min(max_gcode_layer, layer_num - offset))
            # No reliable total: assume 1-based printer layer onto 0-based gcode.
            return max(0, min(max_gcode_layer, layer_num - 1))

        if max_gcode_layer > 0:
            return int((progress_delta / 100.0) * max_gcode_layer)
        return int((progress_delta / 100.0) * 100)

    async def _extract_3mf_usage(
        self,
        threemf_3mf_path: str,
        end_state: str,
        progress_delta: float,
        current_layer: int | None = None,
        total_layer_num: int | None = None,
    ) -> tuple[dict[int, float] | None, dict[int, float] | None, list[dict] | None]:
        """Return (per_slot_usage, layer_grams_per_slot, per_slot_data)."""
        per_slot_usage: dict[int, float] | None = None
        layer_grams_per_slot: dict[int, float] | None = None
        per_slot_data: list[dict] | None = None

        try:
            per_slot_data = await asyncio.to_thread(
                self._extract_per_slot_filament_usage_from_file,
                threemf_3mf_path,
            )
            if per_slot_data:
                logger.info(
                    "Per-slot/layer extraction: extracted per-slot data for %d slots",
                    len(per_slot_data),
                )
                per_slot_usage = {}
                for item in per_slot_data:
                    slot_id = item.get("slot_id", 0)
                    used_g = item.get("used_g", 0.0)
                    if used_g > 0:
                        per_slot_usage[slot_id] = used_g
                        logger.debug(
                            "Per-slot/layer extraction: slot_id=%d â†’ used_g=%.3f grams",
                            slot_id,
                            used_g,
                        )
            else:
                logger.warning(
                    "Per-slot/layer extraction: per-slot data extraction returned None"
                )
        except Exception as e:
            logger.warning(
                "Per-slot/layer extraction: per-slot extraction failed: %s: %s",
                type(e).__name__,
                e,
                exc_info=True,
            )

        # For partial prints, try per-layer gcode accuracy
        if end_state != "completed" and per_slot_usage:
            logger.info(
                "Per-slot/layer extraction: attempting per-layer gcode extraction for partial print"
            )
            try:
                layer_usage = await asyncio.to_thread(
                    self._extract_layer_gcode_usage_from_file,
                    threemf_3mf_path,
                )
                if layer_usage:
                    logger.info(
                        "Per-slot/layer extraction: layer gcode extracted for %d filament(s)",
                        len(layer_usage),
                    )
                    layer_grams_per_slot = {}
                    max_layer = max(
                        (
                            layer_data[-1][0]
                            for layer_data in layer_usage.values()
                            if layer_data
                        ),
                        default=0,
                    )
                    current_layer_est = self._resolve_gcode_layer(
                        current_layer,
                        total_layer_num,
                        max_layer,
                        progress_delta,
                    )
                    logger.debug(
                        "Per-slot/layer extraction: current_layer_est=%d max_layer=%d (layer_num=%s total_layer_num=%s progress_delta=%.2f)",
                        current_layer_est,
                        max_layer,
                        current_layer,
                        total_layer_num,
                        progress_delta,
                    )

                    for filament_id, layer_data in layer_usage.items():
                        if not layer_data:
                            continue
                        mm_at_current = 0.0
                        for layer_num, cumulative_mm in layer_data:
                            if layer_num <= current_layer_est:
                                mm_at_current = cumulative_mm
                            else:
                                break
                        logger.debug(
                            "Per-slot/layer extraction: filament_id=%d â†’ mm_at_layer=%.1f",
                            filament_id,
                            mm_at_current,
                        )

                        slot_id = filament_id + 1
                        if per_slot_data:
                            filament_info = next(
                                (f for f in per_slot_data if f.get("slot_id") == slot_id),
                                None,
                            )
                            if filament_info:
                                density = filament_info.get("density", 1.24)
                                diameter = filament_info.get("diameter", 1.75)
                                layer_grams = self._mm_to_grams(mm_at_current, diameter, density)
                                if layer_grams > 0:
                                    layer_grams_per_slot[slot_id] = layer_grams
                                    logger.info(
                                        "Per-slot/layer extraction: slot_id=%d filament_id=%d - mm=%.1f diameter=%.2f density=%.2f â†’ grams=%.3f",
                                        slot_id,
                                        filament_id,
                                        mm_at_current,
                                        diameter,
                                        density,
                                        layer_grams,
                                    )
                                else:
                                    logger.debug(
                                        "Per-slot/layer extraction: slot_id=%d filament_id=%d - mm_to_grams returned 0",
                                        slot_id,
                                        filament_id,
                                    )
                            else:
                                logger.warning(
                                    "Per-slot/layer extraction: slot_id=%d filament_id=%d - filament_info not found",
                                    slot_id,
                                    filament_id,
                                )
                    if layer_grams_per_slot:
                        logger.info(
                            "Per-slot/layer extraction: computed layer-scale grams: %s",
                            layer_grams_per_slot,
                        )
                    else:
                        logger.warning(
                            "Per-slot/layer extraction: layer gcode data extracted but no layer_grams computed"
                        )
                else:
                    logger.warning(
                        "Per-slot/layer extraction: per-layer gcode extraction returned None"
                    )
            except Exception as e:
                logger.warning(
                    "Per-slot/layer extraction: per-layer gcode extraction failed: %s: %s",
                    type(e).__name__,
                    e,
                    exc_info=True,
                )

        if per_slot_usage:
            logger.info(
                "Per-slot 3MF usage extracted for printer %s: %s (partial=%s, layer_scale=%s)",
                self.printer_id,
                per_slot_usage,
                end_state != "completed",
                "yes" if layer_grams_per_slot else "no",
            )

        return per_slot_usage, layer_grams_per_slot, per_slot_data

    @staticmethod
    def _apply_3mf_grams_to_entries(
        entries: list[dict],
        per_slot_usage: dict[int, float] | None,
        layer_grams_per_slot: dict[int, float] | None,
        per_slot_data: list[dict] | None,
        mapping_array: list[int] | None,
        end_state: str,
        progress_delta: float,
    ) -> None:
        """Mutate entries in-place to set grams_from_3mf from per-slot 3MF data.
        
        Uses mapping_array as source of truth for filament-to-physical-slot translation.
        Falls back to heuristic if mapping_array is unavailable.
        """
        if not per_slot_usage:
            return

        single_3mf_used_g = None
        single_used_slot_index = None
        if len(per_slot_usage) == 1:
            single_3mf_used_g = next(iter(per_slot_usage.values()))
            used_entries = [e for e in entries if e.get("was_used")]
            dropped_entries = [e for e in entries if e.get("had_drop")]
            if len(used_entries) == 1:
                single_used_slot_index = used_entries[0].get("slot_index")
            elif len(dropped_entries) == 1:
                single_used_slot_index = dropped_entries[0].get("slot_index")
            elif len(entries) == 1:
                single_used_slot_index = entries[0].get("slot_index")

            if single_used_slot_index:
                logger.info(
                    "Per-slot/layer scaling: single-filament fallback active (3mf_used_g=%.3f, used_slot=%s)",
                    single_3mf_used_g,
                    single_used_slot_index,
                )

        for entry in entries:
            slot_index = entry.get("slot_index")
            if not slot_index:
                continue
            try:
                ams_id, tray_id = (int(p) for p in slot_index.split("-", 1))
                
                # Try to find filament id using mapping array (source of truth)
                slot_id = None
                mapping_source = "heuristic"
                if mapping_array:
                    slot_id = Driver._find_filament_id_from_mapping(
                        ams_id, tray_id, mapping_array
                    )
                    if slot_id is not None:
                        mapping_source = "mapping_array"
                
                # Fallback to heuristic only when no mapping array is available.
                # If mapping_array is present, it is the source of truth; mixing
                # mapping_array + heuristic can duplicate single-filament usage
                # across multiple physical slots.
                if slot_id is None and not mapping_array:
                    slot_id = Driver._map_slot_index_to_3mf_id(ams_id, tray_id)
                
                if slot_id is not None and slot_id in per_slot_usage:
                    used_g = per_slot_usage[slot_id]
                    if end_state == "completed":
                        entry["grams_from_3mf"] = used_g
                        logger.debug(
                            "Per-slot/layer scaling: slot_index=%s (slot_id=%d, source=%s) â†’ full 3MF weight %.3f grams",
                            slot_index,
                            slot_id,
                            mapping_source,
                            used_g,
                        )
                    elif layer_grams_per_slot and slot_id in layer_grams_per_slot:
                        entry["grams_from_3mf"] = layer_grams_per_slot[slot_id]
                        logger.info(
                            "Per-slot/layer scaling: slot_index=%s (slot_id=%d, source=%s) â†’ layer-scaled weight %.3f grams (partial print)",
                            slot_index,
                            slot_id,
                            mapping_source,
                            layer_grams_per_slot[slot_id],
                        )
                    else:
                        scale = max(0.0, min(progress_delta / 100.0, 1.0))
                        entry["grams_from_3mf"] = used_g * scale
                        logger.info(
                            "Per-slot/layer scaling: slot_index=%s (slot_id=%d, source=%s) â†’ linear-scaled weight %.3f grams (scale=%.1f%%, no layer data)",
                            slot_index,
                            slot_id,
                            mapping_source,
                            used_g * scale,
                            scale * 100,
                        )
                    entry["source_3mf"] = True
                else:
                    if (
                        single_3mf_used_g is not None
                        and single_used_slot_index
                        and slot_index == single_used_slot_index
                    ):
                        if end_state == "completed":
                            entry["grams_from_3mf"] = single_3mf_used_g
                        else:
                            scale = max(0.0, min(progress_delta / 100.0, 1.0))
                            entry["grams_from_3mf"] = single_3mf_used_g * scale
                        entry["source_3mf"] = True
                        logger.info(
                            "Per-slot/layer scaling: slot_index=%s â†’ single-filament fallback weight %.3f grams",
                            slot_index,
                            entry["grams_from_3mf"],
                        )
                    else:
                        logger.debug(
                            "Per-slot/layer scaling: slot_index=%s (slot_id=%s) - slot_id not in per_slot_usage dict",
                            slot_index,
                            slot_id,
                        )
            except (ValueError, TypeError) as e:
                logger.warning(
                    "Per-slot/layer scaling: failed to map slot_index=%s: %s",
                    slot_index,
                    e,
                )

    @staticmethod
    def _resolve_entry_grams(entry: dict) -> tuple[float, str] | None:
        """Return (grams, source) for recording, or None to skip this entry."""
        slot_index = entry.get("slot_index")

        grams = entry.get("grams_from_3mf")
        source = "bambulab_measured_3mf"

        if grams is not None and grams > 0:
            logger.info(
                "Consumption recording: slot_index=%s - using 3MF per-slot grams %.3f",
                slot_index,
                grams,
            )
        else:
            logger.info(
                "Consumption recording: slot_index=%s - skipped: no usable 3MF grams for slot",
                slot_index,
            )
            return None

        if grams is None or grams <= 0:
            logger.warning(
                "Consumption recording: slot_index=%s - skipped: final grams check failed (grams=%.3f)",
                slot_index,
                grams or 0.0,
            )
            return None

        if grams < 1.0:
            logger.info(
                "Consumption recording: slot_index=%s - rounding up %.3f g to 1.0 g (minimum threshold)",
                slot_index,
                grams,
            )
            grams = 1.0

        return grams, source

    @staticmethod
    def _failure_layer_suffix(end_state: str, max_layer_num: int | None) -> str:
        """Return a spool-log suffix labelling a failed print with its layer.

        Completed prints (``FINISH``) get no suffix; anything else (``FAILED``,
        cancelled, etc.) is labelled ``", failed at layer N"`` when the layer is
        known.
        """
        if (end_state or "").upper() != "FINISH" and max_layer_num is not None:
            return f", failed at layer {max_layer_num}"
        return ""

    @staticmethod
    def _should_record_zero_failure_for_entry(entry: dict) -> bool:
        """Return True when a 0g 3MF failure event should hit this spool's log.

        A failed print only consumes filament from slots that were actually
        active: reported as used via the MQTT mapping (``was_used``) or showing
        a physical remain drop (``had_drop``). Writing 0g failure events to
        every mapped spool would spam the spool log, so inactive slots are
        skipped (and only debug-logged) instead.
        """
        return bool(entry.get("was_used") or entry.get("had_drop"))

    @staticmethod
    def _should_record_3mf_failure_zero_events(
        three_mf_fail_reason: str | None,
        final_progress: float | None,
    ) -> bool:
        """Return True when 0g 3MF failure events should be written to spools.

        We only write these failure events when the print has actually progressed
        beyond 0%%. A failed/cancelled job at 0%% should not create spool events.
        """
        if three_mf_fail_reason is None:
            return False

        try:
            progress = float(final_progress or 0.0)
        except (ValueError, TypeError):
            progress = 0.0

        return progress > 0.0

    async def _finalize_consumption_tracking(self, end_state: str) -> None:
        if self._tracking_finalize_in_progress:
            logger.info(
                "Print tracking finalize skipped for printer %s: finalize already in progress (state=%s)",
                self.printer_id,
                end_state,
            )
            return
        self._tracking_finalize_in_progress = True

        started_at = self._tracking_started_at
        progress_delta = max(0.0, self._tracking_progress_delta)
        final_progress_snapshot = self._current_print_progress
        slots_snapshot = dict(self._tracking_slots)
        consumed_slots_snapshot = set(self._tracking_consumed_slots)
        used_slots_snapshot = set(self._tracking_used_slots)
        mapping_array_snapshot = list(self._tracking_mapping_array) if self._tracking_mapping_array else None
        max_layer_num_snapshot = self._tracking_max_layer_num
        total_layer_num_snapshot = self._tracking_total_layer_num
        print_context_snapshot = dict(self._tracking_print_context)
        finalize_dedupe_key = self._build_finalize_dedupe_key(
            print_context_snapshot,
            started_at,
        )
        finalize_marker_path = self._build_finalize_dedupe_marker_path(finalize_dedupe_key)
        prefetched_total_grams_snapshot = self._tracking_prefetched_total_grams
        prefetched_3mf_path_snapshot = self._tracking_prefetched_3mf_path
        finished_at = datetime.now(timezone.utc)

        # Reset before DB work to avoid duplicate finalize processing.
        self._tracking_active = False
        self._tracking_started_at = None
        self._tracking_last_progress = 0.0
        self._tracking_progress_delta = 0.0
        self._tracking_slots = {}
        self._tracking_consumed_slots = set()
        self._tracking_used_slots = set()
        self._tracking_mapping_array = None
        self._tracking_max_layer_num = None
        self._tracking_total_layer_num = None
        self._tracking_print_context = {}
        if self._tracking_prefetch_task and not self._tracking_prefetch_task.done():
            self._tracking_prefetch_task.cancel()
        self._tracking_prefetch_task = None
        self._tracking_prefetched_total_grams = None
        self._tracking_prefetched_3mf_path = None
        self._tracking_prefetch_in_flight = False
        self._tracking_prefetch_generation += 1

        self.log_debug(
            "event",
            "consumption_tracking",
            {
                "event": "finalize_requested",
                "printer_id": self.printer_id,
                "state": end_state,
                "started_at": started_at.isoformat() if started_at else None,
                "finished_at": finished_at.isoformat(),
                "progress_delta": progress_delta,
                "slot_count": len(slots_snapshot),
            },
        )
        logger.info(
            "Print tracking finalize requested for printer %s: state=%s progress_delta=%.2f slots=%s",
            self.printer_id,
            end_state,
            progress_delta,
            len(slots_snapshot),
        )
        logger.info(
            "Finalize diagnostics for printer %s: instance=%s pid=%s key=%s marker=%s job_id=%s lan_task_id=%s model_id=%s gcode_file=%s used_slots=%s mapping=%s",
            self.printer_id,
            hex(id(self)),
            os.getpid(),
            finalize_dedupe_key,
            finalize_marker_path,
            print_context_snapshot.get("job_id"),
            print_context_snapshot.get("lan_task_id"),
            print_context_snapshot.get("model_id"),
            print_context_snapshot.get("gcode_file"),
            sorted(used_slots_snapshot),
            mapping_array_snapshot,
        )

        if not slots_snapshot:
            self._tracking_last_result = {
                "status": "skipped",
                "reason": "no_slots",
                "finished_at": finished_at.isoformat(),
                "state": end_state,
            }
            self.log_debug(
                "event",
                "consumption_tracking",
                {
                    "event": "finalize_skipped",
                    "printer_id": self.printer_id,
                    "reason": "no_slots",
                    "state": end_state,
                },
            )
            logger.warning(
                "Consumption finalize skipped for printer %s: no tracked slots (state=%s)",
                self.printer_id,
                end_state,
            )
            return

        if not self._try_acquire_finalize_dedupe_marker(finalize_dedupe_key):
            self._tracking_last_result = {
                "status": "skipped",
                "reason": "dedupe_marker_exists",
                "finished_at": finished_at.isoformat(),
                "state": end_state,
                "dedupe_key": finalize_dedupe_key,
            }
            logger.info(
                "Consumption finalize deduped for printer %s: state=%s key=%s",
                self.printer_id,
                end_state,
                finalize_dedupe_key,
            )
            self.log_debug(
                "event",
                "consumption_tracking",
                {
                    "event": "finalize_deduped",
                    "printer_id": self.printer_id,
                    "state": end_state,
                    "dedupe_key": finalize_dedupe_key,
                },
            )
            return

        try:
            async with async_session_maker() as db:
                entries: list[dict[str, Any]] = []
                unresolved = 0

                for slot_index, slot_ctx in slots_snapshot.items():
                    try:
                        ams_id, tray_id = (int(p) for p in slot_index.split("-", 1))
                    except (ValueError, TypeError):
                        unresolved += 1
                        continue

                    slot_location_name = self._generate_slot_location_name(ams_id, tray_id)
                    location = await self._resolve_slot_location(
                        db,
                        ams_id,
                        tray_id,
                        create_if_missing=False,
                    )
                    if not location:
                        unresolved += 1
                        logger.warning(
                            "Consumption tracking skipped for slot %s: location '%s' not found",
                            slot_index,
                            slot_location_name,
                        )
                        self.log_debug(
                            "event",
                            "consumption_tracking",
                            {
                                "event": "slot_skipped",
                                "printer_id": self.printer_id,
                                "slot_index": slot_index,
                                "reason": "location_not_found",
                                "location": slot_location_name,
                            },
                        )
                        continue

                    spool_result = await db.execute(
                        select(Spool)
                        .where(Spool.location_id == location.id)
                        .options(selectinload(Spool.status))
                    )
                    mapped_spools = spool_result.scalars().all()
                    if not mapped_spools:
                        unresolved += 1
                        logger.warning(
                            "Consumption tracking skipped for slot %s: no spool at location '%s'",
                            slot_index,
                            slot_location_name,
                        )
                        self.log_debug(
                            "event",
                            "consumption_tracking",
                            {
                                "event": "slot_skipped",
                                "printer_id": self.printer_id,
                                "slot_index": slot_index,
                                "reason": "no_spool_at_location",
                                "location": slot_location_name,
                            },
                        )
                        continue

                    spool = mapped_spools[0]
                    start_remain = self._parse_remain_value(slot_ctx.get("start_remain"))
                    end_remain = self._parse_remain_value(slot_ctx.get("end_remain"))
                    measured_grams: float | None = None

                    if (
                        start_remain is not None
                        and end_remain is not None
                        and start_remain > end_remain
                        and spool.remaining_weight_g is not None
                    ):
                        remain_delta_pct = max(0.0, min(100.0, start_remain - end_remain))
                        basis_pct = end_remain if end_remain > 0 else start_remain
                        if basis_pct > 0:
                            estimated_total = spool.remaining_weight_g / (basis_pct / 100.0)
                            measured_grams = max(
                                0.0,
                                estimated_total * (remain_delta_pct / 100.0),
                            )

                    entries.append(
                        {
                            "slot_index": slot_index,
                            "spool_id": spool.id,
                            "spool": spool,
                            "start_remain": start_remain,
                            "end_remain": end_remain,
                            "grams": measured_grams,
                            "had_drop": slot_index in consumed_slots_snapshot,
                            "was_used": slot_index in used_slots_snapshot,
                            "has_remain_sensor": bool(
                                slot_ctx.get("has_remain_sensor", False)
                            ),
                        }
                    )

                if not entries:
                    self._tracking_last_result = {
                        "status": "skipped",
                        "reason": "no_mapped_spools",
                        "finished_at": finished_at.isoformat(),
                        "state": end_state,
                        "progress_delta": progress_delta,
                    }
                    self.log_debug(
                        "event",
                        "consumption_tracking",
                        {
                            "event": "finalize_skipped",
                            "printer_id": self.printer_id,
                            "reason": "no_mapped_spools",
                            "state": end_state,
                            "progress_delta": progress_delta,
                            "slots_unresolved": unresolved,
                        },
                    )
                    logger.warning(
                        "Consumption finalize skipped for printer %s: no mapped spools (state=%s unresolved=%s)",
                        self.printer_id,
                        end_state,
                        unresolved,
                    )
                    return

                threemf_3mf_path = await self._acquire_3mf_path(
                    prefetched_3mf_path_snapshot,
                    print_context_snapshot,
                )

                per_slot_usage: dict[int, float] | None = None
                layer_grams_per_slot: dict[int, float] | None = None
                per_slot_data: list[dict] | None = None
                if threemf_3mf_path:
                    per_slot_usage, layer_grams_per_slot, per_slot_data = (
                        await self._extract_3mf_usage(
                            threemf_3mf_path,
                            end_state,
                            progress_delta,
                            max_layer_num_snapshot,
                            total_layer_num_snapshot,
                        )
                    )
                else:
                    logger.warning(
                        "Per-slot/layer extraction: skipped (no 3MF file path available)"
                    )

                self._apply_3mf_grams_to_entries(
                    entries,
                    per_slot_usage,
                    layer_grams_per_slot,
                    per_slot_data,
                    mapping_array_snapshot,
                    end_state,
                    progress_delta,
                )

                resolveable_3mf_entries = [
                    e for e in entries if float(e.get("grams_from_3mf") or 0.0) > 0
                ]

                three_mf_fail_reason = None
                if threemf_3mf_path is None:
                    three_mf_fail_reason = "3mf_unavailable"
                elif not per_slot_data:
                    three_mf_fail_reason = "3mf_parse_failed"
                elif not per_slot_usage:
                    three_mf_fail_reason = "3mf_no_per_slot_usage"
                elif not resolveable_3mf_entries:
                    three_mf_fail_reason = "3mf_no_mapped_slot_usage"

                self.log_debug(
                    "event",
                    "consumption_tracking",
                    {
                        "event": "finalize_computed",
                        "printer_id": self.printer_id,
                        "state": end_state,
                        "entries": len(entries),
                        "used_slots": len(used_slots_snapshot),
                        "slots_unresolved": unresolved,
                        "resolved_3mf_entries": len(resolveable_3mf_entries),
                        "three_mf_fail_reason": three_mf_fail_reason,
                    },
                )
                logger.info(
                    "Consumption finalize computed for printer %s: entries=%s resolved_3mf_entries=%s used_slots=%s unresolved=%s 3mf_fail_reason=%s final_progress=%s",
                    self.printer_id,
                    len(entries),
                    len(resolveable_3mf_entries),
                    len(used_slots_snapshot),
                    unresolved,
                    three_mf_fail_reason,
                    final_progress_snapshot,
                )

                service = SpoolService(db)
                recorded_events = 0

                should_record_zero_failures = self._should_record_3mf_failure_zero_events(
                    three_mf_fail_reason,
                    final_progress_snapshot,
                )

                failed_at_layer_suffix = self._failure_layer_suffix(
                    end_state,
                    max_layer_num_snapshot,
                )

                if three_mf_fail_reason is not None and should_record_zero_failures:
                    zero_fail_events = 0
                    for entry in entries:
                        if not self._should_record_zero_failure_for_entry(entry):
                            logger.debug(
                                "Consumption finalize zero-failure skipped for printer %s slot=%s spool=%s: no active assignment (reason=%s)",
                                self.printer_id,
                                entry["slot_index"],
                                entry["spool_id"],
                                three_mf_fail_reason,
                            )
                            self.log_debug(
                                "event",
                                "consumption_tracking",
                                {
                                    "event": "recorded_zero_failure_skipped",
                                    "printer_id": self.printer_id,
                                    "slot_index": entry["slot_index"],
                                    "spool_id": entry["spool_id"],
                                    "source": "bambulab_3mf_failed",
                                    "reason": three_mf_fail_reason,
                                },
                            )
                            continue

                        printer_name = self._printer_name or f"Printer {self.printer_id}"
                        await service.record_consumption(
                            entry["spool"],
                            delta_weight_g=0.0,
                            event_at=finished_at,
                            source="bambulab_3mf_failed",
                            note=(
                                f"Bambu print consumption failed [{printer_name}] ({entry['slot_index']}, "
                                f"reason={three_mf_fail_reason}, unresolved={unresolved}, "
                                f"used_slots={len(used_slots_snapshot)}{failed_at_layer_suffix})"
                            ),
                        )
                        zero_fail_events += 1
                        self.log_debug(
                            "event",
                            "consumption_tracking",
                            {
                                "event": "recorded_zero_failure",
                                "printer_id": self.printer_id,
                                "slot_index": entry["slot_index"],
                                "spool_id": entry["spool_id"],
                                "source": "bambulab_3mf_failed",
                                "reason": three_mf_fail_reason,
                            },
                        )

                    recorded_events = zero_fail_events
                elif three_mf_fail_reason is not None:
                    logger.info(
                        "Consumption finalize 3MF failure events suppressed for printer %s: reason=%s final_progress=%s",
                        self.printer_id,
                        three_mf_fail_reason,
                        final_progress_snapshot,
                    )
                    self.log_debug(
                        "event",
                        "consumption_tracking",
                        {
                            "event": "record_zero_failure_suppressed",
                            "printer_id": self.printer_id,
                            "reason": three_mf_fail_reason,
                            "final_progress": final_progress_snapshot,
                        },
                    )
                else:
                    for entry in entries:
                        result = self._resolve_entry_grams(entry)
                        if result is None:
                            self.log_debug(
                                "event",
                                "consumption_tracking",
                                {
                                    "event": "record_skipped",
                                    "printer_id": self.printer_id,
                                    "slot_index": entry["slot_index"],
                                },
                            )
                            continue
                        grams, source = result

                        printer_name = self._printer_name or f"Printer {self.printer_id}"
                        await service.record_consumption(
                            entry["spool"],
                            delta_weight_g=grams,
                            event_at=finished_at,
                            source=source,
                            note=(
                                f"Bambu print consumption [{printer_name}] ({entry['slot_index']}, "
                                f"source={source}{failed_at_layer_suffix})"
                            ),
                        )
                        recorded_events += 1
                        self.log_debug(
                            "event",
                            "consumption_tracking",
                            {
                                "event": "recorded",
                                "printer_id": self.printer_id,
                                "slot_index": entry["slot_index"],
                                "spool_id": entry["spool_id"],
                                "grams": grams,
                                "source": source,
                            },
                        )
                        logger.info(
                            "Consumption recorded for printer %s: spool=%s slot=%s grams=%.3f source=%s",
                            self.printer_id,
                            entry["spool_id"],
                            entry["slot_index"],
                            grams,
                            source,
                        )

                # Clean up downloaded 3MF temp file
                if threemf_3mf_path and os.path.exists(threemf_3mf_path):
                    try:
                        os.remove(threemf_3mf_path)
                        logger.info(
                            "Cleaned up 3MF temp file for printer %s: %s",
                            self.printer_id,
                            threemf_3mf_path,
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to clean up 3MF temp file for printer %s: %s",
                            self.printer_id,
                            e,
                        )

                self._tracking_last_result = {
                    "status": "ok",
                    "finished_at": finished_at.isoformat(),
                    "state": end_state,
                    "started_at": started_at.isoformat() if started_at else None,
                    "progress_delta": progress_delta,
                    "recorded_events": recorded_events,
                    "slots_considered": len(entries),
                    "slots_unresolved": unresolved,
                    "three_mf_fail_reason": three_mf_fail_reason,
                    "final_progress": final_progress_snapshot,
                }
                self.log_debug(
                    "event",
                    "consumption_tracking",
                    {
                        "event": "finalize_done",
                        "printer_id": self.printer_id,
                        "result": self._tracking_last_result,
                    },
                )
                logger.info(
                    "Consumption finalize done for printer %s: recorded_events=%s slots_considered=%s unresolved=%s progress_delta=%.2f",
                    self.printer_id,
                    recorded_events,
                    len(entries),
                    unresolved,
                    progress_delta,
                )
        except Exception as e:
            self._tracking_last_result = {
                "status": "error",
                "finished_at": finished_at.isoformat(),
                "state": end_state,
                "error": str(e),
            }
            logger.warning(
                "Consumption tracking finalize failed for printer %s: %s",
                self.printer_id,
                e,
            )
            self.log_debug(
                "event",
                "consumption_tracking",
                {
                    "event": "finalize_error",
                    "printer_id": self.printer_id,
                    "state": end_state,
                    "error": str(e),
                },
            )
        finally:
            self._tracking_finalize_queued = False
            self._tracking_finalize_in_progress = False

    def _process_print_tracking(self, payload: dict) -> None:
        print_data = payload.get("print", {})
        if not isinstance(print_data, dict):
            return

        state = str(print_data.get("gcode_state", "")).upper()
        progress = self._parse_progress(print_data)
        is_active = state in ("RUNNING", "PAUSE")
        self._current_print_state = state
        self._current_print_progress = progress

        if not self._consumption_tracking_enabled:
            return

        if state != self._tracking_last_print_state:
            previous_state = self._tracking_last_print_state
            self.log_debug(
                "event",
                "consumption_tracking",
                {
                    "event": "state_changed",
                    "printer_id": self.printer_id,
                    "previous_state": previous_state,
                    "state": state,
                    "progress": progress,
                    "tracking_active": self._tracking_active,
                },
            )
            logger.info(
                "Print state changed for printer %s: %s -> %s (progress=%s tracking_active=%s)",
                self.printer_id,
                previous_state,
                state,
                progress,
                self._tracking_active,
            )
            self._tracking_last_print_state = state

        if is_active and not self._tracking_active:
            self._start_consumption_tracking(progress, print_data)

        if self._tracking_active and is_active:
            self._update_tracking_print_context(print_data)
            self._update_tracking_layer_numbers(print_data)
            if progress is not None and progress > self._tracking_last_progress:
                self._tracking_progress_delta += progress - self._tracking_last_progress
                self._tracking_last_progress = progress
            elif progress is not None:
                self._tracking_last_progress = progress
            self._update_consumption_tracking_usage(print_data)
            self._update_consumption_tracking_slots()

        if (
            self._tracking_active
            and not is_active
            and state
            and self._loop
            and not self._tracking_finalize_queued
        ):
            self._tracking_finalize_queued = True
            logger.info(
                "Print tracking end detected for printer %s: state=%s progress_delta=%.2f slots=%s",
                self.printer_id,
                state,
                self._tracking_progress_delta,
                len(self._tracking_slots),
            )
            self.log_debug(
                "event",
                "consumption_tracking",
                {
                    "event": "finalize_queued",
                    "printer_id": self.printer_id,
                    "state": state,
                    "progress_delta": self._tracking_progress_delta,
                    "slot_count": len(self._tracking_slots),
                },
            )
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._finalize_consumption_tracking(state))
            )
        elif self._tracking_active and not is_active and state and self._tracking_finalize_queued:
            logger.debug(
                "Print tracking finalize already queued for printer %s: state=%s",
                self.printer_id,
                state,
            )

    # -- paho-Thread Callbacks ------------------------------------------------

    def _on_connect(self, mqtt_client, client, userdata, flags, rc, properties):
        """Wird im paho-Thread aufgerufen wenn MQTT verbunden ist."""
        self._connected = True
        self._last_connected_at = time.time()
        self._last_disconnected_at = None
        self._last_disconnect_rc = None
        self._set_write_mode(
            "readonly", "driver", "forced_readonly", emit_if_changed=True
        )
        logger.info(
            f"Bambu driver connected to printer {self.printer_id} at {self._host}"
        )
        self.log_debug("event", "mqtt", {"event": "connected", "rc": str(rc)})

    def _on_disconnect(
        self, mqtt_client, client, userdata, disconnect_flags, rc, properties
    ):
        """Wird im paho-Thread aufgerufen wenn MQTT getrennt wird."""
        self._connected = False
        self._last_disconnected_at = time.time()
        self._last_disconnect_rc = str(rc)
        self._current_print_state = ""
        self._current_print_progress = None
        self._set_write_mode(
            "readonly", "driver", "forced_readonly", emit_if_changed=True
        )
        self._current_slots = []  # Force full re-sync on reconnect
        logger.warning(
            f"Bambu driver disconnected from printer {self.printer_id}: {rc}"
        )
        self.log_debug("event", "mqtt", {"event": "disconnected", "rc": str(rc)})
        # paho auto-reconnect via loop_start() (reconnect_on_failure=True)

    def _on_message(self, mqtt_client, client, userdata, msg):
        """Wird im paho-Thread fÃ¼r jede MQTT-Nachricht aufgerufen."""
        try:
            self._last_mqtt_message_at = time.time()
            payload = json.loads(msg.payload.decode())
            self.log_debug("in", str(msg.topic), payload)

            # push_status Nachrichten verarbeiten
            if payload.get("print", {}).get("command") == "push_status":
                self._process_slots(payload)
                self._process_print_tracking(payload)
                return

            # get_version / push_info: AMS Seriennummern extrahieren
            info_cmd = payload.get("info", {}).get("command", "")
            if info_cmd in ("get_version", "push_info"):
                self._process_version_info(payload)
                return

        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode MQTT message: {e}")
        except Exception as e:
            logger.error(f"Error handling MQTT message: {e}")

    def _set_write_mode(
        self,
        mode: str,
        source: str,
        reason: str,
        emit_if_changed: bool = True,
    ) -> None:
        with self._capability_lock:
            old_mode = self._write_mode
            old_source = self._write_mode_source
            old_reason = self._write_mode_reason
            self._write_mode = mode
            self._write_mode_source = source
            self._write_mode_reason = reason
            self._write_mode_checked_at = time.time()

        changed = (
            old_mode != mode
            or old_source != source
            or old_reason != reason
        )
        if not emit_if_changed or not changed:
            return

        logger.info(
            "Write capability updated for printer %s: mode=%s source=%s reason=%s",
            self.printer_id,
            mode,
            source,
            reason,
        )
        self.log_debug(
            "event",
            "write_mode",
            {
                "mode": mode,
                "source": source,
                "reason": reason,
            },
        )


    def _log_write_blocked(self, action: str) -> None:
        with self._capability_lock:
            mode = self._write_mode
            source = self._write_mode_source
            reason = self._write_mode_reason
        logger.info(
            "Blocked write action '%s' for printer %s: mode=%s source=%s reason=%s",
            action,
            self.printer_id,
            mode,
            source,
            reason,
        )
        self.log_debug(
            "event",
            "write_blocked",
            {
                "action": action,
                "mode": mode,
                "source": source,
                "reason": reason,
            },
        )

    def _process_version_info(self, payload: dict) -> None:
        """AMS Seriennummern aus get_version/push_info Antwort extrahieren."""
        modules = payload.get("info", {}).get("module", [])
        for module in modules:
            name = module.get("name", "")
            if name.startswith("ams/"):
                ams_id = name.split("/")[1]
                sn = module.get("sn", "")
                if sn:
                    self._ams_serials[ams_id] = sn
                    logger.debug(f"AMS {ams_id} serial: {sn}")

    # -- Slot-Verarbeitung (paho-Thread) --------------------------------------

    def _process_slots(self, payload: dict) -> None:
        """AMS/Tray-Daten aus push_status extrahieren und slots_update emittieren.
        Wird bei jeder push_status Nachricht aufgerufen. Emittiert nur wenn sich
        die Slot-Daten geÃ¤ndert haben, um unnÃ¶tige DB-Writes zu vermeiden.

        Merge-Strategie: Nur Slot-Kategorien aktualisieren, die in der aktuellen
        Nachricht vorhanden sind. Fehlende Kategorien behalten ihren vorherigen Zustand.
        BambuLab sendet nicht immer alle Daten in jeder push_status Nachricht.

        Auto-assignment nutzt ausschlieÃŸlich Feld-Vergleich (wie C++ Referenz):
        Erkennt Ã„nderungen in tray_info_idx, tray_type, tray_color, cali_idx, setting_id."""

        print_data = payload.get("print", {})
        ams_section = print_data.get("ams")
        external_tray = self._select_external_tray_data(print_data)
        device_data = print_data.get("device") if self._has_toolheads else None

        # Nur verarbeiten wenn AMS-, External-Tray- oder Toolhead-Daten vorhanden
        if ams_section is None and external_tray is None and device_data is None:
            return

        ams_data = (ams_section or {}).get("ams", [])

        # Leichtgewichtige Nachricht (nur tray_now/version) — keine Slot-Daten vorhanden
        if not ams_data and external_tray is None and device_data is None:
            return

        # -- Merge-Strategie: vorherige Slots als Basis, nur vorhandene Daten aktualisieren --
        # BambuLab sendet nicht immer ams UND External-Tray-Daten in jeder push_status Nachricht.
        # Ohne Merge würde das Fehlen einer Kategorie deren Slots löschen (Flicker).
        prev_ams_slots = [
            s
            for s in self._current_slots
            if not s.get("slot_index", "").startswith("255-")
            and not s.get("slot_index", "").startswith("tool-")
        ]
        prev_toolhead_slots = [
            s
            for s in self._current_slots
            if s.get("slot_index", "").startswith("tool-")
        ]
        prev_ext_slots = [
            s for s in self._current_slots if s.get("slot_index", "").startswith("255-")
        ]

        # AMS-Einheiten Metadaten â€” nur aktualisieren wenn AMS-Daten vorhanden
        if ams_data:
            ams_units: list[dict[str, Any]] = []
            for ams_unit in ams_data:
                ams_id = int(ams_unit.get("id", 0))
                ams_units.append(
                    {
                        "ams_id": ams_id,
                        "humidity": ams_unit.get(
                            "humidity_raw", ams_unit.get("humidity")
                        ),
                        "temp": ams_unit.get("temp"),
                        "tray_count": len(ams_unit.get("tray", [])),
                        "serial": self._ams_serials.get(str(ams_id), None),
                    }
                )
            self._current_ams_units = ams_units
        else:
            ams_units = list(self._current_ams_units)

        # AMS-Trays: nur aktualisieren wenn ams_data vorhanden, sonst vorherige beibehalten
        if ams_data:
            ams_slots: list[dict[str, Any]] = []
            for ams_unit in ams_data:
                ams_id = int(ams_unit.get("id", 0))
                trays = ams_unit.get("tray", [])

                for tray in trays:
                    tray_id = int(tray.get("id", 0))
                    slot_index = f"{ams_id}-{tray_id}"
                    tray_type = tray.get("tray_type", "")

                    if self._is_ams_lite:
                        slot_name = f"AMS Lite - Slot {tray_id + 1}"
                    else:
                        slot_name = f"AMS {ams_id + 1} - Slot {tray_id + 1}"

                    present = bool(tray_type)
                    ams_slots.append(
                        {
                            "slot_index": slot_index,
                            "slot_name": slot_name,
                            "slot_kind": "tray",
                            "tray_info_idx": tray.get("tray_info_idx", ""),
                            "tray_type": tray_type,
                            "tray_color": tray.get("tray_color", ""),
                            "remain": tray.get("remain"),
                            "nozzle_temp_min": tray.get("nozzle_temp_min"),
                            "nozzle_temp_max": tray.get("nozzle_temp_max"),
                            "setting_id": tray.get("setting_id", ""),
                            "cali_idx": tray.get("cali_idx"),
                            "present": present,
                        }
                    )
        else:
            ams_slots = prev_ams_slots

        # H2D Toolhead-Slots: aus device.nozzle.info[] + device.extruder.info[]
        if self._has_toolheads and isinstance(device_data, dict):
            toolhead_slots = self._parse_toolhead_slots(device_data)
        else:
            toolhead_slots = prev_toolhead_slots

        # Externe Spule: nur aktualisieren wenn externe Daten vorhanden, sonst vorherige beibehalten
        if external_tray is not None:
            ext_has_filament = bool(external_tray.get("tray_type"))
            ext_slots = [
                {
                    "slot_index": "255-254",
                    "slot_name": "External Tray",
                    "slot_kind": "external",
                    "tray_info_idx": external_tray.get("tray_info_idx", ""),
                    "tray_type": external_tray.get("tray_type", ""),
                    "tray_color": external_tray.get("tray_color", ""),
                    "remain": external_tray.get("remain"),
                    "nozzle_temp_min": external_tray.get("nozzle_temp_min"),
                    "nozzle_temp_max": external_tray.get("nozzle_temp_max"),
                    "setting_id": external_tray.get("setting_id", ""),
                    "cali_idx": external_tray.get("cali_idx"),
                    "present": ext_has_filament,
                }
            ]
        else:
            ext_slots = prev_ext_slots

        # Zusammenführen: Toolhead-Slots + AMS-Slots + External Slot
        slots = toolhead_slots + ams_slots + ext_slots
        self._apply_virtual_slot_overrides(slots)
        self._inject_slot_spool_ids(slots)
        has_external = len(ext_slots) > 0

        # -- Auto-assignment: Tray-Daten-Vergleich (wie C++ Implementierung) --
        # Erkennt wenn sich Tray-Felder Ã¤ndern (Spule eingelegt/gewechselt).
        # Vergleicht tray_info_idx, tray_type, tray_color, cali_idx und setting_id
        # gegen die zuletzt gespeicherten Slot-Daten.
        # Beibehaltene (unverÃ¤nderte) Slots matchen ihre VorgÃ¤nger â†’ kein false positive.
        if self._pending and self._current_slots:
            _compare_fields = ("tray_info_idx", "tray_type", "tray_color", "cali_idx")
            for new_slot in slots:
                sid = new_slot.get("slot_index", "")
                new_tray_type = new_slot.get("tray_type", "")
                if not new_tray_type:
                    continue  # Leerer Slot, kein Assignment mÃ¶glich
                # Passendes altes Slot finden
                old_slot = next(
                    (s for s in self._current_slots if s.get("slot_index") == sid), None
                )
                if old_slot is None:
                    continue  # Kein Vergleich mÃ¶glich (erster Sync)
                # Wenn alter Slot leer war (tray_type war leer), setting_id zurÃ¼cksetzen (wie C++)
                if not old_slot.get("tray_type", ""):
                    old_slot["setting_id"] = ""
                # setting_id null â†’ leerer String (wie C++: if (trayObj["setting_id"].isNull()) trayObj["setting_id"] = "")
                new_setting_id = new_slot.get("setting_id") or ""
                old_setting_id = old_slot.get("setting_id") or ""
                # PrÃ¼fe ob sich relevante Felder geÃ¤ndert haben
                has_changed = any(
                    new_slot.get(f, "") != old_slot.get(f, "") for f in _compare_fields
                )
                # setting_id: nur vergleichen wenn neuer Wert nicht leer ist (wie C++)
                if (
                    not has_changed
                    and new_setting_id
                    and new_setting_id != old_setting_id
                ):
                    has_changed = True
                if not has_changed:
                    continue
                # Slot-Filter: wenn Pending einen bestimmten Slot will
                if (
                    self._pending.slot_index is not None
                    and self._pending.slot_index != sid
                ):
                    continue
                # Parse ams_id und tray_id aus slot_index (z.B. "0-1" oder "255-254")
                try:
                    parts = sid.split("-")
                    ams_id_parsed, tray_id_parsed = int(parts[0]), int(parts[1])
                except (ValueError, IndexError):
                    continue
                logger.info(
                    f"Tray data changed at slot {sid}: "
                    f"assigning pending spool {self._pending.spool_id}"
                )
                self._apply_assignment_readonly(
                    ams_id_parsed,
                    tray_id_parsed,
                    self._pending.filament_data,
                    action="assign_pending_spool",
                    spool_id=self._pending.spool_id,
                )
                if self._pending.timer and self._loop:
                    self._loop.call_soon_threadsafe(self._pending.timer.cancel)
                self._pending = None
                break  # Nur erste Ã„nderung zuweisen

        # AMS/Slot Zusammenfassung
        total_slots = sum(u.get("tray_count", 0) for u in ams_units)
        if has_external:
            total_slots += 1
        if self._has_toolheads:
            total_slots += len(toolhead_slots)
        ams_info = {
            "ams_count": len(ams_units),
            "ams_type": "AMS Lite" if self._is_ams_lite else "AMS",
            "slot_count": total_slots,
            "external_spool": has_external,
            "ams_units": ams_units,
        }

        # Nur emittieren wenn sich Slot-Daten geÃ¤ndert haben
        if slots == self._current_slots:
            return

        # Delta-Tracking fÃ¼r readonly Filament-Tracking (Insert/Remove/Materialwechsel)
        slot_changes = self._compute_slot_changes(self._current_slots, slots)

        # Event an System melden (muss im asyncio-Thread passieren)
        self._current_slots = slots
        self._inject_slot_spool_ids(self._current_slots)
        logger.info(
            f"Slot data changed for printer {self.printer_id}, emitting slots_update"
        )
        if self._loop:
            self._loop.call_soon_threadsafe(
                self.emit,
                {"event_type": "slots_update", "slots": slots, "ams_info": ams_info},
            )
            if slot_changes:
                # Bei Slot-Entnahme Assignment in FilaMan auflÃ¶sen (plugin-only).
                if self._auto_unassign_on_remove:
                    for change in slot_changes:
                        if change.get("change_type") == "removed":
                            slot_index = str(change.get("slot_index", ""))
                            if slot_index:
                                self._loop.call_soon_threadsafe(
                                    lambda sid=slot_index: asyncio.create_task(
                                        self._clear_slot_assignment(sid)
                                    )
                                )
                self._loop.call_soon_threadsafe(
                    self.emit,
                    {
                        "event_type": "slot_tracking_update",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "changes": slot_changes,
                        "slots": slots,
                    },
                )

    def _compute_slot_changes(
        self, old_slots: list[dict[str, Any]], new_slots: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        old_by_index = {s.get("slot_index"): s for s in old_slots if s.get("slot_index")}
        changes: list[dict[str, Any]] = []

        for slot in new_slots:
            sid = slot.get("slot_index")
            if not sid:
                continue
            prev = old_by_index.get(sid)
            if not prev:
                continue

            prev_present = bool(prev.get("present"))
            curr_present = bool(slot.get("present"))

            if not prev_present and curr_present:
                changes.append(
                    {
                        "change_type": "inserted",
                        "slot_index": sid,
                        "slot_name": slot.get("slot_name", ""),
                        "tray_info_idx": slot.get("tray_info_idx", ""),
                        "tray_type": slot.get("tray_type", ""),
                        "tray_color": slot.get("tray_color", ""),
                        "present": True,
                    }
                )
                continue

            if prev_present and not curr_present:
                changes.append(
                    {
                        "change_type": "removed",
                        "slot_index": sid,
                        "slot_name": slot.get("slot_name", ""),
                        "present": False,
                    }
                )
                continue

            if prev_present and curr_present:
                compare_fields = (
                    "tray_info_idx",
                    "tray_type",
                    "tray_color",
                    "setting_id",
                    "cali_idx",
                )
                changed_fields = [
                    f for f in compare_fields if slot.get(f, "") != prev.get(f, "")
                ]
                if changed_fields:
                    changes.append(
                        {
                            "change_type": "material_changed",
                            "slot_index": sid,
                            "slot_name": slot.get("slot_name", ""),
                            "changed_fields": changed_fields,
                            "previous": {
                                "tray_info_idx": prev.get("tray_info_idx", ""),
                                "tray_type": prev.get("tray_type", ""),
                                "tray_color": prev.get("tray_color", ""),
                                "setting_id": prev.get("setting_id", ""),
                                "cali_idx": prev.get("cali_idx", ""),
                            },
                            "current": {
                                "tray_info_idx": slot.get("tray_info_idx", ""),
                                "tray_type": slot.get("tray_type", ""),
                                "tray_color": slot.get("tray_color", ""),
                                "setting_id": slot.get("setting_id", ""),
                                "cali_idx": slot.get("cali_idx", ""),
                            },
                        }
                    )

        return changes

    def _apply_virtual_slot_overrides(self, slots: list[dict[str, Any]]) -> None:
        """Apply short-lived readonly assignment overlays onto live slot data."""
        if not self._virtual_slot_overrides:
            return

        now = time.time()
        expired = [
            sid
            for sid, ov in self._virtual_slot_overrides.items()
            if ov.get("expires_at", 0) <= now
        ]
        for sid in expired:
            self._virtual_slot_overrides.pop(sid, None)

        if not self._virtual_slot_overrides:
            return

        for slot in slots:
            sid = slot.get("slot_index", "")
            if not sid:
                continue
            ov = self._virtual_slot_overrides.get(sid)
            if not ov:
                continue

            slot["tray_info_idx"] = ov.get("tray_info_idx", slot.get("tray_info_idx", ""))
            slot["tray_type"] = ov.get("tray_type", slot.get("tray_type", ""))
            slot["tray_color"] = ov.get("tray_color", slot.get("tray_color", ""))
            slot["nozzle_temp_min"] = ov.get("nozzle_temp_min", slot.get("nozzle_temp_min"))
            slot["nozzle_temp_max"] = ov.get("nozzle_temp_max", slot.get("nozzle_temp_max"))
            slot["present"] = True

    # -- Readonly assignment flow ---------------------------------------------

    @staticmethod
    def _extract_filaman_spool_id(filament_data: dict[str, Any] | None) -> int | None:
        if not filament_data:
            return None
        raw_spool_id = (
            filament_data.get("filaman_spool_id")
            or filament_data.get("id")
            or filament_data.get("spool_id")
        )
        if raw_spool_id in (None, ""):
            return None
        try:
            return int(raw_spool_id)
        except (TypeError, ValueError):
            logger.warning("Invalid spool_id in filament_data: %r", raw_spool_id)
            return None

    def _apply_assignment_readonly(
        self,
        ams_id: int,
        tray_id: int,
        filament_data: dict[str, Any] | None,
        action: str,
        spool_id: int | None = None,
    ) -> None:
        self._log_write_blocked(action)

        slot_index = f"{ams_id}-{tray_id}"
        existing_slot = next(
            (s for s in self._current_slots if s.get("slot_index") == slot_index),
            None,
        )
        overlay_data: dict[str, Any] = {
            "tray_info_idx": (existing_slot or {}).get("tray_info_idx", ""),
            "material_type": (existing_slot or {}).get("tray_type", ""),
            "color": (existing_slot or {}).get("tray_color", ""),
            "nozzle_temp_min": (existing_slot or {}).get("nozzle_temp_min"),
            "nozzle_temp_max": (existing_slot or {}).get("nozzle_temp_max"),
        }

        if filament_data:
            if "tray_info_idx" in filament_data:
                overlay_data["tray_info_idx"] = filament_data.get("tray_info_idx")
            if "material_type" in filament_data:
                overlay_data["material_type"] = filament_data.get("material_type")
            if "color" in filament_data:
                overlay_data["color"] = filament_data.get("color")
            if "nozzle_temp_min" in filament_data:
                overlay_data["nozzle_temp_min"] = filament_data.get("nozzle_temp_min")
            if "nozzle_temp_max" in filament_data:
                overlay_data["nozzle_temp_max"] = filament_data.get("nozzle_temp_max")

        self._set_slot_spool_id(slot_index, spool_id)
        self._apply_virtual_slot_assignment(
            ams_id,
            tray_id,
            overlay_data,
            spool_id=spool_id,
        )

        if spool_id and self._loop:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self._update_spool_location(spool_id, ams_id, tray_id)
                )
            )

    async def reconnect(self) -> None:
        """Reconnect: MQTT stoppen und neu starten."""
        logger.info(f"Reconnecting Bambu driver for printer {self.printer_id}")
        if self._printer:
            try:
                self._printer.mqtt_client._client.disconnect()
            except Exception:
                pass
            self._printer.mqtt_stop()
            self._connected = False
            self._current_slots = []  # Force full re-sync on reconnect
            self._slot_spool_ids = {}
            self._printer.mqtt_start()
            logger.info(f"Bambu driver reconnected for printer {self.printer_id}")

    def _set_slot_spool_id(self, slot_index: str, spool_id: int | None) -> None:
        if not slot_index:
            return
        if spool_id is None:
            self._slot_spool_ids.pop(slot_index, None)
            return
        try:
            parsed = int(spool_id)
        except (TypeError, ValueError):
            self._slot_spool_ids.pop(slot_index, None)
            return
        if parsed > 0:
            self._slot_spool_ids[slot_index] = parsed
        else:
            self._slot_spool_ids.pop(slot_index, None)

    def _inject_slot_spool_ids(self, slots: list[dict[str, Any]]) -> None:
        """Inject spool IDs only when assignment identity is known to this driver."""
        if not slots:
            return

        seen_slot_indexes: set[str] = set()
        for slot in slots:
            slot_index = str(slot.get("slot_index", ""))
            if not slot_index:
                continue
            seen_slot_indexes.add(slot_index)

            if not bool(slot.get("present")):
                self._slot_spool_ids.pop(slot_index, None)
                slot.pop("spool_id", None)
                continue

            spool_id = self._slot_spool_ids.get(slot_index)
            if spool_id is None:
                slot.pop("spool_id", None)
            else:
                slot["spool_id"] = spool_id

        stale = [sid for sid in self._slot_spool_ids if sid not in seen_slot_indexes]
        for sid in stale:
            self._slot_spool_ids.pop(sid, None)

    def _get_active_spool_id_snapshot(self) -> int | None:
        if not self._current_slots or not self._slot_spool_ids:
            return None

        active_spool_ids: set[int] = set()
        for slot in self._current_slots:
            slot_index = str(slot.get("slot_index", ""))
            if not slot_index or not bool(slot.get("present")):
                continue
            spool_id = self._slot_spool_ids.get(slot_index)
            if spool_id is not None:
                active_spool_ids.add(spool_id)

        if len(active_spool_ids) == 1:
            return next(iter(active_spool_ids))
        return None

    def refresh_status(self) -> dict[str, Any]:
        """Optional startup sync hook used by filaman-system PluginManager."""
        return {"active_spool_id": self._get_active_spool_id_snapshot()}

    def list_connected_models(self) -> dict[str, Any]:
        """Read-only driver action: the printer model(s) served by this driver.

        FilaMan's slicer profile picker calls this for every Bambu driver.
        A direct printer driver only knows its own configured model, so it
        reports that single model with this printer as its representative.
        """
        model = str(self._printer_model or "").strip()
        if not model:
            return {"models": [], "count": 0}
        return {
            "models": [
                {
                    "model": model,
                    "printer_ids": [self.printer_id],
                    "representative_printer_id": self.printer_id,
                }
            ],
            "count": 1,
        }

    def get_profile_coverage(
        self,
        spool_id: int | None = None,
        filament_id: int | None = None,
    ) -> dict[str, Any]:
        """Read-only driver action: per-model slicer profile coverage.

        Per-model cloud profiles are a Bambuddy feature. This direct Bambu
        driver has no cloud preset catalog, so there is nothing to resolve.
        Return a well-formed, empty payload so the FilaMan profile picker
        renders without erroring.
        """
        return {
            "spool_id": int(spool_id) if spool_id is not None else None,
            "filament_id": int(filament_id) if filament_id is not None else None,
            "default_base_name": "",
            "pending_display_name": False,
            "profiles_by_model": {},
            "per_model_profiles_enabled": False,
            "coverage": {},
        }

    def list_cloud_presets(
        self,
        force: bool = False,
        model: str | None = None,
        group: str | None = None,
    ) -> dict[str, Any]:
        """Read-only driver action: cloud slicer profile catalog.

        The direct Bambu driver does not expose a cloud preset catalog, so
        this always returns an empty list.
        """
        return {"presets": [], "count": 0}

    def send_filament_to_tray(
        self, ams_id: int, tray_id: int, filament_data: dict
    ) -> None:
        """Apply assignment in readonly-safe mode for compatibility."""
        self._apply_assignment_readonly(
            ams_id,
            tray_id,
            filament_data,
            action="send_filament_to_tray",
            spool_id=self._extract_filaman_spool_id(filament_data),
        )

    async def assign_spool_to_tray_readonly(
        self,
        spool_id: int,
        ams_id: int,
        tray_id: int,
    ) -> None:
        """DB-only Spool-zu-Slot-Zuweisung ohne Drucker-Schreibbefehl.

        Diese Action ist bewusst kompatibel mit filaman-system `driver/action`:
        Sie benÃ¶tigt kein `filament_data`, damit `spool_id` nicht vom API-Layer
        konsumiert wird.
        """
        self._apply_assignment_readonly(
            ams_id,
            tray_id,
            filament_data=None,
            action="assign_spool_to_tray_readonly",
            spool_id=spool_id,
        )

    async def assign_spool_to_slot_readonly(
        self,
        spool_id: int,
        ams_id: int,
        tray_id: int,
    ) -> None:
        """Alias für DB-only Spool-zu-Slot-Zuweisung."""
        self._apply_assignment_readonly(
            ams_id,
            tray_id,
            filament_data=None,
            action="assign_spool_to_slot_readonly",
            spool_id=spool_id,
        )

    def _emit_current_slots_update(self) -> None:
        if not self._loop:
            return

        self._inject_slot_spool_ids(self._current_slots)

        ext_exists = any(s.get("slot_index") == "255-254" for s in self._current_slots)
        tool_exists = any(s.get("slot_kind") == "toolhead" for s in self._current_slots)
        total_slots = sum(u.get("tray_count", 0) for u in self._current_ams_units)
        if ext_exists:
            total_slots += 1
        if tool_exists:
            total_slots += sum(1 for s in self._current_slots if s.get("slot_kind") == "toolhead")
        ams_info = {
            "ams_count": len(self._current_ams_units),
            "ams_type": "AMS Lite" if self._is_ams_lite else "AMS",
            "slot_count": total_slots,
            "external_spool": ext_exists,
            "ams_units": self._current_ams_units,
        }
        self._loop.call_soon_threadsafe(
            self.emit,
            {
                "event_type": "slots_update",
                "slots": self._current_slots,
                "ams_info": ams_info,
            },
        )

    def _apply_virtual_slot_assignment(
        self,
        ams_id: int,
        tray_id: int,
        filament_data: dict,
        spool_id: int | None = None,
    ) -> None:
        """Update slot state locally for readonly assignments.

        This keeps the existing filaman UI flow compatible (health polling expects
        tray_color/tray metadata change after assign) without requiring core changes.
        """
        slot_index = f"{ams_id}-{tray_id}"
        old_slots = [dict(s) for s in self._current_slots]

        slot = next(
            (s for s in self._current_slots if s.get("slot_index") == slot_index),
            None,
        )
        if slot is None:
            if ams_id >= 200:
                slot_name = "External Tray"
            elif self._is_ams_lite:
                slot_name = f"AMS Lite - Slot {tray_id + 1}"
            else:
                slot_name = f"AMS {ams_id + 1} - Slot {tray_id + 1}"
            slot = {
                "slot_index": slot_index,
                "slot_name": slot_name,
                "slot_kind": "external" if ams_id >= 200 else "tray",
                "tray_info_idx": "",
                "tray_type": "",
                "tray_color": "",
                "nozzle_temp_min": None,
                "nozzle_temp_max": None,
                "setting_id": "",
                "cali_idx": None,
                "present": False,
            }
            self._current_slots.append(slot)

        color = str(filament_data.get("color", "")).replace("#", "").strip()
        color = color[:6] if len(color) >= 6 else "FFFFFF"

        self._virtual_slot_overrides[slot_index] = {
            "tray_info_idx": filament_data.get("tray_info_idx", slot.get("tray_info_idx", "")),
            "tray_type": filament_data.get("material_type", slot.get("tray_type", "")),
            "tray_color": color,
            "nozzle_temp_min": filament_data.get("nozzle_temp_min", slot.get("nozzle_temp_min")),
            "nozzle_temp_max": filament_data.get("nozzle_temp_max", slot.get("nozzle_temp_max")),
            "expires_at": time.time() + VIRTUAL_ASSIGNMENT_TTL_SECONDS,
        }

        slot["tray_info_idx"] = self._virtual_slot_overrides[slot_index]["tray_info_idx"]
        slot["tray_type"] = self._virtual_slot_overrides[slot_index]["tray_type"]
        slot["tray_color"] = self._virtual_slot_overrides[slot_index]["tray_color"]
        slot["nozzle_temp_min"] = self._virtual_slot_overrides[slot_index]["nozzle_temp_min"]
        slot["nozzle_temp_max"] = self._virtual_slot_overrides[slot_index]["nozzle_temp_max"]
        slot["present"] = True
        self._set_slot_spool_id(slot_index, spool_id)
        self._inject_slot_spool_ids(self._current_slots)

        slot_changes = self._compute_slot_changes(old_slots, self._current_slots)
        self._emit_current_slots_update()
        if slot_changes and self._loop:
            self._loop.call_soon_threadsafe(
                self.emit,
                {
                    "event_type": "slot_tracking_update",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "changes": slot_changes,
                    "slots": self._current_slots,
                },
            )

    # -- Pending-Spool API ----------------------------------------------------

    async def assign_pending_spool(
        self,
        spool_id: int,
        filament_data: dict,
        slot_index: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        """Spule fÃ¼r automatische Zuweisung vormerken."""
        if self._pending and self._pending.timer:
            self._pending.timer.cancel()

        self._pending = PendingSpool(spool_id, filament_data, slot_index)
        effective_timeout = (
            timeout_seconds if timeout_seconds is not None else self._timeout_seconds
        )
        self._pending.timer = asyncio.create_task(self._timeout_task(effective_timeout))
        logger.info(
            f"Pending spool {spool_id} for printer {self.printer_id} (slot: {slot_index}, timeout: {effective_timeout}s)"
        )

    async def _timeout_task(self, timeout: int | None = None) -> None:
        """Wartet auf Timeout, dann verwirft Pending."""
        await asyncio.sleep(timeout if timeout is not None else self._timeout_seconds)
        if self._pending:
            logger.info(f"Pending spool {self._pending.spool_id} timed out")
            self._pending = None

    # -- Health ---------------------------------------------------------------

    def _effective_connected(self) -> bool:
        if self._connected:
            return True
        if not self._running:
            return False
        if self._last_disconnected_at is None:
            return False
        grace = max(0.0, float(self._disconnect_grace_seconds))
        if grace <= 0:
            return False
        return (time.time() - self._last_disconnected_at) <= grace

    def health(self) -> dict[str, Any]:
        ext_exists = any(s.get("slot_index") == "255-254" for s in self._current_slots)
        tool_exists = any(s.get("slot_kind") == "toolhead" for s in self._current_slots)
        total_slots = sum(u.get("tray_count", 0) for u in self._current_ams_units)
        if ext_exists:
            total_slots += 1
        if tool_exists:
            total_slots += sum(1 for s in self._current_slots if s.get("slot_kind") == "toolhead")
        with self._capability_lock:
            write_mode = self._write_mode
            write_mode_source = self._write_mode_source
            write_mode_reason = self._write_mode_reason
            write_mode_checked_at = self._write_mode_checked_at
        effective_connected = self._effective_connected()
        return {
            "driver_key": self.driver_key,
            "printer_id": self.printer_id,
            "running": self._running,
            "connected": effective_connected,
            "connected_raw": self._connected,
            "disconnect_grace_seconds": self._disconnect_grace_seconds,
            "last_connected_at": datetime.fromtimestamp(
                self._last_connected_at, tz=timezone.utc
            ).isoformat()
            if self._last_connected_at
            else None,
            "last_disconnected_at": datetime.fromtimestamp(
                self._last_disconnected_at, tz=timezone.utc
            ).isoformat()
            if self._last_disconnected_at
            else None,
            "last_disconnect_rc": self._last_disconnect_rc,
            "last_mqtt_message_at": datetime.fromtimestamp(
                self._last_mqtt_message_at, tz=timezone.utc
            ).isoformat()
            if self._last_mqtt_message_at
            else None,
            "is_printing": self._current_print_state in ("RUNNING", "PAUSE"),
            "print_state": self._current_print_state or None,
            "print_progress": self._current_print_progress,
            "write_mode": write_mode,
            "write_mode_source": write_mode_source,
            "write_mode_reason": write_mode_reason,
            "write_mode_checked_at": write_mode_checked_at,
            "resolve_shop_images": self._resolve_shop_images,
            "auto_unassign_on_remove": self._auto_unassign_on_remove,
            "consumption_tracking_enabled": self._consumption_tracking_enabled,
            "consumption_tracking_active": self._tracking_active,
            "consumption_tracking_started_at": self._tracking_started_at.isoformat()
            if self._tracking_started_at
            else None,
            "consumption_tracking_progress_delta": self._tracking_progress_delta,
            "consumption_tracking_last_result": self._tracking_last_result,
            "pending": self._pending is not None,
            "printer_model": self._printer_model,
            "ams_type": "AMS Lite" if self._is_ams_lite else "AMS",
            "ams_count": len(self._current_ams_units),
            "slot_count": total_slots,
            "external_spool": ext_exists,
            "ams_units": self._current_ams_units,
            "slots": self._current_slots,
        }









from __future__ import annotations

from typing import Any


class SlotSupportMixin:
    """Shared slot parsing and slot-id helper logic for the Bambu driver."""

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

    @staticmethod
    def _select_external_tray_data(print_data: dict[str, Any]) -> dict[str, Any] | None:
        """Return external tray payload from push_status with firmware fallbacks."""
        vt_tray = print_data.get("vt_tray")
        if isinstance(vt_tray, dict):
            return vt_tray

        vir_slots = print_data.get("vir_slot")
        if isinstance(vir_slots, list):
            candidates = [slot for slot in vir_slots if isinstance(slot, dict)]
            if candidates:
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
        slots: list[dict[str, Any]] = []
        nozzle_info = device_data.get("nozzle", {}).get("info", [])
        extruder_info = device_data.get("extruder", {}).get("info", [])

        if not isinstance(nozzle_info, list) or not isinstance(extruder_info, list):
            return slots

        nozzle_by_id: dict[int, dict[str, Any]] = {}
        for nozzle in nozzle_info:
            if isinstance(nozzle, dict) and "id" in nozzle:
                nozzle_by_id[int(nozzle["id"])] = nozzle

        for extruder in extruder_info:
            if not isinstance(extruder, dict):
                continue
            ext_id = int(extruder.get("id", -1))
            filam_bak = extruder.get("filam_bak", [])
            if not isinstance(filam_bak, list):
                filam_bak = []

            nozzle = nozzle_by_id.get(ext_id, {})
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
                "present": len(filam_bak) > 0,
                "nozzle_diameter": nozzle.get("diameter"),
                "nozzle_type": nozzle.get("type", ""),
            }

            if slot["present"] and isinstance(filam_bak[0], dict):
                filament = filam_bak[0]
                slot["tray_type"] = filament.get("tray_type", "")
                slot["tray_color"] = filament.get("tray_color", "")
                slot["tray_info_idx"] = filament.get("tray_info_idx", "")
                slot["nozzle_temp_min"] = filament.get("nozzle_temp_min")
                slot["nozzle_temp_max"] = filament.get("nozzle_temp_max")
                slot["remain"] = filament.get("remain")
                slot["setting_id"] = filament.get("setting_id", "")
                slot["cali_idx"] = filament.get("cali_idx")

            slots.append(slot)

        return slots

    @staticmethod
    def _map_slot_index_to_3mf_id(ams_id: int, tray_id: int) -> int | None:
        if SlotSupportMixin._is_external_slot_ams_id(ams_id) or SlotSupportMixin._is_ams_ht_slot_ams_id(ams_id):
            return None
        return ams_id * 4 + tray_id + 1

    @staticmethod
    def _find_filament_id_from_mapping(ams_id: int, tray_id: int, mapping_array: list[int]) -> int | None:
        encoded_slot = ams_id * 256 + tray_id
        for filament_idx, encoded in enumerate(mapping_array):
            try:
                if int(encoded) == encoded_slot:
                    return filament_idx + 1
            except (ValueError, TypeError):
                continue
        return None

    @staticmethod
    def _slot_index_to_no(slot_index: str) -> int:
        parts = slot_index.split("-", 1)
        if len(parts) == 2:
            try:
                unit, tray = int(parts[0]), int(parts[1])
                if unit >= 200:
                    return 1000 + tray
                return unit * 4 + tray
            except ValueError:
                pass
        return hash(slot_index) % 10000

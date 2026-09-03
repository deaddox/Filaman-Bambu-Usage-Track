"""
Characterization tests for _finalize_consumption_tracking helper methods.

Covers the pure/static helpers extracted in PR8:
  - Driver._resolve_entry_grams
  - Driver._apply_3mf_grams_to_entries
"""
import sys
import os
import uuid

# conftest.py handles sys.modules stubbing and driver loading.
# After conftest runs, `bambulab.driver` is importable.
from bambulab.driver import Driver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(
    slot_index="0-0",
    grams=None,
    had_drop=False,
    was_used=False,
    has_remain_sensor=False,
    grams_from_3mf=None,
):
    """Build a minimal entry dict for testing."""
    e = {
        "slot_index": slot_index,
        "grams": grams,
        "had_drop": had_drop,
        "was_used": was_used,
        "has_remain_sensor": has_remain_sensor,
        "spool_id": 1,
        "start_remain": 80,
        "end_remain": 70,
    }
    if grams_from_3mf is not None:
        e["grams_from_3mf"] = grams_from_3mf
    return e


# ---------------------------------------------------------------------------
# _resolve_entry_grams
# ---------------------------------------------------------------------------

def _resolve(entry):
    return Driver._resolve_entry_grams(entry)


class TestResolveEntryGrams:
    def test_3mf_path_takes_priority(self):
        e = _entry(grams=5.0, grams_from_3mf=12.0)
        result = _resolve(e)
        assert result == (12.0, "bambulab_measured_3mf")

    def test_no_3mf_grams_is_skipped(self):
        e = _entry(grams=7.5)
        result = _resolve(e)
        assert result is None

    def test_zero_grams_after_resolution_skipped(self):
        e = _entry(grams_from_3mf=0.0)
        e["grams"] = 0.0
        result = _resolve(e)
        assert result is None

    def test_sub_1g_rounds_up_to_1g(self):
        e = _entry(grams_from_3mf=0.5)
        result = _resolve(e)
        assert result == (1.0, "bambulab_measured_3mf")

    def test_exactly_1g_not_changed(self):
        e = _entry(grams_from_3mf=1.0)
        result = _resolve(e)
        assert result == (1.0, "bambulab_measured_3mf")

    def test_above_1g_not_changed(self):
        e = _entry(grams_from_3mf=1.055)
        result = _resolve(e)
        assert result == (1.055, "bambulab_measured_3mf")


class TestShouldRecord3mfFailureZeroEvents:
    def test_returns_false_when_no_failure_reason(self):
        assert Driver._should_record_3mf_failure_zero_events(None, 42) is False

    def test_returns_false_when_progress_is_zero(self):
        assert Driver._should_record_3mf_failure_zero_events("3mf_unavailable", 0) is False

    def test_returns_false_when_progress_is_none(self):
        assert Driver._should_record_3mf_failure_zero_events("3mf_unavailable", None) is False

    def test_returns_true_when_progress_above_zero(self):
        assert Driver._should_record_3mf_failure_zero_events("3mf_unavailable", 0.1) is True


# ---------------------------------------------------------------------------
# _apply_3mf_grams_to_entries
# ---------------------------------------------------------------------------

class TestApply3mfGramsToEntries:
    def _apply(self, entries, per_slot_usage, layer_grams=None, per_slot_data=None,
               mapping_array=None, end_state="completed", progress_delta=100.0):
        Driver._apply_3mf_grams_to_entries(
            entries, per_slot_usage, layer_grams, per_slot_data, mapping_array, end_state, progress_delta
        )

    def test_completed_full_weight_assigned(self):
        e = _entry("0-0")
        self._apply([e], {1: 12.5}, end_state="completed")
        assert abs(e["grams_from_3mf"] - 12.5) < 1e-9
        assert e["source_3mf"] is True

    def test_partial_print_linear_scale(self):
        e = _entry("0-0")
        self._apply([e], {1: 20.0}, end_state="cancelled", progress_delta=50.0)
        assert abs(e["grams_from_3mf"] - 10.0) < 1e-9

    def test_partial_print_layer_scale_used_when_available(self):
        e = _entry("0-0")
        layer_grams = {1: 7.5}
        self._apply([e], {1: 20.0}, layer_grams=layer_grams,
                    end_state="cancelled", progress_delta=50.0)
        assert abs(e["grams_from_3mf"] - 7.5) < 1e-9

    def test_single_filament_fallback(self):
        """When 3MF has one slot and one used entry, assign by name match."""
        e = _entry("0-0", was_used=True)
        # slot_id for 0-0 via _map_slot_index_to_3mf_id = 0*4+0+1 = 1, but per_slot_usage keyed on 99
        # so it won't direct-match; use single-filament fallback path.
        self._apply([e], {99: 18.0}, end_state="completed")
        # slot 0-0 maps to slot_id=1, not in {99}, so single-filament fallback kicks in
        assert abs(e.get("grams_from_3mf", 0) - 18.0) < 1e-9
        assert e["source_3mf"] is True

    def test_no_per_slot_usage_noop(self):
        e = _entry("0-0")
        self._apply([e], None)
        assert "grams_from_3mf" not in e

    def test_slot_not_in_per_slot_usage_no_assignment(self):
        """Slot 0-1 (slot_id=2) not in usage dict and no single-filament fallback (2 used)."""
        e1 = _entry("0-0", was_used=True)
        e2 = _entry("0-1", was_used=True)
        # usage only has slot_id=1 (maps to 0-0)
        self._apply([e1, e2], {1: 10.0}, end_state="completed")
        assert abs(e1["grams_from_3mf"] - 10.0) < 1e-9
        assert "grams_from_3mf" not in e2  # not mapped, no single-filament fallback (2 used slots)

    def test_mapping_array_based_slot_resolution(self):
        """Test mapping array-based resolution for multi-filament prints (PETG.gcode.3mf case).
        
        PETG.gcode.3mf had:
        - mapping = [259, 1] where 259 = 1*256+3 (AMS 1, tray 3) and 1 = 0*256+1 (AMS 0, tray 1)
        - per_slot_usage = {1: 0.49, 2: 0.41}  # slicer-local filament ids
        - Entries for physical slots (0-1) and (1-3)
        
        Expected: mapping array maps (1-3) -> filament_id=1, (0-1) -> filament_id=2
        """
        # Physical slots from AMS
        e_ams0_tray1 = _entry("0-1", was_used=True)  # AMS 0, Tray 1
        e_ams1_tray3 = _entry("1-3", was_used=True)  # AMS 1, Tray 3
        
        # Mapping array: [259, 1]
        mapping = [259, 1]  # 259 = AMS 1, Tray 3; 1 = AMS 0, Tray 1
        
        # Per-slot usage from 3MF (slicer-local filament ids 1..2)
        per_slot_usage = {1: 0.49, 2: 0.41}
        
        # Apply with mapping
        self._apply([e_ams0_tray1, e_ams1_tray3], per_slot_usage, 
                    mapping_array=mapping, end_state="completed")
        
        # Verify both slots got correct filament usage
        assert abs(e_ams0_tray1.get("grams_from_3mf", 0) - 0.41) < 1e-9, \
            f"Expected 0.41 for slot 0-1 (filament_id=2), got {e_ams0_tray1.get('grams_from_3mf')}"
        assert e_ams0_tray1["source_3mf"] is True
        
        assert abs(e_ams1_tray3.get("grams_from_3mf", 0) - 0.49) < 1e-9, \
            f"Expected 0.49 for slot 1-3 (filament_id=1), got {e_ams1_tray3.get('grams_from_3mf')}"
        assert e_ams1_tray3["source_3mf"] is True

    def test_mapping_array_fallback_to_heuristic(self):
        """When mapping array doesn't have the slot, fall back to heuristic."""
        # Slot 0-0 with heuristic slot_id = 0*4+0+1 = 1
        e = _entry("0-0", was_used=True)
        
        # Mapping that doesn't include 0-0: [259] (only AMS 1, Tray 3)
        mapping = [259]
        
        # Per-slot usage with filament_id=1 (heuristic slot_id)
        per_slot_usage = {1: 10.5}
        
        # Apply with partial mapping
        self._apply([e], per_slot_usage, mapping_array=mapping, end_state="completed")
        
        # Should fall back to heuristic and find slot_id=1
        assert abs(e.get("grams_from_3mf", 0) - 10.5) < 1e-9
        assert e["source_3mf"] is True

    def test_mapping_array_prevents_heuristic_duplicate_assignment(self):
        """When mapping_array exists, heuristic fallback must not assign extra slots."""
        # Two physical slots exist, but mapping binds filament_id=1 only to slot 1-0.
        e00 = _entry("0-0", was_used=False)
        e10 = _entry("1-0", was_used=True)

        # 1-0 encoded as 1*256 + 0 = 256
        mapping = [256]

        # Single 3MF filament entry
        per_slot_usage = {1: 1.4}

        self._apply([e00, e10], per_slot_usage, mapping_array=mapping, end_state="completed")

        # Only mapped slot receives grams; no heuristic spillover to 0-0.
        assert "grams_from_3mf" not in e00
        assert abs(e10.get("grams_from_3mf", 0) - 1.4) < 1e-9
        assert e10["source_3mf"] is True

    def test_single_filament_fallback_uses_had_drop_when_multiple_were_marked_used(self):
        """If mapped slots over-mark usage, fallback still chooses one physical slot."""
        # Two slots marked used (e.g. ambiguous mapping), but only one had sensor drop.
        e1 = _entry("0-1", was_used=True, had_drop=True)
        e2 = _entry("1-3", was_used=True, had_drop=False)

        # Single filament usage with unmappable slot id triggers fallback.
        self._apply([e1, e2], {99: 14.2}, end_state="completed")

        assert abs(e1.get("grams_from_3mf", 0) - 14.2) < 1e-9
        assert e1.get("source_3mf") is True
        assert "grams_from_3mf" not in e2


class TestUpdateConsumptionTrackingUsage:
    def test_does_not_mark_ambiguous_mapping_slots_as_used(self):
        driver = Driver(
            printer_id=1,
            config={"host": "x", "serial": "y", "access_code": "z"},
            emitter=lambda payload: None,
        )
        driver.log_debug = lambda *args, **kwargs: None
        driver._current_slots = [
            {"slot_index": "0-1", "present": True, "remain": 80},
            {"slot_index": "1-3", "present": True, "remain": 70},
        ]

        # Two mapped slots should not be treated as actual usage evidence.
        driver._update_consumption_tracking_usage({"mapping": [259, 1]})

        assert driver._tracking_used_slots == set()

    def test_marks_single_mapping_slot_as_used(self):
        driver = Driver(
            printer_id=1,
            config={"host": "x", "serial": "y", "access_code": "z"},
            emitter=lambda payload: None,
        )
        driver.log_debug = lambda *args, **kwargs: None
        driver._current_slots = [
            {"slot_index": "0-1", "present": True, "remain": 80},
        ]

        driver._update_consumption_tracking_usage({"mapping": [1]})

        assert driver._tracking_used_slots == {"0-1"}


class TestFinalizeDedupeHelpers:
    def test_build_finalize_dedupe_key_prefers_job_id(self):
        driver = Driver(
            printer_id=7,
            config={"host": "x", "serial": "y", "access_code": "z"},
            emitter=lambda payload: None,
        )

        key = driver._build_finalize_dedupe_key(
            {"job_id": "927303936", "lan_task_id": "0"},
            None,
        )
        assert key == "printer=7|job_id=927303936"

    def test_finalize_dedupe_marker_allows_first_then_blocks_second(self):
        driver = Driver(
            printer_id=7,
            config={"host": "x", "serial": "y", "access_code": "z"},
            emitter=lambda payload: None,
        )

        dedupe_key = f"printer=7|job_id=test-{uuid.uuid4()}"
        marker_path = driver._build_finalize_dedupe_marker_path(dedupe_key)
        if os.path.exists(marker_path):
            os.remove(marker_path)

        try:
            assert driver._try_acquire_finalize_dedupe_marker(dedupe_key) is True
            assert driver._try_acquire_finalize_dedupe_marker(dedupe_key) is False
        finally:
            if os.path.exists(marker_path):
                os.remove(marker_path)

    def test_update_tracking_context_includes_job_identity_fields(self):
        driver = Driver(
            printer_id=7,
            config={"host": "x", "serial": "y", "access_code": "z"},
            emitter=lambda payload: None,
        )

        driver._tracking_print_context = {}
        driver._update_tracking_print_context(
            {
                "gcode_file": "/data/Metadata/plate_1.gcode",
                "job_id": "927303936",
                "lan_task_id": "0",
                "model_id": "USd323bac8136384",
                "design_id": "1647659",
            }
        )

        assert driver._tracking_print_context.get("job_id") == "927303936"
        assert driver._tracking_print_context.get("lan_task_id") == "0"
        assert driver._tracking_print_context.get("model_id") == "USd323bac8136384"
        assert driver._tracking_print_context.get("design_id") == "1647659"


class TestFailureLayerSuffix:
    def test_failed_print_includes_layer(self):
        assert Driver._failure_layer_suffix("FAILED", 618) == ", failed at layer 618"

    def test_cancelled_print_includes_layer(self):
        assert Driver._failure_layer_suffix("CANCELLED", 42) == ", failed at layer 42"

    def test_finished_print_has_no_suffix(self):
        assert Driver._failure_layer_suffix("FINISH", 813) == ""

    def test_unknown_layer_has_no_suffix(self):
        assert Driver._failure_layer_suffix("FAILED", None) == ""

    def test_empty_state_is_treated_as_failure(self):
        assert Driver._failure_layer_suffix("", 3) == ", failed at layer 3"


class TestConfigParsing:
    def _make_driver(self, **config):
        cfg = {"host": "x", "serial": "y", "access_code": "z"}
        cfg.update(config)
        return Driver(printer_id=1, config=cfg, emitter=lambda payload: None)

    def test_reconnect_interval_minutes_int(self):
        d = self._make_driver(reconnect_interval_minutes=10)
        assert d._reconnect_interval == 600

    def test_reconnect_interval_minutes_string(self):
        d = self._make_driver(reconnect_interval_minutes="10")
        assert d._reconnect_interval == 600

    def test_auto_unassign_on_remove_string_false(self):
        d = self._make_driver(auto_unassign_on_remove="false")
        assert d._auto_unassign_on_remove is False

    def test_consumption_tracking_string_off(self):
        d = self._make_driver(enable_consumption_tracking="off")
        assert d._consumption_tracking_enabled is False

    def test_local_3mf_timeout_string(self):
        d = self._make_driver(local_3mf_timeout_seconds="12")
        assert d._local_3mf_timeout_seconds == 12.0

    def test_local_3mf_ftps_verify_tls_string_true(self):
        d = self._make_driver(local_3mf_ftps_verify_tls="true")
        assert d._local_3mf_ftps_verify_tls is True


class TestReadOnlyDriverActions:
    def _make_driver(self, **config):
        cfg = {
            "host": "x",
            "serial": "y",
            "access_code": "z",
            "printer_model": "X1C",
        }
        cfg.update(config)
        return Driver(printer_id=1, config=cfg, emitter=lambda payload: None)

    def test_list_connected_models(self):
        d = self._make_driver()
        result = d.list_connected_models()
        assert result["count"] == 1
        assert result["models"][0]["model"] == "X1C"
        assert result["models"][0]["printer_ids"] == [1]
        assert result["models"][0]["representative_printer_id"] == 1

    def test_list_connected_models_empty_model(self):
        d = self._make_driver(printer_model="")
        assert d.list_connected_models() == {"models": [], "count": 0}

    def test_get_profile_coverage_empty(self):
        d = self._make_driver()
        result = d.get_profile_coverage(spool_id=42, filament_id=7)
        assert result["spool_id"] == 42
        assert result["filament_id"] == 7
        assert result["default_base_name"] == ""
        assert result["pending_display_name"] is False
        assert result["profiles_by_model"] == {}
        assert result["per_model_profiles_enabled"] is False
        assert result["coverage"] == {}

    def test_get_profile_coverage_none_ids(self):
        d = self._make_driver()
        result = d.get_profile_coverage()
        assert result["spool_id"] is None
        assert result["filament_id"] is None

    def test_list_cloud_presets_empty(self):
        d = self._make_driver()
        assert d.list_cloud_presets() == {"presets": [], "count": 0}
        assert d.list_cloud_presets(force=True, model="X1C", group="base") == {
            "presets": [],
            "count": 0,
        }


class TestShouldRecordZeroFailureForEntry:
    def test_used_slot_is_recorded(self):
        assert (
            Driver._should_record_zero_failure_for_entry(
                {"was_used": True, "had_drop": False}
            )
            is True
        )

    def test_dropped_slot_is_recorded(self):
        assert (
            Driver._should_record_zero_failure_for_entry(
                {"was_used": False, "had_drop": True}
            )
            is True
        )

    def test_inactive_slot_is_skipped(self):
        assert (
            Driver._should_record_zero_failure_for_entry(
                {"was_used": False, "had_drop": False}
            )
            is False
        )

    def test_missing_flags_are_skipped(self):
        assert Driver._should_record_zero_failure_for_entry({}) is False

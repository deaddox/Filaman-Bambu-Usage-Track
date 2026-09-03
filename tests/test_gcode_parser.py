"""Tests for static gcode/3MF parser methods on Driver.

Covers:
  - _extract_layer_gcode_usage_from_file: layer markers, relative/absolute E,
    G92 reset, T tool changes, empty/missing gcode.
  - _extract_per_slot_filament_usage_from_file: 1-based IDs, 0-based ID shift,
    attribute vs element parsing, missing fields.
  - _map_slot_index_to_3mf_id: normal AMS, external-tray returns None.
"""
import io
import sys
import zipfile
from pathlib import Path

import pytest

# conftest.py loads the driver module into sys.modules["bambulab.driver"]
# before this file is collected, so the import below is satisfied from cache.
sys.path.insert(0, str(Path(__file__).parent.parent))
Driver = sys.modules["bambulab.driver"].Driver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_3mf(gcode_content: str | None = None, slice_info: str | None = None) -> bytes:
    """Return in-memory bytes of a minimal .3mf zip archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if gcode_content is not None:
            zf.writestr("3D/model.gcode", gcode_content)
        if slice_info is not None:
            zf.writestr("Metadata/slice_info.config", slice_info)
    return buf.getvalue()


def _3mf_path(tmp_path: Path, content: bytes) -> str:
    p = tmp_path / "test.3mf"
    p.write_bytes(content)
    return str(p)


# ---------------------------------------------------------------------------
# _extract_layer_gcode_usage_from_file
# ---------------------------------------------------------------------------

class TestLayerGcodeParser:
    def test_no_gcode_returns_none(self, tmp_path):
        path = _3mf_path(tmp_path, _make_3mf())  # no gcode entry
        assert Driver._extract_layer_gcode_usage_from_file(path) is None

    def test_empty_gcode_returns_none(self, tmp_path):
        path = _3mf_path(tmp_path, _make_3mf(gcode_content=""))
        assert Driver._extract_layer_gcode_usage_from_file(path) is None

    def test_single_layer_relative_mode(self, tmp_path):
        gcode = "\n".join([
            ";LAYER:0",
            "G1 X10 Y10 E1.0",
            "G1 X20 Y20 E2.5",
        ])
        path = _3mf_path(tmp_path, _make_3mf(gcode_content=gcode))
        result = Driver._extract_layer_gcode_usage_from_file(path)
        assert result is not None
        assert 0 in result
        entries = {layer: mm for layer, mm in result[0]}
        # Cumulative: 1.0 + 2.5 = 3.5
        assert entries[0] == pytest.approx(3.5)

    def test_layer_marker_order_is_respected(self, tmp_path):
        """Layer markers must be read before the generic comment-skip guard."""
        gcode = "\n".join([
            ";LAYER:0",
            "G1 E1.0",
            ";LAYER:1",
            "G1 E2.0",
            ";LAYER:2",
            "G1 E3.0",
        ])
        path = _3mf_path(tmp_path, _make_3mf(gcode_content=gcode))
        result = Driver._extract_layer_gcode_usage_from_file(path)
        assert result is not None
        entries = dict(result[0])  # {layer_num: cumulative_mm}
        assert 0 in entries
        assert 1 in entries
        assert 2 in entries
        # Layer 2 cumulative = 1.0+2.0+3.0 = 6.0
        assert entries[2] == pytest.approx(6.0)

    def test_retraction_moves_are_ignored_in_relative_mode(self, tmp_path):
        gcode = "\n".join([
            ";LAYER:0",
            "G1 E2.0",     # extrusion
            "G1 E-0.5",    # retraction – should not subtract
        ])
        path = _3mf_path(tmp_path, _make_3mf(gcode_content=gcode))
        result = Driver._extract_layer_gcode_usage_from_file(path)
        assert result is not None
        assert dict(result[0])[0] == pytest.approx(2.0)

    def test_absolute_e_mode_computes_delta(self, tmp_path):
        gcode = "\n".join([
            "M82",          # absolute mode
            ";LAYER:0",
            "G1 E5.0",      # first absolute E – treated as 5.0 from zero
            ";LAYER:1",
            "G1 E8.0",      # delta = 8.0 - 5.0 = 3.0
        ])
        path = _3mf_path(tmp_path, _make_3mf(gcode_content=gcode))
        result = Driver._extract_layer_gcode_usage_from_file(path)
        assert result is not None
        entries = dict(result[0])
        assert entries[0] == pytest.approx(5.0)   # first value is 5.0 (previous=None → max(0,5))
        assert entries[1] == pytest.approx(8.0)   # cumulative: 5.0 + 3.0

    def test_g92_reset_in_relative_mode(self, tmp_path):
        """G92 E0 in relative mode: next move should NOT add previous cumulative."""
        gcode = "\n".join([
            "M83",          # relative (default)
            ";LAYER:0",
            "G1 E10.0",
            "G92 E0",       # reset: last_e_by_filament[0] = 0
            ";LAYER:1",
            "G1 E3.0",      # adds 3.0 on top of cumulative 10.0
        ])
        path = _3mf_path(tmp_path, _make_3mf(gcode_content=gcode))
        result = Driver._extract_layer_gcode_usage_from_file(path)
        assert result is not None
        entries = dict(result[0])
        # Relative mode: G92 stores a last-e hint but doesn't affect cumulative directly;
        # the cumulative at layer 1 should be 10.0 + 3.0 = 13.0
        assert entries[1] == pytest.approx(13.0)

    def test_g92_reset_in_absolute_mode_adjusts_delta_base(self, tmp_path):
        """G92 E0 in absolute mode resets the baseline for next delta calculation."""
        gcode = "\n".join([
            "M82",          # absolute
            ";LAYER:0",
            "G1 E10.0",     # cumulative = 10.0
            "G92 E0",       # reset baseline: last_e = 0
            ";LAYER:1",
            "G1 E4.0",      # delta from new baseline: 4.0 - 0 = 4.0 → cumulative = 14.0
        ])
        path = _3mf_path(tmp_path, _make_3mf(gcode_content=gcode))
        result = Driver._extract_layer_gcode_usage_from_file(path)
        assert result is not None
        entries = dict(result[0])
        assert entries[1] == pytest.approx(14.0)

    def test_multi_tool_tracking(self, tmp_path):
        """T commands route extrusion to correct filament_id."""
        gcode = "\n".join([
            "T0",
            ";LAYER:0",
            "G1 E5.0",      # filament 0: 5.0
            "T1",
            "G1 E3.0",      # filament 1: 3.0
            "T0",
            ";LAYER:1",
            "G1 E2.0",      # filament 0: 5.0+2.0=7.0
        ])
        path = _3mf_path(tmp_path, _make_3mf(gcode_content=gcode))
        result = Driver._extract_layer_gcode_usage_from_file(path)
        assert result is not None
        assert 0 in result
        assert 1 in result
        f0 = dict(result[0])
        f1 = dict(result[1])
        assert f0[1] == pytest.approx(7.0)
        assert f1[0] == pytest.approx(3.0)

    def test_mode_switch_mid_print(self, tmp_path):
        """Switching from M83 to M82 mid-print is handled without crash."""
        gcode = "\n".join([
            "M83",
            ";LAYER:0",
            "G1 E2.0",
            "M82",
            ";LAYER:1",
            "G1 E5.0",   # first absolute E → delta from None = 5.0; cumulative = 7.0
        ])
        path = _3mf_path(tmp_path, _make_3mf(gcode_content=gcode))
        result = Driver._extract_layer_gcode_usage_from_file(path)
        assert result is not None
        entries = dict(result[0])
        assert entries[1] == pytest.approx(7.0)

    def test_no_layer_markers_all_on_layer_0(self, tmp_path):
        """Without ;LAYER: comments everything accumulates on layer 0."""
        gcode = "\n".join(["G1 E1.0", "G1 E1.0", "G1 E1.0"])
        path = _3mf_path(tmp_path, _make_3mf(gcode_content=gcode))
        result = Driver._extract_layer_gcode_usage_from_file(path)
        assert result is not None
        assert dict(result[0])[0] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# _extract_per_slot_filament_usage_from_file
# ---------------------------------------------------------------------------

_SLICE_INFO_1BASED = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <filament filament_id="1" used_g="12.5" color="FF0000" type="PLA"
              density="1.24" diameter="1.75"/>
    <filament filament_id="2" used_g="7.3" color="00FF00" type="PETG"
              density="1.27" diameter="1.75"/>
  </plate>
</config>"""

_SLICE_INFO_0BASED = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <filament filament_id="0" used_g="9.0" color="0000FF" type="ABS"
              density="1.04" diameter="1.75"/>
    <filament filament_id="1" used_g="4.0" color="FFFFFF" type="ABS"
              density="1.04" diameter="1.75"/>
  </plate>
</config>"""

_SLICE_INFO_CHILD_ELEMENTS = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <filament>
      <filament_id>1</filament_id>
      <used_g>5.5</used_g>
      <color>AA00FF</color>
      <type>TPU</type>
      <density>1.20</density>
      <diameter>1.75</diameter>
    </filament>
  </plate>
</config>"""

_SLICE_INFO_MISSING_USED_G = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <filament filament_id="1" color="FF0000" type="PLA" density="1.24" diameter="1.75"/>
  </plate>
</config>"""


class TestPerSlotExtraction:
    def test_1based_ids_are_preserved(self, tmp_path):
        path = _3mf_path(tmp_path, _make_3mf(slice_info=_SLICE_INFO_1BASED))
        result = Driver._extract_per_slot_filament_usage_from_file(path)
        assert result is not None
        assert len(result) == 2
        ids = {f["slot_id"] for f in result}
        assert ids == {1, 2}

    def test_0based_ids_are_shifted_uniformly(self, tmp_path):
        """0-based IDs (0,1) must become (1,2) without collision."""
        path = _3mf_path(tmp_path, _make_3mf(slice_info=_SLICE_INFO_0BASED))
        result = Driver._extract_per_slot_filament_usage_from_file(path)
        assert result is not None
        ids = {f["slot_id"] for f in result}
        assert ids == {1, 2}   # not {1, 1}

    def test_used_g_values_are_correct(self, tmp_path):
        path = _3mf_path(tmp_path, _make_3mf(slice_info=_SLICE_INFO_1BASED))
        result = Driver._extract_per_slot_filament_usage_from_file(path)
        by_slot = {f["slot_id"]: f for f in result}
        assert by_slot[1]["used_g"] == pytest.approx(12.5)
        assert by_slot[2]["used_g"] == pytest.approx(7.3)

    def test_child_element_format_parsed(self, tmp_path):
        path = _3mf_path(tmp_path, _make_3mf(slice_info=_SLICE_INFO_CHILD_ELEMENTS))
        result = Driver._extract_per_slot_filament_usage_from_file(path)
        assert result is not None
        assert result[0]["slot_id"] == 1
        assert result[0]["used_g"] == pytest.approx(5.5)
        assert result[0]["type"] == "TPU"

    def test_missing_used_g_defaults_to_zero(self, tmp_path):
        path = _3mf_path(tmp_path, _make_3mf(slice_info=_SLICE_INFO_MISSING_USED_G))
        result = Driver._extract_per_slot_filament_usage_from_file(path)
        assert result is not None
        assert result[0]["used_g"] == pytest.approx(0.0)

    def test_no_slice_info_returns_none(self, tmp_path):
        path = _3mf_path(tmp_path, _make_3mf())  # no slice_info entry
        assert Driver._extract_per_slot_filament_usage_from_file(path) is None

    def test_density_diameter_defaults(self, tmp_path):
        """Missing density/diameter should fall back to sensible defaults."""
        info = """<?xml version="1.0"?>
<config><plate>
  <filament filament_id="1" used_g="5.0" type="PLA"/>
</plate></config>"""
        path = _3mf_path(tmp_path, _make_3mf(slice_info=info))
        result = Driver._extract_per_slot_filament_usage_from_file(path)
        assert result is not None
        assert result[0]["density"] == pytest.approx(1.24)
        assert result[0]["diameter"] == pytest.approx(1.75)


# ---------------------------------------------------------------------------
# _map_slot_index_to_3mf_id
# ---------------------------------------------------------------------------

class TestSlotMapping:
    @pytest.mark.parametrize("ams_id,tray_id,expected", [
        (0, 0, 1),
        (0, 1, 2),
        (0, 3, 4),
        (1, 0, 5),
        (1, 3, 8),
        (2, 0, 9),
    ])
    def test_ams_linear_mapping(self, ams_id, tray_id, expected):
        assert Driver._map_slot_index_to_3mf_id(ams_id, tray_id) == expected

    def test_external_tray_returns_none(self):
        """ams_id >= 200 is an external tray; no reliable 3MF slot id."""
        assert Driver._map_slot_index_to_3mf_id(255, 254) is None
        assert Driver._map_slot_index_to_3mf_id(200, 0) is None


# ---------------------------------------------------------------------------
# _parse_layer_num / _resolve_gcode_layer
# ---------------------------------------------------------------------------

class TestLayerNumberParsing:
    def test_parse_layer_num_int(self):
        assert Driver._parse_layer_num(618) == 618

    def test_parse_layer_num_string(self):
        assert Driver._parse_layer_num("618") == 618

    def test_parse_layer_num_float(self):
        assert Driver._parse_layer_num(618.9) == 618

    def test_parse_layer_num_negative_returns_none(self):
        assert Driver._parse_layer_num(-1) is None

    def test_parse_layer_num_invalid_returns_none(self):
        assert Driver._parse_layer_num("abc") is None
        assert Driver._parse_layer_num(None) is None


class TestResolveGcodeLayer:
    def test_uses_exact_layer_with_1_based_offset(self):
        # gcode markers 0..812, printer total 813 -> offset 1
        assert Driver._resolve_gcode_layer(618, 813, 812, 50.0) == 617

    def test_uses_exact_layer_with_0_based_offset(self):
        # gcode markers 0..812, printer total 812 -> offset 0
        assert Driver._resolve_gcode_layer(618, 812, 812, 50.0) == 618

    def test_fractional_fallback_on_unknown_numbering(self):
        # 500/1000 of 800 layers -> 400
        assert Driver._resolve_gcode_layer(500, 1000, 800, 50.0) == 400

    def test_progress_fallback_when_layer_missing(self):
        assert Driver._resolve_gcode_layer(None, None, 100, 25.0) == 25

    def test_progress_fallback_single_layer(self):
        assert Driver._resolve_gcode_layer(None, None, 0, 37.5) == 37

    def test_clamps_to_gcode_layer_bounds(self):
        # offset-1 branch: layer 9999 clamps to max; layer 0 clamps to 0
        assert Driver._resolve_gcode_layer(9999, 813, 812, 50.0) == 812
        assert Driver._resolve_gcode_layer(0, 813, 812, 50.0) == 0

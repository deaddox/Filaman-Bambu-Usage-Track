"""Unit tests for driver-managed slot location identity and naming behavior."""

from bambulab.driver import Driver


def _driver(printer_id: int = 7) -> Driver:
    driver = Driver(
        printer_id=printer_id,
        config={
            "host": "10.0.0.10",
            "serial": "SERIAL-1",
            "access_code": "secret",
        },
        emitter=lambda payload: None,
    )
    driver._printer_name = "H2C"
    return driver


def test_build_slot_location_identifier():
    driver = _driver(7)
    assert driver._build_slot_location_identifier(0, 2) == "bambulab_7_0_2"


def test_parse_driver_slot_identifier_valid():
    assert Driver._parse_driver_slot_identifier("bambulab_7_128_0") == (7, 128, 0)


def test_parse_driver_slot_identifier_invalid():
    assert Driver._parse_driver_slot_identifier(None) is None
    assert Driver._parse_driver_slot_identifier("") is None
    assert Driver._parse_driver_slot_identifier("bambulab_7_0") is None
    assert Driver._parse_driver_slot_identifier("other_7_0_0") is None
    assert Driver._parse_driver_slot_identifier("bambulab_7_a_0") is None


def test_external_slot_classification_starts_at_200():
    assert Driver._is_external_slot_ams_id(127) is False
    assert Driver._is_external_slot_ams_id(128) is False
    assert Driver._is_external_slot_ams_id(199) is False
    assert Driver._is_external_slot_ams_id(200) is True
    assert Driver._is_external_slot_ams_id(255) is True


def test_ams_ht_slot_classification_includes_128_to_199():
    assert Driver._is_ams_ht_slot_ams_id(127) is False
    assert Driver._is_ams_ht_slot_ams_id(128) is True
    assert Driver._is_ams_ht_slot_ams_id(129) is True
    assert Driver._is_ams_ht_slot_ams_id(199) is True
    assert Driver._is_ams_ht_slot_ams_id(200) is False


def test_generate_slot_location_name_for_128_is_ams_ht_a():
    driver = _driver(7)
    assert driver._generate_slot_location_name(128, 0) == "H2C - HT-A"


def test_generate_slot_location_name_for_multiple_ams_ht_units():
    driver = _driver(7)
    assert driver._generate_slot_location_name(128, 0) == "H2C - HT-A"
    assert driver._generate_slot_location_name(129, 0) == "H2C - HT-B"


def test_generate_slot_location_name_for_true_external_slot_200_plus():
    driver = _driver(7)
    assert driver._generate_slot_location_name(255, 254) == "H2C - Ext"


def test_generate_slot_location_name_for_regular_ams_slot():
    driver = _driver(7)
    assert driver._generate_slot_location_name(0, 2) == "H2C - AMS A3"


def test_generate_slot_location_name_maps_ams_one_to_b_row():
    driver = _driver(7)
    assert driver._generate_slot_location_name(1, 0) == "H2C - AMS B1"


def test_generate_slot_location_name_maps_ams_zero_second_slot_to_a2():
    driver = _driver(7)
    assert driver._generate_slot_location_name(0, 1) == "H2C - AMS A2"


def test_3mf_mapper_returns_none_for_ams_ht_slot_128():
    assert Driver._map_slot_index_to_3mf_id(128, 0) is None


def test_3mf_mapper_returns_none_for_external_slot_200_plus():
    assert Driver._map_slot_index_to_3mf_id(255, 254) is None


def test_3mf_mapper_regular_slot_still_supported():
    assert Driver._map_slot_index_to_3mf_id(0, 2) == 3


def test_select_external_tray_prefers_vt_tray_when_present():
    payload = {
        "vt_tray": {"id": "254", "tray_type": "ASA", "tray_info_idx": "GFB01"},
        "vir_slot": [
            {"id": "254", "tray_type": "PETG", "tray_info_idx": "PETA"},
            {"id": "255", "tray_type": "", "tray_info_idx": ""},
        ],
    }
    selected = Driver._select_external_tray_data(payload)
    assert selected is not None
    assert selected.get("tray_type") == "ASA"
    assert selected.get("tray_info_idx") == "GFB01"


def test_select_external_tray_from_vir_slot_prefers_id_254_with_material():
    payload = {
        "vir_slot": [
            {"id": "255", "tray_type": "", "tray_info_idx": ""},
            {"id": "254", "tray_type": "ASA", "tray_info_idx": "GFB01"},
        ]
    }
    selected = Driver._select_external_tray_data(payload)
    assert selected is not None
    assert str(selected.get("id")) == "254"
    assert selected.get("tray_type") == "ASA"


def test_select_external_tray_from_vir_slot_uses_any_material_if_254_empty():
    payload = {
        "vir_slot": [
            {"id": "254", "tray_type": "", "tray_info_idx": ""},
            {"id": "200", "tray_type": "PLA", "tray_info_idx": "P675098c"},
        ]
    }
    selected = Driver._select_external_tray_data(payload)
    assert selected is not None
    assert selected.get("tray_type") == "PLA"


def test_select_external_tray_returns_none_when_missing():
    assert Driver._select_external_tray_data({}) is None
    assert Driver._select_external_tray_data({"vir_slot": []}) is None


def test_refresh_status_reports_single_known_spool_id():
    driver = _driver(7)
    driver._current_slots = [
        {
            "slot_index": "0-0",
            "slot_name": "AMS 1 - Slot 1",
            "slot_kind": "tray",
            "present": True,
        }
    ]
    driver._set_slot_spool_id("0-0", 42)

    assert driver.refresh_status() == {"active_spool_id": 42}


def test_refresh_status_returns_none_when_multiple_spools_known():
    driver = _driver(7)
    driver._current_slots = [
        {
            "slot_index": "0-0",
            "slot_name": "AMS 1 - Slot 1",
            "slot_kind": "tray",
            "present": True,
        },
        {
            "slot_index": "0-1",
            "slot_name": "AMS 1 - Slot 2",
            "slot_kind": "tray",
            "present": True,
        },
    ]
    driver._set_slot_spool_id("0-0", 11)
    driver._set_slot_spool_id("0-1", 12)

    assert driver.refresh_status() == {"active_spool_id": None}


def test_inject_slot_spool_ids_emits_only_for_present_slots():
    driver = _driver(7)
    slots = [
        {
            "slot_index": "0-0",
            "slot_name": "AMS 1 - Slot 1",
            "slot_kind": "tray",
            "present": True,
        },
        {
            "slot_index": "0-1",
            "slot_name": "AMS 1 - Slot 2",
            "slot_kind": "tray",
            "present": False,
        },
    ]
    driver._set_slot_spool_id("0-0", 91)
    driver._set_slot_spool_id("0-1", 92)

    driver._inject_slot_spool_ids(slots)

    assert slots[0]["spool_id"] == 91
    assert "spool_id" not in slots[1]
    assert "0-1" not in driver._slot_spool_ids

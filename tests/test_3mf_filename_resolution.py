"""Tests for resolving 3MF file names from MQTT print payload fields."""

from __future__ import annotations

import os

from bambulab.driver import Driver
import bambulab.driver as driver_module


def _driver() -> Driver:
    return Driver(
        printer_id=99,
        config={
            "host": "10.0.0.20",
            "serial": "TEST-SERIAL",
            "access_code": "secret",
        },
        emitter=lambda payload: None,
    )


def test_build_3mf_candidates_from_push_status_gcode_file():
    context = {
        "gcode_file": "/data/Metadata/plate_1.gcode",
    }

    candidates = Driver._build_3mf_candidate_filenames(context)

    # The first candidate is the expected printer-side metadata artifact.
    assert candidates == ["plate_1.gcode.3mf"]


def test_build_3mf_candidates_dedupes_equivalent_subtask_and_gcode_file():
    context = {
        "subtask_name": "PETG",
        "gcode_file": "PETG.3mf",
    }

    candidates = Driver._build_3mf_candidate_filenames(context)

    assert candidates == ["PETG.3mf", "PETG.gcode.3mf"]


def test_get_3mf_file_path_prefers_cache_plate_metadata_name(monkeypatch):
    driver = _driver()
    probes: list[str] = []
    retr_cmds: list[str] = []

    class FakeFTP:
        def __init__(self, context=None):
            self.context = context

        def connect(self, host, port, timeout):
            return None

        def login(self, user, passwd):
            return None

        def prot_p(self):
            return None

        def size(self, remote_path):
            probes.append(remote_path)
            if remote_path == "/cache/plate_1.gcode.3mf":
                return 123
            raise FileNotFoundError(remote_path)

        def retrbinary(self, cmd, callback):
            retr_cmds.append(cmd)
            callback(b"dummy-3mf-content")

        def quit(self):
            return None

    monkeypatch.setattr(driver_module, "ImplicitFTP_TLS", FakeFTP)

    result_path = driver._get_3mf_file_path(
        {"gcode_file": "/data/Metadata/plate_1.gcode"}
    )

    try:
        assert result_path is not None
        assert probes[0] == "/cache/plate_1.gcode.3mf"
        assert retr_cmds == ["RETR /cache/plate_1.gcode.3mf"]
    finally:
        if result_path and os.path.exists(result_path):
            os.remove(result_path)

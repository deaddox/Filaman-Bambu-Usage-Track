"""Tests for process-local single-instance hardening in Driver."""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from bambulab.driver import Driver


@pytest.fixture(autouse=True)
def clear_driver_instance_registry():
    with Driver._instance_registry_lock:
        Driver._active_instance_claims.clear()
    yield
    with Driver._instance_registry_lock:
        Driver._active_instance_claims.clear()


def _driver(printer_id: int, *, host: str = "10.0.0.10", serial: str = "SERIAL-1") -> Driver:
    return Driver(
        printer_id=printer_id,
        config={
            "host": host,
            "serial": serial,
            "access_code": "secret",
        },
        emitter=lambda payload: None,
    )


def test_claim_rejects_duplicate_printer_id():
    first = _driver(1, host="10.0.0.10", serial="SERIAL-1")
    second = _driver(1, host="10.0.0.11", serial="SERIAL-2")

    first._claim_instance_identity()

    with pytest.raises(RuntimeError, match="printer 1"):
        second._claim_instance_identity()


def test_claim_rejects_duplicate_serial():
    first = _driver(1, host="10.0.0.10", serial="SERIAL-1")
    second = _driver(2, host="10.0.0.11", serial="SERIAL-1")

    first._claim_instance_identity()

    with pytest.raises(RuntimeError, match="serial:serial-1"):
        second._claim_instance_identity()


def test_claim_rejects_duplicate_host_when_serial_missing():
    first = _driver(1, host="10.0.0.10", serial="")
    second = _driver(2, host="10.0.0.10", serial="")

    first._claim_instance_identity()

    with pytest.raises(RuntimeError, match="host:10.0.0.10"):
        second._claim_instance_identity()


def test_release_allows_reclaim():
    first = _driver(1, host="10.0.0.10", serial="SERIAL-1")
    second = _driver(1, host="10.0.0.10", serial="SERIAL-1")

    first._claim_instance_identity()
    first._release_instance_identity()

    second._claim_instance_identity()

    assert second._instance_claim_keys == (
        "printer_id:1",
        "serial:serial-1",
    )


def test_claim_allows_same_host_with_distinct_serials():
    first = _driver(1, host="10.0.0.10", serial="SERIAL-1")
    second = _driver(2, host="10.0.0.10", serial="SERIAL-2")

    first._claim_instance_identity()
    second._claim_instance_identity()

    assert first._instance_claim_keys == ("printer_id:1", "serial:serial-1")
    assert second._instance_claim_keys == ("printer_id:2", "serial:serial-2")


def test_release_ignores_unclaimed_driver():
    driver = _driver(1)

    driver._release_instance_identity()

    assert driver._instance_claim_keys == ()


def test_prefetch_deduplicates_in_flight_downloads():
    driver = _driver(1)
    driver._tracking_active = True
    driver._tracking_print_context = {"subtask_name": "PETG"}
    driver._tracking_prefetch_delay_seconds = 0
    driver._tracking_prefetch_generation = 7

    calls = {"downloads": 0, "extracts": 0}

    def fake_get_3mf_file_path(context):
        calls["downloads"] += 1
        time.sleep(0.05)
        return "prefetched.3mf"

    def fake_extract_total(path):
        calls["extracts"] += 1
        return 2.37

    driver._get_3mf_file_path = fake_get_3mf_file_path
    driver._extract_3mf_total_used_grams_from_file = fake_extract_total

    async def _run():
        task1 = asyncio.create_task(driver._prefetch_tracking_metadata_after_delay(7))
        task2 = asyncio.create_task(driver._prefetch_tracking_metadata_after_delay(7))
        await asyncio.sleep(0)
        driver._tracking_prefetch_task = task1
        await asyncio.gather(task1, task2)

    asyncio.run(_run())

    assert calls == {"downloads": 1, "extracts": 1}
    assert driver._tracking_prefetched_total_grams == pytest.approx(2.37)
    assert driver._tracking_prefetched_3mf_path == "prefetched.3mf"
    assert driver._tracking_prefetch_in_flight is False


def test_cross_process_prefetch_lock_blocks_second_driver(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    first = _driver(1, host="10.0.0.10", serial="SERIAL-1")
    second = _driver(1, host="10.0.0.11", serial="SERIAL-2")

    assert first._acquire_cross_process_prefetch_lock() is True
    assert second._acquire_cross_process_prefetch_lock() is False

    first._release_cross_process_prefetch_lock()
    assert second._acquire_cross_process_prefetch_lock() is True
    second._release_cross_process_prefetch_lock()


def test_cross_process_prefetch_lock_reclaims_stale_file(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    driver = _driver(6)
    driver._tracking_prefetch_lock_stale_seconds = 1

    lock_path = driver._build_prefetch_lock_path()
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write("stale")

    stale_time = time.time() - 120
    os.utime(lock_path, (stale_time, stale_time))

    assert driver._acquire_cross_process_prefetch_lock() is True
    assert driver._tracking_prefetch_lock_path == lock_path
    driver._release_cross_process_prefetch_lock()


def test_health_reports_connected_within_disconnect_grace(monkeypatch):
    driver = _driver(7)
    driver._running = True
    driver._connected = False
    driver._disconnect_grace_seconds = 20
    monkeypatch.setattr("time.time", lambda: 1_000.0)
    driver._last_disconnected_at = 990.0

    health = driver.health()

    assert health["connected"] is True
    assert health["connected_raw"] is False


def test_health_reports_disconnected_after_disconnect_grace(monkeypatch):
    driver = _driver(7)
    driver._running = True
    driver._connected = False
    driver._disconnect_grace_seconds = 20
    monkeypatch.setattr("time.time", lambda: 1_000.0)
    driver._last_disconnected_at = 900.0

    health = driver.health()

    assert health["connected"] is False
    assert health["connected_raw"] is False
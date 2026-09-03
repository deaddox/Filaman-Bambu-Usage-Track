from __future__ import annotations

from pathlib import Path

import pytest

from bambulab.driver import Driver


MODELS_DIR = Path(__file__).parent.parent / "models"
MODEL_PATHS = sorted(MODELS_DIR.rglob("*.3mf"))


def _slot_id_to_slot_index(slot_id: int) -> str:
    zero_based = slot_id - 1
    ams_id, tray_id = divmod(zero_based, 4)
    return f"{ams_id}-{tray_id}"


def _build_entries_from_slots(per_slot_data: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "slot_index": _slot_id_to_slot_index(int(slot["slot_id"])),
            "was_used": True,
        }
        for slot in per_slot_data
    ]


def test_model_fixtures_exist() -> None:
    assert MODELS_DIR.is_dir()
    assert MODEL_PATHS


@pytest.mark.parametrize("model_path", MODEL_PATHS, ids=lambda path: path.name)
def test_all_models_have_consistent_estimation_inputs(model_path: Path) -> None:
    total_grams = Driver._extract_3mf_total_used_grams_from_file(str(model_path))
    per_slot_data = Driver._extract_per_slot_filament_usage_from_file(str(model_path))
    layer_usage = Driver._extract_layer_gcode_usage_from_file(str(model_path))

    assert total_grams is not None
    assert total_grams > 0

    assert per_slot_data is not None
    assert per_slot_data

    slot_ids = set()
    slot_total = 0.0
    for slot in per_slot_data:
        slot_id = int(slot["slot_id"])
        used_g = float(slot["used_g"])
        density = float(slot["density"])
        diameter = float(slot["diameter"])

        assert slot_id > 0
        assert slot_id not in slot_ids
        assert used_g > 0
        assert density > 0
        assert diameter > 0

        slot_ids.add(slot_id)
        slot_total += used_g

    assert slot_total == pytest.approx(total_grams, abs=0.01)

    assert layer_usage is not None
    assert layer_usage

    for filament_id, layers in layer_usage.items():
        assert filament_id >= 0
        assert layers

        previous_layer = None
        previous_cumulative = 0.0
        for layer_number, cumulative_mm in layers:
            assert layer_number >= 0
            assert cumulative_mm > 0
            if previous_layer is not None:
                assert layer_number >= previous_layer
            assert cumulative_mm >= previous_cumulative
            previous_layer = layer_number
            previous_cumulative = cumulative_mm


@pytest.mark.parametrize("model_path", MODEL_PATHS, ids=lambda path: path.name)
def test_completed_estimator_assignment_matches_per_slot_usage(model_path: Path) -> None:
    per_slot_data = Driver._extract_per_slot_filament_usage_from_file(str(model_path))

    assert per_slot_data is not None

    per_slot_usage = {
        int(slot["slot_id"]): float(slot["used_g"])
        for slot in per_slot_data
    }
    entries = _build_entries_from_slots(per_slot_data)

    Driver._apply_3mf_grams_to_entries(
        entries,
        per_slot_usage,
        layer_grams_per_slot=None,
        per_slot_data=per_slot_data,
        mapping_array=None,
        end_state="completed",
        progress_delta=100.0,
    )

    assigned_total = 0.0
    for entry, slot in zip(entries, per_slot_data):
        expected_grams = float(slot["used_g"])
        assert entry.get("source_3mf") is True
        assert entry.get("grams_from_3mf") == pytest.approx(expected_grams, abs=0.01)
        assigned_total += float(entry["grams_from_3mf"])

    assert assigned_total == pytest.approx(sum(per_slot_usage.values()), abs=0.01)
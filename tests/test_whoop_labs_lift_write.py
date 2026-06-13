"""Pure-function tests for native labs ingestion + the Whoop lift write path."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ingest_whoop_private import labs, lift_write
from mcp_server import labs_write_tools as LW


# ---- labs transform --------------------------------------------------------
def test_transform_labs_summary():
    fx = Path(__file__).parent / "fixtures" / "whoop_private" / "labs_summary.json"
    payload = json.loads(fx.read_text())
    meta, fact_rows, stubs = labs.transform_labs_summary(payload, lab_provider="quest")

    assert meta["test_id"] == "9132cb63-4ee4-4866-ad62-b54486728963"
    assert meta["test_name"] == "Back Pain Test"
    assert meta["test_date"] == date(2026, 5, 30)
    # The fixture has 3 tested + 1 UNAVAILABLE -> 3 rows, UNAVAILABLE dropped.
    assert len(fact_rows) == 3
    assert len(stubs) == 3
    r = next(x for x in fact_rows if x["biomarker_id"] == "alanine_aminotransferase")
    assert r["value_numeric"] == 13.0
    assert r["unit"] == "U/L"
    assert r["status_type"] == "OPTIMAL"
    assert r["source"] == "whoop"
    assert r["lab_provider"] == "quest"
    stub = next(s for s in stubs if s["biomarker_id"] == "alanine_aminotransferase")
    assert stub["optimal_low"] == 6 and stub["optimal_high"] == 35


def test_transform_labs_summary_no_test_id():
    assert labs.transform_labs_summary({"response": {}}) == (None, [], [])


# ---- labs status derivation ------------------------------------------------
def test_status_from_ranges():
    assert LW._status_from_ranges(12, 5, 20, 5, 30) == "OPTIMAL"
    assert LW._status_from_ranges(25, 5, 20, 5, 30) == "SUFFICIENT"
    assert LW._status_from_ranges(99, 5, 20, 5, 30) == "OUT_OF_RANGE"
    assert LW._status_from_ranges(12, None, None, None, None) is None  # no ranges -> unknown


# ---- lift write: exercise_details + sets + groups --------------------------
_LIBRARY = {
    "BENCHPRESS_BARBELL": {
        "exercise_id": "BENCHPRESS_BARBELL", "name": "Bench Press - Barbell",
        "push_core_name": "BENCHPRESS_BARBELL", "custom_exercise": False,
        "muscle_groups": ["CHEST"], "equipment": "BARBELL",
        "movement_pattern": "HORIZONTAL_PRESS", "laterality": "BILATERAL",
        "volume_input_format": "WEIGHT", "exercise_type": "STRENGTH",
    },
    "F2A3DDD5-2973-4926-8D85-A89D773CAFA3": {
        "exercise_id": "F2A3DDD5-2973-4926-8D85-A89D773CAFA3", "name": "Abductor Inner",
        "push_core_name": "SEATEDLEGCURL_PULLEYMACHINE", "custom_exercise": True,
        "muscle_groups": ["LEGS"], "equipment": "MACHINE",
        "movement_pattern": "OTHER", "laterality": "BILATERAL",
        "volume_input_format": "REPS", "exercise_type": "STRENGTH",
    },
}


def test_exercise_details_custom_uses_push_core_name():
    d = lift_write._exercise_details(_LIBRARY["F2A3DDD5-2973-4926-8D85-A89D773CAFA3"])
    # The custom's push_core_name is the official base, NOT its own UUID.
    assert d["exercise_id"] == "F2A3DDD5-2973-4926-8D85-A89D773CAFA3"
    assert d["push_core_name"] == "SEATEDLEGCURL_PULLEYMACHINE"
    assert d["muscle_groups"] == ["LEGS"]


def test_build_set_weight_lb_to_kg():
    from datetime import UTC, datetime
    s = lift_write._build_set({"reps": 5, "weight_lb": 135}, datetime(2026, 1, 1, tzinfo=UTC))
    assert s["number_of_reps"] == 5
    assert s["weight"] == pytest.approx(135 * 0.45359237, abs=0.01)
    assert "weightlifting_workout_set_id" in s


def test_build_workout_groups_custom_and_superset():
    from datetime import UTC, datetime
    exercises = [
        {"exercise_id": "BENCHPRESS_BARBELL", "group": 1, "sets": [{"reps": 5, "weight_lb": 135}]},
        {"exercise_id": "F2A3DDD5-2973-4926-8D85-A89D773CAFA3", "group": 1, "sets": [{"reps": 12, "weight_lb": 30}]},
        {"exercise_id": "BENCHPRESS_BARBELL", "sets": [{"reps": 8, "weight_lb": 95}]},  # solo
    ]
    groups, set_count, unknown = lift_write.build_workout_groups(
        exercises, _LIBRARY, datetime(1970, 1, 1, tzinfo=UTC)
    )
    assert unknown == []                      # custom resolved from library
    assert set_count == 3
    assert len(groups) == 2                   # one superset group (2 exercises) + one solo
    assert len(groups[0]["workout_exercises"]) == 2   # the superset
    assert len(groups[1]["workout_exercises"]) == 1   # the solo


def test_build_workout_groups_unknown_exercise():
    from datetime import UTC, datetime
    groups, _, unknown = lift_write.build_workout_groups(
        [{"exercise_id": "NOT_A_REAL_ID", "sets": [{"reps": 5}]}],
        _LIBRARY, datetime(1970, 1, 1, tzinfo=UTC),
    )
    assert unknown == ["NOT_A_REAL_ID"]
    assert groups == []

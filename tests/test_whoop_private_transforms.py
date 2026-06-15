"""Pure-function tests for ingest_whoop_private.transforms.

Fixtures are real Whoop private-API payload shapes:
  - trend_steps.json    : a full STEPS trend graph BFF (week/month/six_month).
  - behavior_impact.json: a trimmed behavior-impact tile list.
  - sleep_need.json     : a coaching-service/v2/sleepneed snapshot.

No DB / network — these assert the parsing contract end to end.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ingest_whoop_private import transforms


@pytest.fixture
def fx() -> Path:
    return Path(__file__).parent / "fixtures" / "whoop_private"


def _load(fx: Path, name: str) -> Any:
    return json.loads((fx / name).read_text())


# ---- helpers ---------------------------------------------------------------
def test_parse_metric_value_variants():
    assert transforms._parse_metric_value("5,442") == 5442.0
    assert transforms._parse_metric_value("47") == 47.0
    assert transforms._parse_metric_value("84%") == 84.0
    assert transforms._parse_metric_value("7:51") == pytest.approx(471.0)  # H:MM -> minutes
    assert transforms._parse_metric_value(None) is None
    assert transforms._parse_metric_value("--") is None
    assert transforms._parse_metric_value("n/a") is None


def test_parse_point_date_yearless_and_explicit():
    assert transforms._parse_point_date("MON, MAY 11", ref_year=2026) == date(2026, 5, 11)
    assert transforms._parse_point_date("FRI, DEC 12, 2025", ref_year=2026) == date(2025, 12, 12)
    assert transforms._parse_point_date("TUE, JUN 9", ref_year=2026) == date(2026, 6, 9)
    assert transforms._parse_point_date("garbage", ref_year=2026) is None
    assert transforms._parse_point_date(None, ref_year=2026) is None


def test_parse_pct():
    assert transforms._parse_pct("+5%") == 5.0
    assert transforms._parse_pct("-10%") == -10.0
    assert transforms._parse_pct("0%") == 0.0
    assert transforms._parse_pct(None) is None


# ---- trends ----------------------------------------------------------------
def test_transform_trend_points_real_steps(fx):
    payload = _load(fx, "trend_steps.json")
    rows = transforms.transform_trend_points(payload, "STEPS", date(2026, 6, 9))

    # The six_month segment spans ~180 distinct days; dedup keeps one row/day.
    assert len(rows) > 150
    by_day = {r["day"]: r for r in rows}
    assert len(by_day) == len(rows)  # no duplicate days

    # Spot-check known values from the fixture.
    assert by_day[date(2026, 6, 9)]["value"] == 6243.0
    assert by_day[date(2026, 5, 11)]["value"] == 4249.0
    assert by_day[date(2025, 12, 12)]["value"] == 6173.0  # prior-year, explicit year
    assert all(r["metric"] == "STEPS" for r in rows)
    assert by_day[date(2026, 6, 9)]["value_display"] == "6,243"


def test_transform_trend_points_empty():
    assert transforms.transform_trend_points({}, "STEPS", date(2026, 6, 9)) == []


def test_transform_trend_points_drops_future_dates():
    # Year-less weekly-bucket labels (seen on HR-zone payloads) can resolve past
    # end_date; a trend can't have data after its own window, so those drop.
    payload = {
        "week_time_segment": {
            "graph": {"plots": [{"plot": {"segments": [{"points": [
                {"data_scrubber_details": {"primary_contextual_display": "WED, JUN 3",
                                           "value_display": "100"}},
                {"data_scrubber_details": {"primary_contextual_display": "WED, DEC 31",
                                           "value_display": "999"}},
            ]}]}}]}
        }
    }
    rows = transforms.transform_trend_points(payload, "HR_ZONES_1_3", date(2026, 6, 9))
    days = {r["day"] for r in rows}
    assert date(2026, 6, 3) in days
    assert date(2026, 12, 31) not in days  # future-dated point dropped
    assert len(rows) == 1


def test_slim_trend_payload_shrinks_and_keeps_points(fx):
    payload = _load(fx, "trend_steps.json")
    slim = transforms.slim_trend_payload(payload, "STEPS", date(2026, 6, 9))
    assert slim["metric"] == "STEPS"
    assert "month_time_segment" in slim["segments"]
    assert len(slim["segments"]["month_time_segment"]["points"]) == 30
    # Slimmed payload must be a fraction of the original BFF size.
    assert len(json.dumps(slim)) < len(json.dumps(payload)) / 2


# ---- sleep need ------------------------------------------------------------
def test_transform_sleep_need(fx):
    payload = _load(fx, "sleep_need.json")
    row = transforms.transform_sleep_need(payload, date(2026, 6, 9))

    assert row["day"] == date(2026, 6, 9)
    assert row["total_need_ms"] == 28267937
    assert row["baseline_ms"] == 28100053
    assert row["strain_ms"] == 167884
    assert row["debt_ms"] == 0
    assert row["nap_credit_ms"] == 0
    assert row["smart_alarm_eligible"] is True
    assert row["schedule_state"] == "ON"
    # 31006800 ms / 60000 -> 516.8 min
    assert row["recommended_tib_minutes"] == pytest.approx(516.8, abs=0.1)


def test_transform_sleep_need_empty():
    assert transforms.transform_sleep_need({}, date(2026, 6, 9)) is None


# ---- behavior impact -------------------------------------------------------
def test_transform_behavior_impact(fx):
    payload = _load(fx, "behavior_impact.json")
    rows = transforms.transform_behavior_impact(payload, date(2026, 6, 9))

    # DIVIDER tile contributes nothing; the two impact tiles contribute 4 cards.
    assert len(rows) == 4
    by_name = {r["behavior_name"]: r for r in rows}

    pos = by_name["79%+ Sleep Performance"]
    assert pos["direction"] == "positive"
    assert pos["impact_pct"] == 5.0
    assert pos["has_sufficient_data"] is True
    assert pos["outcome"] == "recovery"

    assert by_name["Rest Day"]["direction"] == "neutral"
    assert by_name["4+ Strain"]["impact_pct"] == -10.0
    assert by_name["4+ Strain"]["direction"] == "negative"

    insuf = by_name["Alcohol"]
    assert insuf["direction"] == "insufficient"
    assert insuf["impact_pct"] is None
    assert insuf["has_sufficient_data"] is False
    assert insuf["yes_answer_count"] == 19
    assert insuf["no_answer_count"] == 60


def test_transform_behavior_impact_empty():
    assert transforms.transform_behavior_impact({}, date(2026, 6, 9)) == []


# ---- strength trainer (lift) -----------------------------------------------
def test_transform_lift_workout():
    rec = {
        "activity_id": "d40cedac-c7f8-4a20-8e23-6a9864071225",
        "date": "2026-06-06",
        "name": None,
        "duration_ms": 3052077,
        "strain": 13.757264,
        "msk_total_volume_kg": 3348,
        "msk_intensity_pct": 16,
        "exercise_count": 5,
        "set_count": 24,
        "exercises": [
            {"exercise_id": "BENCHPRESS_BARBELL", "set_count": 6, "total_reps": 30,
             "tonnage": 3700, "tonnage_units": "lbs"},
        ],
    }
    row = transforms.transform_lift_workout(rec)
    assert row["activity_id"] == "d40cedac-c7f8-4a20-8e23-6a9864071225"
    assert row["day"] == date(2026, 6, 6)
    assert row["total_volume_kg"] == 3348.0      # already kg, not converted
    assert row["set_count"] == 24
    assert row["exercise_count"] == 5
    assert row["intensity_pct"] == 16.0
    assert row["duration_minutes"] == pytest.approx(50.9, abs=0.1)  # 3052077 ms
    assert row["exercises"][0]["exercise_id"] == "BENCHPRESS_BARBELL"


def test_transform_lift_workout_missing_keys():
    assert transforms.transform_lift_workout({"date": "2026-06-06"}) is None  # no activity_id
    assert transforms.transform_lift_workout({"activity_id": "x"}) is None    # no date


def test_transform_cardio_details_sets():
    payload = {"weightlifting_cardio_details": {"weightlifting_exercises": {"exercise_summary": {
        "tonnage_display": "7,380",
        "exercise_card_groups": [
            {"cards": [{
                "exercise_id": "BENCHPRESS_BARBELL",
                "title_display": "Bench Press - Barbell",
                "volume_title_display": "REPS",
                "bottom_stats": {"tonnage_display": "3700"},
                "stat_rows": [
                    {"volume_display": "5", "weight_display": "95", "avg_hr_display": "130", "achievement_icon": None},
                    {"volume_display": "5", "weight_display": "135", "avg_hr_display": "139", "achievement_icon": "BADGE_BRONZE"},
                ],
            }]},
            {"cards": [{
                "exercise_id": "ROWS_MACHINE",
                "title_display": "Rowing",
                "volume_title_display": "TIME",
                "stat_rows": [
                    {"volume_display": "1:00", "weight_display": "0", "avg_hr_display": "120", "achievement_icon": None},
                ],
            }]},
        ],
    }}}}
    wk, sets = transforms.transform_cardio_details(payload, "act-1", date(2026, 6, 6))

    assert wk["activity_id"] == "act-1"
    assert wk["set_count"] == 3
    assert wk["exercise_count"] == 2
    assert wk["total_volume_kg"] == pytest.approx(7380 * 0.45359237, abs=1)

    bench = [s for s in sets if s["exercise_id"] == "BENCHPRESS_BARBELL"]
    assert [s["set_index"] for s in bench] == [1, 2]
    assert bench[0]["reps"] == 5 and bench[0]["weight_lb"] == 95.0
    assert bench[0]["weight_kg"] == pytest.approx(43.09, abs=0.1)
    assert bench[0]["volume_type"] == "REPS" and bench[0]["is_pr"] is False
    assert bench[1]["is_pr"] is True  # achievement badge -> PR flag

    row = next(s for s in sets if s["exercise_id"] == "ROWS_MACHINE")
    assert row["volume_type"] == "TIME"
    assert row["time_seconds"] == 60 and row["reps"] is None
    assert row["weight_lb"] == 0.0  # bodyweight/timed


def test_transform_cardio_details_non_strength():
    # A cardio-only workout has no weightlifting breakdown.
    assert transforms.transform_cardio_details({"graph_response": {}}, "a", date(2026, 6, 6)) == (None, [])


def test_extract_activity_ids_from_strain_feed():
    # Mirrors the real deep-dive/strain SDUI shape: activity ids on tile destinations.
    payload = {
        "sections": [
            {"items": [{"content": {"destination": {"parameters": {
                "activity_id": "a939d74a-bfa2-4383-89bb-ebda43e84bbe"}}}}]},
            {"items": [
                {"content": {"destination": {"parameters": {
                    "activity_id": "0e8da92e-4df2-49c2-83f9-d1e3ad9d3979"}}}},
                # duplicate should be collapsed
                {"content": {"destination": {"parameters": {
                    "activity_id": "a939d74a-bfa2-4383-89bb-ebda43e84bbe"}}}},
            ]},
        ]
    }
    ids = transforms.extract_activity_ids(payload)
    assert ids == ["a939d74a-bfa2-4383-89bb-ebda43e84bbe",
                   "0e8da92e-4df2-49c2-83f9-d1e3ad9d3979"]
    assert transforms.extract_activity_ids({}) == []
    # non-uuid 'activity_id' values are ignored
    assert transforms.extract_activity_ids({"activity_id": "not-a-uuid"}) == []

"""Pure-function tests for ingest_whoop.transforms.

Catches the failure modes the spec calls out: HRV unit conversion (seconds vs
ms), sleep-stage unit conversion (ms → minutes), nullable upstream fields.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ingest_whoop import transforms


@pytest.fixture
def fx() -> Path:
    return Path(__file__).parent / "fixtures" / "whoop"


def _load(fx: Path, name: str) -> dict:
    return json.loads((fx / name).read_text())


# ---- HRV unit conversion ---------------------------------------------------
def test_hrv_seconds_converted_to_ms():
    # API returned seconds (0.0612 s -> 61.2 ms)
    assert transforms.hrv_to_ms(0.0612) == pytest.approx(61.2)


def test_hrv_already_ms_passthrough():
    assert transforms.hrv_to_ms(61.2) == pytest.approx(61.2)


def test_hrv_none_is_none():
    assert transforms.hrv_to_ms(None) is None


# ---- recovery transform ----------------------------------------------------
def test_transform_recovery_basic(fx):
    api = _load(fx, "recovery.json")
    row = transforms.transform_recovery(api)

    assert row["cycle_id"] == 12345678
    assert row["sleep_id"] == "fc3e2c8a-8e3a-4b5d-9b1a-1234567890ab"
    assert row["recovery_score"] == 72
    assert row["resting_heart_rate"] == 54
    # 0.0612 s → 61.2 ms
    assert row["hrv_rmssd_ms"] == pytest.approx(61.2)
    assert row["spo2_percentage"] == pytest.approx(96.5)
    assert row["skin_temp_celsius"] == pytest.approx(33.7)
    assert row["user_calibrating"] is False
    # 2025-04-01T11:30 UTC → 2025-04-01 in America/New_York (07:30 EDT)
    assert row["day"].isoformat() == "2025-04-01"


def test_transform_recovery_missing_score_doesnt_crash():
    """A SCORED-pending recovery has no score block. Should produce nulls,
    not raise."""
    api = {"cycle_id": 99, "sleep_id": None, "created_at": "2025-04-01T11:30:00.000Z",
           "score_state": "PENDING_SCORE"}
    row = transforms.transform_recovery(api)
    assert row["cycle_id"] == 99
    assert row["recovery_score"] is None
    assert row["hrv_rmssd_ms"] is None


# ---- sleep transform -------------------------------------------------------
def test_transform_sleep_unit_conversion(fx):
    api = _load(fx, "sleep.json")
    row = transforms.transform_sleep(api)

    # 27_900_000 ms / 60_000 = 465.0 min
    assert row["total_in_bed_min"] == pytest.approx(465.0)
    # 8_100_000 ms / 60_000 = 135.0 min
    assert row["total_rem_min"] == pytest.approx(135.0)
    # 5_400_000 ms / 60_000 = 90.0 min
    assert row["total_slow_wave_min"] == pytest.approx(90.0)
    # 1_800_000 ms / 60_000 = 30.0 min
    assert row["total_awake_min"] == pytest.approx(30.0)
    assert row["sleep_cycle_count"] == 5
    assert row["disturbance_count"] == 7
    assert row["sleep_efficiency_pct"] == pytest.approx(93.5)
    assert row["is_nap"] is False


def test_transform_sleep_nap_flag():
    api = {
        "id": "11111111-1111-1111-1111-111111111111",
        "start": "2025-04-01T18:00:00.000Z",
        "end": "2025-04-01T18:30:00.000Z",
        "nap": True,
        "score": {"stage_summary": {"total_in_bed_time_milli": 1800000}},
    }
    row = transforms.transform_sleep(api)
    assert row["is_nap"] is True
    assert row["total_in_bed_min"] == pytest.approx(30.0)


# ---- workout transform -----------------------------------------------------
def test_transform_workout(fx):
    api = _load(fx, "workout.json")
    row = transforms.transform_workout(api)

    assert row["sport_name"] == "Running"
    assert row["strain"] == pytest.approx(13.8)
    assert row["kilojoules"] == 2050
    assert row["distance_meters"] == pytest.approx(8500.0)
    # Zone 3 = 1_200_000 ms = 20.0 min
    assert row["zone_three_min"] == pytest.approx(20.0)
    # Zone 5 = 120_000 ms = 2.0 min
    assert row["zone_five_min"] == pytest.approx(2.0)


# ---- cycle transform -------------------------------------------------------
def test_transform_cycle(fx):
    api = _load(fx, "cycle.json")
    row = transforms.transform_cycle(api)

    assert row["cycle_id"] == 12345678
    assert row["scaled_strain"] == pytest.approx(14.2)
    assert row["day_kilojoules"] == 12500
    assert row["max_heart_rate"] == 178

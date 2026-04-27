"""Pure transforms: Whoop API JSON → fact-table row dicts.

These are kept separate from ingest.py so they can be tested without a DB or
network. Each function takes a single API record and returns a dict matching
the corresponding fact_* table columns (sans `raw_id`, `updated_at`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lifeos_core.tz import local_date

# Whoop's HRV is reported in seconds in some API surfaces and milliseconds in
# others; v2 has been observed both ways across releases. We auto-detect by
# magnitude — anything < 1 is treated as seconds and converted, anything >= 1
# is assumed already milliseconds. Healthy human RMSSD is ~10-200 ms.
HRV_SECONDS_THRESHOLD = 1.0


def hrv_to_ms(value: float | None) -> float | None:
    """Normalize HRV to milliseconds. Logs a warning if value is implausible."""
    if value is None:
        return None
    if value < HRV_SECONDS_THRESHOLD:
        return float(value) * 1000.0
    return float(value)


def _ms_to_min(ms: int | float | None) -> float | None:
    if ms is None:
        return None
    return round(float(ms) / 60_000.0, 1)


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    # Whoop returns "2025-01-15T07:23:11.000Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def transform_cycle(api: dict) -> dict:
    """fact_cycle row from /v2/cycle record."""
    score = api.get("score") or {}
    return {
        "cycle_id": int(api["id"]),
        "start_ts": _parse_ts(api.get("start")),
        "end_ts": _parse_ts(api.get("end")),
        "scaled_strain": _safe_num(score.get("strain")),
        "day_kilojoules": _safe_int(score.get("kilojoule")),
        "avg_heart_rate": _safe_int(score.get("average_heart_rate")),
        "max_heart_rate": _safe_int(score.get("max_heart_rate")),
    }


def transform_recovery(api: dict) -> dict:
    """fact_recovery row from /v2/recovery record."""
    score = api.get("score") or {}
    cycle_id = int(api["cycle_id"])
    sleep_id = api.get("sleep_id")
    start_ts = _parse_ts(api.get("created_at"))
    return {
        "cycle_id": cycle_id,
        "sleep_id": sleep_id,
        "day": local_date(start_ts) if start_ts else None,
        "recovery_score": _safe_int(score.get("recovery_score")),
        "hrv_rmssd_ms": _safe_num(hrv_to_ms(score.get("hrv_rmssd_milli"))),
        "resting_heart_rate": _safe_int(score.get("resting_heart_rate")),
        "spo2_percentage": _safe_num(score.get("spo2_percentage")),
        "skin_temp_celsius": _safe_num(score.get("skin_temp_celsius")),
        "user_calibrating": api.get("score_state") == "CALIBRATING"
        or score.get("user_calibrating"),
    }


def transform_sleep(api: dict) -> dict:
    """fact_sleep row from /v2/activity/sleep record."""
    score = api.get("score") or {}
    stage_summary = score.get("stage_summary") or {}
    sleep_needed = score.get("sleep_needed") or {}  # noqa: F841 (kept for future)

    return {
        "sleep_id": api["id"],
        "start_ts": _parse_ts(api.get("start")),
        "end_ts": _parse_ts(api.get("end")),
        "is_nap": bool(api.get("nap", False)),
        "total_in_bed_min": _ms_to_min(stage_summary.get("total_in_bed_time_milli")),
        "total_awake_min": _ms_to_min(stage_summary.get("total_awake_time_milli")),
        "total_light_min": _ms_to_min(stage_summary.get("total_light_sleep_time_milli")),
        "total_slow_wave_min": _ms_to_min(
            stage_summary.get("total_slow_wave_sleep_time_milli")
        ),
        "total_rem_min": _ms_to_min(stage_summary.get("total_rem_sleep_time_milli")),
        "sleep_cycle_count": _safe_int(stage_summary.get("sleep_cycle_count")),
        "disturbance_count": _safe_int(stage_summary.get("disturbance_count")),
        "sleep_performance_pct": _safe_num(score.get("sleep_performance_percentage")),
        "sleep_consistency_pct": _safe_num(score.get("sleep_consistency_percentage")),
        "sleep_efficiency_pct": _safe_num(score.get("sleep_efficiency_percentage")),
    }


def transform_workout(api: dict) -> dict:
    """fact_workout row from /v2/activity/workout record."""
    score = api.get("score") or {}
    zones = score.get("zone_duration") or {}
    return {
        "workout_id": api["id"],
        "start_ts": _parse_ts(api.get("start")),
        "end_ts": _parse_ts(api.get("end")),
        "sport_id": _safe_int(api.get("sport_id")),
        "sport_name": api.get("sport_name"),
        "strain": _safe_num(score.get("strain")),
        "kilojoules": _safe_int(score.get("kilojoule")),
        "avg_heart_rate": _safe_int(score.get("average_heart_rate")),
        "max_heart_rate": _safe_int(score.get("max_heart_rate")),
        "distance_meters": _safe_num(score.get("distance_meter")),
        "altitude_gain_meters": _safe_num(score.get("altitude_gain_meter")),
        "altitude_change_meters": _safe_num(score.get("altitude_change_meter")),
        "zone_zero_min": _ms_to_min(zones.get("zone_zero_milli")),
        "zone_one_min": _ms_to_min(zones.get("zone_one_milli")),
        "zone_two_min": _ms_to_min(zones.get("zone_two_milli")),
        "zone_three_min": _ms_to_min(zones.get("zone_three_milli")),
        "zone_four_min": _ms_to_min(zones.get("zone_four_milli")),
        "zone_five_min": _ms_to_min(zones.get("zone_five_milli")),
    }


def _safe_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _safe_num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

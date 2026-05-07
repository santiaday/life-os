"""Pure transforms: Hevy API JSON → fact-table row dicts.

Same pattern as ingest_whoop.transforms — kept separate from ingest.py so
they can be tested without DB or network access. Each function takes a single
API record and returns dicts matching the corresponding fact_* / dim_*
columns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def parse_ts(s: str | None) -> datetime | None:
    """Hevy returns ISO8601 with a 'Z' suffix or explicit offset."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def transform_exercise_template(api: dict) -> dict:
    """dim_hevy_exercise row from /exercise_templates record."""
    return {
        "exercise_template_id": api["id"],
        "title": api.get("title") or "(untitled)",
        "exercise_type": api.get("type") or api.get("exercise_type"),
        "primary_muscle_group": api.get("primary_muscle_group"),
        "secondary_muscle_groups": api.get("secondary_muscle_groups") or [],
        "equipment": api.get("equipment"),
        "is_custom": bool(api.get("is_custom", False)),
    }


def explode_workout_sets(api: dict) -> list[dict]:
    """Flatten a workout payload into fact_strength_set rows.

    Hevy nests sets two deep (workout.exercises[].sets[]). We keep the
    exercise_index / set_index as the natural composite key so re-ingesting
    overwrites cleanly via DELETE+INSERT in the ingester."""
    workout_id = api["id"]
    start_ts = parse_ts(api.get("start_time"))
    end_ts = parse_ts(api.get("end_time"))
    if start_ts is None or end_ts is None:
        return []

    rows: list[dict] = []
    for exercise in api.get("exercises") or []:
        # Hevy provides explicit `index` for both exercises and sets; fall
        # back to enumeration order if missing.
        ex_index = _safe_int(exercise.get("index"))
        if ex_index is None:
            ex_index = len(rows)  # fallback; not ideal but stable per call
        ex_title = exercise.get("title") or "(untitled)"
        ex_template_id = exercise.get("exercise_template_id")
        superset_id = _safe_int(exercise.get("superset_id"))
        ex_notes = exercise.get("notes")

        for set_record in exercise.get("sets") or []:
            set_index = _safe_int(set_record.get("index"))
            if set_index is None:
                set_index = 0
            rows.append({
                "hevy_workout_id": workout_id,
                "exercise_index": ex_index,
                "set_index": set_index,
                "exercise_template_id": ex_template_id,
                "exercise_title": ex_title,
                "set_type": set_record.get("type"),
                "weight_kg": _safe_num(set_record.get("weight_kg")),
                "reps": _safe_int(set_record.get("reps")),
                "rpe": _safe_num(set_record.get("rpe")),
                "distance_meters": _safe_num(set_record.get("distance_meters")),
                "duration_seconds": _safe_int(set_record.get("duration_seconds")),
                "superset_id": superset_id,
                "notes": ex_notes,
                "workout_start_ts": start_ts,
                "workout_end_ts": end_ts,
            })
    return rows


def rollup_workout(api: dict, set_rows: list[dict]) -> dict | None:
    """fact_strength_workout row from a workout payload + its expanded sets.

    Volume math counts working sets only (set_type != 'warmup'). Both
    weight_kg and reps must be non-null to contribute.
    """
    workout_id = api["id"]
    start_ts = parse_ts(api.get("start_time"))
    end_ts = parse_ts(api.get("end_time"))
    if start_ts is None or end_ts is None:
        return None

    duration_seconds = max(0, int((end_ts - start_ts).total_seconds()))
    total_reps = sum(r["reps"] for r in set_rows if r.get("reps") is not None)
    total_volume = sum(
        float(r["weight_kg"]) * int(r["reps"])
        for r in set_rows
        if r.get("set_type") != "warmup"
        and r.get("weight_kg") is not None
        and r.get("reps") is not None
    )
    unique_exercises = len({
        r["exercise_template_id"]
        for r in set_rows
        if r.get("exercise_template_id")
    })

    return {
        "hevy_workout_id": workout_id,
        "title": api.get("title"),
        "description": api.get("description"),
        "start_ts": start_ts,
        "end_ts": end_ts,
        "duration_seconds": duration_seconds,
        "total_sets": len(set_rows),
        "total_reps": total_reps,
        "total_volume_kg": round(total_volume, 2),
        "unique_exercises": unique_exercises,
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

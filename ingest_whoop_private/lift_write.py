"""Whoop Strength Trainer WRITE helpers: create custom exercises, save workout
templates, and log workouts — with full custom-exercise support.

The totem MCP can't put a custom exercise into a template/log because it builds
each exercise_details block from a STATIC bundle of the 372 official exercises;
a custom exercise (random-UUID id) isn't in that map, so it's rejected as
"unknown". We instead fetch the user's full library live from
GET /weightlifting-service/v2/exercise (387 exercises incl. all customs, each
with complete metadata) and denormalize exercise_details from that — so official
and custom exercises work identically.

Weights are accepted in POUNDS (the user's unit) and converted to kg for the API
body, which stores kilograms.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

LB_TO_KG = 0.45359237
TEMPLATE_PATH = "/weightlifting-service/v3/workout-template"
ACTIVITY_PATH = "/weightlifting-service/v2/weightlifting-workout/activity"
CUSTOM_EXERCISE_PATH = "/weightlifting-service/v2/custom-exercise"
LIBRARY_PATH = "/weightlifting-service/v2/exercise"


def fetch_exercise_library(client) -> dict[str, dict]:
    """{exercise_id: metadata} for every exercise the user has — official AND
    custom — from the live library. The single source for exercise_details."""
    data = client.exercise_library()
    out: dict[str, dict] = {}
    for ex in (data.get("exercises") if isinstance(data, dict) else None) or []:
        eid = ex.get("exercise_id")
        if eid:
            out[eid] = ex
    return out


def _exercise_details(meta: dict) -> dict:
    """Denormalize one library row into the exercise_details block the
    template/activity POST validator requires. Works for official + custom."""
    return {
        "exercise_id": meta["exercise_id"],
        "push_core_name": meta.get("push_core_name") or meta["exercise_id"],
        "name": meta.get("name"),
        "muscle_groups": meta.get("muscle_groups") or [],
        "translated_muscle_groups": meta.get("translated_muscle_groups"),
        "equipment": meta.get("equipment"),
        "translated_equipment": meta.get("translated_equipment"),
        "movement_pattern": meta.get("movement_pattern"),
        "translated_movement_pattern": meta.get("translated_movement_pattern"),
        "laterality": meta.get("laterality") or "BILATERAL",
        "volume_input_format": meta.get("volume_input_format") or "REPS",
        "exercise_type": meta.get("exercise_type") or "STRENGTH",
        "trackable": meta.get("trackable", True),
        "training_types": meta.get("training_types") or [],
        "instructions": meta.get("instructions") or [],
        "deleted": meta.get("deleted", False),
        "created_at": meta.get("created_at") or "2022-01-01T00:00:00.000Z",
        "updated_at": meta.get("updated_at") or "2025-01-01T00:00:00.000Z",
    }


def _build_set(s: dict, cursor: datetime) -> dict:
    start = cursor.isoformat().replace("+00:00", "Z")
    end = (cursor + timedelta(milliseconds=100)).isoformat().replace("+00:00", "Z")
    weight_lb = s.get("weight_lb")
    weight_kg = round(weight_lb * LB_TO_KG, 4) if weight_lb is not None else 0
    out = {
        "during": f"['{start}','{end}')",
        "msk_total_volume_kg": 0,
        "weight": weight_kg,
        "number_of_reps": int(s.get("reps") or 0),
        "strap_location": "1",
        "strap_location_laterality": s.get("strap_location", "LEFT"),
        "weightlifting_workout_set_id": str(uuid.uuid4()).upper(),
    }
    if s.get("time_seconds") is not None:
        out["time_in_seconds"] = int(s["time_seconds"])
    return out


def build_workout_groups(
    exercises: list[dict], library: dict[str, dict], start: datetime
) -> tuple[list[dict], int, list[str]]:
    """Build workout_groups[]. Exercises sharing a `group` integer become one
    superset group (multiple workout_exercises); the rest are solo. Returns
    (groups, set_count, unknown_exercise_ids)."""
    unknown: list[str] = []
    set_count = 0
    cursor = start
    # Preserve order; collect supersets by group id.
    order: list = []
    by_group: dict = {}
    for ex in exercises:
        g = ex.get("group")
        if g is None:
            order.append(("solo", ex))
        elif g in by_group:
            by_group[g].append(ex)
        else:
            by_group[g] = [ex]
            order.append(("group", g))

    groups: list[dict] = []
    for kind, ref in order:
        members = [ref] if kind == "solo" else by_group[ref]
        workout_exercises = []
        for ex in members:
            meta = library.get(ex.get("exercise_id"))
            if not meta:
                unknown.append(ex.get("exercise_id"))
                continue
            sets = []
            for s in ex.get("sets") or []:
                sets.append(_build_set(s, cursor))
                cursor += timedelta(milliseconds=100)
                set_count += 1
            workout_exercises.append({
                "sets": sets,
                "exercise_details": _exercise_details(meta),
            })
        if workout_exercises:
            groups.append({"workout_exercises": workout_exercises})
    return groups, set_count, unknown


def _during(start: datetime, end: datetime) -> str:
    return (
        f"['{start.isoformat().replace('+00:00', 'Z')}',"
        f"'{end.isoformat().replace('+00:00', 'Z')}')"
    )


def save_template(
    client, name: str, exercises: list[dict], base_template_key: int | None = None,
    *, dry_run: bool = True,
) -> dict:
    """Create (or save-as) a Strength Trainer workout template. Handles custom
    exercises. exercises: [{exercise_id, group?, sets:[{reps, weight_lb?,
    time_seconds?}]}]."""
    library = fetch_exercise_library(client)
    groups, set_count, unknown = build_workout_groups(
        exercises, library, datetime(1970, 1, 1, tzinfo=UTC)
    )
    if unknown:
        return {"ok": False, "error": "unknown exercise_ids", "unknown_exercises": unknown,
                "hint": "Use a valid exercise_id from get_whoop_lift_prs or create the custom exercise first."}
    body: dict = {"name": name, "workout_groups": groups}
    if base_template_key is not None:
        body["workout_template_key"] = base_template_key
    if dry_run:
        return {"ok": True, "dry_run": True, "name": name,
                "exercise_count": len(exercises), "set_count": set_count,
                "custom_exercises": [e["exercise_id"] for e in exercises
                                     if library.get(e["exercise_id"], {}).get("custom_exercise")]}
    receipt = client.post(TEMPLATE_PATH, body)
    return {"ok": True, "created": True,
            "template_id": receipt.get("workout_template_key") or receipt.get("id"),
            "name": name, "exercise_count": len(exercises), "set_count": set_count}


def log_workout(
    client, name: str | None, exercises: list[dict],
    start: str | None = None, end: str | None = None, *, dry_run: bool = True,
) -> dict:
    """Log a finished strength workout. Handles custom exercises."""
    end_dt = datetime.fromisoformat(end) if end else datetime.now(UTC)
    start_dt = datetime.fromisoformat(start) if start else end_dt - timedelta(minutes=30)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=UTC)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=UTC)
    library = fetch_exercise_library(client)
    groups, set_count, unknown = build_workout_groups(exercises, library, start_dt)
    if unknown:
        return {"ok": False, "error": "unknown exercise_ids", "unknown_exercises": unknown}
    body = {
        "name": name or end_dt.date().isoformat(),
        "during": _during(start_dt, end_dt),
        "timezone": settings.LOCAL_TZ,
        "scaled_msk_strain_score": 0,
        "msk_total_volume_kg": 0,
        "msk_intensity_percent": 0,
        "raw_msk_strain_score": 0,
        "workout_groups": groups,
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "name": body["name"],
                "exercise_count": len(exercises), "set_count": set_count}
    receipt = client.post(ACTIVITY_PATH, body)
    return {"ok": True, "logged": True, "activity_id": receipt.get("id"),
            "exercise_count": len(exercises), "set_count": set_count}


def create_custom_exercise(
    client, name: str, base_exercise_id: str, muscle_groups: list[str],
    *, equipment: str = "OTHER", movement_pattern: str = "OTHER",
    laterality: str = "BILATERAL", volume_input_format: str = "REPS",
    exercise_type: str = "STRENGTH", dry_run: bool = True,
) -> dict:
    """Create a custom Strength Trainer exercise based on an official one
    (base_exercise_id). Returns the new exercise_id, which is immediately usable
    in save_template / log_workout."""
    library = fetch_exercise_library(client)
    base = library.get(base_exercise_id)
    if not base:
        return {"ok": False, "error": f"base_exercise_id {base_exercise_id} not found",
                "hint": "Use a valid official exercise_id."}
    new_id = str(uuid.uuid4()).upper()
    body = {
        "created_at": "", "updated_at": "", "exercise_id": new_id,
        "laterality": laterality, "exercise_type": exercise_type,
        "push_core_name": base_exercise_id, "training_types": [],
        "custom_exercise_info": {"linked_exercise": {
            "name": base.get("name"), "exercise_id": base_exercise_id,
            "image_url": base.get("image_url"),
        }},
        "trackable": True, "movement_pattern": movement_pattern,
        "instructions": [], "equipment": equipment, "name": name,
        "volume_input_format": volume_input_format, "muscle_groups": muscle_groups,
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "name": name,
                "base_exercise_id": base_exercise_id, "will_create_id": new_id}
    client.post(CUSTOM_EXERCISE_PATH, body)
    return {"ok": True, "created": True, "exercise_id": new_id, "name": name}

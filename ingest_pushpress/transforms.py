"""Transforms: API payload → DB rows.

Two parsers:

  parse_pushpress_ts  — PushPress returns timestamps as either:
                         "2026-05-07 00:00:00.0"   (their non-ISO format)
                         "2026-05-03T16:00:00Z"    (occasionally ISO)
                         None
                       This normalizes both to aware datetimes (UTC). The
                       non-ISO format omits a timezone — we treat it as UTC,
                       which is what the gym's scheduler stores internally.

  payload_hash        — stable sha256 of a sorted-keys payload. Used to skip
                        no-op upserts (cheap dedup so fetched_at doesn't
                        churn every run for unchanged programming).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone


def parse_pushpress_ts(s: str | None) -> datetime | None:
    """Robustly parse PushPress's timestamps. Returns timezone-aware UTC."""
    if s is None or s == "":
        return None
    raw = s.strip()
    # Try fromisoformat first (handles "2026-05-07T00:00:00", "...Z", "+00:00").
    try:
        if raw.endswith("Z"):
            raw_iso = raw[:-1] + "+00:00"
        else:
            raw_iso = raw
        dt = datetime.fromisoformat(raw_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    # Fallback for "YYYY-MM-DD HH:MM:SS.f" (their default format).
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_pushpress_date(s: str | None) -> date | None:
    """Pull a calendar date out of any of the timestamp formats above."""
    dt = parse_pushpress_ts(s)
    return dt.date() if dt else None


def payload_hash(obj: dict) -> str:
    """Stable hash for change detection. Sort keys recursively before hashing
    so insertion-order differences don't flap."""
    canonical = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def class_type_row(api: dict) -> dict:
    return {
        "uuid": api["uid"],
        "name": api.get("name") or "(unnamed)",
        "origin": api.get("origin"),
        "is_static": api.get("static"),
        "progressive": api.get("progressiveProgram"),
        "last_day_num": api.get("lastDayNum"),
    }


def synthesize_workout_uid(class_type_uuid: str, class_date: date) -> str:
    """Stable synthetic uid for sessions where the API didn't supply one. Used
    when a track (e.g. HYROX) returns a placeholder payload with workoutUid /
    uid both NULL — typically `{title: "Workout not yet available"}`. We still
    want to land the row so refresh_data is idempotent and Claude can see the
    placeholder, so we mint a deterministic id from (class_type, date)."""
    return f"synthetic:{class_type_uuid}:{class_date.isoformat()}"


def synthesize_part_uid(workout_uid: str, ordinal: int) -> str:
    """Stable synthetic part uid. Same idea as synthesize_workout_uid — used
    when the API leaves workoutPartUid NULL (placeholder parts on
    not-yet-published workouts)."""
    return f"synthetic:{workout_uid}:part-{ordinal}"


def session_row(
    api: dict,
    *,
    class_type_uuid: str,
    class_type_name: str | None,
    class_date: date,
) -> dict:
    """Build a fact_pushpress_session row from a top-level workout payload.
    `parts_count` and `divisions` are derived from the parts list so we can
    serve list views without joining."""
    parts = api.get("parts") or []
    divisions: set[str] = set()
    for p in parts:
        for d in (p.get("divisions") or []):
            if d:
                divisions.add(d)
    workout_uid = (
        api.get("workoutUid")
        or api.get("uid")
        or synthesize_workout_uid(class_type_uuid, class_date)
    )
    return {
        "workout_uid": workout_uid,
        "class_type_uuid": class_type_uuid,
        "class_type_name": class_type_name,
        "class_date": class_date,
        "title": api.get("title"),
        "workout_state": api.get("workoutState"),
        "origin": api.get("origin"),
        "parts_count": len(parts),
        "divisions": sorted(divisions) if divisions else None,
        "published_on": parse_pushpress_ts(api.get("publishedOn")),
        "publishing_date": parse_pushpress_ts(api.get("publishingDate")),
        "publishing_time": parse_pushpress_ts(api.get("publishingTime")),
        "created_date": parse_pushpress_ts(api.get("createdDate")),
        "updated_date": parse_pushpress_ts(api.get("updatedDate")),
    }


def part_rows(api: dict, *, class_type_uuid: str, class_date: date) -> list[dict]:
    """Explode parts[] into per-part rows. ordinal is the position in the
    array (PushPress preserves coach-authored order)."""
    workout_uid = (
        api.get("workoutUid")
        or api.get("uid")
        or synthesize_workout_uid(class_type_uuid, class_date)
    )
    out: list[dict] = []
    for i, p in enumerate(api.get("parts") or []):
        out.append({
            "part_uid": p.get("workoutPartUid") or synthesize_part_uid(workout_uid, i),
            "workout_uid": workout_uid,
            "class_type_uuid": class_type_uuid,
            "class_date": class_date,
            "ordinal": i,
            "title": p.get("title"),
            "workout_title": p.get("workoutTitle"),
            "description": p.get("description"),
            "score_type": p.get("scoreType"),
            "score_count": p.get("scoreCount"),
            "set_count": p.get("sets"),
            "default_reps": p.get("defaultReps"),
            "divisions": list(p.get("divisions") or []) or None,
            "unit": p.get("rawUnit"),
            "athletes_notes": p.get("athletesNotes"),
            "coaches_notes": p.get("coachesNotes"),
        })
    return out

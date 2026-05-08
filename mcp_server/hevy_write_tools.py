"""MCP write tools for Hevy strength training.

Three surfaces:

  log_strength_workout(...)       POST /v1/workouts → mirror into local DB
  update_strength_workout(id, ..) PUT  /v1/workouts/{id} → re-mirror local DB
  find_exercise_templates(query)  ILIKE search in dim_hevy_exercise — used by
                                  Claude to resolve free-text exercise names
                                  ('front squat') to the 8-char Hevy template id
                                  required by log_strength_workout.

After every successful POST/PUT we re-derive the affected fact rows from
the API response (raw_hevy_workout → fact_strength_set → fact_strength_workout)
so the rest of the warehouse stays consistent without waiting for the cron
to re-pull.

Returns the standard `_ok` / `_err` envelope from mcp_server.tools.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

from ingest_hevy import transforms
from ingest_hevy.client import HevyAPIError, HevyAuthError, HevyClient
from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.upsert import upsert_rows
from mcp_server.tools import _err, _ok, _serialize

log = get_logger(__name__)


VALID_SET_TYPES = {"warmup", "normal", "failure", "dropset"}
VALID_RPE = {6, 7, 7.5, 8, 8.5, 9, 9.5, 10}
EXERCISE_TEMPLATE_LIMIT = 50


# ---- find_exercise_templates ----------------------------------------------
def find_exercise_templates(
    query: str,
    primary_muscle_group: str | None = None,
    limit: int = EXERCISE_TEMPLATE_LIMIT,
) -> dict:
    """ILIKE search of dim_hevy_exercise. Run this BEFORE log_strength_workout
    when you have a free-text exercise name and need the 8-char
    exercise_template_id.

    Empty catalog? Run `python -m ingest_hevy catalog` (or call
    refresh_data('hevy')) to seed it from /v1/exercise_templates."""
    if not query or not query.strip():
        return _err("find_exercise_templates", ValueError("query is required"))

    where = ["title ILIKE %s"]
    params: list[Any] = [f"%{query}%"]
    if primary_muscle_group:
        where.append("primary_muscle_group = %s")
        params.append(primary_muscle_group)

    q = f"""
        SELECT exercise_template_id, title, exercise_type,
               primary_muscle_group, secondary_muscle_groups,
               equipment, is_custom
          FROM dim_hevy_exercise
         WHERE {" AND ".join(where)}
         ORDER BY is_custom DESC, length(title), title
         LIMIT %s
    """
    from lifeos_core.db import conn
    with conn() as c, c.cursor() as cur:
        cur.execute(q, [*params, limit])
        rows = _serialize(cur.fetchall())

    warnings: list[str] = []
    if not rows:
        warnings.append(
            "No exercise templates matched. Run refresh_data('hevy') to "
            "(re)seed dim_hevy_exercise from Hevy's /v1/exercise_templates."
        )
    return _ok("find_exercise_templates", rows, warnings=warnings)


# ---- log_strength_workout / update_strength_workout -----------------------
def log_strength_workout(
    title: str,
    start_time: str,
    end_time: str,
    exercises: list[dict],
    description: str | None = None,
    is_private: bool = True,
    dry_run: bool = False,
) -> dict:
    """Create a new Hevy workout via POST /v1/workouts and mirror the
    response into raw_hevy_workout / fact_strength_set / fact_strength_workout.

    `exercises` is a list of dicts shaped:
        {
          "exercise_template_id": "<8-char id from find_exercise_templates>",
          "notes": "optional",
          "superset_id": null | int,
          "sets": [
            {"type": "warmup|normal|failure|dropset",
             "weight_kg": 38.6, "reps": 5,
             "rpe": null | 6|7|7.5|8|8.5|9|9.5|10,
             "distance_meters": null, "duration_seconds": null,
             "custom_metric": null},
            ...
          ]
        }

    `start_time` / `end_time` accept ISO8601 strings (YYYY-MM-DDTHH:MM:SSZ
    or with timezone offset). Pass `dry_run=True` to validate the payload
    without sending it to Hevy.
    """
    return _send_workout(
        method="POST",
        workout_id=None,
        title=title,
        start_time=start_time,
        end_time=end_time,
        exercises=exercises,
        description=description,
        is_private=is_private,
        dry_run=dry_run,
    )


def update_strength_workout(
    hevy_workout_id: str,
    title: str,
    start_time: str,
    end_time: str,
    exercises: list[dict],
    description: str | None = None,
    is_private: bool = True,
    dry_run: bool = False,
) -> dict:
    """Overwrite an existing Hevy workout via PUT /v1/workouts/{id}. Body
    shape is identical to log_strength_workout — Hevy replaces the whole
    workout, no partial-update path. Use get_strength_workouts (or fetch
    the local raw_hevy_workout.payload) to read the current state first
    if you only want to change one field."""
    return _send_workout(
        method="PUT",
        workout_id=hevy_workout_id,
        title=title,
        start_time=start_time,
        end_time=end_time,
        exercises=exercises,
        description=description,
        is_private=is_private,
        dry_run=dry_run,
    )


def _send_workout(
    *,
    method: str,
    workout_id: str | None,
    title: str,
    start_time: str,
    end_time: str,
    exercises: list[dict],
    description: str | None,
    is_private: bool,
    dry_run: bool,
) -> dict:
    tool = "log_strength_workout" if method == "POST" else "update_strength_workout"

    # ---- validate + normalize ------------------------------------------
    try:
        payload = _build_payload(
            title=title,
            start_time=start_time,
            end_time=end_time,
            exercises=exercises,
            description=description,
            is_private=is_private,
        )
    except ValueError as e:
        return _err(tool, e)

    if dry_run:
        return _ok(
            tool,
            [{"would_send_payload": payload}],
            warnings=["dry_run=True; nothing sent to Hevy or written locally."],
        )

    # ---- send to Hevy --------------------------------------------------
    try:
        with HevyClient() as client:
            if method == "POST":
                resp = client.create_workout(payload)
            else:
                assert workout_id is not None
                resp = client.update_workout(workout_id, payload)
    except (HevyAuthError, HevyAPIError) as e:
        log.exception("hevy.write.send_failed", tool=tool)
        return _err(tool, e)
    except Exception as e:  # noqa: BLE001
        log.exception("hevy.write.unexpected", tool=tool)
        return _err(tool, e)

    # Hevy wraps create/update responses as {"workout": {...}} per the
    # OpenAPI schema. Normalize before mirroring locally.
    workout_resp = resp.get("workout") if isinstance(resp, dict) else None
    if not isinstance(workout_resp, dict):
        # Some endpoints return the bare workout. Accept either shape.
        workout_resp = resp if isinstance(resp, dict) and resp.get("id") else None
    if not workout_resp or "id" not in workout_resp:
        return _err(
            tool,
            RuntimeError(
                f"Hevy returned an unexpected response shape: {str(resp)[:300]}"
            ),
        )

    # ---- mirror into local DB -----------------------------------------
    try:
        _mirror_workout(workout_resp)
    except Exception as e:  # noqa: BLE001
        log.exception("hevy.write.mirror_failed", workout_id=workout_resp.get("id"))
        # Still surface the success: Hevy is the source of truth, the
        # next ingest_hevy run will re-mirror.
        return _ok(
            tool,
            [_summarize_workout(workout_resp)],
            warnings=[
                f"Workout written to Hevy but local mirror failed: "
                f"{type(e).__name__}: {e}. The next scheduled Hevy sync "
                f"will reconcile."
            ],
        )

    return _ok(
        tool,
        [_summarize_workout(workout_resp)],
        warnings=[
            "mart_daily strength columns won't update until refresh_data('mart') "
            "(or the nightly mart rebuild) runs."
        ],
    )


def _build_payload(
    *,
    title: str,
    start_time: str,
    end_time: str,
    exercises: list[dict],
    description: str | None,
    is_private: bool,
) -> dict:
    """Validate + reshape into the exact body Hevy's POST/PUT expects."""
    if not title or not title.strip():
        raise ValueError("title is required")
    if not isinstance(exercises, list) or not exercises:
        raise ValueError("exercises must be a non-empty list")

    start_iso = _coerce_iso(start_time, "start_time")
    end_iso = _coerce_iso(end_time, "end_time")
    if datetime.fromisoformat(end_iso.replace("Z", "+00:00")) <= datetime.fromisoformat(
        start_iso.replace("Z", "+00:00")
    ):
        raise ValueError("end_time must be after start_time")

    out_exercises: list[dict] = []
    for i, ex in enumerate(exercises):
        if not isinstance(ex, dict):
            raise ValueError(f"exercises[{i}] must be a dict")
        tpl_id = ex.get("exercise_template_id")
        if not tpl_id or not isinstance(tpl_id, str):
            raise ValueError(
                f"exercises[{i}].exercise_template_id is required (use "
                f"find_exercise_templates to resolve a free-text name)"
            )
        sets_raw = ex.get("sets") or []
        if not isinstance(sets_raw, list) or not sets_raw:
            raise ValueError(f"exercises[{i}].sets must be a non-empty list")

        out_sets: list[dict] = []
        for j, s in enumerate(sets_raw):
            if not isinstance(s, dict):
                raise ValueError(f"exercises[{i}].sets[{j}] must be a dict")
            stype = s.get("type", "normal")
            if stype not in VALID_SET_TYPES:
                raise ValueError(
                    f"exercises[{i}].sets[{j}].type must be one of {sorted(VALID_SET_TYPES)}"
                )
            rpe = s.get("rpe")
            if rpe is not None and rpe not in VALID_RPE:
                raise ValueError(
                    f"exercises[{i}].sets[{j}].rpe must be one of {sorted(VALID_RPE)}"
                )
            # Hevy accepts only present-or-null. Don't omit keys; that
            # resets a field on PUT. Default everything explicitly.
            out_sets.append({
                "type": stype,
                "weight_kg": s.get("weight_kg"),
                "reps": s.get("reps"),
                "distance_meters": s.get("distance_meters"),
                "duration_seconds": s.get("duration_seconds"),
                "custom_metric": s.get("custom_metric"),
                "rpe": rpe,
            })

        out_exercises.append({
            "exercise_template_id": tpl_id,
            "superset_id": ex.get("superset_id"),
            "notes": ex.get("notes") or "",
            "sets": out_sets,
        })

    body: dict[str, Any] = {
        "title": title.strip(),
        "description": (description or "").strip() if description else "",
        "start_time": start_iso,
        "end_time": end_iso,
        "is_private": bool(is_private),
        "exercises": out_exercises,
    }
    return body


def _coerce_iso(s: str, field: str) -> str:
    """Accept either an ISO8601 string with 'Z' or with offset, plus a
    naive 'YYYY-MM-DDTHH:MM:SS' which we treat as UTC. Return a Hevy-shaped
    'YYYY-MM-DDTHH:MM:SSZ' string."""
    if not isinstance(s, str) or not s.strip():
        raise ValueError(f"{field} is required")
    raw = s.strip()
    try:
        if raw.endswith("Z"):
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(raw)
    except ValueError as e:
        raise ValueError(f"{field}: not a valid ISO8601 timestamp ({e})") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- local-mirror helpers --------------------------------------------------
def _mirror_workout(payload: dict) -> None:
    """Upsert the Hevy response into raw + derived fact tables. Same logic as
    ingest_hevy.ingest._replace_workout_sets + rollup, but inlined so we
    don't hit the events feed."""
    workout_id = payload["id"]
    set_rows = transforms.explode_workout_sets(payload)
    rollup = transforms.rollup_workout(payload, set_rows)
    if rollup is None:
        # Shouldn't happen for a successful Hevy write — start/end always
        # set — but be defensive.
        log.warning("hevy.mirror.no_ts", workout_id=workout_id)
        return

    updated_src = transforms.parse_ts(payload.get("updated_at"))
    raw_row = {
        "hevy_workout_id": workout_id,
        "payload": Jsonb(payload),
        "updated_at_src": updated_src,
        "deleted": False,
    }

    with tx() as c:
        upsert_rows(
            "raw_hevy_workout",
            [raw_row],
            conflict_cols=["hevy_workout_id"],
            update_cols=["payload", "updated_at_src", "deleted", "fetched_at"],
            connection=c,
        )
        # raw_id back-reference
        with c.cursor() as cur:
            cur.execute(
                "SELECT id FROM raw_hevy_workout WHERE hevy_workout_id = %s",
                [workout_id],
            )
            raw_id = cur.fetchone()["id"]
            # fact_strength_set is DELETE+INSERT to handle reorders.
            cur.execute(
                "DELETE FROM fact_strength_set WHERE hevy_workout_id = %s",
                [workout_id],
            )
        if set_rows:
            upsert_rows(
                "fact_strength_set",
                set_rows,
                conflict_cols=["hevy_workout_id", "exercise_index", "set_index"],
                connection=c,
            )

        rollup["raw_id"] = raw_id
        rollup["updated_at"] = datetime.now(timezone.utc)
        # Whoop linker: same ±10/15min match used by the cron ingester.
        rollup["whoop_workout_id"] = _match_whoop_workout(
            c, rollup["start_ts"], rollup["end_ts"]
        )
        upsert_rows(
            "fact_strength_workout",
            [rollup],
            conflict_cols=["hevy_workout_id"],
            connection=c,
        )


def _match_whoop_workout(connection, start_ts: datetime, end_ts: datetime) -> str | None:
    """±10/15min start/end match against fact_workout. Mirrors
    ingest_hevy.ingest._match_whoop_workout — kept duplicated rather than
    imported to keep the write surface independent of the ingester runtime."""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT workout_id
              FROM fact_workout
             WHERE ABS(EXTRACT(EPOCH FROM (start_ts - %s))) <= 600
               AND ABS(EXTRACT(EPOCH FROM (end_ts   - %s))) <= 900
             ORDER BY ABS(EXTRACT(EPOCH FROM (start_ts - %s)))
             LIMIT 1
            """,
            [start_ts, end_ts, start_ts],
        )
        row = cur.fetchone()
    return row["workout_id"] if row else None


def _summarize_workout(payload: dict) -> dict:
    """Compact round-trip summary for the tool response — full response
    would dwarf the rest of the conversation; the user can fetch detail
    via get_strength_workouts(...) if needed."""
    sets_total = sum(len(e.get("sets") or []) for e in payload.get("exercises") or [])
    return {
        "hevy_workout_id": payload.get("id"),
        "title": payload.get("title"),
        "start_time": payload.get("start_time"),
        "end_time": payload.get("end_time"),
        "exercise_count": len(payload.get("exercises") or []),
        "set_count": sets_total,
        "url": _hevy_url(payload.get("id")),
    }


def _hevy_url(workout_id: str | None) -> str | None:
    if not workout_id:
        return None
    return f"https://hevy.com/workout/{workout_id}"


# ---------------------------------------------------------------------------
# Routines (templates) — create / update / folder management
# ---------------------------------------------------------------------------
VALID_RP_SET_TYPES = {"warmup", "normal", "failure", "dropset"}
VALID_MUSCLE_GROUPS = {
    "abdominals", "shoulders", "biceps", "triceps", "forearms",
    "quadriceps", "hamstrings", "calves", "glutes", "abductors",
    "adductors", "lats", "upper_back", "traps", "lower_back",
    "chest", "cardio", "neck", "full_body", "other",
}
VALID_EQUIPMENT = {
    "none", "barbell", "dumbbell", "kettlebell", "machine",
    "plate", "resistance_band", "suspension", "other",
}
VALID_CUSTOM_EX_TYPES = {
    "weight_reps", "reps_only", "bodyweight_reps", "bodyweight_assisted_reps",
    "duration", "weight_duration", "distance_duration", "short_distance_weight",
}


def create_routine(
    title: str,
    exercises: list[dict],
    folder_id: int | None = None,
    notes: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Create a new Hevy routine (template) via POST /v1/routines and mirror
    it into raw_hevy_routine.

    `exercises` mirrors the workout shape but with a few routine-specific
    fields:
        {
          "exercise_template_id": "<8-char id>",
          "rest_seconds": 90,         # routine-only
          "notes": "…",
          "superset_id": null | int,
          "sets": [
            {"type": "warmup|normal|failure|dropset",
             "weight_kg": 60, "reps": 8,
             "rep_range": {"start": 8, "end": 12},   # routine-only
             "distance_meters": null, "duration_seconds": null,
             "custom_metric": null},
            ...
          ]
        }
    Either reps OR rep_range can drive the prescription; passing both is
    fine (Hevy stores both).
    """
    return _send_routine(
        method="POST",
        routine_id=None,
        title=title,
        exercises=exercises,
        folder_id=folder_id,
        notes=notes,
        dry_run=dry_run,
    )


def update_routine(
    hevy_routine_id: str,
    title: str,
    exercises: list[dict],
    notes: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Overwrite an existing routine via PUT /v1/routines/{id}. Same body
    shape as create_routine; folder_id is NOT updatable through this
    endpoint per the OpenAPI schema (folder moves require deleting + creating
    in the Hevy app)."""
    return _send_routine(
        method="PUT",
        routine_id=hevy_routine_id,
        title=title,
        exercises=exercises,
        folder_id=None,
        notes=notes,
        dry_run=dry_run,
    )


def _send_routine(
    *,
    method: str,
    routine_id: str | None,
    title: str,
    exercises: list[dict],
    folder_id: int | None,
    notes: str | None,
    dry_run: bool,
) -> dict:
    tool = "create_routine" if method == "POST" else "update_routine"
    try:
        payload = _build_routine_payload(
            title=title,
            exercises=exercises,
            folder_id=folder_id,
            notes=notes,
            include_folder=(method == "POST"),
        )
    except ValueError as e:
        return _err(tool, e)

    if dry_run:
        return _ok(
            tool,
            [{"would_send_payload": payload}],
            warnings=["dry_run=True; nothing sent to Hevy or written locally."],
        )

    try:
        with HevyClient() as client:
            if method == "POST":
                resp = client.create_routine(payload)
            else:
                assert routine_id is not None
                resp = client.update_routine(routine_id, payload)
    except (HevyAuthError, HevyAPIError) as e:
        log.exception("hevy.routine.send_failed", tool=tool)
        return _err(tool, e)
    except Exception as e:  # noqa: BLE001
        log.exception("hevy.routine.unexpected", tool=tool)
        return _err(tool, e)

    routine_resp = resp.get("routine") if isinstance(resp, dict) else None
    # Hevy's POST /v1/routines wraps the result as {"routine": [{...}]} (a
    # one-element list), while PUT /v1/routines/{id} returns {"routine": {...}}.
    # Accept both shapes — and also a bare dict at the top level just in case.
    if isinstance(routine_resp, list) and routine_resp:
        routine_resp = routine_resp[0]
    if not isinstance(routine_resp, dict):
        routine_resp = resp if isinstance(resp, dict) and resp.get("id") else None
    if not routine_resp or "id" not in routine_resp:
        return _err(tool, RuntimeError(
            f"Hevy returned an unexpected routine response: {str(resp)[:300]}"
        ))

    try:
        _mirror_routine(routine_resp)
    except Exception as e:  # noqa: BLE001
        log.exception("hevy.routine.mirror_failed", routine_id=routine_resp.get("id"))
        return _ok(
            tool,
            [_summarize_routine(routine_resp)],
            warnings=[
                f"Routine written to Hevy but local mirror failed: "
                f"{type(e).__name__}: {e}. The next refresh_data('hevy') will reconcile."
            ],
        )

    return _ok(tool, [_summarize_routine(routine_resp)])


def _build_routine_payload(
    *,
    title: str,
    exercises: list[dict],
    folder_id: int | None,
    notes: str | None,
    include_folder: bool,
) -> dict:
    if not title or not title.strip():
        raise ValueError("title is required")
    if not isinstance(exercises, list) or not exercises:
        raise ValueError("exercises must be a non-empty list")

    out_exercises: list[dict] = []
    for i, ex in enumerate(exercises):
        if not isinstance(ex, dict):
            raise ValueError(f"exercises[{i}] must be a dict")
        tpl_id = ex.get("exercise_template_id")
        if not tpl_id or not isinstance(tpl_id, str):
            raise ValueError(
                f"exercises[{i}].exercise_template_id is required"
            )
        sets_raw = ex.get("sets") or []
        if not isinstance(sets_raw, list) or not sets_raw:
            raise ValueError(f"exercises[{i}].sets must be a non-empty list")

        out_sets: list[dict] = []
        for j, s in enumerate(sets_raw):
            if not isinstance(s, dict):
                raise ValueError(f"exercises[{i}].sets[{j}] must be a dict")
            stype = s.get("type", "normal")
            if stype not in VALID_RP_SET_TYPES:
                raise ValueError(
                    f"exercises[{i}].sets[{j}].type must be one of "
                    f"{sorted(VALID_RP_SET_TYPES)}"
                )
            rep_range = s.get("rep_range")
            if rep_range is not None:
                if not isinstance(rep_range, dict):
                    raise ValueError(
                        f"exercises[{i}].sets[{j}].rep_range must be a dict"
                    )
                rs, re = rep_range.get("start"), rep_range.get("end")
                if rs is None or re is None or rs > re:
                    raise ValueError(
                        f"exercises[{i}].sets[{j}].rep_range needs start <= end"
                    )
                rep_range = {"start": int(rs), "end": int(re)}
            # Hevy's PUT validator rejects explicit nulls on optional fields
            # (rep_range, custom_metric, etc.) even though POST allows them.
            # Drop any None-valued keys so both verbs work.
            set_dict = {
                "type": stype,
                "weight_kg": s.get("weight_kg"),
                "reps": s.get("reps"),
                "distance_meters": s.get("distance_meters"),
                "duration_seconds": s.get("duration_seconds"),
                "custom_metric": s.get("custom_metric"),
                "rep_range": rep_range,
            }
            out_sets.append({k: v for k, v in set_dict.items() if v is not None})

        rest = ex.get("rest_seconds")
        if rest is not None and (not isinstance(rest, int) or rest < 0):
            raise ValueError(
                f"exercises[{i}].rest_seconds must be a non-negative int"
            )

        out_exercises.append({
            "exercise_template_id": tpl_id,
            "superset_id": ex.get("superset_id"),
            "rest_seconds": rest,
            "notes": ex.get("notes") or "",
            "sets": out_sets,
        })

    body: dict = {
        "title": title.strip(),
        "notes": (notes or "").strip() if notes else "",
        "exercises": out_exercises,
    }
    if include_folder:
        # POST accepts folder_id (nullable); PUT does NOT (the schema omits
        # it, and Hevy errors if you send it on update).
        body["folder_id"] = folder_id
    return body


def _mirror_routine(payload: dict) -> None:
    rid = payload["id"]
    updated_src = transforms.parse_ts(payload.get("updated_at"))
    raw_row = {
        "hevy_routine_id": rid,
        "payload": Jsonb(payload),
        "updated_at_src": updated_src,
        "title": payload.get("title"),
        "folder_id": payload.get("folder_id"),
        "deleted": False,
    }
    upsert_rows(
        "raw_hevy_routine",
        [raw_row],
        conflict_cols=["hevy_routine_id"],
        update_cols=[
            "payload", "updated_at_src", "title", "folder_id",
            "deleted", "fetched_at",
        ],
    )


def _summarize_routine(payload: dict) -> dict:
    sets_total = sum(len(e.get("sets") or []) for e in payload.get("exercises") or [])
    return {
        "hevy_routine_id": payload.get("id"),
        "title": payload.get("title"),
        "folder_id": payload.get("folder_id"),
        "exercise_count": len(payload.get("exercises") or []),
        "set_count": sets_total,
        "url": (
            f"https://hevy.com/routine/{payload['id']}"
            if payload.get("id") else None
        ),
    }


# ---- Routine folders ------------------------------------------------------
def create_routine_folder(title: str) -> dict:
    """Create a new routine folder via POST /v1/routine_folders. Per Hevy's
    docs the new folder is inserted at index 0 and existing folders shift
    down. Mirrors into raw_hevy_routine_folder."""
    if not title or not title.strip():
        return _err("create_routine_folder", ValueError("title is required"))
    try:
        with HevyClient() as client:
            resp = client.create_routine_folder(title.strip())
    except (HevyAuthError, HevyAPIError) as e:
        return _err("create_routine_folder", e)
    except Exception as e:  # noqa: BLE001
        return _err("create_routine_folder", e)

    folder = resp.get("routine_folder") if isinstance(resp, dict) else None
    if not isinstance(folder, dict):
        folder = resp if isinstance(resp, dict) and resp.get("id") is not None else None
    if not folder:
        return _err(
            "create_routine_folder",
            RuntimeError(f"Unexpected response: {str(resp)[:300]}"),
        )

    try:
        upsert_rows(
            "raw_hevy_routine_folder",
            [{
                "folder_id": int(folder["id"]),
                "title": folder.get("title") or title,
                "index": folder.get("index"),
                "payload": Jsonb(folder),
            }],
            conflict_cols=["folder_id"],
            update_cols=["title", "index", "payload", "fetched_at"],
        )
    except Exception as e:  # noqa: BLE001
        log.exception("hevy.folder.mirror_failed", folder_id=folder.get("id"))
        return _ok(
            "create_routine_folder",
            [folder],
            warnings=[f"Folder created but local mirror failed: {e}"],
        )
    return _ok("create_routine_folder", [folder])


# ---- Custom exercise templates -------------------------------------------
def create_custom_exercise(
    title: str,
    exercise_type: str,
    equipment_category: str,
    muscle_group: str,
    other_muscles: list[str] | None = None,
) -> dict:
    """Create a custom exercise template via POST /v1/exercise_templates.
    Useful when Hevy's catalog doesn't have what you need (rare — the
    catalog has 400+ entries)."""
    if not title or not title.strip():
        return _err("create_custom_exercise", ValueError("title is required"))
    if exercise_type not in VALID_CUSTOM_EX_TYPES:
        return _err("create_custom_exercise", ValueError(
            f"exercise_type must be one of {sorted(VALID_CUSTOM_EX_TYPES)}"
        ))
    if equipment_category not in VALID_EQUIPMENT:
        return _err("create_custom_exercise", ValueError(
            f"equipment_category must be one of {sorted(VALID_EQUIPMENT)}"
        ))
    if muscle_group not in VALID_MUSCLE_GROUPS:
        return _err("create_custom_exercise", ValueError(
            f"muscle_group must be one of {sorted(VALID_MUSCLE_GROUPS)}"
        ))
    other_muscles = list(other_muscles or [])
    bad = [m for m in other_muscles if m not in VALID_MUSCLE_GROUPS]
    if bad:
        return _err("create_custom_exercise", ValueError(
            f"other_muscles contains unknown muscle group(s): {bad}"
        ))

    body = {
        "title": title.strip(),
        "exercise_type": exercise_type,
        "equipment_category": equipment_category,
        "muscle_group": muscle_group,
        "other_muscles": other_muscles,
    }
    try:
        with HevyClient() as client:
            resp = client.create_custom_exercise(body)
    except (HevyAuthError, HevyAPIError) as e:
        return _err("create_custom_exercise", e)
    except Exception as e:  # noqa: BLE001
        return _err("create_custom_exercise", e)

    tpl = resp.get("exercise_template") if isinstance(resp, dict) else None
    if not isinstance(tpl, dict):
        tpl = resp if isinstance(resp, dict) and resp.get("id") else None
    if not tpl:
        return _err(
            "create_custom_exercise",
            RuntimeError(f"Unexpected response: {str(resp)[:300]}"),
        )

    # Mirror into dim_hevy_exercise so subsequent find_exercise_templates
    # picks it up immediately.
    try:
        upsert_rows(
            "dim_hevy_exercise",
            [{
                "exercise_template_id": tpl["id"],
                "title": tpl.get("title") or title,
                "exercise_type": tpl.get("type") or tpl.get("exercise_type"),
                "primary_muscle_group": tpl.get("primary_muscle_group") or muscle_group,
                "secondary_muscle_groups": (
                    tpl.get("secondary_muscle_groups") or other_muscles
                ),
                "equipment": tpl.get("equipment") or equipment_category,
                "is_custom": True,
                "payload": Jsonb(tpl),
            }],
            conflict_cols=["exercise_template_id"],
            update_cols=[
                "title", "exercise_type", "primary_muscle_group",
                "secondary_muscle_groups", "equipment", "is_custom",
                "payload", "fetched_at",
            ],
        )
    except Exception as e:  # noqa: BLE001
        log.exception("hevy.custom_ex.mirror_failed", template_id=tpl.get("id"))
        return _ok(
            "create_custom_exercise",
            [tpl],
            warnings=[f"Custom exercise created but local mirror failed: {e}"],
        )
    return _ok("create_custom_exercise", [tpl])

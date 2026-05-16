"""Tool implementations for the MCP server.

Each tool returns a dict matching the SPEC.md §6.2 envelope:

    {ok, tool, rows, row_count, truncated, warnings}
    or
    {ok=False, tool, error, error_type}

Tools use `db.conn()` for the admin pool (mart/fact reads) and
`db.reader_conn()` for the read-only `ask_sql` escape hatch.
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime
from typing import Any

from psycopg import sql

from lifeos_core.db import conn, reader_conn
from lifeos_core.logging import get_logger
from mcp_server.schema_docs import docs_for
from mcp_server.sql_safety import UnsafeQueryError, ensure_limit, validate

log = get_logger(__name__)

ASK_SQL_DEFAULT_LIMIT = 200
ASK_SQL_TIMEOUT_MS = 15_000  # raised from 5s — analytical queries on full
                             # mart_daily history regularly need ~6-10s, and
                             # the 5s ceiling is what tripped the pool in the
                             # transcripts.

# Allowlist of mart_daily columns that may be passed to correlate_metrics.
# Updated alongside any schema change.
CORRELATE_ALLOWLIST = {
    "recovery_score", "hrv_rmssd_ms", "resting_heart_rate", "spo2_percentage",
    "skin_temp_celsius",
    "sleep_total_hours", "sleep_rem_hours", "sleep_slow_wave_hours",
    "sleep_efficiency_pct", "sleep_performance_pct", "sleep_consistency_pct",
    "nap_count", "nap_total_min",
    "strain", "day_kilojoules",
    "workout_count", "workout_total_min", "workout_total_kj", "workout_max_strain",
    "meeting_count", "meeting_hours", "meeting_internal_hours", "meeting_external_hours",
    "longest_focus_block_min", "total_focus_block_min",
    "total_kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "alcohol_g", "caffeine_mg",
    "meal_count", "eating_window_hours",
    "breakfast_kcal", "lunch_kcal", "dinner_kcal", "snack_kcal",
    "total_spend", "food_spend", "restaurant_spend", "groceries_spend", "transportation_spend",
    "alcohol_spend", "bars_spend", "entertainment_spend", "shopping_spend", "travel_spend",
    "dining_out_txn_count", "dining_out_txn_max",
    "weight_kg", "body_fat_pct",
    "strength_total_volume_kg", "strength_total_sets", "strength_unique_exercises",
    # body_image rollup (populated by mart_refresh.refresh_mart_body_image_daily).
    # Useful targets for lagged correlations: alcohol_g → body_image_skin_clarity,
    # sleep_consistency_pct → body_image_under_eye, etc.
    "body_image_overall",
    "body_image_skin_quality",
    "body_image_skin_clarity",
    "body_image_under_eye",
    "body_image_jawline",
    "body_image_hair_quality",
    "body_image_symmetry",
    "body_image_photo_quality",
}


# ---- envelope helpers -------------------------------------------------------
def _ok(tool: str, rows: list[dict], *, truncated: bool = False, warnings: list[str] | None = None,
        extra: dict | None = None) -> dict:
    out: dict = {
        "ok": True,
        "tool": tool,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "warnings": warnings or [],
    }
    if extra:
        out.update(extra)
    return out


def _err(tool: str, exc: BaseException) -> dict:
    return {
        "ok": False,
        "tool": tool,
        "error": str(exc),
        "error_type": type(exc).__name__,
    }


def _serialize(rows: list[dict]) -> list[dict]:
    """Cast non-JSON-native values (date, datetime, Decimal, UUID) to strings.
    psycopg's dict_row already returns Python types; we just stringify the
    ones that don't json.dumps cleanly."""
    out = []
    for r in rows:
        out.append({k: _coerce(v) for k, v in r.items()})
    return out


def _coerce(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, bool)):
        return v
    if isinstance(v, float):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    # psycopg returns Decimal for NUMERIC; preserve precision via float (we're
    # already lossy via JSON).
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return float(v)
    except ImportError:
        pass
    # JSON-native containers (e.g. jsonb_agg results) — recurse so nested
    # datetimes / Decimals get coerced too. Avoids stringifying entire payloads
    # when a tool returns a structured field.
    if isinstance(v, list):
        return [_coerce(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _coerce(val) for k, val in v.items()}
    return str(v)


# ---- get_schema_docs --------------------------------------------------------
def get_schema_docs(table_name: str | None = None) -> dict:
    return _ok("get_schema_docs", [docs_for(table_name)])


# ---- get_daily_summary ------------------------------------------------------
DEFAULT_DAILY_COLS = [
    "day", "recovery_score", "hrv_rmssd_ms", "sleep_total_hours",
    "strain", "meeting_hours", "total_kcal", "total_spend",
]
DAILY_MAX_ROWS = 366


def get_daily_summary(start_date: date, end_date: date, columns: list[str] | None = None) -> dict:
    cols = columns or DEFAULT_DAILY_COLS
    bad = [c for c in cols if c not in CORRELATE_ALLOWLIST and c != "day"]
    if bad:
        return _err("get_daily_summary", ValueError(f"Unknown columns: {bad}"))

    select = sql.SQL(", ").join(map(sql.Identifier, cols))
    q = sql.SQL(
        "SELECT {cols} FROM mart_daily WHERE day BETWEEN %s AND %s ORDER BY day"
    ).format(cols=select)

    with conn() as c, c.cursor() as cur:
        cur.execute(q, [start_date, end_date])
        rows = _serialize(cur.fetchall())

    truncated = len(rows) > DAILY_MAX_ROWS
    warnings: list[str] = []
    if truncated:
        rows = rows[:DAILY_MAX_ROWS]
        warnings.append(f"Result truncated to {DAILY_MAX_ROWS} rows. Narrow the date window.")
    return _ok("get_daily_summary", rows, truncated=truncated, warnings=warnings)


# ---- get_recovery_trend -----------------------------------------------------
def get_recovery_trend(start_date: date, end_date: date, smoothing: int | None = None) -> dict:
    cols = ["day", "recovery_score", "hrv_rmssd_ms", "resting_heart_rate", "sleep_total_hours"]
    select = sql.SQL(", ").join(map(sql.Identifier, cols))
    q = sql.SQL(
        "SELECT {cols} FROM mart_daily WHERE day BETWEEN %s AND %s ORDER BY day"
    ).format(cols=select)

    with conn() as c, c.cursor() as cur:
        cur.execute(q, [start_date, end_date])
        rows = _serialize(cur.fetchall())

    if smoothing and smoothing > 1:
        rows = _add_rolling(rows, ["recovery_score", "hrv_rmssd_ms", "resting_heart_rate"], smoothing)

    return _ok("get_recovery_trend", rows)


def _add_rolling(rows: list[dict], cols: list[str], window: int) -> list[dict]:
    """Trailing N-day rolling average. NULL values skipped from the window."""
    for col in cols:
        rolling_col = f"{col}_roll{window}"
        for i, r in enumerate(rows):
            window_slice = rows[max(0, i - window + 1) : i + 1]
            vals = [w[col] for w in window_slice if w.get(col) is not None]
            r[rolling_col] = round(sum(vals) / len(vals), 2) if vals else None
    return rows


# ---- get_sleep_summary ------------------------------------------------------
def get_sleep_summary(start_date: date, end_date: date, include_naps: bool = False) -> dict:
    base_cols = [
        "day", "sleep_total_hours", "sleep_rem_hours", "sleep_slow_wave_hours",
        "sleep_efficiency_pct", "sleep_performance_pct", "sleep_consistency_pct",
        "sleep_start_ts", "sleep_end_ts",
    ]
    if include_naps:
        base_cols += ["nap_count", "nap_total_min"]

    select = sql.SQL(", ").join(map(sql.Identifier, base_cols))
    q = sql.SQL(
        "SELECT {cols} FROM mart_daily WHERE day BETWEEN %s AND %s ORDER BY day"
    ).format(cols=select)
    with conn() as c, c.cursor() as cur:
        cur.execute(q, [start_date, end_date])
        return _ok("get_sleep_summary", _serialize(cur.fetchall()))


# ---- get_workouts -----------------------------------------------------------
def get_workouts(start_date: date, end_date: date, sport_name: str | None = None) -> dict:
    where = "fw.day BETWEEN %s AND %s"
    params: list = [start_date, end_date]
    if sport_name:
        where += " AND fw.sport_name ILIKE %s"
        params.append(sport_name)
    # LEFT JOIN to fact_strength_workout via the soft-FK whoop_workout_id
    # populated by ingest_hevy. NULL strength_* columns = no Hevy session
    # linked (cardio, walks, etc.); non-NULL = the same physical workout
    # was logged in both Whoop (HR/strain) and Hevy (per-set detail).
    q = f"""
        SELECT fw.workout_id, fw.day, fw.start_ts, fw.end_ts,
               fw.sport_name, fw.strain, fw.kilojoules,
               fw.avg_heart_rate, fw.max_heart_rate, fw.distance_meters,
               fw.zone_two_min, fw.zone_three_min, fw.zone_four_min, fw.zone_five_min,
               fsw.hevy_workout_id,
               fsw.total_volume_kg AS strength_total_volume_kg,
               fsw.total_sets      AS strength_total_sets,
               fsw.unique_exercises AS strength_unique_exercises
        FROM fact_workout fw
        LEFT JOIN fact_strength_workout fsw
          ON fsw.whoop_workout_id = fw.workout_id
        WHERE {where}
        ORDER BY fw.start_ts DESC LIMIT 200
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = _serialize(cur.fetchall())
    truncated = len(rows) >= 200
    warnings = ["Limit 200 workouts. Narrow the window if needed."] if truncated else []
    return _ok("get_workouts", rows, truncated=truncated, warnings=warnings)


# ---- Hevy strength tools ---------------------------------------------------
STRENGTH_WORKOUT_LIMIT = 200
STRENGTH_SET_LIMIT = 1000
EXERCISE_PROGRESSION_LIMIT = 200


def get_strength_workouts(
    start_date: date,
    end_date: date,
    exercise_search: str | None = None,
) -> dict:
    """List Hevy strength sessions with rollup metrics. Joins Whoop's
    HR/strain view via the soft-FK whoop_workout_id populated by
    ingest_hevy."""
    params: list = [start_date, end_date]
    where = ["fsw.day BETWEEN %s AND %s"]
    if exercise_search:
        where.append(
            "EXISTS (SELECT 1 FROM fact_strength_set fss "
            "WHERE fss.hevy_workout_id = fsw.hevy_workout_id "
            "AND fss.exercise_title ILIKE %s)"
        )
        params.append(f"%{exercise_search}%")

    q = f"""
        SELECT fsw.hevy_workout_id, fsw.day, fsw.start_ts, fsw.end_ts,
               fsw.title, fsw.duration_seconds,
               fsw.total_sets, fsw.total_reps, fsw.total_volume_kg,
               fsw.unique_exercises,
               fsw.whoop_workout_id,
               fw.strain        AS whoop_strain,
               fw.avg_heart_rate AS whoop_avg_hr,
               fw.max_heart_rate AS whoop_max_hr,
               fw.kilojoules     AS whoop_kilojoules
        FROM fact_strength_workout fsw
        LEFT JOIN fact_workout fw ON fw.workout_id = fsw.whoop_workout_id
        WHERE {" AND ".join(where)}
        ORDER BY fsw.start_ts DESC
        LIMIT {STRENGTH_WORKOUT_LIMIT + 1}
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = _serialize(cur.fetchall())
    truncated = len(rows) > STRENGTH_WORKOUT_LIMIT
    if truncated:
        rows = rows[:STRENGTH_WORKOUT_LIMIT]
    warnings = (
        [f"More than {STRENGTH_WORKOUT_LIMIT} workouts; truncated. Narrow the window."]
        if truncated else []
    )
    return _ok("get_strength_workouts", rows, truncated=truncated, warnings=warnings)


def get_strength_sets(
    start_date: date,
    end_date: date,
    exercise_search: str | None = None,
    set_type: str | None = None,
    working_sets_only: bool = True,
) -> dict:
    """Per-set Hevy data, filterable. `working_sets_only=True` (default)
    drops warmup sets — what you almost always want for volume / PR work."""
    params: list = [start_date, end_date]
    where = ["day BETWEEN %s AND %s"]
    if exercise_search:
        where.append("exercise_title ILIKE %s")
        params.append(f"%{exercise_search}%")
    if set_type:
        where.append("set_type = %s")
        params.append(set_type)
    elif working_sets_only:
        where.append("(set_type IS NULL OR set_type <> 'warmup')")

    q = f"""
        SELECT hevy_workout_id, exercise_index, set_index,
               exercise_template_id, exercise_title, set_type,
               weight_kg, reps, rpe,
               distance_meters, duration_seconds,
               superset_id,
               workout_start_ts, workout_end_ts, day
        FROM fact_strength_set
        WHERE {" AND ".join(where)}
        ORDER BY workout_start_ts DESC, exercise_index, set_index
        LIMIT {STRENGTH_SET_LIMIT + 1}
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = _serialize(cur.fetchall())
    truncated = len(rows) > STRENGTH_SET_LIMIT
    if truncated:
        rows = rows[:STRENGTH_SET_LIMIT]
    warnings = (
        [f"More than {STRENGTH_SET_LIMIT} sets; truncated. Filter further."]
        if truncated else []
    )
    return _ok("get_strength_sets", rows, truncated=truncated, warnings=warnings)


def get_exercise_progression(
    exercise_search: str,
    start_date: date,
    end_date: date,
    metric: str = "top_weight",
) -> dict:
    """Per-session progression for an exercise. ILIKE matches >1 distinct
    exercise_title (e.g. 'squat' → 'Front Squat (Barbell)' + 'Back Squat
    (Barbell)') we resolve to the most-frequent title in the window so the
    series stays comparable.

    metric ∈ {top_weight, top_set_volume, session_volume, estimated_1rm}.
    Each row carries every numeric so callers can pivot client-side; the
    `metric` param drives the trend summary at the end.
    """
    valid_metrics = {"top_weight", "top_set_volume", "session_volume", "estimated_1rm"}
    if metric not in valid_metrics:
        return _err("get_exercise_progression",
                    ValueError(f"metric must be one of {sorted(valid_metrics)}"))

    # Resolve which exercise_title to anchor on. Most-frequent match in the
    # window wins — keeps Front Squat vs Back Squat from being silently
    # mixed.
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT exercise_title, COUNT(*) AS n
              FROM fact_strength_set
             WHERE day BETWEEN %s AND %s
               AND exercise_title ILIKE %s
               AND (set_type IS NULL OR set_type <> 'warmup')
             GROUP BY exercise_title
             ORDER BY n DESC
             LIMIT 5
            """,
            [start_date, end_date, f"%{exercise_search}%"],
        )
        candidates = _serialize(cur.fetchall())
    if not candidates:
        return _ok(
            "get_exercise_progression",
            [],
            warnings=[f"No working sets matched '{exercise_search}' in window."],
            extra={"matched_titles": [], "anchored_title": None, "metric": metric},
        )

    anchored = candidates[0]["exercise_title"]
    other_matches = [c["exercise_title"] for c in candidates[1:]]

    # Per-session aggregates. Estimated 1RM uses Epley: w * (1 + r/30).
    q = """
        WITH sets AS (
          SELECT hevy_workout_id, day, exercise_title,
                 weight_kg, reps,
                 weight_kg * reps AS set_volume_kg,
                 weight_kg * (1 + reps / 30.0) AS epley_1rm
            FROM fact_strength_set
           WHERE day BETWEEN %s AND %s
             AND exercise_title = %s
             AND (set_type IS NULL OR set_type <> 'warmup')
             AND weight_kg IS NOT NULL
             AND reps      IS NOT NULL
        ),
        per_session AS (
          SELECT
            day, hevy_workout_id, exercise_title,
            COUNT(*)                            AS n_working_sets,
            MAX(weight_kg)                      AS top_weight_kg,
            MAX(set_volume_kg)                  AS top_set_volume_kg,
            SUM(set_volume_kg)                  AS session_volume_kg,
            MAX(epley_1rm)                      AS estimated_1rm_kg
          FROM sets
          GROUP BY day, hevy_workout_id, exercise_title
        ),
        top_reps AS (
          SELECT DISTINCT ON (s.hevy_workout_id)
            s.hevy_workout_id, s.reps AS top_reps_at_top_weight
          FROM sets s
          JOIN per_session p ON p.hevy_workout_id = s.hevy_workout_id
                            AND p.top_weight_kg  = s.weight_kg
          ORDER BY s.hevy_workout_id, s.reps DESC
        )
        SELECT p.day, p.hevy_workout_id, p.exercise_title, p.n_working_sets,
               p.top_weight_kg, tr.top_reps_at_top_weight,
               p.top_set_volume_kg, p.session_volume_kg, p.estimated_1rm_kg
          FROM per_session p
          LEFT JOIN top_reps tr ON tr.hevy_workout_id = p.hevy_workout_id
          ORDER BY p.day
          LIMIT %s
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, [start_date, end_date, anchored, EXERCISE_PROGRESSION_LIMIT + 1])
        rows = _serialize(cur.fetchall())

    truncated = len(rows) > EXERCISE_PROGRESSION_LIMIT
    if truncated:
        rows = rows[:EXERCISE_PROGRESSION_LIMIT]

    summary = _exercise_progression_summary(rows, metric)
    if other_matches:
        summary["other_matched_titles"] = other_matches

    return _ok(
        "get_exercise_progression",
        rows,
        truncated=truncated,
        extra={
            "anchored_title": anchored,
            "metric": metric,
            "matched_titles": [c["exercise_title"] for c in candidates],
            "summary": summary,
        },
    )


def _exercise_progression_summary(rows: list[dict], metric: str) -> dict:
    """PR row + linear-regression slope (per 30 days) over the chosen metric.

    Skipped (returns empty fields) for <3 sessions because slope is
    meaningless on tiny n."""
    metric_col = {
        "top_weight": "top_weight_kg",
        "top_set_volume": "top_set_volume_kg",
        "session_volume": "session_volume_kg",
        "estimated_1rm": "estimated_1rm_kg",
    }[metric]

    valid = [r for r in rows if r.get(metric_col) is not None]
    if not valid:
        return {"current_pr": None, "trend_pct_change_30d": None, "n_sessions": 0}

    pr_row = max(valid, key=lambda r: float(r[metric_col]))

    n = len(valid)
    out: dict = {
        "n_sessions": n,
        "current_pr_value": float(pr_row[metric_col]),
        "current_pr_metric": metric,
        "current_pr_session_id": pr_row["hevy_workout_id"],
        "current_pr_date": pr_row["day"],
    }

    if n < 3:
        out["trend_pct_change_30d"] = None
        return out

    # Linear regression: y = a + b*x, with x = days from first session, y =
    # the metric value. Project slope * 30 / mean(y) → 30-day % change.
    from datetime import date as _date

    first_day = valid[0]["day"]
    if isinstance(first_day, str):
        first_day = _date.fromisoformat(first_day)
    xs: list[float] = []
    ys: list[float] = []
    for r in valid:
        d = r["day"]
        if isinstance(d, str):
            d = _date.fromisoformat(d)
        xs.append(float((d - first_day).days))
        ys.append(float(r[metric_col]))

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0 or mean_y == 0:
        out["trend_pct_change_30d"] = None
    else:
        slope = num / den
        out["trend_pct_change_30d"] = round((slope * 30.0) / mean_y * 100.0, 2)
    return out


def get_strength_volume_trend(
    start_date: date,
    end_date: date,
    granularity: str = "week",
    group_by_muscle_group: bool = False,
) -> dict:
    """Volume rollup over time. granularity ∈ {day, week, month}.
    `group_by_muscle_group=True` joins through dim_hevy_exercise to break
    out per primary_muscle_group — useful for 'am I balancing push/pull'
    questions."""
    if granularity not in {"day", "week", "month"}:
        return _err(
            "get_strength_volume_trend",
            ValueError("granularity must be day | week | month"),
        )

    bucket_expr = {
        "day":   "fsw.day",
        "week":  "date_trunc('week', fsw.day)::date",
        "month": "date_trunc('month', fsw.day)::date",
    }[granularity]

    if not group_by_muscle_group:
        q = f"""
            SELECT
              {bucket_expr} AS period_start,
              COUNT(*)                                  AS total_workouts,
              SUM(fsw.total_sets)                       AS total_sets,
              SUM(fsw.total_volume_kg)                  AS total_volume_kg,
              SUM(fsw.unique_exercises)                 AS total_unique_exercises,
              ROUND(AVG(fsw.total_volume_kg), 2)        AS avg_volume_per_workout
            FROM fact_strength_workout fsw
            WHERE fsw.day BETWEEN %s AND %s
            GROUP BY {bucket_expr}
            ORDER BY period_start
            LIMIT 366
        """
        with conn() as c, c.cursor() as cur:
            cur.execute(q, [start_date, end_date])
            return _ok("get_strength_volume_trend", _serialize(cur.fetchall()))

    # Muscle-group breakout: per-set volume joined to dim_hevy_exercise.
    bucket_expr_set = bucket_expr.replace("fsw.day", "fss.day")
    q = f"""
        SELECT
          {bucket_expr_set}                              AS period_start,
          COALESCE(dhe.primary_muscle_group, 'unknown')  AS primary_muscle_group,
          COUNT(DISTINCT fss.hevy_workout_id)            AS total_workouts,
          COUNT(*)                                       AS total_sets,
          SUM(fss.weight_kg * fss.reps) FILTER (
            WHERE (fss.set_type IS NULL OR fss.set_type <> 'warmup')
              AND fss.weight_kg IS NOT NULL AND fss.reps IS NOT NULL
          )                                              AS total_volume_kg,
          COUNT(DISTINCT fss.exercise_template_id)       AS total_unique_exercises
        FROM fact_strength_set fss
        LEFT JOIN dim_hevy_exercise dhe
          ON dhe.exercise_template_id = fss.exercise_template_id
        WHERE fss.day BETWEEN %s AND %s
        GROUP BY {bucket_expr_set}, dhe.primary_muscle_group
        ORDER BY period_start, primary_muscle_group
        LIMIT 1000
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, [start_date, end_date])
        return _ok("get_strength_volume_trend", _serialize(cur.fetchall()))


# ---- Hevy routines + exercise history -------------------------------------
ROUTINE_LIMIT = 100


def list_routines(folder_id: int | None = None, search: str | None = None) -> dict:
    """Hevy routines (templates). Reads from local mirror raw_hevy_routine,
    populated by `python -m ingest_hevy ingest` (or refresh_data('hevy'))."""
    where: list[str] = ["NOT deleted"]
    params: list = []
    if folder_id is not None:
        where.append("folder_id = %s")
        params.append(folder_id)
    if search:
        where.append("title ILIKE %s")
        params.append(f"%{search}%")
    q = f"""
        SELECT hevy_routine_id, title, folder_id, updated_at_src,
               jsonb_array_length(COALESCE(payload->'exercises', '[]'::jsonb)) AS exercise_count,
               (SELECT COUNT(*) FROM jsonb_array_elements(payload->'exercises') ex,
                                       jsonb_array_elements(ex->'sets') s) AS set_count,
               payload->>'notes' AS notes
        FROM raw_hevy_routine
        WHERE {" AND ".join(where)}
        ORDER BY COALESCE(updated_at_src, fetched_at) DESC NULLS LAST
        LIMIT {ROUTINE_LIMIT}
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = _serialize(cur.fetchall())
    warnings: list[str] = []
    if not rows:
        warnings.append(
            "No routines found locally. Run refresh_data('hevy') if you've "
            "created routines in the Hevy app."
        )
    return _ok("list_routines", rows, warnings=warnings)


def list_routine_folders() -> dict:
    """Routine folders, in Hevy's display order."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT f.folder_id, f.title, f.index,
                   COUNT(r.hevy_routine_id) FILTER (WHERE NOT r.deleted) AS routine_count
              FROM raw_hevy_routine_folder f
              LEFT JOIN raw_hevy_routine r ON r.folder_id = f.folder_id
             GROUP BY f.folder_id, f.title, f.index
             ORDER BY COALESCE(f.index, 0), f.title
            """
        )
        return _ok("list_routine_folders", _serialize(cur.fetchall()))


def get_routine(hevy_routine_id: str) -> dict:
    """Full payload for one routine — every exercise, every prescribed set,
    rest_seconds, notes, rep_range. Use this before starting a workout from
    a routine, so you know what to do."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT hevy_routine_id, title, folder_id, updated_at_src,
                   payload
              FROM raw_hevy_routine
             WHERE hevy_routine_id = %s
            """,
            [hevy_routine_id],
        )
        row = cur.fetchone()
    if row is None:
        return _err("get_routine", ValueError(
            f"Unknown routine id '{hevy_routine_id}'. Use list_routines to discover."
        ))
    return _ok("get_routine", _serialize([row]))


def get_exercise_history(
    exercise_search: str,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 500,
) -> dict:
    """Every set ever logged for one exercise across all your workouts.
    Hits Hevy's /v1/exercise_history/{template_id} live, so it's authoritative
    even for workouts older than your local backfill window. exercise_search
    is ILIKE on dim_hevy_exercise.title; if it matches multiple templates we
    pick the most-frequent one in fact_strength_set so the series stays
    comparable."""
    if not exercise_search or not exercise_search.strip():
        return _err("get_exercise_history", ValueError("exercise_search is required"))

    # Resolve template_id locally (most-frequent first, falls back to title len).
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT dhe.exercise_template_id, dhe.title,
                   COALESCE((
                     SELECT COUNT(*) FROM fact_strength_set fss
                      WHERE fss.exercise_template_id = dhe.exercise_template_id
                   ), 0) AS n_sets
              FROM dim_hevy_exercise dhe
             WHERE dhe.title ILIKE %s
             ORDER BY n_sets DESC, length(dhe.title), dhe.title
             LIMIT 5
            """,
            [f"%{exercise_search}%"],
        )
        candidates = _serialize(cur.fetchall())

    if not candidates:
        return _err("get_exercise_history", ValueError(
            f"No exercise matched '{exercise_search}'. Use find_exercise_templates "
            f"to discover. If the catalog is empty, run refresh_data('hevy')."
        ))

    chosen = candidates[0]
    template_id = chosen["exercise_template_id"]
    other = [c["title"] for c in candidates[1:]]

    # Hit Hevy live for the canonical history.
    try:
        from ingest_hevy.client import HevyClient
        with HevyClient() as client:
            entries = client.exercise_history(
                template_id,
                start_date=str(start_date) if start_date else None,
                end_date=str(end_date) if end_date else None,
            )
    except Exception as e:  # noqa: BLE001
        return _err("get_exercise_history", e)

    truncated = len(entries) > limit
    if truncated:
        entries = entries[:limit]

    # Compute lightweight summary stats (top weight, top reps, est 1RM).
    working = [
        e for e in entries
        if (e.get("set_type") or "normal") != "warmup"
        and e.get("weight_kg") is not None
        and e.get("reps") is not None
    ]
    summary: dict = {
        "anchored_title": chosen["title"],
        "exercise_template_id": template_id,
        "n_total_sets": len(entries),
        "n_working_sets": len(working),
    }
    if working:
        top = max(working, key=lambda r: float(r["weight_kg"]))
        epley = max(
            float(r["weight_kg"]) * (1 + float(r["reps"]) / 30.0)
            for r in working
        )
        summary.update({
            "top_weight_kg": float(top["weight_kg"]),
            "top_weight_reps": int(top["reps"]),
            "top_weight_workout_id": top.get("workout_id"),
            "top_weight_date": top.get("workout_start_time"),
            "estimated_1rm_kg": round(epley, 2),
        })
    if other:
        summary["other_matched_titles"] = other

    return _ok(
        "get_exercise_history",
        _serialize(entries),
        truncated=truncated,
        warnings=([f"Truncated to {limit} sets."] if truncated else []),
        extra={"summary": summary},
    )


# ---- get_food_log -----------------------------------------------------------
FOOD_LOG_LIMIT = 500


def get_food_log(
    start_date: date,
    end_date: date,
    meal_window: str | None = None,
    search: str | None = None,
) -> dict:
    where = ["day BETWEEN %s AND %s"]
    params: list = [start_date, end_date]
    if meal_window:
        where.append("meal_group ILIKE %s")
        params.append(meal_window if "%" in meal_window else f"{meal_window}%")
    if search:
        where.append("food_name ILIKE %s")
        params.append(f"%{search}%")

    where_clause = " AND ".join(where)
    q = f"""
        SELECT id, eaten_at, day, meal_group, food_name, amount, unit,
               energy_kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g,
               sodium_mg, caffeine_mg, alcohol_g
        FROM fact_food_log
        WHERE {where_clause}
        ORDER BY eaten_at DESC
        LIMIT {FOOD_LOG_LIMIT + 1}
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = _serialize(cur.fetchall())

    truncated = len(rows) > FOOD_LOG_LIMIT
    warnings: list[str] = []
    if truncated:
        rows = rows[:FOOD_LOG_LIMIT]
        warnings.append(
            f"More than {FOOD_LOG_LIMIT} matches; truncated. "
            "Try narrowing date range or use get_meal_summary for aggregates."
        )
    return _ok("get_food_log", rows, truncated=truncated, warnings=warnings)


# ---- get_meal_summary -------------------------------------------------------
def get_meal_summary(start_date: date, end_date: date, meal_window: str | None = None) -> dict:
    where = ["day BETWEEN %s AND %s"]
    params: list = [start_date, end_date]
    if meal_window:
        where.append("meal_window = %s")
        params.append(meal_window)
    q = f"""
        SELECT day, meal_window, start_ts, end_ts, duration_min, item_count,
               total_kcal, protein_g, carbs_g, fat_g, fiber_g, food_names
        FROM mart_meal WHERE {" AND ".join(where)} ORDER BY start_ts
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        return _ok("get_meal_summary", _serialize(cur.fetchall()))


# ---- get_calendar_load ------------------------------------------------------
def get_calendar_load(start_date: date, end_date: date) -> dict:
    cols = [
        "day", "meeting_count", "meeting_hours", "meeting_internal_hours",
        "meeting_external_hours", "first_meeting_time", "last_meeting_time",
        "longest_focus_block_min", "total_focus_block_min",
    ]
    select = sql.SQL(", ").join(map(sql.Identifier, cols))
    q = sql.SQL(
        "SELECT {cols} FROM mart_daily WHERE day BETWEEN %s AND %s ORDER BY day"
    ).format(cols=select)
    with conn() as c, c.cursor() as cur:
        cur.execute(q, [start_date, end_date])
        return _ok("get_calendar_load", _serialize(cur.fetchall()))


# ---- get_calendar_events ----------------------------------------------------
def get_calendar_events(
    start_date: date,
    end_date: date,
    classification: str | None = None,
    search: str | None = None,
) -> dict:
    where = ["day BETWEEN %s AND %s"]
    params: list = [start_date, end_date]
    if classification:
        where.append("classification = %s")
        params.append(classification)
    if search:
        where.append("title ILIKE %s")
        params.append(f"%{search}%")
    q = f"""
        SELECT calendar_id, event_id, start_ts, end_ts, day, duration_min,
               title, status, classification, attendee_count, attendee_internal_count,
               attendee_external_count, response_status, has_video_link, location
        FROM fact_calendar_event
        WHERE {" AND ".join(where)}
        ORDER BY start_ts DESC
        LIMIT 500
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        return _ok("get_calendar_events", _serialize(cur.fetchall()))


# ---- get_spending -----------------------------------------------------------
GROUP_BY_OPTIONS = {"day", "week", "month", "category", "merchant", "account"}


def _escape_like(s: str) -> str:
    """Escape ILIKE special chars (%, _, \\) so a substring match treats them
    literally. Used everywhere we accept user-supplied substrings — without
    this, a category named 'Bars & Nightlife' or anything containing _ silently
    matched the wrong thing."""
    return s.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def get_spending(
    start_date: date,
    end_date: date,
    category: str | None = None,
    group_by: str = "day",
    account_id: str | None = None,
    account: str | None = None,
    exact_category: bool = False,
    merchant: str | None = None,
) -> dict:
    """Aggregated spending with rich filtering.

    `category`     ILIKE substring (escapes %, _) unless `exact_category=True`.
    `account_id`   Exact dim_account.account_id.
    `account`      ILIKE substring against dim_account.name (e.g. 'Amazon Card').
    `merchant`     ILIKE substring against fact_transaction.merchant.
    `group_by`     day | week | month | category | merchant | account.
    """
    if group_by not in GROUP_BY_OPTIONS:
        return _err("get_spending", ValueError(f"group_by must be one of {sorted(GROUP_BY_OPTIONS)}"))

    where = ["t.date BETWEEN %s AND %s", "NOT t.is_excluded", "t.amount > 0"]
    params: list = [start_date, end_date]
    if category:
        if exact_category:
            where.append("c.name = %s")
            params.append(category)
        else:
            where.append(r"c.name ILIKE %s ESCAPE '\'")
            params.append(f"%{_escape_like(category)}%")
    if account_id:
        where.append("t.account_id = %s")
        params.append(account_id)
    if account:
        where.append(r"a.name ILIKE %s ESCAPE '\'")
        params.append(f"%{_escape_like(account)}%")
    if merchant:
        where.append(r"t.merchant ILIKE %s ESCAPE '\'")
        params.append(f"%{_escape_like(merchant)}%")

    where_clause = " AND ".join(where)
    if group_by == "day":
        bucket_select, bucket_alias = "t.date", "bucket"
    elif group_by == "week":
        bucket_select, bucket_alias = "date_trunc('week', t.date)::date", "bucket"
    elif group_by == "month":
        bucket_select, bucket_alias = "date_trunc('month', t.date)::date", "bucket"
    elif group_by == "category":
        bucket_select, bucket_alias = "COALESCE(c.name, 'Uncategorized')", "bucket"
    elif group_by == "merchant":
        bucket_select, bucket_alias = "t.merchant", "bucket"
    else:  # account
        bucket_select, bucket_alias = "COALESCE(a.name, t.account_id, 'unknown')", "bucket"

    order_by = "total DESC" if group_by in ("category", "merchant", "account") else "bucket"

    q = f"""
        SELECT {bucket_select} AS {bucket_alias},
               SUM(t.amount) AS total,
               COUNT(*)      AS txn_count
        FROM fact_transaction t
        LEFT JOIN dim_category c ON c.category_id = t.category_id
        LEFT JOIN dim_account  a ON a.account_id  = t.account_id
        WHERE {where_clause}
        GROUP BY {bucket_alias}
        ORDER BY {order_by}
        LIMIT 500
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        return _ok("get_spending", _serialize(cur.fetchall()))


# ---- get_transactions -------------------------------------------------------
def get_transactions(
    start_date: date,
    end_date: date,
    category: str | None = None,
    merchant: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    tag: str | None = None,
    has_no_tags: bool = False,
    untagged_for_couples: bool = False,
    account_id: str | None = None,
    account: str | None = None,
    account_ids: list[str] | None = None,
    exclude_excluded: bool = True,
    only_charges: bool = False,
    exact_category: bool = False,
    limit: int = 500,
) -> dict:
    """Individual transactions with rich filters.

    Filters (combine freely; AND semantics):
        category               ILIKE substring (auto-escapes %, _) unless
                               exact_category=True.
        merchant               ILIKE substring (auto-escapes %, _).
        min_amount/max_amount  Compared against ABS(amount).
        tag                    ILIKE against any element of tags[].
        has_no_tags            Only rows with empty tag list.
        untagged_for_couples   Only rows missing me/partner/joint tags.
        account_id             Exact match.
        account                ILIKE against dim_account.name.
        account_ids            List of account_ids (any match).
        exclude_excluded       Drop rows with is_excluded=true (default true).
        only_charges           Drop refunds/income; keep amount > 0.
        limit                  1..1000.
    """
    if limit < 1 or limit > 1000:
        return _err("get_transactions", ValueError("limit must be between 1 and 1000"))

    where = ["t.date BETWEEN %s AND %s"]
    params: list = [start_date, end_date]
    if exclude_excluded:
        where.append("NOT t.is_excluded")
    if only_charges:
        where.append("t.amount > 0")
    if category:
        if exact_category:
            where.append("c.name = %s")
            params.append(category)
        else:
            where.append(r"c.name ILIKE %s ESCAPE '\'")
            params.append(f"%{_escape_like(category)}%")
    if merchant:
        where.append(r"t.merchant ILIKE %s ESCAPE '\'")
        params.append(f"%{_escape_like(merchant)}%")
    if min_amount is not None:
        where.append("ABS(t.amount) >= %s")
        params.append(min_amount)
    if max_amount is not None:
        where.append("ABS(t.amount) <= %s")
        params.append(max_amount)
    if account_id:
        where.append("t.account_id = %s")
        params.append(account_id)
    if account_ids:
        where.append("t.account_id = ANY(%s)")
        params.append(list(account_ids))
    if account:
        where.append(r"a.name ILIKE %s ESCAPE '\'")
        params.append(f"%{_escape_like(account)}%")
    if tag:
        where.append(r"EXISTS (SELECT 1 FROM unnest(t.tags) x WHERE x ILIKE %s ESCAPE '\')")
        params.append(f"%{_escape_like(tag)}%")
    if has_no_tags:
        where.append("(t.tags IS NULL OR cardinality(t.tags) = 0)")
    if untagged_for_couples:
        from lifeos_core.settings import settings as _s
        couple_names = [
            _s.COUPLE_TAG_ME.lower(),
            _s.COUPLE_TAG_PARTNER.lower(),
            _s.COUPLE_TAG_JOINT.lower(),
        ]
        where.append(
            "NOT EXISTS (SELECT 1 FROM unnest(t.tags) x WHERE LOWER(x) = ANY(%s))"
        )
        params.append(couple_names)
    q = f"""
        SELECT t.transaction_id, t.date, t.amount, t.merchant, t.description,
               c.name AS category, c.category_id, t.is_pending, t.is_recurring,
               t.is_excluded, t.is_reviewed, t.notes, t.tip_amount, t.parent_id,
               t.copilot_type, t.tags, t.tag_ids,
               a.name AS account, t.account_id
        FROM fact_transaction t
        LEFT JOIN dim_category c ON c.category_id = t.category_id
        LEFT JOIN dim_account  a ON a.account_id  = t.account_id
        WHERE {" AND ".join(where)}
        ORDER BY t.date DESC, t.amount DESC
        LIMIT %s
    """
    params.append(limit + 1)
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = _serialize(cur.fetchall())
    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]
    warnings = (
        [f"More than {limit} matches; truncated. Narrow the filter or raise limit."]
        if truncated else []
    )
    return _ok("get_transactions", rows, truncated=truncated, warnings=warnings)


# ---- get_biometrics ---------------------------------------------------------
def get_biometrics(
    metric: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    if metric is None:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT metric, COUNT(*) AS n,
                       MIN(measured_at) AS first_seen,
                       MAX(measured_at) AS last_seen
                FROM fact_biometric GROUP BY metric ORDER BY n DESC
                """
            )
            return _ok("get_biometrics", _serialize(cur.fetchall()))

    where = ["metric = %s"]
    params: list = [metric]
    if start_date is not None:
        where.append("day >= %s")
        params.append(start_date)
    if end_date is not None:
        where.append("day <= %s")
        params.append(end_date)
    q = f"""
        SELECT id, measured_at, day, metric, value, unit, note, source
        FROM fact_biometric WHERE {" AND ".join(where)}
        ORDER BY measured_at DESC LIMIT 1000
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        return _ok("get_biometrics", _serialize(cur.fetchall()))


# ---- correlate_metrics ------------------------------------------------------
def correlate_metrics(
    metric_a: str,
    metric_b: str,
    start_date: date,
    end_date: date,
    lag_days: int = 0,
    method: str = "pearson",
    lag_range: list[int] | None = None,
    return_series: bool = True,
) -> dict:
    """Correlate two mart_daily metrics over a range.

    `lag_range`     Optional [min, max] inclusive. If passed, runs the
                    correlation at each integer lag in that range (capped
                    at 21 lags to stay under typical token budgets) and
                    returns them in `lags`. Useful for finding the
                    strongest predictive lag in one call.
    `return_series` Set to false to skip the row-level paired data — handy
                    for sweep mode where the agent only needs aggregate
                    stats per lag.
    """
    if metric_a not in CORRELATE_ALLOWLIST or metric_b not in CORRELATE_ALLOWLIST:
        bad = [m for m in (metric_a, metric_b) if m not in CORRELATE_ALLOWLIST]
        return _err("correlate_metrics", ValueError(f"Not in allowlist: {bad}"))
    if method not in ("pearson", "spearman"):
        return _err("correlate_metrics", ValueError("method must be 'pearson' or 'spearman'"))

    a = sql.Identifier(metric_a)
    b = sql.Identifier(metric_b)

    try:
        from scipy import stats  # local import keeps test imports light
    except Exception as e:
        log.exception("correlate_metrics.scipy_failed")
        return _err("correlate_metrics", e)

    def _run(lag: int) -> tuple[list[dict], dict]:
        q = sql.SQL(
            """
            SELECT m1.day AS day,
                   m1.{a} AS a,
                   m2.{b} AS b
            FROM mart_daily m1
            JOIN mart_daily m2 ON m2.day = m1.day + %s::int
            WHERE m1.day BETWEEN %s AND %s
              AND m1.{a} IS NOT NULL
              AND m2.{b} IS NOT NULL
            ORDER BY m1.day
            """
        ).format(a=a, b=b)
        with conn() as c, c.cursor() as cur:
            cur.execute(q, [lag, start_date, end_date])
            rows = cur.fetchall()
        n = len(rows)
        if n < 3:
            return rows, {"n": n, "pearson_r": None, "p_value": None,
                          "spearman_r": None, "lag_days": lag}
        a_vals = [float(r["a"]) for r in rows]
        b_vals = [float(r["b"]) for r in rows]
        p_r, p_p = stats.pearsonr(a_vals, b_vals)
        s_r, s_p = stats.spearmanr(a_vals, b_vals)
        primary_p = float(p_p if method == "pearson" else s_p)
        return rows, {
            "n": n,
            "pearson_r": _safe(p_r),
            "p_value": _safe(primary_p),
            "spearman_r": _safe(s_r),
            "lag_days": lag,
        }

    # ---- sweep mode ----
    if lag_range is not None:
        if (
            len(lag_range) != 2 or not all(isinstance(x, int) for x in lag_range)
            or lag_range[0] > lag_range[1]
        ):
            return _err(
                "correlate_metrics",
                ValueError("lag_range must be [min, max] integers with min <= max"),
            )
        lo, hi = lag_range
        lag_count = hi - lo + 1
        if lag_count > 21:
            return _err(
                "correlate_metrics",
                ValueError(f"lag_range covers {lag_count} lags; max is 21."),
            )
        results = []
        for lag in range(lo, hi + 1):
            _, stats_dict = _run(lag)
            results.append(stats_dict)
        # Highest-magnitude lag is what the user usually wants surfaced.
        non_null = [r for r in results if r.get("pearson_r") is not None]
        best = max(non_null, key=lambda r: abs(r["pearson_r"]), default=None)
        return _ok(
            "correlate_metrics",
            [],
            extra={
                "metric_a": metric_a,
                "metric_b": metric_b,
                "method": method,
                "lag_range": [lo, hi],
                "lags": results,
                "best_lag": best,
            },
        )

    # ---- single-lag mode (default) ----
    rows, stats_dict = _run(lag_days)
    if stats_dict["n"] < 3:
        return _ok(
            "correlate_metrics",
            _serialize(rows) if return_series else [],
            warnings=["Fewer than 3 paired observations; correlation undefined."],
            extra={**stats_dict, "metric_a": metric_a, "metric_b": metric_b,
                   "method": method},
        )
    capped = _serialize(rows[:366]) if return_series else []
    return _ok(
        "correlate_metrics",
        capped,
        truncated=len(rows) > 366 if return_series else False,
        extra={**stats_dict, "metric_a": metric_a, "metric_b": metric_b,
               "method": method},
    )


def _safe(x: float) -> float | None:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return float(x)


# ---- Whoop journal reads ---------------------------------------------------
def list_behaviors(category: str | None = None, search: str | None = None) -> dict:
    """List Whoop's behavior catalog. Filter by category (DAYTIME, NIGHTTIME,
    YOUR WEEKLY PLAN, ...) or substring search on title/internal_name."""
    where: list[str] = []
    params: list = []
    if category:
        where.append("category ILIKE %s")
        params.append(category)
    if search:
        where.append("(title ILIKE %s OR internal_name ILIKE %s)")
        params.append(f"%{search}%")
        params.append(f"%{search}%")
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    q = f"""
        SELECT behavior_id, internal_name, title, question_text, category,
               behavior_type, question_type, magnitude_type, magnitude_unit,
               magnitude_min, magnitude_max, status
        FROM dim_whoop_behavior {where_clause}
        ORDER BY category NULLS LAST, title
        LIMIT 500
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        return _ok("list_behaviors", _serialize(cur.fetchall()))


def get_journal_entries(start_date: date, end_date: date, day: date | None = None) -> dict:
    """Daily journal entries from raw_whoop_journal. If `day` given, returns
    just that day's full payload. Otherwise returns one row per day in the
    window with the parsed habit log + free-text notes."""
    if day is not None:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT day, fetched_at, payload FROM raw_whoop_journal WHERE day = %s",
                [day],
            )
            row = cur.fetchone()
        if row is None:
            return _ok("get_journal_entries", [])
        # Return the parsed habit_log alongside.
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT habit_key, answered_yes, magnitude_value, magnitude_unit,
                       time_input_value, user_reviewed
                FROM fact_habit_log
                WHERE day = %s
                ORDER BY habit_key
                """,
                [day],
            )
            habits = _serialize(cur.fetchall())
        out = _serialize([{**dict(row), "habits": habits}])
        return _ok("get_journal_entries", out)

    # Window summary: per-day habit count + notes.
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT
              j.day,
              j.fetched_at,
              j.payload->'journal'->>'notes' AS notes,
              (SELECT COUNT(*) FROM fact_habit_log h WHERE h.day = j.day) AS habit_count,
              (SELECT COUNT(*) FROM fact_habit_log h WHERE h.day = j.day AND h.answered_yes IS TRUE) AS yes_count
            FROM raw_whoop_journal j
            WHERE j.day BETWEEN %s AND %s
            ORDER BY j.day DESC
            """,
            [start_date, end_date],
        )
        return _ok("get_journal_entries", _serialize(cur.fetchall()))


def get_habit_history(
    habit_key: str,
    start_date: date,
    end_date: date,
) -> dict:
    """Time series for a single habit. `habit_key` is dim_whoop_behavior.
    internal_name (e.g. 'alcohol', 'caffeine', 'late-meal'). Use list_behaviors
    to discover available keys."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT day, answered_yes, magnitude_value, magnitude_unit,
                   time_input_value, user_reviewed
            FROM fact_habit_log
            WHERE habit_key = %s AND day BETWEEN %s AND %s
            ORDER BY day
            """,
            [habit_key, start_date, end_date],
        )
        rows = _serialize(cur.fetchall())

    yes_count = sum(1 for r in rows if r.get("answered_yes") is True)
    return _ok(
        "get_habit_history",
        rows,
        extra={
            "habit_key": habit_key,
            "n_days": len(rows),
            "yes_count": yes_count,
            "yes_rate": round(yes_count / len(rows), 3) if rows else None,
        },
    )


# ---- Whoop labs reads ------------------------------------------------------
def list_lab_tests() -> dict:
    """All ingested Advanced Labs panels — one row per test_id."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT r.test_id, r.test_name, r.test_date, r.fetched_at,
                   COUNT(f.id) AS biomarker_count,
                   COUNT(*) FILTER (WHERE f.status_type = 'OPTIMAL')      AS n_optimal,
                   COUNT(*) FILTER (WHERE f.status_type = 'SUFFICIENT')   AS n_sufficient,
                   COUNT(*) FILTER (WHERE f.status_type = 'OUT_OF_RANGE') AS n_out_of_range
            FROM raw_whoop_labs r
            LEFT JOIN fact_lab_result f ON f.test_id = r.test_id
            GROUP BY r.test_id, r.test_name, r.test_date, r.fetched_at
            ORDER BY r.test_date DESC NULLS LAST
            """
        )
        rows = _serialize(cur.fetchall())
    return _ok("list_lab_tests", rows)


def get_lab_results(
    biomarker_id: str | None = None,
    status: str | None = None,
    category: str | None = None,
    test_id: str | None = None,
    search: str | None = None,
) -> dict:
    """Lab biomarker results joined with their reference info.

    Defaults to the most recent panel. Filters compose with AND.
    Returns one row per biomarker with: current value+unit, status,
    optimal/sufficient bands, description, what high/low means, and
    the indicator's percentile on Whoop's range meter.
    """
    where: list[str] = []
    params: list = []

    if test_id is None:
        where.append("f.test_id = (SELECT test_id FROM raw_whoop_labs ORDER BY test_date DESC NULLS LAST LIMIT 1)")
    else:
        where.append("f.test_id = %s")
        params.append(test_id)

    if biomarker_id:
        where.append("f.biomarker_id = %s")
        params.append(biomarker_id)

    if status:
        where.append("f.status_type = %s")
        params.append(status.upper())

    if category:
        where.append("d.category ILIKE %s")
        params.append(category)

    if search:
        where.append("(d.title ILIKE %s OR d.biomarker_id ILIKE %s OR d.description ILIKE %s)")
        params.append(f"%{search}%")
        params.append(f"%{search}%")
        params.append(f"%{search}%")

    where_clause = " WHERE " + " AND ".join(where) if where else ""
    q = f"""
        SELECT
          f.biomarker_id,
          d.title,
          d.category,
          f.value_text       AS value,
          f.value_numeric,
          COALESCE(f.unit, d.unit) AS unit,
          f.status_type,
          f.trend,
          f.trend_display,
          d.optimal_low,
          d.optimal_high,
          d.sufficient_low,
          d.sufficient_high,
          d.description,
          d.what_high_means,
          d.what_low_means,
          d.influenced_by,
          d.notes,
          f.indicator_percent,
          f.test_id,
          f.test_date
        FROM fact_lab_result f
        JOIN dim_lab_biomarker d ON d.biomarker_id = f.biomarker_id
        {where_clause}
        ORDER BY
          CASE f.status_type
            WHEN 'OUT_OF_RANGE' THEN 0
            WHEN 'SUFFICIENT'   THEN 1
            WHEN 'OPTIMAL'      THEN 2
            ELSE 3
          END,
          d.category, d.title
        LIMIT 200
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = _serialize(cur.fetchall())

    summary = {
        "total": len(rows),
        "n_optimal":      sum(1 for r in rows if r.get("status_type") == "OPTIMAL"),
        "n_sufficient":   sum(1 for r in rows if r.get("status_type") == "SUFFICIENT"),
        "n_out_of_range": sum(1 for r in rows if r.get("status_type") == "OUT_OF_RANGE"),
    }
    return _ok("get_lab_results", rows, extra={"summary": summary})


def get_biomarker_info(biomarker_id: str) -> dict:
    """Reference card for a biomarker: description, optimal/sufficient
    ranges, what high/low means, influenced_by — plus the user's most
    recent measured value if a panel has been ingested."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT
              d.biomarker_id, d.title, d.category, d.unit, d.description,
              d.optimal_low, d.optimal_high, d.sufficient_low, d.sufficient_high,
              d.what_high_means, d.what_low_means, d.influenced_by, d.notes,
              f.value_text         AS most_recent_value,
              f.value_numeric      AS most_recent_value_numeric,
              f.unit               AS most_recent_unit,
              f.status_type        AS most_recent_status,
              f.trend_display      AS most_recent_trend_display,
              f.test_date          AS most_recent_test_date
            FROM dim_lab_biomarker d
            LEFT JOIN LATERAL (
              SELECT * FROM fact_lab_result
              WHERE biomarker_id = d.biomarker_id
              ORDER BY test_date DESC NULLS LAST
              LIMIT 1
            ) f ON TRUE
            WHERE d.biomarker_id = %s
            """,
            [biomarker_id],
        )
        row = cur.fetchone()
    if row is None:
        return _err("get_biomarker_info", ValueError(
            f"Unknown biomarker_id '{biomarker_id}'. Use get_lab_results(search=...) to discover."
        ))
    return _ok("get_biomarker_info", _serialize([row]))


# ---- ask_sql ----------------------------------------------------------------
def ask_sql(
    query: str,
    max_rows: int = 200,
    timeout_ms: int | None = None,
    explain: bool = False,
) -> dict:
    """Read-only SQL escape hatch.

    `timeout_ms` overrides the default per-statement timeout (default 15s,
    bounded at 60s). `explain=True` returns the EXPLAIN plan instead of
    executing — handy for diagnosing why a query is slow without burning the
    full timeout."""
    try:
        validate(query)
    except UnsafeQueryError as e:
        return _err("ask_sql", e)

    final = ensure_limit(query, max_rows)
    if explain:
        final = "EXPLAIN (ANALYZE FALSE, BUFFERS FALSE, VERBOSE FALSE) " + final

    timeout = timeout_ms if timeout_ms is not None else ASK_SQL_TIMEOUT_MS
    timeout = max(500, min(timeout, 60_000))

    # Retry once if the pool is contended — transient on personal-scale data,
    # but the user shouldn't see "PoolTimeout" mid-conversation.
    last_err: Exception | None = None
    for attempt in range(2):
        t0 = time.perf_counter()
        try:
            with reader_conn() as c, c.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {timeout}")
                cur.execute(final)
                try:
                    rows = _serialize(cur.fetchall())
                except Exception:
                    rows = []
                cols = [d.name for d in (cur.description or [])]
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            truncated = len(rows) >= max_rows
            return _ok(
                "ask_sql",
                rows,
                truncated=truncated,
                warnings=[],
                extra={
                    "columns": cols,
                    "execution_ms": elapsed_ms,
                    "query_executed": final,
                    "attempts": attempt + 1,
                },
            )
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            # Only retry pool-timeout / transient connection errors.
            if attempt == 0 and ("pool" in msg or "timeout" in msg or "ssl" in msg):
                log.warning("ask_sql.retry", error=str(e))
                time.sleep(0.25)
                continue
            log.warning("ask_sql.exec_failed", error=str(e))
            return _err("ask_sql", e)

    return _err("ask_sql", last_err or RuntimeError("ask_sql: unknown failure"))


# ---- PushPress (programmed gym workouts) -----------------------------------
# Reads from the local mirror populated by ingest_pushpress. The API itself
# is private and rate-sensitive, so MCP tools never round-trip — they just
# select from fact_pushpress_*. Run refresh_data('pushpress') if today's
# programming was just published and you don't see it yet.

PUSHPRESS_LIST_LIMIT = 200


def _pushpress_class_type_filter(class_type: str | None) -> tuple[str, list]:
    """Build a SQL clause for the class_type arg. Accepts a UUID, a partial
    name match (ILIKE — 'crossfit' matches 'CrossFit & HIIT'), or None."""
    if not class_type:
        return "", []
    if "-" in class_type and len(class_type) >= 32:
        return "AND s.class_type_uuid = %s", [class_type]
    return "AND s.class_type_name ILIKE %s", [f"%{class_type}%"]


def list_pushpress_class_types() -> dict:
    """Class-type registry: each row is one programming track the gym runs
    (CrossFit & HIIT, Barbell / Weightlifting Club, HYROX, ...). Pass the
    `uuid` or any substring of `name` to the other PushPress tools to scope
    a query to one track."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT uuid, name, origin, is_static, progressive, last_day_num,
                   fetched_at,
                   (SELECT COUNT(*) FROM fact_pushpress_session s
                     WHERE s.class_type_uuid = ct.uuid) AS session_count
              FROM dim_pushpress_class_type ct
             ORDER BY name
            """
        )
        rows = _serialize(cur.fetchall())
    warnings: list[str] = []
    if not rows:
        warnings.append(
            "No PushPress class types yet. Bootstrap auth with "
            "`python -m ingest_pushpress login` then run "
            "`refresh_data('pushpress')`."
        )
    return _ok("list_pushpress_class_types", rows, warnings=warnings)


def _fetch_pushpress_sessions(
    where: list[str],
    params: list,
    *,
    limit: int = PUSHPRESS_LIST_LIMIT,
) -> list[dict]:
    """Shared SELECT for the get_pushpress_* tools. Returns rows with the
    parts list nested as JSON so a single round-trip gives Claude the full
    programming for each session."""
    q = f"""
        SELECT s.workout_uid, s.class_date, s.class_type_uuid, s.class_type_name,
               s.title, s.workout_state, s.origin, s.parts_count, s.divisions,
               s.published_on, s.publishing_date, s.updated_date,
               s.whoop_workout_id, s.fetched_at,
               COALESCE(
                 (SELECT jsonb_agg(
                    jsonb_build_object(
                      'ordinal', p.ordinal,
                      'part_uid', p.part_uid,
                      'title', p.title,
                      'workout_title', p.workout_title,
                      'description', p.description,
                      'score_type', p.score_type,
                      'score_count', p.score_count,
                      'set_count', p.set_count,
                      'default_reps', p.default_reps,
                      'divisions', p.divisions,
                      'unit', p.unit,
                      'athletes_notes', p.athletes_notes,
                      'coaches_notes', p.coaches_notes
                    )
                    ORDER BY p.ordinal
                  )
                  FROM fact_pushpress_part p
                  WHERE p.workout_uid = s.workout_uid),
                 '[]'::jsonb
               ) AS parts
          FROM fact_pushpress_session s
         WHERE {" AND ".join(where) if where else "TRUE"}
         ORDER BY s.class_date, s.class_type_name
         LIMIT {int(limit)}
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        return _serialize(cur.fetchall())


def get_pushpress_upcoming(
    days_ahead: int = 7,
    class_type: str | None = None,
) -> dict:
    """Programmed sessions from today through `days_ahead` days forward.
    Optional class_type filter accepts a UUID or any substring of the class
    name (case-insensitive). One row per programmed session, each with a
    nested `parts` array (POSTERIOR / WORKOUT OF THE DAY / etc., each with
    its prescribed lift, score type, and freeform description)."""
    if days_ahead < 0:
        return _err("get_pushpress_upcoming",
                    ValueError("days_ahead must be >= 0"))
    today = date.today()
    end = date.fromordinal(today.toordinal() + days_ahead)
    where = ["s.class_date BETWEEN %s AND %s"]
    params: list = [today, end]
    extra_clause, extra_params = _pushpress_class_type_filter(class_type)
    if extra_clause:
        where.append(extra_clause.removeprefix("AND ").strip())
        params.extend(extra_params)
    rows = _fetch_pushpress_sessions(where, params)
    warnings: list[str] = []
    if not rows:
        warnings.append(
            "No upcoming PushPress sessions in window. The gym usually "
            "publishes the next week's programming by 4 PM ET the prior "
            "week — try refresh_data('pushpress') if you expect there to "
            "be programming."
        )
    return _ok(
        "get_pushpress_upcoming",
        rows,
        warnings=warnings,
        extra={
            "window_start": today.isoformat(),
            "window_end": end.isoformat(),
            "class_type": class_type,
        },
    )


def get_pushpress_session(
    class_date: date,
    class_type: str,
) -> dict:
    """One programmed session for the given (class_date, class_type). Returns
    parts[] inline. class_type accepts UUID or substring of class name —
    if the substring matches multiple tracks, returns all matches and
    surfaces the ambiguity in `warnings`."""
    where = ["s.class_date = %s"]
    params: list = [class_date]
    extra_clause, extra_params = _pushpress_class_type_filter(class_type)
    if extra_clause:
        where.append(extra_clause.removeprefix("AND ").strip())
        params.extend(extra_params)
    rows = _fetch_pushpress_sessions(where, params, limit=10)

    warnings: list[str] = []
    if not rows:
        warnings.append(
            f"No programmed session for {class_date} matching '{class_type}'. "
            "Either the date is a rest day, or programming hasn't been "
            "published yet — try refresh_data('pushpress')."
        )
    elif len(rows) > 1:
        warnings.append(
            f"'{class_type}' matched {len(rows)} class types on {class_date}. "
            "Pass a more specific name or the UUID to disambiguate."
        )
    return _ok("get_pushpress_session", rows, warnings=warnings)


def get_pushpress_history(
    start_date: date,
    end_date: date,
    class_type: str | None = None,
) -> dict:
    """Range query over programmed sessions. Same shape as
    get_pushpress_upcoming but with explicit start/end dates so you can pull
    historical programming alongside fact_strength_workout / fact_workout to
    see what was programmed vs what you actually did."""
    if end_date < start_date:
        return _err("get_pushpress_history",
                    ValueError("end_date must be >= start_date"))
    where = ["s.class_date BETWEEN %s AND %s"]
    params: list = [start_date, end_date]
    extra_clause, extra_params = _pushpress_class_type_filter(class_type)
    if extra_clause:
        where.append(extra_clause.removeprefix("AND ").strip())
        params.extend(extra_params)
    rows = _fetch_pushpress_sessions(where, params)
    return _ok(
        "get_pushpress_history",
        rows,
        extra={
            "window_start": start_date.isoformat(),
            "window_end": end_date.isoformat(),
            "class_type": class_type,
        },
    )


# ---- Coach (parsed plan + load recommendations + review queue) ------------
# These read from pushpress_part_movement (populated by the coach pipeline)
# and surface the structured plan + reasoning to Claude. Never call the
# Anthropic API at MCP-tool time — that's for the cron path. If a workout
# hasn't been parsed yet, the tool returns the unparsed parts so Claude
# can still describe what was programmed.

def get_coach_plan(
    class_date: date | None = None,
    class_type: str | None = None,
) -> dict:
    """Today's recommended training plan: programmed movements with the
    coach's recommended load + the reasoning behind each suggestion.

    Defaults to today. Returns one row per programmed movement with the
    matched exercise (or analog), recommended kg, prescribed reps/sets,
    and a human-readable reasoning string. If a session hasn't been parsed
    yet, the row count is 0 and the warnings list says so."""
    target_date = class_date or date.today()
    where = ["m.class_date = %s"]
    params: list = [target_date]
    if class_type:
        if "-" in class_type and len(class_type) >= 32:
            where.append("s.class_type_uuid = %s")
        else:
            where.append("s.class_type_name ILIKE %s")
            class_type = f"%{class_type}%"
        params.append(class_type)

    q = f"""
        SELECT m.id, m.workout_uid, m.class_date, m.sequence,
               s.class_type_name, s.title AS session_title,
               s.workout_format, s.workout_rounds, s.workout_duration_s,
               s.parsed_at, s.hevy_routine_id, s.parser_confidence,
               m.raw_text, m.exercise_template_id,
               m.novel_exercise, m.analog_exercise_template_id,
               m.prescribed_reps, m.prescribed_sets, m.prescribed_load_kg,
               m.prescribed_load_pct, m.recommended_load_kg,
               m.recommendation_reasoning, m.recommendation_confidence,
               resolved.title AS exercise_title,
               analog.title AS analog_title
          FROM pushpress_part_movement m
          JOIN fact_pushpress_session s ON s.workout_uid = m.workout_uid
          LEFT JOIN dim_hevy_exercise resolved
            ON resolved.exercise_template_id = m.exercise_template_id
          LEFT JOIN dim_hevy_exercise analog
            ON analog.exercise_template_id = m.analog_exercise_template_id
         WHERE {" AND ".join(where)}
         ORDER BY s.class_type_name, m.sequence
         LIMIT 200
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = _serialize(cur.fetchall())

    warnings: list[str] = []
    if not rows:
        # Check if the session exists but isn't parsed yet.
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n,
                       COUNT(*) FILTER (WHERE parsed_at IS NULL) AS unparsed
                  FROM fact_pushpress_session
                 WHERE class_date = %s
                """,
                [target_date],
            )
            stats = cur.fetchone()
        if stats and stats["n"] > 0 and stats["unparsed"] > 0:
            warnings.append(
                f"{stats['unparsed']} session(s) on {target_date} not parsed yet. "
                "Run refresh_data('coach') or wait for the next cron tick."
            )
        elif stats and stats["n"] == 0:
            warnings.append(
                f"No PushPress session for {target_date}. "
                "Run refresh_data('pushpress') to fetch programming."
            )
    return _ok(
        "get_coach_plan",
        rows,
        warnings=warnings,
        extra={"class_date": target_date.isoformat(), "class_type": class_type},
    )


def list_coach_review_queue(limit: int = 50) -> dict:
    """Movements the parser/normalizer flagged as novel — not a confident
    match against the Hevy exercise catalog. The recommender used the
    suggested analog, but a human (or Claude) should confirm or override.

    Each row has the raw_text the coach wrote, the analog we picked, and
    the workout context. Resolve via update_coach_review (TODO) or just
    accept the analog by leaving resolved_at NULL — routines still go to
    Hevy with the analog load either way."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.movement_id, r.raw_text,
                   r.suggested_template_id, r.suggested_title,
                   r.created_at, r.resolved_at, r.resolved_template_id,
                   m.class_date, m.workout_uid, s.class_type_name,
                   s.title AS session_title
              FROM pushpress_movement_review r
              JOIN pushpress_part_movement m ON m.id = r.movement_id
              JOIN fact_pushpress_session s ON s.workout_uid = m.workout_uid
             WHERE r.resolved_at IS NULL
             ORDER BY m.class_date DESC, r.created_at DESC
             LIMIT %s
            """,
            [limit],
        )
        rows = _serialize(cur.fetchall())
    return _ok("list_coach_review_queue", rows)


def get_rep_maxes(
    exercise_search: str,
    include_estimated: bool = True,
) -> dict:
    """Every direct rep-max the user has hit for one exercise — the actual
    1RM, 3RM, 5RM, 8RM, 10RM, 12RM, 15RM weights they've logged in Hevy.
    Plus (with include_estimated=true) the Epley-projected 1RM from each
    rep count, so you can see which set is the user's strongest data point
    overall.

    Use this to ground load-recommendation conversations: 'you've hit
    102.5 kg for 5 in May → estimated 1RM 119 kg → today's 5×5 should be
    around X'. Resolves the exercise via dim_hevy_exercise.title (ILIKE).
    Picks the most-frequent template if multiple match."""
    if not exercise_search or not exercise_search.strip():
        return _err("get_rep_maxes", ValueError("exercise_search is required"))

    # Tier 1: direct ILIKE on the full search string. Catches the obvious
    # cases ('Bench Press (Barbell)').
    candidates: list[dict]
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT dhe.exercise_template_id, dhe.title,
                   COALESCE((
                     SELECT COUNT(*) FROM fact_strength_set fss
                      WHERE fss.exercise_template_id = dhe.exercise_template_id
                   ), 0) AS n_sets
              FROM dim_hevy_exercise dhe
             WHERE dhe.title ILIKE %s
             ORDER BY n_sets DESC, length(dhe.title), dhe.title
             LIMIT 5
            """,
            [f"%{exercise_search}%"],
        )
        candidates = _serialize(cur.fetchall())

    # Tier 2: route through the coach normalizer so 'back squat' lands on
    # Squat (Barbell) etc. Pure SQL path inside the normalizer (alias cache
    # + stemmed ILIKE) — won't make an LLM call unless we explicitly let it.
    if not candidates:
        from coach.normalizer import resolve as _resolve
        m = _resolve(exercise_search, write_alias=False)
        tid = m.exercise_template_id or m.analog_template_id
        if tid:
            with conn() as c, c.cursor() as cur:
                cur.execute(
                    """
                    SELECT exercise_template_id, title,
                           (SELECT COUNT(*) FROM fact_strength_set fss
                             WHERE fss.exercise_template_id = %s) AS n_sets
                      FROM dim_hevy_exercise
                     WHERE exercise_template_id = %s
                    """,
                    [tid, tid],
                )
                row = cur.fetchone()
                if row:
                    candidates = _serialize([row])

    if not candidates:
        return _err("get_rep_maxes", ValueError(
            f"No exercise matched '{exercise_search}'. "
            "Use find_exercise_templates to discover the exact catalog title."
        ))

    chosen = candidates[0]
    template_id = chosen["exercise_template_id"]
    other = [c["title"] for c in candidates[1:]]

    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT rep_count,
                   max_weight_kg,
                   last_hit_day,
                   sample_count
              FROM vw_exercise_rep_max
             WHERE exercise_template_id = %s
             ORDER BY rep_count
            """,
            [template_id],
        )
        rep_maxes = _serialize(cur.fetchall())

    rows: list[dict] = []
    best_estimated_1rm: float | None = None
    best_estimated_anchor: dict | None = None
    for r in rep_maxes:
        reps = int(r["rep_count"])
        w = float(r["max_weight_kg"])
        out: dict = {
            "rep_count": reps,
            "max_weight_kg": round(w, 2),
            "last_hit_day": r["last_hit_day"],
            "sample_count": r["sample_count"],
        }
        if include_estimated:
            est = w * (1.0 + reps / 30.0)
            out["estimated_1rm_kg"] = round(est, 2)
            if best_estimated_1rm is None or est > best_estimated_1rm:
                best_estimated_1rm = est
                best_estimated_anchor = out
        rows.append(out)

    extra: dict = {
        "exercise_template_id": template_id,
        "exercise_title": chosen["title"],
        "other_matches": other,
        "rep_max_count": len(rows),
    }
    if best_estimated_1rm is not None and rows:
        extra["best_estimated_1rm_kg"] = round(best_estimated_1rm, 2)
        extra["best_estimated_1rm_anchor"] = (
            f"{best_estimated_anchor['max_weight_kg']:g} kg × "
            f"{best_estimated_anchor['rep_count']} reps "
            f"on {best_estimated_anchor['last_hit_day']}"
        )

    warnings: list[str] = []
    if not rows:
        warnings.append(
            f"No working sets logged for {chosen['title']!r}. Once you log "
            "any normal-set in Hevy, future calls will populate the rep-max "
            "table automatically."
        )

    return _ok("get_rep_maxes", rows, warnings=warnings, extra=extra)


def record_workout_score(
    score: str,
    class_date: date | None = None,
    class_type: str | None = None,
    division: str | None = None,
    rx: bool | None = None,
    notes: str | None = None,
) -> dict:
    """Record the score for a metcon — the workout-level result that Hevy
    can't represent natively. Pass `score` as a human string and we infer
    the type:
      '8:42'        → score_type=time, 522 seconds
      '5+12'        → score_type=rounds_reps (5 rounds + 12 extra reps)
      '250 reps'    → score_type=total_reps, 250
      '120 kg'      → score_type=total_weight, 120
      '1500 m'      → score_type=distance, 1500

    Defaults: class_date=today, class_type matches the only parsed session
    on that date if there's exactly one. division is freeform; common
    values 'Performance', 'Fitness', 'RX'."""
    target_date = class_date or date.today()

    # Resolve the workout_uid.
    where = ["s.class_date = %s", "s.parsed_at IS NOT NULL"]
    params: list = [target_date]
    if class_type:
        if "-" in class_type and len(class_type) >= 32:
            where.append("s.class_type_uuid = %s")
        else:
            where.append("s.class_type_name ILIKE %s")
            class_type = f"%{class_type}%"
        params.append(class_type)
    with conn() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT s.workout_uid, s.class_type_name FROM fact_pushpress_session s "
            f"WHERE {' AND '.join(where)} ORDER BY s.class_type_name LIMIT 5",
            params,
        )
        sessions = cur.fetchall()
    if not sessions:
        return _err("record_workout_score", ValueError(
            f"No parsed PushPress session for {target_date}"
            + (f" matching {class_type!r}" if class_type else "")
        ))
    if len(sessions) > 1:
        names = ", ".join(s["class_type_name"] for s in sessions)
        return _err("record_workout_score", ValueError(
            f"Ambiguous: {len(sessions)} sessions match. "
            f"Pass class_type to disambiguate. Found: {names}"
        ))
    workout_uid = sessions[0]["workout_uid"]

    # Parse the score.
    s = score.strip().lower()
    score_type: str
    score_seconds: int | None = None
    score_int: int | None = None
    score_kg: float | None = None
    score_text: str | None = None

    import re
    if re.fullmatch(r"\d+:\d{1,2}", s):  # time format
        m, sec = s.split(":")
        score_seconds = int(m) * 60 + int(sec)
        score_type = "time"
    elif re.fullmatch(r"\d+\+\d+", s):  # rounds+reps
        score_text = s
        score_type = "rounds_reps"
    elif m := re.fullmatch(r"(\d+(?:\.\d+)?)\s*kg", s):
        score_kg = float(m.group(1))
        score_type = "total_weight"
    elif m := re.fullmatch(r"(\d+(?:\.\d+)?)\s*lb", s):
        score_kg = round(float(m.group(1)) * 0.4536, 2)
        score_type = "total_weight"
    elif m := re.fullmatch(r"(\d+)\s*m", s):
        score_int = int(m.group(1))
        score_type = "distance"
    elif m := re.fullmatch(r"(\d+)\s*reps?", s):
        score_int = int(m.group(1))
        score_type = "total_reps"
    elif s.isdigit():
        # Bare integer — assume reps for metcons.
        score_int = int(s)
        score_type = "total_reps"
    else:
        return _err("record_workout_score", ValueError(
            f"Couldn't parse score {score!r}. "
            "Use 'M:SS' for time, 'X+Y' for rounds+reps, '<n> reps', "
            "'<kg> kg' / '<lb> lb', or '<m> m'."
        ))

    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pushpress_workout_score
              (workout_uid, class_date, division, score_type,
               score_value_seconds, score_value_int, score_value_kg,
               score_value_text, rx, notes, source, recorded_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'manual', now())
            ON CONFLICT (workout_uid, division) DO UPDATE SET
              score_type = EXCLUDED.score_type,
              score_value_seconds = EXCLUDED.score_value_seconds,
              score_value_int = EXCLUDED.score_value_int,
              score_value_kg = EXCLUDED.score_value_kg,
              score_value_text = EXCLUDED.score_value_text,
              rx = EXCLUDED.rx,
              notes = EXCLUDED.notes,
              recorded_at = now()
            RETURNING id
            """,
            [workout_uid, target_date, division, score_type,
             score_seconds, score_int, score_kg, score_text, rx, notes],
        )
        score_id = cur.fetchone()["id"]
        c.commit()

    return _ok(
        "record_workout_score",
        [{
            "score_id": score_id,
            "workout_uid": workout_uid,
            "class_date": target_date.isoformat(),
            "class_type": sessions[0]["class_type_name"],
            "division": division,
            "score_type": score_type,
            "score": score,
            "rx": rx,
        }],
    )


def get_workout_scores(
    start_date: date,
    end_date: date,
    class_type: str | None = None,
) -> dict:
    """Recorded scores in [start_date, end_date]. Pair with
    get_strength_workouts for programmed-vs-performed analysis."""
    where = ["sc.class_date BETWEEN %s AND %s"]
    params: list = [start_date, end_date]
    if class_type:
        where.append("s.class_type_name ILIKE %s")
        params.append(f"%{class_type}%")
    q = f"""
        SELECT sc.id, sc.class_date, s.class_type_name, sc.division,
               sc.score_type, sc.score_value_seconds, sc.score_value_int,
               sc.score_value_kg, sc.score_value_text, sc.rx, sc.notes,
               sc.recorded_at,
               s.workout_format, s.title AS session_title
          FROM pushpress_workout_score sc
          JOIN fact_pushpress_session s ON s.workout_uid = sc.workout_uid
         WHERE {" AND ".join(where)}
         ORDER BY sc.class_date DESC
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        return _ok("get_workout_scores", _serialize(cur.fetchall()))


def override_coach_movement(
    movement_id: int,
    exercise_template_id: str,
    learn_alias: bool = True,
) -> dict:
    """Manually override the exercise mapping for one parsed movement.
    Use when the parser/normalizer picked the wrong Hevy template — common
    for CrossFit-specific movements where Hevy's catalog is incomplete.

    Updates pushpress_part_movement to point at the new template, clears
    the novel/analog flags, re-runs the load recommender so the
    recommendation reflects YOUR history at the right exercise, and (by
    default) writes an alias so future parses of the same raw_text land
    correctly.

    To find movement_ids, call get_coach_plan() — each row has an `id`."""
    from coach.normalizer import normalize_pattern
    from coach.recommend import recommend as _recommend

    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT id, raw_text, class_date,
                   prescribed_reps, prescribed_load_kg, prescribed_load_pct
              FROM pushpress_part_movement
             WHERE id = %s
            """,
            [movement_id],
        )
        m = cur.fetchone()
        if m is None:
            return _err("override_coach_movement",
                        ValueError(f"movement_id {movement_id} not found"))

        cur.execute(
            "SELECT title FROM dim_hevy_exercise WHERE exercise_template_id = %s",
            [exercise_template_id],
        )
        tpl = cur.fetchone()
        if tpl is None:
            return _err("override_coach_movement", ValueError(
                f"exercise_template_id {exercise_template_id!r} not in catalog. "
                "Use find_exercise_templates to discover valid ids."
            ))

        # Re-run the recommender with the corrected template_id.
        rec = _recommend(
            template_id=exercise_template_id,
            analog_template_id=None,
            prescribed_load_kg=(
                float(m["prescribed_load_kg"])
                if m["prescribed_load_kg"] is not None else None
            ),
            prescribed_load_pct=(
                float(m["prescribed_load_pct"])
                if m["prescribed_load_pct"] is not None else None
            ),
            prescribed_reps=m["prescribed_reps"],
            class_date=m["class_date"],
        )
        cur.execute(
            """
            UPDATE pushpress_part_movement
               SET exercise_template_id = %s,
                   novel_exercise = FALSE,
                   analog_exercise_template_id = NULL,
                   recommended_load_kg = %s,
                   recommendation_reasoning = %s,
                   recommendation_confidence = %s,
                   computed_at = now()
             WHERE id = %s
            """,
            [exercise_template_id, rec.recommended_load_kg, rec.reasoning,
             rec.confidence, movement_id],
        )

        # Auto-resolve any open review row for this movement.
        cur.execute(
            """
            UPDATE pushpress_movement_review
               SET resolved_template_id = %s, resolved_at = now()
             WHERE movement_id = %s AND resolved_at IS NULL
            """,
            [exercise_template_id, movement_id],
        )

        # Auto-learn the alias so future parses of the same raw_text
        # bypass the LLM tier and land directly on this template.
        if learn_alias:
            pattern = normalize_pattern(m["raw_text"])
            if pattern:
                cur.execute(
                    """
                    INSERT INTO pushpress_movement_alias
                      (pattern, exercise_template_id, source, hit_count, last_seen_at)
                    VALUES (%s, %s, 'manual', 1, now())
                    ON CONFLICT (pattern) DO UPDATE SET
                      exercise_template_id = EXCLUDED.exercise_template_id,
                      source = 'manual',
                      hit_count = pushpress_movement_alias.hit_count + 1,
                      last_seen_at = now()
                    """,
                    [pattern, exercise_template_id],
                )
        c.commit()

    return _ok(
        "override_coach_movement",
        [{
            "movement_id": movement_id,
            "raw_text": m["raw_text"],
            "exercise_template_id": exercise_template_id,
            "exercise_title": tpl["title"],
            "recommended_load_kg": rec.recommended_load_kg,
            "recommendation_reasoning": rec.reasoning,
            "alias_learned": learn_alias,
        }],
    )


def resolve_coach_review(
    review_id: int,
    exercise_template_id: str,
    notes: str | None = None,
) -> dict:
    """Mark a review row resolved with the user-chosen exercise. Also
    propagates the choice into pushpress_part_movement (so future Hevy
    syncs use the corrected exercise) and writes an alias so the same
    raw_text lands directly next time."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT movement_id, raw_text FROM pushpress_movement_review WHERE id = %s",
            [review_id],
        )
        row = cur.fetchone()
        if row is None:
            return _err("resolve_coach_review", ValueError(
                f"review_id {review_id} not found"
            ))
        movement_id = row["movement_id"]
        raw_text = row["raw_text"]
        cur.execute(
            """
            UPDATE pushpress_movement_review
               SET resolved_template_id = %s,
                   resolved_at = now(),
                   notes = %s
             WHERE id = %s
            """,
            [exercise_template_id, notes, review_id],
        )
        cur.execute(
            """
            UPDATE pushpress_part_movement
               SET exercise_template_id = %s,
                   novel_exercise = FALSE,
                   analog_exercise_template_id = NULL,
                   computed_at = now()
             WHERE id = %s
            """,
            [exercise_template_id, movement_id],
        )
        c.commit()

    # Auto-learn the alias so re-encounters short-circuit. Lazy import —
    # don't pull anthropic into the MCP tool path unless we need it (we
    # don't here; alias write is pure DB).
    from coach.normalizer import normalize_pattern
    pattern = normalize_pattern(raw_text)
    if pattern:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pushpress_movement_alias
                  (pattern, exercise_template_id, source, hit_count, last_seen_at)
                VALUES (%s, %s, 'manual', 1, now())
                ON CONFLICT (pattern) DO UPDATE SET
                  exercise_template_id = EXCLUDED.exercise_template_id,
                  source = 'manual',
                  last_seen_at = now()
                """,
                [pattern, exercise_template_id],
            )
            c.commit()
    return _ok("resolve_coach_review",
               [{"review_id": review_id,
                 "movement_id": movement_id,
                 "exercise_template_id": exercise_template_id}])


# ---- registry ---------------------------------------------------------------
# Tool name → callable + JSON schema (input). Used by server.py to wire MCP.
TOOLS: dict[str, dict] = {
    "get_schema_docs": {
        "fn": get_schema_docs,
        "description": "Return curated documentation about life-os tables, columns, conventions. CALL THIS FIRST for any analytical question.",
        "input": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string", "description": "Optional table to scope the docs."},
            },
        },
    },
    "get_daily_summary": {
        "fn": get_daily_summary,
        "description": "Daily-grain summary from mart_daily. Default columns are recovery, hrv, sleep, strain, meeting hours, kcal, spend.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "columns": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    "get_recovery_trend": {
        "fn": get_recovery_trend,
        "description": "Daily recovery, HRV, RHR, sleep duration. Optional trailing-N-day rolling averages.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "smoothing": {"type": "integer", "minimum": 2},
            },
        },
    },
    "get_sleep_summary": {
        "fn": get_sleep_summary,
        "description": "Per-day sleep metrics. Set include_naps=true to add nap counts/minutes.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "include_naps": {"type": "boolean", "default": False},
            },
        },
    },
    "get_workouts": {
        "fn": get_workouts,
        "description": (
            "Individual workouts from fact_workout (Whoop HR/strain view). "
            "Optional sport_name ILIKE filter. Each row carries the linked "
            "Hevy session's per-set rollup (hevy_workout_id, "
            "strength_total_volume_kg, strength_total_sets, "
            "strength_unique_exercises) when both apps logged the same "
            "physical workout — NULL columns mean no Hevy match (cardio, "
            "walks, etc.)."
        ),
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "sport_name": {"type": "string"},
            },
        },
    },
    "get_strength_workouts": {
        "fn": get_strength_workouts,
        "description": (
            "Hevy strength sessions with rollup metrics (total_sets, total_reps, "
            "total_volume_kg over working sets, unique_exercises) and the linked "
            "Whoop workout's strain + HR if the same session was logged in both. "
            "Use exercise_search (ILIKE) to scope to sessions that included a "
            "specific lift."
        ),
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date":   {"type": "string", "format": "date"},
                "exercise_search": {"type": "string"},
            },
        },
    },
    "get_strength_sets": {
        "fn": get_strength_sets,
        "description": (
            "Per-set Hevy data. Filters: exercise_search (ILIKE on title), "
            "set_type (warmup|normal|failure|dropset), working_sets_only "
            "(default true — drops warmup). Returns weight_kg, reps, rpe, "
            "set_type, exercise_title, distance/duration for every matching "
            "set within the window."
        ),
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date":   {"type": "string", "format": "date"},
                "exercise_search":   {"type": "string"},
                "set_type": {
                    "type": "string",
                    "enum": ["warmup", "normal", "failure", "dropset"],
                },
                "working_sets_only": {"type": "boolean", "default": True},
            },
        },
    },
    "get_exercise_progression": {
        "fn": get_exercise_progression,
        "description": (
            "Per-session progression for a specific exercise. exercise_search "
            "is ILIKE on fact_strength_set.exercise_title; if it matches "
            "multiple distinct titles, we anchor on the most-frequent one in "
            "the window (so 'squat' won't silently mix Front + Back Squat). "
            "Returns one row per session with top_weight_kg, "
            "top_reps_at_top_weight, top_set_volume_kg, session_volume_kg, "
            "and estimated_1rm_kg (Epley: w * (1 + r/30)). The summary block "
            "carries the current PR for the selected metric and a 30-day "
            "linear-trend % change. metric ∈ {top_weight, top_set_volume, "
            "session_volume, estimated_1rm}."
        ),
        "input": {
            "type": "object",
            "required": ["exercise_search", "start_date", "end_date"],
            "properties": {
                "exercise_search": {"type": "string"},
                "start_date": {"type": "string", "format": "date"},
                "end_date":   {"type": "string", "format": "date"},
                "metric": {
                    "type": "string",
                    "enum": ["top_weight", "top_set_volume", "session_volume", "estimated_1rm"],
                    "default": "top_weight",
                },
            },
        },
    },
    "get_strength_volume_trend": {
        "fn": get_strength_volume_trend,
        "description": (
            "Volume rollup over time across all strength training. "
            "granularity ∈ {day, week, month}. Set "
            "group_by_muscle_group=true to break each period out by "
            "dim_hevy_exercise.primary_muscle_group — handy for "
            "'am I hitting all muscle groups' / push-pull balance questions."
        ),
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date":   {"type": "string", "format": "date"},
                "granularity": {
                    "type": "string",
                    "enum": ["day", "week", "month"],
                    "default": "week",
                },
                "group_by_muscle_group": {"type": "boolean", "default": False},
            },
        },
    },
    "list_routines": {
        "fn": list_routines,
        "description": (
            "List Hevy routines (templates / programs) the user has saved. "
            "Optional folder_id (from list_routine_folders) and search "
            "(ILIKE on title). Each row carries the routine id, title, "
            "folder, exercise count, set count, and notes — call "
            "get_routine(id) for the full prescription."
        ),
        "input": {
            "type": "object",
            "properties": {
                "folder_id": {"type": "integer"},
                "search": {"type": "string"},
            },
        },
    },
    "list_routine_folders": {
        "fn": list_routine_folders,
        "description": (
            "List the user's routine folders, in display order. Returns "
            "folder_id (use as filter for list_routines), title, index, "
            "and routine_count."
        ),
        "input": {"type": "object", "properties": {}},
    },
    "get_routine": {
        "fn": get_routine,
        "description": (
            "Full payload for one routine — every exercise, every "
            "prescribed set, rest_seconds, notes, rep_range. Use this "
            "before logging a workout from a routine so you know the "
            "prescription. hevy_routine_id is from list_routines."
        ),
        "input": {
            "type": "object",
            "required": ["hevy_routine_id"],
            "properties": {"hevy_routine_id": {"type": "string"}},
        },
    },
    "get_exercise_history": {
        "fn": get_exercise_history,
        "description": (
            "Every set ever logged for one exercise, across ALL workouts in "
            "Hevy (live API call — authoritative even outside the local "
            "backfill window). exercise_search is ILIKE on the template "
            "catalog; if multiple titles match, the most-frequent in "
            "fact_strength_set is anchored. Returns one row per set "
            "(weight_kg, reps, set_type, RPE, workout_id, workout_start_time) "
            "plus a summary with top_weight_kg, estimated_1rm_kg, "
            "n_working_sets."
        ),
        "input": {
            "type": "object",
            "required": ["exercise_search"],
            "properties": {
                "exercise_search": {"type": "string"},
                "start_date": {"type": "string", "format": "date"},
                "end_date":   {"type": "string", "format": "date"},
                "limit": {"type": "integer", "default": 500, "minimum": 1, "maximum": 5000},
            },
        },
    },
    "get_food_log": {
        "fn": get_food_log,
        "description": "Per-item food log. Optional meal_window and food name search.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "meal_window": {"type": "string"},
                "search": {"type": "string"},
            },
        },
    },
    "get_meal_summary": {
        "fn": get_meal_summary,
        "description": "Per-meal rollup from mart_meal: kcal, macros, food names per meal_window.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "meal_window": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack"]},
            },
        },
    },
    "get_calendar_load": {
        "fn": get_calendar_load,
        "description": "Per-day meeting load and focus blocks from mart_daily.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
            },
        },
    },
    "get_calendar_events": {
        "fn": get_calendar_events,
        "description": "Individual calendar events. Optional classification + title search.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "classification": {
                    "type": "string",
                    "enum": ["meeting", "focus", "all_day", "declined", "personal"],
                },
                "search": {"type": "string"},
            },
        },
    },
    "get_spending": {
        "fn": get_spending,
        "description": (
            "Aggregated spending. group_by: day|week|month|category|merchant|account. "
            "Filters: category (ILIKE — pass exact_category=true for strict match; "
            "auto-escapes %, _, & so 'Bars & Nightlife' works), account_id (exact), "
            "account (ILIKE on account name), merchant (ILIKE)."
        ),
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "category": {"type": "string"},
                "group_by": {"type": "string", "enum": list(GROUP_BY_OPTIONS), "default": "day"},
                "account_id": {"type": "string"},
                "account": {"type": "string"},
                "exact_category": {"type": "boolean", "default": False},
                "merchant": {"type": "string"},
            },
        },
    },
    "get_transactions": {
        "fn": get_transactions,
        "description": (
            "Individual transactions with rich filters. "
            "category/merchant/account/tag are ILIKE substrings (auto-escape %, _, &); "
            "pass exact_category=true for strict category match. "
            "account_id / account_ids are exact. "
            "min_amount / max_amount compare against ABS(amount). "
            "only_charges=true drops income/refunds. "
            "Returns full Copilot metadata: tags, tag_ids, is_reviewed, tip_amount, parent_id, copilot_type."
        ),
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "category": {"type": "string"},
                "exact_category": {"type": "boolean", "default": False},
                "merchant": {"type": "string"},
                "min_amount": {"type": "number"},
                "max_amount": {"type": "number"},
                "tag": {"type": "string"},
                "has_no_tags": {"type": "boolean", "default": False},
                "untagged_for_couples": {"type": "boolean", "default": False},
                "account_id": {"type": "string"},
                "account": {"type": "string"},
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "exclude_excluded": {"type": "boolean", "default": True},
                "only_charges": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 500, "minimum": 1, "maximum": 1000},
            },
        },
    },
    "get_biometrics": {
        "fn": get_biometrics,
        "description": "If metric is omitted, lists available metrics with counts and date ranges. Otherwise returns measurements.",
        "input": {
            "type": "object",
            "properties": {
                "metric": {"type": "string"},
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
            },
        },
    },
    "correlate_metrics": {
        "fn": correlate_metrics,
        "description": (
            "Pearson + Spearman correlation between two mart_daily columns over "
            "a date range. Single-lag mode (default) returns the paired series + "
            "stats. Sweep mode (pass lag_range=[min,max], up to 21 lags) returns "
            "stats per lag plus the best-magnitude lag — use this to find when "
            "an effect is strongest without 21 separate calls. "
            "Available metrics include all spend categories: total_spend, "
            "alcohol_spend, bars_spend, entertainment_spend, restaurant_spend, "
            "dining_out_txn_count, etc."
        ),
        "input": {
            "type": "object",
            "required": ["metric_a", "metric_b", "start_date", "end_date"],
            "properties": {
                "metric_a": {"type": "string", "enum": sorted(CORRELATE_ALLOWLIST)},
                "metric_b": {"type": "string", "enum": sorted(CORRELATE_ALLOWLIST)},
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "lag_days": {"type": "integer", "default": 0},
                "lag_range": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 2, "maxItems": 2,
                },
                "method": {"type": "string", "enum": ["pearson", "spearman"], "default": "pearson"},
                "return_series": {"type": "boolean", "default": True},
            },
        },
    },
    "list_lab_tests": {
        "fn": list_lab_tests,
        "description": (
            "List all ingested Whoop Advanced Labs panels with per-panel "
            "biomarker counts (n optimal / sufficient / out-of-range). One "
            "row per test_id."
        ),
        "input": {"type": "object", "properties": {}},
    },
    "get_lab_results": {
        "fn": get_lab_results,
        "description": (
            "Whoop Advanced Labs biomarker results joined with reference info. "
            "Defaults to the most recent panel. Filters: biomarker_id (exact), "
            "status (OPTIMAL|SUFFICIENT|OUT_OF_RANGE), category (ILIKE — e.g. "
            "'Cardiometabolic', 'Hormones', 'Liver', 'Kidney', 'Inflammation', "
            "'Blood Count', 'Iron Metabolism', 'Vitamins & Minerals'), test_id, "
            "or search (substring across title/description). Each row carries "
            "the user's value, unit, optimal/sufficient ranges, description, "
            "what high/low means, and influencing factors. Out-of-range rows "
            "sort first. ALWAYS check this for any health/biomarker question."
        ),
        "input": {
            "type": "object",
            "properties": {
                "biomarker_id": {"type": "string"},
                "status": {"type": "string", "enum": ["OPTIMAL", "SUFFICIENT", "OUT_OF_RANGE"]},
                "category": {"type": "string"},
                "test_id": {"type": "string"},
                "search": {"type": "string"},
            },
        },
    },
    "get_biomarker_info": {
        "fn": get_biomarker_info,
        "description": (
            "Reference card for a single biomarker: description, optimal "
            "and sufficient ranges, clinical interpretation of high/low, "
            "what influences it, plus the user's most recent measured "
            "value if available. biomarker_id is the Whoop slug "
            "(e.g. 'apolipoprotein_b', 'vitamin_d', 'estradiol')."
        ),
        "input": {
            "type": "object",
            "required": ["biomarker_id"],
            "properties": {"biomarker_id": {"type": "string"}},
        },
    },
    "ask_sql": {
        "fn": ask_sql,
        "description": (
            "Read-only SQL escape hatch against curated tables/views. Forbidden "
            "keywords (INSERT/UPDATE/DELETE/...) rejected; runs as the lifeos_mcp "
            "role. Default statement_timeout=15s (override via timeout_ms, max 60s). "
            "On pool exhaustion, retried once automatically. Pass explain=true to "
            "get the EXPLAIN plan without executing."
        ),
        "input": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "max_rows": {"type": "integer", "default": ASK_SQL_DEFAULT_LIMIT, "minimum": 1, "maximum": 5000},
                "timeout_ms": {"type": "integer", "minimum": 500, "maximum": 60000},
                "explain": {"type": "boolean", "default": False},
            },
        },
    },
    "list_pushpress_class_types": {
        "fn": list_pushpress_class_types,
        "description": (
            "List the gym's PushPress class types (programming tracks) — "
            "e.g. 'CrossFit & HIIT', 'Barbell / Weightlifting Club', 'HYROX'. "
            "Use the returned uuid (or any substring of name) to scope "
            "get_pushpress_upcoming / get_pushpress_session / "
            "get_pushpress_history to a specific track."
        ),
        "input": {"type": "object", "properties": {}},
    },
    "get_pushpress_upcoming": {
        "fn": get_pushpress_upcoming,
        "description": (
            "Programmed gym workouts from today through `days_ahead` days "
            "forward. Each row is one programmed session with its parts[] "
            "inline (POSTERIOR, WORKOUT OF THE DAY, etc.) — each part has "
            "title, prescribed lift, score type, and freeform description. "
            "Optional class_type accepts a uuid or any substring of the "
            "class name (case-insensitive)."
        ),
        "input": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "default": 7, "minimum": 0, "maximum": 30},
                "class_type": {"type": "string"},
            },
        },
    },
    "get_pushpress_session": {
        "fn": get_pushpress_session,
        "description": (
            "One programmed session for a specific (class_date, class_type) "
            "pair. Returns parts[] inline. class_type accepts a uuid or "
            "substring of the class name — when the substring matches "
            "multiple tracks, all matches are returned with a warning."
        ),
        "input": {
            "type": "object",
            "required": ["class_date", "class_type"],
            "properties": {
                "class_date": {"type": "string", "format": "date"},
                "class_type": {"type": "string"},
            },
        },
    },
    "get_pushpress_history": {
        "fn": get_pushpress_history,
        "description": (
            "Range query over programmed gym sessions in [start_date, "
            "end_date]. Pair with get_strength_workouts / get_workouts to "
            "compare what was programmed against what you actually did. "
            "Optional class_type filter (uuid or substring of name)."
        ),
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date":   {"type": "string", "format": "date"},
                "class_type": {"type": "string"},
            },
        },
    },
    "get_coach_plan": {
        "fn": get_coach_plan,
        "description": (
            "Today's recommended training plan: each programmed movement "
            "with the matched Hevy exercise, recommended load (kg), the "
            "reasoning behind that suggestion, and prescribed reps/sets "
            "from the coach. Defaults to today; pass class_date for any "
            "other day. Optional class_type filter to scope to one track. "
            "If a session exists but isn't parsed yet, returns a warning "
            "telling you to run refresh_data('coach')."
        ),
        "input": {
            "type": "object",
            "properties": {
                "class_date": {"type": "string", "format": "date"},
                "class_type": {"type": "string"},
            },
        },
    },
    "get_rep_maxes": {
        "fn": get_rep_maxes,
        "description": (
            "Every rep-max the user has logged for one exercise — actual "
            "1RM/3RM/5RM/8RM/10RM/12RM/15RM weights from fact_strength_set, "
            "plus (default) the Epley-projected 1RM from each rep count. "
            "Use this to ground load conversations: a 5RM = 100kg implies "
            "1RM ≈ 117kg, which the recommender uses for percentage-based "
            "prescriptions. exercise_search is ILIKE on dim_hevy_exercise.title."
        ),
        "input": {
            "type": "object",
            "required": ["exercise_search"],
            "properties": {
                "exercise_search": {"type": "string"},
                "include_estimated": {"type": "boolean", "default": True},
            },
        },
    },
    "record_workout_score": {
        "fn": record_workout_score,
        "description": (
            "Record the metcon score for a session — the workout-level "
            "result Hevy can't represent (Hevy logs atomic sets only). "
            "Pass `score` as a human string and we infer the type: "
            "'8:42' for time, '5+12' for rounds+reps, '250 reps' for "
            "total reps, '120 kg' for total weight, '1500 m' for distance. "
            "Defaults: class_date=today, class_type auto-detected when "
            "there's only one parsed session that day. Use after the "
            "workout (e.g. 'I just finished today's CrossFit, scored 9:14 RX')."
        ),
        "input": {
            "type": "object",
            "required": ["score"],
            "properties": {
                "score": {"type": "string"},
                "class_date": {"type": "string", "format": "date"},
                "class_type": {"type": "string"},
                "division": {"type": "string"},
                "rx": {"type": "boolean"},
                "notes": {"type": "string"},
            },
        },
    },
    "get_workout_scores": {
        "fn": get_workout_scores,
        "description": (
            "Recorded metcon scores in [start_date, end_date]. Joins to "
            "fact_pushpress_session for the workout context. Use to track "
            "performance trends ('have my Fran times improved this month')."
        ),
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "class_type": {"type": "string"},
            },
        },
    },
    "override_coach_movement": {
        "fn": override_coach_movement,
        "description": (
            "Manually override the exercise mapping for one parsed movement "
            "(use when the parser/normalizer picked the wrong Hevy template). "
            "Updates the movement to point at the chosen template, re-runs "
            "the load recommender (so the suggested kg reflects YOUR PRs at "
            "the corrected exercise), and learns an alias so future parses "
            "of the same raw text land directly on this template. After "
            "overriding, run refresh_data('coach') (or wait for the next cron) "
            "to push the corrected routine to Hevy. Find movement_ids via "
            "get_coach_plan."
        ),
        "input": {
            "type": "object",
            "required": ["movement_id", "exercise_template_id"],
            "properties": {
                "movement_id": {"type": "integer"},
                "exercise_template_id": {"type": "string"},
                "learn_alias": {"type": "boolean", "default": True},
            },
        },
    },
    "list_coach_review_queue": {
        "fn": list_coach_review_queue,
        "description": (
            "Movements the parser flagged as novel (no confident exercise "
            "match in the Hevy catalog). Each row carries the raw text the "
            "coach wrote, the analog we picked, and the workout context. "
            "Use resolve_coach_review to confirm or override — that also "
            "auto-adds the alias so the same wording lands directly next "
            "time. Routines still sync to Hevy using the analog while "
            "rows stay unresolved."
        ),
        "input": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
        },
    },
    "resolve_coach_review": {
        "fn": resolve_coach_review,
        "description": (
            "Resolve a coach-review row. Sets resolved_template_id, updates "
            "the underlying pushpress_part_movement to point at the chosen "
            "exercise, and writes an alias so future encounters short-"
            "circuit to a direct match. After resolving, run "
            "refresh_data('coach') (or wait for the next cron) to push the "
            "corrected routine to Hevy."
        ),
        "input": {
            "type": "object",
            "required": ["review_id", "exercise_template_id"],
            "properties": {
                "review_id": {"type": "integer"},
                "exercise_template_id": {"type": "string"},
                "notes": {"type": "string"},
            },
        },
    },
}


def call(name: str, args: dict) -> dict:
    """Invoke a tool by name with a kwargs dict. Coerce date strings to date()."""
    spec = TOOLS.get(name)
    if spec is None:
        return _err(name, ValueError(f"Unknown tool: {name}"))

    coerced = _coerce_args(args, spec["input"])
    try:
        return spec["fn"](**coerced)
    except TypeError as e:
        return _err(name, e)


def _coerce_args(args: dict, schema: dict) -> dict:
    """Convert ISO date strings into date objects so tool fns get the types
    they declared, regardless of what the MCP client passed."""
    out = dict(args)
    props = schema.get("properties", {})
    for key, spec in props.items():
        if key not in out or out[key] is None:
            continue
        if spec.get("format") == "date" and isinstance(out[key], str):
            out[key] = date.fromisoformat(out[key])
    return out

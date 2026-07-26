"""MCP tools over the unified data layer.

These are the tools to reach for by default. The older per-source tools
(`get_workouts`, `get_strength_workouts`, `get_whoop_lift_workouts`, ...) still
work and still read their own vendor tables, but they each see one slice of
the truth. Everything here reads the deduplicated, SI-unit, quality-filtered
canonical tables, so a single call covers Whoop, Garmin, Hevy and the file
imports at once.

Two conventions across every read tool here:

  include_flagged=False    rows that unify.quality flagged as implausible are
                           excluded by default. Pass True to see them, with
                           the reason attached.
  source provenance        every row says which source produced it, because
                           "165 lb" from a bathroom scale and from a DXA are
                           not interchangeable claims.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from lifeos_core.db import conn, tx
from lifeos_core.logging import get_logger
from mcp_server.tools import _err, _ok, _serialize

log = get_logger(__name__)

MAX_ROWS = 1000


# ---------------------------------------------------------------------------
# Freshness -- GAP-8. The first call in any analytical session.
# ---------------------------------------------------------------------------

def get_data_freshness(domain: str | None = None,
                       problems_only: bool = False) -> dict:
    """Per-source last row, lag, SLA and status."""
    q = """
        SELECT source_key, domain, display_name, mode, status,
               last_row_day, lag_hours, sla_hours,
               coverage_start, coverage_end, notes
          FROM vw_data_freshness
         WHERE (%s::text IS NULL OR domain = %s)
           AND (NOT %s OR status IN ('stale','lagging','no_data'))
         ORDER BY CASE status WHEN 'stale' THEN 0 WHEN 'lagging' THEN 1
                              WHEN 'no_data' THEN 2 ELSE 3 END, source_key
    """
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(q, [domain, domain, problems_only])
            rows = _serialize(cur.fetchall())
    except Exception as e:
        return _err("get_data_freshness", e)

    warnings = []
    bad = [r for r in rows if r["status"] in ("stale", "no_data")]
    if bad:
        warnings.append(
            "Stale sources: " + ", ".join(f"{r['source_key']} (last {r['last_row_day']})"
                                          for r in bad))
    return _ok("get_data_freshness", rows, warnings=warnings)


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------

def get_activity(start_date: date, end_date: date,
                 activity_type: str | None = None,
                 source: str | None = None,
                 resistance_only: bool = False,
                 include_flagged: bool = False) -> dict:
    """One row per training session, any source, deduplicated."""
    q = """
        SELECT day, start_ts, activity_type, name, source, duration_min,
               strain, training_load, avg_hr, max_hr, kcal, hard_minutes,
               zone_4_s, zone_5_s, distance_m, total_sets, total_reps,
               total_volume_kg, unique_exercises, is_resistance,
               source_count, is_flagged, activity_uid
          FROM vw_activity
         WHERE day BETWEEN %s AND %s
           AND (%s::text IS NULL OR activity_type = %s)
           AND (%s::text IS NULL OR source = %s)
           AND (NOT %s OR is_resistance)
           AND (%s OR NOT is_flagged)
         ORDER BY start_ts
    """
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(q, [start_date, end_date, activity_type, activity_type,
                            source, source, resistance_only, include_flagged])
            rows = _serialize(cur.fetchall())
    except Exception as e:
        return _err("get_activity", e)
    truncated = len(rows) > MAX_ROWS
    return _ok("get_activity", rows[:MAX_ROWS], truncated=truncated)


def get_activity_exercises(start_date: date, end_date: date,
                           exercise: str | None = None,
                           muscle: str | None = None,
                           exclude_spinal_flexion: bool = False) -> dict:
    """Per-exercise volume across every source.

    Garmin rows carry granularity='exercise' (the export only has aggregates);
    Whoop and Hevy rows carry granularity='set' and are derived from real sets.
    """
    q = """
        SELECT day, start_ts, session_name, source, exercise_key, exercise_name,
               vendor_exercise, granularity, set_count, total_reps,
               total_volume_kg, max_weight_kg, duration_s, is_pr,
               movement_pattern, primary_muscle, equipment, is_compound,
               is_spinal_flexion
          FROM vw_activity_exercise
         WHERE day BETWEEN %s AND %s
           AND (%s::text IS NULL OR exercise_key = %s
                OR exercise_name ILIKE '%%' || %s || '%%')
           AND (%s::text IS NULL OR primary_muscle = %s)
           AND (NOT %s OR NOT COALESCE(is_spinal_flexion, false))
         ORDER BY day, exercise_index
    """
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(q, [start_date, end_date, exercise, exercise, exercise,
                            muscle, muscle, exclude_spinal_flexion])
            rows = _serialize(cur.fetchall())
    except Exception as e:
        return _err("get_activity_exercises", e)
    truncated = len(rows) > MAX_ROWS
    return _ok("get_activity_exercises", rows[:MAX_ROWS], truncated=truncated)


def get_activity_sets(start_date: date, end_date: date,
                      exercise: str | None = None) -> dict:
    """True per-set detail. Only sources that record sets individually
    (Whoop Strength Trainer, Hevy) appear here -- Garmin never exported sets,
    so use get_activity_exercises for the Dec 2024 - Aug 2025 window."""
    q = """
        SELECT day, start_ts, session_name, source, exercise_key, exercise_name,
               set_index, reps, weight_kg, weight_lb, volume_kg, duration_s,
               rpe, avg_hr, is_pr, movement_pattern, primary_muscle
          FROM vw_activity_set
         WHERE day BETWEEN %s AND %s
           AND (%s::text IS NULL OR exercise_key = %s
                OR exercise_name ILIKE '%%' || %s || '%%')
         ORDER BY day, exercise_index, set_index
    """
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(q, [start_date, end_date, exercise, exercise, exercise])
            rows = _serialize(cur.fetchall())
    except Exception as e:
        return _err("get_activity_sets", e)
    truncated = len(rows) > MAX_ROWS
    return _ok("get_activity_sets", rows[:MAX_ROWS], truncated=truncated)


def get_adherence(weeks: int = 12, as_of: date | None = None) -> dict:
    """GAP-10: rolling sessions/week, current streak, longest gap.

    Four training blocks in three years were all abandoned between month four
    and month six. Rolling 12-week sessions/week is the variable that moves
    first, so `below_floor` (12-week rate under 3 for 2+ consecutive weeks) is
    the early warning, not a retrospective.
    """
    as_of = as_of or date.today()
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT day, sessions, resistance_sessions,
                       sessions_per_week_4w, sessions_per_week_12w,
                       sessions_per_week_26w, resistance_per_week_12w, below_floor
                  FROM vw_adherence WHERE day = %s
                """, [as_of])
            current = cur.fetchone()

            window_start = as_of - timedelta(weeks=weeks)
            cur.execute(
                "SELECT day, session_count FROM vw_training_day "
                "WHERE day BETWEEN %s AND %s ORDER BY day",
                [window_start, as_of])
            days = cur.fetchall()

            cur.execute(
                "SELECT count(*) AS n FROM vw_adherence "
                "WHERE below_floor AND day BETWEEN %s AND %s",
                [window_start, as_of])
            below = cur.fetchone()["n"]
    except Exception as e:
        return _err("get_adherence", e)

    trained = {d["day"] for d in days}
    # Current streak = consecutive weeks (not days) with at least one session.
    streak_weeks = 0
    probe = as_of
    while probe >= window_start:
        week_days = {probe - timedelta(days=i) for i in range(7)}
        if trained & week_days:
            streak_weeks += 1
            probe -= timedelta(days=7)
        else:
            break

    longest_gap, gap, prev = 0, 0, None
    for d in sorted(trained):
        if prev is not None:
            gap = (d - prev).days - 1
            longest_gap = max(longest_gap, gap)
        prev = d
    if prev is not None:
        longest_gap = max(longest_gap, (as_of - prev).days)

    row = {
        "as_of": as_of,
        "window_weeks": weeks,
        "sessions_per_week_4w": current["sessions_per_week_4w"] if current else None,
        "sessions_per_week_12w": current["sessions_per_week_12w"] if current else None,
        "sessions_per_week_26w": current["sessions_per_week_26w"] if current else None,
        "resistance_per_week_12w": current["resistance_per_week_12w"] if current else None,
        "below_floor": current["below_floor"] if current else None,
        "days_below_floor_in_window": below,
        "current_streak_weeks": streak_weeks,
        "longest_gap_days": longest_gap,
        "training_days_in_window": len(trained),
    }
    warnings = []
    if current and current["below_floor"]:
        warnings.append(
            "12-week rate is below 3 sessions/week -- this is the pattern that "
            "preceded each abandoned block.")
    return _ok("get_adherence", _serialize([row]), warnings=warnings)


# ---------------------------------------------------------------------------
# Body composition
# ---------------------------------------------------------------------------

def get_body_composition(start_date: date, end_date: date,
                         method: str | None = None,
                         daily: bool = True,
                         include_flagged: bool = False) -> dict:
    """Weight and body composition with the measurement method attached.

    `daily=True` returns one best reading per day (DXA beats BodPod beats
    scale beats wrist bioimpedance). `daily=False` returns every raw reading,
    which is what you want when checking whether a day's readings disagree.
    """
    if daily:
        q = """
            SELECT day, weight_kg, weight_lb, weight_method, weight_source,
                   body_fat_pct, lean_mass_kg, fat_mass_kg, bf_method, bf_source
              FROM vw_body_daily
             WHERE day BETWEEN %s AND %s ORDER BY day
        """
        params: list = [start_date, end_date]
    else:
        q = """
            SELECT day, measured_at, method, source, weight_kg, weight_lb,
                   body_fat_pct, lean_mass_kg, fat_mass_kg, bmi, visceral_fat,
                   bone_mineral_kg, confidence, is_flagged, measurement_uid
              FROM vw_body_composition
             WHERE day BETWEEN %s AND %s
               AND (%s::text IS NULL OR method = %s)
               AND (%s OR NOT is_flagged)
             ORDER BY measured_at
        """
        params = [start_date, end_date, method, method, include_flagged]
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(q, params)
            rows = _serialize(cur.fetchall())
    except Exception as e:
        return _err("get_body_composition", e)
    truncated = len(rows) > MAX_ROWS
    return _ok("get_body_composition", rows[:MAX_ROWS], truncated=truncated)


def log_body_composition(measured_on: str, method: str,
                         weight_kg: float | None = None,
                         weight_lb: float | None = None,
                         body_fat_pct: float | None = None,
                         lean_mass_kg: float | None = None,
                         fat_mass_kg: float | None = None,
                         bone_mineral_kg: float | None = None,
                         visceral_fat: float | None = None,
                         region: dict | None = None,
                         notes: str | None = None) -> dict:
    """Record a body-composition measurement -- this is where a DXA goes.

    method must be one of: dxa, bodpod, bioimpedance, calipers, tape, scale,
    manual. DXA outranks every other method on read, so a scan entered here
    immediately becomes the authoritative body-fat number for its date.
    """
    valid = {"dxa", "bodpod", "bioimpedance", "calipers", "tape", "scale", "manual"}
    method = (method or "").lower().strip()
    if method not in valid:
        return _err("log_body_composition",
                    ValueError(f"method must be one of {sorted(valid)}"))
    day = date.fromisoformat(measured_on) if isinstance(measured_on, str) else measured_on
    if weight_kg is None and weight_lb is not None:
        weight_kg = float(weight_lb) * 0.45359237
    if weight_kg and body_fat_pct is not None:
        lean_mass_kg = lean_mass_kg if lean_mass_kg is not None else weight_kg * (1 - body_fat_pct / 100)
        fat_mass_kg = fat_mass_kg if fat_mass_kg is not None else weight_kg * body_fat_pct / 100

    confidence = {"dxa": 1.0, "bodpod": 0.9, "calipers": 0.6, "scale": 0.6,
                  "bioimpedance": 0.4, "tape": 0.4, "manual": 0.5}[method]
    ts = datetime.combine(day, datetime.min.time())
    uid = f"manual:{method}:{int(ts.timestamp())}"

    try:
        with tx() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fact_body_composition
                  (measurement_uid, source, method, measured_at, day, weight_kg,
                   body_fat_pct, lean_mass_kg, fat_mass_kg, bone_mineral_kg,
                   visceral_fat, region, confidence, payload)
                VALUES (%s,'manual',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)
                ON CONFLICT (measurement_uid) DO UPDATE SET
                  weight_kg = EXCLUDED.weight_kg,
                  body_fat_pct = EXCLUDED.body_fat_pct,
                  lean_mass_kg = EXCLUDED.lean_mass_kg,
                  fat_mass_kg = EXCLUDED.fat_mass_kg,
                  bone_mineral_kg = EXCLUDED.bone_mineral_kg,
                  visceral_fat = EXCLUDED.visceral_fat,
                  region = EXCLUDED.region,
                  confidence = EXCLUDED.confidence,
                  payload = EXCLUDED.payload,
                  updated_at = now()
                RETURNING measurement_uid
                """,
                [uid, method, ts, day, weight_kg, body_fat_pct, lean_mass_kg,
                 fat_mass_kg, bone_mineral_kg, visceral_fat,
                 __import__("json").dumps(region or {}), confidence,
                 __import__("json").dumps({"notes": notes} if notes else {})],
            )
            written = cur.fetchone()["measurement_uid"]
    except Exception as e:
        return _err("log_body_composition", e)
    return _ok("log_body_composition",
               [{"measurement_uid": written, "day": str(day), "method": method,
                 "weight_kg": weight_kg, "body_fat_pct": body_fat_pct}],
               warnings=["Run `python -m mart_refresh` (or wait for the nightly "
                         "rebuild) for this to appear in mart_daily."])


# ---------------------------------------------------------------------------
# Nutrition
# ---------------------------------------------------------------------------

def get_nutrition(start_date: date, end_date: date,
                  per_item: bool = False,
                  source: str | None = None) -> dict:
    """Daily nutrition totals, or every logged item when per_item=True.

    `source` on each daily row says which logger the numbers came from.
    Days missing entirely mean nothing was logged anywhere that day -- check
    get_data_freshness before concluding the intake was low.
    """
    if per_item:
        q = """
            SELECT day, eaten_at, meal, food_name, source, amount, unit,
                   energy_kcal, protein_g, carbs_g, fiber_g, sugar_g, fat_g,
                   saturated_fat_g, sodium_mg, caffeine_mg, alcohol_g
              FROM fact_nutrition_entry
             WHERE day BETWEEN %s AND %s
               AND (%s::text IS NULL OR source = %s)
             ORDER BY eaten_at
        """
    else:
        q = """
            SELECT day, source, energy_kcal, protein_g, carbs_g, net_carbs_g,
                   fiber_g, sugar_g, fat_g, saturated_fat_g, sodium_mg,
                   caffeine_mg, alcohol_g, entry_count, first_eaten_at,
                   last_eaten_at, eating_window_hours, source_count
              FROM vw_nutrition_daily
             WHERE day BETWEEN %s AND %s
               AND (%s::text IS NULL OR source = %s)
             ORDER BY day
        """
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(q, [start_date, end_date, source, source])
            rows = _serialize(cur.fetchall())
    except Exception as e:
        return _err("get_nutrition", e)
    truncated = len(rows) > MAX_ROWS
    return _ok("get_nutrition", rows[:MAX_ROWS], truncated=truncated)


# ---------------------------------------------------------------------------
# Labs
# ---------------------------------------------------------------------------

def get_lab_panels(start_date: date | None = None,
                   end_date: date | None = None,
                   include_duplicates: bool = False) -> dict:
    """Every blood draw. Duplicated draws (the same collection reported by two
    systems) are collapsed unless include_duplicates=True."""
    q = """
        SELECT panel_uid, collected_on, provider, source, panel_name,
               result_count, cluster_id, is_primary
          FROM fact_lab_panel
         WHERE (%s::date IS NULL OR collected_on >= %s)
           AND (%s::date IS NULL OR collected_on <= %s)
           AND (%s OR is_primary)
         ORDER BY collected_on DESC
    """
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(q, [start_date, start_date, end_date, end_date,
                            include_duplicates])
            rows = _serialize(cur.fetchall())
    except Exception as e:
        return _err("get_lab_panels", e)
    return _ok("get_lab_panels", rows)


def get_lab_results(biomarker: str | None = None,
                    category: str | None = None,
                    start_date: date | None = None,
                    end_date: date | None = None,
                    include_duplicates: bool = False) -> dict:
    """Lab values on canonical biomarker keys.

    Whoop Advanced Labs and a Quest report spell the same analyte differently
    (`alanine_aminotransferase` vs `alt`); both resolve to `alt` here, so a
    trend query sees one series rather than two half-series.
    """
    q = """
        SELECT m.collected_on, m.biomarker_key, m.display_name, d.category,
               m.value_numeric, m.value_text, m.unit, m.ref_low, m.ref_high,
               m.status, m.source, m.vendor_key, p.provider
          FROM fact_lab_measurement m
          LEFT JOIN dim_biomarker d ON d.biomarker_key = m.biomarker_key
          LEFT JOIN fact_lab_panel p ON p.panel_uid = m.panel_uid
         WHERE (%s::text IS NULL OR m.biomarker_key = %s)
           AND (%s::text IS NULL OR d.category = %s)
           AND (%s::date IS NULL OR m.collected_on >= %s)
           AND (%s::date IS NULL OR m.collected_on <= %s)
           AND (%s OR m.is_primary)
         ORDER BY m.collected_on DESC, d.category, m.biomarker_key
    """
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(q, [biomarker, biomarker, category, category,
                            start_date, start_date, end_date, end_date,
                            include_duplicates])
            rows = _serialize(cur.fetchall())
    except Exception as e:
        return _err("get_lab_results", e)
    truncated = len(rows) > MAX_ROWS
    return _ok("get_lab_results", rows[:MAX_ROWS], truncated=truncated)


def get_imaging(start_date: date | None = None,
                modality: str | None = None) -> dict:
    """Radiology studies: MRI, CT, X-ray, ultrasound, DEXA."""
    q = """
        SELECT study_uid, study_date, modality, body_region, provider,
               ordering_reason, impression, findings, source
          FROM fact_imaging
         WHERE (%s::date IS NULL OR study_date >= %s)
           AND (%s::text IS NULL OR modality = upper(%s))
         ORDER BY study_date DESC
    """
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(q, [start_date, start_date, modality, modality])
            rows = _serialize(cur.fetchall())
    except Exception as e:
        return _err("get_imaging", e)
    return _ok("get_imaging", rows)


# ---------------------------------------------------------------------------
# Coverage + data quality
# ---------------------------------------------------------------------------

def get_coverage(domain: str | None = None, min_gap_days: int = 4,
                 start_date: date | None = None) -> dict:
    """Which days have exercise / sleep / nutrition / weight data, and the
    contiguous stretches that don't."""
    from unify.coverage import report

    try:
        with conn() as c:
            rep = report(c, start_date)
    except Exception as e:
        return _err("get_coverage", e)
    if domain:
        rep["gaps"] = [g for g in rep.get("gaps", []) if g["domain"] == domain]
    rep["gaps"] = [g for g in rep.get("gaps", []) if g["days"] >= min_gap_days]
    return _ok("get_coverage", _serialize([rep]))


def get_data_quality_flags(start_date: date | None = None,
                           table_name: str | None = None,
                           rule: str | None = None) -> dict:
    """Readings the quality rules judged implausible. Nothing is ever deleted;
    these rows still exist in the fact tables and are excluded from read paths
    by default."""
    q = """
        SELECT table_name, row_key, day, metric, value, reason, rule,
               severity, flagged_by, resolved, created_at
          FROM data_quality_flag
         WHERE NOT resolved
           AND (%s::date IS NULL OR day >= %s)
           AND (%s::text IS NULL OR table_name = %s)
           AND (%s::text IS NULL OR rule = %s)
         ORDER BY day DESC NULLS LAST, table_name
    """
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(q, [start_date, start_date, table_name, table_name,
                            rule, rule])
            rows = _serialize(cur.fetchall())
    except Exception as e:
        return _err("get_data_quality_flags", e)
    truncated = len(rows) > MAX_ROWS
    return _ok("get_data_quality_flags", rows[:MAX_ROWS], truncated=truncated)


def flag_data_quality(table_name: str, row_key: str, reason: str,
                      day: str | None = None, metric: str | None = None,
                      severity: str = "warn") -> dict:
    """Mark a row as suspect by hand. Human-entered flags survive the automatic
    re-flagging pass, which only clears its own verdicts."""
    d = date.fromisoformat(day) if isinstance(day, str) and day else None
    try:
        with tx() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO data_quality_flag
                  (table_name, row_key, day, metric, reason, rule, severity, flagged_by)
                VALUES (%s,%s,%s,%s,%s,'manual',%s,'user')
                ON CONFLICT (table_name, row_key, rule, metric) DO UPDATE SET
                  reason = EXCLUDED.reason, severity = EXCLUDED.severity,
                  day = EXCLUDED.day, resolved = FALSE, created_at = now()
                RETURNING id
                """,
                [table_name, row_key, d, metric, reason, severity])
            new_id = cur.fetchone()["id"]
    except Exception as e:
        return _err("flag_data_quality", e)
    return _ok("flag_data_quality", [{"id": new_id, "table_name": table_name,
                                      "row_key": row_key, "reason": reason}])


def resolve_data_quality_flag(flag_id: int) -> dict:
    """Clear a flag -- the reading was real after all."""
    try:
        with tx() as c, c.cursor() as cur:
            cur.execute("UPDATE data_quality_flag SET resolved = TRUE "
                        "WHERE id = %s RETURNING id", [flag_id])
            row = cur.fetchone()
    except Exception as e:
        return _err("resolve_data_quality_flag", e)
    if not row:
        return _err("resolve_data_quality_flag", ValueError(f"no flag id={flag_id}"))
    return _ok("resolve_data_quality_flag", [{"id": flag_id, "resolved": True}])

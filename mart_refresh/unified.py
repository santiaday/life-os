"""Unified-layer overlay for mart_daily.

Runs after the existing rebuild. Two jobs:

1. **Extend the spine.** The old rebuild starts the day series at the earliest
   Whoop/Cronometer row. The unified layer reaches back to 2023-08-14 via Zero
   and the Cronometer Apple Health passthrough, so days before the old start
   need to exist before they can be filled.

2. **Overwrite the domain columns** -- training, sleep, nutrition, body
   composition, movement, fasting -- from the unified views, which are
   cross-source, deduplicated, and exclude rows flagged by unify.quality.

Everything else in mart_daily (calendar, spending, journal pivots, body image)
is left exactly as the existing rebuild wrote it.
"""

from __future__ import annotations

from lifeos_core.db import tx
from lifeos_core.logging import get_logger

log = get_logger(__name__)


EXTEND_SPINE = """
INSERT INTO mart_daily (day, refreshed_at)
SELECT c.day, now()
  FROM vw_coverage_daily c
  LEFT JOIN mart_daily m ON m.day = c.day
 WHERE m.day IS NULL
ON CONFLICT (day) DO NOTHING
"""

# --- training ---------------------------------------------------------------
OVERLAY_ACTIVITY = """
UPDATE mart_daily m SET
  workout_count      = COALESCE(t.session_count, 0),
  workout_total_min  = COALESCE(ROUND(t.total_min, 1), 0),
  workout_max_strain = t.max_strain,
  hard_minutes       = ROUND(t.hard_minutes, 1),
  resistance_count   = COALESCE(t.resistance_count, 0),
  activity_sources   = t.sources,
  strength_total_volume_kg = COALESCE(ROUND(t.volume_kg, 1), 0),
  refreshed_at       = now()
FROM (
  SELECT day,
         COUNT(*)                                              AS session_count,
         COUNT(*) FILTER (WHERE is_resistance)                 AS resistance_count,
         SUM(duration_s) / 60.0                                AS total_min,
         SUM(COALESCE(total_volume_kg, 0))                     AS volume_kg,
         SUM((COALESCE(zone_4_s,0) + COALESCE(zone_5_s,0)) / 60.0) AS hard_minutes,
         MAX(strain)                                           AS max_strain,
         string_agg(DISTINCT source, ',' ORDER BY source)      AS sources
    FROM vw_activity
   GROUP BY day
) t
WHERE m.day = t.day
"""

# Days with no session at all must be zeroed, not left holding a stale count.
ZERO_ACTIVITY = """
UPDATE mart_daily m SET
  workout_count = 0, workout_total_min = 0, hard_minutes = 0,
  resistance_count = 0, activity_sources = NULL,
  strength_total_volume_kg = 0, workout_max_strain = NULL
WHERE NOT EXISTS (SELECT 1 FROM vw_activity a WHERE a.day = m.day)
"""

# --- sleep ------------------------------------------------------------------
OVERLAY_SLEEP = """
UPDATE mart_daily m SET
  sleep_total_hours     = s.asleep_hours,
  sleep_rem_hours       = s.rem_hours,
  sleep_slow_wave_hours = s.deep_hours,
  sleep_efficiency_pct  = s.efficiency_pct,
  sleep_performance_pct = s.performance_pct,
  sleep_consistency_pct = s.consistency_pct,
  sleep_start_ts        = s.start_ts,
  sleep_end_ts          = s.end_ts,
  nap_count             = COALESCE(s.nap_count, 0),
  nap_total_min         = COALESCE(ROUND(s.nap_total_min, 1), 0),
  sleep_debt_minutes    = s.sleep_debt_min,
  sleep_source          = s.source,
  refreshed_at          = now()
FROM vw_sleep_daily s
WHERE m.day = s.day
"""

# --- nutrition --------------------------------------------------------------
OVERLAY_NUTRITION = """
UPDATE mart_daily m SET
  total_kcal          = n.energy_kcal,
  protein_g           = n.protein_g,
  carbs_g             = n.carbs_g,
  fat_g               = n.fat_g,
  fiber_g             = n.fiber_g,
  alcohol_g           = n.alcohol_g,
  caffeine_mg         = n.caffeine_mg,
  meal_count          = NULLIF(n.entry_count, 0),
  first_meal_time     = (n.first_eaten_at AT TIME ZONE 'America/New_York')::time,
  last_meal_time      = (n.last_eaten_at  AT TIME ZONE 'America/New_York')::time,
  eating_window_hours = n.eating_window_hours,
  nutrition_source    = n.source,
  refreshed_at        = now()
FROM vw_nutrition_daily n
WHERE m.day = n.day
"""

# --- body composition -------------------------------------------------------
OVERLAY_BODY = """
UPDATE mart_daily m SET
  weight_kg       = b.weight_kg,
  weight_method   = b.weight_method,
  weight_source   = b.weight_source,
  body_fat_pct    = b.body_fat_pct,
  body_fat_method = b.bf_method,
  lean_mass_kg    = ROUND(b.lean_mass_kg, 2),
  fat_mass_kg     = ROUND(b.fat_mass_kg, 2),
  refreshed_at    = now()
FROM vw_body_daily b
WHERE m.day = b.day
"""

# --- daily metrics ----------------------------------------------------------
OVERLAY_METRICS = """
UPDATE mart_daily m SET
  steps             = COALESCE(v.steps::int, m.steps),
  calories_burned   = COALESCE(v.calories_total, m.calories_burned),
  resting_heart_rate= COALESCE(v.resting_hr::int, m.resting_heart_rate),
  hrv_rmssd_ms      = COALESCE(v.hrv, m.hrv_rmssd_ms),
  recovery_score    = COALESCE(v.recovery_score::int, m.recovery_score),
  spo2_percentage   = COALESCE(v.spo2_avg, m.spo2_percentage),
  skin_temp_celsius = COALESCE(v.skin_temp, m.skin_temp_celsius),
  respiratory_rate  = COALESCE(v.respiratory_rate, m.respiratory_rate),
  vo2_max           = COALESCE(v.vo2_max, m.vo2_max),
  strain            = COALESCE(v.day_strain, m.strain),
  active_minutes    = v.active_minutes,
  intensity_minutes = COALESCE(v.intensity_minutes_moderate, 0)
                    + COALESCE(v.intensity_minutes_vigorous, 0),
  refreshed_at      = now()
FROM (
  SELECT day,
    MAX(value) FILTER (WHERE metric = 'steps')                      AS steps,
    MAX(value) FILTER (WHERE metric = 'calories_total')             AS calories_total,
    MAX(value) FILTER (WHERE metric = 'resting_hr')                 AS resting_hr,
    MAX(value) FILTER (WHERE metric = 'hrv')                        AS hrv,
    MAX(value) FILTER (WHERE metric = 'recovery_score')             AS recovery_score,
    MAX(value) FILTER (WHERE metric = 'spo2_avg')                   AS spo2_avg,
    MAX(value) FILTER (WHERE metric = 'skin_temp')                  AS skin_temp,
    MAX(value) FILTER (WHERE metric = 'respiratory_rate')           AS respiratory_rate,
    MAX(value) FILTER (WHERE metric = 'vo2_max')                    AS vo2_max,
    MAX(value) FILTER (WHERE metric = 'day_strain')                 AS day_strain,
    MAX(value) FILTER (WHERE metric = 'active_minutes')             AS active_minutes,
    MAX(value) FILTER (WHERE metric = 'intensity_minutes_moderate') AS intensity_minutes_moderate,
    MAX(value) FILTER (WHERE metric = 'intensity_minutes_vigorous') AS intensity_minutes_vigorous
  FROM vw_daily_metric
  GROUP BY day
) v
WHERE m.day = v.day
"""

# --- adherence (GAP-10) -----------------------------------------------------
OVERLAY_ADHERENCE = """
UPDATE mart_daily m SET
  sessions_per_week_4w  = a.sessions_per_week_4w,
  sessions_per_week_12w = a.sessions_per_week_12w,
  sessions_per_week_26w = a.sessions_per_week_26w,
  below_floor           = a.below_floor
FROM vw_adherence a
WHERE m.day = a.day
"""

# --- fasting ----------------------------------------------------------------
OVERLAY_FASTS = """
UPDATE mart_daily m SET fast_hours = f.hours
FROM (
  SELECT day, ROUND(MAX(duration_s) / 3600.0, 2) AS hours
    FROM fact_fast WHERE duration_s IS NOT NULL GROUP BY day
) f
WHERE m.day = f.day
"""

# --- data quality -----------------------------------------------------------
OVERLAY_QUALITY = """
UPDATE mart_daily m SET quality_flag_count = COALESCE(q.n, 0)
FROM (
  SELECT day, count(*) AS n FROM data_quality_flag
   WHERE NOT resolved AND day IS NOT NULL GROUP BY day
) q
WHERE m.day = q.day
"""

STEPS = (
    ("extend_spine",  EXTEND_SPINE),
    ("zero_activity", ZERO_ACTIVITY),
    ("activity",      OVERLAY_ACTIVITY),
    ("sleep",         OVERLAY_SLEEP),
    ("nutrition",     OVERLAY_NUTRITION),
    ("body",          OVERLAY_BODY),
    ("metrics",       OVERLAY_METRICS),
    ("adherence",     OVERLAY_ADHERENCE),
    ("fasts",         OVERLAY_FASTS),
    ("quality",       OVERLAY_QUALITY),
)


def refresh_unified_overlay() -> dict[str, int]:
    out: dict[str, int] = {}
    with tx() as c, c.cursor() as cur:
        for name, sql in STEPS:
            cur.execute(sql)
            out[name] = cur.rowcount
    log.info("mart.unified_overlay", **out)
    return out

"""The big rebuild SQL strings, kept as constants so refresh.py reads cleanly.

Each mart is rebuilt as TRUNCATE then INSERT … SELECT — we do NOT use upsert.
The entire table is recomputed from fact every refresh. Cheap because we only
have thousands of rows.

The TRUNCATE and INSERT are kept as separate constants because psycopg's
extended-query protocol (which kicks in whenever you pass parameters) cannot
send multiple statements in one Execute message. refresh.py runs them
back-to-back inside a single transaction.
"""

from __future__ import annotations


# ---- mart_daily ------------------------------------------------------------
TRUNCATE_MART_DAILY = "TRUNCATE mart_daily"
# Sources used:
#   fact_recovery       (daily Whoop recovery)
#   fact_sleep          (primary night sleep + naps split)
#   fact_cycle          (Whoop daily strain/kJ)
#   fact_workout        (count, total min, total kJ, max strain)
#   fact_calendar_event (meeting hours, focus blocks)
#   fact_food_daily     (Cronometer daily totals)
#   fact_food_log       (per-meal time-of-day + per-meal-window kcal)
#   fact_transaction    (spending) joined to dim_category for category labels
#   fact_biometric      (latest weight & body_fat per day)
#
# Days come from a generate_series spanning the earliest-known data date
# to today, so empty-data days still get a row with NULLs.
INSERT_MART_DAILY = """
INSERT INTO mart_daily (
  day,
  recovery_score, hrv_rmssd_ms, resting_heart_rate, spo2_percentage, skin_temp_celsius,
  sleep_total_hours, sleep_rem_hours, sleep_slow_wave_hours,
  sleep_efficiency_pct, sleep_performance_pct, sleep_consistency_pct,
  sleep_start_ts, sleep_end_ts,
  nap_count, nap_total_min,
  strain, day_kilojoules,
  workout_count, workout_total_min, workout_total_kj, workout_max_strain,
  meeting_count, meeting_hours, meeting_internal_hours, meeting_external_hours,
  first_meeting_time, last_meeting_time,
  longest_focus_block_min, total_focus_block_min,
  total_kcal, protein_g, carbs_g, fat_g, fiber_g, alcohol_g, caffeine_mg,
  meal_count, first_meal_time, last_meal_time, eating_window_hours,
  breakfast_kcal, lunch_kcal, dinner_kcal, snack_kcal,
  total_spend, food_spend, restaurant_spend, groceries_spend, transportation_spend,
  weight_kg, body_fat_pct,
  refreshed_at
)
WITH bounds AS (
  -- Earliest signal across any source. COALESCE handles tables that may be
  -- empty in early phases (e.g. food/spending before Phase 6/7 lands).
  SELECT
    LEAST(
      COALESCE((SELECT MIN(day)  FROM fact_recovery),       CURRENT_DATE),
      COALESCE((SELECT MIN(day)  FROM fact_sleep),          CURRENT_DATE),
      COALESCE((SELECT MIN(day)  FROM fact_cycle),          CURRENT_DATE),
      COALESCE((SELECT MIN(day)  FROM fact_workout),        CURRENT_DATE),
      COALESCE((SELECT MIN(day)  FROM fact_calendar_event), CURRENT_DATE),
      COALESCE((SELECT MIN(day)  FROM fact_food_daily),     CURRENT_DATE),
      COALESCE((SELECT MIN(day)  FROM fact_food_log),       CURRENT_DATE),
      COALESCE((SELECT MIN(date) FROM fact_transaction),    CURRENT_DATE),
      COALESCE((SELECT MIN(day)  FROM fact_biometric),      CURRENT_DATE)
    ) AS min_day
),
days AS (
  SELECT generate_series(b.min_day, CURRENT_DATE, '1 day'::interval)::date AS day
  FROM bounds b
),
sleep_primary AS (
  -- Longest non-nap sleep ending on each day = "the night's sleep".
  SELECT DISTINCT ON (day)
    day, sleep_id,
    total_in_bed_min, total_rem_min, total_slow_wave_min,
    sleep_efficiency_pct, sleep_performance_pct, sleep_consistency_pct,
    start_ts, end_ts
  FROM fact_sleep
  WHERE NOT is_nap
  ORDER BY day, total_in_bed_min DESC
),
cycle_primary AS (
  -- Whoop occasionally produces multiple cycles per local day around tz
  -- transitions / naps. Pick the longest one as the "main" daily cycle.
  SELECT DISTINCT ON (day)
    day, scaled_strain, day_kilojoules
  FROM fact_cycle
  ORDER BY day, COALESCE(end_ts - start_ts, INTERVAL '0') DESC
),
recovery_primary AS (
  -- Recovery is keyed to cycle_id, not day. If there are multiple recoveries
  -- per day (split cycles), pick the highest-scored one.
  SELECT DISTINCT ON (day)
    day, recovery_score, hrv_rmssd_ms, resting_heart_rate,
    spo2_percentage, skin_temp_celsius
  FROM fact_recovery
  ORDER BY day, recovery_score DESC NULLS LAST
),
naps AS (
  SELECT day, COUNT(*) AS nap_count, SUM(total_in_bed_min) AS nap_total_min
  FROM fact_sleep WHERE is_nap GROUP BY day
),
calendar_agg AS (
  SELECT
    day,
    COUNT(*) FILTER (WHERE classification = 'meeting')                      AS meeting_count,
    COALESCE(SUM(duration_min) FILTER (WHERE classification = 'meeting'), 0)/60.0
      AS meeting_hours,
    COALESCE(SUM(duration_min) FILTER (
      WHERE classification = 'meeting' AND attendee_external_count = 0), 0)/60.0
      AS meeting_internal_hours,
    COALESCE(SUM(duration_min) FILTER (
      WHERE classification = 'meeting' AND attendee_external_count > 0), 0)/60.0
      AS meeting_external_hours,
    MIN((start_ts AT TIME ZONE %(tz)s)::time) FILTER (WHERE classification = 'meeting')
      AS first_meeting_time,
    MAX((end_ts   AT TIME ZONE %(tz)s)::time) FILTER (WHERE classification = 'meeting')
      AS last_meeting_time,
    MAX(duration_min) FILTER (WHERE classification = 'focus')               AS longest_focus_block_min,
    SUM(duration_min) FILTER (WHERE classification = 'focus')               AS total_focus_block_min
  FROM fact_calendar_event
  WHERE response_status IS DISTINCT FROM 'declined'
  GROUP BY day
),
food_agg AS (
  SELECT
    day,
    COUNT(*) AS meal_count,
    MIN((eaten_at AT TIME ZONE %(tz)s)::time) AS first_meal_time,
    MAX((eaten_at AT TIME ZONE %(tz)s)::time) AS last_meal_time,
    EXTRACT(EPOCH FROM (MAX(eaten_at) - MIN(eaten_at)))/3600.0 AS eating_window_hours,
    SUM(energy_kcal) FILTER (WHERE meal_group ILIKE 'Breakfast') AS breakfast_kcal,
    SUM(energy_kcal) FILTER (WHERE meal_group ILIKE 'Lunch')     AS lunch_kcal,
    SUM(energy_kcal) FILTER (WHERE meal_group ILIKE 'Dinner')    AS dinner_kcal,
    SUM(energy_kcal) FILTER (WHERE meal_group ILIKE 'Snack%%')   AS snack_kcal
  FROM fact_food_log GROUP BY day
),
spend_agg AS (
  SELECT
    t.date AS day,
    SUM(amount) FILTER (WHERE NOT is_excluded AND amount > 0)                       AS total_spend,
    SUM(amount) FILTER (WHERE NOT is_excluded AND c.name ILIKE 'Food%%')            AS food_spend,
    SUM(amount) FILTER (WHERE NOT is_excluded AND c.name ILIKE 'Restaurants%%')     AS restaurant_spend,
    SUM(amount) FILTER (WHERE NOT is_excluded AND c.name ILIKE 'Groceries%%')       AS groceries_spend,
    SUM(amount) FILTER (WHERE NOT is_excluded AND c.name ILIKE 'Trans%%')           AS transportation_spend
  FROM fact_transaction t
  LEFT JOIN dim_category c ON c.category_id = t.category_id
  GROUP BY t.date
),
workout_agg AS (
  SELECT
    day,
    COUNT(*) AS workout_count,
    SUM(EXTRACT(EPOCH FROM (end_ts - start_ts))/60.0) AS workout_total_min,
    SUM(kilojoules) AS workout_total_kj,
    MAX(strain) AS workout_max_strain
  FROM fact_workout GROUP BY day
)
SELECT
  d.day,
  r.recovery_score, r.hrv_rmssd_ms, r.resting_heart_rate, r.spo2_percentage, r.skin_temp_celsius,
  ROUND(sp.total_in_bed_min/60.0, 2),
  ROUND(sp.total_rem_min/60.0, 2),
  ROUND(sp.total_slow_wave_min/60.0, 2),
  sp.sleep_efficiency_pct, sp.sleep_performance_pct, sp.sleep_consistency_pct,
  sp.start_ts, sp.end_ts,
  COALESCE(n.nap_count, 0), COALESCE(n.nap_total_min, 0),
  c.scaled_strain, c.day_kilojoules,
  COALESCE(w.workout_count, 0), COALESCE(w.workout_total_min, 0),
  COALESCE(w.workout_total_kj, 0), w.workout_max_strain,
  COALESCE(ca.meeting_count, 0),
  COALESCE(ca.meeting_hours, 0),
  COALESCE(ca.meeting_internal_hours, 0),
  COALESCE(ca.meeting_external_hours, 0),
  ca.first_meeting_time, ca.last_meeting_time,
  ca.longest_focus_block_min, ca.total_focus_block_min,
  fd.energy_kcal, fd.protein_g, fd.carbs_g, fd.fat_g, fd.fiber_g, fd.alcohol_g, fd.caffeine_mg,
  fa.meal_count, fa.first_meal_time, fa.last_meal_time, fa.eating_window_hours,
  fa.breakfast_kcal, fa.lunch_kcal, fa.dinner_kcal, fa.snack_kcal,
  sa.total_spend, sa.food_spend, sa.restaurant_spend, sa.groceries_spend, sa.transportation_spend,
  bw.value, bf.value,
  now()
FROM days d
LEFT JOIN recovery_primary r  ON r.day  = d.day
LEFT JOIN sleep_primary    sp ON sp.day = d.day
LEFT JOIN naps             n  ON n.day  = d.day
LEFT JOIN cycle_primary    c  ON c.day  = d.day
LEFT JOIN workout_agg     w  ON w.day  = d.day
LEFT JOIN calendar_agg    ca ON ca.day = d.day
LEFT JOIN fact_food_daily fd ON fd.day = d.day
LEFT JOIN food_agg        fa ON fa.day = d.day
LEFT JOIN spend_agg       sa ON sa.day = d.day
LEFT JOIN LATERAL (
  SELECT value FROM fact_biometric
  WHERE day = d.day AND metric = 'weight'
  ORDER BY measured_at DESC LIMIT 1
) bw ON TRUE
LEFT JOIN LATERAL (
  SELECT value FROM fact_biometric
  WHERE day = d.day AND metric = 'body_fat'
  ORDER BY measured_at DESC LIMIT 1
) bf ON TRUE;
"""


# ---- mart_meal -------------------------------------------------------------
# Group fact_food_log by (day, normalized meal_window). Cronometer's groups
# include Breakfast/Lunch/Dinner/Snack 1/2/3/Uncategorized — we collapse all
# Snack* to "snack" and Uncategorized to "snack" as a fallback (better than
# losing the rows entirely; a smarter time-clustering pass can replace this
# later if it matters).
TRUNCATE_MART_MEAL = "TRUNCATE mart_meal"
INSERT_MART_MEAL = """
INSERT INTO mart_meal (
  day, meal_window, start_ts, end_ts, duration_min, item_count,
  total_kcal, protein_g, carbs_g, fat_g, fiber_g, food_names, refreshed_at
)
SELECT
  day,
  CASE
    WHEN meal_group ILIKE 'Breakfast' THEN 'breakfast'
    WHEN meal_group ILIKE 'Lunch'     THEN 'lunch'
    WHEN meal_group ILIKE 'Dinner'    THEN 'dinner'
    WHEN meal_group ILIKE 'Snack%%'   THEN 'snack'
    ELSE 'snack'
  END AS meal_window,
  MIN(eaten_at) AS start_ts,
  MAX(eaten_at) AS end_ts,
  EXTRACT(EPOCH FROM (MAX(eaten_at) - MIN(eaten_at)))/60.0 AS duration_min,
  COUNT(*) AS item_count,
  SUM(energy_kcal) AS total_kcal,
  SUM(protein_g)   AS protein_g,
  SUM(carbs_g)     AS carbs_g,
  SUM(fat_g)       AS fat_g,
  SUM(fiber_g)     AS fiber_g,
  ARRAY_AGG(food_name ORDER BY eaten_at) AS food_names,
  now()
FROM fact_food_log
GROUP BY day,
  CASE
    WHEN meal_group ILIKE 'Breakfast' THEN 'breakfast'
    WHEN meal_group ILIKE 'Lunch'     THEN 'lunch'
    WHEN meal_group ILIKE 'Dinner'    THEN 'dinner'
    WHEN meal_group ILIKE 'Snack%%'   THEN 'snack'
    ELSE 'snack'
  END;
"""


# ---- mart_weekly -----------------------------------------------------------
# date_trunc('week', ...) gives Monday in Postgres (ISO 8601).
TRUNCATE_MART_WEEKLY = "TRUNCATE mart_weekly"
INSERT_MART_WEEKLY = """
INSERT INTO mart_weekly (
  week_start, avg_recovery_score, avg_hrv_rmssd_ms, avg_rhr,
  total_strain, total_workout_min, total_meeting_hours,
  avg_meeting_hours_per_workday, total_kcal, avg_kcal_per_day,
  avg_protein_g, total_spend, refreshed_at
)
SELECT
  date_trunc('week', day)::date AS week_start,
  AVG(recovery_score)::numeric(5,2),
  AVG(hrv_rmssd_ms)::numeric(6,2),
  AVG(resting_heart_rate)::int,
  SUM(strain)::numeric(7,2),
  SUM(workout_total_min)::numeric(7,1),
  SUM(meeting_hours)::numeric(6,2),
  AVG(meeting_hours) FILTER (WHERE EXTRACT(ISODOW FROM day) BETWEEN 1 AND 5)::numeric(5,2),
  SUM(total_kcal)::numeric(12,2),
  AVG(total_kcal)::numeric(10,2),
  AVG(protein_g)::numeric(8,2),
  SUM(total_spend)::numeric(12,2),
  now()
FROM mart_daily
GROUP BY date_trunc('week', day);
"""

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
  alcohol_spend, bars_spend, entertainment_spend, shopping_spend, travel_spend,
  dining_out_txn_count, dining_out_txn_max,
  weight_kg, body_fat_pct,
  -- Whoop journal habit pivots
  had_alcohol, alcohol_drinks,
  had_caffeine, caffeine_servings, caffeine_last_serving_time,
  late_meal, read_in_bed,
  device_in_bed, device_in_bed_minutes,
  morning_sunlight, sexual_activity, stretching, rest_day,
  took_magnesium, took_vitamin_d, took_creatine, took_l_theanine,
  joint_pain, headache,
  journal_notes,
  -- Hevy strength rollup
  strength_total_volume_kg, strength_total_sets, strength_unique_exercises,
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
      COALESCE((SELECT MIN(day)  FROM fact_biometric),      CURRENT_DATE),
      COALESCE((SELECT MIN(day)  FROM fact_strength_workout), CURRENT_DATE)
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
  -- Whoop cycles run bedtime → next bedtime. Neither start_ts nor end_ts
  -- gives the right calendar day for the cycle's activity:
  --   * local_date(start_ts) = bedtime-of-day-N — wrong, activity is mostly N+1
  --   * local_date(end_ts)   = bedtime-of-day-N+1 — wrong when bedtime is just
  --     past midnight (e.g. cycle ends 01:34, but activity was the prior day)
  -- The correct attribution is the cycle's midpoint, which lands during the
  -- waking-activity portion regardless of bedtime drift. For the active cycle
  -- (end_ts IS NULL), use now() as the open-bound proxy.
  --
  -- Whoop occasionally produces multiple cycles per local day around tz
  -- transitions / naps; we keep the longest as the "main" daily cycle.
  SELECT DISTINCT ON (effective_day)
    effective_day AS day, scaled_strain, day_kilojoules
  FROM (
    SELECT
      c.cycle_id,
      local_date(c.start_ts + (COALESCE(c.end_ts, now()) - c.start_ts) / 2)
        AS effective_day,
      c.scaled_strain,
      c.day_kilojoules,
      c.start_ts,
      c.end_ts
    FROM fact_cycle c
  ) rebucketed
  ORDER BY effective_day, COALESCE(end_ts - start_ts, INTERVAL '0') DESC
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
    SUM(amount) FILTER (WHERE NOT is_excluded AND c.name ILIKE 'Trans%%')           AS transportation_spend,
    SUM(amount) FILTER (WHERE NOT is_excluded AND c.name ILIKE 'Alcohol%%')         AS alcohol_spend,
    SUM(amount) FILTER (
      WHERE NOT is_excluded AND amount > 0 AND (
        c.name ILIKE 'Bars%%' OR c.name ILIKE 'Nightlife%%'
        OR c.name ILIKE '%%Bars & Nightlife%%' OR c.name ILIKE '%%Bar%%'
      )
    )                                                                                AS bars_spend,
    SUM(amount) FILTER (
      WHERE NOT is_excluded AND amount > 0 AND (
        c.name ILIKE 'Entertainment%%' OR c.name ILIKE 'Music%%' OR c.name ILIKE 'Concerts%%'
      )
    )                                                                                AS entertainment_spend,
    SUM(amount) FILTER (
      WHERE NOT is_excluded AND amount > 0 AND (
        c.name ILIKE 'Shopping%%' OR c.name ILIKE 'Clothing%%' OR c.name ILIKE 'Personal%%'
      )
    )                                                                                AS shopping_spend,
    SUM(amount) FILTER (
      WHERE NOT is_excluded AND amount > 0 AND (
        c.name ILIKE 'Travel%%' OR c.name ILIKE 'Vacation%%' OR c.name ILIKE 'Hotel%%'
      )
    )                                                                                AS travel_spend,
    COUNT(*) FILTER (
      WHERE NOT is_excluded AND amount >= 50 AND (
        c.name ILIKE 'Restaurants%%' OR c.name ILIKE 'Bars%%'
        OR c.name ILIKE 'Nightlife%%' OR c.name ILIKE '%%Bar%%'
      )
    )                                                                                AS dining_out_txn_count,
    MAX(amount) FILTER (
      WHERE NOT is_excluded AND amount > 0 AND (
        c.name ILIKE 'Restaurants%%' OR c.name ILIKE 'Bars%%'
        OR c.name ILIKE 'Nightlife%%' OR c.name ILIKE '%%Bar%%'
      )
    )                                                                                AS dining_out_txn_max
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
),
strength_agg AS (
  -- Per-day rollup of Whoop Strength Trainer sessions (replaced Hevy after the
  -- Hevy/PushPress/coach deprecation). Volume/sets are summed at the workout
  -- grain; unique_exercises is the DISTINCT count across the day's workouts'
  -- exercises[] JSONB (so two sessions that both did Bench Press count it once).
  -- The two sub-aggregations are kept separate so the per-exercise fan-out in
  -- the distinct count doesn't inflate the volume/sets sums.
  SELECT
    w.day,
    w.strength_total_volume_kg,
    w.strength_total_sets,
    COALESCE(e.strength_unique_exercises, 0) AS strength_unique_exercises
  FROM (
    SELECT day,
           SUM(total_volume_kg)::numeric(12,2) AS strength_total_volume_kg,
           SUM(set_count)::int                 AS strength_total_sets
    FROM fact_whoop_lift_workout
    GROUP BY day
  ) w
  LEFT JOIN (
    SELECT flw.day,
           COUNT(DISTINCT ex.elem->>'exercise_id')::int AS strength_unique_exercises
    FROM fact_whoop_lift_workout flw
    CROSS JOIN LATERAL jsonb_array_elements(
                 COALESCE(flw.exercises, '[]'::jsonb)) AS ex(elem)
    GROUP BY flw.day
  ) e ON e.day = w.day
),
journal_notes_agg AS (
  -- Whoop's daily notes lives in raw_whoop_journal.payload.journal.notes;
  -- pull straight from JSONB so we don't have to materialize a column.
  SELECT day, payload->'journal'->>'notes' AS journal_notes
  FROM raw_whoop_journal
),
habit_pivot AS (
  -- Pivot the highest-frequency habits to typed columns. Anything not
  -- pivoted stays in fact_habit_log for ad-hoc queries.
  --
  -- The internal_name strings come from Whoop's behavior catalog. Keep this
  -- list in sync with the column list above and with the README/schema_docs.
  SELECT
    day,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'alcohol')         AS had_alcohol,
    MAX(magnitude_value)  FILTER (WHERE habit_key = 'alcohol')         AS alcohol_drinks,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'caffeine')        AS had_caffeine,
    MAX(magnitude_value)  FILTER (WHERE habit_key = 'caffeine')        AS caffeine_servings,
    MAX((time_input_value AT TIME ZONE %(tz)s)::time)
      FILTER (WHERE habit_key = 'caffeine')                            AS caffeine_last_serving_time,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'late-meal')       AS late_meal,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'read-in-bed')     AS read_in_bed,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'device-in-bed')   AS device_in_bed,
    MAX(magnitude_value)  FILTER (WHERE habit_key = 'device-in-bed')   AS device_in_bed_minutes,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'morning-sunlight') AS morning_sunlight,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'sexual-activity') AS sexual_activity,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'stretching')      AS stretching,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'rest-day')        AS rest_day,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'magnesium')       AS took_magnesium,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'vitamin-d')       AS took_vitamin_d,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'creatine')        AS took_creatine,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'l-theanine')      AS took_l_theanine,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'joint-pain')      AS joint_pain,
    BOOL_OR(answered_yes) FILTER (WHERE habit_key = 'headache')        AS headache
  FROM fact_habit_log
  GROUP BY day
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
  -- Spending columns: COALESCE to 0 for any day where fact_transaction
  -- has any rows but the category-FILTER produced NULL (no charges in that
  -- subset). For days with NO fact_transaction rows at all, sa.* is NULL via
  -- the LEFT JOIN — keep total_spend NULL (no spend data) but default the
  -- derived counts/sums sensibly so analytics tools can sum them.
  COALESCE(sa.total_spend,        0), COALESCE(sa.food_spend,           0),
  COALESCE(sa.restaurant_spend,   0), COALESCE(sa.groceries_spend,      0),
  COALESCE(sa.transportation_spend, 0),
  COALESCE(sa.alcohol_spend,      0), COALESCE(sa.bars_spend,           0),
  COALESCE(sa.entertainment_spend,0), COALESCE(sa.shopping_spend,       0),
  COALESCE(sa.travel_spend,       0),
  COALESCE(sa.dining_out_txn_count, 0), sa.dining_out_txn_max,
  bw.value, bf.value,
  hp.had_alcohol, hp.alcohol_drinks,
  hp.had_caffeine, hp.caffeine_servings, hp.caffeine_last_serving_time,
  hp.late_meal, hp.read_in_bed,
  hp.device_in_bed, hp.device_in_bed_minutes,
  hp.morning_sunlight, hp.sexual_activity, hp.stretching, hp.rest_day,
  hp.took_magnesium, hp.took_vitamin_d, hp.took_creatine, hp.took_l_theanine,
  hp.joint_pain, hp.headache,
  jn.journal_notes,
  COALESCE(st.strength_total_volume_kg, 0),
  COALESCE(st.strength_total_sets,      0),
  COALESCE(st.strength_unique_exercises, 0),
  now()
FROM days d
LEFT JOIN recovery_primary r  ON r.day  = d.day
LEFT JOIN sleep_primary    sp ON sp.day = d.day
LEFT JOIN naps             n  ON n.day  = d.day
LEFT JOIN cycle_primary    c  ON c.day  = d.day
LEFT JOIN workout_agg     w  ON w.day  = d.day
LEFT JOIN calendar_agg    ca ON ca.day = d.day
LEFT JOIN habit_pivot     hp ON hp.day = d.day
LEFT JOIN journal_notes_agg jn ON jn.day = d.day
LEFT JOIN fact_food_daily fd ON fd.day = d.day
LEFT JOIN food_agg        fa ON fa.day = d.day
LEFT JOIN spend_agg       sa ON sa.day = d.day
LEFT JOIN strength_agg    st ON st.day = d.day
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


# ---- mart_body_image_daily -------------------------------------------------
# One row per day per user with per-feature averages across every LLM
# rater / specialist / run, plus the deterministic geometry metrics.
# The body_image_rating.dimensions JSONB stores feature names verbatim
# from each rater (e.g. "skin_quality" lives inside surface specialists),
# so we look them up across all rows for the day.

TRUNCATE_MART_BODY_IMAGE_DAILY = "TRUNCATE mart_body_image_daily"

INSERT_MART_BODY_IMAGE_DAILY = """
INSERT INTO mart_body_image_daily (
  day, user_id,
  body_image_overall,
  body_image_skin_quality, body_image_skin_clarity, body_image_under_eye,
  body_image_jawline, body_image_chin,
  body_image_eye_quality, body_image_nose_harmony, body_image_lip_quality,
  body_image_hair_quality, body_image_hairline,
  body_image_beard_density, body_image_grooming,
  body_image_expression, body_image_photo_quality,
  body_image_symmetry, body_image_gonial_angle, body_image_jaw_ratio,
  body_image_photo_count, body_image_rating_count,
  refreshed_at
)
WITH per_day AS (
  SELECT
    date_trunc('day', p.created_at)::date AS day,
    p.user_id,
    r.source, r.dimensions, r.overall
  FROM body_image_photo p
  JOIN body_image_rating r ON r.photo_id = p.id
)
SELECT
  d.day, d.user_id,
  AVG(d.overall) FILTER (WHERE d.source NOT IN ('geometry') AND d.overall IS NOT NULL)::numeric AS body_image_overall,
  AVG((d.dimensions->>'skin_quality')::numeric)        FILTER (WHERE d.source NOT IN ('geometry')) AS body_image_skin_quality,
  AVG((d.dimensions->>'skin_clarity')::numeric)        FILTER (WHERE d.source NOT IN ('geometry')) AS body_image_skin_clarity,
  AVG((d.dimensions->>'under_eye_quality')::numeric)   FILTER (WHERE d.source NOT IN ('geometry')) AS body_image_under_eye,
  AVG((d.dimensions->>'jawline_definition')::numeric)  FILTER (WHERE d.source NOT IN ('geometry')) AS body_image_jawline,
  AVG((d.dimensions->>'chin_projection')::numeric)     FILTER (WHERE d.source NOT IN ('geometry')) AS body_image_chin,
  AVG((d.dimensions->>'eye_quality')::numeric)         FILTER (WHERE d.source NOT IN ('geometry')) AS body_image_eye_quality,
  AVG((d.dimensions->>'nose_harmony')::numeric)        FILTER (WHERE d.source NOT IN ('geometry')) AS body_image_nose_harmony,
  AVG((d.dimensions->>'lip_quality')::numeric)         FILTER (WHERE d.source NOT IN ('geometry')) AS body_image_lip_quality,
  AVG((d.dimensions->>'hair_quality')::numeric)        FILTER (WHERE d.source NOT IN ('geometry')) AS body_image_hair_quality,
  AVG((d.dimensions->>'hairline_quality')::numeric)    FILTER (WHERE d.source NOT IN ('geometry')) AS body_image_hairline,
  AVG((d.dimensions->>'beard_density')::numeric)       FILTER (WHERE d.source NOT IN ('geometry')) AS body_image_beard_density,
  AVG((d.dimensions->>'grooming_overall')::numeric)    FILTER (WHERE d.source NOT IN ('geometry')) AS body_image_grooming,
  AVG((d.dimensions->>'expression_appeal')::numeric)   FILTER (WHERE d.source NOT IN ('geometry')) AS body_image_expression,
  AVG((d.dimensions->>'photo_quality_isolated')::numeric) FILTER (WHERE d.source NOT IN ('geometry')) AS body_image_photo_quality,
  AVG((d.dimensions->>'symmetry_score')::numeric)        FILTER (WHERE d.source = 'geometry') AS body_image_symmetry,
  AVG((d.dimensions->>'gonial_angle_deg')::numeric)      FILTER (WHERE d.source = 'geometry') AS body_image_gonial_angle,
  AVG((d.dimensions->>'bigonial_bizygomatic_ratio')::numeric) FILTER (WHERE d.source = 'geometry') AS body_image_jaw_ratio,
  COUNT(DISTINCT (d.day, d.user_id)) FILTER (WHERE d.source NOT IN ('geometry'))::int AS body_image_photo_count,
  COUNT(*)::int AS body_image_rating_count,
  now()
FROM per_day d
GROUP BY d.day, d.user_id;
"""


# Mirror the subset onto mart_daily so the existing correlate_metrics
# MCP tool (which queries mart_daily) sees body-image columns without
# schema awareness of the new tables.
UPDATE_MART_DAILY_BODY_IMAGE = """
UPDATE mart_daily md
   SET body_image_overall       = m.body_image_overall,
       body_image_skin_quality  = m.body_image_skin_quality,
       body_image_skin_clarity  = m.body_image_skin_clarity,
       body_image_under_eye     = m.body_image_under_eye,
       body_image_jawline       = m.body_image_jawline,
       body_image_hair_quality  = m.body_image_hair_quality,
       body_image_symmetry      = m.body_image_symmetry,
       body_image_photo_quality = m.body_image_photo_quality
  FROM mart_body_image_daily m
 WHERE md.day = m.day;
"""


# Patch the Whoop private-API daily metrics onto mart_daily. Runs AFTER
# mart_daily is rebuilt (refresh_all sequences this). Long-format
# fact_whoop_metric_daily is pivoted to the handful of columns mart_daily
# exposes; weight / body-composition are deliberately NOT pulled here (those
# columns stay owned by fact_biometric). SLEEP_DEBT_POST is a duration the
# trend graph renders as "H:MM", which transforms parse to minutes.
UPDATE_MART_DAILY_WHOOP_PRIVATE = """
UPDATE mart_daily md
   SET steps              = s.steps,
       calories_burned    = s.calories_burned,
       vo2_max            = s.vo2_max,
       respiratory_rate   = s.respiratory_rate,
       sleep_debt_minutes = s.sleep_debt_minutes
  FROM (
    SELECT day,
      MAX(value) FILTER (WHERE metric = 'STEPS')            AS steps,
      MAX(value) FILTER (WHERE metric = 'CALORIES')         AS calories_burned,
      MAX(value) FILTER (WHERE metric = 'VO2_MAX')          AS vo2_max,
      MAX(value) FILTER (WHERE metric = 'RESPIRATORY_RATE') AS respiratory_rate,
      MAX(value) FILTER (WHERE metric = 'SLEEP_DEBT_POST')  AS sleep_debt_minutes
    FROM fact_whoop_metric_daily
    GROUP BY day
  ) s
 WHERE md.day = s.day;
"""


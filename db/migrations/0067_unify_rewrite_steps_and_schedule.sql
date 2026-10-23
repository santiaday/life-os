-- 0067_unify_rewrite_steps_and_schedule.sql
--
-- The two unify_run() steps that delete and rewrite derived rows, and the
-- pg_cron jobs that take over from the droplet scheduler.
--
--   unify_quality()   replaces every flagged_by='auto' row in data_quality_flag
--                     with vw_quality_target. Manual flags are never touched.
--   refresh_marts()   rewrites the last 60 days of mart_daily's training, sleep,
--                     nutrition, body and metric columns from the unified views,
--                     and rebuilds mart_meal and mart_weekly for that window.
--                     Days before the window keep what the last Python rebuild
--                     wrote, which is the only copy of the retired sources
--                     (Whoop recovery, calendar, spending, journal).
--
-- Meal-level columns (mart_meal, breakfast_kcal ... snack_kcal) now read
-- fact_nutrition_entry for the day's winning nutrition source instead of
-- fact_food_log, which only the droplet's cronometer-export binary wrote.
--
--   job unify            hourly at :15  unify_run()
--   job cronometer-sync  every 15 min   edge function cronometer-sync, last 3 days

CREATE OR REPLACE FUNCTION unify_quality()
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE cnt int; by_rule jsonb;
BEGIN
  DELETE FROM data_quality_flag WHERE flagged_by = 'auto';
  INSERT INTO data_quality_flag (table_name, row_key, day, metric, value, reason, rule,
                                 severity, flagged_by, resolved, created_at)
  SELECT DISTINCT ON (table_name, row_key, rule, metric)
         table_name, row_key, day, metric, value, reason, rule, severity, 'auto', false, now()
    FROM vw_quality_target
  ON CONFLICT (table_name, row_key, rule, metric) DO UPDATE SET
    day = EXCLUDED.day, value = EXCLUDED.value, reason = EXCLUDED.reason,
    severity = EXCLUDED.severity, resolved = EXCLUDED.resolved, created_at = EXCLUDED.created_at;
  GET DIAGNOSTICS cnt = ROW_COUNT;
  SELECT coalesce(jsonb_object_agg(rule, c), '{}') INTO by_rule
    FROM (SELECT rule, count(*) AS c FROM data_quality_flag WHERE flagged_by = 'auto' GROUP BY rule) x;
  RETURN jsonb_build_object('flags', cnt) || by_rule;
END
$$;

CREATE OR REPLACE FUNCTION refresh_marts(p_since date DEFAULT CURRENT_DATE - 60)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  result jsonb := '{}';
  cnt int;
  wk date := date_trunc('week', p_since)::date;
BEGIN
  INSERT INTO mart_daily (day, refreshed_at)
  SELECT d::date, now()
    FROM generate_series(p_since, CURRENT_DATE, interval '1 day') d
  ON CONFLICT (day) DO NOTHING;
  GET DIAGNOSTICS cnt = ROW_COUNT; result := result || jsonb_build_object('new_days', cnt);

  -- training ----------------------------------------------------------------
  UPDATE mart_daily m SET
    workout_count = 0, workout_total_min = 0, hard_minutes = 0,
    resistance_count = 0, activity_sources = NULL,
    strength_total_volume_kg = 0, strength_total_sets = 0, strength_unique_exercises = 0,
    workout_max_strain = NULL
  WHERE m.day >= p_since
    AND NOT EXISTS (SELECT 1 FROM vw_activity a WHERE a.day = m.day);

  UPDATE mart_daily m SET
    workout_count      = coalesce(t.session_count, 0),
    workout_total_min  = coalesce(round(t.total_min, 1), 0),
    workout_max_strain = t.max_strain,
    hard_minutes       = round(t.hard_minutes, 1),
    resistance_count   = coalesce(t.resistance_count, 0),
    activity_sources   = t.sources,
    strength_total_volume_kg  = coalesce(round(t.volume_kg, 1), 0),
    strength_total_sets       = coalesce(t.sets, 0),
    strength_unique_exercises = coalesce(x.unique_exercises, 0),
    refreshed_at       = now()
  FROM (
    SELECT day,
           count(*)                                                    AS session_count,
           count(*) FILTER (WHERE is_resistance)                       AS resistance_count,
           sum(duration_s) / 60.0                                      AS total_min,
           sum(coalesce(total_volume_kg, 0))                           AS volume_kg,
           sum(coalesce(total_sets, 0))                                AS sets,
           sum((coalesce(zone_4_s, 0) + coalesce(zone_5_s, 0)) / 60.0) AS hard_minutes,
           max(strain)                                                 AS max_strain,
           string_agg(DISTINCT source, ',' ORDER BY source)            AS sources
      FROM vw_activity WHERE day >= p_since
     GROUP BY day
  ) t
  LEFT JOIN (
    SELECT day, count(DISTINCT exercise_key) AS unique_exercises
      FROM vw_activity_set WHERE day >= p_since GROUP BY day
  ) x USING (day)
  WHERE m.day = t.day;
  GET DIAGNOSTICS cnt = ROW_COUNT; result := result || jsonb_build_object('activity', cnt);

  -- sleep -------------------------------------------------------------------
  UPDATE mart_daily m SET
    sleep_total_hours     = s.asleep_hours,
    sleep_rem_hours       = s.rem_hours,
    sleep_slow_wave_hours = s.deep_hours,
    sleep_efficiency_pct  = s.efficiency_pct,
    sleep_performance_pct = s.performance_pct,
    sleep_consistency_pct = s.consistency_pct,
    sleep_start_ts        = s.start_ts,
    sleep_end_ts          = s.end_ts,
    nap_count             = coalesce(s.nap_count, 0),
    nap_total_min         = coalesce(round(s.nap_total_min, 1), 0),
    sleep_debt_minutes    = s.sleep_debt_min,
    sleep_source          = s.source,
    refreshed_at          = now()
  FROM vw_sleep_daily s
  WHERE m.day = s.day AND s.day >= p_since;
  GET DIAGNOSTICS cnt = ROW_COUNT; result := result || jsonb_build_object('sleep', cnt);

  -- nutrition ---------------------------------------------------------------
  UPDATE mart_daily m SET
    total_kcal          = n.energy_kcal,
    protein_g           = n.protein_g,
    carbs_g             = n.carbs_g,
    fat_g               = n.fat_g,
    fiber_g             = n.fiber_g,
    alcohol_g           = n.alcohol_g,
    caffeine_mg         = n.caffeine_mg,
    meal_count          = nullif(n.entry_count, 0),
    first_meal_time     = (n.first_eaten_at AT TIME ZONE 'America/New_York')::time,
    last_meal_time      = (n.last_eaten_at  AT TIME ZONE 'America/New_York')::time,
    eating_window_hours = n.eating_window_hours,
    nutrition_source    = n.source,
    refreshed_at        = now()
  FROM vw_nutrition_daily n
  WHERE m.day = n.day AND n.day >= p_since;
  GET DIAGNOSTICS cnt = ROW_COUNT; result := result || jsonb_build_object('nutrition', cnt);

  -- Per-meal kcal from the item-level entries of the day's winning source.
  UPDATE mart_daily m SET
    breakfast_kcal = f.breakfast_kcal, lunch_kcal = f.lunch_kcal,
    dinner_kcal = f.dinner_kcal, snack_kcal = f.snack_kcal
  FROM (
    SELECT e.day,
           sum(e.energy_kcal) FILTER (WHERE e.meal = 'breakfast')          AS breakfast_kcal,
           sum(e.energy_kcal) FILTER (WHERE e.meal = 'lunch')              AS lunch_kcal,
           sum(e.energy_kcal) FILTER (WHERE e.meal = 'dinner')             AS dinner_kcal,
           sum(e.energy_kcal) FILTER (WHERE e.meal IN ('snack', 'snacks')) AS snack_kcal
      FROM fact_nutrition_entry e
      JOIN vw_nutrition_daily v ON v.day = e.day AND v.source = e.source
     WHERE e.day >= p_since
     GROUP BY e.day
  ) f
  WHERE m.day = f.day;

  -- body --------------------------------------------------------------------
  UPDATE mart_daily m SET
    weight_kg       = b.weight_kg,
    weight_method   = b.weight_method,
    weight_source   = b.weight_source,
    body_fat_pct    = b.body_fat_pct,
    body_fat_method = b.bf_method,
    lean_mass_kg    = round(b.lean_mass_kg, 2),
    fat_mass_kg     = round(b.fat_mass_kg, 2),
    refreshed_at    = now()
  FROM vw_body_daily b
  WHERE m.day = b.day AND b.day >= p_since;
  GET DIAGNOSTICS cnt = ROW_COUNT; result := result || jsonb_build_object('body', cnt);

  -- daily metrics -----------------------------------------------------------
  UPDATE mart_daily m SET
    steps              = coalesce(v.steps::int, m.steps),
    calories_burned    = coalesce(v.calories_total, m.calories_burned),
    resting_heart_rate = coalesce(v.resting_hr::int, m.resting_heart_rate),
    hrv_rmssd_ms       = coalesce(v.hrv, m.hrv_rmssd_ms),
    recovery_score     = coalesce(v.recovery_score::int, m.recovery_score),
    spo2_percentage    = coalesce(v.spo2_avg, m.spo2_percentage),
    skin_temp_celsius  = coalesce(v.skin_temp, m.skin_temp_celsius),
    respiratory_rate   = coalesce(v.respiratory_rate, m.respiratory_rate),
    vo2_max            = coalesce(v.vo2_max, m.vo2_max),
    strain             = coalesce(v.day_strain, m.strain),
    active_minutes     = v.active_minutes,
    intensity_minutes  = coalesce(v.intensity_minutes_moderate, 0)
                       + coalesce(v.intensity_minutes_vigorous, 0),
    refreshed_at       = now()
  FROM (
    SELECT day,
      max(value) FILTER (WHERE metric = 'steps')                      AS steps,
      max(value) FILTER (WHERE metric = 'calories_total')             AS calories_total,
      max(value) FILTER (WHERE metric = 'resting_hr')                 AS resting_hr,
      max(value) FILTER (WHERE metric = 'hrv')                        AS hrv,
      max(value) FILTER (WHERE metric = 'recovery_score')             AS recovery_score,
      max(value) FILTER (WHERE metric = 'spo2_avg')                   AS spo2_avg,
      max(value) FILTER (WHERE metric = 'skin_temp')                  AS skin_temp,
      max(value) FILTER (WHERE metric = 'respiratory_rate')           AS respiratory_rate,
      max(value) FILTER (WHERE metric = 'vo2_max')                    AS vo2_max,
      max(value) FILTER (WHERE metric = 'day_strain')                 AS day_strain,
      max(value) FILTER (WHERE metric = 'active_minutes')             AS active_minutes,
      max(value) FILTER (WHERE metric = 'intensity_minutes_moderate') AS intensity_minutes_moderate,
      max(value) FILTER (WHERE metric = 'intensity_minutes_vigorous') AS intensity_minutes_vigorous
    FROM vw_daily_metric WHERE day >= p_since
    GROUP BY day
  ) v
  WHERE m.day = v.day;
  GET DIAGNOSTICS cnt = ROW_COUNT; result := result || jsonb_build_object('metrics', cnt);

  -- adherence and quality ---------------------------------------------------
  UPDATE mart_daily m SET
    sessions_per_week_4w  = a.sessions_per_week_4w,
    sessions_per_week_12w = a.sessions_per_week_12w,
    sessions_per_week_26w = a.sessions_per_week_26w,
    below_floor           = a.below_floor
  FROM vw_adherence a
  WHERE m.day = a.day AND a.day >= p_since;

  UPDATE mart_daily m SET quality_flag_count = coalesce(q.flags, 0)
  FROM (SELECT d.day, count(f.id) AS flags
          FROM mart_daily d
          LEFT JOIN data_quality_flag f ON f.day = d.day AND NOT f.resolved
         WHERE d.day >= p_since GROUP BY d.day) q
  WHERE m.day = q.day;

  -- mart_meal ---------------------------------------------------------------
  DELETE FROM mart_meal WHERE day >= p_since;
  INSERT INTO mart_meal (day, meal_window, start_ts, end_ts, duration_min, item_count,
                         total_kcal, protein_g, carbs_g, fat_g, fiber_g, food_names, refreshed_at)
  SELECT e.day, e.w, min(e.eaten_at), max(e.eaten_at),
         extract(epoch FROM (max(e.eaten_at) - min(e.eaten_at))) / 60.0, count(*),
         sum(e.energy_kcal), sum(e.protein_g), sum(e.carbs_g), sum(e.fat_g), sum(e.fiber_g),
         array_agg(e.food_name ORDER BY e.eaten_at), now()
    FROM (SELECT e.*, CASE WHEN e.meal IN ('breakfast', 'lunch', 'dinner') THEN e.meal ELSE 'snack' END AS w
            FROM fact_nutrition_entry e
            JOIN vw_nutrition_daily v ON v.day = e.day AND v.source = e.source
           WHERE e.day >= p_since) e
   GROUP BY e.day, e.w;
  GET DIAGNOSTICS cnt = ROW_COUNT; result := result || jsonb_build_object('mart_meal', cnt);

  -- mart_weekly -------------------------------------------------------------
  DELETE FROM mart_weekly WHERE week_start >= wk;
  INSERT INTO mart_weekly (week_start, avg_recovery_score, avg_hrv_rmssd_ms, avg_rhr,
                           total_strain, total_workout_min, total_meeting_hours,
                           avg_meeting_hours_per_workday, total_kcal, avg_kcal_per_day,
                           avg_protein_g, total_spend, refreshed_at)
  SELECT date_trunc('week', day)::date,
         avg(recovery_score)::numeric(5,2), avg(hrv_rmssd_ms)::numeric(6,2),
         avg(resting_heart_rate)::int, sum(strain)::numeric(7,2),
         sum(workout_total_min)::numeric(7,1), sum(meeting_hours)::numeric(6,2),
         (avg(meeting_hours) FILTER (WHERE extract(isodow FROM day) BETWEEN 1 AND 5))::numeric(5,2),
         sum(total_kcal)::numeric(12,2), avg(total_kcal)::numeric(10,2),
         avg(protein_g)::numeric(8,2), sum(total_spend)::numeric(12,2), now()
    FROM mart_daily WHERE day >= wk
   GROUP BY date_trunc('week', day);
  GET DIAGNOSTICS cnt = ROW_COUNT; result := result || jsonb_build_object('mart_weekly', cnt);

  RETURN result;
END
$$;

SELECT cron.schedule('unify', '15 * * * *', $$SELECT public.unify_run()$$);
SELECT cron.schedule('cronometer-sync', '*/15 * * * *', $$SELECT public.call_edge_function('cronometer-sync', '?days=3')$$);

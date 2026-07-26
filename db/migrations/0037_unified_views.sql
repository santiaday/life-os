-- 0037_unified_views.sql
--
-- The analysis surface over the unified layer from 0036.
--
-- Every view here returns ONE row per real-world event or per day, with
-- cross-source conflicts resolved by an explicit, editable precedence table
-- (dim_source_priority) rather than by a CASE expression buried in SQL.

BEGIN;

-- =========================================================================
-- Source precedence. Higher priority wins when two sources describe the same
-- day or the same event. Edit this table to change resolution behaviour --
-- no view rewrite needed.
-- =========================================================================
CREATE TABLE IF NOT EXISTS dim_source_priority (
  domain     TEXT NOT NULL,   -- activity|sleep|nutrition|body|metric
  source     TEXT NOT NULL,
  priority   INTEGER NOT NULL,   -- higher wins
  notes      TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (domain, source)
);

INSERT INTO dim_source_priority (domain, source, priority, notes) VALUES
  -- ACTIVITY: per-set truth beats summary. Whoop lift and Hevy record sets;
  -- Garmin records per-exercise aggregates; Whoop workout is a summary only.
  ('activity','hevy',            90, 'per-set, user-entered'),
  ('activity','whoop_lift',      85, 'per-set with HR'),
  ('activity','garmin',          80, 'per-exercise aggregates + HR zones'),
  ('activity','whoop',           70, 'session summary + strain + zones'),
  ('activity','whoop_export',    65, 'official CSV export, historical'),
  ('activity','pushpress',       50, 'class attendance'),
  ('activity','loseit',          20, 'self-reported minutes only'),
  ('activity','zero',            15, 'Apple-sourced active minutes'),
  ('activity','manual',          95, 'explicitly entered by the user'),

  -- SLEEP: Whoop is the primary wearable; Garmin covers the Whoop-off window.
  ('sleep','whoop',              90, 'public API, full stage detail'),
  ('sleep','whoop_export',       85, 'official CSV export, full stage detail'),
  ('sleep','whoop_private',      80, 'private trends, no stage split'),
  ('sleep','garmin',             70, 'full stage detail, covers Whoop-off window'),
  ('sleep','zero',               30, 'Apple Health passthrough, duration only'),
  ('sleep','loseit',             20, 'manually entered hours'),
  ('sleep','manual',             95, 'explicitly entered by the user'),

  -- NUTRITION: macro-complete official APIs beat photo estimates beat totals.
  ('nutrition','cronometer',     90, 'official API, micronutrient complete'),
  ('nutrition','loseit',         70, 'per-item macros from export'),
  ('nutrition','cal_ai',         60, 'photo estimate, macros only'),
  ('nutrition','apple_health',   40, 'daily totals passthrough'),
  ('nutrition','zero',           10, 'timestamps corrupted in export'),
  ('nutrition','manual',         95, 'explicitly entered by the user'),

  -- BODY: measurement method dominates, but source breaks ties within method.
  ('body','dxa_report',         100, 'clinical scan'),
  ('body','manual',              95, 'explicitly entered by the user'),
  ('body','withings',            80, 'smart scale'),
  ('body','garmin',              70, 'Garmin Index / manual entry'),
  ('body','loseit',              65, 'manually logged scale weight'),
  ('body','zero',                60, 'Apple Health passthrough'),
  ('body','whoop',               50, 'Whoop Body bioimpedance'),
  ('body','apple_health',        45, 'aggregated passthrough'),
  ('body','cronometer',          40, 'manual biometric entry'),

  -- METRIC: whichever wearable was actually worn.
  ('metric','whoop',             90, ''),
  ('metric','whoop_export',      85, ''),
  ('metric','garmin',            80, ''),
  ('metric','apple_health',      50, ''),
  ('metric','zero',              40, ''),
  ('metric','loseit',            30, ''),
  ('metric','cronometer',        20, ''),
  ('metric','manual',            95, '')
ON CONFLICT (domain, source) DO UPDATE
  SET priority = EXCLUDED.priority,
      notes    = EXCLUDED.notes,
      updated_at = now();

-- =========================================================================
-- ACTIVITY
-- =========================================================================

-- One row per real session. Cluster-deduped: when Whoop and Garmin both
-- recorded the same lift, only the primary row survives.
CREATE OR REPLACE VIEW vw_activity AS
SELECT
  a.activity_uid,
  a.day,
  a.start_ts,
  a.end_ts,
  a.activity_type,
  a.name,
  a.source,
  a.is_resistance,
  a.duration_s,
  ROUND(a.duration_s / 60.0, 1)                       AS duration_min,
  a.strain,
  a.training_load,
  a.rpe,
  a.avg_hr,
  a.max_hr,
  a.kcal,
  a.kilojoules,
  a.distance_m,
  a.elevation_gain_m,
  a.steps,
  a.zone_0_s, a.zone_1_s, a.zone_2_s, a.zone_3_s, a.zone_4_s, a.zone_5_s,
  ROUND((COALESCE(a.zone_4_s,0) + COALESCE(a.zone_5_s,0)) / 60.0, 1) AS hard_minutes,
  a.total_sets,
  a.total_reps,
  a.total_volume_kg,
  a.unique_exercises,
  a.cluster_id,
  (SELECT count(*) FROM fact_activity o
     WHERE o.cluster_id = a.cluster_id AND a.cluster_id IS NOT NULL) AS source_count,
  EXISTS (SELECT 1 FROM data_quality_flag f
            WHERE f.table_name = 'fact_activity'
              AND f.row_key = a.activity_uid AND NOT f.resolved)     AS is_flagged
FROM fact_activity a
WHERE a.is_primary;

-- Every exercise performed, any source, one shape. Garmin rows carry
-- granularity='exercise' (aggregates only); Whoop/Hevy carry 'set'.
CREATE OR REPLACE VIEW vw_activity_exercise AS
SELECT
  e.activity_uid,
  e.day,
  a.start_ts,
  a.activity_type,
  a.name                AS session_name,
  e.source,
  e.exercise_index,
  COALESCE(e.exercise_key, 'unmapped:' || lower(e.vendor_exercise)) AS exercise_key,
  COALESCE(d.display_name, e.vendor_exercise)                       AS exercise_name,
  e.vendor_exercise,
  e.vendor_category,
  e.granularity,
  e.set_count,
  e.total_reps,
  e.total_volume_kg,
  e.max_weight_kg,
  e.duration_s,
  e.is_pr,
  d.movement_pattern,
  d.primary_muscle,
  d.equipment,
  d.is_compound,
  d.is_spinal_flexion
FROM fact_activity_exercise e
JOIN fact_activity a USING (activity_uid)
LEFT JOIN dim_exercise d ON d.exercise_key = e.exercise_key
WHERE a.is_primary;

-- Per-set detail where it exists.
CREATE OR REPLACE VIEW vw_activity_set AS
SELECT
  s.activity_uid,
  s.day,
  a.start_ts,
  a.name                AS session_name,
  s.source,
  s.exercise_index,
  s.set_index,
  COALESCE(s.exercise_key, 'unmapped:' || lower(s.vendor_exercise)) AS exercise_key,
  COALESCE(d.display_name, s.vendor_exercise)                       AS exercise_name,
  s.vendor_exercise,
  s.set_type,
  s.reps,
  s.weight_kg,
  ROUND(s.weight_kg * 2.20462, 1)  AS weight_lb,
  s.volume_kg,
  s.duration_s,
  s.rpe,
  s.avg_hr,
  s.is_pr,
  d.movement_pattern,
  d.primary_muscle,
  d.is_spinal_flexion
FROM fact_activity_set s
JOIN fact_activity a USING (activity_uid)
LEFT JOIN dim_exercise d ON d.exercise_key = s.exercise_key
WHERE a.is_primary;

-- Estimated rep maxes per canonical exercise (Epley), from true per-set rows.
CREATE OR REPLACE VIEW vw_exercise_rep_max_unified AS
SELECT
  exercise_key,
  exercise_name,
  reps                                             AS rep_count,
  MAX(weight_kg)                                   AS max_weight_kg,
  MAX(weight_kg * (1 + reps / 30.0))               AS est_one_rm_kg,
  MAX(day)                                         AS last_hit_day,
  COUNT(*)                                         AS sample_count
FROM vw_activity_set
WHERE weight_kg > 0 AND reps > 0
GROUP BY exercise_key, exercise_name, reps;

-- =========================================================================
-- SLEEP -- one main sleep per day, best available source.
-- =========================================================================
CREATE OR REPLACE VIEW vw_sleep_daily AS
WITH ranked AS (
  SELECT s.*,
         COALESCE(p.priority, 0) AS prio,
         ROW_NUMBER() OVER (
           PARTITION BY s.day
           ORDER BY COALESCE(p.priority, 0) DESC,
                    COALESCE(s.asleep_s, 0) DESC,
                    s.updated_at DESC
         ) AS rn
  FROM fact_sleep_session s
  LEFT JOIN dim_source_priority p ON p.domain = 'sleep' AND p.source = s.source
  WHERE NOT s.is_nap AND s.is_primary
)
SELECT
  day,
  sleep_uid,
  source,
  start_ts,
  end_ts,
  ROUND(asleep_s / 3600.0, 2)  AS asleep_hours,
  ROUND(time_in_bed_s / 3600.0, 2) AS in_bed_hours,
  ROUND(rem_s / 3600.0, 2)     AS rem_hours,
  ROUND(deep_s / 3600.0, 2)    AS deep_hours,
  ROUND(light_s / 3600.0, 2)   AS light_hours,
  ROUND(awake_s / 60.0, 1)     AS awake_min,
  efficiency_pct,
  performance_pct,
  consistency_pct,
  score,
  ROUND(sleep_need_s / 3600.0, 2) AS sleep_need_hours,
  ROUND(sleep_debt_s / 60.0, 1)   AS sleep_debt_min,
  respiratory_rate,
  avg_hr,
  avg_spo2,
  disturbance_count,
  cycle_count,
  (SELECT count(*) FROM fact_sleep_session n
     WHERE n.day = ranked.day AND n.is_nap AND n.is_primary)                    AS nap_count,
  (SELECT COALESCE(SUM(n.asleep_s), 0) / 60.0 FROM fact_sleep_session n
     WHERE n.day = ranked.day AND n.is_nap AND n.is_primary)                    AS nap_total_min
FROM ranked
WHERE rn = 1;

-- =========================================================================
-- NUTRITION -- one row per day, best available source.
-- =========================================================================
CREATE OR REPLACE VIEW vw_nutrition_daily AS
WITH ranked AS (
  SELECT n.*,
         COALESCE(p.priority, 0) AS prio,
         ROW_NUMBER() OVER (
           PARTITION BY n.day
           ORDER BY COALESCE(p.priority, 0) DESC, n.energy_kcal DESC NULLS LAST
         ) AS rn
  FROM fact_nutrition_daily n
  LEFT JOIN dim_source_priority p ON p.domain = 'nutrition' AND p.source = n.source
  WHERE COALESCE(n.energy_kcal, 0) > 0
)
SELECT
  day, source, energy_kcal, protein_g, carbs_g, net_carbs_g, fiber_g, sugar_g,
  fat_g, saturated_fat_g, cholesterol_mg, sodium_mg, potassium_mg,
  caffeine_mg, alcohol_g, entry_count, first_eaten_at, last_eaten_at,
  ROUND(EXTRACT(EPOCH FROM (last_eaten_at - first_eaten_at)) / 3600.0, 2) AS eating_window_hours,
  is_rollup,
  (SELECT count(DISTINCT o.source) FROM fact_nutrition_daily o
     WHERE o.day = ranked.day AND COALESCE(o.energy_kcal,0) > 0) AS source_count
FROM ranked
WHERE rn = 1;

-- =========================================================================
-- BODY COMPOSITION -- method beats source. DXA always wins.
-- =========================================================================
CREATE OR REPLACE VIEW vw_body_composition AS
SELECT
  b.*,
  CASE b.method
    WHEN 'dxa'          THEN 100
    WHEN 'bodpod'       THEN 90
    WHEN 'calipers'     THEN 60
    WHEN 'scale'        THEN 55
    WHEN 'bioimpedance' THEN 50
    WHEN 'tape'         THEN 40
    WHEN 'manual'       THEN 45
    ELSE 10
  END                                        AS method_rank,
  COALESCE(p.priority, 0)                    AS source_rank,
  ROUND(b.weight_kg * 2.20462, 1)            AS weight_lb,
  EXISTS (SELECT 1 FROM data_quality_flag f
            WHERE f.table_name = 'fact_body_composition'
              AND f.row_key = b.measurement_uid AND NOT f.resolved) AS is_flagged
FROM fact_body_composition b
LEFT JOIN dim_source_priority p ON p.domain = 'body' AND p.source = b.source;

-- One weight + body-fat reading per day: highest method rank, then source,
-- then the median-most reading of that day. Flagged rows excluded.
CREATE OR REPLACE VIEW vw_body_daily AS
WITH clean AS (
  SELECT * FROM vw_body_composition WHERE NOT is_flagged
),
w AS (
  SELECT DISTINCT ON (day) day, weight_kg, weight_lb, method, source, measured_at
  FROM clean WHERE weight_kg IS NOT NULL
  ORDER BY day, method_rank DESC, source_rank DESC, measured_at DESC
),
f AS (
  SELECT DISTINCT ON (day) day, body_fat_pct, lean_mass_kg, fat_mass_kg,
         method AS bf_method, source AS bf_source
  FROM clean WHERE body_fat_pct IS NOT NULL
  ORDER BY day, method_rank DESC, source_rank DESC, measured_at DESC
)
SELECT
  COALESCE(w.day, f.day) AS day,
  w.weight_kg, w.weight_lb, w.method AS weight_method, w.source AS weight_source,
  f.body_fat_pct, f.lean_mass_kg, f.fat_mass_kg, f.bf_method, f.bf_source
FROM w FULL OUTER JOIN f ON w.day = f.day;

-- =========================================================================
-- DAILY METRICS -- one value per (day, metric), best source.
-- =========================================================================
CREATE OR REPLACE VIEW vw_daily_metric AS
SELECT DISTINCT ON (m.day, m.metric)
  m.day, m.metric, m.value, m.unit, m.source
FROM fact_daily_metric m
LEFT JOIN dim_source_priority p ON p.domain = 'metric' AND p.source = m.source
WHERE m.value IS NOT NULL
ORDER BY m.day, m.metric, COALESCE(p.priority, 0) DESC, m.updated_at DESC;

-- =========================================================================
-- ADHERENCE -- GAP-10. The variable that predicts training-block collapse.
-- =========================================================================
CREATE OR REPLACE VIEW vw_training_day AS
SELECT
  day,
  COUNT(*)                                             AS session_count,
  COUNT(*) FILTER (WHERE is_resistance)                AS resistance_count,
  SUM(duration_s) / 60.0                               AS total_min,
  SUM(COALESCE(total_volume_kg, 0))                    AS volume_kg,
  SUM((COALESCE(zone_4_s,0) + COALESCE(zone_5_s,0)) / 60.0) AS hard_minutes,
  MAX(strain)                                          AS max_strain
FROM vw_activity
GROUP BY day;

CREATE OR REPLACE VIEW vw_adherence AS
WITH spine AS (
  SELECT generate_series(
           (SELECT MIN(day) FROM vw_training_day),
           CURRENT_DATE,
           '1 day'::interval
         )::date AS day
),
j AS (
  SELECT s.day,
         COALESCE(t.session_count, 0)    AS sessions,
         COALESCE(t.resistance_count, 0) AS resistance_sessions
  FROM spine s LEFT JOIN vw_training_day t USING (day)
)
SELECT
  day,
  sessions,
  resistance_sessions,
  ROUND(SUM(sessions) OVER w4  / 4.0,  2) AS sessions_per_week_4w,
  ROUND(SUM(sessions) OVER w12 / 12.0, 2) AS sessions_per_week_12w,
  ROUND(SUM(sessions) OVER w26 / 26.0, 2) AS sessions_per_week_26w,
  ROUND(SUM(resistance_sessions) OVER w12 / 12.0, 2) AS resistance_per_week_12w,
  (SUM(sessions) OVER w12 / 12.0) < 3 AS below_floor
FROM j
WINDOW
  w4  AS (ORDER BY day ROWS BETWEEN 27  PRECEDING AND CURRENT ROW),
  w12 AS (ORDER BY day ROWS BETWEEN 83  PRECEDING AND CURRENT ROW),
  w26 AS (ORDER BY day ROWS BETWEEN 181 PRECEDING AND CURRENT ROW);

-- =========================================================================
-- FRESHNESS -- GAP-8. "Is what you're telling me current?" in one query.
-- =========================================================================
CREATE OR REPLACE VIEW vw_data_freshness AS
SELECT
  h.source_key,
  h.domain,
  h.display_name,
  h.mode,
  h.last_row_day,
  h.expected_lag_hours                            AS sla_hours,
  (CURRENT_DATE - h.last_row_day) * 24            AS lag_hours,
  h.coverage_start,
  h.coverage_end,
  CASE
    WHEN h.mode = 'retired'    THEN 'retired'
    WHEN h.mode = 'historical' THEN 'historical'
    WHEN h.last_row_day IS NULL THEN 'no_data'
    WHEN h.expected_lag_hours IS NULL THEN 'ok'
    WHEN (CURRENT_DATE - h.last_row_day) * 24 <= h.expected_lag_hours THEN 'ok'
    WHEN (CURRENT_DATE - h.last_row_day) * 24 <= h.expected_lag_hours * 3 THEN 'lagging'
    ELSE 'stale'
  END                                             AS status,
  h.last_checked_at,
  h.notes
FROM source_health h;

-- =========================================================================
-- COVERAGE -- day x domain matrix. Drives the "what's missing" report.
-- =========================================================================
CREATE OR REPLACE VIEW vw_coverage_daily AS
WITH bounds AS (
  SELECT LEAST(
           (SELECT MIN(day) FROM fact_activity),
           (SELECT MIN(day) FROM fact_sleep_session),
           (SELECT MIN(day) FROM fact_nutrition_daily),
           (SELECT MIN(day) FROM fact_body_composition)
         ) AS d0
),
spine AS (
  SELECT generate_series((SELECT d0 FROM bounds), CURRENT_DATE, '1 day'::interval)::date AS day
)
SELECT
  s.day,
  EXISTS (SELECT 1 FROM fact_activity        a WHERE a.day = s.day AND a.is_primary) AS has_activity,
  EXISTS (SELECT 1 FROM fact_sleep_session   sl WHERE sl.day = s.day AND NOT sl.is_nap) AS has_sleep,
  EXISTS (SELECT 1 FROM fact_nutrition_daily n WHERE n.day = s.day AND COALESCE(n.energy_kcal,0) > 0) AS has_nutrition,
  EXISTS (SELECT 1 FROM fact_body_composition b WHERE b.day = s.day AND b.weight_kg IS NOT NULL) AS has_weight,
  EXISTS (SELECT 1 FROM fact_daily_metric    m WHERE m.day = s.day) AS has_daily_metric,
  (SELECT string_agg(DISTINCT a.source, ',') FROM fact_activity a WHERE a.day = s.day)        AS activity_sources,
  (SELECT string_agg(DISTINCT sl.source, ',') FROM fact_sleep_session sl WHERE sl.day = s.day) AS sleep_sources,
  (SELECT string_agg(DISTINCT n.source, ',') FROM fact_nutrition_daily n
     WHERE n.day = s.day AND COALESCE(n.energy_kcal,0) > 0)                                    AS nutrition_sources
FROM spine s;

COMMIT;

-- 0036_unified_data_layer.sql
--
-- The unified data layer. Every health/training/nutrition source lands here in
-- ONE canonical shape, in SI units (kg, meters, seconds), tagged with its
-- source and a confidence/method where the measurement technique matters.
--
-- Layering:
--     raw_import  (immutable JSONB, natural-keyed, never transformed)
--         -> fact_* canonical (SI, source-tagged, cluster-deduped)
--             -> vw_* / mart_daily (analysis surface)
--
-- Source-specific fact tables (fact_workout, fact_whoop_lift_*, fact_sleep,
-- fact_strength_*, fact_food_log, ...) are NOT dropped. They stay as the
-- landing zone for their own ingesters; the `unify` package projects them into
-- the canonical tables below. Query the canonical tables; treat the
-- source-specific ones as implementation detail.
--
-- Deduplication: the same session can be recorded by Whoop AND Garmin AND
-- Hevy. Rows that describe the same real-world event share a `cluster_id`;
-- exactly one row per cluster has `is_primary = true` (highest-priority source
-- with the richest payload). Read through vw_activity / vw_sleep_session to
-- get one row per real event.

BEGIN;

-- =========================================================================
-- RAW LAYER  ---------------------------------------------------------------
-- One generic immutable landing table instead of N per-vendor raw tables.
-- Every file import and every API pull that isn't already covered by an
-- existing raw_* table writes here first, unchanged.
-- =========================================================================
CREATE TABLE IF NOT EXISTS raw_import (
  id            BIGSERIAL PRIMARY KEY,
  source        TEXT NOT NULL,          -- 'garmin', 'zero', 'loseit', 'whoop_export', 'calai_pdf', ...
  entity        TEXT NOT NULL,          -- 'activity', 'sleep', 'daily', 'weight', 'food_log', ...
  natural_key   TEXT NOT NULL,          -- stable per-record identity within (source, entity)
  occurred_on   DATE,                   -- local day the record describes, when knowable
  payload       JSONB NOT NULL,
  file_name     TEXT,                   -- provenance: which export file it came from
  imported_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source, entity, natural_key)
);
CREATE INDEX IF NOT EXISTS raw_import_source_entity_day_idx
  ON raw_import (source, entity, occurred_on);
CREATE INDEX IF NOT EXISTS raw_import_day_idx ON raw_import (occurred_on);

-- =========================================================================
-- DIMENSIONS  --------------------------------------------------------------
-- =========================================================================

-- Vendor activity string -> canonical type. GAP-11.
CREATE TABLE IF NOT EXISTS dim_activity_type (
  source         TEXT NOT NULL,        -- 'whoop', 'garmin', 'hevy', 'pushpress', 'loseit', 'zero', 'manual'
  vendor_type    TEXT NOT NULL,        -- raw vendor string, lowercased
  canonical_type TEXT NOT NULL,        -- strength|conditioning|run|cycle|swim|racquet|walk|mobility|sport|other
  is_resistance  BOOLEAN NOT NULL DEFAULT FALSE,
  is_cardio      BOOLEAN NOT NULL DEFAULT FALSE,
  notes          TEXT,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source, vendor_type)
);

-- Canonical movement catalogue. `is_spinal_flexion` lets programming queries
-- filter contraindicated movements against the L4-L5 / L5-S1 findings without
-- hardcoding exercise names in every query.
CREATE TABLE IF NOT EXISTS dim_exercise (
  exercise_key       TEXT PRIMARY KEY,      -- snake_case canonical, e.g. 'barbell_bench_press'
  display_name       TEXT NOT NULL,
  movement_pattern   TEXT,                  -- squat|hinge|push_h|push_v|pull_h|pull_v|carry|lunge|isolation|core|other
  primary_muscle     TEXT,
  secondary_muscles  TEXT[],
  equipment          TEXT,                  -- barbell|dumbbell|cable|machine|bodyweight|kettlebell|band|other
  is_compound        BOOLEAN NOT NULL DEFAULT FALSE,
  is_spinal_flexion  BOOLEAN NOT NULL DEFAULT FALSE,
  is_unilateral      BOOLEAN NOT NULL DEFAULT FALSE,
  notes              TEXT,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Vendor exercise id / free-text name -> canonical exercise_key.
CREATE TABLE IF NOT EXISTS dim_exercise_alias (
  source        TEXT NOT NULL,   -- 'garmin', 'whoop', 'hevy', 'pushpress', 'manual'
  vendor_key    TEXT NOT NULL,   -- vendor id or normalized name
  exercise_key  TEXT NOT NULL REFERENCES dim_exercise(exercise_key) ON UPDATE CASCADE,
  vendor_label  TEXT,            -- human label as the vendor spelled it
  confidence    NUMERIC NOT NULL DEFAULT 1.0,   -- 1.0 exact, <1 fuzzy-matched
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source, vendor_key)
);
CREATE INDEX IF NOT EXISTS dim_exercise_alias_key_idx ON dim_exercise_alias (exercise_key);

-- =========================================================================
-- FACT: ACTIVITY  ----------------------------------------------------------
-- One row per training session, any source. Supersedes fact_workout /
-- fact_strength_workout / fact_whoop_lift_workout / fact_pushpress_session
-- as the query surface.
-- =========================================================================
CREATE TABLE IF NOT EXISTS fact_activity (
  activity_uid       TEXT PRIMARY KEY,      -- '<source>:<source_activity_id>'
  source             TEXT NOT NULL,
  source_activity_id TEXT NOT NULL,

  start_ts           TIMESTAMPTZ NOT NULL,
  end_ts             TIMESTAMPTZ,
  day                DATE NOT NULL,          -- America/New_York local date of start
  duration_s         NUMERIC,                -- elapsed
  moving_duration_s  NUMERIC,

  activity_type      TEXT NOT NULL DEFAULT 'other',  -- canonical
  vendor_type        TEXT,
  name               TEXT,
  is_resistance      BOOLEAN NOT NULL DEFAULT FALSE,

  -- load / intensity
  strain             NUMERIC,     -- Whoop 0-21
  training_load      NUMERIC,     -- Garmin activityTrainingLoad
  aerobic_te         NUMERIC,
  anaerobic_te       NUMERIC,
  rpe                NUMERIC,
  intensity_pct      NUMERIC,

  -- heart / energy
  avg_hr             INTEGER,
  max_hr             INTEGER,
  min_hr             INTEGER,
  kcal               NUMERIC,
  kilojoules         NUMERIC,

  -- HR zone seconds (SI: seconds, not minutes)
  zone_0_s           NUMERIC,
  zone_1_s           NUMERIC,
  zone_2_s           NUMERIC,
  zone_3_s           NUMERIC,
  zone_4_s           NUMERIC,
  zone_5_s           NUMERIC,

  -- movement
  distance_m         NUMERIC,
  elevation_gain_m   NUMERIC,
  steps              INTEGER,

  -- strength rollups (from fact_activity_exercise / _set)
  total_sets         INTEGER,
  total_reps         INTEGER,
  total_volume_kg    NUMERIC,
  unique_exercises   INTEGER,

  -- dedupe across sources
  cluster_id         BIGINT,
  is_primary         BOOLEAN NOT NULL DEFAULT TRUE,

  raw_id             BIGINT,      -- raw_import.id when file-sourced
  payload            JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fact_activity_day_idx      ON fact_activity (day);
CREATE INDEX IF NOT EXISTS fact_activity_start_idx    ON fact_activity (start_ts);
CREATE INDEX IF NOT EXISTS fact_activity_type_idx     ON fact_activity (activity_type, day);
CREATE INDEX IF NOT EXISTS fact_activity_source_idx   ON fact_activity (source, day);
CREATE INDEX IF NOT EXISTS fact_activity_cluster_idx  ON fact_activity (cluster_id);
CREATE INDEX IF NOT EXISTS fact_activity_primary_idx  ON fact_activity (day) WHERE is_primary;

-- Per-exercise rollup. Present for EVERY source that records exercises,
-- including Garmin (which only exports per-exercise aggregates, never per-set).
CREATE TABLE IF NOT EXISTS fact_activity_exercise (
  activity_uid       TEXT NOT NULL REFERENCES fact_activity(activity_uid) ON DELETE CASCADE,
  exercise_index     INTEGER NOT NULL,
  exercise_key       TEXT,                  -- canonical; NULL when unmapped
  vendor_exercise    TEXT NOT NULL,         -- vendor's own label
  vendor_category    TEXT,                  -- Garmin category (SHRUG, CURL, ...)
  source             TEXT NOT NULL,
  day                DATE NOT NULL,
  granularity        TEXT NOT NULL,         -- 'set'  = derived from per-set rows
                                            -- 'exercise' = vendor only gave aggregates
  set_count          INTEGER,
  total_reps         INTEGER,
  total_volume_kg    NUMERIC,
  max_weight_kg      NUMERIC,
  duration_s         NUMERIC,
  is_pr              BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (activity_uid, exercise_index)
);
CREATE INDEX IF NOT EXISTS fact_activity_exercise_key_idx ON fact_activity_exercise (exercise_key, day);
CREATE INDEX IF NOT EXISTS fact_activity_exercise_day_idx ON fact_activity_exercise (day);

-- True per-set rows. Only sources that record sets individually (Whoop
-- Strength Trainer, Hevy) populate this.
CREATE TABLE IF NOT EXISTS fact_activity_set (
  activity_uid    TEXT NOT NULL REFERENCES fact_activity(activity_uid) ON DELETE CASCADE,
  exercise_index  INTEGER NOT NULL,
  set_index       INTEGER NOT NULL,
  exercise_key    TEXT,
  vendor_exercise TEXT NOT NULL,
  source          TEXT NOT NULL,
  day             DATE NOT NULL,
  set_type        TEXT,                -- normal|warmup|failure|drop
  reps            INTEGER,
  weight_kg       NUMERIC,
  duration_s      NUMERIC,
  distance_m      NUMERIC,
  rpe             NUMERIC,
  avg_hr          INTEGER,
  is_pr           BOOLEAN NOT NULL DEFAULT FALSE,
  volume_kg       NUMERIC,             -- reps * weight_kg, materialized for speed
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (activity_uid, exercise_index, set_index)
);
CREATE INDEX IF NOT EXISTS fact_activity_set_key_idx ON fact_activity_set (exercise_key, day);
CREATE INDEX IF NOT EXISTS fact_activity_set_day_idx ON fact_activity_set (day);

-- =========================================================================
-- FACT: SLEEP  -------------------------------------------------------------
-- Unified across Whoop (public + private), Garmin, Zero, Lose It, Apple.
-- All durations in seconds.
-- =========================================================================
CREATE TABLE IF NOT EXISTS fact_sleep_session (
  sleep_uid         TEXT PRIMARY KEY,     -- '<source>:<id>'
  source            TEXT NOT NULL,
  source_sleep_id   TEXT NOT NULL,

  start_ts          TIMESTAMPTZ NOT NULL,
  end_ts            TIMESTAMPTZ NOT NULL,
  day               DATE NOT NULL,        -- the WAKE day (local)
  is_nap            BOOLEAN NOT NULL DEFAULT FALSE,

  time_in_bed_s     NUMERIC,
  asleep_s          NUMERIC,
  awake_s           NUMERIC,
  light_s           NUMERIC,
  deep_s            NUMERIC,
  rem_s             NUMERIC,
  unmeasurable_s    NUMERIC,

  efficiency_pct    NUMERIC,
  performance_pct   NUMERIC,
  consistency_pct   NUMERIC,
  score             NUMERIC,              -- vendor sleep score (Garmin overallScore, ...)
  sleep_need_s      NUMERIC,
  sleep_debt_s      NUMERIC,

  respiratory_rate  NUMERIC,
  avg_hr            NUMERIC,
  avg_spo2          NUMERIC,
  lowest_spo2       NUMERIC,
  avg_stress        NUMERIC,
  disturbance_count INTEGER,
  cycle_count       INTEGER,
  restless_moments  INTEGER,

  cluster_id        BIGINT,
  is_primary        BOOLEAN NOT NULL DEFAULT TRUE,

  raw_id            BIGINT,
  payload           JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fact_sleep_session_day_idx     ON fact_sleep_session (day);
CREATE INDEX IF NOT EXISTS fact_sleep_session_source_idx  ON fact_sleep_session (source, day);
CREATE INDEX IF NOT EXISTS fact_sleep_session_cluster_idx ON fact_sleep_session (cluster_id);
CREATE INDEX IF NOT EXISTS fact_sleep_session_primary_idx ON fact_sleep_session (day) WHERE is_primary AND NOT is_nap;

-- =========================================================================
-- FACT: NUTRITION  ---------------------------------------------------------
-- Per-item entries across Cronometer, Lose It, Cal AI, Apple Health, manual.
-- =========================================================================
CREATE TABLE IF NOT EXISTS fact_nutrition_entry (
  entry_uid       TEXT PRIMARY KEY,      -- '<source>:<hash-or-id>'
  source          TEXT NOT NULL,
  source_entry_id TEXT,

  eaten_at        TIMESTAMPTZ NOT NULL,
  day             DATE NOT NULL,
  meal            TEXT,                  -- breakfast|lunch|dinner|snack|unknown
  food_name       TEXT NOT NULL,
  brand           TEXT,
  amount          NUMERIC,
  unit            TEXT,

  energy_kcal     NUMERIC,
  protein_g       NUMERIC,
  carbs_g         NUMERIC,
  net_carbs_g     NUMERIC,
  fiber_g         NUMERIC,
  sugar_g         NUMERIC,
  fat_g           NUMERIC,
  saturated_fat_g NUMERIC,
  cholesterol_mg  NUMERIC,
  sodium_mg       NUMERIC,
  potassium_mg    NUMERIC,
  caffeine_mg     NUMERIC,
  alcohol_g       NUMERIC,
  micros          JSONB NOT NULL DEFAULT '{}'::jsonb,

  raw_id          BIGINT,
  payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fact_nutrition_entry_day_idx    ON fact_nutrition_entry (day);
CREATE INDEX IF NOT EXISTS fact_nutrition_entry_source_idx ON fact_nutrition_entry (source, day);

-- Daily totals per source. Some sources only ever give daily totals (Lose It
-- summary CSVs, Apple Health); others are rolled up from entries. `vw_nutrition_daily`
-- picks one row per day by source precedence.
CREATE TABLE IF NOT EXISTS fact_nutrition_daily (
  day             DATE NOT NULL,
  source          TEXT NOT NULL,
  energy_kcal     NUMERIC,
  protein_g       NUMERIC,
  carbs_g         NUMERIC,
  net_carbs_g     NUMERIC,
  fiber_g         NUMERIC,
  sugar_g         NUMERIC,
  fat_g           NUMERIC,
  saturated_fat_g NUMERIC,
  cholesterol_mg  NUMERIC,
  sodium_mg       NUMERIC,
  potassium_mg    NUMERIC,
  caffeine_mg     NUMERIC,
  alcohol_g       NUMERIC,
  entry_count     INTEGER NOT NULL DEFAULT 0,
  first_eaten_at  TIMESTAMPTZ,
  last_eaten_at   TIMESTAMPTZ,
  is_rollup       BOOLEAN NOT NULL DEFAULT TRUE,  -- FALSE = vendor-reported daily total
  micros          JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, source)
);
CREATE INDEX IF NOT EXISTS fact_nutrition_daily_day_idx ON fact_nutrition_daily (day);

-- =========================================================================
-- FACT: BODY COMPOSITION  --------------------------------------------------
-- GAP-9. Method provenance is mandatory: a DXA and a bathroom scale are not
-- the same measurement and must never be averaged together silently.
-- =========================================================================
CREATE TABLE IF NOT EXISTS fact_body_composition (
  measurement_uid   TEXT PRIMARY KEY,     -- '<source>:<method>:<epoch>'
  source            TEXT NOT NULL,
  method            TEXT NOT NULL,        -- dxa|bodpod|bioimpedance|calipers|tape|scale|manual|estimate
  measured_at       TIMESTAMPTZ NOT NULL,
  day               DATE NOT NULL,

  weight_kg         NUMERIC,
  body_fat_pct      NUMERIC,
  lean_mass_kg      NUMERIC,
  fat_mass_kg       NUMERIC,
  bone_mineral_kg   NUMERIC,
  visceral_fat      NUMERIC,
  bmi               NUMERIC,
  muscle_mass_kg    NUMERIC,
  body_water_pct    NUMERIC,
  region            JSONB NOT NULL DEFAULT '{}'::jsonb,  -- DXA regional breakdown
  confidence        NUMERIC NOT NULL DEFAULT 0.5,        -- 1.0 DXA, 0.6 scale, 0.4 bioimpedance wrist

  raw_id            BIGINT,
  payload           JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fact_body_composition_day_idx    ON fact_body_composition (day);
CREATE INDEX IF NOT EXISTS fact_body_composition_method_idx ON fact_body_composition (method, day);

-- =========================================================================
-- FACT: DAILY METRIC  ------------------------------------------------------
-- Long-format daily metrics from every source: steps, resting_hr, hrv, vo2max,
-- calories, body_battery, stress, spo2, intensity minutes... One row per
-- (day, metric, source). SI units.
-- =========================================================================
CREATE TABLE IF NOT EXISTS fact_daily_metric (
  day        DATE NOT NULL,
  metric     TEXT NOT NULL,
  source     TEXT NOT NULL,
  value      NUMERIC,
  unit       TEXT,
  payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, metric, source)
);
CREATE INDEX IF NOT EXISTS fact_daily_metric_metric_idx ON fact_daily_metric (metric, day);
CREATE INDEX IF NOT EXISTS fact_daily_metric_day_idx    ON fact_daily_metric (day);

-- =========================================================================
-- FACT: FASTING  -----------------------------------------------------------
-- =========================================================================
CREATE TABLE IF NOT EXISTS fact_fast (
  fast_uid     TEXT PRIMARY KEY,
  source       TEXT NOT NULL,
  start_ts     TIMESTAMPTZ NOT NULL,
  end_ts       TIMESTAMPTZ,
  day          DATE NOT NULL,      -- local day the fast STARTED
  duration_s   NUMERIC,
  goal_hours   NUMERIC,
  goal_key     TEXT,
  is_ended     BOOLEAN NOT NULL DEFAULT TRUE,
  hit_goal     BOOLEAN,
  raw_id       BIGINT,
  payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fact_fast_day_idx ON fact_fast (day);

-- =========================================================================
-- OPERATIONS: source health + data quality  --------------------------------
-- =========================================================================

-- GAP-8 half 1: one row per logical source stream, so "is what you're telling
-- me current?" is a single query rather than max(day) on nine tables.
CREATE TABLE IF NOT EXISTS source_health (
  source_key         TEXT PRIMARY KEY,   -- 'whoop.sleep', 'garmin.activity', 'cronometer.food', ...
  domain             TEXT NOT NULL,      -- sleep|activity|nutrition|body|labs|metric|finance|calendar
  display_name       TEXT NOT NULL,
  mode               TEXT NOT NULL,      -- live | historical | retired
  expected_lag_hours NUMERIC,            -- NULL for historical/retired: never stale
  table_name         TEXT,               -- table whose max(day) defines freshness
  day_column         TEXT NOT NULL DEFAULT 'day',
  source_filter      TEXT,               -- value of `source` column identifying this stream
  coverage_start     DATE,               -- first day this source is expected to have data
  coverage_end       DATE,               -- last day (for retired sources)
  notes              TEXT,
  last_row_day       DATE,
  last_checked_at    TIMESTAMPTZ,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- GAP-8 half 2: flag, never delete. The 239.4 lb reading is informative.
CREATE TABLE IF NOT EXISTS data_quality_flag (
  id          BIGSERIAL PRIMARY KEY,
  table_name  TEXT NOT NULL,
  row_key     TEXT,                 -- primary-key value of the offending row, when known
  day         DATE,
  metric      TEXT,
  value       NUMERIC,
  reason      TEXT NOT NULL,
  rule        TEXT,                 -- 'plausibility'|'dispersion'|'rate_of_change'|'manual'
  severity    TEXT NOT NULL DEFAULT 'warn',   -- warn|reject
  flagged_by  TEXT NOT NULL DEFAULT 'auto',
  resolved    BOOLEAN NOT NULL DEFAULT FALSE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (table_name, row_key, rule, metric)
);
CREATE INDEX IF NOT EXISTS data_quality_flag_day_idx   ON data_quality_flag (day);
CREATE INDEX IF NOT EXISTS data_quality_flag_table_idx ON data_quality_flag (table_name, resolved);

COMMIT;

-- 0009_whoop_journal.sql
-- Whoop journal entries (private API). Three new tables + a pivot of the
-- highest-frequency habits onto mart_daily for fast correlation queries.
-- Anything not pivoted is still queryable via fact_habit_log.

-- Raw response payloads keyed by day. The drafts endpoint returns one big
-- JSON per date with tracked_behaviors[], notes, integrations, sleep_during.
CREATE TABLE IF NOT EXISTS raw_whoop_journal (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  day DATE NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

-- Catalog of all 200+ Whoop behaviors. Refreshed weekly. Identifiers come
-- from Whoop's /v3/journals/behaviors endpoint.
CREATE TABLE IF NOT EXISTS dim_whoop_behavior (
  behavior_id INT PRIMARY KEY,
  internal_name TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  question_text TEXT,
  category TEXT,
  behavior_type TEXT,
  question_type TEXT,
  magnitude_type TEXT,
  magnitude_unit TEXT,
  magnitude_min NUMERIC(10,3),
  magnitude_max NUMERIC(10,3),
  status TEXT NOT NULL DEFAULT 'active',
  payload JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (day, behavior). UNIQUE(day, whoop_behavior_id) enforces it
-- without us hand-rolling the constraint into source_row_hash.
CREATE TABLE IF NOT EXISTS fact_habit_log (
  id BIGSERIAL PRIMARY KEY,
  day DATE NOT NULL,
  source TEXT NOT NULL DEFAULT 'whoop_journal',
  habit_key TEXT NOT NULL,
  whoop_behavior_id INT REFERENCES dim_whoop_behavior(behavior_id),
  whoop_journal_entry_id BIGINT,
  whoop_cycle_id BIGINT,
  answered_yes BOOLEAN,
  magnitude_value NUMERIC(10,3),
  magnitude_unit TEXT,
  time_input_value TIMESTAMPTZ,
  user_reviewed BOOLEAN,
  notes TEXT,
  source_row_hash TEXT NOT NULL UNIQUE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(day, whoop_behavior_id)
);

CREATE INDEX IF NOT EXISTS ix_habit_log_key_day
  ON fact_habit_log(habit_key, day);
CREATE INDEX IF NOT EXISTS ix_habit_log_day
  ON fact_habit_log(day);

-- Pivoted habit columns on mart_daily for fast correlation queries.
-- Anything with non-trivial daily frequency lives here as a typed column;
-- everything else stays in fact_habit_log.
ALTER TABLE mart_daily
  ADD COLUMN IF NOT EXISTS had_alcohol BOOLEAN,
  ADD COLUMN IF NOT EXISTS alcohol_drinks NUMERIC(4,1),
  ADD COLUMN IF NOT EXISTS had_caffeine BOOLEAN,
  ADD COLUMN IF NOT EXISTS caffeine_servings NUMERIC(4,1),
  ADD COLUMN IF NOT EXISTS caffeine_last_serving_time TIME,
  ADD COLUMN IF NOT EXISTS late_meal BOOLEAN,
  ADD COLUMN IF NOT EXISTS read_in_bed BOOLEAN,
  ADD COLUMN IF NOT EXISTS device_in_bed BOOLEAN,
  ADD COLUMN IF NOT EXISTS device_in_bed_minutes NUMERIC(5,1),
  ADD COLUMN IF NOT EXISTS morning_sunlight BOOLEAN,
  ADD COLUMN IF NOT EXISTS sexual_activity BOOLEAN,
  ADD COLUMN IF NOT EXISTS stretching BOOLEAN,
  ADD COLUMN IF NOT EXISTS rest_day BOOLEAN,
  ADD COLUMN IF NOT EXISTS took_magnesium BOOLEAN,
  ADD COLUMN IF NOT EXISTS took_vitamin_d BOOLEAN,
  ADD COLUMN IF NOT EXISTS took_creatine BOOLEAN,
  ADD COLUMN IF NOT EXISTS took_l_theanine BOOLEAN,
  ADD COLUMN IF NOT EXISTS joint_pain BOOLEAN,
  ADD COLUMN IF NOT EXISTS headache BOOLEAN,
  ADD COLUMN IF NOT EXISTS journal_notes TEXT;

-- Apple-Health-derived macros pulled from Whoop's integrations.tracker_inputs.
-- Stored separately from Cronometer's fact_food_daily so we can cross-validate
-- and so each source remains independently queryable.
CREATE TABLE IF NOT EXISTS fact_food_daily_apple_health (
  day DATE PRIMARY KEY,
  energy_kcal NUMERIC(10,2),
  protein_g NUMERIC(10,3),
  carbs_g NUMERIC(10,3),
  fat_g NUMERIC(10,3),
  fiber_g NUMERIC(10,3),
  sodium_mg NUMERIC(10,2),
  calcium_mg NUMERIC(10,2),
  magnesium_mg NUMERIC(10,2),
  water_servings NUMERIC(6,2),
  source TEXT NOT NULL DEFAULT 'apple_health_via_whoop',
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

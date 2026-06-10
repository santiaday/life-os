-- 0026_whoop_lift.sql
-- Whoop Strength Trainer ingestion. Replaces Hevy as the source of mart_daily's
-- strength rollup (strength_total_volume_kg / strength_total_sets /
-- strength_unique_exercises) after the Hevy/PushPress/coach deprecation.
--
-- Whoop's per-workout strength aggregates (msk_total_volume_kg already in kg,
-- set_count, exercise_count) map directly onto the existing mart columns; the
-- per-exercise breakdown is preserved as JSONB on the raw row.

CREATE TABLE IF NOT EXISTS raw_whoop_lift (
  id          BIGSERIAL PRIMARY KEY,
  fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  activity_id TEXT NOT NULL UNIQUE,
  payload     JSONB NOT NULL
);

-- One row per logged strength-trainer workout. total_volume_kg is Whoop's
-- msk_total_volume_kg (kilograms); exercise_count / set_count are the workout's
-- own tallies. exercises holds the per-exercise breakdown
-- ([{exercise_id, name, set_count, total_reps, tonnage, tonnage_units}, ...])
-- so per-exercise queries don't need a separate table yet.
CREATE TABLE IF NOT EXISTS fact_whoop_lift_workout (
  activity_id      TEXT PRIMARY KEY,
  day              DATE NOT NULL,
  name             TEXT,
  duration_minutes NUMERIC(8,1),
  strain           NUMERIC(6,3),
  total_volume_kg  NUMERIC(12,2),
  intensity_pct    NUMERIC(6,2),
  exercise_count   INT,
  set_count        INT,
  exercises        JSONB,
  raw_id           BIGINT REFERENCES raw_whoop_lift(id),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_whoop_lift_workout_day
  ON fact_whoop_lift_workout (day);

-- 0015_hevy_schema.sql
-- Hevy strength-training ingestion. Hevy gives per-set exercise/weight/reps/RPE
-- detail that Whoop deliberately omits; fact_workout still owns the HR/strain
-- view of the same session via the optional whoop_workout_id link below.
--
-- Tables follow the existing raw_*/dim_*/fact_* convention:
--   raw_hevy_workout       — full JSON payload, BIGSERIAL surrogate + UUID natural key
--   dim_hevy_exercise      — Hevy's exercise template catalog
--   fact_strength_set      — one row per set, denormalized day for time queries
--   fact_strength_workout  — workout-level rollup with optional whoop_workout_id link
--
-- mart_daily gets three additive columns (strength_total_volume_kg,
-- strength_total_sets, strength_unique_exercises) populated by mart_refresh.

BEGIN;

-- ---- Raw payloads ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_hevy_workout (
  id              BIGSERIAL PRIMARY KEY,
  fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  hevy_workout_id UUID NOT NULL UNIQUE,
  payload         JSONB NOT NULL,
  -- payload.updated_at — promoted out so the daily incremental can pick the
  -- highest updated_at_src as the "since" cursor without re-parsing JSON.
  updated_at_src  TIMESTAMPTZ,
  -- Tombstone bit: Hevy /workouts/events emits 'deleted' for removed sessions.
  -- We keep the raw row around (audit / debugging) and propagate the delete
  -- by cascading from the fact tables.
  deleted         BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS ix_raw_hevy_workout_updated
  ON raw_hevy_workout(updated_at_src);

-- ---- Exercise template catalog ---------------------------------------------
CREATE TABLE IF NOT EXISTS dim_hevy_exercise (
  exercise_template_id    UUID PRIMARY KEY,
  title                   TEXT NOT NULL,
  exercise_type           TEXT,        -- weight_reps | reps_only | duration | distance_duration
  primary_muscle_group    TEXT,
  secondary_muscle_groups TEXT[],
  equipment               TEXT,
  is_custom               BOOLEAN,
  payload                 JSONB,
  fetched_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_dim_hevy_exercise_muscle
  ON dim_hevy_exercise(primary_muscle_group);

-- ---- Fact: per-set ---------------------------------------------------------
-- Composite PK on (hevy_workout_id, exercise_index, set_index). FK references
-- raw_hevy_workout.hevy_workout_id (UNIQUE) so DELETE on the raw row cascades
-- and a re-ingest can DELETE+INSERT cleanly.
CREATE TABLE IF NOT EXISTS fact_strength_set (
  hevy_workout_id      UUID NOT NULL REFERENCES raw_hevy_workout(hevy_workout_id) ON DELETE CASCADE,
  exercise_index       INT NOT NULL,
  set_index            INT NOT NULL,
  exercise_template_id UUID REFERENCES dim_hevy_exercise(exercise_template_id),
  exercise_title       TEXT NOT NULL,
  set_type             TEXT,        -- warmup | normal | failure | dropset
  weight_kg            NUMERIC(8,3),
  reps                 INT,
  rpe                  NUMERIC(4,1),
  distance_meters      NUMERIC(10,2),
  duration_seconds     INT,
  superset_id          INT,
  notes                TEXT,
  workout_start_ts     TIMESTAMPTZ NOT NULL,
  workout_end_ts       TIMESTAMPTZ NOT NULL,
  day                  DATE GENERATED ALWAYS AS (local_date(workout_start_ts)) STORED,
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (hevy_workout_id, exercise_index, set_index)
);

CREATE INDEX IF NOT EXISTS ix_fact_strength_set_day
  ON fact_strength_set(day);
CREATE INDEX IF NOT EXISTS ix_fact_strength_set_template
  ON fact_strength_set(exercise_template_id);
CREATE INDEX IF NOT EXISTS ix_fact_strength_set_title
  ON fact_strength_set(exercise_title);

-- ---- Fact: workout rollup --------------------------------------------------
-- One row per Hevy workout. whoop_workout_id is populated by the ingester
-- via the ±10/15min start/end window match against fact_workout — soft FK so
-- a Whoop re-ingest that changes workout_id doesn't break us.
CREATE TABLE IF NOT EXISTS fact_strength_workout (
  hevy_workout_id   UUID PRIMARY KEY REFERENCES raw_hevy_workout(hevy_workout_id) ON DELETE CASCADE,
  raw_id            BIGINT REFERENCES raw_hevy_workout(id),
  title             TEXT,
  description       TEXT,
  start_ts          TIMESTAMPTZ NOT NULL,
  end_ts            TIMESTAMPTZ NOT NULL,
  day               DATE GENERATED ALWAYS AS (local_date(start_ts)) STORED,
  duration_seconds  INT NOT NULL,
  total_sets        INT NOT NULL DEFAULT 0,
  total_reps        INT NOT NULL DEFAULT 0,
  -- Working sets only (set_type != 'warmup'). Sum(weight_kg * reps).
  total_volume_kg   NUMERIC(12,2) NOT NULL DEFAULT 0,
  unique_exercises  INT NOT NULL DEFAULT 0,
  whoop_workout_id  UUID,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_fact_strength_workout_day
  ON fact_strength_workout(day);
CREATE INDEX IF NOT EXISTS ix_fact_strength_workout_whoop
  ON fact_strength_workout(whoop_workout_id);

-- ---- mart_daily strength columns -------------------------------------------
-- Populated by mart_refresh.sql on each rebuild. NOT NULL DEFAULT 0 so days
-- with no Hevy session sum cleanly in correlate_metrics / get_daily_summary.
ALTER TABLE mart_daily
  ADD COLUMN IF NOT EXISTS strength_total_volume_kg  NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS strength_total_sets       INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS strength_unique_exercises INT NOT NULL DEFAULT 0;

COMMIT;

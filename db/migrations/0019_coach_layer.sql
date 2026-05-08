-- 0019_coach_layer.sql
-- Coach layer on top of PushPress: parse plaintext WOD descriptions into
-- structured movements, recommend loads from training history, and pre-create
-- Hevy routines so a session is one-tap to start.
--
-- Reuses existing tables instead of duplicating into a separate schema:
--   pr_coach.exercises          → dim_hevy_exercise (Hevy is the canonical registry)
--   pr_coach.programmed_workouts → fact_pushpress_session
--   pr_coach.programmed_movements→ fact_pushpress_part (extended below)
--   pr_coach.exercise_rep_max    → vw_exercise_rep_max view created here
--
-- New surfaces in this migration:
--   - parsed/recommendation columns on fact_pushpress_part
--   - hevy_routine_id link on fact_pushpress_session
--   - pushpress_movement_alias: auto-grown raw_text → exercise_template_id map
--   - vw_exercise_rep_max: per-(exercise, reps) max weight from fact_strength_set

BEGIN;

-- ---- programmed-movement structure + recommendations ---------------------
-- One fact_pushpress_part can prescribe MULTIPLE movements (parser splits the
-- description into per-movement rows). The fact_pushpress_part row stays as
-- the section-level container; pushpress_part_movement is one row per
-- movement extracted by the parser.
CREATE TABLE IF NOT EXISTS pushpress_part_movement (
  id                          BIGSERIAL PRIMARY KEY,
  part_uid                    TEXT NOT NULL REFERENCES fact_pushpress_part(part_uid)
                                  ON DELETE CASCADE,
  workout_uid                 TEXT NOT NULL,           -- denorm for fast joins
  class_date                  DATE NOT NULL,           -- denorm for date-range scans
  sequence                    INT NOT NULL,            -- 0-indexed order within the part
  raw_text                    TEXT NOT NULL,           -- e.g. "50 Box Step-ups @ 20\""
  exercise_template_id        TEXT REFERENCES dim_hevy_exercise(exercise_template_id),
  novel_exercise              BOOLEAN NOT NULL DEFAULT FALSE,
  analog_exercise_template_id TEXT REFERENCES dim_hevy_exercise(exercise_template_id),
  -- Prescription (what the coach programmed):
  prescribed_reps             TEXT,                    -- "21-15-9" | "5" | "AMRAP"
  prescribed_sets             INT,
  prescribed_load_kg          NUMERIC(8,3),
  prescribed_load_pct         NUMERIC(5,2),            -- % of 1RM if programmed as such
  prescribed_distance_m       NUMERIC(10,2),
  prescribed_duration_s       INT,
  -- Recommendation (what we suggest given training history + recovery):
  recommended_load_kg         NUMERIC(8,3),
  recommendation_reasoning    TEXT,
  recommendation_confidence   NUMERIC(3,2),            -- 0-1
  parser_confidence           NUMERIC(3,2),
  -- Bookkeeping:
  computed_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (part_uid, sequence)
);

CREATE INDEX IF NOT EXISTS ix_pp_part_movement_workout
  ON pushpress_part_movement(workout_uid);
CREATE INDEX IF NOT EXISTS ix_pp_part_movement_class_date
  ON pushpress_part_movement(class_date);
CREATE INDEX IF NOT EXISTS ix_pp_part_movement_template
  ON pushpress_part_movement(exercise_template_id);

-- Workout-level parser metadata. The parser writes one of these per session
-- once it succeeds, so re-runs can bail early via parsed_at.
ALTER TABLE fact_pushpress_session
  ADD COLUMN IF NOT EXISTS workout_format    TEXT,    -- amrap|rft|for_time|emom|strength|chipper|skill|mixed
  ADD COLUMN IF NOT EXISTS workout_duration_s INT,
  ADD COLUMN IF NOT EXISTS workout_rounds    INT,
  ADD COLUMN IF NOT EXISTS workout_score_type TEXT,
  ADD COLUMN IF NOT EXISTS parser_confidence  NUMERIC(3,2),
  ADD COLUMN IF NOT EXISTS parsed_at          TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS hevy_routine_id    TEXT,
  ADD COLUMN IF NOT EXISTS hevy_routine_synced_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_fact_pp_session_routine
  ON fact_pushpress_session(hevy_routine_id);
CREATE INDEX IF NOT EXISTS ix_fact_pp_session_parsed
  ON fact_pushpress_session(parsed_at);

-- ---- alias auto-learning -------------------------------------------------
-- Every successful match writes a row here. Future runs hit this table first
-- (cheap exact lookup) before falling back to the LLM-assisted matcher.
-- pattern is normalized: lower-cased, punctuation/whitespace collapsed.
CREATE TABLE IF NOT EXISTS pushpress_movement_alias (
  id                    BIGSERIAL PRIMARY KEY,
  pattern               TEXT NOT NULL UNIQUE,
  exercise_template_id  TEXT NOT NULL REFERENCES dim_hevy_exercise(exercise_template_id),
  source                TEXT NOT NULL DEFAULT 'auto', -- 'auto' | 'manual' | 'seed'
  hit_count             INT  NOT NULL DEFAULT 0,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_pp_alias_template
  ON pushpress_movement_alias(exercise_template_id);

-- ---- review queue --------------------------------------------------------
-- Movements the parser couldn't confidently match. Surface via MCP tool so
-- the user (or Claude) can confirm/override the analog mapping. Routine
-- creation does NOT block on this — we use the analog and flag for review.
CREATE TABLE IF NOT EXISTS pushpress_movement_review (
  id                          BIGSERIAL PRIMARY KEY,
  movement_id                 BIGINT NOT NULL REFERENCES pushpress_part_movement(id)
                                  ON DELETE CASCADE,
  raw_text                    TEXT NOT NULL,
  suggested_template_id       TEXT REFERENCES dim_hevy_exercise(exercise_template_id),
  suggested_title             TEXT,
  resolved_template_id        TEXT REFERENCES dim_hevy_exercise(exercise_template_id),
  resolved_at                 TIMESTAMPTZ,
  notes                       TEXT,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_pp_review_unresolved
  ON pushpress_movement_review(resolved_at) WHERE resolved_at IS NULL;

-- ---- analog ratios (for novel exercises) ---------------------------------
-- Seed dictionary mapping (analog_exercise, base_exercise) → load ratio.
-- The recommender uses this when a programmed movement doesn't have its own
-- training history yet. Manually extendable.
CREATE TABLE IF NOT EXISTS pushpress_analog_ratio (
  id                    BIGSERIAL PRIMARY KEY,
  analog_template_id    TEXT NOT NULL REFERENCES dim_hevy_exercise(exercise_template_id),
  base_template_id      TEXT NOT NULL REFERENCES dim_hevy_exercise(exercise_template_id),
  ratio                 NUMERIC(4,3) NOT NULL,         -- analog = base * ratio
  notes                 TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (analog_template_id, base_template_id)
);

-- ---- exercise rep-max view -----------------------------------------------
-- For each (exercise_template_id, rep_count), the max weight ever lifted at
-- that rep count. Used by the recommender to drive 1RM/3RM/5RM lookups.
-- Working sets only — warmups and dropsets are noise.
CREATE OR REPLACE VIEW vw_exercise_rep_max AS
SELECT
  s.exercise_template_id,
  s.exercise_title,
  s.reps                   AS rep_count,
  MAX(s.weight_kg)         AS max_weight_kg,
  MAX(s.day)               AS last_hit_day,
  COUNT(*)                 AS sample_count
FROM fact_strength_set s
WHERE s.weight_kg IS NOT NULL
  AND s.reps IS NOT NULL
  AND s.reps > 0
  AND s.set_type IN ('normal', 'failure')
GROUP BY s.exercise_template_id, s.exercise_title, s.reps;

COMMIT;

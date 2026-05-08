-- 0020_pushpress_workout_score.sql
-- Capture metcon scores (time, rounds+reps, total reps, total weight, distance)
-- alongside the structured workout. Hevy logs atomic sets; the SCORE is workout-
-- level metcon semantics that Hevy can't represent natively, so we own it here.
--
-- Score model: (workout_uid, score_type, score_value_seconds, score_value_int,
-- score_value_text). Use the typed slot that matches score_type:
--   'time'         → score_value_seconds (single number, e.g. "8:42" → 522)
--   'total_reps'   → score_value_int     (e.g. 250)
--   'total_weight' → score_value_seconds repurposed? no, use a separate float
--   'rounds_reps'  → score_value_text    (e.g. "5+12" = 5 rounds + 12 extra reps)
--   'distance'     → score_value_int     (meters)
-- One row per (workout_uid, division). Most users compete in one division per
-- session; the table allows multiple in case the user records both their RX
-- and scaled effort, or a Performance + Fitness experiment.

BEGIN;

CREATE TABLE IF NOT EXISTS pushpress_workout_score (
  id                    BIGSERIAL PRIMARY KEY,
  workout_uid           TEXT NOT NULL REFERENCES fact_pushpress_session(workout_uid)
                            ON DELETE CASCADE,
  class_date            DATE NOT NULL,            -- denorm for date-range scans
  division              TEXT,                     -- 'Performance' | 'Fitness' | 'RX' | NULL
  score_type            TEXT NOT NULL,            -- 'time' | 'rounds_reps' | 'total_reps' | 'total_weight' | 'distance' | 'none'
  score_value_seconds   INT,                      -- for 'time' — total seconds
  score_value_int       INT,                      -- for 'total_reps' / 'distance' — count or meters
  score_value_kg        NUMERIC(8,3),             -- for 'total_weight' — kg
  score_value_text      TEXT,                     -- for 'rounds_reps' — '5+12' format
  rx                    BOOLEAN,                  -- true if hit prescribed loads/reps cleanly
  notes                 TEXT,                     -- free-form: cap reached, scaling notes
  source                TEXT NOT NULL DEFAULT 'manual',  -- 'manual' | 'hevy_marker' | 'lifelog'
  recorded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (workout_uid, division)
);

CREATE INDEX IF NOT EXISTS ix_pp_score_class_date
  ON pushpress_workout_score(class_date);
CREATE INDEX IF NOT EXISTS ix_pp_score_workout
  ON pushpress_workout_score(workout_uid);

COMMIT;

-- 0027_whoop_lift_set.sql
-- Exact per-set Whoop Strength Trainer detail (reps x weight per set), sourced
-- from /core-details-bff/v1/cardio-details?activityId={id} (the per-workout
-- breakdown), enumerated from fact_workout's strength activities. Complements
-- the per-workout aggregates already in fact_whoop_lift_workout.

CREATE TABLE IF NOT EXISTS fact_whoop_lift_set (
  id            BIGSERIAL PRIMARY KEY,
  activity_id   TEXT NOT NULL REFERENCES fact_whoop_lift_workout(activity_id) ON DELETE CASCADE,
  day           DATE NOT NULL,
  exercise_id   TEXT NOT NULL,
  exercise_name TEXT,
  set_index     INT NOT NULL,          -- 1-based order of this set within the exercise
  volume_type   TEXT,                  -- REPS | TIME
  reps          INT,                   -- null for time-based sets
  time_seconds  INT,                   -- null for rep-based sets
  weight_lb     NUMERIC(8,2),          -- native units (Whoop reports lbs); 0 = bodyweight
  weight_kg     NUMERIC(8,2),          -- normalized
  avg_hr        INT,
  is_pr         BOOLEAN NOT NULL DEFAULT FALSE,
  raw_id        BIGINT REFERENCES raw_whoop_lift(id),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (activity_id, exercise_id, set_index)
);
CREATE INDEX IF NOT EXISTS ix_whoop_lift_set_day        ON fact_whoop_lift_set (day);
CREATE INDEX IF NOT EXISTS ix_whoop_lift_set_exercise   ON fact_whoop_lift_set (exercise_id, day);
CREATE INDEX IF NOT EXISTS ix_whoop_lift_set_activity   ON fact_whoop_lift_set (activity_id);

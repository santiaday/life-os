-- 0031_strength_exercise_norm.sql
-- Cross-era progression/PR queries were fragmenting the SAME lift into two
-- because Hevy and Whoop name exercises differently:
--   Hevy:  "Deadlift (Barbell)"   Whoop: "Deadlift - Barbell"
-- vw_strength_set matched on raw exercise_name, so a deadlift PR showed up as
-- two rows (one per era) and progression drew two separate lines. Add an
-- exercise_norm key that canonicalizes the equipment-suffix conventions
-- ("(Barbell)" / " - Barbell") to a single token string, so the read tools can
-- GROUP BY a stable key that spans both sources. Lowercase, strip parens,
-- collapse any run of hyphens/whitespace to one space, trim.
--   "Deadlift (Barbell)"  -> "deadlift barbell"
--   "Deadlift - Barbell"  -> "deadlift barbell"   (now unified)
--   "Pull-Up"             -> "pull up"
--   "Push Up"             -> "push up"

CREATE OR REPLACE VIEW vw_strength_set AS
WITH unified AS (
  SELECT
    activity_id,
    day,
    exercise_id,
    exercise_name,
    set_index,
    volume_type,
    reps,
    time_seconds,
    weight_lb,
    weight_kg,
    avg_hr,
    is_pr,
    'whoop'::text AS source
  FROM fact_whoop_lift_set
  UNION ALL
  SELECT
    hevy_workout_id::text                        AS activity_id,
    day,
    exercise_template_id                         AS exercise_id,
    exercise_title                               AS exercise_name,
    set_index,
    'REPS'::text                                 AS volume_type,
    reps,
    duration_seconds                             AS time_seconds,
    ROUND((weight_kg / 0.45359237)::numeric, 2)  AS weight_lb,
    weight_kg,
    NULL::int                                    AS avg_hr,
    FALSE                                        AS is_pr,
    'hevy'::text                                 AS source
  FROM fact_strength_set
  WHERE set_type IS NULL OR set_type <> 'warmup'
)
SELECT
  u.*,
  btrim(
    regexp_replace(
      regexp_replace(lower(COALESCE(exercise_name, '')), '[()]', '', 'g'),
      '[-\s]+', ' ', 'g'
    )
  ) AS exercise_norm
FROM unified u;

GRANT SELECT ON vw_strength_set TO lifeos_mcp;

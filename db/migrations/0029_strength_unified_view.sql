-- 0029_strength_unified_view.sql
-- Unify the strength training record across sources so progression/PR queries
-- span the FULL history. The warehouse holds two eras: Hevy (fact_strength_set,
-- through ~2026-06-04) and Whoop Strength Trainer (fact_whoop_lift_set, after).
-- Neither alone is a complete training log; this view stitches them, normalizing
-- weight to both lb (the user's unit) and kg, and tagging the source.

CREATE OR REPLACE VIEW vw_strength_set AS
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
  WHERE set_type IS NULL OR set_type <> 'warmup';

GRANT SELECT ON vw_strength_set TO lifeos_mcp;

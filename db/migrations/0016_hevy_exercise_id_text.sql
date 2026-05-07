-- 0016_hevy_exercise_id_text.sql
-- Hevy's exercise_template_id is actually an 8-char hex string (e.g.
-- '3BC06AD3'), not a UUID — confirmed against live /v1/exercise_templates
-- and the POST /v1/workouts request schema example ('D04AC939'). The Hevy
-- OpenAPI doc shows UUID-shaped examples in places, but they're stale
-- placeholders.
--
-- 0015 used UUID for these columns; INSERTs from the live API would fail
-- on the type cast. Convert to TEXT before any data lands. Safe to run on
-- empty tables (verified 0 rows on 2026-05-07 before applying).

BEGIN;

-- fact_strength_set has the FK to dim_hevy_exercise; drop+recreate it so
-- both ends move together.
ALTER TABLE fact_strength_set
  DROP CONSTRAINT IF EXISTS fact_strength_set_exercise_template_id_fkey;

ALTER TABLE dim_hevy_exercise
  ALTER COLUMN exercise_template_id TYPE TEXT USING exercise_template_id::text;

ALTER TABLE fact_strength_set
  ALTER COLUMN exercise_template_id TYPE TEXT USING exercise_template_id::text;

ALTER TABLE fact_strength_set
  ADD CONSTRAINT fact_strength_set_exercise_template_id_fkey
  FOREIGN KEY (exercise_template_id)
  REFERENCES dim_hevy_exercise(exercise_template_id);

COMMIT;

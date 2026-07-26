-- 0041_source_registry_corrections.sql
--
-- Corrections to the registry after the first live freshness run.
--
-- The point of `mode` is to separate "this should be flowing and isn't" from
-- "this ended, on purpose". Getting it wrong in either direction is costly:
-- a dead sync that reads as historical never gets fixed, and a finished import
-- that reads as live cries wolf every day forever.

BEGIN;

-- PushPress was never projected into fact_activity. Its tables hold the gym's
-- programmed workouts (what the class was), not attendance (whether he went),
-- so claiming activity coverage for it would be wrong. Drop the row rather
-- than leave a permanently empty stream in the freshness table.
DELETE FROM source_health WHERE source_key = 'pushpress.activity';

-- Lose It splits: food logging stopped 2025-09-05, but weigh-ins have
-- continued and are currently the freshest weight source in the warehouse.
-- One source, two streams with genuinely different lifecycles.
UPDATE source_health SET
  mode = 'historical',
  expected_lag_hours = NULL,
  coverage_end = '2025-09-05',
  notes = 'Food logging stopped 2025-09-05. Weigh-ins continue -- see loseit.body.',
  updated_at = now()
WHERE source_key = 'loseit.nutrition';

INSERT INTO source_health
  (source_key, domain, display_name, mode, expected_lag_hours,
   table_name, day_column, source_filter, coverage_start, notes)
VALUES
  ('loseit.weight', 'body', 'Lose It weigh-ins', 'live', 72,
   'fact_body_composition', 'day', 'loseit', '2023-10-03',
   'Still actively used. Kept current by the 2-hourly export sync; needs '
   'LOSEIT_SESSION_COOKIE to be set.')
ON CONFLICT (source_key) DO UPDATE
  SET mode = EXCLUDED.mode,
      expected_lag_hours = EXCLUDED.expected_lag_hours,
      notes = EXCLUDED.notes,
      updated_at = now();

-- The old historical row for Lose It weights is now redundant with the live one.
DELETE FROM source_health WHERE source_key = 'loseit.body';

-- Apple Health reaches the warehouse only through Cronometer's biometric
-- passthrough. It stopped advancing on 2026-05-30, at the same time as the
-- Cronometer food log -- one broken link, two stale streams. Record the
-- dependency so the next person doesn't debug them separately.
UPDATE source_health SET
  notes = 'Arrives only via Cronometer''s biometric passthrough. If '
          'cronometer.nutrition is stale, this will be too -- they share a link.',
  updated_at = now()
WHERE source_key IN ('apple_health.body', 'apple_health.metric');

UPDATE source_health SET
  notes = 'Sync is healthy and authenticating; the gap is days with nothing '
          'logged, not a pipeline failure. Verified 2026-07-26 by walking the '
          'mobile API day by day.',
  updated_at = now()
WHERE source_key = 'cronometer.nutrition';

-- Zero's fasting log ended when the app stopped being used.
UPDATE source_health SET
  coverage_end = '2025-06-30',
  notes = 'Zero is no longer in use. 136 fasts, Jan 2024 - Jun 2025.',
  updated_at = now()
WHERE source_key = 'zero.fast';

COMMIT;

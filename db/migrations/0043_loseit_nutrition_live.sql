-- 0043_loseit_nutrition_live.sql
--
-- 0041 marked loseit.nutrition `historical` with coverage_end 2025-09-05,
-- because food logging had stopped there while weigh-ins continued. That was
-- true as a description of the past and wrong as a configuration: `historical`
-- means "a finished import that can never go stale", so if logging resumes the
-- row would keep reporting historical and never flag a lapse.
--
-- Lose It is a live source that currently has nothing recent in it — exactly
-- the shape cronometer.nutrition already has. Same treatment: live, with a
-- note saying the gap is behavioural rather than a broken pipe, so nobody
-- re-diagnoses it as an outage.

BEGIN;

UPDATE source_health SET
  mode = 'live',
  expected_lag_hours = 48,
  coverage_end = NULL,
  notes = 'Sync is healthy and self-renewing (see ingest_loseit.auth). Food '
          'logging paused 2025-09-05; if it resumes, entries appear within '
          'the 2-hourly window with no further setup. A stale reading here '
          'means nothing was logged, not that the pipeline broke.',
  updated_at = now()
WHERE source_key = 'loseit.nutrition';

COMMIT;

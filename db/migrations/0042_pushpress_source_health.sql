-- 0042_pushpress_source_health.sql
--
-- PushPress went six weeks stale without anything noticing, because it wasn't
-- registered anywhere that gets checked. Migration 0041 deliberately DELETED
-- its source_health row on the reasoning that PushPress rows are the gym's
-- programming rather than attendance, so counting them as activity coverage
-- would be wrong.
--
-- That reasoning was right about `fact_activity` and wrong about monitoring.
-- Not-attendance is a reason to keep it out of the activity domain, not a
-- reason to stop watching whether it is flowing. Registered here under a
-- `programming` domain of its own: it never contributes to activity coverage,
-- and it does show up in get_data_freshness.
--
-- SLA is generous (36h) because the job runs twice daily and the gym publishes
-- ahead — a few hours late is not a problem, six weeks is.

BEGIN;

INSERT INTO source_health
  (source_key, domain, display_name, mode, expected_lag_hours,
   table_name, day_column, source_filter, coverage_start, notes)
VALUES
  ('pushpress.programming', 'programming', 'PushPress programmed workouts',
   'live', 36, 'fact_pushpress_session', 'class_date', NULL, '2026-04-30',
   'The gym''s PUBLISHED programming, not attendance — never present it as '
   'training that was performed. class_date runs into the FUTURE because '
   'PushPress publishes ahead, so a healthy lag here is negative.')
ON CONFLICT (source_key) DO UPDATE
  SET domain = EXCLUDED.domain,
      display_name = EXCLUDED.display_name,
      mode = EXCLUDED.mode,
      expected_lag_hours = EXCLUDED.expected_lag_hours,
      table_name = EXCLUDED.table_name,
      day_column = EXCLUDED.day_column,
      source_filter = EXCLUDED.source_filter,
      coverage_start = EXCLUDED.coverage_start,
      notes = EXCLUDED.notes,
      updated_at = now();

COMMIT;

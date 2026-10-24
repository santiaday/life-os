-- 0071_withings_direct.sql
--
-- Withings weigh-ins straight from the Withings cloud via edge function
-- withings-ingest (Withings notifies it seconds after each weigh-in), instead of
-- waiting for the Withings app to be opened and push into Apple Health.
-- dim_source_priority already ranks withings (80) above apple_watch (64) for
-- body, so vw_body_daily prefers the direct reading when both exist.

INSERT INTO source_health (source_key, domain, display_name, mode, expected_lag_hours,
                           table_name, day_column, source_filter, coverage_start, notes)
VALUES ('withings.body', 'body', 'Withings scale (direct API)', 'live', 168,
        'fact_body_composition', 'day', 'withings', current_date,
        'Edge function withings-ingest: Withings notification per weigh-in, plus an hourly pg_cron poll. Back-filled 90 days on connect.')
ON CONFLICT (source_key) DO UPDATE SET mode = 'live', table_name = EXCLUDED.table_name,
  source_filter = EXCLUDED.source_filter, notes = EXCLUDED.notes, updated_at = now();

-- One-time state for the connect link (valid one hour; regenerate the same way).
INSERT INTO ingest_cursor (name, cursor, updated_at)
VALUES ('withings.connect_state', encode(extensions.gen_random_bytes(16), 'hex'), now())
ON CONFLICT (name) DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now();

UPDATE ai_guide SET body = replace(body,
  '  hevy          edge function hevy-sync',
  '  withings      edge function withings-ingest: every Withings weigh-in -> fact_body_composition (source ''withings''), seconds after it happens. Preferred over the same reading echoed through Apple Health.
  hevy          edge function hevy-sync')
WHERE topic = 'sources' AND body NOT LIKE '%withings-ingest%';

UPDATE ai_guide SET body = replace(body,
  '  :50        ingest_watchdog_run()',
  '  :22        withings-ingest /sync (safety net for missed Withings notifications)
  :50        ingest_watchdog_run()')
WHERE topic = 'jobs' AND body NOT LIKE '%withings%';

-- Applied separately once the account was connected (2026-10-24), since the poll needs tokens:
SELECT cron.schedule('withings-sync', '22 * * * *', $$SELECT public.call_edge_function('withings-ingest', '/sync')$$);

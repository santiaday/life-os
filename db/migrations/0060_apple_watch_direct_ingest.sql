-- Apple Watch via Health Auto Export -> Supabase Edge Function `apple-health-ingest`.
-- Distinct from 'apple_health' (Cronometer passthrough). HRV lands as hrv_sdnn,
-- never as 'hrv' (Whoop RMSSD) -- the two are different statistics.
--
-- Back-filled into the repo from supabase_migrations; applied 2026-10-20. The
-- token hash is redacted here (the repo is public): set it with
--   UPDATE ingest_token SET token_sha256 = encode(extensions.digest('<token>', 'sha256'), 'hex')
--    WHERE name = 'health_auto_export';
CREATE TABLE IF NOT EXISTS ingest_token (
  name        TEXT PRIMARY KEY,
  token_sha256 TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ
);
ALTER TABLE ingest_token ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ingest_token FROM anon, authenticated;

INSERT INTO ingest_token (name, token_sha256)
VALUES ('health_auto_export', '<redacted>')
ON CONFLICT (name) DO NOTHING;

INSERT INTO dim_source_priority (domain, source, priority, notes) VALUES
  ('metric',  'apple_watch', 88, 'Apple Watch via Health Auto Export (direct). HRV stored as hrv_sdnn.'),
  ('sleep',   'apple_watch', 88, 'Apple Watch sleep stages via Health Auto Export'),
  ('body',    'apple_watch', 64, 'Apple Health weight/body fat via Health Auto Export'),
  ('activity','apple_watch', 75, 'Apple Watch workouts via Health Auto Export; HR, kcal, distance')
ON CONFLICT (domain, source) DO UPDATE SET priority = EXCLUDED.priority, notes = EXCLUDED.notes, updated_at = now();

INSERT INTO source_health (source_key, domain, display_name, mode, expected_lag_hours,
  table_name, day_column, source_filter, coverage_start, coverage_end, notes) VALUES
  ('apple_watch.metric',  'metric',  'Apple Watch daily metrics (Health Auto Export)', 'live', 36,
   'fact_daily_metric', 'day', 'apple_watch', '2026-10-23', NULL, 'Pushed by Health Auto Export -> edge function apple-health-ingest. Stale = phone automation stopped.'),
  ('apple_watch.sleep',   'sleep',   'Apple Watch sleep (Health Auto Export)', 'live', 36,
   'fact_sleep_session', 'day', 'apple_watch', '2026-10-23', NULL, 'day = local date of wake.'),
  ('apple_watch.body',    'body',    'Apple Health weight (Health Auto Export)', 'live', 168,
   'fact_body_composition', 'day', 'apple_watch', '2026-10-23', NULL, NULL),
  ('apple_watch.activity','activity','Apple Watch workouts (Health Auto Export)', 'live', 72,
   'fact_activity', 'day', 'apple_watch', '2026-10-23', NULL, 'No per-set loads; Apple does not record them.')
ON CONFLICT (source_key) DO UPDATE SET display_name = EXCLUDED.display_name, expected_lag_hours = EXCLUDED.expected_lag_hours,
  notes = EXCLUDED.notes, updated_at = now();

CREATE OR REPLACE FUNCTION check_ingest_token(p_name TEXT, p_token TEXT) RETURNS BOOLEAN
LANGUAGE sql SECURITY DEFINER SET search_path = public, extensions AS $$
  UPDATE ingest_token SET last_used_at = now()
   WHERE name = p_name AND token_sha256 = encode(extensions.digest(p_token, 'sha256'), 'hex')
  RETURNING true;
$$;
REVOKE EXECUTE ON FUNCTION check_ingest_token(TEXT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION check_ingest_token(TEXT, TEXT) TO service_role;

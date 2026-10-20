-- 0060_apple_watch_direct_ingest.sql  (applied to dqnrcarouldholuqfxbb 2026-10-20)
-- Apple Watch -> Supabase Edge Function `apple-health-ingest` (supabase/functions/).
-- Distinct from 'apple_health' (Cronometer passthrough). Apple HRV is SDNN and
-- lands as metric 'hrv_sdnn'; RMSSD computed on-device lands as 'hrv'.
CREATE TABLE IF NOT EXISTS ingest_token (
  name         TEXT PRIMARY KEY,
  token_sha256 TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ
);
ALTER TABLE ingest_token ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ingest_token FROM anon, authenticated;
-- token row inserted out-of-band (hash only; plaintext never stored)

INSERT INTO dim_source_priority (domain, source, priority, notes) VALUES
  ('metric',  'apple_watch', 88, 'Apple Watch direct. HRV stored as hrv_sdnn; on-device RMSSD as hrv.'),
  ('sleep',   'apple_watch', 88, 'Apple Watch sleep stages'),
  ('body',    'apple_watch', 64, 'Apple Health weight/body fat'),
  ('activity','apple_watch', 75, 'Apple Watch workouts; HR, kcal, distance')
ON CONFLICT (domain, source) DO UPDATE SET priority = EXCLUDED.priority, notes = EXCLUDED.notes, updated_at = now();

INSERT INTO source_health (source_key, domain, display_name, mode, expected_lag_hours,
  table_name, day_column, source_filter, coverage_start, coverage_end, notes) VALUES
  ('apple_watch.metric',  'metric',  'Apple Watch daily metrics', 'live', 36, 'fact_daily_metric', 'day', 'apple_watch', '2026-10-23', NULL, 'Pushed by phone -> edge fn apple-health-ingest.'),
  ('apple_watch.sleep',   'sleep',   'Apple Watch sleep', 'live', 36, 'fact_sleep_session', 'day', 'apple_watch', '2026-10-23', NULL, 'day = local date of wake.'),
  ('apple_watch.body',    'body',    'Apple Health weight', 'live', 168, 'fact_body_composition', 'day', 'apple_watch', '2026-10-23', NULL, NULL),
  ('apple_watch.activity','activity','Apple Watch workouts', 'live', 72, 'fact_activity', 'day', 'apple_watch', '2026-10-23', NULL, 'No per-set loads.')
ON CONFLICT (source_key) DO UPDATE SET display_name = EXCLUDED.display_name, expected_lag_hours = EXCLUDED.expected_lag_hours, notes = EXCLUDED.notes, updated_at = now();

CREATE OR REPLACE FUNCTION check_ingest_token(p_name TEXT, p_token TEXT) RETURNS BOOLEAN
LANGUAGE sql SECURITY DEFINER SET search_path = public, extensions AS $$
  UPDATE ingest_token SET last_used_at = now()
   WHERE name = p_name AND token_sha256 = encode(extensions.digest(p_token, 'sha256'), 'hex')
  RETURNING true;
$$;
REVOKE EXECUTE ON FUNCTION check_ingest_token(TEXT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION check_ingest_token(TEXT, TEXT) TO service_role;

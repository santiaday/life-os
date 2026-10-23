-- Hevy moves off the droplet: edge function `hevy-sync` on a pg_cron schedule.
--
-- Back-filled into the repo from supabase_migrations; applied 2026-10-20.
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net WITH SCHEMA extensions;

-- Incremental cursors for pull-based syncs.
CREATE TABLE IF NOT EXISTS ingest_cursor (
  name        TEXT PRIMARY KEY,
  cursor      TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE ingest_cursor ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ingest_cursor FROM anon, authenticated;

-- Canonical exercise vocabulary from unify/taxonomy.py, so SQL/edge code can
-- resolve vendor labels without the Python package. Loaded separately.
CREATE TABLE IF NOT EXISTS dim_exercise_vocab (
  alias         TEXT PRIMARY KEY,   -- normalized slug
  exercise_key  TEXT NOT NULL
);
ALTER TABLE dim_exercise_vocab ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON dim_exercise_vocab FROM anon, authenticated;
GRANT SELECT ON dim_exercise_vocab TO lifeos_mcp;

INSERT INTO dim_source_priority (domain, source, priority, notes) VALUES
  ('activity','hevy', 90, 'per-set, logged in Hevy (Watch or phone)')
ON CONFLICT (domain, source) DO UPDATE SET priority = EXCLUDED.priority, notes = EXCLUDED.notes, updated_at = now();

UPDATE source_health
   SET mode = 'live', expected_lag_hours = 72, coverage_end = NULL,
       notes = 'Live again from 2026-10-20 via edge fn hevy-sync (pg_cron every 30 min). Per-set loads.'
 WHERE source_key = 'hevy.activity';

-- When a higher-priority source recorded the same session (e.g. Hevy sets),
-- the Apple Watch copy stops being primary and donates HR/kcal to it.
-- Lightweight stand-in for unify.dedupe for the apple_watch source only.
CREATE OR REPLACE FUNCTION merge_apple_watch_overlaps(p_since DATE DEFAULT current_date - 14)
RETURNS INTEGER LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE n INTEGER := 0; r RECORD;
BEGIN
  FOR r IN
    SELECT aw.activity_uid AS aw_uid, o.activity_uid AS o_uid,
           aw.avg_hr, aw.max_hr, aw.min_hr, aw.kcal
      FROM fact_activity aw
      JOIN fact_activity o
        ON o.source <> 'apple_watch' AND o.is_primary
       AND o.start_ts < coalesce(aw.end_ts, aw.start_ts + make_interval(secs => coalesce(aw.duration_s, 0)))
       AND aw.start_ts < coalesce(o.end_ts, o.start_ts + make_interval(secs => coalesce(o.duration_s, 0)))
      LEFT JOIN dim_source_priority pa ON pa.domain = 'activity' AND pa.source = 'apple_watch'
      LEFT JOIN dim_source_priority po ON po.domain = 'activity' AND po.source = o.source
     WHERE aw.source = 'apple_watch' AND aw.day >= p_since
       AND coalesce(po.priority, 0) > coalesce(pa.priority, 0)
       AND extract(epoch FROM (least(aw.end_ts, o.end_ts) - greatest(aw.start_ts, o.start_ts)))
           >= 0.5 * least(extract(epoch FROM aw.end_ts - aw.start_ts), extract(epoch FROM o.end_ts - o.start_ts))
  LOOP
    UPDATE fact_activity SET is_primary = false, updated_at = now() WHERE activity_uid = r.aw_uid AND is_primary;
    UPDATE fact_activity SET
      avg_hr = coalesce(avg_hr, r.avg_hr), max_hr = coalesce(max_hr, r.max_hr),
      min_hr = coalesce(min_hr, r.min_hr), kcal = coalesce(kcal, r.kcal),
      payload = payload || jsonb_build_object('merged_from', 'apple_watch'), updated_at = now()
     WHERE activity_uid = r.o_uid;
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;
REVOKE EXECUTE ON FUNCTION merge_apple_watch_overlaps(DATE) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION merge_apple_watch_overlaps(DATE) TO service_role;

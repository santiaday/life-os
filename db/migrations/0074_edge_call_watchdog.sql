-- 0074_edge_call_watchdog.sql
--
-- pg_cron marks a job "succeeded" as soon as call_edge_function() queues the
-- HTTP request, whatever the edge function answers. hevy-sync returned 503
-- (HEVY_API_KEY not set) on every call from 2026-10-20 and nothing noticed.
--
--   edge_call             one row per scheduled edge-function call, with the HTTP
--                         status copied from net._http_response (pg_net keeps
--                         responses ~6 hours; this keeps them for good)
--   edge_call_collect()   copies statuses in; ingest_watchdog_run() calls it first
--   ingest_check 'edge.<function>'  stale when the function hasn't answered 2xx
--                         within max_age; the alert detail carries the last error

CREATE TABLE IF NOT EXISTS edge_call (
  request_id  bigint PRIMARY KEY,           -- net.http_post id = net._http_response.id
  fn          text NOT NULL,
  query       text,
  called_at   timestamptz NOT NULL DEFAULT now(),
  status_code int,
  error       text,                         -- body or pg_net error when not 2xx
  checked_at  timestamptz                   -- when the response was copied in
);
CREATE INDEX IF NOT EXISTS edge_call_fn_called_idx ON edge_call (fn, called_at DESC);
ALTER TABLE edge_call ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON edge_call FROM anon, authenticated;
COMMENT ON TABLE edge_call IS
  'Every call_edge_function() call (pg_cron -> edge function) and the HTTP status it got. status_code NULL with checked_at NULL = no response yet. Use it to see whether a scheduled sync is actually working: cron.job_run_details only says the request was queued.';

CREATE OR REPLACE FUNCTION call_edge_function(p_name text, p_query text DEFAULT '')
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE rid bigint;
BEGIN
  rid := net.http_post(
    url := 'https://dqnrcarouldholuqfxbb.supabase.co/functions/v1/' || p_name || p_query,
    headers := jsonb_build_object('content-type', 'application/json',
               'x-cron-key', (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'cron_key')),
    body := '{}'::jsonb,
    timeout_milliseconds := 150000
  );
  INSERT INTO edge_call (request_id, fn, query) VALUES (rid, p_name, nullif(p_query, ''));
  RETURN rid;
END
$$;
REVOKE EXECUTE ON FUNCTION call_edge_function(text, text) FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE FUNCTION edge_call_collect()
RETURNS int LANGUAGE sql SECURITY DEFINER SET search_path = public, net AS $$
  WITH upd AS (
    UPDATE edge_call e
       SET status_code = r.status_code,
           error = CASE WHEN r.status_code BETWEEN 200 AND 299 THEN NULL
                        ELSE left(coalesce(r.error_msg, r.content::text, 'no response body'), 500) END,
           checked_at = now()
      FROM net._http_response r
     WHERE r.id = e.request_id AND e.checked_at IS NULL
       AND (r.status_code IS NOT NULL OR r.error_msg IS NOT NULL OR r.timed_out)
    RETURNING 1)
  SELECT count(*)::int FROM upd
$$;
REVOKE EXECUTE ON FUNCTION edge_call_collect() FROM PUBLIC, anon, authenticated;

INSERT INTO ingest_check (check_key, description, max_age, active_from) VALUES
  ('edge.hevy-sync', 'pg_cron -> edge function hevy-sync (:07 :37). Newest call that answered 2xx (edge_call).', interval '3 hours', now()),
  ('edge.cronometer-sync', 'pg_cron -> edge function cronometer-sync (every 15 min). Newest call that answered 2xx (edge_call).', interval '2 hours', now()),
  ('edge.withings-ingest', 'pg_cron -> edge function withings-ingest /sync (:22). Newest call that answered 2xx (edge_call).', interval '3 hours', now())
ON CONFLICT (check_key) DO NOTHING;

CREATE OR REPLACE FUNCTION ingest_check_last_seen(p_check_key text)
RETURNS timestamptz LANGUAGE sql STABLE AS $$
  SELECT CASE
    WHEN p_check_key = 'apple_health.hae_push' THEN greatest(
      (SELECT max(imported_at) FROM raw_import WHERE source = 'apple_watch' AND entity = 'hae_batch'),
      (SELECT max(updated_at) FROM fact_daily_metric WHERE source = 'apple_watch'))
    WHEN p_check_key = 'apple_health.mirror_push' THEN greatest(
      (SELECT max(synced_at) FROM hk_sample),
      (SELECT max(synced_at) FROM hk_hourly),
      (SELECT max(synced_at) FROM hk_object))
    WHEN p_check_key = 'apple_health.sleep' THEN
      (SELECT max(end_ts) FROM fact_sleep_session WHERE source = 'apple_watch')
    WHEN p_check_key LIKE 'edge.%' THEN
      (SELECT max(called_at) FROM edge_call
        WHERE fn = substr(p_check_key, 6) AND status_code BETWEEN 200 AND 299)
  END
$$;

CREATE OR REPLACE FUNCTION ingest_watchdog_run()
RETURNS TABLE (check_key text, last_seen_at timestamptz, stale boolean, action text)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE
  c        ingest_check%ROWTYPE;
  seen     timestamptz;
  is_stale boolean;
  act      text;
  last_err text;
BEGIN
  PERFORM edge_call_collect();
  FOR c IN SELECT * FROM ingest_check WHERE enabled ORDER BY ingest_check.check_key LOOP
    seen := ingest_check_last_seen(c.check_key);
    is_stale := now() >= c.active_from
                AND (seen IS NULL OR seen < now() - c.max_age);
    act := 'ok';

    IF is_stale THEN
      last_err := CASE WHEN c.check_key LIKE 'edge.%' THEN
        (SELECT format(' Last failure %s: HTTP %s %s', date_trunc('minute', e.called_at), coalesce(e.status_code::text, '-'), e.error)
           FROM edge_call e WHERE e.fn = substr(c.check_key, 6) AND e.error IS NOT NULL
          ORDER BY e.called_at DESC LIMIT 1) END;
      INSERT INTO ingest_alert (check_key, last_seen_at, max_age, detail)
      VALUES (c.check_key, seen, c.max_age,
              CASE WHEN seen IS NULL
                   THEN format('%s: no data ever. Threshold %s.', c.check_key, c.max_age)
                   ELSE format('%s: newest data %s (%s ago). Threshold %s.',
                               c.check_key, seen, date_trunc('minute', now() - seen), c.max_age)
              END || coalesce(last_err, ''))
      ON CONFLICT (check_key) WHERE resolved_at IS NULL DO NOTHING;
      act := CASE WHEN FOUND THEN 'opened' ELSE 'still_open' END;
    ELSE
      UPDATE ingest_alert a SET resolved_at = now()
       WHERE a.check_key = c.check_key AND a.resolved_at IS NULL;
      IF FOUND THEN act := 'resolved'; END IF;
    END IF;

    UPDATE ingest_check SET last_run_at = now(), last_seen_at = seen
     WHERE ingest_check.check_key = c.check_key;

    check_key := c.check_key; last_seen_at := seen; stale := is_stale; action := act;
    RETURN NEXT;
  END LOOP;
END
$$;

COMMENT ON VIEW v_ingest_alert_open IS
  'Open ingest alerts. Empty = every Apple Health push and every scheduled sync (Hevy, Cronometer, Withings) is current. Check this before trusting recent data.';

UPDATE ai_guide SET body = replace(body, 'empty = every Apple Health push is current',
                                         'empty = every Apple Health push and scheduled sync is current')
 WHERE topic = 'start_here';

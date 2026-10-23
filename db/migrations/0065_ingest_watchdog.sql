-- 0065_ingest_watchdog.sql
--
-- Hourly check that the Apple Health pushes are still arriving. Nothing is
-- sent anywhere: a stale check opens a row in ingest_alert, and Claude reads
-- v_ingest_alert_open at the start of a session (see the table comments).
--
--   ingest_check   one row per check: what "stale" means for it
--   ingest_alert   one row per incident: opened when a check goes stale,
--                  resolved when it recovers. Never more than one open row
--                  per check, so a dead pipeline doesn't log every hour.
--   ingest_watchdog_run()  evaluates every enabled check; pg_cron runs it
--                  hourly at :50.
--
-- Thresholds come from the observed push cadence, not the ideal one: Health
-- Auto Export and Tributary push from iOS background automations, and gaps of
-- 19-29h between pushes were normal in the first week (2026-10-20..23).

CREATE TABLE IF NOT EXISTS ingest_check (
    check_key    text PRIMARY KEY,
    description  text NOT NULL,
    max_age      interval NOT NULL,
    -- No alert before this time: covers a check whose data can't exist yet
    -- (sleep before the first night on the watch).
    active_from  timestamptz NOT NULL DEFAULT now(),
    enabled      boolean NOT NULL DEFAULT true,
    last_run_at  timestamptz,
    last_seen_at timestamptz,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingest_alert (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    check_key    text NOT NULL REFERENCES ingest_check (check_key),
    opened_at    timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz,
    last_seen_at timestamptz,          -- newest data when the alert opened (NULL = never any)
    max_age      interval NOT NULL,    -- threshold in force when it opened
    detail       text NOT NULL,
    acknowledged_at timestamptz,
    note         text
);

CREATE UNIQUE INDEX IF NOT EXISTS ingest_alert_one_open
    ON ingest_alert (check_key) WHERE resolved_at IS NULL;

INSERT INTO ingest_check (check_key, description, max_age, active_from) VALUES
  ('apple_health.hae_push',
   'Health Auto Export -> edge function apple-health-ingest. Newest of raw_import (hae_batch) and fact_daily_metric (source apple_watch) write times.',
   interval '36 hours', now()),
  ('apple_health.mirror_push',
   'Tributary -> edge function health-mirror. Newest synced_at across hk_sample, hk_hourly, hk_object.',
   interval '36 hours', now()),
  ('apple_health.sleep',
   'Apple Watch sleep landing in fact_sleep_session (source apple_watch). Newest end_ts. Catches pushes that arrive without sleep in them.',
   interval '36 hours', timestamptz '2026-10-24 18:00+00')
ON CONFLICT (check_key) DO NOTHING;

CREATE OR REPLACE FUNCTION ingest_check_last_seen(p_check_key text)
RETURNS timestamptz
LANGUAGE sql STABLE AS $$
  SELECT CASE p_check_key
    WHEN 'apple_health.hae_push' THEN greatest(
      (SELECT max(imported_at) FROM raw_import WHERE source = 'apple_watch' AND entity = 'hae_batch'),
      (SELECT max(updated_at) FROM fact_daily_metric WHERE source = 'apple_watch'))
    WHEN 'apple_health.mirror_push' THEN greatest(
      (SELECT max(synced_at) FROM hk_sample),
      (SELECT max(synced_at) FROM hk_hourly),
      (SELECT max(synced_at) FROM hk_object))
    WHEN 'apple_health.sleep' THEN
      (SELECT max(end_ts) FROM fact_sleep_session WHERE source = 'apple_watch')
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
BEGIN
  FOR c IN SELECT * FROM ingest_check WHERE enabled ORDER BY ingest_check.check_key LOOP
    seen := ingest_check_last_seen(c.check_key);
    is_stale := now() >= c.active_from
                AND (seen IS NULL OR seen < now() - c.max_age);
    act := 'ok';

    IF is_stale THEN
      INSERT INTO ingest_alert (check_key, last_seen_at, max_age, detail)
      VALUES (c.check_key, seen, c.max_age,
              CASE WHEN seen IS NULL
                   THEN format('%s: no data ever. Threshold %s.', c.check_key, c.max_age)
                   ELSE format('%s: newest data %s (%s ago). Threshold %s.',
                               c.check_key, seen, date_trunc('minute', now() - seen), c.max_age)
              END)
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

CREATE OR REPLACE VIEW v_ingest_alert_open AS
SELECT a.id, a.check_key, c.description, a.opened_at, a.last_seen_at,
       now() - a.opened_at AS open_for, a.detail, a.acknowledged_at, a.note
  FROM ingest_alert a
  JOIN ingest_check c USING (check_key)
 WHERE a.resolved_at IS NULL
 ORDER BY a.opened_at;

COMMENT ON TABLE ingest_check IS
  'Ingest watchdog checks. One row per check; max_age is how old the newest data may get before an alert opens. Tune by UPDATE; add a check by inserting a row AND adding a WHEN branch to ingest_check_last_seen().';
COMMENT ON TABLE ingest_alert IS
  'Ingest watchdog incidents, written hourly by ingest_watchdog_run() (pg_cron job ingest-watchdog). One open row per stale check; resolved_at is set when data arrives again. Claude: SELECT * FROM v_ingest_alert_open at the start of any analytical session and tell the user about anything open before answering.';
COMMENT ON VIEW v_ingest_alert_open IS
  'Open ingest alerts. Empty = every Apple Health push is current. Check this before trusting recent Apple Health data.';

ALTER TABLE ingest_check ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingest_alert ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ingest_check, ingest_alert, v_ingest_alert_open FROM anon, authenticated;

SELECT cron.schedule('ingest-watchdog', '50 * * * *', $$SELECT public.ingest_watchdog_run()$$);

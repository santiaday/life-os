-- 0076_cronometer_reconcile.sql
--
-- Two silent undercounts got past every check: foods whose cached record was
-- a throttling reply (no nutrition), and recipes scaled per 100 g instead of
-- per recipe. Both showed in one number Cronometer already hands us: each
-- diary day's summary.consumed.total. This makes that the guard.
--
--   vw_cronometer_reconcile     every day read from Cronometer: its own total
--                               next to the stored fact_nutrition_daily total
--   ingest_check 'cronometer.reconcile'
--                               opens an alert when a day in the last 45 days
--                               has differed by more than 5 kcal for over 2 h
--                               (the 15-minute sync gets two hours to settle)
--   job cronometer-sync-30d     nightly re-read of the last 30 days, so edits
--                               to older days in Cronometer land too

CREATE OR REPLACE VIEW vw_cronometer_reconcile AS
SELECT r.natural_key::date                                             AS day,
       (r.payload -> 'summary' -> 'consumed' ->> 'total')::numeric     AS cronometer_kcal,
       coalesce(f.energy_kcal, 0)                                      AS stored_kcal,
       round(coalesce(f.energy_kcal, 0)
             - (r.payload -> 'summary' -> 'consumed' ->> 'total')::numeric, 1) AS diff_kcal,
       (r.payload -> 'summary' -> 'consumed' ->> 'protein_g')::numeric AS cronometer_protein_g,
       f.protein_g                                                     AS stored_protein_g,
       r.imported_at                                                   AS read_at
  FROM raw_import r
  LEFT JOIN fact_nutrition_daily f ON f.day = r.natural_key::date AND f.source = 'cronometer'
 WHERE r.source = 'cronometer' AND r.entity = 'diary'
   AND r.payload -> 'summary' -> 'consumed' ->> 'total' IS NOT NULL;
REVOKE ALL ON vw_cronometer_reconcile FROM anon, authenticated;
COMMENT ON VIEW vw_cronometer_reconcile IS
  'Cronometer''s own daily intake total (diary summary.consumed.total, as of read_at) beside the stored fact_nutrition_daily total for source cronometer. |diff_kcal| > 5 means the stored day is wrong or stale; the watchdog check cronometer.reconcile alerts on it.';

INSERT INTO ingest_check (check_key, description, max_age, active_from) VALUES
  ('cronometer.reconcile',
   'Stored Cronometer daily totals vs Cronometer''s own (vw_cronometer_reconcile), last 45 days. Stale when a day has been off by more than 5 kcal for longer than max_age.',
   interval '2 hours', now())
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
    -- "Last seen healthy": now when every recent day matches, else when the
    -- oldest still-mismatched day was read.
    WHEN p_check_key = 'cronometer.reconcile' THEN coalesce(
      (SELECT min(read_at) FROM vw_cronometer_reconcile
        WHERE day >= current_date - 45 AND abs(diff_kcal) > 5),
      now())
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
      last_err := CASE
        WHEN c.check_key LIKE 'edge.%' THEN
          (SELECT format(' Last failure %s: HTTP %s %s', date_trunc('minute', e.called_at), coalesce(e.status_code::text, '-'), e.error)
             FROM edge_call e WHERE e.fn = substr(c.check_key, 6) AND e.error IS NOT NULL
            ORDER BY e.called_at DESC LIMIT 1)
        WHEN c.check_key = 'cronometer.reconcile' THEN
          (SELECT ' Days off (stored vs Cronometer kcal): '
                  || string_agg(format('%s %s vs %s', day, round(stored_kcal), round(cronometer_kcal)), ', ' ORDER BY day DESC)
             FROM vw_cronometer_reconcile WHERE day >= current_date - 45 AND abs(diff_kcal) > 5)
        END;
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

SELECT cron.schedule('cronometer-sync-30d', '5 9 * * *', $$SELECT public.call_edge_function('cronometer-sync', '?days=30')$$);

UPDATE ai_guide SET body = replace(body,
  '  every 15m  cronometer-sync (last 3 days)',
  '  every 15m  cronometer-sync (last 3 days); 09:05 UTC daily the last 30 days. Each run checks its days against Cronometer''s own totals (vw_cronometer_reconcile).')
WHERE topic = 'jobs' AND body NOT LIKE '%vw_cronometer_reconcile%';

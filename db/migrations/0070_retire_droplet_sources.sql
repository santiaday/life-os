-- 0070_retire_droplet_sources.sql
--
-- Run AFTER the droplet is shut down (docs/CUTOVER.md step 4). Every stream
-- the droplet ingested becomes historical: its rows stay, it stops counting as
-- stale in vw_data_freshness, and unify_source_health() stops polling it.
--
-- Still live afterwards: apple_watch.* (apple-health-ingest), hevy.activity
-- (hevy-sync), cronometer.nutrition (cronometer-sync).

UPDATE source_health
   SET mode = 'historical',
       coverage_end = coalesce(last_row_day, coverage_end),
       notes = concat_ws(' ', notes, 'Retired 2026-10 with the DigitalOcean droplet.'),
       updated_at = now()
 WHERE mode = 'live'
   AND source_key IN (
     'whoop.activity', 'whoop.sleep', 'whoop.metric', 'whoop.body', 'whoop_lift.activity',
     'loseit.nutrition', 'loseit.weight',
     'pushpress.programming',
     -- Apple Health through Cronometer's biometric passthrough; Apple Health now
     -- arrives directly as apple_watch.*
     'apple_health.metric', 'apple_health.body'
   );

-- The droplet's MCP server role. Nothing connects as it any more.
ALTER ROLE lifeos_mcp NOLOGIN;

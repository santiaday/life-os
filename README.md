# life-os

Santi's health, training and nutrition warehouse. Data lands in the Supabase
project `life-os` (`dqnrcarouldholuqfxbb`); Claude reads it there through the
Supabase connector and writes through three Cloudflare Workers.

```
Health Auto Export ──▶ apple-health-ingest ─┐
Tributary ───────────▶ health-mirror ───────┤
Hevy API ◀── pg_cron ─ hevy-sync ───────────┼──▶ Postgres (raw_* / fact_* / mart_*)
Cronometer ◀─ pg_cron ─ cronometer-sync ────┘        │
                       unify_run() hourly ◀──────────┤  labs, quality flags, freshness, marts
                       ingest_watchdog_run() ◀───────┘  ingest_alert / v_ingest_alert_open

Claude ──▶ Supabase connector (read)
       ──▶ hevy-mcp, cronometer-mcp, calendar-mcp on Cloudflare (write)
```

| Path | What |
|---|---|
| `db/migrations/` | Every schema change, in order. Applied to Supabase; the files are the record. |
| `supabase/functions/` | Edge functions. `hevy-sync` and `health-mirror` live only in Supabase; pull them with `supabase functions download <name> --project-ref dqnrcarouldholuqfxbb`. |
| `workers/` | Cloudflare Workers: `cronometer-mcp` and `calendar-mcp` (iCloud CalDAV), sharing `workers/lib/mcp.js`. Tests: `deno test --allow-env workers/test/workers.test.js`. |
| `docs/CUTOVER.md` | Job map, secrets, connector URLs, and how the move off the droplet was done. |

The schema documents itself: `SELECT topic, body FROM ai_guide ORDER BY ord`
and the table and column comments.

Never commit secrets. Worker secrets are set with `npx wrangler secret put`,
edge function secrets in the Supabase dashboard.

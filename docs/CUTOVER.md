# Cutover: DigitalOcean droplet -> Supabase + Cloudflare

Everything the droplet ran either moved or was retired. Done 2026-10-24: the
droplet is shut down, 0067 and 0070 are applied, and the Python code is gone
from the repo (it is in git history before this change).

## Where things live now

| Job | Before (droplet) | Now |
|---|---|---|
| Apple Watch metrics, sleep, workouts, weight | (none) | Supabase edge function `apple-health-ingest`, pushed by Health Auto Export |
| Raw HealthKit + Health Records labs | (none) | Supabase edge function `health-mirror`, pushed by Tributary |
| Hevy sync | `ingest_hevy` | Supabase edge function `hevy-sync`, pg_cron `:07 :37` |
| Cronometer sync | `ingest_cronometer` (Go export + mobile API) | Supabase edge function `cronometer-sync`, pg_cron every 15 min |
| Normalization (`python -m unify all`) | scheduler 04:15 + every 4 h | `unify_run()` in Postgres, pg_cron `:15` (labs, quality flags, freshness, Apple Watch/Hevy merge) |
| Marts (`python -m mart_refresh`) | scheduler 04:30 | `refresh_marts()` inside `unify_run()`, last 60 days |
| Ingest alerting | `alert_hourly` (Pushover) | `ingest_watchdog_run()` pg_cron `:50`, rows in `ingest_alert` / `v_ingest_alert_open` |
| Claude's read access | life-os MCP server (123 tools) | Supabase connector, guided by `ai_guide` and table/column comments |
| Hevy writes | life-os MCP | Cloudflare Worker `hevy-mcp` (OAuth; paste the Hevy API key on its authorize page) |
| Cronometer writes | life-os MCP | Cloudflare Worker `cronometer-mcp` |
| Calendar | Google Calendar ingest + `calendar_sync` | Cloudflare Worker `calendar-mcp` (iCloud CalDAV, read/write) |
| Whoop, PushPress, Lose It, Copilot, Google Calendar, body-image, lifelog, possessions | droplet | retired; history stays in Supabase |

Code: `db/migrations/0060`-`0070`, `supabase/functions/` (`cronometer-sync`,
`apple-health-ingest`), `workers/`. `hevy-sync` and `health-mirror` were written
directly in Supabase; `supabase functions download <name> --project-ref
dqnrcarouldholuqfxbb` pulls them into `supabase/functions/`.

## 1. Secrets

```sh
# Supabase (cronometer-sync)
supabase secrets set --project-ref dqnrcarouldholuqfxbb \
  CRONOMETER_USERNAME='you@example.com' CRONOMETER_PASSWORD='...'

# Cloudflare
cd workers/cronometer-mcp && npx wrangler secret put CRONOMETER_USERNAME && npx wrangler secret put CRONOMETER_PASSWORD
cd ../calendar-mcp && npx wrangler secret put ICLOUD_USERNAME && npx wrangler secret put ICLOUD_APP_PASSWORD
```

`apple-music-mcp` needs four secrets of its own and a one-time browser step;
see `docs/APPLE_MUSIC.md`. It was never on the droplet -- it is new -- but it
lands here because this is where the connector URLs live.

`ICLOUD_APP_PASSWORD` is an app-specific password from appleid.apple.com ->
Sign-In and Security -> App-Specific Passwords. The normal Apple ID password is
rejected.

Each Worker's `MCP_TOKEN` is already set. Rotate with
`npx wrangler secret put MCP_TOKEN` and update the connector URL.

## 2. Migration 0067 (applied 2026-10-23)

`db/migrations/0067_unify_rewrite_steps_and_schedule.sql` creates
`unify_quality()`, `refresh_mart_daily()` and `refresh_marts()` and schedules
the `unify` and `cronometer-sync` jobs. Statements containing DELETE need an
in-app approval when Claude applies them through the Supabase connector; when
that prompt doesn't arrive, run the file in the SQL editor.

## 3. Connect the MCPs

| Server | URL for a claude.ai custom connector | Header auth (scripts, bots, Claude Code) |
|---|---|---|
| Hevy | `https://hevy-mcp.santiagoaday7.workers.dev/mcp` (OAuth) | OAuth |
| Cronometer | `https://cronometer-mcp.santiagoaday7.workers.dev/mcp/<MCP_TOKEN>` | `POST /mcp` with `Authorization: Bearer <MCP_TOKEN>` |
| Apple Calendar | `https://calendar-mcp.santiagoaday7.workers.dev/mcp/<MCP_TOKEN>` | same |
| Apple Music | `https://apple-music-mcp.santiagoaday7.workers.dev/mcp/<MCP_TOKEN>` | same |

Claude Code on the laptop:

```sh
claude mcp add --transport http apple-calendar https://calendar-mcp.santiagoaday7.workers.dev/mcp \
  --header "Authorization: Bearer <MCP_TOKEN>"
```

Then remove the old `life-os` connector.

## 4. Shut the droplet down (done 2026-10-24)

```sh
ssh lifeos-droplet
cd ~/life-os
docker compose run --rm scheduler python -m unify all     # last projection of the retired sources
docker compose run --rm scheduler python -m mart_refresh  # last full mart rebuild
./scripts/backup.sh                                        # final pg_dump to Spaces, keep it
docker compose down
```

Then, in Supabase, apply `db/migrations/0070_retire_droplet_sources.sql` so the
retired streams stop reading as stale. Destroy the droplet, the Spaces bucket
once the final dump is copied somewhere you keep, and the `lifeos.ledion.io`
DNS record in Cloudflare.

## 5. Claude project instructions

Paste into the Claude project that uses the Supabase connector:

> Health, training, nutrition and lab data live in the Supabase project
> `life-os` (dqnrcarouldholuqfxbb). Before answering anything analytical, run
> `SELECT topic, body FROM ai_guide ORDER BY ord` once per conversation, then
> `SELECT * FROM v_ingest_alert_open` and the freshness query it gives. Report
> any open ingest alert before answering. Read table and column comments
> before querying an unfamiliar table. Log food with the Cronometer MCP,
> workouts with the Hevy MCP, and calendar changes with the Apple Calendar
> MCP; never write rows owned by a sync.

## Checks

```sql
SELECT * FROM v_ingest_alert_open;
SELECT source_key, mode, status, last_row_day FROM vw_data_freshness ORDER BY mode, source_key;
SELECT source, data_type, status, started_at, error_message FROM ingestion_runs ORDER BY started_at DESC LIMIT 10;
SELECT jobname, status, return_message, start_time FROM cron.job_run_details ORDER BY start_time DESC LIMIT 10;
```

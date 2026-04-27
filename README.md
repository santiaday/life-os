# life-os

Personal life-data warehouse + Claude MCP server.

Ingests Whoop, Google Calendar, Cronometer, and Copilot Money into Supabase
Postgres on a three-layer schema (raw → fact → mart), then exposes a small
set of semantic tools over a public HTTP MCP server consumed as a Claude.ai
custom connector.

See [SPEC.md](SPEC.md) for the full design rationale.

---

## Architecture at a glance

```
                  ┌──────────────────────────────┐
                  │  Supabase Postgres           │
                  │  (raw_* / fact_* / mart_*)   │
                  └──┬──────────────┬────────────┘
   ┌─ ingest_whoop ──┤              │
   │  ingest_calendar ┤             │           ┌──────────────────────┐
   │  ingest_cronometer (Go binary) ┼──────────▶│ mcp_server (FastAPI) │
   │  ingest_copilot ─┤             │           │  /mcp streamable-http│
   │                  │             │           │  /health             │
   └─ scheduler ──────┴──┬──────────┘           └──────────┬───────────┘
                         │                                 │
                  mart_refresh                           Caddy (HTTPS)
                                                           │
                                                       Claude.ai
```

All Python services live in this monorepo. The scheduler container bundles
every ingester package and invokes them via subprocess on cron. After each
successful ingest, the scheduler chains `mart_refresh` so analytical queries
see fresh data immediately.

---

## Rebuild from scratch

### 1 — Provision

| Resource          | What you do                                                                 |
|-------------------|-----------------------------------------------------------------------------|
| Supabase project  | Create one. Copy the **direct** (port 5432) and **pooled** (6543) URLs.     |
| MCP DB password   | `openssl rand -hex 32`                                                       |
| MCP API key       | `openssl rand -hex 32`                                                       |
| DigitalOcean VPS  | A 1GB droplet is plenty. Install Docker + Docker Compose.                    |
| DNS               | Point `lifeos.<your-domain>` A-record at the droplet.                        |
| Whoop OAuth       | developer.whoop.com → app → redirect URI `https://lifeos.<dom>/oauth/whoop/callback` |
| Google OAuth      | console.cloud.google.com → enable Calendar API → OAuth client                |
| Backups bucket    | DigitalOcean Spaces (or any s3-compatible). Make access key.                 |
| Pushover (optional) | pushover.net → app → user key + API token.                                |

### 2 — Configure

```bash
cp .env.example .env
$EDITOR .env       # paste every value above
chmod 600 .env
```

### 3 — Migrate

Always run migrations against the **direct** URL — Supabase's pooled
connection mangles role/extension statements that 0001/0006 need.

```bash
uv sync
uv run python -m db.apply
psql "$SUPABASE_DB_URL_DIRECT" -c "\dt"   # verify all raw_/fact_/dim_/mart_ tables
```

### 4 — One-time OAuth bootstrap

```bash
python -m ingest_whoop oauth-init      # visit URL, grab ?code=
python -m ingest_whoop oauth-exchange --code <code>

python -m ingest_calendar oauth-init
python -m ingest_calendar oauth-exchange --code <code>

# Copilot uses email+password, no bootstrap needed.
```

### 5 — Initial backfills

```bash
python -m ingest_whoop      ingest --backfill 365
python -m ingest_calendar   ingest --force-full
python -m ingest_cronometer ingest --backfill 365
python -m ingest_copilot    ingest --backfill 1825   # 5 years
python -m mart_refresh
```

### 6 — Deploy

```bash
docker compose up -d --build
```

Then in Anthropic settings → Connectors → Add custom connector:
- URL: `https://lifeos.<your-domain>/mcp`
- Auth: Bearer, paste `$MCP_API_KEY`

Suggested system prompt prefix:

> *You have access to my life-os connector. Call `get_schema_docs` first when answering analytical questions about my data. Prefer `mart_daily` for daily-grain queries. Use `ask_sql` only when no semantic tool fits.*

### 7 — Backups

Add to host crontab:

```
5 4 * * * /opt/life-os/scripts/backup.sh >> /var/log/lifeos-backup.log 2>&1
```

The script `pg_dump`s the database, gzips, uploads to your S3 bucket, and
prunes objects older than `BACKUP_RETENTION_DAYS` (default 30).

---

## Layout

```
life-os/
├── lifeos_core/        shared library: db, settings, logging, oauth_store, runs, alerts
├── ingest_whoop/       Phase 2 — Whoop API
├── ingest_calendar/    Phase 3 — Google Calendar API
├── ingest_cronometer/  Phase 6 — Cronometer (Go binary subprocess)
├── ingest_copilot/     Phase 7 — Copilot Money GraphQL
├── mart_refresh/       Phase 4 — fact → mart rebuild
├── mcp_server/         Phase 5 — FastAPI + MCP streamable-http
├── scheduler/          Phase 1+ — APScheduler with all jobs
├── db/
│   ├── apply.py        migration runner with ${VAR} env substitution
│   └── migrations/     ordered SQL files
├── scripts/
│   └── backup.sh       daily pg_dump → S3 with retention
└── tests/
    ├── fixtures/       saved API/CSV samples per source
    └── test_*.py       pure-function transform tests
```

## Key behaviors worth knowing

- **Idempotent everywhere.** Every ingester upserts on a natural key; re-runs
  are free. Backfill is just a wider time window.
- **HRV unit auto-detection.** Whoop's API has flip-flopped between seconds
  and milliseconds across versions. `ingest_whoop.transforms.hrv_to_ms`
  detects by magnitude and warns loudly if values look implausible.
- **Calendar incremental sync.** First call per calendar is a full window
  (-90d/+30d). Subsequent calls use the stored `syncToken` for
  delta-only fetches; 410 GONE triggers automatic full re-sync.
- **Cronometer auth fragility.** The Go binary's GWT API can break with
  Cronometer app updates. Failures land in `ingestion_runs.error_message`
  with full stderr; the scheduler keeps running.
- **Copilot schema versioning.** `SCHEMA_VERSION` is recorded in every
  `ingestion_runs.metadata`. Field drift raises `SchemaDriftError` loudly.
- **MCP tools default to `mart_daily`.** The schema docs returned by
  `get_schema_docs` explicitly tell Claude to start there. `ask_sql` is the
  escape hatch for anything outside the curated tool set.
- **ask_sql is doubly safe.** Runs as the `lifeos_mcp` read-only role *and*
  validates against a forbidden-keyword block-list (with comment + literal
  stripping so legitimate `'%DELETE%'` ILIKE patterns aren't rejected).
  5-second statement timeout.

## Health & alerting

- `GET /health` (unauthenticated): per-source last-success / last-attempt timestamps.
- Hourly scheduler job surveys staleness and pushes to Pushover or Slack
  when any source crosses its threshold (configurable per source in
  `lifeos_core/alerts.py`).
- All errors flow through Sentry if `SENTRY_DSN` is set; otherwise they're
  in `docker compose logs`.

## Testing

```bash
uv run pytest -v
```

Pure-function tests (transforms, parsers, SQL safety) run with no DB.
Integration tests are gated on `LIFEOS_TEST_DB_URL`.

## Open decisions baked into defaults

See SPEC.md §11. Currently:

- Domain placeholder: `lifeos.example.com` — replace before deploy.
- Backfill: 365 days for Whoop/Calendar/Cronometer, 1825 days for Copilot.
- Cronometer Gold: assumed (per-meal timestamps; non-Gold falls back to local-noon).
- Calendar: env-driven (`GOOGLE_CALENDAR_IDS`), supports multiple.
- Raw retention: forever.
- Sentry: optional (no-op if `SENTRY_DSN` blank).

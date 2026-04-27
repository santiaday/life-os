-- 0001_init.sql
-- Foundational extensions, helpers, and infrastructure tables shared by every
-- ingester (run log, OAuth token store, calendar sync state).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Single source of truth for "what local day did this happen". Used by
-- generated columns on fact_* tables and by mart_refresh aggregation.
CREATE OR REPLACE FUNCTION local_date(ts TIMESTAMPTZ, tz TEXT DEFAULT 'America/New_York')
RETURNS DATE LANGUAGE SQL IMMUTABLE AS $$
  SELECT (ts AT TIME ZONE tz)::DATE
$$;

-- Every fetch creates a row, success or not. Drives observability and
-- "last successful ingest" health checks.
CREATE TABLE IF NOT EXISTS ingestion_runs (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,            -- 'whoop' | 'calendar' | 'cronometer' | 'copilot'
  data_type TEXT NOT NULL,         -- 'recovery' | 'sleep' | 'events' | ...
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'running',  -- running | success | failure
  rows_fetched INT,
  rows_upserted INT,
  error_message TEXT,
  metadata JSONB
);

CREATE INDEX IF NOT EXISTS ix_ingestion_runs_source_started
  ON ingestion_runs(source, started_at DESC);

CREATE INDEX IF NOT EXISTS ix_ingestion_runs_source_status
  ON ingestion_runs(source, status, started_at DESC);

-- Persistent OAuth token storage. Refresh tokens rotate (Whoop, Google), so
-- .env can't be the source of truth; this table is.
CREATE TABLE IF NOT EXISTS oauth_tokens (
  service TEXT PRIMARY KEY,         -- 'whoop' | 'google' | 'copilot'
  access_token TEXT,
  refresh_token TEXT NOT NULL,
  expires_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-calendar sync token state for Google's incremental sync API.
CREATE TABLE IF NOT EXISTS calendar_sync_state (
  calendar_id TEXT PRIMARY KEY,
  sync_token TEXT,
  last_full_sync_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

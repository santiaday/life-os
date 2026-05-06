-- 0014_whoop_journal_iphone_bridge.sql
-- Whoop journal: iPhone-as-auth-broker rewrite.
--
-- The iPhone Shortcut now does REFRESH_TOKEN_AUTH against Whoop's auth-service
-- (Cloudflare blocks our server from doing so directly, and direct Cognito
-- requires a SECRET_HASH we don't have). The Shortcut POSTs fresh tokens
-- to /lifelog/whoop/refresh-callback, which writes to oauth_tokens. Our
-- server is a pure consumer.
--
-- Schema changes:
--
-- 1. oauth_tokens gains id_token (JWT identity claims; cheap to keep) and
--    metadata (JSONB for provenance like {"source": "ios_shortcut_refresh"}).
--    Both nullable / defaulted so existing rows keep working.
--
-- 2. fact_journal_day: typed day-level pivot of the journal envelope. Layered
--    underneath the existing mart_daily.journal_notes column — mart_refresh
--    keeps reading from raw_whoop_journal.payload until a follow-up PR
--    swaps it to fact_journal_day. Don't conflate.
--
-- 3. fact_habit_log.whoop_behavior_id → behavior_id. Cleaner: dim_whoop_behavior
--    already has behavior_id as PK, so the FK column should match. Existing
--    UNIQUE(day, whoop_behavior_id) constraint and ix_habit_log_key_day
--    index travel through the rename automatically (Postgres tracks them by
--    column oid, not name). The FK targets the same dim row.
--
-- 4. New unique index UNIQUE(day, habit_key, source) on fact_habit_log so a
--    single day can't have duplicate (habit_key, source) pairs from a
--    misbehaving ingester. Distinct from the existing UNIQUE(day, behavior_id):
--    if Whoop ever rotates behavior_id but keeps internal_name stable, this
--    catches it. (Today, both invariants hold; the extra index is cheap insurance.)

BEGIN;

-- 1. oauth_tokens
ALTER TABLE oauth_tokens
  ADD COLUMN IF NOT EXISTS id_token TEXT,
  ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

-- 2. fact_journal_day
CREATE TABLE IF NOT EXISTS fact_journal_day (
  day              DATE PRIMARY KEY,
  journal_entry_id BIGINT,
  cycle_id         BIGINT,
  notes            TEXT,
  user_reviewed    BOOLEAN,
  sleep_during     JSONB,
  ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 3. Rename fact_habit_log.whoop_behavior_id → behavior_id.
--    DO block makes the migration re-runnable: skip if already renamed.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'fact_habit_log' AND column_name = 'whoop_behavior_id'
  ) THEN
    ALTER TABLE fact_habit_log RENAME COLUMN whoop_behavior_id TO behavior_id;
  END IF;
END$$;

-- 4. Unique (day, habit_key, source) — additive insurance.
CREATE UNIQUE INDEX IF NOT EXISTS ux_habit_log_day_key_source
  ON fact_habit_log(day, habit_key, source);

COMMIT;

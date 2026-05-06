-- 0013_events_open_sessions.sql
-- Allow `events` rows to represent in-progress sessions.
--
-- The Lifelog iOS app writes a row at session start with ended_at NULL,
-- then PATCHes it on session end. The existing schema (0012) requires
-- ended_at NOT NULL because every prior source (Whoop, ActivityWatch)
-- only emits already-completed events.
--
-- Calendar sync handles ended_at NULL by skipping the row until it closes
-- (see calendar_sync.sync update from this phase).

-- 1. Drop the NOT NULL on ended_at.
ALTER TABLE events ALTER COLUMN ended_at DROP NOT NULL;

-- 2. Rebuild the generated duration_seconds column so it returns NULL while
--    a session is open. Generated columns can't be altered in place — drop
--    and re-add. Existing rows stay valid because the new expression returns
--    the same value when ended_at is set.
ALTER TABLE events DROP COLUMN duration_seconds;
ALTER TABLE events ADD COLUMN duration_seconds INT GENERATED ALWAYS AS (
  CASE WHEN ended_at IS NULL THEN NULL
       ELSE EXTRACT(EPOCH FROM (ended_at - started_at))::INT
  END
) STORED;

-- 3. Hot path for /events/active: "is there an open ios_manual event?"
CREATE INDEX IF NOT EXISTS events_active_session
  ON events (source, started_at DESC)
  WHERE ended_at IS NULL;

-- 4. Hot path for the stale closer.
CREATE INDEX IF NOT EXISTS events_stale_check
  ON events (started_at)
  WHERE ended_at IS NULL;

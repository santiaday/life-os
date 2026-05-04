-- 0012_events.sql
-- Generic timeline event log driving the lifelog Google Calendar sync.
--
-- Why a separate table from fact_sleep / fact_workout / fact_calendar_event:
--   - fact_* tables are typed and source-specific. This table is the union view
--     across every wearable / laptop / future iOS source, with calendar-sync
--     state attached.
--   - Calendar-sync state (calendar_id, calendar_event_id, synced_at) doesn't
--     belong on the typed fact tables — those are pure derived data, safe to
--     drop and rebuild from raw_*.
--   - One unified events table simplifies the calendar-sync writer (single
--     scan for "what's unsynced") and naturally extends to new sources
--     (ActivityWatch work blocks, future iOS activities) without schema churn.
--
-- (source, source_event_id) is the natural key. Whoop uses its own UUIDs,
-- ActivityWatch synthesizes a hash of (hostname, started_at) so re-running
-- the laptop daemon is idempotent.

CREATE TABLE IF NOT EXISTS events (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,                  -- 'whoop_sleep' | 'whoop_workout' | 'aw_work' | 'aw_personal' | 'ios_manual'
  source_event_id TEXT NOT NULL,         -- idempotency key from source
  event_type TEXT NOT NULL,              -- 'sleep' | 'workout' | 'work_block' | 'activity'
  category TEXT NOT NULL,                -- 'Sleep' | 'Workout' | 'DoorLoop work' | 'Personal work'
  title TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  ended_at TIMESTAMPTZ NOT NULL,
  day DATE GENERATED ALWAYS AS (local_date(started_at)) STORED,
  duration_seconds INT GENERATED ALWAYS AS
    (EXTRACT(EPOCH FROM (ended_at - started_at))::INT) STORED,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  calendar_id TEXT,                      -- Google Calendar this lives on
  calendar_event_id TEXT,                -- set after successful sync
  synced_at TIMESTAMPTZ,                 -- NULL = pending sync
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source, source_event_id)
);

-- "What needs to be pushed to Google Calendar?" — primary hot path.
CREATE INDEX IF NOT EXISTS ix_events_unsynced
  ON events(ended_at) WHERE synced_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_events_started
  ON events(started_at DESC);

CREATE INDEX IF NOT EXISTS ix_events_type_started
  ON events(event_type, started_at DESC);

CREATE INDEX IF NOT EXISTS ix_events_day
  ON events(day);

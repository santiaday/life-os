-- 0021_host_heartbeats.sql
-- Liveness signal for the per-laptop aw_sync daemons.
--
-- Why: the daemon only writes an `events` row when AW reports activity.
-- A dead daemon is indistinguishable from "user wasn't on the computer"
-- if we only look at events. This table records a tick whether or not
-- any blocks were written, so the scheduler's alert job can detect
-- "host has been silent N hours" and notify.

CREATE TABLE IF NOT EXISTS host_heartbeats (
  host           TEXT PRIMARY KEY,
  source         TEXT NOT NULL,         -- 'aw_work' | 'aw_personal'
  last_tick_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_blocks    INT,                   -- count written on the last tick
  raw_aw_events  INT,                   -- count returned by AW on last tick
  metadata       JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_host_heartbeats_tick
  ON host_heartbeats(last_tick_at DESC);

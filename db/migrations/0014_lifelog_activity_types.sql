-- 0014_lifelog_activity_types.sql
-- Move the Lifelog activity catalog out of activity_types.json into a
-- DB-backed table, and add the annotation event-kind.
--
-- Multi-tenant from day one. `user_id` defaults to 'santi' today so the
-- iOS app keeps working without an auth changeover; when we move to a
-- dedicated multi-tenant DB later, the only piece that needs to change
-- is the auth resolver that converts a bearer token into a user_id.
-- Schema is already future-shape.
--
-- Two kinds of activities:
--   session    — has start + end, optional Live Activity, optional
--                location/contacts. The original Lifelog model.
--   annotation — instant log ("drank alcohol", "pain flare-up"). One
--                timestamp, no duration. Stored in events with
--                ended_at = started_at and event_kind = 'annotation'.
--
-- Seeded with the 8 built-in types from activity_types.json. is_custom
-- distinguishes user-created from system-seeded; only is_custom rows can
-- be deleted (built-ins can be unpinned but never removed).

-- ── 1. Activity-type catalog ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS lifelog_activity_types (
  id            BIGSERIAL PRIMARY KEY,
  -- Tenant. Hardcoded today; will be FK to a users table in the
  -- multi-tenant rewrite.
  user_id       TEXT NOT NULL DEFAULT 'santi',
  -- Stable string id the iOS app reads/writes (e.g. 'watching_tv'). The
  -- BIGSERIAL id is internal only. (user_id, slug) is the natural key.
  slug          TEXT NOT NULL,
  label         TEXT NOT NULL,
  emoji         TEXT NOT NULL,
  -- OKLCH hue 0–360.
  hue           INT  NOT NULL CHECK (hue >= 0 AND hue < 360),
  kind          TEXT NOT NULL CHECK (kind IN ('session', 'annotation')),
  focus_mode    TEXT,
  default_capture_location BOOLEAN NOT NULL DEFAULT FALSE,
  default_capture_contacts BOOLEAN NOT NULL DEFAULT FALSE,
  -- For sessions: whether the Live Activity surfaces a running timer.
  -- Annotations ignore this (no LA at all).
  live_activity_show_timer BOOLEAN NOT NULL DEFAULT TRUE,
  sort_order    INT  NOT NULL DEFAULT 100,
  -- Pinned activities appear on the iOS home grid. Unpinned show only in
  -- the "More" sheet.
  is_pinned     BOOLEAN NOT NULL DEFAULT TRUE,
  -- FALSE = system seed (cannot be deleted, can be unpinned + edited).
  -- TRUE  = user-created (full CRUD).
  is_custom     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, slug)
);

-- List-by-user (the hot path for /lifelog/activity-types).
CREATE INDEX IF NOT EXISTS lifelog_activity_types_user
  ON lifelog_activity_types (user_id, is_pinned DESC, sort_order, label);

-- ── 2. Seed the built-in 8 ───────────────────────────────────────────────

INSERT INTO lifelog_activity_types
  (user_id, slug, label, emoji, hue, kind, focus_mode,
   default_capture_location, default_capture_contacts,
   live_activity_show_timer, sort_order, is_pinned, is_custom)
VALUES
  ('santi', 'watching_tv',      'Watching TV',       '📺', 268, 'session',
   'Personal',         FALSE, FALSE, TRUE,  10, TRUE, FALSE),
  ('santi', 'out_with_friends', 'Out with Friends',  '🍻',  55, 'session',
   'Personal',         TRUE,  TRUE,  TRUE,  20, TRUE, FALSE),
  ('santi', 'gym',              'Gym · CrossFit',    '🏋️',  25, 'session',
   'Do Not Disturb',   TRUE,  FALSE, TRUE,  30, TRUE, FALSE),
  ('santi', 'reading',          'Reading',           '📖', 145, 'session',
   'Reading',          FALSE, FALSE, FALSE, 40, TRUE, FALSE),
  ('santi', 'deep_work',        'Deep Work',         '🎯', 220, 'session',
   'Work',             FALSE, FALSE, TRUE,  50, TRUE, FALSE),
  ('santi', 'eating_out',       'Eating Out',        '🍽️',  10, 'session',
   NULL,               TRUE,  TRUE,  TRUE,  60, TRUE, FALSE),
  ('santi', 'travel_commute',   'Travel · Commute',  '🚗', 200, 'session',
   'Driving',          TRUE,  FALSE, TRUE,  70, TRUE, FALSE),
  ('santi', 'paulina_time',     'Time with Paulina', '💛',  35, 'session',
   'Personal',         FALSE, FALSE, FALSE, 80, TRUE, FALSE)
ON CONFLICT (user_id, slug) DO NOTHING;

-- ── 3. event_kind on the events timeline ─────────────────────────────────

ALTER TABLE events
  ADD COLUMN IF NOT EXISTS event_kind TEXT NOT NULL DEFAULT 'session'
    CHECK (event_kind IN ('session', 'annotation'));

-- Most queries care about sessions only. Index supports the
-- "list recent sessions" path used by the iOS history.
CREATE INDEX IF NOT EXISTS events_ios_manual_by_kind
  ON events (source, event_kind, started_at DESC)
  WHERE source = 'ios_manual';

-- ── 4. user_id on the events table ───────────────────────────────────────

-- Multi-tenant future-state. Same default trick: keeps existing rows
-- valid (they all belong to 'santi') and makes new code explicitly
-- per-user without needing a backfill later.
ALTER TABLE events
  ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT 'santi';

CREATE INDEX IF NOT EXISTS events_user_started
  ON events (user_id, source, started_at DESC);

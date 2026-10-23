-- Faithful mirror of Apple HealthKit, written by Tributary via edge fn
-- `health-mirror`. Nothing curated: every source writing to Health is kept and
-- tagged. Curated, deduplicated values stay in the fact_* tables.
--
-- Back-filled into the repo from supabase_migrations; applied 2026-10-20.

CREATE TABLE IF NOT EXISTS hk_sample (
  uuid          UUID PRIMARY KEY,
  type          TEXT NOT NULL,
  kind          TEXT NOT NULL,              -- 'quantity' | 'category'
  value         DOUBLE PRECISION,
  unit          TEXT,
  label         TEXT,                       -- category value name where known
  start_ts      TIMESTAMPTZ NOT NULL,
  end_ts        TIMESTAMPTZ NOT NULL,
  source_name   TEXT,
  source_bundle TEXT,
  device        TEXT,
  metadata      JSONB,
  synced_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hk_sample_type_start_idx ON hk_sample (type, start_ts);

CREATE TABLE IF NOT EXISTS hk_hourly (
  type        TEXT NOT NULL,
  hour_start  TIMESTAMPTZ NOT NULL,
  unit        TEXT NOT NULL,
  sum         DOUBLE PRECISION,
  avg         DOUBLE PRECISION,
  min         DOUBLE PRECISION,
  max         DOUBLE PRECISION,
  synced_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (type, hour_start)
);

CREATE TABLE IF NOT EXISTS hk_workout (
  uuid            UUID PRIMARY KEY,
  activity_type   INTEGER NOT NULL,
  activity_name   TEXT NOT NULL,
  start_ts        TIMESTAMPTZ NOT NULL,
  end_ts          TIMESTAMPTZ NOT NULL,
  duration_s      DOUBLE PRECISION,
  energy_kcal     DOUBLE PRECISION,
  distance_m      DOUBLE PRECISION,
  avg_hr          DOUBLE PRECISION,
  max_hr          DOUBLE PRECISION,
  min_hr          DOUBLE PRECISION,
  source_name     TEXT,
  source_bundle   TEXT,
  device          TEXT,
  metadata        JSONB,
  statistics      JSONB,
  activities      JSONB,
  events          JSONB,
  hr_series       JSONB,
  route           JSONB,
  synced_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hk_workout_start_idx ON hk_workout (start_ts);

CREATE TABLE IF NOT EXISTS hk_object (
  uuid          UUID PRIMARY KEY,
  type          TEXT NOT NULL,   -- ecg | heartbeat_series | audiogram | state_of_mind
  start_ts      TIMESTAMPTZ NOT NULL,
  end_ts        TIMESTAMPTZ NOT NULL,
  source_name   TEXT,
  source_bundle TEXT,
  device        TEXT,
  data          JSONB NOT NULL,
  synced_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hk_object_type_start_idx ON hk_object (type, start_ts);

CREATE TABLE IF NOT EXISTS hk_activity_summary (
  day                 DATE PRIMARY KEY,
  move_kcal           DOUBLE PRECISION,
  move_goal_kcal      DOUBLE PRECISION,
  move_time_s         DOUBLE PRECISION,
  move_time_goal_s    DOUBLE PRECISION,
  exercise_s          DOUBLE PRECISION,
  exercise_goal_s     DOUBLE PRECISION,
  stand_hours         DOUBLE PRECISION,
  stand_goal_hours    DOUBLE PRECISION,
  synced_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hk_characteristics (
  id          INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  data        JSONB NOT NULL,
  synced_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE hk_sample ENABLE ROW LEVEL SECURITY;
ALTER TABLE hk_hourly ENABLE ROW LEVEL SECURITY;
ALTER TABLE hk_workout ENABLE ROW LEVEL SECURITY;
ALTER TABLE hk_object ENABLE ROW LEVEL SECURITY;
ALTER TABLE hk_activity_summary ENABLE ROW LEVEL SECURITY;
ALTER TABLE hk_characteristics ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON hk_sample, hk_hourly, hk_workout, hk_object, hk_activity_summary, hk_characteristics FROM anon, authenticated;
GRANT SELECT ON hk_sample, hk_hourly, hk_workout, hk_object, hk_activity_summary, hk_characteristics TO lifeos_mcp;

CREATE OR REPLACE VIEW vw_hk_coverage AS
  SELECT 'sample' AS kind, type, count(*) AS n, min(start_ts) AS first_ts, max(start_ts) AS last_ts,
         string_agg(DISTINCT source_name, ', ') AS sources
    FROM hk_sample GROUP BY type
  UNION ALL
  SELECT 'hourly', type, count(*), min(hour_start), max(hour_start), NULL FROM hk_hourly GROUP BY type
  UNION ALL
  SELECT 'object', type, count(*), min(start_ts), max(start_ts), string_agg(DISTINCT source_name, ', ')
    FROM hk_object GROUP BY type
  UNION ALL
  SELECT 'workout', activity_name, count(*), min(start_ts), max(start_ts), string_agg(DISTINCT source_name, ', ')
    FROM hk_workout GROUP BY activity_name
  UNION ALL
  SELECT 'activity_summary', 'rings', count(*), min(day)::timestamptz, max(day)::timestamptz, NULL FROM hk_activity_summary;
REVOKE ALL ON vw_hk_coverage FROM anon, authenticated;
GRANT SELECT ON vw_hk_coverage TO lifeos_mcp;

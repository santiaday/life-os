-- 0017_hevy_routines.sql
-- Hevy routine + routine-folder ingestion. Routines are TEMPLATES (the
-- prescription); workouts are SESSIONS (the execution). They live on
-- separate Hevy endpoints so we mirror them into separate tables.
--
-- We don't bother deriving fact_* tables for routines yet — the prescribed
-- sets are denormalized JSONB and ad-hoc queries against payload->'exercises'
-- are fast enough at personal scale. If routine analytics ever matter,
-- we can add a fact_routine_exercise table later without breaking anything.

BEGIN;

-- Routines (templates). Hevy ids are UUIDs.
CREATE TABLE IF NOT EXISTS raw_hevy_routine (
  id              BIGSERIAL PRIMARY KEY,
  fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  hevy_routine_id UUID NOT NULL UNIQUE,
  payload         JSONB NOT NULL,
  updated_at_src  TIMESTAMPTZ,
  -- Fast-path columns promoted out so list queries don't have to parse JSON.
  title           TEXT,
  folder_id       INT,
  deleted         BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS ix_raw_hevy_routine_updated
  ON raw_hevy_routine(updated_at_src);
CREATE INDEX IF NOT EXISTS ix_raw_hevy_routine_folder
  ON raw_hevy_routine(folder_id);

-- Routine folders. Folder id is an integer (NOT a UUID — Hevy treats
-- folders like a flat ordered list per user).
CREATE TABLE IF NOT EXISTS raw_hevy_routine_folder (
  folder_id   INT PRIMARY KEY,
  title       TEXT NOT NULL,
  index       INT,
  payload     JSONB NOT NULL,
  fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;

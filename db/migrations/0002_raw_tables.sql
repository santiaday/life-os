-- 0002_raw_tables.sql
-- Immutable JSONB snapshots straight from each source API. natural_key is
-- whatever uniquely identifies the row from the source; payload is whatever
-- the API returned, untouched. Re-fetching is always safe: ON CONFLICT DO
-- UPDATE replaces payload + bumps fetched_at.

-- ---- Whoop ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_whoop_recovery (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  cycle_id BIGINT NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_whoop_sleep (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  sleep_id UUID NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_whoop_workout (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  workout_id UUID NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_whoop_cycle (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  cycle_id BIGINT NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_whoop_profile (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  payload JSONB NOT NULL
);

-- ---- Google Calendar --------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_calendar_event (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  calendar_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  etag TEXT,
  payload JSONB NOT NULL,
  UNIQUE(calendar_id, event_id)
);

-- ---- Cronometer (one row per export day) -----------------------------------
CREATE TABLE IF NOT EXISTS raw_cronometer_servings (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  day DATE NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_cronometer_daily_nutrition (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  day DATE NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_cronometer_biometrics (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  day DATE NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_cronometer_exercises (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  day DATE NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

-- ---- Copilot Money ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_copilot_transaction (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  transaction_id TEXT NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_copilot_account (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  account_id TEXT NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_copilot_category (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  category_id TEXT NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

-- 0025_whoop_private_trends.sql
-- Whoop PRIVATE iOS-API ingestion (beyond the public OAuth surface in
-- ingest_whoop). Reuses the daily-refreshed oauth_tokens(service='whoop_private')
-- bearer the iPhone Shortcut maintains — see ingest_whoop_private/.
--
-- Three data families, each a raw JSONB snapshot + a typed fact:
--   1. Daily metric trends  — /progression-service/v3/trends/{metric}.
--      Long-format fact_whoop_metric_daily(day, metric, value) is the centerpiece:
--      steps, calories, VO2max, body-comp, weight, respiratory rate, stress,
--      sleep-debt, restorative sleep, HR-zone time, etc. — metrics the public API
--      doesn't expose.
--   2. Sleep need           — /coaching-service/v2/sleepneed. Daily snapshot of the
--      recommended-time-in-bed breakdown (baseline / debt / strain / nap credit).
--   3. Behavior impact      — /behavior-impact-service/v1/impact. Whoop's own causal
--      estimate of how each journal behavior moves recovery, over a trailing 90d.

-- ---- 1. Daily metric trends ------------------------------------------------
-- One raw envelope per (metric, end_date) fetch. The wire payload is a heavy
-- graph BFF; ingest_whoop_private trims it to the data-bearing keys before
-- storing (see transforms.slim_trend_payload) so this table stays small.
CREATE TABLE IF NOT EXISTS raw_whoop_trend (
  id          BIGSERIAL PRIMARY KEY,
  fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  metric      TEXT NOT NULL,
  end_date    DATE NOT NULL,
  payload     JSONB NOT NULL,
  UNIQUE (metric, end_date)
);

-- Long-format daily metric fact. One row per (day, metric). Overlapping fetch
-- windows (week / month / six_month all carry the same day) dedup at upsert via
-- ON CONFLICT (day, metric). Weekly-cadence metrics (VO2_MAX, BODY_COMPOSITION,
-- WEIGHT) simply land on fewer distinct days. `value` is the parsed number;
-- `value_display` keeps Whoop's formatted string verbatim as a fidelity anchor.
CREATE TABLE IF NOT EXISTS fact_whoop_metric_daily (
  id            BIGSERIAL PRIMARY KEY,
  day           DATE NOT NULL,
  metric        TEXT NOT NULL,
  value         NUMERIC(14,3),
  value_display TEXT,
  unit          TEXT,
  raw_id        BIGINT REFERENCES raw_whoop_trend(id),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (day, metric)
);
CREATE INDEX IF NOT EXISTS ix_whoop_metric_daily_metric_day
  ON fact_whoop_metric_daily (metric, day);
CREATE INDEX IF NOT EXISTS ix_whoop_metric_daily_day
  ON fact_whoop_metric_daily (day);

-- ---- 2. Sleep need (daily snapshot) ----------------------------------------
CREATE TABLE IF NOT EXISTS raw_whoop_sleep_need (
  id          BIGSERIAL PRIMARY KEY,
  fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  day         DATE NOT NULL UNIQUE,
  payload     JSONB NOT NULL
);

-- need_breakdown is milliseconds on the wire; store ms verbatim plus a derived
-- recommended-time-in-bed in minutes (the "100%" / full-need recommendation).
CREATE TABLE IF NOT EXISTS fact_whoop_sleep_need (
  day                      DATE PRIMARY KEY,
  recommended_tib_minutes  NUMERIC(7,1),
  total_need_ms            BIGINT,
  baseline_ms              BIGINT,
  debt_ms                  BIGINT,
  strain_ms                BIGINT,
  nap_credit_ms            BIGINT,
  smart_alarm_eligible     BOOLEAN,
  schedule_state           TEXT,
  raw_id                   BIGINT REFERENCES raw_whoop_sleep_need(id),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---- 3. Behavior impact ----------------------------------------------------
-- One envelope per day fetched (the analysis is a trailing-90d rollup, so a
-- daily snapshot is the natural grain).
CREATE TABLE IF NOT EXISTS raw_whoop_behavior_impact (
  id          BIGSERIAL PRIMARY KEY,
  fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  captured_on DATE NOT NULL UNIQUE,
  payload     JSONB NOT NULL
);

-- One row per (captured_on, impact_uuid). outcome is 'recovery' today (the only
-- impact surface Whoop exposes) but kept in the key so a future sleep/strain
-- impact surface won't collide. impact_pct parsed from "+5%" / "-10%" / "0%";
-- NULL when has_sufficient_data is false (still logged, with answer counts).
CREATE TABLE IF NOT EXISTS fact_whoop_behavior_impact (
  id                  BIGSERIAL PRIMARY KEY,
  captured_on         DATE NOT NULL,
  impact_uuid         TEXT NOT NULL,
  behavior_name       TEXT NOT NULL,
  outcome             TEXT NOT NULL DEFAULT 'recovery',
  direction           TEXT,                 -- positive|negative|neutral|insufficient
  impact_pct          NUMERIC(6,2),
  impact_display      TEXT,
  has_sufficient_data BOOLEAN,
  yes_answer_count    INT,
  no_answer_count     INT,
  raw_id              BIGINT REFERENCES raw_whoop_behavior_impact(id),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (captured_on, impact_uuid, outcome)
);
CREATE INDEX IF NOT EXISTS ix_whoop_behavior_impact_uuid
  ON fact_whoop_behavior_impact (impact_uuid);

-- ---- mart_daily new columns ------------------------------------------------
-- Patched post-rebuild from fact_whoop_metric_daily by
-- mart_refresh.UPDATE_MART_DAILY_WHOOP_PRIVATE. weight_kg / body_fat_pct already
-- exist (0004) and stay owned by fact_biometric — the Whoop WEIGHT /
-- BODY_COMPOSITION trends remain queryable in fact_whoop_metric_daily only, so we
-- don't silently fork those two columns across two sources.
ALTER TABLE mart_daily
  ADD COLUMN IF NOT EXISTS steps              INT,
  ADD COLUMN IF NOT EXISTS calories_burned    NUMERIC(10,2),
  ADD COLUMN IF NOT EXISTS vo2_max            NUMERIC(6,2),
  ADD COLUMN IF NOT EXISTS respiratory_rate   NUMERIC(6,2),
  ADD COLUMN IF NOT EXISTS sleep_debt_minutes NUMERIC(7,1);

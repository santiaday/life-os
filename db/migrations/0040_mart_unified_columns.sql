-- 0040_mart_unified_columns.sql
--
-- mart_daily becomes the unified surface.
--
-- The existing rebuild in mart_refresh/sql.py stays exactly as it is -- it
-- fills the calendar, spending, journal and body-image columns, and rewriting
-- 600 lines of working SQL to change where three of them come from would be a
-- bad trade. Instead an overlay pass runs after it and rewrites the training,
-- sleep, nutrition and body columns from the unified views, which are
-- cross-source, deduplicated and quality-filtered.
--
-- The columns added here are the ones the unified layer can answer and the
-- old per-source SQL could not: hard minutes, rolling adherence, measurement
-- method, and the provenance of each day's numbers.

BEGIN;

ALTER TABLE mart_daily
  -- training
  ADD COLUMN IF NOT EXISTS hard_minutes            NUMERIC,
  ADD COLUMN IF NOT EXISTS resistance_count        INTEGER,
  ADD COLUMN IF NOT EXISTS activity_sources        TEXT,
  ADD COLUMN IF NOT EXISTS sessions_per_week_4w    NUMERIC,
  ADD COLUMN IF NOT EXISTS sessions_per_week_12w   NUMERIC,
  ADD COLUMN IF NOT EXISTS sessions_per_week_26w   NUMERIC,
  ADD COLUMN IF NOT EXISTS below_floor             BOOLEAN,
  -- body composition, with the method that produced it
  ADD COLUMN IF NOT EXISTS weight_method           TEXT,
  ADD COLUMN IF NOT EXISTS weight_source           TEXT,
  ADD COLUMN IF NOT EXISTS body_fat_method         TEXT,
  ADD COLUMN IF NOT EXISTS lean_mass_kg            NUMERIC,
  ADD COLUMN IF NOT EXISTS fat_mass_kg             NUMERIC,
  -- provenance
  ADD COLUMN IF NOT EXISTS sleep_source            TEXT,
  ADD COLUMN IF NOT EXISTS nutrition_source        TEXT,
  -- movement + fasting
  ADD COLUMN IF NOT EXISTS active_minutes          NUMERIC,
  ADD COLUMN IF NOT EXISTS intensity_minutes       NUMERIC,
  ADD COLUMN IF NOT EXISTS fast_hours              NUMERIC,
  -- data quality
  ADD COLUMN IF NOT EXISTS quality_flag_count      INTEGER NOT NULL DEFAULT 0;

-- The unified layer reaches back to 2023-08-14; the old spine started later.
-- vw_coverage_daily is the authority on how far back a row should exist.

CREATE INDEX IF NOT EXISTS mart_daily_below_floor_idx ON mart_daily (day) WHERE below_floor;

COMMIT;

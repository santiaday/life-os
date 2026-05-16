-- 0023_body_image_optimizations.sql
--
-- Tier 1+2+3 optimization landing for the body-image pipeline:
--
--   * Session bundling. Front + 3/4 left + 3/4 right captured in one
--     iOS Shortcut run share a session_id, so the dashboard can show
--     all three side-by-side and "multi-photo noise floor" panels are
--     trivial.
--
--   * Run sampling. Each photo can be rated N times per rater (temp=0,
--     rotated seed) to estimate model-internal variance — the floor of
--     what any trend movement has to clear before it's signal.
--
--   * Specialist splits. `source` now distinguishes claude_structure /
--     claude_surface / gpt4v_* / gemini_* / geometry — different
--     prompts targeting orthogonal failure modes. The unique constraint
--     becomes (photo_id, source, run_index).
--
--   * Intervention log. Discrete events (started tret, fresh haircut)
--     drive vertical markers on dashboard charts and lagged-correlation
--     queries.
--
--   * Mart wiring. body_image_* columns join mart_daily so the existing
--     correlate_metrics MCP tool can cross-reference body-image scores
--     against everything else in life-os.

-- ── 1. Sessions + runs on existing rows ─────────────────────────────

ALTER TABLE body_image_photo
  ADD COLUMN IF NOT EXISTS session_id UUID;

CREATE INDEX IF NOT EXISTS body_image_photo_session
  ON body_image_photo (session_id);

ALTER TABLE body_image_rating
  ADD COLUMN IF NOT EXISTS run_index INT NOT NULL DEFAULT 1;

-- Swap the unique constraint to include run_index. Drop the old one if
-- it exists (was created by the index-as-constraint trick in 0022).
DROP INDEX IF EXISTS body_image_rating_photo_source;
CREATE UNIQUE INDEX IF NOT EXISTS body_image_rating_photo_source_run
  ON body_image_rating (photo_id, source, run_index);

-- ── 2. Intervention log ─────────────────────────────────────────────
--
-- One row per discrete behavior change. The dashboard reads these to
-- draw vertical markers on every trend chart, and ad-hoc SQL can
-- correlate "started tret" against subsequent skin_clarity movement.

CREATE TABLE IF NOT EXISTS body_image_intervention (
  id             BIGSERIAL PRIMARY KEY,
  user_id        TEXT NOT NULL DEFAULT 'santi',
  -- Stable key for grouping ('tretinoin', 'minoxidil_cheeks', 'haircut',
  -- 'spf', 'curl_cream', ...). Keep snake_case for chart lookup.
  intervention_key TEXT NOT NULL,
  -- Event type. 'start' / 'stop' bracket continuous interventions;
  -- 'apply' is a one-off (e.g. "applied red light therapy"); 'milestone'
  -- marks notable checkpoints ("first month on tret").
  event          TEXT NOT NULL CHECK (event IN ('start', 'stop', 'apply', 'milestone')),
  occurred_on    DATE NOT NULL,
  -- Free-form context: dosage, brand, notes, anything you'd want to see
  -- when hovering the marker on the chart.
  metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS body_image_intervention_user_date
  ON body_image_intervention (user_id, occurred_on DESC);

CREATE INDEX IF NOT EXISTS body_image_intervention_key
  ON body_image_intervention (user_id, intervention_key, occurred_on);

-- ── 3. Daily body-image rollup (mart_body_image_daily) ──────────────
--
-- One row per (day, user) with per-feature averages across all LLM
-- raters / runs / specialists. Geometry metrics live here too. The
-- mart_refresh service truncates + repopulates this on every refresh,
-- and then UPDATEs the matching columns on mart_daily so the existing
-- correlate_metrics tool can see body-image data without schema
-- knowledge of the new tables.

CREATE TABLE IF NOT EXISTS mart_body_image_daily (
  day                       DATE NOT NULL,
  user_id                   TEXT NOT NULL DEFAULT 'santi',
  -- Composite (avg of every LLM rating's `overall` across all
  -- specialists / runs / models for that day).
  body_image_overall        NUMERIC,
  -- Subjective features (averaged across LLM raters / runs).
  body_image_skin_quality   NUMERIC,
  body_image_skin_clarity   NUMERIC,
  body_image_under_eye      NUMERIC,
  body_image_jawline        NUMERIC,
  body_image_chin           NUMERIC,
  body_image_eye_quality    NUMERIC,
  body_image_nose_harmony   NUMERIC,
  body_image_lip_quality    NUMERIC,
  body_image_hair_quality   NUMERIC,
  body_image_hairline       NUMERIC,
  body_image_beard_density  NUMERIC,
  body_image_grooming       NUMERIC,
  body_image_expression     NUMERIC,
  body_image_photo_quality  NUMERIC,
  -- Geometry (deterministic, no model noise).
  body_image_symmetry       NUMERIC,
  body_image_gonial_angle   NUMERIC,
  body_image_jaw_ratio      NUMERIC,
  -- Counts for self-validation on the dashboard.
  body_image_photo_count    INT  NOT NULL DEFAULT 0,
  body_image_rating_count   INT  NOT NULL DEFAULT 0,
  refreshed_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, day)
);

CREATE INDEX IF NOT EXISTS mart_body_image_daily_day
  ON mart_body_image_daily (day);

-- ── 4. mart_daily column additions ──────────────────────────────────
--
-- Mirror a subset of body-image metrics onto mart_daily so the existing
-- MCP correlate_metrics tool (which queries mart_daily directly) can
-- correlate them against HRV, alcohol, sleep, etc.

ALTER TABLE mart_daily
  ADD COLUMN IF NOT EXISTS body_image_overall      NUMERIC,
  ADD COLUMN IF NOT EXISTS body_image_skin_quality NUMERIC,
  ADD COLUMN IF NOT EXISTS body_image_skin_clarity NUMERIC,
  ADD COLUMN IF NOT EXISTS body_image_under_eye    NUMERIC,
  ADD COLUMN IF NOT EXISTS body_image_jawline      NUMERIC,
  ADD COLUMN IF NOT EXISTS body_image_hair_quality NUMERIC,
  ADD COLUMN IF NOT EXISTS body_image_symmetry     NUMERIC,
  ADD COLUMN IF NOT EXISTS body_image_photo_quality NUMERIC;

-- 0039_unified_labs.sql
--
-- Lab convergence. The 2026-05-30 Quest draw is currently in the warehouse
-- twice -- once ingested from Whoop Advanced Labs and once entered from the
-- Quest report -- under different slugs for the same analyte (`alt` vs
-- `alanine_aminotransferase`, `egfr` vs `estimated_glomerular_filtration_rate`,
-- and an `alkaline_phosotase` typo). Same blood, same numbers, two rows.
--
-- Fix: a canonical biomarker vocabulary with an alias table, one panel row per
-- physical draw, and one measurement row per analyte per draw. `fact_lab_result`
-- stays as the source-specific landing table.

BEGIN;

-- Canonical analytes.
CREATE TABLE IF NOT EXISTS dim_biomarker (
  biomarker_key   TEXT PRIMARY KEY,        -- 'alt', 'ldl_c', 'vitamin_d_25oh'
  display_name    TEXT NOT NULL,
  category        TEXT,                    -- liver|kidney|lipid|metabolic|hematology|
                                           -- thyroid|hormone|inflammation|vitamin|
                                           -- electrolyte|autoimmune|muscle|other
  canonical_unit  TEXT,
  optimal_low     NUMERIC,
  optimal_high    NUMERIC,
  ref_low         NUMERIC,
  ref_high        NUMERIC,
  higher_is_better BOOLEAN,
  description     TEXT,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dim_biomarker_alias (
  alias         TEXT PRIMARY KEY,          -- normalized vendor slug or label
  biomarker_key TEXT NOT NULL REFERENCES dim_biomarker(biomarker_key) ON UPDATE CASCADE,
  source        TEXT,                      -- which vendor spells it this way
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dim_biomarker_alias_key_idx ON dim_biomarker_alias (biomarker_key);

-- One row per physical blood draw / collection event.
CREATE TABLE IF NOT EXISTS fact_lab_panel (
  panel_uid        TEXT PRIMARY KEY,       -- '<source>:<test_id>'
  source           TEXT NOT NULL,
  source_panel_id  TEXT NOT NULL,
  collected_on     DATE NOT NULL,
  reported_on      DATE,
  provider         TEXT,                   -- 'quest_diagnostics', 'whoop', 'labcorp'
  ordering_provider TEXT,
  panel_name       TEXT,
  result_count     INTEGER NOT NULL DEFAULT 0,
  -- Same draw reported by two systems shares a cluster; one is primary.
  cluster_id       BIGINT,
  is_primary       BOOLEAN NOT NULL DEFAULT TRUE,
  raw_id           BIGINT,
  payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fact_lab_panel_day_idx ON fact_lab_panel (collected_on);

-- One row per analyte per draw per source.
CREATE TABLE IF NOT EXISTS fact_lab_measurement (
  measurement_uid TEXT PRIMARY KEY,
  panel_uid       TEXT REFERENCES fact_lab_panel(panel_uid) ON DELETE CASCADE,
  source          TEXT NOT NULL,
  collected_on    DATE NOT NULL,
  biomarker_key   TEXT,                    -- NULL when unmapped
  vendor_key      TEXT NOT NULL,
  display_name    TEXT,
  value_numeric   NUMERIC,
  value_text      TEXT,
  unit            TEXT,
  ref_low         NUMERIC,
  ref_high        NUMERIC,
  optimal_low     NUMERIC,
  optimal_high    NUMERIC,
  status          TEXT,                    -- optimal|normal|low|high|critical
  is_primary      BOOLEAN NOT NULL DEFAULT TRUE,
  raw_id          BIGINT,
  payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fact_lab_measurement_day_idx    ON fact_lab_measurement (collected_on);
CREATE INDEX IF NOT EXISTS fact_lab_measurement_marker_idx ON fact_lab_measurement (biomarker_key, collected_on);
CREATE INDEX IF NOT EXISTS fact_lab_measurement_panel_idx  ON fact_lab_measurement (panel_uid);

-- Imaging gets the same treatment: a canonical study table that accepts
-- anything from a radiology PDF to a one-line note dictated into chat.
CREATE TABLE IF NOT EXISTS fact_imaging (
  study_uid       TEXT PRIMARY KEY,
  source          TEXT NOT NULL,
  study_date      DATE NOT NULL,
  modality        TEXT NOT NULL,           -- MRI|CT|XR|US|DEXA|ECHO|PET|OTHER
  body_region     TEXT,
  laterality      TEXT,
  provider        TEXT,
  ordering_reason TEXT,
  impression      TEXT,
  findings        JSONB NOT NULL DEFAULT '[]'::jsonb,
  measurements    JSONB NOT NULL DEFAULT '{}'::jsonb,
  raw_text        TEXT,
  report_url      TEXT,
  raw_id          BIGINT,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fact_imaging_date_idx ON fact_imaging (study_date);
CREATE INDEX IF NOT EXISTS fact_imaging_modality_idx ON fact_imaging (modality, study_date);

COMMIT;

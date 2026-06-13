-- 0030_imaging.sql
-- Structured imaging studies (MRI / X-ray / CT / ultrasound). Radiology lives on
-- paper / PDFs; this gives it a home so back/SI-joint findings can be tracked and
-- correlated. Submitted directly via the submit_imaging_study MCP tool.

CREATE TABLE IF NOT EXISTS raw_imaging (
  id         BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  study_id   TEXT NOT NULL UNIQUE,
  payload    JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_imaging_study (
  study_id        TEXT PRIMARY KEY,
  study_date      DATE,
  modality        TEXT,              -- MRI | X-RAY | CT | ULTRASOUND | DEXA | ...
  body_region     TEXT,              -- e.g. 'lumbar spine', 'SI joints'
  provider        TEXT,
  ordering_reason TEXT,
  impression      TEXT,              -- radiologist's summary/impression
  findings        JSONB,             -- [{location, finding, severity?}]
  raw_text        TEXT,              -- full report text, verbatim
  source          TEXT NOT NULL DEFAULT 'external',
  raw_id          BIGINT REFERENCES raw_imaging(id),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_imaging_study_date ON fact_imaging_study(study_date);
CREATE INDEX IF NOT EXISTS ix_imaging_region     ON fact_imaging_study(body_region);

GRANT SELECT ON fact_imaging_study, raw_imaging TO lifeos_mcp;

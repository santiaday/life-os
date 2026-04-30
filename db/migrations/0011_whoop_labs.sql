-- 0011_whoop_labs.sql
-- Whoop Advanced Labs ingestion. Three tables:
--   raw_whoop_labs       — one row per panel test (full JSON payload).
--   dim_lab_biomarker    — hand-curated catalog of every biomarker Whoop tests
--                          for, with descriptions, units, optimal/sufficient
--                          reference ranges, and what high/low values mean.
--                          Keyed by Whoop's stable biomarker_id slug.
--   fact_lab_result      — one row per (test, biomarker). Captures the actual
--                          measured value, status classification, and the
--                          range-meter geometry from the JSON payload.

-- Raw response payloads keyed by Whoop's test_id (UUID-like string).
CREATE TABLE IF NOT EXISTS raw_whoop_labs (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  test_id TEXT NOT NULL UNIQUE,
  test_name TEXT,
  test_date DATE,
  payload JSONB NOT NULL
);

-- Catalog of every biomarker Whoop tests for (Comprehensive Health Panel
-- includes 75). description / what_high_means / what_low_means seed clinical
-- context the MCP surfaces on health questions. Reference ranges follow the
-- conventions surfaced in Whoop Advanced Labs ("optimal" = tighter clinical
-- target, "sufficient" = acceptable, outside both = concern flagged).
CREATE TABLE IF NOT EXISTS dim_lab_biomarker (
  biomarker_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  category TEXT NOT NULL,
  unit TEXT,
  description TEXT NOT NULL,
  optimal_low NUMERIC(14,4),
  optimal_high NUMERIC(14,4),
  sufficient_low NUMERIC(14,4),
  sufficient_high NUMERIC(14,4),
  what_high_means TEXT,
  what_low_means TEXT,
  influenced_by TEXT,
  notes TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_dim_lab_biomarker_category
  ON dim_lab_biomarker(category);

-- One row per (test, biomarker). UNIQUE(test_id, biomarker_id) is the natural
-- key so re-ingesting the same panel is idempotent.
CREATE TABLE IF NOT EXISTS fact_lab_result (
  id BIGSERIAL PRIMARY KEY,
  raw_id BIGINT REFERENCES raw_whoop_labs(id),
  test_id TEXT NOT NULL,
  test_date DATE,
  biomarker_id TEXT NOT NULL REFERENCES dim_lab_biomarker(biomarker_id),
  value_text TEXT,                      -- raw "6.0" / "1.7" / "13.6" string
  value_numeric NUMERIC(14,4),          -- parsed numeric, NULL if non-numeric
  unit TEXT,
  status_type TEXT,                     -- OPTIMAL | SUFFICIENT | OUT_OF_RANGE
  trend TEXT,                           -- POSITIVE_RANGE | SUFFICIENT_BLUE | CONCERN_RANGE
  trend_display TEXT,                   -- human label e.g. "Out of Range"
  range_meter JSONB,                    -- normalized 0-1 sections + indicator
  indicator_percent NUMERIC(10,8),      -- where current value sits on the meter
  source_row_hash TEXT NOT NULL UNIQUE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(test_id, biomarker_id)
);

CREATE INDEX IF NOT EXISTS ix_fact_lab_result_biomarker
  ON fact_lab_result(biomarker_id);
CREATE INDEX IF NOT EXISTS ix_fact_lab_result_status
  ON fact_lab_result(status_type);
CREATE INDEX IF NOT EXISTS ix_fact_lab_result_date
  ON fact_lab_result(test_date);

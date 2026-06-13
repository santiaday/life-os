-- 0028_labs_native_and_external.sql
-- Make Advanced Labs ingestion native + open it to externally-submitted tests.
--
-- 1. fact_lab_result gains `source` (whoop | external), `lab_provider`, and
--    `reference_ranges` (the absolute optimal/sufficient/out-of-range bounds the
--    native /biomarker-tests/{id}/summary endpoint returns — the old SDUI capture
--    only had normalized meter geometry).
-- 2. dim_lab_biomarker NOT NULLs are relaxed so a biomarker first seen in a test
--    result (Whoop adds new markers; external panels carry their own) can be
--    auto-stubbed without full curation. The curated rows from biomarkers.py keep
--    their rich descriptions.
-- 3. raw_external_lab stores directly-submitted (non-Whoop) lab payloads.

ALTER TABLE fact_lab_result
  ADD COLUMN IF NOT EXISTS source           TEXT NOT NULL DEFAULT 'whoop',
  ADD COLUMN IF NOT EXISTS lab_provider     TEXT,
  ADD COLUMN IF NOT EXISTS reference_ranges JSONB;

CREATE INDEX IF NOT EXISTS ix_fact_lab_result_source ON fact_lab_result(source);

-- Auto-stub support: a biomarker can enter via a result before it's curated.
ALTER TABLE dim_lab_biomarker ALTER COLUMN description DROP NOT NULL;
ALTER TABLE dim_lab_biomarker ALTER COLUMN category    DROP NOT NULL;

-- Externally-submitted labs (a PDF/printout the user hands over, not routed
-- through Whoop). fact_lab_result rows reference these by test_id (source='external').
CREATE TABLE IF NOT EXISTS raw_external_lab (
  id          BIGSERIAL PRIMARY KEY,
  fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  test_id     TEXT NOT NULL UNIQUE,
  test_name   TEXT,
  test_date   DATE,
  provider    TEXT,
  payload     JSONB NOT NULL
);

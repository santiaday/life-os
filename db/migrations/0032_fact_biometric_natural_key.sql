-- 0032_fact_biometric_natural_key.sql
-- fact_biometric was deduped ONLY on source_row_hash = sha256(day|time|metric|
-- value|unit). Because the VALUE is in the hash, any source that revised a value
-- for the same (metric, measured_at) — Whoop/Apple recomputing recovery, sleep,
-- weight after a backfill — produced a NEW hash, so ON CONFLICT(source_row_hash)
-- never matched and a SECOND row was inserted. ~86% of the table became stale
-- duplicates (e.g. 2026-06-11 recovery_whoop = 33 AND 61 at the same instant),
-- and consumers reading by (metric, day) got nondeterministic answers.
--
-- Collapse to the natural grain (metric, measured_at), keeping the freshest row,
-- then enforce it so future revisions UPDATE in place. The ingester switches its
-- conflict key to (metric, measured_at) in the same change.

-- 1) Keep only the freshest row per (metric, measured_at); drop older dupes.
DELETE FROM fact_biometric
WHERE id IN (
  SELECT id FROM (
    SELECT id,
           ROW_NUMBER() OVER (
             PARTITION BY metric, measured_at
             ORDER BY updated_at DESC NULLS LAST, id DESC
           ) AS rn
    FROM fact_biometric
    WHERE measured_at IS NOT NULL
  ) ranked
  WHERE rn > 1
);

-- 2) Enforce the natural grain. (The old UNIQUE(source_row_hash) stays as a
--    harmless provenance guard; the upsert now conflicts on this instead.)
ALTER TABLE fact_biometric
  ADD CONSTRAINT uq_fact_biometric_metric_measured UNIQUE (metric, measured_at);

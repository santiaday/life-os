-- 0035_food_log_natural_key.sql
-- fact_food_log (Cronometer) deduplication. The Cronometer upsert keyed on
-- source_row_hash = sha256(day|time|food|AMOUNT|UNIT) — a VALUE-derived key.
-- Editing a logged serving's quantity in Cronometer changed the hash, so the
-- next ingest couldn't find the prior row and INSERTed a duplicate, leaving the
-- stale row behind (the fact_biometric bug class, fixed there by 0032). This
-- left 17 excess rows over-counting kcal/macros in mart_daily food_agg and
-- mart_meal on 8 days. ingest_cronometer/parsers.py now hashes IMMUTABLE
-- identity only (cronometer|day|meal_group|food_name|ordinal); this migration
-- removes the existing stale dups and re-hashes survivors to match the new
-- scheme so a re-ingest updates in place.

-- 1) Drop stale duplicates: keep the freshest (highest id = latest re-ingest)
--    row per (day, eaten_at, food_name, meal_group) among Cronometer rows.
DELETE FROM fact_food_log a
USING fact_food_log b
WHERE a.source = 'cronometer' AND b.source = 'cronometer'
  AND a.day = b.day AND a.eaten_at = b.eaten_at
  AND a.food_name = b.food_name AND a.meal_group = b.meal_group
  AND a.id < b.id;

-- 2) Re-hash survivors to the new immutable-identity scheme so future ingests
--    of the same logical serving collide on source_row_hash and UPDATE in place.
UPDATE fact_food_log f
   SET source_row_hash = encode(digest(
         'cronometer|' || to_char(f.day, 'YYYY-MM-DD') || '|' ||
         f.meal_group || '|' || f.food_name || '|' || (o.rn - 1)::text,
         'sha256'), 'hex')
  FROM (
    SELECT id, row_number() OVER (
             PARTITION BY day, meal_group, food_name ORDER BY eaten_at, id
           ) AS rn
    FROM fact_food_log WHERE source = 'cronometer'
  ) o
 WHERE f.id = o.id AND f.source = 'cronometer';

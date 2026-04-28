-- 0007_copilot_metadata.sql
-- Persist Copilot per-transaction metadata that previously only lived in
-- the API. With these columns, MCP read tools (get_transactions, the couples
-- workflow) no longer need to round-trip to Copilot for every row.
--
-- Forward-compatible: existing rows get NULL for the new columns. Next
-- ingest run populates them from the GraphQL response.

ALTER TABLE fact_transaction
  ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
  ADD COLUMN IF NOT EXISTS tag_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
  ADD COLUMN IF NOT EXISTS is_reviewed BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS tip_amount NUMERIC(14,2),
  ADD COLUMN IF NOT EXISTS parent_id TEXT,
  ADD COLUMN IF NOT EXISTS copilot_type TEXT;

-- GIN index on tags so the couples workflow can query "transactions with no
-- tag in {me, partner, joint}" without a sequential scan.
CREATE INDEX IF NOT EXISTS ix_transaction_tags
  ON fact_transaction USING GIN (tags);

-- Helpful for "uncategorized" queries.
CREATE INDEX IF NOT EXISTS ix_transaction_uncategorized
  ON fact_transaction(date) WHERE category_id IS NULL;

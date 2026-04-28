-- 0008_transaction_item_id.sql
-- Copilot's editTransaction mutation requires (itemId, accountId, id) as the
-- transaction-locator triple. accountId we already have; itemId is the
-- per-transaction identifier of the underlying linked-account item (Plaid
-- /MX item). Persist it so edits don't need a Copilot round-trip first.

ALTER TABLE fact_transaction
  ADD COLUMN IF NOT EXISTS item_id TEXT;

CREATE INDEX IF NOT EXISTS ix_transaction_item_id
  ON fact_transaction(item_id) WHERE item_id IS NOT NULL;

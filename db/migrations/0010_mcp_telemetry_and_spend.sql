-- 0010_mcp_telemetry_and_spend.sql
-- Three concerns bundled into one migration:
--
--   1. mcp_tool_log: every MCP tool call gets a row. Powers "which tools are
--      slow / repeated / failing" without a third-party APM. Indexed on
--      (tool_name, started_at) for the dashboards we run via ask_sql.
--
--   2. fact_transaction perf indexes: account_id was missing, which forced
--      full-table scans whenever we filtered to "the joint Chase card". Also
--      a date-DESC partial index on non-excluded charges (the hot path for
--      every spending query).
--
--   3. mart_daily spend extension: split out alcohol, bars/nightlife,
--      entertainment, shopping, and an evening-restaurant count column so
--      "did I go out drinking" questions don't require pulling raw txns.

-- ---- 1. tool log ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS mcp_tool_log (
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  tool_name TEXT NOT NULL,
  duration_ms INTEGER NOT NULL,
  ok BOOLEAN NOT NULL,
  row_count INTEGER,
  truncated BOOLEAN,
  error_type TEXT,
  error_message TEXT,
  args_summary JSONB,                   -- date ranges, scalar args, NOT free-text
  caller TEXT                           -- 'mcp' | 'cli' | 'test'
);

CREATE INDEX IF NOT EXISTS ix_mcp_tool_log_tool_started
  ON mcp_tool_log (tool_name, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_mcp_tool_log_started
  ON mcp_tool_log (started_at DESC);
CREATE INDEX IF NOT EXISTS ix_mcp_tool_log_failures
  ON mcp_tool_log (started_at DESC) WHERE NOT ok;

-- Rolling p50/p95 view; the MCP analytics tool reads this directly.
CREATE OR REPLACE VIEW mcp_tool_perf AS
SELECT
  tool_name,
  COUNT(*)                                                        AS n,
  COUNT(*) FILTER (WHERE NOT ok)                                  AS errors,
  ROUND(AVG(duration_ms))::INT                                    AS mean_ms,
  PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY duration_ms)::INT  AS p50_ms,
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms)::INT  AS p95_ms,
  PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY duration_ms)::INT  AS p99_ms,
  MAX(duration_ms)                                                AS max_ms,
  MIN(started_at)                                                 AS first_call,
  MAX(started_at)                                                 AS last_call
FROM mcp_tool_log
WHERE started_at >= now() - INTERVAL '30 days'
GROUP BY tool_name
ORDER BY n DESC;

-- ---- 2. fact_transaction indexes ------------------------------------------
-- account_id filter was sequentially scanning every transaction.
CREATE INDEX IF NOT EXISTS ix_transaction_account_date
  ON fact_transaction (account_id, date DESC);

-- The hot path: "give me all real charges in this date range, newest first".
-- Partial (NOT is_excluded AND amount > 0) keeps the index tight.
CREATE INDEX IF NOT EXISTS ix_transaction_active_date
  ON fact_transaction (date DESC, amount DESC)
  WHERE NOT is_excluded AND amount > 0;

-- Merchant ILIKE searches: trigram index. Cheap, dramatic speedup on
-- "find Bulla Gastrobar" against thousands of rows.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS ix_transaction_merchant_trgm
  ON fact_transaction USING GIN (merchant gin_trgm_ops);

-- ---- 3. mart_daily extra spend columns -----------------------------------
ALTER TABLE mart_daily
  ADD COLUMN IF NOT EXISTS alcohol_spend NUMERIC(12,2),
  ADD COLUMN IF NOT EXISTS bars_spend NUMERIC(12,2),
  ADD COLUMN IF NOT EXISTS entertainment_spend NUMERIC(12,2),
  ADD COLUMN IF NOT EXISTS shopping_spend NUMERIC(12,2),
  ADD COLUMN IF NOT EXISTS travel_spend NUMERIC(12,2),
  -- Count of restaurant/bar txns posted on this date >= $50; cleanest signal
  -- for "did I go out the night before". Kept as a count not a flag so we can
  -- threshold differently in queries without re-running the mart.
  ADD COLUMN IF NOT EXISTS dining_out_txn_count INT,
  ADD COLUMN IF NOT EXISTS dining_out_txn_max NUMERIC(12,2);

-- 0033_mcp_write_audit.sql
-- Forensic log for the generic DB write tools (execute_sql / db_insert /
-- db_update / db_delete / db_upsert). mcp_tool_log records that a write tool
-- was called, but only an args_summary — NOT the actual SQL or affected rows.
-- This table captures the full statement, bound params, row count, and whether
-- it was a dry-run or a real commit, so every mutation Claude makes is
-- reviewable and (if needed) reversible by hand.
--
-- Written by the admin role (same one the write tools use). Kept out of the
-- read-only ask_sql role's concern; it's just SELECT-able like any other table.

CREATE TABLE IF NOT EXISTS mcp_write_audit (
  id            BIGSERIAL PRIMARY KEY,
  ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
  tool          TEXT NOT NULL,         -- execute_sql | db_insert | db_update | db_delete | db_upsert
  operation     TEXT,                  -- SELECT | INSERT | UPDATE | DELETE | UPSERT | DDL_* | TRUNCATE | OTHER
  target_table  TEXT,                  -- best-effort table name the statement touches
  statement     TEXT NOT NULL,         -- the exact SQL sent (or would-be, for dry-run)
  params        JSONB,                 -- bound params, truncated/summarized
  affected_rows INTEGER,               -- cur.rowcount after execution
  dry_run       BOOLEAN NOT NULL DEFAULT FALSE,
  committed     BOOLEAN NOT NULL DEFAULT FALSE,
  ok            BOOLEAN NOT NULL,
  error         TEXT,
  result_sample JSONB                  -- sample of RETURNING rows, if any
);

CREATE INDEX IF NOT EXISTS ix_mcp_write_audit_ts
  ON mcp_write_audit (ts DESC);
CREATE INDEX IF NOT EXISTS ix_mcp_write_audit_table
  ON mcp_write_audit (target_table, ts DESC);

-- The read-only role can review the write history alongside everything else.
GRANT SELECT ON mcp_write_audit TO lifeos_reader;

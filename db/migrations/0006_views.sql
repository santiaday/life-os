-- 0006_views.sql
-- Read-only role for the MCP `ask_sql` tool. Defense-in-depth even for a
-- single-user setup — keeps Claude-generated SQL from being able to mutate
-- anything via the read connection pool.
--
-- The ${MCP_DB_PASSWORD} placeholder is substituted by db/apply.py from the
-- environment before this file is sent to the server. Generate one with:
--   openssl rand -hex 32
-- and put it in .env as MCP_DB_PASSWORD.

DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lifeos_reader') THEN
    CREATE ROLE lifeos_reader NOLOGIN;
  END IF;
END
$do$;

GRANT USAGE ON SCHEMA public TO lifeos_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO lifeos_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO lifeos_reader;

DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lifeos_mcp') THEN
    CREATE ROLE lifeos_mcp LOGIN PASSWORD '${MCP_DB_PASSWORD}' IN ROLE lifeos_reader;
  ELSE
    -- Idempotent password rotation: re-running the migration with a new env
    -- value will sync the role.
    EXECUTE format('ALTER ROLE lifeos_mcp WITH PASSWORD %L', '${MCP_DB_PASSWORD}');
  END IF;
END
$do$;

-- Re-grant SELECT explicitly in case new tables were added in earlier
-- migrations (idempotent).
GRANT SELECT ON ALL TABLES IN SCHEMA public TO lifeos_reader;

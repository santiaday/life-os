-- life-os has no browser/mobile client. Every caller is either a direct
-- Postgres role (lifeos_mcp, postgres) or uses the service_role key.
-- Before this, the anon + authenticated roles could SELECT/INSERT/UPDATE/DELETE
-- 129 of 130 public tables over PostgREST with only the publishable key.
--
-- Back-filled into the repo from supabase_migrations; applied 2026-10-20.
REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES    FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM anon, authenticated;

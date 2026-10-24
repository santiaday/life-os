-- 0072_withings_vault_secrets.sql
--
-- The Withings app credentials live in Vault (withings_client_id,
-- withings_client_secret), stored with vault.create_secret outside this file.
-- withings-ingest reads them through this function when the
-- WITHINGS_CLIENT_ID / WITHINGS_CLIENT_SECRET edge secrets aren't set.
-- Only service_role may call it, and only for withings_* names.

CREATE OR REPLACE FUNCTION withings_secret(p_name text)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, vault AS $$
  SELECT decrypted_secret FROM vault.decrypted_secrets
   WHERE name = p_name AND p_name LIKE 'withings\_%'
$$;
REVOKE EXECUTE ON FUNCTION withings_secret(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION withings_secret(text) TO service_role;

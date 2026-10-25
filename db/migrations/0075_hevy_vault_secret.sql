-- 0075_hevy_vault_secret.sql
--
-- The Hevy API key lives in Vault (hevy_api_key), stored with
-- vault.create_secret outside this file. hevy-sync reads it through this
-- function when the HEVY_API_KEY edge secret isn't set. service_role only.

CREATE OR REPLACE FUNCTION hevy_api_key()
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, vault AS $$
  SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'hevy_api_key'
$$;
REVOKE EXECUTE ON FUNCTION hevy_api_key() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION hevy_api_key() TO service_role;

-- Shared key for pg_cron -> edge functions. Generated in-database; plaintext
-- lives only in Vault, the hash in ingest_token.
--
-- Back-filled into the repo from supabase_migrations; applied 2026-10-20.
DO $$
DECLARE tok TEXT;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM vault.secrets WHERE name = 'cron_key') THEN
    tok := encode(extensions.gen_random_bytes(32), 'hex');
    PERFORM vault.create_secret(tok, 'cron_key', 'pg_cron -> edge function auth');
    INSERT INTO ingest_token (name, token_sha256)
    VALUES ('cron', encode(extensions.digest(tok, 'sha256'), 'hex'))
    ON CONFLICT (name) DO UPDATE SET token_sha256 = EXCLUDED.token_sha256, created_at = now();
  END IF;
END $$;

CREATE OR REPLACE FUNCTION call_edge_function(p_name TEXT, p_query TEXT DEFAULT '')
RETURNS BIGINT LANGUAGE sql SECURITY DEFINER SET search_path = public, extensions AS $$
  SELECT net.http_post(
    url := 'https://dqnrcarouldholuqfxbb.supabase.co/functions/v1/' || p_name || p_query,
    headers := jsonb_build_object('content-type', 'application/json',
               'x-cron-key', (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'cron_key')),
    body := '{}'::jsonb,
    timeout_milliseconds := 150000
  );
$$;
REVOKE EXECUTE ON FUNCTION call_edge_function(TEXT, TEXT) FROM PUBLIC, anon, authenticated;

SELECT cron.unschedule(jobid) FROM cron.job WHERE jobname = 'hevy-sync';
SELECT cron.schedule('hevy-sync', '7,37 * * * *', $$SELECT public.call_edge_function('hevy-sync')$$);

# Whoop Journal — Operations Runbook

The journal ingester pulls Whoop's private mobile journal API (yes/no/magnitude
prompts, free-text notes, Apple-Health autofill macros) into the warehouse. The
public Whoop OAuth API doesn't expose this data — only the iOS app does.

## Architecture

```
  iPhone Shortcut (5:30 AM daily)
        │
        ├─ POST api.prod.whoop.com/auth-service/v3/whoop
        │      AuthFlow=REFRESH_TOKEN_AUTH
        │      reads ~/iCloud/whoop_refresh_token.txt
        │
        ▼ (Whoop returns fresh AccessToken + RefreshToken)
  POST https://lifeos.<domain>/lifelog/whoop/refresh-callback
       X-Shared-Secret: $WHOOP_REFRESH_WEBHOOK_SECRET
       { access_token, refresh_token, id_token, expires_in }
        │
        ▼
  oauth_tokens(service='whoop_private')
        ▲
        │ (read)
  scheduler @ 5:35 AM
        └─ python -m ingest_whoop_journal
              │
              ├─ GET /journal-service/v3/journals/drafts/mobile/{day}
              │
              ▼
  raw_whoop_journal, fact_journal_day, fact_habit_log,
  fact_food_daily_apple_health, dim_whoop_behavior
```

The iPhone is the only thing that talks to Whoop's auth-service. The server is
a pure consumer. Two facts make this architecture necessary:

1. **Cloudflare hard-blocks the auth-service from servers.** Any POST to
   `api.prod.whoop.com/auth-service/*` from a non-iPhone client (regardless of
   TLS impersonation, headers, or cookies) returns 403 with the "you have been
   blocked" page.

2. **Direct AWS Cognito requires a SECRET_HASH.** Whoop's Cognito client
   (`37365lrcda1js3fapqfe2n40eh`) is configured as confidential, and we don't
   have the iOS app's `client_secret`. Both `USER_PASSWORD_AUTH` and (under
   some conditions) `REFRESH_TOKEN_AUTH` reject us at
   `cognito-idp.us-west-2.amazonaws.com`.

The iPhone has neither problem: Cloudflare allowlists real iOS-app traffic,
and Whoop's auth-service applies the SECRET_HASH server-side when proxying
to Cognito.

## One-time bootstrap

Run this once. After it succeeds, the daily iPhone Shortcut takes over.

### 1. Capture a token bundle from your Whoop iPhone app

Set up mitmproxy on your laptop, point your iPhone's Wi-Fi proxy at it,
install the mitm CA cert on your iPhone, then **fully sign out of the Whoop
app and sign back in** (you must trigger the password + SMS flow — not just
a launch — for `RespondToAuthChallenge` to fire).

Save the captured flows: in `mitmweb`, Files → Save Flows; or with the
terminal mitmproxy `:save_flows` command. You'll get a binary file (often
named `Mitmproxy_Flows`).

### 2. Apply the migration

```bash
psql "$SUPABASE_DB_URL_DIRECT" -f db/migrations/0014_whoop_journal_iphone_bridge.sql
```

### 3. Generate and set the webhook shared secret

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
# paste into .env as WHOOP_REFRESH_WEBHOOK_SECRET=
```

You'll also paste this same value into the iOS Shortcut (step 5).

### 4. Run the bootstrap CLI

```bash
docker compose run --rm -it ingest_whoop_journal \
    python -m ingest_whoop_journal.bootstrap_from_capture \
    /path/to/Mitmproxy_Flows
```

This:
- Parses the flow file, finds the `RespondToAuthChallenge` response with
  `AuthenticationResult` (the moment Cognito handed tokens back).
- Writes `AccessToken` / `RefreshToken` / `IdToken` to
  `oauth_tokens(service='whoop_private')`.
- Writes the bare refresh token to `./whoop_refresh_token.txt`.

Verify:

```bash
psql "$SUPABASE_DB_URL_DIRECT" -c \
    "SELECT service, expires_at FROM oauth_tokens WHERE service = 'whoop_private';"
# one row, expires_at ~24h from now
```

### 5. Configure the iOS Shortcut

Drop `whoop_refresh_token.txt` into iCloud Drive (anywhere — the Shortcut
references it by file path). Then build a Shortcut that:

1. **Get File** from iCloud Drive → `whoop_refresh_token.txt`
2. **Get Text from Input** (so the next action sees the literal string)
3. **Get Contents of URL**
   - URL: `https://api.prod.whoop.com/auth-service/v3/whoop`
   - Method: `POST`
   - Headers:
     - `Content-Type: application/x-amz-json-1.1`
     - `x-amz-target: AWSCognitoIdentityProviderService.InitiateAuth`
   - Request Body (JSON):
     ```json
     {"AuthFlow": "REFRESH_TOKEN_AUTH",
      "ClientId": "37365lrcda1js3fapqfe2n40eh",
      "AuthParameters": {"REFRESH_TOKEN": "<paste step-2 output>"}}
     ```
4. **Get Dictionary Value** → key `AuthenticationResult`
5. Build a JSON dict: `{"access_token": <AccessToken>, "refresh_token": <step-2 input — the existing refresh token>, "id_token": <IdToken>, "expires_in": <ExpiresIn>}`
6. **Get Contents of URL**
   - URL: `https://lifeos.<your-domain>/lifelog/whoop/refresh-callback`
   - Method: `POST`
   - Headers: `X-Shared-Secret: <paste WHOOP_REFRESH_WEBHOOK_SECRET>`,
     `Content-Type: application/json`
   - Request Body: the JSON dict from step 5

Set the Shortcut to run on a daily Personal Automation at **5:30 AM**.

### 6. Verify the loop end-to-end

Trigger the Shortcut once manually. Then:

```bash
docker compose run --rm ingest_whoop_journal \
    python -m ingest_whoop_journal --backfill 7
```

Expect logs showing per-day counts of `habit_log`, `journal_day`,
`food_daily_ah`. Spot-check the data:

```sql
SELECT day, jsonb_array_length(payload->'journal'->'tracked_behaviors') AS tracked
FROM raw_whoop_journal ORDER BY day DESC LIMIT 8;

SELECT day, source, COUNT(*) FROM fact_habit_log
WHERE day >= CURRENT_DATE - 7 GROUP BY 1,2 ORDER BY 1 DESC;
```

The next day at 5:35 AM the scheduler fires automatically.

## Failure modes

### `WhoopAuthExpired` raised by the ingester

The iPhone Shortcut hasn't run, ran but failed, or the network cut between
the Shortcut and the webhook. Check:

```sql
SELECT service, expires_at, updated_at FROM oauth_tokens WHERE service = 'whoop_private';
```

If `updated_at` is older than ~24h, the iPhone path is broken. Trigger the
Shortcut manually from your phone (long-press → Run); inspect its run log.

If the Shortcut keeps failing, the refresh token has rotated out (~30+ days
since last successful refresh). Re-run the bootstrap (capture a fresh
mitmproxy flow + run `bootstrap_from_capture` again).

### `403 Forbidden` from journal-service GETs

Whoop occasionally rotates their journal-service auth requirements. Check
the response body — if it's the Cloudflare "you have been blocked" page,
the gateway is now WAF'ing journal-service the same way it WAFs
auth-service. There's no quick fix; this would need an iPhone-side bridge
for journal-service GETs too. (At time of writing: journal-service accepts
plain bearer auth from anywhere.)

### `404` on every day

Either the access token isn't actually valid (Whoop's gateway returns 404
not 401 in some cases) or the date range predates your Whoop subscription.
First check the token row freshness; then try a known-good day directly:

```bash
docker compose run --rm ingest_whoop_journal \
    python -m ingest_whoop_journal --day 2026-05-04
```

### Webhook returns 401 from the Shortcut

The `X-Shared-Secret` header value doesn't match `WHOOP_REFRESH_WEBHOOK_SECRET`
on the server. They're case-sensitive and trim-sensitive — re-paste both.

### Webhook returns 400 ("doesn't look like a JWT")

The Shortcut is forwarding a Cloudflare challenge HTML page, not the
Cognito JSON. Either Whoop's gateway intermittently challenges the iPhone
(rare, but happens) or the Shortcut is unwrapping the response wrong. Add
a "Show Result" step before the final POST and inspect what the Shortcut
is about to send.

## Re-bootstrap (token rotation)

Refresh tokens last ~30 days. If you wake up to `WhoopAuthExpired` more
than ~28 days after the last bootstrap, repeat steps 1 and 4. The webhook
secret and the Shortcut configuration don't need to change — only the
sidecar file does.

## Follow-ups (not in this PR)

- `csv.py`: parallel CSV-export ingester as a reliability backstop (runs
  monthly via Whoop's web export). Not yet built.
- `mart_refresh` currently sources `mart_daily.journal_notes` from
  `raw_whoop_journal.payload`. Once `fact_journal_day` is verified populated,
  flip the source to `fact_journal_day.notes` (separate PR).

# Cal AI — Reverse-Engineering & Capture Runbook

Goal: make **Cal AI** (the photo-based calorie tracker) the warehouse's nutrition
source of record, replacing Cronometer. Cal AI has no public API, so we recover
its internal API the same way the Whoop journal was bootstrapped — a one-time
**mitmproxy capture** from your iPhone. You run the capture and hand me the flow
file; I reverse-engineer the auth + endpoints and finish the `ingest_calai`
package.

> **Status:** blocked on your capture. Everything below step 4 is what I build
> *after* you provide the flow. Steps 1–3 are yours.

---

## Why a capture is needed

Cal AI's app talks to a private backend (most of these apps use a custom API
behind a Firebase/Supabase/RevenueCat-issued bearer token). I can't see your
phone's HTTPS traffic, and there's no documented API or export. A 5-minute
mitmproxy session reveals (a) the API host, (b) how requests are authenticated,
and (c) the food-log + daily-summary endpoints and their JSON shapes — which is
everything I need to write a native ingester that fits this repo's
raw → fact → mart pattern.

---

## 1. Set up mitmproxy on your Mac

```bash
brew install mitmproxy        # if not already installed
mitmweb                       # starts the proxy + a web UI at http://127.0.0.1:8081
```

Leave it running. Note your Mac's LAN IP (`ipconfig getifaddr en0`).

## 2. Point your iPhone at the proxy + trust the CA

1. iPhone → Settings → Wi-Fi → (your network) → **Configure Proxy → Manual**.
   Server = your Mac's IP, Port = **8080**.
2. In Safari on the iPhone, visit **http://mitm.it** and install the **iOS**
   certificate profile.
3. Settings → General → About → **Certificate Trust Settings** → toggle
   **ON** for the mitmproxy cert. (This step is required — without it, TLS to
   Cal AI fails or shows nothing.)

> If Cal AI uses certificate pinning and step 3 isn't enough (you'll see
> connection errors in the app but no Cal AI flows in mitmweb), tell me — there
> are workarounds (a jailbroken device, Frida/objection unpinning, or grabbing
> the token from the app container), but most of these calorie apps are **not**
> pinned, so try the simple path first.

## 3. Exercise the app, then save the flows

With mitmweb capturing, in the Cal AI app:

1. **Fully sign out and sign back in** (so the login/token-refresh request is
   captured — this is the auth we need to replicate).
2. Open your **diary / today view** so it loads logged meals.
3. **Scroll back several days/weeks** of history (this triggers the food-log
   list/history endpoint with date params — exactly what the ingester paginates).
4. Open **one meal's detail** (per-item macros).
5. Optionally log a new item so we see the write shape (we only read, but it
   helps map fields).

Then in mitmweb: **File → Save** (or filter to the Cal AI host first via the
search bar, then save the filtered set). You'll get a binary flows file.

**Hand me:** the saved flows file (or, if you'd rather not share the whole
capture, a HAR/JSON export of just the Cal AI host's requests+responses). Put it
somewhere I can read, e.g. `~/Documents/LifeOS/calai_flows`, and tell me the
path.

### What I'm looking for in the capture
- The **API host** (e.g. `api.cal-ai.app`, a Supabase project URL, or similar).
- The **auth header** on data requests (`Authorization: Bearer …`) and how it's
  obtained/refreshed (Firebase `securetoken.googleapis.com`, Supabase
  `/auth/v1/token`, RevenueCat, or a custom `/login`).
- The **food-log endpoint**: how it's paginated/date-filtered, and the JSON
  fields for each logged item (calories, protein, carbs, fat, fiber, timestamp,
  meal type, food name, photo id).
- The **daily-summary endpoint** if one exists (daily macro totals + targets).

---

## 4. What I build once I have the flow (no action needed from you)

Following the standard ingester pattern (`ingest_cronometer` / `ingest_hevy`):

- **`ingest_calai/`** package: `client.py` (auth + paginated fetch),
  `transforms.py` (Cal AI JSON → fact rows), `ingest.py` (three-pass upsert +
  `run_all` + `--backfill`), `__main__.py`, `Dockerfile`.
- **Auth**: replicate whatever the capture shows. If it's a refreshable bearer,
  the token bundle is stored in `oauth_tokens(service='calai')` and refreshed
  natively (or, if it needs an iPhone bridge like Whoop, a refresh-callback
  webhook — we'll know from the capture).
- **Migration `0026_calai_nutrition.sql`**:
  - `raw_calai_meal` / `raw_calai_daily` raw JSONB tables (natural keys = Cal AI
    item/day ids).
  - `ALTER TABLE fact_food_log / fact_food_daily ADD COLUMN IF NOT EXISTS
    source TEXT DEFAULT 'cronometer'` so rows are source-attributable.
- **Transforms** map Cal AI meals → `fact_food_log` (`eaten_at`, `meal_group`,
  `food_name`, `energy_kcal`, `protein_g`, `carbs_g`, `fat_g`, `fiber_g`, …) and
  daily totals → `fact_food_daily`, all tagged `source='calai'`. Because these
  are the **same tables Cronometer wrote**, `mart_refresh`, `mart_daily`,
  `mart_meal`, and every nutrition MCP tool keep working with zero changes.

## 5. Cutover (after Cal AI backfill is verified)

- Add Cal AI scheduler jobs (daily + weekly rebackfill), mirroring the
  Cronometer slots in `scheduler/__main__.py`.
- **Retire** the two Cronometer jobs (`cronometer_daily`, `cronometer_weekly`).
  Keep the `ingest_cronometer` code and historical rows (`source='cronometer'`)
  for archival — just stop scheduling it.
- Run `python -m ingest_calai ingest --backfill 365` + `python -m mart_refresh`,
  then spot-check `mart_daily.total_kcal` and `mart_meal` against the Cal AI app
  for a few days.

---

## Notes
- We only ever **read** from Cal AI. No writes back to their service.
- If the capture shows the data is also written to **Apple Health** (many of
  these apps do), that's a no-capture fallback for daily totals — but it lacks
  per-meal/photo detail, so the direct API is preferred.

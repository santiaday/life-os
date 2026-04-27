# life-os — Build Spec

> A personal life-data warehouse + Claude MCP server. Ingests Whoop, Google
> Calendar, Cronometer, and Copilot Money into Supabase Postgres on a three-layer
> schema (raw → fact → mart), then exposes a small set of semantic tools via a
> public HTTP MCP server consumed as a Claude.ai custom connector.
>
> **Single user.** No multi-tenancy, no orgs, no auth UI. Server-side service
> role key for DB writes. MCP endpoint protected by a static API key.

---

## 0. Decisions already made (do not re-litigate)

- **Runtime:** New isolated DigitalOcean droplet, Docker Compose. Image registry: `ghcr.io/<santi-username>/life-os-*`.
- **DB:** New dedicated Supabase project (`life-os`). All app tables live in `public` schema. RLS off (single user, server-side only).
- **MCP transport:** Streamable HTTP, public endpoint behind Caddy/HTTPS, single static API key in the `Authorization: Bearer <key>` header.
- **Language:** Python 3.12 across all services. FastAPI for HTTP, `psycopg[binary,pool]` for DB, `pydantic v2` for models, `apscheduler` for cron, `httpx` for outbound, `structlog` for logging.
- **Cronometer:** Use [`cronometer-export`](https://github.com/jrmycanady/cronometer-export) Go binary (CLI from same author as `gocronometer`). Build it in a multi-stage Dockerfile and shell out from Python via `subprocess`. Do not port to Python.
- **Schema pattern:** `raw_*` (immutable JSONB), `fact_*` / `dim_*` (typed), `mart_*` (denormalized rollups). Identical to Ledion conventions.
- **Idempotency:** All ingesters upsert by natural key. Re-runs are safe and free.
- **Timezone:** Store all timestamps as `TIMESTAMPTZ` in UTC. Mart-layer `date` columns are computed in `America/New_York` (configurable via env `LOCAL_TZ`).

---

## 1. Repository layout

Single monorepo. Services share a `lifeos_core` library for DB access, schemas, logging.

```
life-os/
├── README.md
├── SPEC.md                      ← this file
├── docker-compose.yml
├── Caddyfile
├── .env.example
├── pyproject.toml               ← uv / pip workspace root
├── lifeos_core/                 ← shared library
│   ├── __init__.py
│   ├── db.py                    ← psycopg pool, transaction helpers
│   ├── settings.py              ← pydantic-settings, loads .env
│   ├── logging.py               ← structlog config
│   ├── tz.py                    ← LOCAL_TZ helpers
│   ├── upsert.py                ← generic upsert helpers
│   └── models/                  ← pydantic models per table (typed reads)
├── ingest_whoop/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── ingest_whoop/
│   │   ├── __main__.py          ← CLI entrypoint, supports --backfill
│   │   ├── oauth.py
│   │   ├── client.py
│   │   ├── ingest.py            ← raw → fact transforms
│   │   └── webhooks.py          ← FastAPI app for Whoop webhooks
├── ingest_calendar/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── ingest_calendar/
│   │   ├── __main__.py
│   │   ├── oauth.py
│   │   ├── client.py
│   │   └── ingest.py
├── ingest_cronometer/
│   ├── Dockerfile               ← multi-stage: golang builder → python runtime
│   ├── pyproject.toml
│   ├── ingest_cronometer/
│   │   ├── __main__.py
│   │   ├── exporter.py          ← subprocess wrapper for cronometer-export
│   │   └── ingest.py            ← parses CSV → raw + fact tables
├── ingest_copilot/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── ingest_copilot/
│   │   ├── __main__.py
│   │   ├── auth.py              ← JWT refresh / login
│   │   ├── graphql.py           ← schema-versioned client
│   │   └── ingest.py
├── mart_refresh/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── mart_refresh/
│   │   ├── __main__.py
│   │   └── refresh.py           ← rebuilds mart_* tables from fact_*
├── mcp_server/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── mcp_server/
│   │   ├── __main__.py          ← uvicorn entrypoint
│   │   ├── server.py            ← FastAPI + MCP streamable HTTP
│   │   ├── auth.py              ← Bearer token middleware
│   │   ├── tools.py             ← tool definitions & SQL behind each
│   │   └── schema_docs.py       ← embedded schema documentation
├── scheduler/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── scheduler/
│   │   └── __main__.py          ← APScheduler with all jobs declared
├── db/
│   ├── migrations/              ← sqitch-style ordered SQL files
│   │   ├── 0001_init.sql
│   │   ├── 0002_raw_tables.sql
│   │   ├── 0003_fact_tables.sql
│   │   ├── 0004_mart_tables.sql
│   │   ├── 0005_indexes.sql
│   │   └── 0006_views.sql
│   └── apply.py                 ← simple migration runner
└── tests/
    ├── conftest.py
    ├── test_whoop_transform.py
    ├── test_calendar_derive.py
    ├── test_cronometer_parse.py
    ├── test_copilot_parse.py
    ├── test_mart_refresh.py
    └── test_mcp_tools.py
```

---

## 2. Environment variables

All services read from a shared `.env` mounted into each container.

```env
# DB
SUPABASE_DB_URL=postgresql://postgres.xxx:<pwd>@aws-0-us-east-1.pooler.supabase.com:6543/postgres
LOCAL_TZ=America/New_York

# Whoop
WHOOP_CLIENT_ID=
WHOOP_CLIENT_SECRET=
WHOOP_REDIRECT_URI=https://lifeos.<santi-domain>/oauth/whoop/callback
WHOOP_REFRESH_TOKEN=             # written by oauth bootstrap, read on every run
WHOOP_WEBHOOK_SECRET=

# Google Calendar
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=https://lifeos.<santi-domain>/oauth/google/callback
GOOGLE_REFRESH_TOKEN=
GOOGLE_CALENDAR_IDS=primary,work@example.com   # comma-separated
INTERNAL_EMAIL_DOMAINS=doorloop.com            # for internal/external classification

# Cronometer
CRONOMETER_USERNAME=
CRONOMETER_PASSWORD=

# Copilot Money
COPILOT_EMAIL=
COPILOT_PASSWORD=                # for re-auth when refresh fails
COPILOT_REFRESH_TOKEN=

# MCP
MCP_API_KEY=                     # generate: openssl rand -hex 32
MCP_PUBLIC_BASE_URL=https://lifeos.<santi-domain>

# Logging
LOG_LEVEL=INFO
SENTRY_DSN=                      # optional
```

Secrets in production live in DigitalOcean Spaces (encrypted) or just the
droplet `.env` with `chmod 600`. No need for Vault for one user.

---

## 3. Database schema

Apply migrations in numeric order via `db/apply.py`. Schema below is the **canonical reference** — all DDL must match.

### 3.1 Init (`0001_init.sql`)

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Single source of truth for "what day did this happen" in local tz
CREATE OR REPLACE FUNCTION local_date(ts TIMESTAMPTZ, tz TEXT DEFAULT 'America/New_York')
RETURNS DATE LANGUAGE SQL IMMUTABLE AS $$
  SELECT (ts AT TIME ZONE tz)::DATE
$$;

-- Generic ingestion log — every fetch creates a row, success or not
CREATE TABLE ingestion_runs (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,            -- 'whoop' | 'calendar' | 'cronometer' | 'copilot'
  data_type TEXT NOT NULL,         -- 'recovery' | 'sleep' | 'events' | ...
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'running',  -- running | success | failure
  rows_fetched INT,
  rows_upserted INT,
  error_message TEXT,
  metadata JSONB
);

CREATE INDEX ix_ingestion_runs_source_started ON ingestion_runs(source, started_at DESC);
```

### 3.2 Raw tables (`0002_raw_tables.sql`)

Pattern: every raw table has `(id, fetched_at, natural_key, payload jsonb)`. `natural_key` is whatever uniquely identifies the row from the source. Upsert on `natural_key`. Payload is whatever the API returned, untouched.

```sql
-- Whoop
CREATE TABLE raw_whoop_recovery (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  cycle_id BIGINT NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE raw_whoop_sleep (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  sleep_id UUID NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE raw_whoop_workout (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  workout_id UUID NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE raw_whoop_cycle (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  cycle_id BIGINT NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE raw_whoop_profile (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  payload JSONB NOT NULL
);

-- Calendar
CREATE TABLE raw_calendar_event (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  calendar_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  etag TEXT,
  payload JSONB NOT NULL,
  UNIQUE(calendar_id, event_id)
);

-- Cronometer (one row per export day, payload is the full CSV-parsed JSON)
CREATE TABLE raw_cronometer_servings (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  day DATE NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE raw_cronometer_daily_nutrition (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  day DATE NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE raw_cronometer_biometrics (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  day DATE NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE raw_cronometer_exercises (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  day DATE NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

-- Copilot Money
CREATE TABLE raw_copilot_transaction (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  transaction_id TEXT NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE raw_copilot_account (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  account_id TEXT NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE TABLE raw_copilot_category (
  id BIGSERIAL PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  category_id TEXT NOT NULL UNIQUE,
  payload JSONB NOT NULL
);
```

### 3.3 Fact + dim tables (`0003_fact_tables.sql`)

Typed columns, derived from `raw_*` on every ingestion run. **Dropping and recreating fact rows from raw is always safe** — raw is the source of truth.

```sql
-- Whoop physiological cycle = one "Whoop day" (~24h, doesn't align to midnight)
CREATE TABLE fact_cycle (
  cycle_id BIGINT PRIMARY KEY,
  start_ts TIMESTAMPTZ NOT NULL,
  end_ts TIMESTAMPTZ,
  day DATE GENERATED ALWAYS AS (local_date(start_ts)) STORED,
  scaled_strain NUMERIC(5,2),
  day_kilojoules INT,
  avg_heart_rate INT,
  max_heart_rate INT,
  raw_id BIGINT REFERENCES raw_whoop_cycle(id),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE fact_recovery (
  cycle_id BIGINT PRIMARY KEY,
  sleep_id UUID,
  day DATE NOT NULL,                  -- local_date of cycle start
  recovery_score INT,                 -- 0-100
  hrv_rmssd_ms NUMERIC(6,2),          -- ms, NOT seconds (convert from API)
  resting_heart_rate INT,
  spo2_percentage NUMERIC(5,2),
  skin_temp_celsius NUMERIC(4,2),
  user_calibrating BOOLEAN,
  raw_id BIGINT REFERENCES raw_whoop_recovery(id),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE fact_sleep (
  sleep_id UUID PRIMARY KEY,
  start_ts TIMESTAMPTZ NOT NULL,
  end_ts TIMESTAMPTZ NOT NULL,
  day DATE GENERATED ALWAYS AS (local_date(end_ts)) STORED,  -- assigned to wake-up day
  is_nap BOOLEAN NOT NULL DEFAULT FALSE,
  total_in_bed_min NUMERIC(6,1),
  total_awake_min NUMERIC(6,1),
  total_light_min NUMERIC(6,1),
  total_slow_wave_min NUMERIC(6,1),
  total_rem_min NUMERIC(6,1),
  sleep_cycle_count INT,
  disturbance_count INT,
  sleep_performance_pct NUMERIC(5,2),
  sleep_consistency_pct NUMERIC(5,2),
  sleep_efficiency_pct NUMERIC(5,2),
  raw_id BIGINT REFERENCES raw_whoop_sleep(id),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE fact_workout (
  workout_id UUID PRIMARY KEY,
  start_ts TIMESTAMPTZ NOT NULL,
  end_ts TIMESTAMPTZ NOT NULL,
  day DATE GENERATED ALWAYS AS (local_date(start_ts)) STORED,
  sport_id INT,
  sport_name TEXT,
  strain NUMERIC(5,2),
  kilojoules INT,
  avg_heart_rate INT,
  max_heart_rate INT,
  distance_meters NUMERIC(10,2),
  altitude_gain_meters NUMERIC(8,2),
  altitude_change_meters NUMERIC(8,2),
  zone_zero_min NUMERIC(6,1),
  zone_one_min NUMERIC(6,1),
  zone_two_min NUMERIC(6,1),
  zone_three_min NUMERIC(6,1),
  zone_four_min NUMERIC(6,1),
  zone_five_min NUMERIC(6,1),
  raw_id BIGINT REFERENCES raw_whoop_workout(id),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Calendar
CREATE TABLE fact_calendar_event (
  calendar_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  start_ts TIMESTAMPTZ NOT NULL,
  end_ts TIMESTAMPTZ NOT NULL,
  day DATE GENERATED ALWAYS AS (local_date(start_ts)) STORED,
  duration_min NUMERIC(6,1) GENERATED ALWAYS AS (
    EXTRACT(EPOCH FROM (end_ts - start_ts)) / 60.0
  ) STORED,
  title TEXT,
  status TEXT,                         -- confirmed | tentative | cancelled
  organizer_email TEXT,
  organizer_self BOOLEAN,
  attendee_count INT NOT NULL DEFAULT 0,
  attendee_internal_count INT NOT NULL DEFAULT 0,
  attendee_external_count INT NOT NULL DEFAULT 0,
  is_recurring BOOLEAN NOT NULL DEFAULT FALSE,
  recurring_event_id TEXT,
  is_all_day BOOLEAN NOT NULL DEFAULT FALSE,
  has_video_link BOOLEAN NOT NULL DEFAULT FALSE,
  location TEXT,
  visibility TEXT,
  response_status TEXT,                -- accepted | declined | tentative | needsAction
  classification TEXT,                 -- 'meeting' | 'focus' | 'personal' | 'all_day' (derived)
  raw_id BIGINT REFERENCES raw_calendar_event(id),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (calendar_id, event_id)
);

-- Cronometer
-- One row per food serving eaten with a timestamp
CREATE TABLE fact_food_log (
  id BIGSERIAL PRIMARY KEY,
  eaten_at TIMESTAMPTZ NOT NULL,           -- from Cronometer's per-meal timestamp (Gold required)
  day DATE GENERATED ALWAYS AS (local_date(eaten_at)) STORED,
  meal_group TEXT,                          -- Cronometer's group: Breakfast, Lunch, Dinner, Snack, Uncategorized
  food_name TEXT NOT NULL,
  amount NUMERIC(10,3),
  unit TEXT,
  -- Macros
  energy_kcal NUMERIC(10,2),
  protein_g NUMERIC(10,3),
  carbs_g NUMERIC(10,3),
  net_carbs_g NUMERIC(10,3),
  fiber_g NUMERIC(10,3),
  sugar_g NUMERIC(10,3),
  fat_g NUMERIC(10,3),
  saturated_fat_g NUMERIC(10,3),
  -- Common micros (full set lives in micros JSONB below)
  sodium_mg NUMERIC(10,2),
  potassium_mg NUMERIC(10,2),
  caffeine_mg NUMERIC(10,2),
  alcohol_g NUMERIC(10,3),
  -- Full nutrient set as JSONB for queries that need micros
  micros JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_row_hash TEXT NOT NULL UNIQUE,    -- sha256(day || eaten_at || food_name || amount || unit)
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_food_log_day ON fact_food_log(day);
CREATE INDEX ix_food_log_eaten_at ON fact_food_log(eaten_at);

-- Cronometer's own daily totals (preferred over re-summing fact_food_log
-- since Cronometer's nutrient calc handles edge cases like recipes)
CREATE TABLE fact_food_daily (
  day DATE PRIMARY KEY,
  energy_kcal NUMERIC(10,2),
  protein_g NUMERIC(10,3),
  carbs_g NUMERIC(10,3),
  net_carbs_g NUMERIC(10,3),
  fiber_g NUMERIC(10,3),
  fat_g NUMERIC(10,3),
  saturated_fat_g NUMERIC(10,3),
  sodium_mg NUMERIC(10,2),
  alcohol_g NUMERIC(10,3),
  caffeine_mg NUMERIC(10,2),
  micros JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Tall format: weight, blood pressure, blood sugar, body fat, etc.
CREATE TABLE fact_biometric (
  id BIGSERIAL PRIMARY KEY,
  measured_at TIMESTAMPTZ NOT NULL,
  day DATE GENERATED ALWAYS AS (local_date(measured_at)) STORED,
  metric TEXT NOT NULL,                    -- e.g. 'weight', 'systolic_bp', 'fasting_glucose'
  value NUMERIC(12,4) NOT NULL,
  unit TEXT NOT NULL,
  note TEXT,
  source TEXT NOT NULL DEFAULT 'cronometer',
  source_row_hash TEXT NOT NULL UNIQUE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_biometric_metric_day ON fact_biometric(metric, day);

-- Copilot Money
CREATE TABLE dim_account (
  account_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  institution TEXT,
  type TEXT,                               -- checking | savings | credit | investment | loan
  currency TEXT NOT NULL DEFAULT 'USD',
  is_hidden BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE dim_category (
  category_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  parent_category_id TEXT REFERENCES dim_category(category_id),
  type TEXT,                               -- expense | income | transfer
  is_hidden BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE fact_transaction (
  transaction_id TEXT PRIMARY KEY,
  date DATE NOT NULL,
  posted_ts TIMESTAMPTZ,
  amount NUMERIC(14,2) NOT NULL,           -- positive = expense, negative = income/refund (match Copilot convention)
  currency TEXT NOT NULL DEFAULT 'USD',
  merchant TEXT,
  description TEXT,
  category_id TEXT REFERENCES dim_category(category_id),
  account_id TEXT REFERENCES dim_account(account_id),
  is_pending BOOLEAN NOT NULL DEFAULT FALSE,
  is_recurring BOOLEAN NOT NULL DEFAULT FALSE,
  is_excluded BOOLEAN NOT NULL DEFAULT FALSE,  -- Copilot's "exclude from totals"
  notes TEXT,
  raw_id BIGINT REFERENCES raw_copilot_transaction(id),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_transaction_date ON fact_transaction(date);
CREATE INDEX ix_transaction_category ON fact_transaction(category_id, date);
```

### 3.4 Mart tables (`0004_mart_tables.sql`)

These are the analysis-ready tables. **Most MCP tool queries hit mart, not fact.** Refresh by truncate-and-rebuild from fact in `mart_refresh`. Cheap because we're talking thousands of rows, not millions.

```sql
-- The cross-source daily table — one row per day, every metric available
CREATE TABLE mart_daily (
  day DATE PRIMARY KEY,
  -- Whoop recovery
  recovery_score INT,
  hrv_rmssd_ms NUMERIC(6,2),
  resting_heart_rate INT,
  spo2_percentage NUMERIC(5,2),
  skin_temp_celsius NUMERIC(4,2),
  -- Whoop sleep (primary nightly sleep, not naps)
  sleep_total_hours NUMERIC(4,2),
  sleep_rem_hours NUMERIC(4,2),
  sleep_slow_wave_hours NUMERIC(4,2),
  sleep_efficiency_pct NUMERIC(5,2),
  sleep_performance_pct NUMERIC(5,2),
  sleep_consistency_pct NUMERIC(5,2),
  sleep_start_ts TIMESTAMPTZ,
  sleep_end_ts TIMESTAMPTZ,
  nap_count INT NOT NULL DEFAULT 0,
  nap_total_min NUMERIC(6,1) NOT NULL DEFAULT 0,
  -- Whoop strain
  strain NUMERIC(5,2),
  day_kilojoules INT,
  -- Whoop workouts
  workout_count INT NOT NULL DEFAULT 0,
  workout_total_min NUMERIC(6,1) NOT NULL DEFAULT 0,
  workout_total_kj INT NOT NULL DEFAULT 0,
  workout_max_strain NUMERIC(5,2),
  -- Calendar
  meeting_count INT NOT NULL DEFAULT 0,
  meeting_hours NUMERIC(5,2) NOT NULL DEFAULT 0,
  meeting_internal_hours NUMERIC(5,2) NOT NULL DEFAULT 0,
  meeting_external_hours NUMERIC(5,2) NOT NULL DEFAULT 0,
  first_meeting_time TIME,
  last_meeting_time TIME,
  longest_focus_block_min NUMERIC(6,1),
  total_focus_block_min NUMERIC(6,1),
  -- Food
  total_kcal NUMERIC(10,2),
  protein_g NUMERIC(10,3),
  carbs_g NUMERIC(10,3),
  fat_g NUMERIC(10,3),
  fiber_g NUMERIC(10,3),
  alcohol_g NUMERIC(10,3),
  caffeine_mg NUMERIC(10,2),
  meal_count INT,
  first_meal_time TIME,
  last_meal_time TIME,
  eating_window_hours NUMERIC(4,2),
  breakfast_kcal NUMERIC(10,2),
  lunch_kcal NUMERIC(10,2),
  dinner_kcal NUMERIC(10,2),
  snack_kcal NUMERIC(10,2),
  -- Spending
  total_spend NUMERIC(12,2),
  food_spend NUMERIC(12,2),
  restaurant_spend NUMERIC(12,2),
  groceries_spend NUMERIC(12,2),
  transportation_spend NUMERIC(12,2),
  -- Body
  weight_kg NUMERIC(5,2),
  body_fat_pct NUMERIC(4,2),
  -- Meta
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-meal rollup
CREATE TABLE mart_meal (
  id BIGSERIAL PRIMARY KEY,
  day DATE NOT NULL,
  meal_window TEXT NOT NULL,           -- 'breakfast' | 'lunch' | 'dinner' | 'snack'
  start_ts TIMESTAMPTZ NOT NULL,
  end_ts TIMESTAMPTZ NOT NULL,
  duration_min NUMERIC(6,1),
  item_count INT NOT NULL,
  total_kcal NUMERIC(10,2),
  protein_g NUMERIC(10,3),
  carbs_g NUMERIC(10,3),
  fat_g NUMERIC(10,3),
  fiber_g NUMERIC(10,3),
  food_names TEXT[] NOT NULL,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_mart_meal_day_window ON mart_meal(day, meal_window);

-- Weekly rollup for trend questions
CREATE TABLE mart_weekly (
  week_start DATE PRIMARY KEY,         -- Monday of the week
  avg_recovery_score NUMERIC(5,2),
  avg_hrv_rmssd_ms NUMERIC(6,2),
  avg_rhr INT,
  total_strain NUMERIC(7,2),
  total_workout_min NUMERIC(7,1),
  total_meeting_hours NUMERIC(6,2),
  avg_meeting_hours_per_workday NUMERIC(5,2),
  total_kcal NUMERIC(12,2),
  avg_kcal_per_day NUMERIC(10,2),
  avg_protein_g NUMERIC(8,2),
  total_spend NUMERIC(12,2),
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 3.5 Indexes (`0005_indexes.sql`)

Beyond the inline indexes already declared above, add:

```sql
CREATE INDEX ix_calendar_event_day ON fact_calendar_event(day);
CREATE INDEX ix_calendar_event_classification ON fact_calendar_event(classification, day);
CREATE INDEX ix_workout_day_sport ON fact_workout(day, sport_name);
CREATE INDEX ix_recovery_day ON fact_recovery(day);
CREATE INDEX ix_sleep_day ON fact_sleep(day);
CREATE INDEX ix_food_log_meal_group ON fact_food_log(meal_group, day);
CREATE INDEX ix_transaction_pending ON fact_transaction(is_pending) WHERE is_pending;
```

### 3.6 Read-only views for the SQL escape hatch (`0006_views.sql`)

The MCP `ask_sql` tool runs against a dedicated read-only role on a curated set of views. This is a defense-in-depth measure even though it's just Santi — prevents Claude from generating destructive SQL.

```sql
CREATE ROLE lifeos_reader NOLOGIN;
GRANT USAGE ON SCHEMA public TO lifeos_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO lifeos_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO lifeos_reader;

-- A login role inheriting reader permissions
CREATE ROLE lifeos_mcp LOGIN PASSWORD :'mcp_db_password' IN ROLE lifeos_reader;
```

The MCP server connects with `lifeos_mcp` credentials for `ask_sql` and the
service-role connection for everything else (mart writes happen in the
mart_refresh service, not from MCP).

---

## 4. Ingestion services

Common conventions for every ingester:

1. **CLI:** Each service has `python -m ingest_<source> [--backfill DAYS] [--data-type X]`. Default behavior is incremental (since last successful run).
2. **Idempotent upserts:** Always `ON CONFLICT (natural_key) DO UPDATE`. Re-running is free.
3. **Run logging:** Open `ingestion_runs` row at start, close it at end with status, row counts, and any error.
4. **Strict raw → fact:** Raw upserts happen first. Fact upserts read from raw. If fact transform fails, raw is still saved — we can replay.
5. **Backoff:** httpx with `tenacity` retries on 429/5xx, exponential backoff, max 3 retries.

### 4.1 `ingest_whoop`

**Reference:** https://developer.whoop.com/api

OAuth scopes required: `read:recovery read:cycles read:sleep read:workout read:profile read:body_measurement offline`.

Bootstrap flow (one-time):
1. `python -m ingest_whoop oauth-init` — prints authorize URL.
2. User visits URL, approves, gets redirected with `?code=...`.
3. `python -m ingest_whoop oauth-exchange --code <code>` — exchanges for access + refresh, writes refresh to `.env` (or to `whoop_oauth` table — see below).

Token storage: don't trust `.env` for refresh tokens since they rotate. Add a small table:

```sql
CREATE TABLE oauth_tokens (
  service TEXT PRIMARY KEY,               -- 'whoop' | 'google' | 'copilot'
  access_token TEXT,
  refresh_token TEXT NOT NULL,
  expires_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Add to `0001_init.sql`. All OAuth services use this.

**Endpoints to call (v2):**
- `GET /developer/v2/cycle?start=...&end=...&limit=25` (paginated)
- `GET /developer/v2/recovery?start=...&end=...&limit=25`
- `GET /developer/v2/activity/sleep?start=...&end=...&limit=25`
- `GET /developer/v2/activity/workout?start=...&end=...&limit=25`
- `GET /developer/v2/user/profile/basic`
- `GET /developer/v2/user/measurement/body`

**Pagination:** Whoop uses `nextToken` cursor. Iterate until empty.

**Transform notes:**
- HRV from API is in **seconds** as a decimal (e.g. `0.0778`). Multiply by 1000 to store ms in `fact_recovery.hrv_rmssd_ms`. Verify with one known sample at runtime — fail loudly if values look wrong-by-1000x after deploy.
- Sleep stages come as totals in milliseconds. Divide by 60_000 for minutes.
- Recovery is keyed to a `cycle_id` AND a `sleep_id`. Both are useful — store both. `day` on `fact_recovery` is the local date of the cycle's `start`.
- Naps are returned in the sleep endpoint with `nap: true`. Don't overwrite the primary night's sleep with naps.

**Webhooks (optional, phase 3):** `webhooks.py` runs as part of the MCP container, exposes `POST /webhooks/whoop`. Verify signature with `WHOOP_WEBHOOK_SECRET`. On event, queue a small fetch of just that record. Phase-3 nice-to-have, not blocking.

**Schedule:** Hourly incremental from "last cycle.start_ts". Plus a daily 6am full re-fetch of last 3 days (Whoop sometimes back-fills/edits older recoveries when you sync).

### 4.2 `ingest_calendar`

**Reference:** https://developers.google.com/calendar/api/v3/reference

Scopes: `https://www.googleapis.com/auth/calendar.readonly`.

Same OAuth flow as Whoop — store refresh token in `oauth_tokens`.

**Endpoints:**
- `GET /calendar/v3/calendars/{calendarId}/events?timeMin=...&timeMax=...&singleEvents=true&pageToken=...`
- Use `singleEvents=true` to expand recurring events into individual occurrences.

**Sync token strategy (preferred):**
- First call: full sync over the configured date window (e.g. last 90 days, next 30 days).
- Save returned `nextSyncToken` per `calendar_id` in a small `calendar_sync_state` table.
- Subsequent calls: pass `syncToken` to get only changed events. On 410 GONE, do a full resync.

```sql
CREATE TABLE calendar_sync_state (
  calendar_id TEXT PRIMARY KEY,
  sync_token TEXT,
  last_full_sync_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Derived fields for fact_calendar_event:**
- `attendee_internal_count`: attendees whose email domain is in `INTERNAL_EMAIL_DOMAINS`.
- `attendee_external_count`: rest. Self does not count.
- `has_video_link`: true if `hangoutLink` set or any `entryPoint.entryPointType == 'video'`.
- `is_all_day`: `start.date` set instead of `start.dateTime`. Set `start_ts = date 00:00 LOCAL_TZ`, `end_ts = end.date 00:00 LOCAL_TZ`.
- `response_status`: my own response from `attendees[].self == true`.
- **`classification`**: derive with this priority:
  1. If `is_all_day` → `'all_day'`
  2. If response_status == 'declined' → `'declined'` (excluded from meeting load)
  3. If attendee_count <= 1 (just me) AND title matches `/(focus|deep work|block|do not schedule|dns)/i` → `'focus'`
  4. If attendee_count >= 2 → `'meeting'`
  5. Else → `'personal'`

**Schedule:** Every 30 minutes. Calendar changes happen all day.

### 4.3 `ingest_cronometer`

This is the trickiest source. Approach:

1. **Build `cronometer-export` Go binary in a multi-stage Dockerfile:**

```dockerfile
# Stage 1: build the Go binary
FROM golang:1.22-alpine AS builder
RUN apk add --no-cache git
RUN git clone https://github.com/jrmycanady/cronometer-export.git /src
WORKDIR /src
RUN go build -o /out/cronometer-export .

# Stage 2: python runtime
FROM python:3.12-slim
COPY --from=builder /out/cronometer-export /usr/local/bin/cronometer-export
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir .
COPY ingest_cronometer ./ingest_cronometer
CMD ["python", "-m", "ingest_cronometer"]
```

2. **Wrapper (`exporter.py`):** subprocess call:

```python
def export(data_type: str, start: date, end: date) -> str:
    """Returns CSV string. data_type in {servings, daily-nutrition, exercises, biometrics, notes}."""
    result = subprocess.run([
        "cronometer-export",
        "-u", settings.CRONOMETER_USERNAME,
        "-p", settings.CRONOMETER_PASSWORD,
        "-t", data_type,
        "-s", start.isoformat(),
        "-e", end.isoformat(),
    ], capture_output=True, text=True, check=True, timeout=60)
    return result.stdout
```

3. **Parsing:** servings CSV has columns roughly:
   `Day,Time,Group,Food Name,Amount,Unit,Energy (kcal),Protein (g),...`
   Read with `csv.DictReader`. Build `eaten_at = day + time` (already local TZ from Cronometer — convert to UTC using `LOCAL_TZ`). Compute `source_row_hash = sha256(f"{day}|{time}|{food_name}|{amount}|{unit}")`.

4. **Strategy:**
   - Daily run at 3am local time, fetch yesterday + day-before-yesterday (window of 2 days handles late-night edits).
   - Weekly run on Sunday at 3am, fetch last 14 days (catch late corrections).
   - Backfill mode: `python -m ingest_cronometer --backfill 365` to bootstrap initial year of history.

5. **Auth fragility:** the Go binary uses Cronometer's GWT API which can break with their app updates. Wrap subprocess calls in clear error logging. If the binary returns non-zero, log full stderr to `ingestion_runs.error_message` and **don't crash the scheduler** — alert and continue.

6. **Cronometer's micros are dozens of columns.** Parse the macro columns into typed fact columns; pack everything else into `fact_food_log.micros` JSONB. Same for `fact_food_daily`.

7. **Biometrics:** `Date,Metric,Amount,Unit`. Map to `fact_biometric` directly.

### 4.4 `ingest_copilot`

Santi has already mapped the GraphQL endpoint. Reverse-engineered, fragile. Treat with care.

**Endpoint:** `https://app.copilot.money/api/graphql`
**Auth:** Bearer JWT. Refresh token flow.

**Auth module:**
- Try refresh first. If 401, fall back to email/password login (Cognito-backed). Persist new refresh token to `oauth_tokens`.
- Never log full tokens. Mask in logs.

**Queries to run (one each):**
- `transactions(startDate, endDate)` — fetch last 35 days every run, last 5 years on `--backfill`.
- `categories` — fetch all (small).
- `accounts` — fetch all.

**Schema versioning:**
Copilot's GraphQL schema can change. Defensive parsing:

```python
class GraphQLClient:
    SCHEMA_VERSION = "2026-04-26"   # bump when you change the queries

    def transactions(self, start: date, end: date) -> list[dict]:
        query = """
        query Transactions($startDate: String!, $endDate: String!) {
          transactions(startDate: $startDate, endDate: $endDate) {
            id date amount currency merchant note
            category { id name parent { id } }
            account { id name institution type }
            isPending isRecurring isExcluded
            postedAt
          }
        }
        """
        ...
```

Log `SCHEMA_VERSION` in `ingestion_runs.metadata`. When fields go missing, raise a `SchemaDriftError` with the offending query — don't silently skip.

**Strategy:**
- 4-hourly incremental for transactions (last 35 days window).
- Daily full refresh of accounts and categories.
- Backfill mode for initial bootstrap.

**Sign convention:** Copilot returns `amount` as positive for expenses, negative for income. Store as-is. Document this convention in `schema_docs`.

---

## 5. `mart_refresh` service

Single Python service that rebuilds `mart_*` tables from `fact_*`. Runs after every successful ingestion of any source, and nightly at 4am for a clean rebuild.

**Approach:** `TRUNCATE` + `INSERT INTO ... SELECT ...`. Each mart has a single SQL statement that builds it. No incremental logic — keep it simple and stateless.

**`mart_daily` rebuild SQL** (the meaty one):

```sql
TRUNCATE mart_daily;

INSERT INTO mart_daily (day, ...)
WITH days AS (
  SELECT generate_series(
    (SELECT LEAST(
      MIN(day) FROM fact_recovery,
      (SELECT MIN(day) FROM fact_calendar_event),
      (SELECT MIN(day) FROM fact_food_daily),
      (SELECT MIN(date) FROM fact_transaction)
    )),
    CURRENT_DATE,
    '1 day'::interval
  )::date AS day
),
sleep_primary AS (
  -- Take the longest non-nap sleep ending each day
  SELECT DISTINCT ON (day)
    day, sleep_id, total_in_bed_min, total_rem_min, total_slow_wave_min,
    sleep_efficiency_pct, sleep_performance_pct, sleep_consistency_pct,
    start_ts, end_ts
  FROM fact_sleep
  WHERE NOT is_nap
  ORDER BY day, total_in_bed_min DESC
),
naps AS (
  SELECT day, COUNT(*) AS nap_count, SUM(total_in_bed_min) AS nap_total_min
  FROM fact_sleep WHERE is_nap GROUP BY day
),
calendar_agg AS (
  SELECT
    day,
    COUNT(*) FILTER (WHERE classification = 'meeting') AS meeting_count,
    SUM(duration_min) FILTER (WHERE classification = 'meeting') / 60.0 AS meeting_hours,
    SUM(duration_min) FILTER (WHERE classification = 'meeting' AND attendee_external_count = 0) / 60.0 AS meeting_internal_hours,
    SUM(duration_min) FILTER (WHERE classification = 'meeting' AND attendee_external_count > 0) / 60.0 AS meeting_external_hours,
    MIN(start_ts AT TIME ZONE 'America/New_York')::time FILTER (WHERE classification = 'meeting') AS first_meeting_time,
    MAX(end_ts AT TIME ZONE 'America/New_York')::time FILTER (WHERE classification = 'meeting') AS last_meeting_time,
    MAX(duration_min) FILTER (WHERE classification = 'focus') AS longest_focus_block_min,
    SUM(duration_min) FILTER (WHERE classification = 'focus') AS total_focus_block_min
  FROM fact_calendar_event
  WHERE response_status != 'declined' OR response_status IS NULL
  GROUP BY day
),
food_agg AS (
  SELECT
    day,
    COUNT(*) AS meal_count,
    MIN(eaten_at AT TIME ZONE 'America/New_York')::time AS first_meal_time,
    MAX(eaten_at AT TIME ZONE 'America/New_York')::time AS last_meal_time,
    EXTRACT(EPOCH FROM (MAX(eaten_at) - MIN(eaten_at))) / 3600.0 AS eating_window_hours,
    SUM(energy_kcal) FILTER (WHERE meal_group ILIKE 'Breakfast') AS breakfast_kcal,
    SUM(energy_kcal) FILTER (WHERE meal_group ILIKE 'Lunch') AS lunch_kcal,
    SUM(energy_kcal) FILTER (WHERE meal_group ILIKE 'Dinner') AS dinner_kcal,
    SUM(energy_kcal) FILTER (WHERE meal_group ILIKE 'Snack%') AS snack_kcal
  FROM fact_food_log GROUP BY day
),
spend_agg AS (
  SELECT
    date AS day,
    SUM(amount) FILTER (WHERE NOT is_excluded AND amount > 0) AS total_spend,
    SUM(amount) FILTER (WHERE NOT is_excluded AND c.name ILIKE 'Food%') AS food_spend,
    SUM(amount) FILTER (WHERE NOT is_excluded AND c.name = 'Restaurants') AS restaurant_spend,
    SUM(amount) FILTER (WHERE NOT is_excluded AND c.name = 'Groceries') AS groceries_spend,
    SUM(amount) FILTER (WHERE NOT is_excluded AND c.name ILIKE 'Trans%') AS transportation_spend
  FROM fact_transaction t
  LEFT JOIN dim_category c ON c.category_id = t.category_id
  GROUP BY date
),
workout_agg AS (
  SELECT
    day,
    COUNT(*) AS workout_count,
    SUM(EXTRACT(EPOCH FROM (end_ts - start_ts))/60.0) AS workout_total_min,
    SUM(kilojoules) AS workout_total_kj,
    MAX(strain) AS workout_max_strain
  FROM fact_workout GROUP BY day
)
SELECT
  d.day,
  r.recovery_score, r.hrv_rmssd_ms, r.resting_heart_rate, r.spo2_percentage, r.skin_temp_celsius,
  sp.total_in_bed_min/60.0, sp.total_rem_min/60.0, sp.total_slow_wave_min/60.0,
  sp.sleep_efficiency_pct, sp.sleep_performance_pct, sp.sleep_consistency_pct,
  sp.start_ts, sp.end_ts,
  COALESCE(n.nap_count, 0), COALESCE(n.nap_total_min, 0),
  c.scaled_strain, c.day_kilojoules,
  COALESCE(w.workout_count, 0), COALESCE(w.workout_total_min, 0),
  COALESCE(w.workout_total_kj, 0), w.workout_max_strain,
  COALESCE(ca.meeting_count, 0), COALESCE(ca.meeting_hours, 0),
  COALESCE(ca.meeting_internal_hours, 0), COALESCE(ca.meeting_external_hours, 0),
  ca.first_meeting_time, ca.last_meeting_time,
  ca.longest_focus_block_min, ca.total_focus_block_min,
  fd.energy_kcal, fd.protein_g, fd.carbs_g, fd.fat_g, fd.fiber_g, fd.alcohol_g, fd.caffeine_mg,
  fa.meal_count, fa.first_meal_time, fa.last_meal_time, fa.eating_window_hours,
  fa.breakfast_kcal, fa.lunch_kcal, fa.dinner_kcal, fa.snack_kcal,
  sa.total_spend, sa.food_spend, sa.restaurant_spend, sa.groceries_spend, sa.transportation_spend,
  bw.value AS weight_kg, bf.value AS body_fat_pct,
  now()
FROM days d
LEFT JOIN fact_recovery r ON r.day = d.day
LEFT JOIN sleep_primary sp ON sp.day = d.day
LEFT JOIN naps n ON n.day = d.day
LEFT JOIN fact_cycle c ON c.day = d.day
LEFT JOIN workout_agg w ON w.day = d.day
LEFT JOIN calendar_agg ca ON ca.day = d.day
LEFT JOIN fact_food_daily fd ON fd.day = d.day
LEFT JOIN food_agg fa ON fa.day = d.day
LEFT JOIN spend_agg sa ON sa.day = d.day
LEFT JOIN LATERAL (
  SELECT value FROM fact_biometric WHERE day = d.day AND metric = 'weight' ORDER BY measured_at DESC LIMIT 1
) bw ON TRUE
LEFT JOIN LATERAL (
  SELECT value FROM fact_biometric WHERE day = d.day AND metric = 'body_fat' ORDER BY measured_at DESC LIMIT 1
) bf ON TRUE;
```

`mart_meal` rebuild logic:
- Group `fact_food_log` rows by `day` and `meal_group`.
- Map `meal_group`: `Breakfast` → `breakfast`, `Lunch` → `lunch`, `Dinner` → `dinner`, `Snack 1/2/3` → `snack`, `Uncategorized` → infer from time-of-day clustering (a small Python pass after the SQL truncate-insert).

`mart_weekly` rebuild: straight `GROUP BY date_trunc('week', day)` over `mart_daily`.

---

## 6. MCP server

**Library:** `mcp` (official Anthropic Python SDK) with the streamable HTTP transport. Alternatively, use FastAPI with the MCP protocol implemented manually if the SDK proves limiting — but try the SDK first.

**Auth:** All requests require `Authorization: Bearer $MCP_API_KEY`. Reject otherwise with 401. Add Caddy basic rate-limiting (10 req/sec) in front for safety.

**Connection to Supabase:** two connection pools.
- `db_admin`: service role, used by tools that read mart/fact directly via parameterized queries.
- `db_reader`: `lifeos_mcp` role, used only by `ask_sql`.

### 6.1 Tool inventory

Each tool: signature, behavior, SQL behind it, response shape (compact JSON, NOT pretty-printed — token efficiency matters).

#### `get_schema_docs`
- **Args:** `(table_name: Optional[str] = None)`
- **Behavior:** Returns embedded schema docs (see §7). If `table_name` given, returns docs for just that table.
- **Use case:** Claude calls this first when a user asks an unfamiliar question, before touching data.

#### `get_daily_summary`
- **Args:** `(start_date: date, end_date: date, columns: Optional[list[str]] = None)`
- **Default columns** when not specified: `[day, recovery_score, hrv_rmssd_ms, sleep_total_hours, strain, meeting_hours, total_kcal, total_spend]`
- **SQL:** `SELECT <columns> FROM mart_daily WHERE day BETWEEN $1 AND $2 ORDER BY day`
- **Limit:** 366 rows max. If exceeded, truncate + warn in response.

#### `get_recovery_trend`
- **Args:** `(start_date, end_date, smoothing: Optional[int] = None)`
- Returns `day, recovery_score, hrv_rmssd_ms, resting_heart_rate, sleep_total_hours`. If smoothing given, also returns trailing-N-day rolling averages.

#### `get_sleep_summary`
- **Args:** `(start_date, end_date, include_naps: bool = False)`

#### `get_workouts`
- **Args:** `(start_date, end_date, sport_name: Optional[str] = None)`
- **SQL:** filtered `SELECT * FROM fact_workout`. Limit 200.

#### `get_food_log`
- **Args:** `(start_date, end_date, meal_window: Optional[str] = None, search: Optional[str] = None)`
- `search` does ILIKE on `food_name`. Limit 500. If exceeded, return aggregate suggestion.

#### `get_meal_summary`
- **Args:** `(start_date, end_date, meal_window: Optional[str] = None)`
- Returns `mart_meal` rows.

#### `get_calendar_load`
- **Args:** `(start_date, end_date)`
- Returns `day, meeting_count, meeting_hours, meeting_internal_hours, meeting_external_hours, first_meeting_time, last_meeting_time, longest_focus_block_min` from `mart_daily`.

#### `get_calendar_events`
- **Args:** `(start_date, end_date, classification: Optional[str] = None, search: Optional[str] = None)`
- For when you want individual events, not just rollups.

#### `get_spending`
- **Args:** `(start_date, end_date, category: Optional[str] = None, group_by: str = 'day')`
- `group_by ∈ {day, week, month, category, merchant}`. Default `day`.
- Returns sum + count.

#### `get_transactions`
- **Args:** `(start_date, end_date, category: Optional[str] = None, merchant: Optional[str] = None, min_amount: Optional[float] = None)`
- Individual rows from `fact_transaction`.

#### `get_biometrics`
- **Args:** `(metric: Optional[str] = None, start_date: Optional[date] = None, end_date: Optional[date] = None)`
- If no metric, returns the list of available metrics + counts.

#### `correlate_metrics`
- **Args:** `(metric_a: str, metric_b: str, start_date: date, end_date: date, lag_days: int = 0, method: str = 'pearson')`
- Both metrics must be column names in `mart_daily` (validated against an allowlist).
- Returns: `{ "n": int, "pearson_r": float, "p_value": float, "spearman_r": float, "lag_days": int }`.
- Compute in Python using `scipy.stats`. Drop NULL pairs.
- **Important:** Returns the underlying paired data (date, a, b) up to 366 points so Claude can reason about the relationship beyond a single number.

#### `ask_sql`
- **Args:** `(query: str, max_rows: int = 200)`
- **Auth:** uses `lifeos_mcp` read-only role.
- **Validation:** reject if query contains (case-insensitive) `INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|COPY` outside string literals. Belt-and-suspenders with the role permissions.
- **Statement timeout:** 5 seconds. Set per-session.
- **Limit:** auto-append `LIMIT max_rows` if no LIMIT present and query is a SELECT.
- **Response:** rows + columns + execution_ms.

### 6.2 Response shape

Compact, machine-friendly JSON. Standard envelope:

```json
{
  "ok": true,
  "tool": "get_daily_summary",
  "rows": [...],
  "row_count": 30,
  "truncated": false,
  "warnings": []
}
```

On error:

```json
{
  "ok": false,
  "tool": "...",
  "error": "...",
  "error_type": "ValidationError | SchemaDriftError | DBError"
}
```

---

## 7. Schema documentation strategy

The single biggest reason "ask my data anything" systems fail is that the LLM doesn't know what your fields mean. Solve this with an embedded, queryable doc.

**Implementation:** `mcp_server/schema_docs.py` contains a hand-curated dict, returned by `get_schema_docs`. Keep it under ~3000 tokens so Claude can fit it alongside other context.

**Structure:**

```python
SCHEMA_DOCS = {
  "tables": {
    "mart_daily": {
      "purpose": "One row per calendar day. The primary table for cross-source analysis. Always prefer this over fact_* tables for daily-grain questions.",
      "grain": "1 row per day",
      "columns": {
        "day": "Local date (America/New_York). Not UTC.",
        "recovery_score": "Whoop recovery, 0-100. Higher = more recovered. Reflects the night's sleep ending on `day`.",
        "hrv_rmssd_ms": "HRV during last sleep, in milliseconds. Whoop's primary recovery signal. Note: Whoop API returns seconds; we store ms.",
        "sleep_total_hours": "Total in-bed time of the primary nightly sleep ending on `day`. Excludes naps.",
        "sleep_efficiency_pct": "% of in-bed time actually asleep. 90%+ is good.",
        "strain": "Whoop's daily strain (0-21). Logarithmic scale.",
        "meeting_hours": "Total hours in events classified as 'meeting' (>=2 attendees, not declined).",
        "longest_focus_block_min": "Largest unbroken solo block tagged 'focus' (regex match on title).",
        "total_kcal": "From Cronometer's daily nutrition (preferred over summing food_log).",
        "first_meal_time, last_meal_time": "Local time of first and last logged eating event.",
        "eating_window_hours": "Time between first and last meal. NOT fasting window.",
        ...
      },
      "common_queries": [
        "Average recovery for the last 30 days: SELECT AVG(recovery_score) FROM mart_daily WHERE day >= CURRENT_DATE - 30",
        "..."
      ],
      "gotchas": [
        "Days with no Whoop sync show NULL recovery, not 0.",
        "Nap minutes are tracked in nap_total_min, separate from sleep_total_hours.",
      ]
    },
    "fact_food_log": { ... },
    ...
  },
  "metric_glossary": {
    "hrv_rmssd_ms": "Root mean square of successive differences between heartbeats, measured in milliseconds during the last slow-wave sleep period. Whoop's headline recovery input.",
    "strain": "Whoop's cardiovascular load score. 0-21 logarithmic.",
    ...
  },
  "conventions": {
    "timezone": "All `day` columns are in America/New_York. All `*_ts` columns are UTC.",
    "amount_sign_copilot": "fact_transaction.amount: positive = expense, negative = income or refund.",
    "calendar_classification": "Events are classified as 'meeting', 'focus', 'all_day', 'declined', or 'personal'. See fact_calendar_event docs.",
  }
}
```

The first thing the system prompt for the connector should tell Claude: *"Call `get_schema_docs` once at the start of any new analytical question."*

---

## 8. Deployment

### 8.1 docker-compose.yml (sketch)

```yaml
services:
  caddy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
    restart: unless-stopped

  mcp:
    image: ghcr.io/santi/life-os-mcp:latest
    env_file: .env
    expose: ["8080"]
    restart: unless-stopped

  scheduler:
    image: ghcr.io/santi/life-os-scheduler:latest
    env_file: .env
    restart: unless-stopped

volumes:
  caddy_data:
```

### 8.2 Caddyfile

```
lifeos.<santi-domain> {
  reverse_proxy mcp:8080
  encode gzip
  rate_limit {
    zone mcp {
      key {remote_host}
      events 10
      window 1s
    }
  }
}
```

### 8.3 Image build & push (matches Ledion workflow)

```bash
# Local build
docker build -t ghcr.io/santi/life-os-mcp:latest -f mcp_server/Dockerfile .
docker build -t ghcr.io/santi/life-os-scheduler:latest -f scheduler/Dockerfile .
# (etc for ingest_*)

# Push
docker push ghcr.io/santi/life-os-mcp:latest
# (etc)

# On droplet
ssh lifeos-droplet
cd /opt/life-os
git pull
docker compose pull
docker compose up -d
```

### 8.4 Scheduler jobs (`scheduler/__main__.py`)

```python
sched = BlockingScheduler(timezone=settings.LOCAL_TZ)

# Whoop hourly + daily backfill
sched.add_job(run_in_subprocess, "cron", minute=15, args=["ingest_whoop"])
sched.add_job(run_in_subprocess, "cron", hour=6, minute=0, args=["ingest_whoop", "--backfill", "3"])

# Calendar every 30 min
sched.add_job(run_in_subprocess, "cron", minute="*/30", args=["ingest_calendar"])

# Cronometer 3am daily, full pull weekly Sunday 3:30am
sched.add_job(run_in_subprocess, "cron", hour=3, minute=0, args=["ingest_cronometer", "--backfill", "2"])
sched.add_job(run_in_subprocess, "cron", day_of_week="sun", hour=3, minute=30, args=["ingest_cronometer", "--backfill", "14"])

# Copilot 4-hourly + nightly full
sched.add_job(run_in_subprocess, "cron", minute=0, hour="*/4", args=["ingest_copilot"])
sched.add_job(run_in_subprocess, "cron", hour=4, minute=0, args=["ingest_copilot", "--backfill", "1825"])

# Mart refresh 4:30am AND on-demand after every successful ingest
sched.add_job(run_in_subprocess, "cron", hour=4, minute=30, args=["mart_refresh"])
```

Plus: each ingester service, on successful exit, fires `mart_refresh` as a downstream subprocess. Simple coupling, no broker needed.

### 8.5 Connecting to Claude.ai

After deploy:
1. Anthropic settings → Connectors → Add custom connector.
2. Server URL: `https://lifeos.<santi-domain>/mcp`
3. Auth: bearer token, paste `$MCP_API_KEY`.
4. Approve scopes / tools list.

In a chat, prompt prefix recommendation: *"You have access to my life-os connector. Call `get_schema_docs` first when answering analytical questions about my data. Prefer `mart_daily` for daily-grain queries. Use `ask_sql` only when no semantic tool fits."*

---

## 9. Build phases (order of operations)

Don't build all of this at once. Phases below are sequential, each phase shippable.

### Phase 1 — Foundation (Days 1-2)
- Repo skeleton, `pyproject.toml`, `lifeos_core` library.
- Supabase project + migrations 0001-0005 applied.
- Empty docker-compose, Caddy, droplet provisioned, DNS pointed.
- `db/apply.py` migration runner working.
- **Acceptance:** `psql` connect works, migrations applied, droplet reachable on HTTPS.

### Phase 2 — Whoop end-to-end (Days 3-4)
- OAuth bootstrap CLI.
- Ingest service: cycle, recovery, sleep, workout, profile.
- Backfill last 365 days.
- Hourly cron.
- **Acceptance:** `fact_recovery` has ≥300 rows, `fact_sleep` has ≥300 rows, daily delta ingestion runs without error for 3 consecutive days.

### Phase 3 — Calendar end-to-end (Day 5)
- Google OAuth.
- Ingest service with sync token strategy.
- Classification logic.
- **Acceptance:** `fact_calendar_event` has events from last 90 days, classifications make sense on spot-check, sync tokens working (next run is incremental).

### Phase 4 — Mart layer (Day 6)
- `mart_refresh` service.
- `mart_daily` populated.
- Verify cross-source joins (e.g. recovery vs meeting hours).
- **Acceptance:** `SELECT * FROM mart_daily WHERE day > CURRENT_DATE - 30` returns 30 rows with sensible values across sources.

### Phase 5 — MCP v1 (Day 7)
- MCP server with: `get_schema_docs`, `get_daily_summary`, `get_recovery_trend`, `get_calendar_load`, `correlate_metrics`, `ask_sql`.
- Connector registered with Claude.ai.
- **Acceptance:** Can ask Claude.ai "how was my recovery last week vs my meeting load?" and get a real answer with real numbers.

### Phase 6 — Cronometer (Days 8-10)
- Multi-stage Docker with `cronometer-export`.
- Servings + daily nutrition + biometrics ingestion.
- Backfill last 365 days.
- Add food fields to `mart_daily` refresh, build `mart_meal`.
- Add food tools to MCP: `get_food_log`, `get_meal_summary`.
- **Acceptance:** Can ask "what's my average breakfast calories on workout days vs rest days?" and get a real answer.

### Phase 7 — Copilot (Days 11-12)
- Auth + GraphQL client + ingestion.
- `mart_daily.spend_*` populated.
- `get_spending`, `get_transactions` tools.
- **Acceptance:** Can ask "how does my restaurant spending correlate with workout strain?" and the result reflects reality.

### Phase 8 — Hardening (Days 13-14)
- Sentry integration.
- Health-check endpoint on MCP (`GET /health` returns last-successful-ingest time per source).
- Alerting: if any source's last successful ingest > 24h old, send a Pushover/email alert.
- README with rebuild-from-scratch instructions.
- Backup strategy: daily `pg_dump` to DigitalOcean Spaces.

### Phase 9 — Optional, post-MVP
- Whoop webhooks for near-real-time updates.
- Apple Health bridge (only if a real gap is identified).
- Visualization: a tiny Next.js dashboard reading from Supabase. Not for this build; mention as future.

---

## 10. Acceptance tests (write these alongside)

Minimal pytest suite. Don't aim for 80% coverage; aim for high-value tests that catch the failures that actually happen.

- `test_whoop_transform.py`: feed a saved API JSON fixture, assert `fact_recovery` row matches expected (especially HRV unit conversion).
- `test_calendar_derive.py`: synthetic events, assert classifications and attendee_internal/external counts.
- `test_cronometer_parse.py`: saved CSV fixture, assert `fact_food_log` rows including timezone conversion.
- `test_copilot_parse.py`: saved GraphQL response, assert `fact_transaction` rows.
- `test_mart_refresh.py`: insert synthetic fact rows, run `mart_refresh`, assert mart_daily values.
- `test_mcp_tools.py`: call each tool against a seeded test DB, assert response shape and SQL safety on `ask_sql`.

---

## 11. Open decisions for Santi (please answer before Phase 1 or note "decide later")

1. **Domain name** for the MCP endpoint? (`lifeos.<your-domain>`?)
2. **Initial backfill window** — 1 year for everything? Some sources can go further (Copilot has 5+ years).
3. **Cronometer Gold subscription active?** Required for per-meal timestamps. If not, food log timestamps will be coarse (date-only) and `mart_meal` becomes much weaker.
4. **Calendar accounts** — just personal Google, or also DoorLoop work calendar? If both, are you OK with work meetings being analyzable? (You should be, but flagging.)
5. **Data retention for raw_*** — keep forever, or prune after 90 days now that fact is built? Recommend keep forever — disk is cheap, replay is gold.
6. **Sentry** — yes/no for error tracking?

---

## 12. Out of scope (explicitly)

- Multi-user / auth UI / RLS.
- Mobile app.
- Real-time streaming (everything is batch).
- ML models / forecasting (Claude does the analysis at query time).
- Apple Health (deferred per earlier conversation).
- Web dashboard (deferred).
- Any "writes back to source" — this is read-only ingestion.

---

## Appendix A — Key library versions

```
python = "^3.12"
psycopg = {version = "^3.2", extras = ["binary", "pool"]}
fastapi = "^0.115"
uvicorn = "^0.32"
httpx = "^0.27"
pydantic = "^2.9"
pydantic-settings = "^2.5"
apscheduler = "^3.10"
structlog = "^24.4"
tenacity = "^9.0"
mcp = "^1.0"           # Anthropic MCP SDK
scipy = "^1.14"
google-api-python-client = "^2.140"
google-auth-oauthlib = "^1.2"
sentry-sdk = "^2.14"
pytest = "^8.3"
pytest-asyncio = "^0.24"
```

## Appendix B — Useful one-liners

```bash
# Apply migrations
python -m db.apply

# Backfill Whoop 1 year
docker compose run --rm ingest_whoop python -m ingest_whoop --backfill 365

# Force mart rebuild
docker compose run --rm mart_refresh python -m mart_refresh

# Tail MCP logs
docker compose logs -f mcp

# DB shell
psql $SUPABASE_DB_URL
```

---

**End of spec.** Build in order, ship phase by phase, don't skip the schema docs.

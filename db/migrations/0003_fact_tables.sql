-- 0003_fact_tables.sql
-- Typed fact + dim tables, derived from raw_*. Dropping and re-deriving from
-- raw is always safe — raw is the source of truth. `day` columns are STORED
-- generated from local_date(start/end), so they're queryable & indexable.

-- ---- Whoop physiological cycle ---------------------------------------------
CREATE TABLE IF NOT EXISTS fact_cycle (
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

CREATE TABLE IF NOT EXISTS fact_recovery (
  cycle_id BIGINT PRIMARY KEY,
  sleep_id UUID,
  day DATE NOT NULL,                  -- local_date of cycle start
  recovery_score INT,                 -- 0-100
  hrv_rmssd_ms NUMERIC(6,2),          -- ms (API returns seconds; we convert)
  resting_heart_rate INT,
  spo2_percentage NUMERIC(5,2),
  skin_temp_celsius NUMERIC(4,2),
  user_calibrating BOOLEAN,
  raw_id BIGINT REFERENCES raw_whoop_recovery(id),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fact_sleep (
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

CREATE TABLE IF NOT EXISTS fact_workout (
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

-- ---- Calendar --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_calendar_event (
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
  classification TEXT,                 -- 'meeting' | 'focus' | 'personal' | 'all_day' | 'declined'
  raw_id BIGINT REFERENCES raw_calendar_event(id),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (calendar_id, event_id)
);

-- ---- Cronometer food log ---------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_food_log (
  id BIGSERIAL PRIMARY KEY,
  eaten_at TIMESTAMPTZ NOT NULL,           -- per-meal timestamp (Cronometer Gold)
  day DATE GENERATED ALWAYS AS (local_date(eaten_at)) STORED,
  meal_group TEXT,                          -- Breakfast | Lunch | Dinner | Snack | Uncategorized
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
  -- Common micros (full set in micros JSONB)
  sodium_mg NUMERIC(10,2),
  potassium_mg NUMERIC(10,2),
  caffeine_mg NUMERIC(10,2),
  alcohol_g NUMERIC(10,3),
  micros JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_row_hash TEXT NOT NULL UNIQUE,    -- sha256(day|time|food|amount|unit)
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_food_log_day ON fact_food_log(day);
CREATE INDEX IF NOT EXISTS ix_food_log_eaten_at ON fact_food_log(eaten_at);

-- Cronometer's own daily totals — preferred over re-summing fact_food_log
-- since their nutrient calc handles edge cases like recipes.
CREATE TABLE IF NOT EXISTS fact_food_daily (
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

-- Tall biometrics (weight, BP, body fat, glucose, ...)
CREATE TABLE IF NOT EXISTS fact_biometric (
  id BIGSERIAL PRIMARY KEY,
  measured_at TIMESTAMPTZ NOT NULL,
  day DATE GENERATED ALWAYS AS (local_date(measured_at)) STORED,
  metric TEXT NOT NULL,                    -- 'weight' | 'systolic_bp' | 'fasting_glucose' | ...
  value NUMERIC(12,4) NOT NULL,
  unit TEXT NOT NULL,
  note TEXT,
  source TEXT NOT NULL DEFAULT 'cronometer',
  source_row_hash TEXT NOT NULL UNIQUE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_biometric_metric_day ON fact_biometric(metric, day);

-- ---- Copilot Money ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_account (
  account_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  institution TEXT,
  type TEXT,                               -- checking | savings | credit | investment | loan
  currency TEXT NOT NULL DEFAULT 'USD',
  is_hidden BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dim_category (
  category_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  parent_category_id TEXT REFERENCES dim_category(category_id),
  type TEXT,                               -- expense | income | transfer
  is_hidden BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fact_transaction (
  transaction_id TEXT PRIMARY KEY,
  date DATE NOT NULL,
  posted_ts TIMESTAMPTZ,
  amount NUMERIC(14,2) NOT NULL,           -- positive = expense, negative = income/refund
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

CREATE INDEX IF NOT EXISTS ix_transaction_date ON fact_transaction(date);
CREATE INDEX IF NOT EXISTS ix_transaction_category ON fact_transaction(category_id, date);

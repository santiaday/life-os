-- 0004_mart_tables.sql
-- Analysis-ready tables. Most MCP tools hit mart, not fact. Refreshed by
-- truncate-and-rebuild from fact in mart_refresh; cheap because we're talking
-- thousands of rows.

CREATE TABLE IF NOT EXISTS mart_daily (
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

CREATE TABLE IF NOT EXISTS mart_meal (
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

CREATE INDEX IF NOT EXISTS ix_mart_meal_day_window ON mart_meal(day, meal_window);

CREATE TABLE IF NOT EXISTS mart_weekly (
  week_start DATE PRIMARY KEY,         -- Monday
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

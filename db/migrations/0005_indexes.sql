-- 0005_indexes.sql
-- Supplementary indexes beyond what's already declared inline alongside the
-- table definitions in 0003/0004.

CREATE INDEX IF NOT EXISTS ix_calendar_event_day
  ON fact_calendar_event(day);

CREATE INDEX IF NOT EXISTS ix_calendar_event_classification
  ON fact_calendar_event(classification, day);

CREATE INDEX IF NOT EXISTS ix_workout_day_sport
  ON fact_workout(day, sport_name);

CREATE INDEX IF NOT EXISTS ix_recovery_day
  ON fact_recovery(day);

CREATE INDEX IF NOT EXISTS ix_sleep_day
  ON fact_sleep(day);

CREATE INDEX IF NOT EXISTS ix_food_log_meal_group
  ON fact_food_log(meal_group, day);

CREATE INDEX IF NOT EXISTS ix_transaction_pending
  ON fact_transaction(is_pending) WHERE is_pending;

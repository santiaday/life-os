-- 0038_source_registry.sql
--
-- Two things the unified layer needs before it can answer "is this current?"
-- and "which number wins?":
--
--   1. Precedence rows for the source keys introduced by the file loaders.
--   2. The source_health registry itself -- one row per logical stream, with
--      the SLA it is expected to meet and where its freshness is measured.
--
-- `mode` distinguishes streams that should keep flowing from ones that are
-- finished. A historical import is never "stale"; treating it that way was
-- how the original audit ended up unable to tell a dead sync from a dataset
-- that simply ended.

BEGIN;

INSERT INTO dim_source_priority (domain, source, priority, notes) VALUES
  ('nutrition','cronometer',      92, 'mobile JSON API, micronutrient complete'),
  ('nutrition','cronometer_csv',  88, 'GWT-RPC export binary; same meals, fewer micros'),
  ('nutrition','cal_ai',          62, 'in-app diary from device backup'),
  ('nutrition','cal_ai_pdf',      58, 'Summary Report PDF; wider range, no meal labels'),

  ('body','apple_health',         46, 'via Cronometer biometric passthrough'),
  ('body','cal_ai',               42, 'weight typed into Cal AI'),

  ('metric','apple_health',       50, 'via Cronometer biometric passthrough'),
  ('metric','cronometer',         20, 'Cronometer-native biometrics'),

  ('activity','zero',             15, 'Apple activity blocks; duration only'),
  ('sleep','cal_ai',               5, 'not a sleep source; placeholder')
ON CONFLICT (domain, source) DO UPDATE
  SET priority = EXCLUDED.priority,
      notes = EXCLUDED.notes,
      updated_at = now();

-- ---------------------------------------------------------------------------
-- The freshness registry.
--   mode='live'        expected to keep producing; lag is measured against SLA
--   mode='historical'  a completed one-time import; never stale by definition
--   mode='retired'     the source existed and no longer does
-- ---------------------------------------------------------------------------
INSERT INTO source_health
  (source_key, domain, display_name, mode, expected_lag_hours,
   table_name, day_column, source_filter, coverage_start, coverage_end, notes)
VALUES
  -- live wearable + API streams
  ('whoop.activity',     'activity',  'Whoop workouts (public API)',    'live', 36,
   'fact_activity', 'day', 'whoop', '2023-11-16', NULL,
   'Re-enabled 2026-07-26. Sole source of HR-zone detail on new sessions.'),
  ('whoop.sleep',        'sleep',     'Whoop sleep (public API)',       'live', 36,
   'fact_sleep_session', 'day', 'whoop', '2023-11-17', NULL, NULL),
  ('whoop_lift.activity','activity',  'Whoop Strength Trainer (private)','live', 12,
   'fact_activity', 'day', 'whoop_lift', '2025-09-03', NULL,
   'Only Whoop stream with per-set reps and loads.'),
  ('whoop.metric',       'metric',    'Whoop daily trends (private)',   'live', 36,
   'fact_daily_metric', 'day', 'whoop', '2023-11-16', NULL, NULL),
  ('whoop.body',         'body',      'Whoop Body bioimpedance',        'live', 168,
   'fact_body_composition', 'day', 'whoop', '2024-12-15', NULL, NULL),
  ('cronometer.nutrition','nutrition','Cronometer food log',            'live', 48,
   'fact_nutrition_daily', 'day', 'cronometer', '2025-09-06', NULL,
   'Sync is healthy; gaps are days with nothing logged, not pipeline failures.'),
  ('apple_health.metric','metric',    'Apple Health (via Cronometer)',  'live', 72,
   'fact_daily_metric', 'day', 'apple_health', '2023-08-14', NULL, NULL),
  ('apple_health.body',  'body',      'Apple Health weight/body fat',   'live', 72,
   'fact_body_composition', 'day', 'apple_health', '2023-08-14', NULL, NULL),
  ('loseit.nutrition',   'nutrition', 'Lose It food log (API)',         'live', 48,
   'fact_nutrition_daily', 'day', 'loseit', '2023-10-04', NULL,
   'Historical rows are from the export; ongoing sync via ingest_loseit.'),

  -- completed one-time imports
  ('garmin.activity',    'activity',  'Garmin activities (export)',     'historical', NULL,
   'fact_activity', 'day', 'garmin', '2024-12-03', '2025-08-29',
   'GDPR export. Only per-exercise load data for this window.'),
  ('garmin.sleep',       'sleep',     'Garmin sleep (export)',          'historical', NULL,
   'fact_sleep_session', 'day', 'garmin', '2024-07-30', '2025-09-01',
   'Covers the window where no Whoop was worn.'),
  ('garmin.metric',      'metric',    'Garmin daily wellness (export)', 'historical', NULL,
   'fact_daily_metric', 'day', 'garmin', '2024-08-20', '2025-09-24', NULL),
  ('garmin.body',        'body',      'Garmin body metrics (export)',   'historical', NULL,
   'fact_body_composition', 'day', 'garmin', '2023-08-14', '2025-09-23', NULL),
  ('zero.body',          'body',      'Zero weight log (export)',       'historical', NULL,
   'fact_body_composition', 'day', 'zero', '2023-08-14', '2026-07-22',
   'Densest weight series; the only source before 2023-11.'),
  ('zero.sleep',         'sleep',     'Zero sleep (export)',            'historical', NULL,
   'fact_sleep_session', 'day', 'zero', '2023-08-18', '2026-07-25', NULL),
  ('zero.fast',          'metric',    'Zero fasting log (export)',      'historical', NULL,
   'fact_fast', 'day', 'zero', '2024-01-02', NULL, NULL),
  ('whoop_export.activity','activity','Whoop CSV export',               'historical', NULL,
   'fact_activity', 'day', 'whoop_export', '2023-11-16', '2026-07-25', NULL),
  ('whoop_export.sleep', 'sleep',     'Whoop CSV export (sleep)',       'historical', NULL,
   'fact_sleep_session', 'day', 'whoop_export', '2023-11-17', '2026-07-25', NULL),
  ('loseit.body',        'body',      'Lose It weights (export)',       'historical', NULL,
   'fact_body_composition', 'day', 'loseit', '2023-10-03', '2025-09-01', NULL),
  ('cal_ai_pdf.nutrition','nutrition','Cal AI Summary Report PDF',      'historical', NULL,
   'fact_nutrition_daily', 'day', 'cal_ai_pdf', '2026-05-30', '2026-07-24',
   'Re-export from the app to extend; there is no Cal AI API.'),
  ('cal_ai.nutrition',   'nutrition', 'Cal AI diary (device backup)',   'historical', NULL,
   'fact_nutrition_daily', 'day', 'cal_ai', '2026-05-30', '2026-06-15',
   'Needs a fresh iOS backup capture to advance.'),

  -- retired
  ('hevy.activity',      'activity',  'Hevy workouts',                  'retired', NULL,
   'fact_activity', 'day', 'hevy', '2026-05-07', '2026-06-04',
   'Replaced by Whoop Strength Trainer.'),
  ('pushpress.activity', 'activity',  'PushPress classes',              'retired', NULL,
   'fact_activity', 'day', 'pushpress', '2026-04-30', '2026-06-20', NULL)
ON CONFLICT (source_key) DO UPDATE
  SET domain = EXCLUDED.domain,
      display_name = EXCLUDED.display_name,
      mode = EXCLUDED.mode,
      expected_lag_hours = EXCLUDED.expected_lag_hours,
      table_name = EXCLUDED.table_name,
      day_column = EXCLUDED.day_column,
      source_filter = EXCLUDED.source_filter,
      coverage_start = EXCLUDED.coverage_start,
      coverage_end = EXCLUDED.coverage_end,
      notes = EXCLUDED.notes,
      updated_at = now();

COMMIT;

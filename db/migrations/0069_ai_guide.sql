-- 0069_ai_guide.sql
--
-- What the droplet MCP server's get_tool_guide() and server instructions used
-- to tell Claude, rewritten for Claude reading Supabase directly. One row per
-- topic; `SELECT topic, body FROM ai_guide ORDER BY ord` is the whole guide.
--
-- Edit by UPDATE. This is the only place the warehouse explains itself in
-- prose, so keep it current when a source starts or stops.

CREATE TABLE IF NOT EXISTS ai_guide (
  topic      text PRIMARY KEY,
  ord        int  NOT NULL,
  body       text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE ai_guide ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ai_guide FROM anon, authenticated;

COMMENT ON TABLE ai_guide IS
  'Start here. How to read this warehouse correctly: session opening queries, which table answers which question, live vs historical sources, traps, and how to write. SELECT topic, body FROM ai_guide ORDER BY ord.';

INSERT INTO ai_guide (topic, ord, body) VALUES
('start_here', 10, $g$
Santi's health, training, nutrition and lab warehouse. Run these two queries before answering anything analytical:

  SELECT * FROM v_ingest_alert_open;   -- empty = every Apple Health push is current; otherwise say so first
  SELECT source_key, mode, status, last_row_day, notes FROM vw_data_freshness ORDER BY mode, source_key;

Freshness is the only way to tell a broken sync from a source that ended or a stretch with nothing logged. mode: live = should keep flowing; historical = finished import, never stale; retired = gone. Reading a nutrition gap as "he barely ate" when nothing was logged is the most expensive mistake available here.

Then: daily-grain questions -> mart_daily (one row per day, joins done, provenance columns activity_sources / sleep_source / nutrition_source / weight_method). Event-grain questions -> the vw_* views below. Table and column COMMENTs carry the details; read them before writing SQL against an unfamiliar table.
$g$),
('sources', 20, $g$
Live (as of 2026-10-23):
  apple_watch   Apple Watch via Health Auto Export -> edge function apple-health-ingest: daily metrics (fact_daily_metric), sleep (fact_sleep_session), workouts (fact_activity), weight via Withings (fact_body_composition). Watch data starts 2026-10-23.
  apple_health  Tributary -> edge function health-mirror: raw HealthKit in hk_sample / hk_hourly / hk_workout / hk_object, and Health Records lab results -> fact_lab_result (source 'apple_health').
  hevy          edge function hevy-sync every 30 min: per-set lifting (fact_activity, fact_activity_set, fact_strength_*). Hevy is the lifting log.
  cronometer    edge function cronometer-sync every 15 min: food diary -> fact_nutrition_entry / fact_nutrition_daily (source 'cronometer'). Cronometer is the nutrition source of truth.

Historical only (no new rows): Whoop (all streams, through 2026-10-23), Garmin export, Zero, Lose It, Cal AI, Copilot (finance; Era replaced it), PushPress, Google Calendar, body-image, possessions, lifelog. Their rows stay queryable.

Not here: calendar (use the Apple Calendar MCP), money (Era), live Hevy/Cronometer state (their MCPs).
$g$),
('where_to_read', 30, $g$
  training sessions         vw_activity (one row per real session, deduplicated across sources; Apple Watch HR merged into the Hevy session it overlaps)
  sets / reps / loads       vw_activity_set (Whoop Strength Trainer Sep 2025 - Oct 2026, Hevy May 2026 on). Dec 2024 - Aug 2025 Garmin exported per-EXERCISE totals only: use vw_activity_exercise there, or you will conclude there was no lifting for nine months.
  rep maxes / 1RM           vw_exercise_rep_max_unified
  adherence                 vw_adherence (rolling sessions/week; below_floor = 12-week average under 3/week. Surface it unprompted if it is true.)
  sleep                     vw_sleep_daily
  nutrition                 vw_nutrition_daily (one source per day by precedence); items in fact_nutrition_entry
  weight / body fat / DXA   vw_body_daily (best reading per day: DXA > BodPod > scale > bioimpedance); every reading with its method in vw_body_composition. Never average methods.
  daily metrics             vw_daily_metric (one value per day+metric, best source)
  labs                      fact_lab_measurement WHERE is_primary, on canonical biomarker_key; reference info in dim_biomarker
  imaging                   fact_imaging
  coverage / gaps           vw_coverage_daily
  suspect readings          data_quality_flag (flagged, never deleted)
$g$),
('traps', 40, $g$
- HRV: Apple stores SDNN, Whoop stored RMSSD. Different statistics. fact_daily_metric metric 'hrv' is RMSSD (Whoop history, and Apple Watch values computed by Tributary); 'hrv_sdnn' is Apple's SDNN. Never put them on one trend line.
- recovery_score, strain, sleep_performance, journal habits are Whoop concepts: NULL from 2026-10-23. Do not call that a data gap.
- Quality flags: vw_activity carries is_flagged; vw_body_daily already excludes flagged rows. When querying fact_* tables directly, exclude rows with an unresolved data_quality_flag (table_name + row_key).
- Whoop BODY_COMPOSITION was lean-mass %, converted on ingest. Old "81% body fat" style readings are that bug, flagged.
- Lose It entries have no real clock time; the minute is fabricated per meal.
- Cronometer's API returns an EMPTY diary with HTTP 200 when throttled. A Cronometer day that is empty in raw_import may be throttling, not fasting; compare with fact_nutrition_daily coverage before concluding.
- PushPress rows (history) are the gym's programmed workout, never training that was performed.
- NULL means the source had no data, never zero.
- Timezone: every `day` is America/New_York; every *_ts is UTC.
$g$),
('writing', 50, $g$
Writes to the outside world go through the MCPs, not SQL:
  Hevy workouts / routines   Hevy MCP
  food log                   Cronometer MCP (log_food, create_custom_food, delete_food_entries). cronometer-sync lands the change here within 15 min.
  calendar                   Apple Calendar MCP

Never write rows a sync owns (source apple_watch, apple_health, hevy, cronometer): the next sync overwrites or deletes them.

Warehouse writes that ARE yours:
  external lab report        INSERT INTO fact_lab_result (source 'external', test_id, test_date, lab_provider, biomarker_id, value_numeric/value_text, unit, reference_ranges {"low":..,"high":..}). biomarker_id must exist in dim_lab_biomarker (insert it first if new). Then SELECT unify_labs(); the draw is clustered with any other report of the same date.
  imaging report             INSERT INTO fact_imaging_study, then SELECT unify_labs().
  DXA / manual weigh-in      INSERT INTO fact_body_composition with source 'manual' and the right method; a DXA outranks every other method for its day.
  flag a bad reading         INSERT INTO data_quality_flag (..., flagged_by 'user'); automatic passes never clear user flags.

State the change before making it, run it in a transaction, and read it back. mart_daily picks changes up at the next hourly unify run (or run SELECT unify_run() now).
$g$),
('jobs', 60, $g$
pg_cron:
  :07 :37    hevy-sync
  every 15m  cronometer-sync (last 3 days)
  :15        unify_run(): Apple Watch/Hevy session merge, labs projection, quality flags, source freshness, marts for the last 60 days. One ingestion_runs row per run (source 'unify').
  :50        ingest_watchdog_run(): opens/resolves rows in ingest_alert when Apple Health pushes stop.
Health Auto Export and Tributary push from the phone whenever iOS lets them; gaps of a day are normal.

When something looks stale: SELECT * FROM ingestion_runs ORDER BY started_at DESC LIMIT 20; then SELECT jobname, status, return_message, start_time FROM cron.job_run_details ORDER BY start_time DESC LIMIT 20.
$g$)
ON CONFLICT (topic) DO UPDATE SET ord = EXCLUDED.ord, body = EXCLUDED.body, updated_at = now();

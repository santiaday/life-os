-- 0073_hk_agent_views.sql
--
-- Everything Tributary mirrors from Apple Health, readable without knowing
-- HealthKit identifiers: logged events (symptoms, sexual activity, cycle,
-- heart and hearing alerts, mindful minutes, ...), every quantity per day,
-- medications and doses, and mood / mental-health assessments. Plus the
-- ai_guide entry that points agents at them.

CREATE OR REPLACE FUNCTION hk_short(t text) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
  SELECT regexp_replace(t, '^HK(Quantity|Category)TypeIdentifier', '')
$$;

-- One row per logged Apple Health event (every category type except sleep
-- stages and stand hours, which have their own homes).
CREATE OR REPLACE VIEW vw_hk_event WITH (security_invoker = true) AS
SELECT
  (s.start_ts AT TIME ZONE 'America/New_York')::date AS day,
  hk_short(s.type) AS event,
  CASE
    WHEN hk_short(s.type) IN ('MenstrualFlow','IntermenstrualBleeding','InfrequentMenstrualCycles',
      'IrregularMenstrualCycles','PersistentIntermenstrualBleeding','ProlongedMenstrualPeriods',
      'CervicalMucusQuality','OvulationTestResult','ProgesteroneTestResult','PregnancyTestResult',
      'SexualActivity','Contraceptive','Pregnancy','Lactation','BleedingDuringPregnancy',
      'BleedingAfterPregnancy','MenopausalState','BleedingAfterMenopause') THEN 'sexual_reproductive'
    WHEN hk_short(s.type) IN ('HighHeartRateEvent','LowHeartRateEvent','IrregularHeartRhythmEvent',
      'LowCardioFitnessEvent','HypertensionEvent') THEN 'heart_alert'
    WHEN hk_short(s.type) IN ('AudioExposureEvent','EnvironmentalAudioExposureEvent',
      'HeadphoneAudioExposureEvent') THEN 'hearing_alert'
    WHEN hk_short(s.type) = 'MindfulSession' THEN 'mindfulness'
    WHEN hk_short(s.type) IN ('ToothbrushingEvent','HandwashingEvent') THEN 'hygiene'
    WHEN hk_short(s.type) = 'AppleWalkingSteadinessEvent' THEN 'mobility_alert'
    WHEN hk_short(s.type) = 'SleepApneaEvent' THEN 'sleep_alert'
    ELSE 'symptom'
  END AS domain,
  coalesce(s.label, s.value::int::text) AS value_label,
  s.value AS value_raw,
  s.start_ts,
  s.end_ts,
  round((extract(epoch FROM s.end_ts - s.start_ts) / 60)::numeric, 1) AS duration_min,
  s.source_name,
  s.metadata,
  s.uuid
FROM hk_sample s
WHERE s.kind = 'category'
  AND s.type NOT IN ('HKCategoryTypeIdentifierSleepAnalysis', 'HKCategoryTypeIdentifierAppleStandHour');

COMMENT ON VIEW vw_hk_event IS
  'Every event logged in Apple Health (Tributary mirror): symptoms with severity (value_label mild/moderate/severe/notPresent), sexual activity (metadata HKSexualActivityProtectionUsed), cycle tracking, heart and hearing alerts, mindful sessions, toothbrushing, sleep apnea. day = America/New_York. Sleep stages: vw_sleep_daily / hk_sample SleepAnalysis. Grain: 1 row per HealthKit sample.';

-- Every quantity type per day: raw samples summed/averaged, plus the
-- high-frequency types Tributary sends as hourly statistics.
CREATE OR REPLACE VIEW vw_hk_quantity_daily WITH (security_invoker = true) AS
SELECT (start_ts AT TIME ZONE 'America/New_York')::date AS day, hk_short(type) AS metric, unit,
       count(*) AS n, sum(value) AS sum, avg(value) AS avg, min(value) AS min, max(value) AS max,
       'samples'::text AS grain
FROM hk_sample WHERE kind = 'quantity'
GROUP BY 1, 2, 3
UNION ALL
SELECT (hour_start AT TIME ZONE 'America/New_York')::date, hk_short(type), unit,
       count(*), sum(sum), avg(avg), min(coalesce(min, avg)), max(coalesce(max, avg)), 'hourly'
FROM hk_hourly
GROUP BY 1, 2, 3;

COMMENT ON VIEW vw_hk_quantity_daily IS
  'Every Apple Health quantity per America/New_York day, by HealthKit metric name (BloodGlucose, NumberOfAlcoholicBeverages, DietaryCaffeine, HeartRate, PhysicalEffort, ...). grain samples: sum/avg/min/max over raw samples (use sum for cumulative types like dietary or steps, avg for rates). grain hourly: sum = total of hourly sums (cumulative types), avg = mean of hourly averages, min/max = extremes. Units as HealthKit reported them (HRV SDNN in s). For curated daily metrics prefer vw_daily_metric.';

CREATE OR REPLACE VIEW vw_hk_medication WITH (security_invoker = true) AS
SELECT data->>'medication_id' AS medication_id, data->>'name' AS name, data->>'nickname' AS nickname,
       data->>'form' AS form, (data->>'archived')::boolean AS archived,
       (data->>'has_schedule')::boolean AS has_schedule, data->'codings' AS codings, synced_at
FROM hk_object WHERE type = 'medication';

COMMENT ON VIEW vw_hk_medication IS
  'Medications listed in Apple Health → Medications (iOS 26+), as far as Santi shared them with Tributary (per-medication consent). Doses: vw_hk_medication_dose.';

CREATE OR REPLACE VIEW vw_hk_medication_dose WITH (security_invoker = true) AS
SELECT (o.start_ts AT TIME ZONE 'America/New_York')::date AS day, o.start_ts AS logged_ts,
       coalesce(o.data->>'medication', m.data->>'name') AS medication,
       (o.data->>'dose')::double precision AS dose,
       (o.data->>'scheduled_dose')::double precision AS scheduled_dose,
       o.data->>'unit' AS unit, o.data->>'log_status' AS log_status,
       o.data->>'schedule_type' AS schedule_type, (o.data->>'scheduled_at')::timestamptz AS scheduled_ts,
       o.data->>'medication_id' AS medication_id, o.uuid
FROM hk_object o
LEFT JOIN hk_object m ON m.type = 'medication' AND m.data->>'medication_id' = o.data->>'medication_id'
WHERE o.type = 'medication_dose';

COMMENT ON VIEW vw_hk_medication_dose IS
  'Every medication dose logged in Apple Health (taken / skipped per log_status). day = America/New_York.';

CREATE OR REPLACE VIEW vw_hk_mind WITH (security_invoker = true) AS
SELECT (start_ts AT TIME ZONE 'America/New_York')::date AS day, start_ts, type AS kind,
       (data->>'valence')::double precision AS valence, data->>'valence_class' AS valence_class,
       data->'labels' AS labels, data->'associations' AS associations,
       (data->>'score')::int AS score, data->>'risk' AS risk, uuid
FROM hk_object WHERE type IN ('state_of_mind', 'gad7', 'phq9');

COMMENT ON VIEW vw_hk_mind IS
  'Mental wellbeing from Apple Health: State of Mind logs (valence -1..1, labels/associations as HealthKit raw values) and GAD-7 / PHQ-9 scores.';

REVOKE ALL ON vw_hk_event, vw_hk_quantity_daily, vw_hk_medication, vw_hk_medication_dose, vw_hk_mind FROM anon, authenticated;
REVOKE ALL ON FUNCTION hk_short(text) FROM anon, authenticated;
GRANT SELECT ON vw_hk_event, vw_hk_quantity_daily, vw_hk_medication, vw_hk_medication_dose, vw_hk_mind TO lifeos_mcp;

INSERT INTO ai_guide (topic, ord, body) VALUES
('apple_health_everything', 35, $g$
Everything else Apple Health holds (Tributary mirror, live, pushed on every Health change):
  symptoms, sexual activity, cycle tracking,   vw_hk_event (domain symptom / sexual_reproductive / heart_alert / hearing_alert /
  heart + hearing alerts, mindful, hygiene     mindfulness / hygiene / sleep_alert; value_label = severity or category value; metadata has details, e.g. HKSexualActivityProtectionUsed)
  any quantity, per day                        vw_hk_quantity_daily (metric = HealthKit name: BloodGlucose, NumberOfAlcoholicBeverages, DietaryCaffeine, HeartRate, PhysicalEffort, ...)
  medications / doses                          vw_hk_medication, vw_hk_medication_dose
  mood, GAD-7, PHQ-9                           vw_hk_mind
  workouts with HR series + GPS route          hk_workout (vw_activity is the deduplicated session view)
  rings                                        hk_activity_summary
  ECG, beat-to-beat series, audiogram          hk_object (type ecg / heartbeat_series / audiogram)
  profile (DOB, sex, blood type)               hk_characteristics
  per-type counts and date ranges              vw_hk_coverage
An empty result means nothing was logged OR Tributary is not allowed to read that type (iOS hides read denials); check vw_hk_coverage and the tributary_diagnostics row in hk_object before calling it "none".
$g$)
ON CONFLICT (topic) DO UPDATE SET body = EXCLUDED.body, ord = EXCLUDED.ord, updated_at = now();

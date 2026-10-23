-- 0066_unify_in_sql.sql
--
-- The droplet's Python normalization layer (`python -m unify all` followed by
-- `python -m mart_refresh`), rewritten as Postgres functions so it keeps
-- running after the droplet is gone. pg_cron job `unify` runs unify_run()
-- hourly at :15 (scheduled in 0067, together with unify_quality() and
-- refresh_marts(), the two steps that delete-and-rewrite derived rows).
--
-- Applied to Supabase in parts: 0066a_unify_helpers_and_labs,
-- 0066b_unify_quality_views, 0066c_unify_labs_health_run, 0066e_unify_run.
--
-- What moved, and where it lives now:
--
--   unify/sources/labs.py   -> vw_lab_panel_target, vw_lab_measurement_target,
--                              unify_labs()
--   unify/quality.py        -> vw_quality_body_static, quality_rate_flags(),
--                              vw_quality_target, unify_quality()
--   unify/health.py         -> unify_source_health()
--   mart_refresh/unified.py -> refresh_marts(p_since)  (plus mart_meal and
--   + sql.py (meal, weekly)    mart_weekly for the same window)
--   unify/dedupe.py         -> merge_apple_watch_overlaps() (0063, already in
--                              Supabase; called here and by hevy-sync)
--
-- What did not move, on purpose: the per-vendor projections in
-- unify/sources/*.py. Every live source now writes the canonical tables itself
-- (apple-health-ingest, health-mirror, hevy-sync, cronometer-sync), and every
-- other source is history that the last Python run already projected.
--
-- The _target views are the projection as a query, so a change can be checked
-- by diffing a view against its table before anything is written.

-- ---------------------------------------------------------------------------
-- helpers
-- ---------------------------------------------------------------------------

-- Python's '%g' for the reason strings: six significant digits, no trailing
-- zeros. 239.4 -> '239.4', 40.0 -> '40'.
CREATE OR REPLACE FUNCTION fmt_g(v numeric)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN v IS NULL THEN NULL
    WHEN v = 0 THEN '0'
    ELSE trim_scale(round(v, greatest(0, 5 - floor(log(10, abs(v)))::int)))::text
  END
$$;

-- unify.biomarkers.normalize
CREATE OR REPLACE FUNCTION biomarker_normalize(label text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT btrim(regexp_replace(lower(btrim(coalesce(label, ''))), '[^a-z0-9]+', '_', 'g'), '_')
$$;

-- unify.biomarkers.resolve: exact alias first, then the same match with every
-- underscore removed ('vitamin_d_25_oh' = 'vitamin_d_25oh'). NULL = unmapped.
CREATE OR REPLACE FUNCTION biomarker_resolve(vendor_key text)
RETURNS text LANGUAGE sql STABLE AS $$
  WITH c AS (SELECT biomarker_normalize(vendor_key) AS n)
  SELECT coalesce(
    (SELECT a.biomarker_key FROM dim_biomarker_alias a, c WHERE a.alias = c.n),
    (SELECT a.biomarker_key FROM dim_biomarker_alias a, c
      WHERE c.n <> '' AND replace(a.alias, '_', '') = replace(c.n, '_', '')
      ORDER BY (a.alias = a.biomarker_key) DESC, a.alias
      LIMIT 1))
$$;

-- unify.biomarkers.status_for: the source's range wins, the catalogue's is
-- the fallback.
CREATE OR REPLACE FUNCTION biomarker_status(p_key text, p_value numeric,
                                            p_low numeric, p_high numeric)
RETURNS text LANGUAGE sql STABLE AS $$
  SELECT CASE
    WHEN p_value IS NULL THEN NULL
    WHEN lo IS NOT NULL AND p_value < lo THEN 'low'
    WHEN hi IS NOT NULL AND p_value > hi THEN 'high'
    WHEN lo IS NULL AND hi IS NULL THEN NULL
    ELSE 'normal'
  END
  FROM (SELECT coalesce(p_low,  (SELECT ref_low  FROM dim_biomarker WHERE biomarker_key = p_key)) AS lo,
               coalesce(p_high, (SELECT ref_high FROM dim_biomarker WHERE biomarker_key = p_key)) AS hi) b
$$;

-- Python's round(): halves go to the even neighbour (2102.5 -> 2102).
CREATE OR REPLACE FUNCTION round_half_even(v numeric)
RETURNS numeric LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN abs(v - trunc(v)) = 0.5 THEN 2 * round(v / 2) ELSE round(v) END
$$;

CREATE OR REPLACE FUNCTION try_numeric(t text)
RETURNS numeric LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
  IF t IS NULL OR btrim(t) = '' THEN RETURN NULL; END IF;
  RETURN nullif(t::numeric, 'NaN');
EXCEPTION WHEN others THEN
  RETURN NULL;
END
$$;

-- ---------------------------------------------------------------------------
-- labs  (unify/sources/labs.py)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW vw_lab_measurement_target AS
WITH r AS (
  SELECT DISTINCT ON (concat_ws(':', source, test_id, biomarker_id))
         r.*,
         concat_ws(':', source, test_id)               AS panel_uid,
         concat_ws(':', source, test_id, biomarker_id) AS measurement_uid,
         biomarker_resolve(biomarker_id)               AS biomarker_key,
         CASE WHEN jsonb_typeof(reference_ranges) = 'object'
              THEN try_numeric(reference_ranges ->> 'low') END  AS ref_low,
         CASE WHEN jsonb_typeof(reference_ranges) = 'object'
              THEN try_numeric(reference_ranges ->> 'high') END AS ref_high,
         nullif(value_numeric, 'NaN')                  AS value_num
    FROM fact_lab_result r
   WHERE test_date IS NOT NULL
   ORDER BY concat_ws(':', source, test_id, biomarker_id), id DESC
)
SELECT r.measurement_uid, r.panel_uid, r.source, r.test_date AS collected_on,
       r.biomarker_key, r.biomarker_id AS vendor_key,
       coalesce(b.display_name, r.biomarker_id)        AS display_name,
       r.value_num AS value_numeric, r.value_text,
       coalesce(nullif(r.unit, ''), b.canonical_unit)  AS unit,
       r.ref_low, r.ref_high,
       NULL::numeric AS optimal_low, NULL::numeric AS optimal_high,
       coalesce(nullif(r.status_type, ''),
                CASE WHEN r.biomarker_key IS NOT NULL
                     THEN biomarker_status(r.biomarker_key, r.value_num, r.ref_low, r.ref_high) END) AS status,
       r.raw_id,
       jsonb_build_object('lab_provider', r.lab_provider, 'trend', r.trend) AS payload
  FROM r
  LEFT JOIN dim_biomarker b ON b.biomarker_key = r.biomarker_key;

CREATE OR REPLACE VIEW vw_lab_panel_target AS
SELECT concat_ws(':', source, test_id)                                         AS panel_uid,
       source, test_id AS source_panel_id,
       (array_agg(test_date ORDER BY id))[1]                                   AS collected_on,
       (array_agg(coalesce(nullif(lab_provider, ''), source) ORDER BY id))[1]  AS provider,
       count(*)::int                                                           AS result_count,
       (array_agg(raw_id ORDER BY id))[1]                                      AS raw_id
  FROM fact_lab_result
 WHERE test_date IS NOT NULL
 GROUP BY source, test_id;

CREATE OR REPLACE FUNCTION unify_labs()
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  n_panels int; n_meas int; n_flips int; n_draws int; n_dup int; n_img int; n_unmapped int;
BEGIN
  INSERT INTO fact_lab_panel (panel_uid, source, source_panel_id, collected_on, reported_on,
                              provider, ordering_provider, panel_name, result_count,
                              cluster_id, is_primary, raw_id, payload, updated_at)
  SELECT panel_uid, source, source_panel_id, collected_on, NULL, provider, NULL, NULL,
         result_count, NULL, true, raw_id, '{}'::jsonb, now()
    FROM vw_lab_panel_target
  ON CONFLICT (panel_uid) DO UPDATE SET
    source = EXCLUDED.source, source_panel_id = EXCLUDED.source_panel_id,
    collected_on = EXCLUDED.collected_on, provider = EXCLUDED.provider,
    result_count = EXCLUDED.result_count, raw_id = EXCLUDED.raw_id, updated_at = now()
  WHERE (fact_lab_panel.source, fact_lab_panel.source_panel_id, fact_lab_panel.collected_on,
         fact_lab_panel.provider, fact_lab_panel.result_count, fact_lab_panel.raw_id)
        IS DISTINCT FROM
        (EXCLUDED.source, EXCLUDED.source_panel_id, EXCLUDED.collected_on,
         EXCLUDED.provider, EXCLUDED.result_count, EXCLUDED.raw_id);
  GET DIAGNOSTICS n_panels = ROW_COUNT;

  INSERT INTO fact_lab_measurement (measurement_uid, panel_uid, source, collected_on,
                                    biomarker_key, vendor_key, display_name, value_numeric,
                                    value_text, unit, ref_low, ref_high, optimal_low,
                                    optimal_high, status, is_primary, raw_id, payload, updated_at)
  SELECT measurement_uid, panel_uid, source, collected_on, biomarker_key, vendor_key,
         display_name, value_numeric, value_text, unit, ref_low, ref_high, optimal_low,
         optimal_high, status, true, raw_id, payload, now()
    FROM vw_lab_measurement_target
  ON CONFLICT (measurement_uid) DO UPDATE SET
    panel_uid = EXCLUDED.panel_uid, source = EXCLUDED.source,
    collected_on = EXCLUDED.collected_on, biomarker_key = EXCLUDED.biomarker_key,
    vendor_key = EXCLUDED.vendor_key, display_name = EXCLUDED.display_name,
    value_numeric = EXCLUDED.value_numeric, value_text = EXCLUDED.value_text,
    unit = EXCLUDED.unit, ref_low = EXCLUDED.ref_low, ref_high = EXCLUDED.ref_high,
    status = EXCLUDED.status, raw_id = EXCLUDED.raw_id, payload = EXCLUDED.payload,
    updated_at = now()
  WHERE (fact_lab_measurement.panel_uid, fact_lab_measurement.collected_on,
         fact_lab_measurement.biomarker_key, fact_lab_measurement.display_name,
         fact_lab_measurement.value_numeric, fact_lab_measurement.value_text,
         fact_lab_measurement.unit, fact_lab_measurement.ref_low, fact_lab_measurement.ref_high,
         fact_lab_measurement.status, fact_lab_measurement.raw_id, fact_lab_measurement.payload)
        IS DISTINCT FROM
        (EXCLUDED.panel_uid, EXCLUDED.collected_on, EXCLUDED.biomarker_key,
         EXCLUDED.display_name, EXCLUDED.value_numeric, EXCLUDED.value_text, EXCLUDED.unit,
         EXCLUDED.ref_low, EXCLUDED.ref_high, EXCLUDED.status, EXCLUDED.raw_id, EXCLUDED.payload);
  GET DIAGNOSTICS n_meas = ROW_COUNT;

  -- One collection date is one draw, whoever reported it. Inside a draw the
  -- lab's own report outranks an app's copy of it, then the richer panel.
  WITH p AS (
    SELECT p.panel_uid, p.collected_on, p.source,
           count(m.measurement_uid) FILTER (WHERE m.biomarker_key IS NOT NULL) AS mapped
      FROM fact_lab_panel p
      LEFT JOIN fact_lab_measurement m ON m.panel_uid = p.panel_uid
     GROUP BY p.panel_uid, p.collected_on, p.source
  ), ranked AS (
    SELECT panel_uid,
           dense_rank() OVER (ORDER BY collected_on) AS cid,
           row_number() OVER (PARTITION BY collected_on
                              ORDER BY CASE source WHEN 'manual' THEN 3 WHEN 'external' THEN 2 ELSE 1 END DESC,
                                       mapped DESC, panel_uid) AS rn
      FROM p
  )
  UPDATE fact_lab_panel f SET cluster_id = r.cid, is_primary = (r.rn = 1), updated_at = now()
    FROM ranked r
   WHERE r.panel_uid = f.panel_uid
     AND (f.cluster_id IS DISTINCT FROM r.cid OR f.is_primary IS DISTINCT FROM (r.rn = 1));

  -- Deduplicate per analyte, not per panel: an analyte only one panel of the
  -- draw measured stays visible (the 2026-07-22 testosterone case).
  WITH ranked AS (
    SELECT m.measurement_uid,
           row_number() OVER (PARTITION BY m.collected_on, coalesce(m.biomarker_key, m.vendor_key)
                              ORDER BY p.is_primary DESC NULLS LAST,
                                       (m.value_numeric IS NOT NULL) DESC,
                                       m.measurement_uid) AS rn
      FROM fact_lab_measurement m
      LEFT JOIN fact_lab_panel p ON p.panel_uid = m.panel_uid
  )
  UPDATE fact_lab_measurement m SET is_primary = (r.rn = 1), updated_at = now()
    FROM ranked r
   WHERE r.measurement_uid = m.measurement_uid AND m.is_primary IS DISTINCT FROM (r.rn = 1);
  GET DIAGNOSTICS n_flips = ROW_COUNT;

  SELECT count(*), count(*) FILTER (WHERE n > 1) INTO n_draws, n_dup
    FROM (SELECT collected_on, count(*) AS n FROM fact_lab_panel GROUP BY collected_on) d;

  INSERT INTO fact_imaging (study_uid, source, study_date, modality, body_region, laterality,
                            provider, ordering_reason, impression, findings, measurements,
                            raw_text, report_url, raw_id, updated_at)
  SELECT concat_ws(':', source, study_id), source, study_date,
         upper(coalesce(nullif(modality, ''), 'OTHER')), body_region, NULL, provider,
         ordering_reason, impression, coalesce(findings, '[]'::jsonb), '{}'::jsonb,
         raw_text, NULL, raw_id, now()
    FROM fact_imaging_study
   WHERE study_date IS NOT NULL
  ON CONFLICT (study_uid) DO UPDATE SET
    source = EXCLUDED.source, study_date = EXCLUDED.study_date, modality = EXCLUDED.modality,
    body_region = EXCLUDED.body_region, provider = EXCLUDED.provider,
    ordering_reason = EXCLUDED.ordering_reason, impression = EXCLUDED.impression,
    findings = EXCLUDED.findings, raw_text = EXCLUDED.raw_text, raw_id = EXCLUDED.raw_id,
    updated_at = now()
  WHERE (fact_imaging.study_date, fact_imaging.modality, fact_imaging.body_region,
         fact_imaging.provider, fact_imaging.ordering_reason, fact_imaging.impression,
         fact_imaging.findings, fact_imaging.raw_text, fact_imaging.raw_id)
        IS DISTINCT FROM
        (EXCLUDED.study_date, EXCLUDED.modality, EXCLUDED.body_region, EXCLUDED.provider,
         EXCLUDED.ordering_reason, EXCLUDED.impression, EXCLUDED.findings, EXCLUDED.raw_text,
         EXCLUDED.raw_id);
  GET DIAGNOSTICS n_img = ROW_COUNT;

  SELECT count(DISTINCT vendor_key) INTO n_unmapped
    FROM fact_lab_measurement WHERE biomarker_key IS NULL;

  RETURN jsonb_build_object('panels_written', n_panels, 'measurements_written', n_meas,
                            'primary_flips', n_flips, 'draws', n_draws, 'duplicate_draws', n_dup,
                            'imaging_written', n_img, 'unmapped_vendor_keys', n_unmapped);
END
$$;

-- ---------------------------------------------------------------------------
-- data quality  (unify/quality.py)
-- ---------------------------------------------------------------------------

-- Body composition: absolute plausibility and same-day dispersion.
CREATE OR REPLACE VIEW vw_quality_body_static AS
WITH b AS (
  SELECT f.measurement_uid, f.day, c.col, c.v
    FROM fact_body_composition f
   CROSS JOIN LATERAL (VALUES ('weight_kg', f.weight_kg),
                              ('body_fat_pct', f.body_fat_pct),
                              ('lean_mass_kg', f.lean_mass_kg)) AS c(col, v)
   WHERE c.v IS NOT NULL
), bounds(col, lo, hi) AS (
  VALUES ('weight_kg', 40.0, 200.0), ('body_fat_pct', 3.0, 50.0), ('lean_mass_kg', 25.0, 120.0)
), tol(col, tol) AS (
  VALUES ('weight_kg', 5.0), ('body_fat_pct', 6.0)
), med AS (
  SELECT day, col, percentile_cont(0.5) WITHIN GROUP (ORDER BY v)::numeric AS med, count(*) AS n
    FROM b WHERE col IN ('weight_kg', 'body_fat_pct')
   GROUP BY day, col HAVING count(*) >= 3
)
SELECT 'fact_body_composition'::text AS table_name, b.measurement_uid AS row_key, b.day,
       b.col AS metric, b.v AS value,
       format('%s=%s outside plausible range %s-%s', b.col, fmt_g(b.v), fmt_g(x.lo), fmt_g(x.hi)) AS reason,
       'plausibility'::text AS rule, 'reject'::text AS severity
  FROM b JOIN bounds x USING (col)
 WHERE b.v < x.lo OR b.v > x.hi
UNION ALL
SELECT 'fact_body_composition', b.measurement_uid, b.day, b.col, b.v,
       format('%s=%s deviates %s from same-day median %s across %s readings',
              b.col, fmt_g(b.v), round(abs(b.v - m.med), 1), fmt_g(m.med), m.n),
       'dispersion', 'reject'
  FROM b JOIN med m USING (day, col) JOIN tol t USING (col)
 WHERE abs(b.v - m.med) > t.tol;

-- Body composition: day-over-day change against the previous clean reading.
-- Procedural because "previous clean reading" depends on what was skipped.
CREATE OR REPLACE FUNCTION quality_rate_flags()
RETURNS TABLE (row_key text, day date, metric text, value numeric, reason text)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE
  flagged text[];
  new_flags text[];
  col text;
  lim numeric;
  r record;
  prev_day date;
  prev_val numeric;
  delta numeric;
BEGIN
  SELECT coalesce(array_agg(DISTINCT s.row_key), '{}') INTO flagged FROM vw_quality_body_static s;
  FOREACH col IN ARRAY ARRAY['weight_kg', 'body_fat_pct'] LOOP
    lim := CASE col WHEN 'weight_kg' THEN 1.5 ELSE 2.0 END;
    prev_day := NULL; prev_val := NULL; new_flags := '{}';
    FOR r IN EXECUTE format(
      'SELECT measurement_uid AS uid, day AS d, %I AS v FROM fact_body_composition
        WHERE %I IS NOT NULL ORDER BY day, measured_at, measurement_uid', col, col)
    LOOP
      CONTINUE WHEN r.uid = ANY (flagged);
      IF prev_day IS NOT NULL THEN
        delta := abs(r.v - prev_val) / greatest(r.d - prev_day, 1);
        IF delta > lim THEN
          row_key := r.uid; day := r.d; metric := col; value := r.v;
          reason := format('%s moved %s/day vs %s on %s (limit %s/day)',
                           col, to_char(round(delta, 2), 'FM9999990.00'), fmt_g(prev_val),
                           prev_day, fmt_g(lim));
          RETURN NEXT;
          new_flags := new_flags || r.uid;
          CONTINUE;
        END IF;
      END IF;
      prev_day := r.d; prev_val := r.v;
    END LOOP;
    flagged := flagged || new_flags;
  END LOOP;
END
$$;

CREATE OR REPLACE VIEW vw_quality_target AS
WITH metric_bounds(metric, lo, hi) AS (
  VALUES ('weight_kg', 40.0, 200.0), ('body_fat_pct', 3.0, 50.0), ('lean_mass_kg', 25.0, 120.0),
         ('resting_hr', 30.0, 120.0), ('hrv', 5.0, 300.0), ('spo2_avg', 70.0, 100.0),
         ('respiratory_rate', 6.0, 30.0), ('vo2_max', 20.0, 90.0), ('steps', 0.0, 80000.0),
         ('calories_total', 800.0, 8000.0), ('skin_temp', 28.0, 40.0), ('day_strain', 0.0, 21.0)
), nutrition_bounds(col, lo, hi) AS (
  VALUES ('energy_kcal', 0.0, 12000.0), ('protein_g', 0.0, 600.0),
         ('carbs_g', 0.0, 1500.0), ('fat_g', 0.0, 800.0)
), sleep_bounds(col, lo, hi) AS (
  VALUES ('asleep_s', 1800.0, 57600.0), ('efficiency_pct', 0.0, 100.0)
), nut AS (
  SELECT n.day, n.source, c.col, c.v
    FROM fact_nutrition_daily n
   CROSS JOIN LATERAL (VALUES ('energy_kcal', n.energy_kcal), ('protein_g', n.protein_g),
                              ('carbs_g', n.carbs_g), ('fat_g', n.fat_g)) AS c(col, v)
), slp AS (
  SELECT s.sleep_uid, s.day, c.col, c.v
    FROM fact_sleep_session s
   CROSS JOIN LATERAL (VALUES ('asleep_s', s.asleep_s), ('efficiency_pct', s.efficiency_pct)) AS c(col, v)
   WHERE NOT s.is_nap AND c.v IS NOT NULL
)
SELECT table_name, row_key, day, metric, value, reason, rule, severity FROM vw_quality_body_static
UNION ALL
SELECT 'fact_body_composition', q.row_key, q.day, q.metric, q.value, q.reason, 'rate_of_change', 'warn'
  FROM quality_rate_flags() q
UNION ALL
SELECT 'fact_daily_metric', m.day::text || '|' || m.metric || '|' || m.source, m.day, m.metric, m.value,
       format('%s=%s outside plausible range %s-%s', m.metric, fmt_g(m.value), fmt_g(b.lo), fmt_g(b.hi)),
       'plausibility', 'reject'
  FROM fact_daily_metric m JOIN metric_bounds b USING (metric)
 WHERE m.value IS NOT NULL AND (m.value < b.lo OR m.value > b.hi)
UNION ALL
SELECT 'fact_nutrition_daily', nut.day::text || '|' || nut.source, nut.day, nut.col, nut.v,
       format('%s=%s outside plausible range %s-%s', nut.col, fmt_g(nut.v), fmt_g(b.lo), fmt_g(b.hi)),
       'plausibility', 'reject'
  FROM nut JOIN nutrition_bounds b USING (col)
 WHERE nut.v IS NOT NULL AND (nut.v < b.lo OR nut.v > b.hi)
UNION ALL
SELECT 'fact_nutrition_daily', n.day::text || '|' || n.source, n.day, 'energy_kcal', n.energy_kcal,
       format('macros imply %s kcal but total says %s',
              round_half_even(n.protein_g * 4 + n.carbs_g * 4 + n.fat_g * 9), round_half_even(n.energy_kcal)),
       'consistency', 'warn'
  FROM fact_nutrition_daily n
 WHERE n.energy_kcal > 200
   AND n.protein_g IS NOT NULL AND n.carbs_g IS NOT NULL AND n.fat_g IS NOT NULL
   AND (n.protein_g * 4 + n.carbs_g * 4 + n.fat_g * 9) > 0
   AND abs((n.protein_g * 4 + n.carbs_g * 4 + n.fat_g * 9) - n.energy_kcal) / n.energy_kcal > 0.25
UNION ALL
SELECT 'fact_sleep_session', slp.sleep_uid, slp.day, slp.col, slp.v,
       format('%s=%s outside plausible range %s-%s', slp.col, fmt_g(slp.v), fmt_g(b.lo), fmt_g(b.hi)),
       'plausibility', 'warn'
  FROM slp JOIN sleep_bounds b USING (col)
 WHERE slp.v < b.lo OR slp.v > b.hi;

-- ---------------------------------------------------------------------------
-- source freshness  (unify/health.py)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION unify_source_health()
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE s record; last date; ok int := 0; bad int := 0;
BEGIN
  FOR s IN SELECT source_key, table_name, day_column, source_filter
             FROM source_health WHERE table_name IS NOT NULL AND mode = 'live'
  LOOP
    BEGIN
      IF s.source_filter IS NULL THEN
        EXECUTE format('SELECT max(%I) FROM %I', s.day_column, s.table_name) INTO last;
      ELSE
        EXECUTE format('SELECT max(%I) FROM %I WHERE source = $1', s.day_column, s.table_name)
          INTO last USING s.source_filter;
      END IF;
      UPDATE source_health SET last_row_day = last, last_checked_at = now(), updated_at = now()
       WHERE source_key = s.source_key;
      ok := ok + 1;
    EXCEPTION WHEN others THEN
      bad := bad + 1;
      RAISE WARNING 'unify_source_health % failed: %', s.source_key, SQLERRM;
    END;
  END LOOP;
  RETURN jsonb_build_object('updated', ok, 'failed', bad);
END
$$;

-- ---------------------------------------------------------------------------
-- orchestration
-- ---------------------------------------------------------------------------
--
-- Same order as the droplet (dedupe -> labs -> quality -> health -> marts).
-- Each step runs in its own subtransaction, so one failure doesn't stop the
-- rest; any failure marks the ingestion_runs row 'failure' with the errors.

CREATE OR REPLACE FUNCTION unify_run(p_since date DEFAULT CURRENT_DATE - 60)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  run_id bigint;
  result jsonb := '{}';
  errs jsonb := '{}';
  step text;
  res jsonb;
BEGIN
  INSERT INTO ingestion_runs (source, data_type, started_at, status, metadata)
  VALUES ('unify', 'sql', now(), 'running', jsonb_build_object('since', p_since))
  RETURNING id INTO run_id;

  FOREACH step IN ARRAY ARRAY['dedupe', 'labs', 'quality', 'health', 'marts'] LOOP
    BEGIN
      res := CASE step
        WHEN 'dedupe'  THEN jsonb_build_object('apple_watch_merged', merge_apple_watch_overlaps(CURRENT_DATE - 14))
        WHEN 'labs'    THEN unify_labs()
        WHEN 'quality' THEN unify_quality()
        WHEN 'health'  THEN unify_source_health()
        WHEN 'marts'   THEN refresh_marts(p_since)
      END;
      result := result || jsonb_build_object(step, res);
    EXCEPTION WHEN others THEN
      errs := errs || jsonb_build_object(step, SQLERRM);
    END;
  END LOOP;

  UPDATE ingestion_runs SET
    finished_at = now(),
    status = CASE WHEN errs = '{}' THEN 'success' ELSE 'failure' END,
    error_message = CASE WHEN errs = '{}' THEN NULL ELSE errs::text END,
    metadata = metadata || jsonb_build_object('result', result)
  WHERE id = run_id;

  RETURN result || CASE WHEN errs = '{}' THEN '{}'::jsonb ELSE jsonb_build_object('errors', errs) END;
END
$$;

COMMENT ON FUNCTION unify_run(date) IS
  'Normalization layer (replaces python -m unify all + python -m mart_refresh). pg_cron job "unify" runs it hourly at :15; each run logs one ingestion_runs row (source unify, data_type sql).';
COMMENT ON VIEW vw_quality_target IS
  'Automatic data-quality flags as a query. unify_quality() replaces every flagged_by=auto row in data_quality_flag with this.';
COMMENT ON VIEW vw_lab_measurement_target IS
  'fact_lab_result projected onto canonical biomarker keys. unify_labs() upserts it into fact_lab_measurement.';


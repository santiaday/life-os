# Unified data layer — runbook

Every health, training and nutrition source lands in one canonical schema, in
SI units, tagged with its source and (where the technique matters) the method
that produced the number.

```
vendor export / API
        │
        ▼
 raw_import  ──────────────  immutable JSONB, natural-keyed, never transformed
 fact_workout, fact_sleep,
 fact_food_log, …            source-specific landing tables (unchanged)
        │
        ▼   unify/
 fact_activity            fact_sleep_session      fact_nutrition_entry
 fact_activity_exercise   fact_body_composition   fact_nutrition_daily
 fact_activity_set        fact_daily_metric       fact_fast
 fact_lab_panel           fact_lab_measurement    fact_imaging
        │
        ▼
 vw_activity, vw_sleep_daily, vw_nutrition_daily, vw_body_daily,
 vw_daily_metric, vw_adherence, vw_data_freshness, vw_coverage_daily
        │
        ▼
 mart_daily  ──────────────  one row per day, unified columns + provenance
```

## Why the source-specific tables still exist

Their ingesters keep writing to them, unchanged. `unify` projects them into
the canonical tables. That keeps the risk of this refactor to the projection
layer: if a projection is wrong, fix it and re-run — no ingest data is lost,
because every canonical table is a pure function of `raw_import` plus the
source fact tables.

That property is what makes `--rebuild` safe.

## Commands

```bash
python -m unify all                # project everything, dedupe, flag, refresh health
python -m unify all --rebuild      # TRUNCATE derived tables first, then re-derive
python -m unify source garmin      # one source
python -m unify freshness          # per-source status table
python -m unify coverage           # day-by-day gap report
python -m unify quality            # re-run the data-quality rules
```

Run `python -m unify all` **before** `python -m mart_refresh` — the mart's
unified overlay reads the views this builds. The scheduler already orders them
(04:15 unify, 04:30 mart).

## One-time file loaders

```bash
python -m ingest_files.garmin       --dir ~/Downloads/<garmin-export>
python -m ingest_files.whoop_export --dir ~/Downloads/my_whoop_data_<date>
python -m ingest_files.zero         --dir ~/Downloads/data
python -m ingest_files.loseit       --dir ~/Downloads/loseit-export
python -m ingest_files.calai_pdf    --pdf ~/Downloads/<report>.pdf [--dry-run]
```

All are idempotent — natural keys are content- or id-derived, so re-running the
same export rewrites identical rows. Run `python -m unify all` afterwards.

## Deduplication

The same lifting session can appear four times: Whoop public (strain, HR,
zones), Whoop Strength Trainer (per-set loads), the Whoop CSV export
(historical zones), Garmin (per-exercise volume, training load). None is a
superset of the others.

`unify.dedupe` clusters rows describing the same real event, picks a primary
by `dim_source_priority`, then **enriches** the primary with every non-NULL
field its siblings have and it doesn't. The result is one row strictly richer
than any single source. `payload.merged_from` records which sources
contributed.

Clustering is **anchor-based**, not union-find. Each row is compared only
against cluster anchors, never against other members, so `A~B` and `B~C` cannot
imply `A~C`. That matters: an earlier union-find version let a 194-minute Apple
activity block chain three consecutive pickleball games into one cluster, which
would have undercounted the sessions `vw_adherence` is built on.

To change which source wins, edit `dim_source_priority` — no code change.

## Data quality

`unify.quality` flags, never deletes. Three rule families:

| rule | catches |
|---|---|
| `plausibility` | values outside physical bounds (weight 40–200 kg, body fat 3–50%) |
| `dispersion` | a reading far from the same day's median across sources |
| `rate_of_change` | day-over-day deltas biology doesn't permit (>1.5 kg/day) |

Automatic flags are recomputed from scratch each run, so a corrected row stops
being flagged. **Manual flags (`flagged_by='user'`) are never cleared by the
automatic pass.** Read paths exclude flagged rows by default; pass
`include_flagged=true` to see them with the reason attached.

## Adding a source

1. Land it in `raw_import` (or an existing `raw_*` table) — verbatim.
2. Write `unify/sources/<name>.py` with a `project_all(conn)`.
3. Register it in `unify/__main__.py` `SOURCES`.
4. Add precedence rows to `dim_source_priority` and a registry row to
   `source_health` (a migration, so it's reproducible).
5. Add vendor strings to `unify/taxonomy.py` if it logs activities or exercises.

## Known vendor defects

These are export bugs, not pipeline bugs. Recorded here so nobody re-diagnoses
them:

- **Zero** — all 1,707 `restingHRBPM` values export as `0`; every
  `caloric_intake_data` timestamp is zeroed to `0001-01-01`. Both are landed in
  `raw_import` and deliberately not projected.
- **Whoop** — the `BODY_COMPOSITION` trend is **lean mass %**, not body fat.
  Converted on ingest. Taking it literally produced "81% body fat" rows.
- **Garmin** — files leg extensions under the `CRUNCH` category. Subcategory is
  trusted over category for that reason.
- **Garmin units** — grams, milliseconds, and **centimeters** for distance and
  elevation; `avgSpeed` is cm/ms. `startTimeLocal` is epoch-ms of the local wall
  clock, i.e. already shifted — not a real instant.
- **Cronometer** — the mobile API answers HTTP 200 with an *empty* diary when
  rate-limited, indistinguishable from a day with nothing logged. The history
  backfill throttles (`--delay`, default 1.2s) and `--skip-known` makes repeated
  runs converge.
- **Lose It** — the export has no clock time, only a meal name. Entries are
  placed at a representative hour per meal; ordering within a day is real, the
  minute is not.
- **Apple Health via Cronometer** — some weights land in a pounds-labelled field
  holding a kilogram value (80.69 "lb" = 178 lb actual). These surface as
  dispersion flags rather than being silently rewritten.

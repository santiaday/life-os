"""The `get_tool_guide` payload — how to actually use this MCP well.

Three layers of guidance, deliberately separated:

    server `instructions`   auto-surfaced at connect time. Must stay short
                            enough that it's always read. Orientation + the
                            two or three rules that prevent wrong answers.
    get_tool_guide()        this file. Which tool for which question, the
                            per-source traps, worked recipes. Read on demand.
    get_schema_docs()       column-level semantics. Read when writing SQL.

Kept as data rather than prose so a caller can request one topic instead of
the whole thing.
"""

from __future__ import annotations

START_HERE = {
    "the_two_opening_calls": [
        "get_data_freshness() — separates a broken sync from a source that "
        "legitimately ended. `mode` is the field that matters: live = should "
        "be flowing, historical = a finished one-time import (never stale), "
        "retired = the source no longer exists.",
        "get_tool_guide() — this. Skip it only if you already know the tool.",
    ],
    "why_freshness_first": (
        "Most domains here are sparse by nature, not by failure. Nutrition "
        "covers ~31% of days and weight ~28%, because logging was intermittent "
        "— the syncs are healthy. Diagnosing 'the Cronometer sync is broken' "
        "when the user simply stopped logging wastes the session and produces "
        "a confidently wrong answer."
    ),
    "then": (
        "For a daily-grain question go straight to mart_daily via "
        "get_daily_summary or ask_sql. For an event-grain question "
        "(sessions, sets, meals, draws) use the unified get_* tools below."
    ),
}

TOOL_SELECTION = {
    "training / workouts": {
        "use": "get_activity",
        "instead_of": ["get_workouts", "get_strength_workouts",
                       "get_whoop_lift_workouts"],
        "why": (
            "One row per real session across Whoop, Garmin, Hevy and Lose It, "
            "deduplicated. The same lift can be recorded by four sources; the "
            "surviving row is enriched with every non-NULL field its siblings "
            "had, so it is strictly richer than any single source."
        ),
        "carries": "strain, training_load, avg/max HR, kcal, hard_minutes "
                   "(zone 4+5), distance, total volume, per-session sources",
    },
    "sets, reps, loads": {
        "use": "get_activity_sets",
        "caveat": (
            "Per-set data exists only where a source recorded sets: Whoop "
            "Strength Trainer (Sep 2025+) and Hevy (May-Jun 2026). For "
            "Dec 2024 - Aug 2025 Garmin exported per-EXERCISE aggregates only "
            "— use get_activity_exercises there or you will conclude he did "
            "no lifting for nine months."
        ),
    },
    "exercise volume / movement history": {
        "use": "get_activity_exercises",
        "why": "Spans every source on canonical movement names, so 'bench "
               "press' is one series across Garmin, Hevy and Whoop rather "
               "than three. Supports exclude_spinal_flexion for the L4-L5 / "
               "L5-S1 findings.",
    },
    "training consistency / 'am I falling off'": {
        "use": "get_adherence",
        "why": (
            "Four training blocks in three years were all abandoned between "
            "month four and month six. Rolling 12-week sessions/week is the "
            "variable that moves first. `below_floor` is an early warning, "
            "not a retrospective — surface it unprompted if it flips."
        ),
    },
    "nutrition": {
        "use": "get_nutrition",
        "note": "Daily totals by default; per_item=true for individual foods. "
                "Each row names its source. A missing day means nothing was "
                "logged anywhere, not that a sync failed.",
    },
    "weight / body fat / DXA": {
        "use": "get_body_composition",
        "why": (
            "Every reading carries the METHOD that produced it. A DXA and a "
            "wrist bioimpedance reading differ by 3-5 percentage points and "
            "must never be averaged. daily=true returns one best reading per "
            "day (DXA > BodPod > scale > bioimpedance); daily=false returns "
            "every raw reading, which is how you check whether a day's "
            "sources disagree."
        ),
        "writing": "log_body_composition(method='dxa', ...) — a DXA entered "
                   "this way immediately becomes authoritative for its date.",
    },
    "labs / biomarkers": {
        "use": "get_lab_values",
        "instead_of": ["get_lab_results"],
        "why": (
            "Canonical biomarker keys, so a trend sees one series even when "
            "sources spell the analyte differently (`alt` vs "
            "`alanine_aminotransferase`). The same draw reported by both Whoop "
            "Advanced Labs and the lab's own PDF is resolved per-analyte: you "
            "get the union of the draw with nothing counted twice."
        ),
    },
    "what data is missing": {
        "use": "get_coverage",
        "why": "Day-by-day coverage per domain plus contiguous gaps. Use it "
               "before claiming a trend over a window that has no data.",
    },
    "suspect readings": {
        "use": "get_data_quality_flags",
        "why": "Implausible values are flagged, never deleted, and excluded "
               "from read paths by default.",
    },
}

GOTCHAS = {
    "garmin_has_no_sets": (
        "Garmin's export carries per-exercise aggregates only. get_activity_sets "
        "returns nothing for Dec 2024 - Aug 2025; that window is NOT a training "
        "gap. Use get_activity_exercises."
    ),
    "whoop_body_composition": (
        "Whoop's BODY_COMPOSITION trend is LEAN MASS percentage, not body fat. "
        "It is converted on ingest. Taking it literally produced '81% body fat' "
        "rows before it was caught."
    ),
    "pushpress_is_programming": (
        "PushPress rows are the gym's published workout-of-the-day, not "
        "attendance. Never present them as training he performed. class_date "
        "runs into the FUTURE — it publishes ahead — so a healthy lag is "
        "negative, and get_pushpress_upcoming is the tool for 'what's "
        "tomorrow'. Stored RAW and unparsed by explicit choice: the LLM parser "
        "in coach/ is retired and must not be reintroduced. Read title and "
        "raw_text directly; parsed_at / parser_confidence are NULL on "
        "everything ingested after 2026-06-07 and that is correct, not a gap."
    ),
    "loseit_times_are_fabricated": (
        "The Lose It export has no clock time, only a meal name. Entries sit at "
        "a representative hour per meal — ordering within a day is real, the "
        "minute is not. Do not compute meal timing from Lose It rows."
    ),
    "zero_defects": (
        "All 1,707 Zero resting-HR records export as 0, and every Zero "
        "caloric_intake timestamp is zeroed to 0001-01-01. Both are vendor "
        "export bugs; the rows are landed raw but deliberately not projected."
    ),
    "cronometer_throttling": (
        "Cronometer's mobile API answers HTTP 200 with an EMPTY diary when "
        "rate-limited — indistinguishable from a day with nothing logged. Never "
        "trust a Cronometer day-scan that ran without a delay."
    ),
    "apple_health_is_downstream": (
        "Apple Health reaches the warehouse only through Cronometer's biometric "
        "passthrough. When cronometer.nutrition goes stale, apple_health.body "
        "and apple_health.metric go stale with it — one broken link, three "
        "stale streams. Don't debug them separately."
    ),
    "whoop_cycles_are_not_days": (
        "Whoop cycles run bedtime-to-bedtime. mart_daily re-buckets each cycle "
        "to the local date of its midpoint. Don't join raw cycle timestamps to "
        "calendar dates yourself."
    ),
}

WRITING = {
    "prefer_semantic_tools": (
        "log_food, submit_lab_results, submit_imaging_study, "
        "log_body_composition, log_whoop_workout, update_transaction — these "
        "handle source side-effects and downstream projection. Labs and "
        "imaging project into the unified layer immediately on write, so a "
        "panel uploaded in chat is queryable in the same turn."
    ),
    "generic_fallback": (
        "When no semantic tool fits: db_list_tables -> db_describe_table (get "
        "real columns and constraints, don't guess) -> db_insert/db_update/"
        "db_upsert with dry_run=true -> apply -> verify with ask_sql. Every "
        "write is audited in mcp_write_audit."
    ),
    "after_a_write": (
        "mart_daily is rebuilt nightly and every 4 hours. If the user needs a "
        "write reflected in a daily-grain answer immediately, say that it will "
        "appear after the next refresh rather than silently returning stale "
        "numbers."
    ),
}

RECIPES = {
    "is my training holding up": [
        "get_data_freshness(domain='activity')",
        "get_adherence(weeks=12)",
        "If below_floor is true, get_activity(last 12 weeks) to see which "
        "modality dropped out first.",
    ],
    "did the protein floor hold": [
        "get_data_freshness(domain='nutrition') — establish which days are "
        "even logged before averaging anything.",
        "get_nutrition(start, end) and report coverage alongside the mean; a "
        "170 g/day floor over 9 logged days out of 30 is not an average.",
    ],
    "how has bench press progressed": [
        "get_activity_exercises(start, end, exercise='bench_press') for the "
        "full multi-source series including the Garmin window.",
        "get_activity_sets(..., exercise='bench_press') for per-set detail "
        "where it exists (Sep 2025+).",
    ],
    "what changed since the last blood draw": [
        "get_lab_panels() to list draws.",
        "get_lab_values(start_date=<prior draw>) and compare by "
        "biomarker_key; status is precomputed against the source's own "
        "reference range where one was supplied.",
    ],
    "log a DXA": [
        "log_body_composition(measured_on, method='dxa', weight_kg=..., "
        "body_fat_pct=..., lean_mass_kg=..., visceral_fat=...)",
        "It outranks every other method on read from that moment.",
    ],
}

GUIDE = {
    "start_here": START_HERE,
    "tool_selection": TOOL_SELECTION,
    "gotchas": GOTCHAS,
    "writing": WRITING,
    "recipes": RECIPES,
}

TOPICS = sorted(GUIDE)


def guide_for(topic: str | None = None) -> dict:
    if topic is None:
        return {"topics": TOPICS, **GUIDE}
    hit = GUIDE.get(topic)
    if hit is None:
        return {"error": f"unknown topic {topic!r}", "topics": TOPICS}
    return {"topic": topic, topic: hit}

"""Hand-curated schema documentation surfaced via the get_schema_docs tool.

Per SPEC.md §7, the single biggest reason "ask my data anything" tools fail
is that the LLM doesn't know what fields mean. This file is the ground truth
the connector reads at the start of any analytical question.

Keep total length under ~3000 tokens so it fits comfortably alongside other
context. If you add new tables or columns, add them here too.
"""

from __future__ import annotations

SCHEMA_DOCS: dict = {
    "tables": {
        "mart_daily": {
            "purpose": (
                "One row per calendar day. The primary table for cross-source "
                "analysis. Always prefer this over fact_* tables for "
                "daily-grain questions."
            ),
            "grain": "1 row per day",
            "columns": {
                "day": "Local date (America/New_York). Not UTC.",
                "recovery_score": "Whoop recovery, 0-100. Higher = more recovered. Reflects the night's sleep ending on `day`.",
                "hrv_rmssd_ms": "HRV during last sleep, in milliseconds. Whoop's primary recovery signal. Healthy ~10-200 ms.",
                "resting_heart_rate": "Whoop RHR for the night ending on `day`.",
                "spo2_percentage": "Blood oxygen % during sleep. Healthy ~95-99%.",
                "skin_temp_celsius": "Skin temperature during sleep. Useful for spotting illness onset.",
                "sleep_total_hours": "Total in-bed time of the primary nightly sleep ending on `day`. Excludes naps.",
                "sleep_rem_hours": "REM sleep, in hours.",
                "sleep_slow_wave_hours": "Slow-wave (deep) sleep, in hours.",
                "sleep_efficiency_pct": "% of in-bed time actually asleep. 90%+ is good.",
                "sleep_performance_pct": "Whoop's performance score: how much sleep you got vs needed.",
                "sleep_consistency_pct": "Whoop's consistency score: how regular your bed/wake times are.",
                "sleep_start_ts, sleep_end_ts": "UTC timestamps of the primary night's sleep. Convert to local for human display.",
                "nap_count": "Naps logged for this day. Naps are NOT in sleep_total_hours.",
                "nap_total_min": "Sum of nap minutes for this day.",
                "strain": "Whoop's daily strain (0-21). Logarithmic scale. Whoop cycles run bedtime→bedtime, so neither cycle.start_ts nor end_ts cleanly maps to the activity day. mart_daily re-buckets each cycle to local_date(midpoint) — the day where the cycle's waking activity lives. Active (still-running) cycles treat now() as the open bound.",
                "day_kilojoules": "Estimated daily energy expenditure (kJ).",
                "workout_count": "Number of distinct logged workouts.",
                "workout_total_min": "Sum of workout durations.",
                "workout_total_kj": "Sum of workout-specific kJ (subset of day_kilojoules).",
                "workout_max_strain": "Highest single-workout strain.",
                "meeting_count": "Events classified as 'meeting' (>=2 attendees, not declined).",
                "meeting_hours": "Total meeting hours.",
                "meeting_internal_hours": "Meetings with no external attendees (just the configured INTERNAL_EMAIL_DOMAINS).",
                "meeting_external_hours": "Meetings with at least one external attendee.",
                "first_meeting_time, last_meeting_time": "Local time of earliest/latest meeting.",
                "longest_focus_block_min": "Largest unbroken solo block tagged 'focus' (regex match: focus|deep work|block|do not schedule|dns).",
                "total_focus_block_min": "Sum of focus blocks for the day.",
                "total_kcal": "From Cronometer's daily nutrition (preferred over summing fact_food_log).",
                "protein_g, carbs_g, fat_g, fiber_g, alcohol_g, caffeine_mg": "Macros + flagged micros from Cronometer's daily totals.",
                "meal_count": "Distinct food log entries.",
                "first_meal_time, last_meal_time": "Local time of first and last logged eating event.",
                "eating_window_hours": "Time between first and last meal. NOT a fasting window.",
                "breakfast_kcal, lunch_kcal, dinner_kcal, snack_kcal": "Sum of energy_kcal grouped by Cronometer's meal_group.",
                "total_spend": "Sum of non-excluded positive transactions (Copilot convention: positive = expense). 0 if no transactions on that day.",
                "food_spend, restaurant_spend, groceries_spend, transportation_spend": "Subsets of total_spend by category name match.",
                "alcohol_spend, bars_spend, entertainment_spend, shopping_spend, travel_spend": "Additional category subsets — useful for 'going-out' analysis. Bars matches 'Bars', 'Nightlife', or any category containing 'Bar'.",
                "dining_out_txn_count": "Number of restaurant/bar transactions >= $50 posted on this date. Cleanest single signal for 'did I go out the night before'. Threshold ($50) keeps coffee/lunch noise out.",
                "dining_out_txn_max": "Largest single restaurant/bar charge on this date.",
                "weight_kg": "Most recent weight measurement on this day, from fact_biometric.",
                "body_fat_pct": "Most recent body-fat % on this day.",
                "had_alcohol, alcohol_drinks": "From Whoop journal. had_alcohol is the yes/no answer; alcohol_drinks is the magnitude (drinks).",
                "had_caffeine, caffeine_servings, caffeine_last_serving_time": "From Whoop journal. Last serving time in local tz.",
                "late_meal, read_in_bed, device_in_bed, device_in_bed_minutes": "Sleep-affecting behaviors from Whoop journal.",
                "morning_sunlight, sexual_activity, stretching, rest_day": "Wellness behaviors from Whoop journal.",
                "took_magnesium, took_vitamin_d, took_creatine, took_l_theanine": "Supplement compliance from Whoop journal.",
                "joint_pain, headache": "Discomfort flags from Whoop journal.",
                "journal_notes": "Free-text notes from Whoop journal for the day.",
                "strength_total_volume_kg": "Sum of total_volume_kg across Hevy sessions on this day (working sets only). 0 on rest days. Use this for daily strength load alongside meeting/recovery context.",
                "strength_total_sets": "Total working+warmup sets across all Hevy sessions on the day. 0 on rest days.",
                "strength_unique_exercises": "Distinct dim_hevy_exercise.exercise_template_id values touched on the day. 0 on rest days.",
            },
            "common_queries": [
                "Average recovery for the last 30 days: SELECT AVG(recovery_score) FROM mart_daily WHERE day >= CURRENT_DATE - 30",
                "Days with low recovery and high meeting load: SELECT day, recovery_score, meeting_hours FROM mart_daily WHERE recovery_score < 50 AND meeting_hours > 4 ORDER BY day DESC",
                "Late-eating effect on next-day recovery: SELECT a.day, a.last_meal_time, b.recovery_score FROM mart_daily a JOIN mart_daily b ON b.day = a.day + 1",
            ],
            "gotchas": [
                "Days with no Whoop sync show NULL recovery, not 0.",
                "Nap minutes live in nap_total_min, separate from sleep_total_hours.",
                "Eating window is first→last meal, not the inverse fasting window.",
                "Spending uses Copilot's sign convention: positive = expense.",
            ],
        },
        "mart_meal": {
            "purpose": "Per-meal rollup of fact_food_log, grouped by Cronometer's meal_group (Snack 1/2/3 collapsed to 'snack').",
            "grain": "1 row per (day, meal_window)",
            "columns": {
                "day": "Local date.",
                "meal_window": "'breakfast' | 'lunch' | 'dinner' | 'snack'",
                "start_ts, end_ts": "First and last item eaten in this meal (UTC).",
                "duration_min": "Span between first and last item in the meal.",
                "item_count": "Distinct food items in the meal.",
                "total_kcal, protein_g, carbs_g, fat_g, fiber_g": "Sums across the meal's items.",
                "food_names": "Array of food names, ordered by eaten_at.",
            },
        },
        "mart_weekly": {
            "purpose": "Weekly rollup over mart_daily for trend questions.",
            "grain": "1 row per ISO week (Monday).",
            "columns": {
                "week_start": "Monday of the ISO week.",
                "avg_recovery_score, avg_hrv_rmssd_ms, avg_rhr": "Means across the week.",
                "total_strain, total_workout_min, total_meeting_hours": "Sums across the week.",
                "avg_meeting_hours_per_workday": "Mean meeting_hours filtered to Mon-Fri only.",
                "total_kcal, avg_kcal_per_day, avg_protein_g, total_spend": "Self-explanatory.",
            },
        },
        "fact_calendar_event": {
            "purpose": "One row per calendar event occurrence (recurring events expanded). Use when you need individual events, not daily aggregates.",
            "grain": "1 row per (calendar_id, event_id)",
            "columns": {
                "classification": "'meeting' | 'focus' | 'all_day' | 'declined' | 'personal'. Derived per SPEC.md §4.2.",
                "attendee_count": "Total invitees including self.",
                "attendee_internal_count, attendee_external_count": "Excludes self. Internal = email domain in INTERNAL_EMAIL_DOMAINS.",
                "response_status": "Self's response: accepted | declined | tentative | needsAction.",
                "has_video_link": "True if hangoutLink set or any conferenceData entryPoint is video.",
            },
        },
        "fact_workout": {
            "purpose": (
                "Per-workout detail (Whoop view: HR/strain/zones/kJ). For "
                "the per-set / weight / reps view of the same physical "
                "session, see fact_strength_workout — soft-linked via "
                "fact_strength_workout.whoop_workout_id."
            ),
            "grain": "1 row per workout.",
            "columns": {
                "sport_name": "Whoop's sport label (e.g. 'Running', 'Cycling').",
                "strain": "Whoop strain for just this workout.",
                "zone_zero_min..zone_five_min": "Minutes spent in each Whoop HR zone.",
            },
        },
        "fact_strength_workout": {
            "purpose": (
                "Per-strength-session rollup from Hevy. Covers what Whoop "
                "deliberately doesn't (per-exercise volume, sets, reps). "
                "whoop_workout_id is a soft FK to fact_workout, populated "
                "by ingest_hevy via a ±10/15min start/end window match — "
                "use that to join Whoop's HR/strain to Hevy's per-set data. "
                "ALWAYS prefer this over fact_workout for strength/volume "
                "questions."
            ),
            "grain": "1 row per Hevy workout.",
            "columns": {
                "hevy_workout_id": "UUID from Hevy. Joinable to fact_strength_set.",
                "title": "User-entered workout title from Hevy (e.g. 'Push Day').",
                "duration_seconds": "end_ts - start_ts.",
                "total_sets": "All sets, including warmups.",
                "total_reps": "Sum of reps across all sets.",
                "total_volume_kg": "Sum of weight_kg * reps over WORKING SETS ONLY (set_type != 'warmup').",
                "unique_exercises": "Distinct exercise_template_id count for this session.",
                "whoop_workout_id": "Soft FK to fact_workout when both apps logged the same physical session. NULL if no match within ±10/15min.",
            },
            "common_queries": [
                "Sessions in a window: SELECT * FROM fact_strength_workout WHERE day BETWEEN ... — but prefer get_strength_workouts.",
            ],
            "gotchas": [
                "total_volume_kg excludes warmups; if you want the truly raw figure, recompute from fact_strength_set without the set_type filter.",
                "whoop_workout_id is a SOFT link — Whoop re-ingests can change workout_ids; the link is recomputed on next Hevy ingest, not auto-repaired.",
            ],
        },
        "fact_strength_set": {
            "purpose": (
                "One row per set across every Hevy session. Use this for "
                "exercise-progression / per-rep questions. Composite PK "
                "(hevy_workout_id, exercise_index, set_index) — re-ingest "
                "is a clean DELETE+INSERT per workout."
            ),
            "grain": "1 row per (hevy_workout_id, exercise_index, set_index).",
            "columns": {
                "exercise_template_id": "FK to dim_hevy_exercise. NULL for custom-exercise sets the user entered freehand without selecting a template.",
                "exercise_title": "Hevy's display name (e.g. 'Front Squat (Barbell)').",
                "set_type": "warmup | normal | failure | dropset. Filter set_type='warmup' out of volume math.",
                "weight_kg": "Always kg in storage; the Hevy app handles lb-display locally.",
                "rpe": "Rate of perceived exertion, 0-10. Often NULL.",
                "day": "Generated from local_date(workout_start_ts). Pre-indexed.",
            },
        },
        "dim_hevy_exercise": {
            "purpose": "Hevy's exercise-template catalog (refreshable via `python -m ingest_hevy catalog`). The reference table for primary_muscle_group breakouts in get_strength_volume_trend.",
            "grain": "1 row per exercise template.",
            "columns": {
                "exercise_template_id": "Hevy's UUID. Used as fact_strength_set.exercise_template_id.",
                "exercise_type": "weight_reps | reps_only | duration | distance_duration.",
                "primary_muscle_group": "e.g. 'quadriceps', 'chest', 'back'.",
                "secondary_muscle_groups": "TEXT[] — additional muscles worked.",
                "is_custom": "True for user-defined templates; False for Hevy's built-in catalog.",
            },
        },
        "raw_hevy_workout": {
            "purpose": "Full JSON payload per Hevy workout. fact_strength_workout / fact_strength_set are derived from this. Query raw only if you need a payload field that isn't pivoted.",
            "grain": "1 row per Hevy workout.",
            "columns": {
                "hevy_workout_id": "UUID natural key.",
                "updated_at_src": "payload.updated_at — promoted out so the daily incremental can pick max(updated_at_src) as its `since` cursor.",
                "deleted": "Tombstone flag. Hevy /workouts/events 'deleted' marks the row, but the corresponding fact_* rows are hard-deleted on the same pass so reads stay clean.",
            },
        },
        "fact_pushpress_session": {
            "purpose": (
                "One row per programmed gym session from PushPress (the gym's "
                "workout-of-the-day across class types: CrossFit & HIIT, "
                "Barbell / Weightlifting Club, HYROX). Programmed = what the "
                "coach published, NOT what the user did — for the actually-"
                "performed view see fact_strength_workout (Hevy) and "
                "fact_workout (Whoop). Pulled by ingest_pushpress in a ±7-day "
                "window around today, daily at 4 AM ET. Always prefer the "
                "get_pushpress_* tools over raw SQL — they nest the parts[] "
                "array for free."
            ),
            "grain": "1 row per programmed (workout_uid).",
            "columns": {
                "workout_uid": "UUID natural key (PushPress's stable id).",
                "class_type_uuid": "FK to dim_pushpress_class_type. The programming track.",
                "class_type_name": "Denorm of dim_pushpress_class_type.name for fast list views.",
                "class_date": "Calendar date the workout is programmed for.",
                "workout_state": "PUBLISHED | DRAFT | SCHEDULED. Filter to PUBLISHED for 'what's actually on'.",
                "parts_count": "Number of fact_pushpress_part rows for this workout.",
                "divisions": "TEXT[] — union of part-level divisions (e.g. {Performance, Fitness}).",
                "whoop_workout_id": "Soft link to fact_workout on the same day. NULL if no Whoop session.",
            },
            "gotchas": [
                "Descriptions on fact_pushpress_part are FREEFORM PLAINTEXT (e.g. 'AMRAP 16: 50 Box Step-ups @ 20\"'). To get structured movement counts you need an LLM-parsed version — fact_pushpress_movement is reserved but not built yet.",
                "An empty raw_pushpress_workout_of_day row with is_empty=TRUE means we asked and got nothing back (rest day or unprogrammed) — distinct from 'we never fetched this date'.",
            ],
        },
        "fact_pushpress_part": {
            "purpose": (
                "Exploded parts of a PushPress programmed session. Each row "
                "is one section of the workout — POSTERIOR / WORKOUT OF THE "
                "DAY / A) Snatch / etc. — with the prescribed lift, score "
                "type, and the freeform plaintext description that names the "
                "movements and rep schemes."
            ),
            "grain": "1 row per (workout_uid, ordinal).",
            "columns": {
                "ordinal": "0-indexed position within the workout (coach-authored order preserved).",
                "title": "Section header (e.g. 'POSTERIOR', 'WORKOUT OF THE DAY').",
                "workout_title": "Prescribed lift / WOD name (e.g. 'Deadlifts', 'Get on your hands').",
                "description": "Freeform plaintext movements + rep schemes. The thing an LLM parser would consume.",
                "score_type": "Weight | Rounds/Reps | Time | (and others). Drives how the score field is interpreted.",
                "set_count": "Number of sets prescribed (API field 'sets').",
                "default_reps": "Default reps per set, when applicable.",
                "divisions": "TEXT[] — Performance / Fitness / RX divisions this part applies to.",
                "unit": "IMPERIAL | METRIC | NULL.",
            },
        },
        "raw_hevy_routine": {
            "purpose": (
                "Hevy routine templates (the user's saved programs — 'Push "
                "A', 'Pull B', etc.). Each row is one routine; "
                "payload->'exercises' is a JSONB array of {exercise_template_id, "
                "rest_seconds, sets: [{type, weight_kg, reps, rep_range, ...}]}. "
                "Workouts (raw_hevy_workout) are EXECUTIONS; routines are "
                "the PRESCRIPTIONS."
            ),
            "grain": "1 row per routine.",
            "columns": {
                "hevy_routine_id": "UUID natural key.",
                "title, folder_id": "Promoted out of payload for fast list queries.",
                "payload": "Full Hevy response, including the exercises[] / sets[] prescription.",
            },
            "common_queries": [
                "Routines in folder 42: SELECT title FROM raw_hevy_routine WHERE folder_id = 42 AND NOT deleted",
                "All exercises across routines: SELECT title, ex->>'exercise_template_id', s FROM raw_hevy_routine, jsonb_array_elements(payload->'exercises') ex, jsonb_array_elements(ex->'sets') s",
            ],
        },
        "raw_hevy_routine_folder": {
            "purpose": "Hevy routine folders for grouping templates (e.g. 'Hypertrophy block', 'Cutting phase'). Tiny table.",
            "grain": "1 row per folder.",
            "columns": {
                "folder_id": "Hevy's INT id (NOT a UUID).",
                "title": "Folder name.",
                "index": "Display order in Hevy's UI.",
            },
        },
        "fact_habit_log": {
            "purpose": "One row per (day, behavior). Sourced from Whoop's mobile journal — both user-entered behaviors and Apple-Health autofill rows. Behaviors not pivoted onto mart_daily live here for ad-hoc queries.",
            "grain": "1 row per (day, behavior_id).",
            "columns": {
                "habit_key": "dim_whoop_behavior.internal_name. Use this string in get_habit_history.",
                "behavior_id": "FK to dim_whoop_behavior.behavior_id (Whoop's numeric id).",
                "source": "'whoop_private_api' for user-logged behaviors, 'whoop_apple_health' for autofill rows from Whoop's Apple Health integration. User-logged wins on conflict.",
                "answered_yes": "Yes/No to the journal prompt. NULL if unanswered.",
                "magnitude_value, magnitude_unit": "Numeric input for behaviors that ask one (e.g. drinks, mg).",
                "time_input_value": "UTC timestamp for behaviors that ask 'when?' (e.g. last caffeine).",
                "user_reviewed": "True if Santi has finalized the entry, False/NULL if still draft.",
            },
        },
        "dim_whoop_behavior": {
            "purpose": "Whoop's behavior catalog (200+ trackable behaviors). Reference table for what habit_key values mean.",
            "grain": "1 row per behavior.",
            "columns": {
                "behavior_id": "Whoop's numeric id (PK). Used as fact_habit_log.behavior_id.",
                "internal_name": "Stable string id (e.g. 'alcohol', 'caffeine'). Use as habit_key.",
                "category": "DAYTIME / NIGHTTIME / YOUR WEEKLY PLAN / AUTOFILL / ...",
                "behavior_type": "POSITIVE / NEGATIVE / NORMAL / NOT_ACTIONABLE — Whoop's directionality hint.",
            },
            "gotchas": [
                "Rows with category='AUTOFILL' are synthesized by ingest_whoop_journal when an Apple-Health autofill row references a behavior_id that's not in the catalog yet. The next catalog refresh overwrites them with real metadata.",
            ],
        },
        "fact_journal_day": {
            "purpose": "Typed day-level pivot of the Whoop journal envelope (notes, cycle_id, sleep window). Source-of-truth for day-level journal data. fact_habit_log handles the per-behavior rows; this handles everything else.",
            "grain": "1 row per day.",
            "columns": {
                "journal_entry_id": "Whoop's BIGINT for the entry.",
                "cycle_id": "Whoop cycle id this journal entry belongs to.",
                "notes": "Free-text day notes from the Whoop app.",
                "user_reviewed": "True once Santi marked the entry done.",
                "sleep_during": "JSONB: the sleep window the journal applies to (if Whoop tagged one).",
            },
            "gotchas": [
                "mart_daily.journal_notes is sourced from raw_whoop_journal.payload (legacy); a follow-up PR will swap it to fact_journal_day.notes. For now, prefer mart_daily.journal_notes.",
            ],
        },
        "raw_whoop_journal": {
            "purpose": "Untyped JSON payloads from Whoop's /journal-service/v3/journals/drafts/mobile/{day}. One row per day. The fact_/dim_ tables are derived from this — query raw only if you need a field that isn't pivoted.",
            "grain": "1 row per day.",
            "columns": {
                "day": "The local date the entry covers.",
                "fetched_at": "When the ingester last upserted this row. Useful for staleness checks.",
                "payload": "Full Whoop response. tracked_behaviors[], notes, integrations.tracker_inputs[], sleep_during all live here.",
            },
        },
        "oauth_tokens": {
            "purpose": "Per-source credential store. Single row per `service`. `whoop_private` is the iPhone-bridge Whoop journal token (refreshed by the iPhone Shortcut, consumed by ingest_whoop_journal). Other services: `whoop` (public OAuth), `google`, `copilot`.",
            "grain": "1 row per service.",
            "columns": {
                "service": "Source key. Lookup with WHERE service = '<name>'.",
                "access_token": "Bearer token. Short-lived (typically 24h for Whoop).",
                "refresh_token": "Long-lived. Either rotated by the source on each refresh, or held stable for ~30 days (Whoop).",
                "id_token": "Optional. Cognito JWT with identity claims (sub, email). Populated for whoop_private; null for OAuth-only sources.",
                "expires_at": "When access_token expires. ingest_whoop_journal raises WhoopAuthExpired if past this minus a 5-min skew.",
                "metadata": "JSONB. Provenance hints like {\"source\": \"ios_shortcut_refresh\", \"received_at\": \"...\"}.",
            },
            "gotchas": [
                "Don't update partial — always pass all five token columns when writing, or you'll silently null id_token / metadata.",
                "Refresh logic for whoop_private lives on the iPhone, not the server. If the row is stale, the fix is to re-trigger the Shortcut, not to call Cognito from the server.",
            ],
        },
        "dim_lab_biomarker": {
            "purpose": (
                "Catalog of every biomarker Whoop Advanced Labs measures (75 in "
                "the Comprehensive Health Panel). Curated reference data: "
                "description, category, optimal/sufficient ranges, what high/low "
                "means, and influencing factors. Joined to fact_lab_result on "
                "biomarker_id. CONSULT THIS FOR ANY HEALTH QUESTION involving "
                "blood markers, hormones, lipids, kidney/liver function, "
                "inflammation, or vitamins."
            ),
            "grain": "1 row per biomarker.",
            "columns": {
                "biomarker_id": "Whoop's stable slug — e.g. 'apolipoprotein_b', 'vitamin_d', 'hemoglobin_a1c'.",
                "category": "Cardiometabolic | Liver | Kidney | Hormones | Inflammation | Blood Count | Iron Metabolism | Vitamins & Minerals.",
                "optimal_low, optimal_high": "Tight clinical target (Whoop's 'Optimal' band). Inside this = OPTIMAL classification.",
                "sufficient_low, sufficient_high": "Outer acceptable range. Outside this = OUT_OF_RANGE.",
                "what_high_means, what_low_means": "Clinical interpretation strings — surface these when explaining a result.",
                "influenced_by": "Lifestyle / drug / physiologic factors that shift the value.",
            },
        },
        "fact_lab_result": {
            "purpose": (
                "Per-biomarker results from Whoop Advanced Labs panels. One row "
                "per (test_id, biomarker_id). Always join to dim_lab_biomarker "
                "to get the description and reference ranges — do that via the "
                "get_lab_results tool, which composes the join for you."
            ),
            "grain": "1 row per (test_id, biomarker_id).",
            "columns": {
                "value_text": "Raw value as Whoop displays it (e.g. '6.0', '293.0', '0.31').",
                "value_numeric": "Parsed numeric for filtering/comparison.",
                "status_type": "OPTIMAL | SUFFICIENT | OUT_OF_RANGE — Whoop's classification.",
                "trend": "POSITIVE_RANGE | SUFFICIENT_BLUE | CONCERN_RANGE — UI tint hint.",
                "indicator_percent": "Where the value sits on Whoop's range meter, 0-1.",
                "range_meter": "JSONB of normalized meter sections + indicator. UI geometry, not absolute units.",
            },
            "common_queries": [
                "Out of range: SELECT biomarker_id, value_text, unit FROM fact_lab_result WHERE status_type = 'OUT_OF_RANGE'",
                "Hormones: prefer get_lab_results(category='Hormones').",
            ],
            "gotchas": [
                "range_meter holds 0-1 normalized positions, NOT absolute reference range bounds. For absolute ranges read dim_lab_biomarker.optimal_low/high or sufficient_low/high.",
                "Same biomarker can appear in multiple panels over time — filter by test_id or test_date for a specific draw.",
            ],
        },
        "raw_whoop_labs": {
            "purpose": "Raw JSON payloads from Whoop Advanced Labs panels. Keyed by test_id.",
            "grain": "1 row per panel test.",
            "columns": {
                "test_id": "Whoop's UUID for the test.",
                "test_name": "e.g. 'Comprehensive Health Panel (75)'.",
                "test_date": "Date of blood draw (parsed from the panel header).",
                "payload": "Full UI JSON dump — only useful as a fallback; everything actionable is parsed into fact_lab_result.",
            },
        },
        "fact_food_daily_apple_health": {
            "purpose": "Daily macros from Apple Health, pulled via Whoop's integrations.tracker_inputs. Use for cross-validation with Cronometer's fact_food_daily, or as a fallback when Cronometer is broken.",
            "grain": "1 row per day.",
        },
        "fact_food_log": {
            "purpose": "Every individual food item logged in Cronometer with timestamp.",
            "grain": "1 row per logged serving.",
            "columns": {
                "eaten_at": "UTC timestamp. Cronometer per-meal time (Gold required).",
                "meal_group": "Cronometer label: Breakfast | Lunch | Dinner | Snack 1/2/3 | Uncategorized.",
                "micros": "JSONB of every micronutrient Cronometer reported (vitamins, minerals, amino acids).",
            },
            "gotchas": (
                "Two paths feed this table: (1) the nightly Go-binary GWT "
                "pipeline (servings + daily-nutrition CSV → fact_food_log / "
                "fact_food_daily) and (2) the MCP write tools (log_food / "
                "delete_food_entry) which hit mobile.cronometer.com to write "
                "to Cronometer's diary, then immediately auto-trigger path "
                "(1) so the new row lands here within seconds. mart_daily "
                "rollups are NOT auto-refreshed — call refresh_data('mart') "
                "if you need today's totals reflected. Batch flows can pass "
                "sync=False on each write and sync once at the end."
            ),
        },
        "fact_transaction": {
            "purpose": "Every Copilot Money transaction.",
            "grain": "1 row per transaction.",
            "columns": {
                "amount": "Positive = expense, negative = income or refund (Copilot convention).",
                "is_excluded": "True if 'exclude from totals' is set in Copilot.",
                "category_id": "FK to dim_category.",
                "tags": "TEXT[] of human-readable tag names (e.g. ['Santi', 'Trip Tahiti']). Use the `tag` filter on get_transactions to query by name.",
                "tag_ids": "TEXT[] of Copilot tag UUIDs aligned with `tags`. Used internally by set_couple_tag.",
                "is_reviewed": "True if you've marked the transaction reviewed in Copilot.",
                "tip_amount": "Tip portion of the transaction, if any.",
                "parent_id": "If this is a split transaction, the parent's ID.",
                "copilot_type": "Copilot's internal type field (regular | transfer | etc.).",
            },
        },
    },
    "metric_glossary": {
        "hrv_rmssd_ms": "Root mean square of successive differences between heartbeats during the last slow-wave sleep period, in milliseconds. Whoop's headline recovery input.",
        "strain": "Whoop's cardiovascular load score on a 0-21 logarithmic scale. ~10 = light day, 14+ = strenuous.",
        "recovery_score": "Whoop's 0-100 score combining HRV, RHR, sleep performance, and respiratory rate.",
        "sleep_efficiency_pct": "Fraction of in-bed time spent actually asleep. 90%+ is good.",
        "meeting_hours_classification": "Events classified as 'meeting' must have ≥2 attendees and not be declined by self.",
    },
    "conventions": {
        "timezone": "All `day` columns are in America/New_York. All `*_ts` columns are UTC TIMESTAMPTZ.",
        "amount_sign_copilot": "fact_transaction.amount: positive = expense, negative = income or refund.",
        "calendar_classification": "Events are classified as 'meeting', 'focus', 'all_day', 'declined', or 'personal'. See fact_calendar_event docs.",
        "null_means_missing": "NULL values mean the source had no data, not zero. Never treat NULL as 0 unless the question explicitly calls for that imputation.",
        "preferred_table_for_daily_grain": "Always start with mart_daily for any daily-grain question. fact_* tables are for per-event detail.",
    },
    "session_prelude": (
        "If the user asks about RECENT data (today, this week, latest, "
        "current balance, etc.) call `refresh_data(source='all')` BEFORE "
        "anything else. The cron only runs every few hours, so without a "
        "refresh you may be analyzing stale data. For purely historical "
        "questions (e.g. 'how was my recovery in March?') you can skip the "
        "refresh — historical data doesn't change."
    ),
    "write_workflows": {
        "auto_categorize": (
            "User asks to categorize uncategorized transactions: "
            "1) `get_transactions(start_date, end_date)` filtered to where "
            "category is NULL or 'Uncategorized'. "
            "2) Propose a category for each based on merchant. "
            "3) For each: `update_transaction_category(transaction_id, category_id)` "
            "OR for many at once with same category: `bulk_update_transactions("
            "filter={merchantName: 'Netflix'}, category_id='cat_xyz')`. "
            "Get category_id via the `dim_category` table (use `ask_sql` or "
            "the get_transactions output which includes category_id)."
        ),
        "edit_anything": (
            "Any single-field or multi-field edit to a transaction: "
            "`update_transaction(transaction_id, ...)`. Available fields: "
            "category_id, user_notes, name (merchant rename), amount, date "
            "(YYYY-MM-DD), tip_amount, is_reviewed, copilot_type, hidden, "
            "tag_ids. Pass only what you want to change. Local DB is "
            "re-fetched after."
        ),
        "fix_recurring": (
            "Copilot mis-categorized a one-off as recurring (or vice versa). "
            "1) `get_transactions(...)` to find the txn and its recurring_id. "
            "2) `exclude_transaction_from_recurring(transaction_id)` to detach. "
            "3) Or `add_transaction_to_recurring(transaction_id, recurring_id)` "
            "to attach to a known stream. To find recurring stream ids of "
            "existing streams, look at recurring_id values in get_transactions output."
        ),
        "tag_a_trip": (
            "User asks to tag trip expenses: "
            "1) `list_tags()` to find or `create_tag(name)` if missing. "
            "2) `get_transactions` for the date range. "
            "3) For each matching transaction: `tag_transaction(transaction_id, "
            "[trip_tag_id, ...other_tag_ids])` — REPLACES the tag set, so "
            "include any tags you want to keep."
        ),
        "log_food_to_cronometer": (
            "User asks to log a food / meal to Cronometer. "
            "1) `search_foods(query)` — pick the best match (database foods "
            "have brand + source; user's own custom foods appear too). Note "
            "the food_id, measure_id, and translation_id. "
            "2a) If found: `log_food(food_id, grams, measure_id, "
            "translation_id, meal_window='breakfast|lunch|dinner|snacks', "
            "eaten_at='YYYY-MM-DD' or ISO datetime)`. For 'X servings' "
            "multiply X by the serving gram weight (visible in search "
            "results' measure_display). log_food auto-resolves "
            "defaultMeasureId if measure_id is omitted. "
            "2b) If NOT found (restaurant meal, recipe, etc.): "
            "`create_custom_food(name, serving_size_g, calories, protein_g, "
            "fat_g, carbs_g, ...)` then call `log_food` with the returned "
            "food_id + measure_id. Macros for custom foods are PER SERVING. "
            "3) Tool returns entry_id; keep it for delete_food_entry. "
            "4) log_food and delete_food_entry both auto-sync fact_food_log "
            "and fact_food_daily after each write (Go binary subprocess, "
            "~5-15s). So a subsequent get_food_log in the same conversation "
            "WILL see the new entry. mart_daily columns are NOT refreshed "
            "automatically — run refresh_data('mart') if a daily-grain query "
            "needs the updated total. "
            "5) For batch logging (e.g. logging an entire day's food at "
            "once), pass sync=False on each call except the last to avoid "
            "running the Go binary N times."
        ),
        "couples_split": (
            "User shares finances with partner. Three tags carry meaning: "
            "'me' / 'paulina' / 'joint' (configurable). Workflow: "
            "1) `refresh_data('copilot')` to ensure fresh transactions. "
            "2) `list_pending_couple_review(start, end)` — transactions in "
            "window with no couple tag. "
            "3) For each: ask the user 'me / paulina / joint?' then call "
            "`set_couple_tag(transaction_id, owner)`. "
            "4) When done (or anytime): `compute_couple_balances(start, end)` "
            "returns 'X owes Y $N' plus per-transaction breakdown. "
            "If `list_account_owners()` shows accounts as 'unassigned', tell "
            "the user to set COUPLE_ACCOUNTS_ME / _PARTNER / _JOINT in .env "
            "(comma-separated account_ids) so the balance math knows who paid."
        ),
        "couples_owed_on_specific_cards": (
            "User asks: 'how much do me and my wife owe on the joint Chase + "
            "Amazon card this month, joint split 65/35'. Use compute_couple_owed "
            "directly — no pre-tagging required. Pass account_names=['Chase','Amazon'] "
            "(or account_ids), split_me=0.65, split_partner=0.35. The tool "
            "auto-flags pending+posted duplicates and surfaces untagged charges "
            "in needs_review for the user to triage. ONE tool call replaces the "
            "old multi-step flow."
        ),
        "health_question_with_labs": (
            "User asks about anything blood-test-adjacent — energy, fatigue, "
            "recovery problems, hormones, libido, lipids/cholesterol, liver, "
            "kidney, inflammation, vitamin status, sleep + biomarkers, etc. "
            "1) `get_lab_results()` — the latest panel, sorted out-of-range "
            "first. Skim the OUT_OF_RANGE rows to identify what's actually "
            "off. "
            "2) For specific biomarker deep dives: `get_biomarker_info("
            "biomarker_id)` returns description, optimal/sufficient ranges, "
            "what high/low means, influencing factors — pair with the "
            "user's value to give a grounded answer. "
            "3) Reach for `get_lab_results(category='Hormones')` (or "
            "'Cardiometabolic', 'Liver', 'Kidney', 'Inflammation', "
            "'Blood Count', 'Iron Metabolism', 'Vitamins & Minerals') to "
            "scope to one body system. "
            "4) Reference recovery/sleep/journal data alongside (mart_daily) "
            "when the user asks how lifestyle correlates."
        ),
        "self_observability": (
            "User asks 'why is the MCP slow' or 'which tools are getting called "
            "the most'. Call get_tool_stats(window_minutes=60 or 1440). Returns "
            "per-tool n, p50/p95 latency, and error count from mcp_tool_log."
        ),
        "manage_hevy_routines": (
            "Routines are TEMPLATES (the prescription); workouts are "
            "SESSIONS (the execution). Use routines when the user wants a "
            "reusable program ('build me a 3-day push/pull/legs', 'save "
            "this as my Tuesday workout'); use log_strength_workout when "
            "they're logging today's actual session. Discovery: "
            "list_routine_folders → list_routines(folder_id=...) → "
            "get_routine(id). Editing: update_routine REPLACES the whole "
            "thing (read first via get_routine, then re-send with edits — "
            "folder_id is not updatable through PUT). Routines support "
            "rep_range {start, end} per set — use this when you want to "
            "prescribe '8-12 reps' rather than a fixed target. Workouts "
            "support RPE per set; routines do not. Folder moves require "
            "the Hevy mobile app — only NEW folders can be created via "
            "API. To follow a routine in a session: get_routine(id), "
            "translate prescribed sets into actual sets (with the weights "
            "the user lifted), call log_strength_workout."
        ),
        "log_strength_workout": (
            "User says 'log a workout' / 'I just did X / Y / Z' / 'add this "
            "to Hevy'. "
            "1) For each exercise the user mentions, call "
            "find_exercise_templates(query='front squat') to resolve the "
            "free-text name to an 8-char exercise_template_id. If multiple "
            "match, pick the one whose title best matches (prefer 'Front "
            "Squat (Barbell)' over 'Front Squat (Smith Machine)' unless "
            "the user specified equipment). "
            "2) Build the `exercises` list with sets. type defaults to "
            "'normal'; mark warmups explicitly as 'warmup'. weight_kg, "
            "reps, rpe optional per set — but for typical lifts include "
            "weight_kg + reps. "
            "3) Call log_strength_workout(title, start_time, end_time, "
            "exercises, dry_run=True) FIRST to surface validation errors "
            "without polluting the user's Hevy account. "
            "4) Once the dry-run looks right, re-call without dry_run. The "
            "tool returns the hevy_workout_id and a hevy.com URL. "
            "5) Mention to the user that mart_daily strength columns won't "
            "show the new workout until refresh_data('mart') runs."
        ),
        "strength_progression": (
            "User asks about lifting / sets / reps / PRs / volume / "
            "specific-exercise progress. Tools: "
            "1) get_strength_workouts(start, end) for a window of sessions. "
            "2) get_exercise_progression(exercise_search='squat', start, end, "
            "metric='top_weight'|'estimated_1rm'|...) for time-series of one "
            "lift with a PR + 30-day-trend summary. "
            "3) get_strength_volume_trend(start, end, granularity='week', "
            "group_by_muscle_group=true) for push/pull-balance and weekly load. "
            "4) get_strength_sets(...) for raw per-set data when you need "
            "set-level detail (e.g. RPE distribution, failure-set count). "
            "Strength sessions are also linked to Whoop's HR/strain view: "
            "get_strength_workouts returns whoop_strain + whoop_avg_hr when "
            "the user wore the band, and get_workouts now carries the linked "
            "hevy_workout_id + strength rollup."
        ),
    },
    "body_image_surface": {
        "purpose": (
            "Daily headshot rating pipeline. iOS Shortcut posts a 3-photo "
            "session (front / 3-4 left / 3-4 right) to /body-image/upload. "
            "Each photo gets rated by every configured LLM rater (Claude "
            "+ optionally GPT-4o + Gemini), each split into Structure + "
            "Surface specialist calls, plus a MediaPipe geometry sidecar "
            "(front photo only — 3-4 angles get the geometry skipped). "
            "Scores are calibrated three ways: anchored on 5 SCUT-FBP "
            "Caucasian-Male reference photos, ceiling-rule rubric "
            "(weakest feature drags overall), and personal slope/offset "
            "(slope*raw + offset, derived from 15 blind user-rated "
            "photos via Track B)."
        ),
        "tables": [
            "body_image_photo (id, session_id, storage_path, caption, "
            "metadata (incl. angle), created_at)",
            "body_image_rating (photo_id, source, run_index, overall, "
            "dimensions JSONB) — source ∈ {claude_structure, "
            "claude_surface, gpt4v_*, gemini_*, geometry}; dimensions "
            "JSONB has per-feature 0-100 scores plus qualitative arrays "
            "three_biggest_structural_negatives, "
            "three_biggest_surface_negatives, "
            "three_highest_roi_structural_changes, "
            "three_highest_roi_surface_changes; overall is the "
            "personally-calibrated score, dimensions._raw_overall keeps "
            "the model's pre-correction output",
            "body_image_intervention (intervention_key, event in "
            "{start,stop,apply,milestone}, occurred_on, metadata) — "
            "behavior changes that overlay every dashboard trend chart",
            "body_image_recommendation (window_days, brief JSONB) — "
            "weekly synthesized brief from body_image.coach with themes "
            "→ tagged actions + avoid lists",
            "mart_body_image_daily (one row per day per user with "
            "body_image_overall + per-feature averages + geometry). "
            "Mirror columns also live on mart_daily so correlate_metrics "
            "sees body-image alongside HRV/sleep/alcohol etc.",
        ],
        "tools": [
            "get_body_image_summary  — daily composite trend",
            "get_body_image_sessions — 3-photo sessions with per-photo ratings",
            "get_body_image_photo     — single-photo deep dive",
            "get_body_image_critique  — top recurring qualitative themes",
            "get_body_image_interventions — behavior-change log",
            "get_body_image_geometry  — symmetry / gonial / jaw-ratio time series",
            "get_body_image_recommendations — latest synthesized brief",
            "log_body_image_intervention — write a new intervention",
            "regenerate_body_image_recommendations — fresh brief (~$0.10)",
        ],
        "lag_examples": (
            "skin_clarity vs alcohol_g (lag 1-3 days), under_eye vs "
            "sleep_consistency_pct (lag 1), overall vs recovery_score "
            "(lag 1). Use correlate_metrics with lag_range=[0,7]."
        ),
    },
    "performance_tips": {
        "prefer_mart_daily": "For any daily-grain question (recovery, sleep, spend trends), mart_daily already has the joins done — one query, no expression-evaluation overhead.",
        "lag_sweeps_in_one_call": "Use correlate_metrics with lag_range=[min,max] (up to 21 lags) instead of N separate calls. Returns the best-magnitude lag plus per-lag stats.",
        "narrow_with_account": "Spending questions about specific cards: pass account_id (exact) or account=substring on get_spending / get_transactions instead of pulling all transactions and filtering client-side.",
        "use_compute_couple_owed": "For 'who owes whom on these specific cards' questions, compute_couple_owed is one call. Don't combine list_account_owners + get_transactions + Python.",
        "ask_sql_timeout": "ask_sql defaults to 15s statement_timeout. For heavier analytics you can raise via timeout_ms (max 60s). Pass explain=true to inspect a slow query's plan first.",
    },
}


def docs_for(table_name: str | None = None) -> dict:
    """Return the full docs blob, or just the entry for one table."""
    if table_name is None:
        return SCHEMA_DOCS
    tbl = SCHEMA_DOCS["tables"].get(table_name)
    if tbl is None:
        return {
            "error": f"No docs for table '{table_name}'.",
            "available": sorted(SCHEMA_DOCS["tables"].keys()),
        }
    return {
        "table": table_name,
        **tbl,
        "conventions": SCHEMA_DOCS["conventions"],
    }

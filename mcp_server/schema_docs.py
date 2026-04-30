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
                "strain": "Whoop's daily strain (0-21). Logarithmic scale.",
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
            "purpose": "Per-workout detail. Zone breakdown for HR-zone analysis.",
            "grain": "1 row per workout.",
            "columns": {
                "sport_name": "Whoop's sport label (e.g. 'Running', 'Cycling').",
                "strain": "Whoop strain for just this workout.",
                "zone_zero_min..zone_five_min": "Minutes spent in each Whoop HR zone.",
            },
        },
        "fact_habit_log": {
            "purpose": "One row per (day, Whoop behavior). Sourced from Whoop's mobile journal. Behaviors not pivoted onto mart_daily live here for ad-hoc queries.",
            "grain": "1 row per (day, whoop_behavior_id).",
            "columns": {
                "habit_key": "dim_whoop_behavior.internal_name. Use this string in get_habit_history.",
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
                "internal_name": "Stable string id (e.g. 'alcohol', 'caffeine'). Use as habit_key.",
                "category": "DAYTIME / NIGHTTIME / YOUR WEEKLY PLAN / ...",
                "behavior_type": "POSITIVE / NEGATIVE / NORMAL / NOT_ACTIONABLE — Whoop's directionality hint.",
            },
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

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
                "total_spend": "Sum of non-excluded positive transactions (Copilot convention: positive = expense).",
                "food_spend, restaurant_spend, groceries_spend, transportation_spend": "Subsets of total_spend by category name match.",
                "weight_kg": "Most recent weight measurement on this day, from fact_biometric.",
                "body_fat_pct": "Most recent body-fat % on this day.",
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
            "3) For each: `update_transaction_category(transaction_id, category_id)`. "
            "Get category_id via the `dim_category` table (use `ask_sql` to list)."
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

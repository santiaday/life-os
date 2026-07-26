"""Unification layer.

Projects every source-specific fact table and every `raw_import` row into the
canonical tables from migration 0036:

    fact_activity / fact_activity_exercise / fact_activity_set
    fact_sleep_session
    fact_nutrition_entry / fact_nutrition_daily
    fact_body_composition
    fact_daily_metric
    fact_fast

Source ingesters keep writing to their own tables; nothing here changes their
behaviour. `python -m unify all` re-derives the canonical layer from whatever
is currently landed, and is safe to run repeatedly — every write is an upsert
on a deterministic natural key.
"""

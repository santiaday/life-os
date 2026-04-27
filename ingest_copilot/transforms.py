"""Pure transforms: Copilot GraphQL row → fact/dim dicts.

Sign convention preserved as-is (Copilot returns positive amount = expense,
negative = income/refund). Documented in schema_docs.conventions.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def transform_account(api: dict) -> dict:
    """Map Copilot Account → dim_account row.

    Notes:
    - Copilot doesn't return institution name on the account itself (just
      institutionId). Leaving institution NULL until we add a separate
      institutions query.
    - is_hidden = true if user explicitly hid OR closed the account.
    - currency isn't in the account schema; default USD.
    """
    return {
        "account_id": api["id"],
        "name": api.get("name") or "Unknown",
        "institution": api.get("institutionId"),
        "type": api.get("subType") or api.get("type"),
        "currency": "USD",
        "is_hidden": bool(api.get("isUserHidden")) or bool(api.get("isUserClosed")),
    }


def transform_category(api: dict) -> dict:
    """Map Copilot Category → dim_category row. `_parent_id` is injected by
    the GraphQL client when flattening the nested childCategories shape."""
    return {
        "category_id": api["id"],
        "name": api.get("name") or "Unknown",
        "parent_category_id": api.get("_parent_id"),
        # Copilot doesn't expose income/expense/transfer as a categorical type
        # on the category itself; leave NULL.
        "type": None,
        "is_hidden": bool(api.get("isExcluded")),
    }


def transform_transaction(api: dict) -> dict:
    """Map Copilot Transaction → fact_transaction row."""
    return {
        "transaction_id": api["id"],
        "date": _to_date(api.get("date")),
        "posted_ts": _to_dt(api.get("createdAt")),
        "amount": float(api["amount"]),
        "currency": (api.get("isoCurrencyCode") or "USD").upper(),
        # `name` is the merchant string in Copilot's schema (the field is
        # called name, not merchant — they renamed at some point).
        "merchant": api.get("name"),
        # Copilot has no separate description; userNotes carries any free-text.
        "description": api.get("userNotes"),
        "category_id": api.get("categoryId"),
        "account_id": api.get("accountId"),
        "is_pending": bool(api.get("isPending")),
        "is_recurring": api.get("recurringId") is not None,
        # Per-transaction "exclude from totals" doesn't appear in the new
        # schema (categories carry isExcluded instead). Mart layer joins
        # through dim_category.is_hidden if you want category-level exclude.
        "is_excluded": False,
        "notes": api.get("userNotes"),
    }


def _to_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _to_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None

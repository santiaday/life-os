"""Pure transforms: Copilot GraphQL row → fact/dim dicts.

Sign convention preserved as-is (positive = expense). Documented in
schema_docs.conventions.amount_sign_copilot.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def transform_account(api: dict) -> dict:
    return {
        "account_id": api["id"],
        "name": api.get("name") or "Unknown",
        "institution": api.get("institution"),
        "type": api.get("type"),
        "currency": (api.get("currency") or "USD").upper(),
        "is_hidden": bool(api.get("isHidden")),
    }


def transform_category(api: dict) -> dict:
    return {
        "category_id": api["id"],
        "name": api.get("name") or "Unknown",
        "parent_category_id": _opt_id(api.get("parent")),
        "type": api.get("type"),
        "is_hidden": bool(api.get("isHidden")),
    }


def transform_transaction(api: dict) -> dict:
    """fact_transaction row from a transactions GraphQL record.
    `amount` left as-is (Copilot's sign convention)."""
    cat = api.get("category") or {}
    acct = api.get("account") or {}
    return {
        "transaction_id": api["id"],
        "date": _to_date(api.get("date")),
        "posted_ts": _to_dt(api.get("postedAt")),
        "amount": float(api["amount"]),
        "currency": (api.get("currency") or "USD").upper(),
        "merchant": api.get("merchant"),
        "description": api.get("description") or api.get("note"),
        "category_id": cat.get("id"),
        "account_id": acct.get("id"),
        "is_pending": bool(api.get("isPending")),
        "is_recurring": bool(api.get("isRecurring")),
        "is_excluded": bool(api.get("isExcluded")),
        "notes": api.get("note"),
    }


def _opt_id(v: Any) -> str | None:
    if not v:
        return None
    if isinstance(v, dict):
        return v.get("id")
    return str(v)


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
    return datetime.fromisoformat(s)

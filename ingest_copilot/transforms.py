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
    tags = api.get("tags") or []
    return {
        "transaction_id": api["id"],
        "date": _to_date(api.get("date")),
        "posted_ts": _to_dt(api.get("createdAt")),
        "amount": float(api["amount"]),
        "currency": (api.get("isoCurrencyCode") or "USD").upper(),
        "merchant": api.get("name"),
        "description": api.get("userNotes"),
        "category_id": _nonempty(api.get("categoryId")),
        "account_id": _nonempty(api.get("accountId")),
        "is_pending": bool(api.get("isPending")),
        "is_recurring": api.get("recurringId") is not None,
        # Per-transaction "exclude from totals" isn't in the new Copilot
        # schema; categories carry isExcluded instead.
        "is_excluded": False,
        "notes": api.get("userNotes"),
        # 0007 metadata (Copilot per-transaction extras).
        "tags": [t.get("name") for t in tags if t.get("name")],
        "tag_ids": [t.get("id") for t in tags if t.get("id")],
        "is_reviewed": bool(api.get("isReviewed")),
        "tip_amount": float(api["tipAmount"]) if api.get("tipAmount") is not None else None,
        "parent_id": _nonempty(api.get("parentId")),
        "copilot_type": api.get("type"),
    }


def _nonempty(v: Any) -> Any:
    """Convert empty string to None; pass through everything else. Used to
    coerce Copilot's "" sentinel for uncategorized/unassigned FK columns."""
    if v == "" or v is None:
        return None
    return v


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

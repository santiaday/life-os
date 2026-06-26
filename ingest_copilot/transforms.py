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


# Copilot removed per-transaction "exclude from totals" from its GraphQL
# schema (read AND write) as of ~2026-06, and its `type=INTERNAL_TRANSFER`
# is unreliable (it mislabels real merchants like Slack / restaurants as
# transfers). So we can't trust Copilot for exclusion. Instead we derive
# is_excluded from the merchant NAME using a conservative allow-list of
# genuine transfer/payment merchants — credit-card payments, brokerage and
# savings transfers, etc. These substrings were verified against the full
# transaction history to match ONLY transfers (no real expenses). Keeping
# this here means every sync re-applies it, so transfers stay out of spend
# totals durably and automatically for future transactions too.
_TRANSFER_NAME_PATTERNS = (
    "thank",                          # Payment Thank You / Autopay Payment - Thank You / ...
    "withdrawal to expense account",  # Wealthfront cash withdrawal
    "chase credit crd",
    "synchrony bank",
    "applecard",
    "barclaycard",
    "citi card online",
    "capital one (account",           # Capital One inter-account moves
    "capital one bank",
    "wealthfront",
    "kalshi",
    "robinhood",
    "joint savings",                  # Withdrawal To / Deposit From Joint Savings
    "house savings",
    "cross_account_transfer",
    "ssbtrustops",
    "returned payment",
    "payment escrow",
    "check deposit",
)


def _is_transfer(name: str | None) -> bool:
    """True if the merchant name is a genuine transfer/payment that should be
    excluded from spend totals. Name-based (not Copilot's unreliable type)."""
    if not name:
        return False
    n = name.lower()
    return any(p in n for p in _TRANSFER_NAME_PATTERNS)


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
        # Derived transfer exclusion — see _is_transfer / _TRANSFER_NAME_PATTERNS.
        "is_excluded": _is_transfer(api.get("name")),
        "notes": api.get("userNotes"),
        # 0007 metadata (Copilot per-transaction extras).
        "tags": [t.get("name") for t in tags if t.get("name")],
        "tag_ids": [t.get("id") for t in tags if t.get("id")],
        "is_reviewed": bool(api.get("isReviewed")),
        "tip_amount": float(api["tipAmount"]) if api.get("tipAmount") is not None else None,
        "parent_id": _nonempty(api.get("parentId")),
        "copilot_type": api.get("type"),
        # 0008: itemId is required by the editTransaction mutation alongside
        # accountId + id. Persist it so writes don't need a fetch first.
        "item_id": _nonempty(api.get("itemId")),
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

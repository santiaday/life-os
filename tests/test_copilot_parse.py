"""Tests for ingest_copilot.transforms.

Sign convention preservation, FK shape, pending/recurring/excluded flags,
nullable parent on dim_category.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ingest_copilot import transforms


@pytest.fixture
def fx() -> Path:
    return Path(__file__).parent / "fixtures" / "copilot"


def _txn(fx: Path) -> list[dict]:
    return json.loads((fx / "transactions.json").read_text())


# ---- transactions ---------------------------------------------------------
def test_transform_transaction_grocery_expense(fx):
    api = _txn(fx)[0]
    row = transforms.transform_transaction(api)
    assert row["transaction_id"] == "txn_aa11"
    assert row["date"].isoformat() == "2025-04-01"
    assert row["amount"] == pytest.approx(47.83)  # Copilot convention: positive = expense
    assert row["merchant"] == "Whole Foods Market"
    assert row["category_id"] == "cat_groc"
    assert row["account_id"] == "acct_chase"
    assert row["is_pending"] is False
    assert row["is_excluded"] is False
    assert row["notes"] == "Weekly grocery run"


def test_transform_transaction_pending(fx):
    api = _txn(fx)[1]
    row = transforms.transform_transaction(api)
    assert row["is_pending"] is True
    assert row["category_id"] == "cat_rest"


def test_transform_transaction_income_negative(fx):
    """Income is stored as negative — we preserve the sign."""
    api = _txn(fx)[2]
    row = transforms.transform_transaction(api)
    assert row["amount"] == pytest.approx(-2500.00)
    assert row["is_recurring"] is True
    assert row["account_id"] == "acct_check"


def test_transform_transaction_handles_missing_posted_at(fx):
    """posted_ts is optional — non-posted txns have None."""
    api = _txn(fx)[2]
    row = transforms.transform_transaction(api)
    assert row["posted_ts"] is None


# ---- categories -----------------------------------------------------------
def test_transform_category_with_parent():
    api = {"id": "cat_groc", "name": "Groceries", "type": "expense",
           "isHidden": False, "parent": {"id": "cat_food"}}
    row = transforms.transform_category(api)
    assert row["category_id"] == "cat_groc"
    assert row["name"] == "Groceries"
    assert row["parent_category_id"] == "cat_food"
    assert row["type"] == "expense"
    assert row["is_hidden"] is False


def test_transform_category_root_has_null_parent():
    api = {"id": "cat_food", "name": "Food", "type": "expense", "isHidden": False, "parent": None}
    row = transforms.transform_category(api)
    assert row["parent_category_id"] is None


# ---- accounts -------------------------------------------------------------
def test_transform_account_basic():
    api = {"id": "acct_chase", "name": "Chase Sapphire", "institution": "Chase",
           "type": "credit", "currency": "USD", "isHidden": False}
    row = transforms.transform_account(api)
    assert row["account_id"] == "acct_chase"
    assert row["type"] == "credit"
    assert row["currency"] == "USD"


def test_transform_account_currency_uppercased():
    api = {"id": "acct_x", "name": "X", "currency": "usd"}
    row = transforms.transform_account(api)
    assert row["currency"] == "USD"


def test_transform_account_default_currency():
    api = {"id": "acct_x", "name": "X"}
    row = transforms.transform_account(api)
    assert row["currency"] == "USD"

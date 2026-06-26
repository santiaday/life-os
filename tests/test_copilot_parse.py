"""Tests for ingest_copilot.transforms.

Sign convention preservation, FK shape, pending/recurring flags, nullable
parent on dim_category, account hide/close handling.
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
    assert row["amount"] == pytest.approx(47.83)
    assert row["merchant"] == "Whole Foods Market"
    assert row["category_id"] == "cat_groc"
    assert row["account_id"] == "acct_chase"
    assert row["is_pending"] is False
    assert row["is_excluded"] is False
    assert row["notes"] == "Weekly grocery run"


def test_transfer_detection_excludes_transfers_not_real_expenses():
    # Genuine transfers/payments → excluded from spend totals.
    for name in (
        "Payment Thank You-mobile",
        "Withdrawal To Expense Account Xxxxxxx",
        "Capital One (account ) Money In Deposit",
        "Wealthfront Brokerag",
        "Kalshi",
        "Withdrawal To Joint Savings Xxxxxxx",
    ):
        assert transforms._is_transfer(name) is True, name
    # Copilot mislabels these as INTERNAL_TRANSFER, but they are real spend —
    # must NOT be excluded. Also P2P payments stay in.
    for name in (
        "Slack",
        "Ideal Nutrition",
        "El Cielo Miami",
        "Withdrawal To Paulina Income Xxxxxxx",
        "Whole Foods Market",
    ):
        assert transforms._is_transfer(name) is False, name


def test_transform_transaction_transfer_sets_excluded():
    api = {
        "id": "txn_xfer", "amount": 500.0, "date": "2026-05-01",
        "name": "Payment Thank You-mobile", "accountId": "acct_chase",
    }
    assert transforms.transform_transaction(api)["is_excluded"] is True


def test_transform_transaction_pending(fx):
    api = _txn(fx)[1]
    row = transforms.transform_transaction(api)
    assert row["is_pending"] is True
    assert row["category_id"] == "cat_rest"


def test_transform_transaction_income_negative_with_recurring(fx):
    api = _txn(fx)[2]
    row = transforms.transform_transaction(api)
    assert row["amount"] == pytest.approx(-2500.00)
    assert row["is_recurring"] is True
    assert row["account_id"] == "acct_check"


def test_transform_transaction_handles_missing_posted_at(fx):
    api = _txn(fx)[2]
    row = transforms.transform_transaction(api)
    assert row["posted_ts"] is None


# ---- categories -----------------------------------------------------------
def test_transform_category_with_parent():
    """Parent ID is injected as `_parent_id` by the GraphQL client when it
    flattens the nested childCategories shape."""
    api = {"id": "cat_groc", "name": "Groceries", "isExcluded": False,
           "_parent_id": "cat_food"}
    row = transforms.transform_category(api)
    assert row["category_id"] == "cat_groc"
    assert row["name"] == "Groceries"
    assert row["parent_category_id"] == "cat_food"
    assert row["is_hidden"] is False


def test_transform_category_root_has_null_parent():
    api = {"id": "cat_food", "name": "Food", "isExcluded": False, "_parent_id": None}
    row = transforms.transform_category(api)
    assert row["parent_category_id"] is None


def test_transform_category_excluded_marks_hidden():
    api = {"id": "cat_x", "name": "Hidden", "isExcluded": True, "_parent_id": None}
    row = transforms.transform_category(api)
    assert row["is_hidden"] is True


# ---- accounts -------------------------------------------------------------
def test_transform_account_basic():
    api = {"id": "acct_chase", "name": "Chase Sapphire", "type": "credit_card",
           "subType": "credit", "balance": -1234.56, "mask": "1234",
           "isUserHidden": False, "isUserClosed": False, "institutionId": "inst_chase"}
    row = transforms.transform_account(api)
    assert row["account_id"] == "acct_chase"
    # Prefer subType over type when both present
    assert row["type"] == "credit"
    assert row["currency"] == "USD"
    assert row["is_hidden"] is False


def test_transform_account_hidden_or_closed_marks_hidden():
    closed = transforms.transform_account({"id": "x", "name": "X", "isUserClosed": True})
    assert closed["is_hidden"] is True
    hidden = transforms.transform_account({"id": "y", "name": "Y", "isUserHidden": True})
    assert hidden["is_hidden"] is True


def test_transform_account_default_currency():
    api = {"id": "acct_x", "name": "X"}
    row = transforms.transform_account(api)
    assert row["currency"] == "USD"

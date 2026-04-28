"""GraphQL client for Copilot Money.

Reverse-engineered endpoint at https://app.copilot.money/api/graphql.
Schema is undocumented; queries here are kept in sync with the JaviSoto
copilot-money-cli and Hermosilla copilot-money-mcp open-source projects.

Auth is a Firebase ID token (Bearer); see ingest_copilot.auth for how it's
obtained from a long-lived refresh token.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ingest_copilot import auth
from lifeos_core.logging import get_logger

log = get_logger(__name__)

ENDPOINT = "https://app.copilot.money/api/graphql"
TIMEOUT = 30.0

# Bumped whenever the queries below change. Logged into ingestion_runs.metadata
# so we can correlate schema drift with run failures.
SCHEMA_VERSION = "2026-04-27"

PAGE_SIZE = 100


class SchemaDriftError(RuntimeError):
    """A query returned data missing required fields."""


class CopilotAPIError(RuntimeError):
    pass


# ---- queries (verbatim from the public-source clients) ---------------------
Q_TRANSACTIONS = """
query Transactions($first: Int, $after: String, $filter: TransactionFilter, $sort: [TransactionSort!]) {
  transactions(first: $first, after: $after, filter: $filter, sort: $sort) {
    edges {
      cursor
      node {
        id
        amount
        date
        name
        type
        accountId
        categoryId
        recurringId
        parentId
        isPending
        isReviewed
        isoCurrencyCode
        tipAmount
        userNotes
        itemId
        createdAt
        tags { id name colorName __typename }
        __typename
      }
      __typename
    }
    pageInfo {
      endCursor
      hasNextPage
      __typename
    }
    __typename
  }
}
""".strip()

Q_CATEGORIES = """
query Categories {
  categories {
    id
    name
    colorName
    isExcluded
    templateId
    childCategories {
      id
      name
      colorName
      isExcluded
      templateId
      __typename
    }
    __typename
  }
}
""".strip()

Q_ACCOUNTS = """
query Accounts {
  accounts {
    id
    name
    type
    subType
    balance
    mask
    isUserHidden
    isUserClosed
    isManual
    institutionId
    __typename
  }
}
""".strip()

Q_TAGS = """
query Tags {
  tags { id name colorName __typename }
}
""".strip()

Q_TRANSACTION_BY_ID = """
query TransactionById($id: ID!) {
  transaction(id: $id) {
    id amount date name type accountId categoryId recurringId parentId
    isPending isReviewed isoCurrencyCode tipAmount userNotes itemId createdAt
    tags { id name colorName __typename }
    __typename
  }
}
""".strip()

# ---- mutations -------------------------------------------------------------
# Copilot's edit endpoint takes the (itemId, accountId, id) locator triple.
# All editable fields go in the EditTransactionInput object. Field set
# confirmed against the OSS clients: categoryId, userNotes, tagIds,
# isReviewed, plus likely accepted-but-less-tested: amount, date, name,
# type, hidden, splits, tipAmount.
M_EDIT_TRANSACTION = """
mutation EditTransaction(
  $itemId: ID!, $accountId: ID!, $id: ID!, $input: EditTransactionInput
) {
  editTransaction(itemId: $itemId, accountId: $accountId, id: $id, input: $input) {
    transaction {
      id amount date name type accountId categoryId recurringId parentId
      isPending isReviewed isoCurrencyCode tipAmount userNotes itemId createdAt
      tags { id name colorName __typename }
      __typename
    }
  }
}
""".strip()

# Bulk variant — accepts a TransactionFilter and an input dict applied to
# every match. Useful for "categorize every uncategorized Netflix charge as
# Subscriptions" style operations.
M_BULK_EDIT_TRANSACTIONS = """
mutation BulkEditTransactions($input: BulkEditTransactionInput!, $filter: TransactionFilter) {
  bulkEditTransactions(filter: $filter, input: $input) {
    updated {
      id amount date name categoryId userNotes isReviewed tipAmount
      tags { id name __typename }
      __typename
    }
    failed {
      transaction { id name __typename }
      error
      errorCode
      __typename
    }
    __typename
  }
}
""".strip()

M_TAG_TRANSACTION = """
mutation TagTransaction($transactionId: ID!, $tagIds: [ID!]!) {
  tagTransaction(transactionId: $transactionId, tagIds: $tagIds) {
    transaction { id tags { id name __typename } __typename }
  }
}
""".strip()

M_CREATE_TAG = """
mutation CreateTag($name: String!, $colorName: String) {
  createTag(name: $name, colorName: $colorName) {
    tag { id name colorName __typename }
  }
}
""".strip()

# Recurring stream linking. AddTransactionToRecurring attaches the txn to a
# specific recurring stream (by its id); ExcludeTransactionFromRecurring
# detaches it. To create a brand-new recurring stream we'd need a separate
# CreateRecurring mutation — out of scope for now; user can do that in
# Copilot's UI.
M_ADD_TO_RECURRING = """
mutation AddTransactionToRecurring($transactionId: ID!, $recurringId: ID!) {
  addTransactionToRecurring(transactionId: $transactionId, recurringId: $recurringId) {
    transaction { id recurringId __typename }
  }
}
""".strip()

M_EXCLUDE_FROM_RECURRING = """
mutation ExcludeTransactionFromRecurring($transactionId: ID!) {
  excludeTransactionFromRecurring(transactionId: $transactionId) {
    transaction { id recurringId __typename }
  }
}
""".strip()


class GraphQLClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._client = httpx.Client(
            timeout=TIMEOUT,
            headers={"Content-Type": "application/json"},
        )

    def __enter__(self) -> "GraphQLClient":
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def _headers(self, force_refresh: bool = False) -> dict:
        if self._token is None or force_refresh:
            self._token = auth.refresh_access_token()
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=1, max=20),
        reraise=True,
    )
    def _post(self, query: str, variables: dict | None = None) -> dict:
        body = {"query": query, "variables": variables or {}}
        resp = self._client.post(ENDPOINT, json=body, headers=self._headers())
        if resp.status_code == 401:
            log.info("copilot.gql.401_refresh")
            resp = self._client.post(
                ENDPOINT, json=body, headers=self._headers(force_refresh=True)
            )
        if resp.status_code >= 400:
            raise CopilotAPIError(
                f"Copilot GraphQL {resp.status_code}: {resp.text[:300]}"
            )
        payload = resp.json()
        if "errors" in payload and payload["errors"]:
            raise CopilotAPIError(f"Copilot GraphQL errors: {payload['errors']}")
        return payload.get("data") or {}

    # ---- public surface ----------------------------------------------------
    def transactions(self, start: date, end: date) -> list[dict]:
        """Page through transactions and return everything in [start, end].

        TransactionFilter shape is reverse-engineered from the web app's
        introspection-disabled schema. We send the simplest possible filter
        and fall back to client-side date filtering if Copilot rejects it.
        """
        all_rows: list[dict] = []
        # Try server-side date filter first.
        variables: dict[str, Any] = {
            "first": PAGE_SIZE,
            "after": None,
            "sort": [{"field": "DATE", "direction": "DESC"}],
            "filter": {
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
            },
        }

        try:
            return list(self._iterate_transactions(variables, start, end))
        except CopilotAPIError as e:
            if "filter" not in str(e).lower() and "TransactionFilter" not in str(e):
                raise
            log.warning(
                "copilot.gql.filter_rejected_falling_back_to_clientside",
                error=str(e)[:200],
            )

        # Fallback: no filter; we cap pages by date locally.
        variables["filter"] = None
        return list(self._iterate_transactions(variables, start, end))

    def _iterate_transactions(
        self, variables: dict, start: date, end: date
    ) -> list[dict]:
        out: list[dict] = []
        after: str | None = None
        while True:
            v = {**variables, "after": after}
            data = self._post(Q_TRANSACTIONS, v)
            page = data.get("transactions")
            if page is None:
                raise SchemaDriftError("transactions field missing from response")
            for edge in page.get("edges", []):
                node = edge.get("node") or {}
                if not node:
                    continue
                # Client-side bound check (works regardless of whether server
                # honored the filter).
                d = node.get("date")
                if d:
                    nd = date.fromisoformat(d[:10])
                    if nd < start:
                        return out
                    if nd > end:
                        continue
                out.append(node)
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if not after:
                break
        return out

    def categories(self) -> list[dict]:
        """Return a flat list of categories with parent_id derived from the
        nested childCategories shape."""
        data = self._post(Q_CATEGORIES)
        roots = data.get("categories")
        if roots is None:
            raise SchemaDriftError("categories field missing from response")
        flat: list[dict] = []
        for root in roots:
            flat.append({**root, "_parent_id": None})
            for child in root.get("childCategories") or []:
                flat.append({**child, "_parent_id": root.get("id")})
        return flat

    def accounts(self) -> list[dict]:
        data = self._post(Q_ACCOUNTS)
        rows = data.get("accounts")
        if rows is None:
            raise SchemaDriftError("accounts field missing from response")
        return rows

    def tags(self) -> list[dict]:
        data = self._post(Q_TAGS)
        rows = data.get("tags")
        if rows is None:
            raise SchemaDriftError("tags field missing from response")
        return rows

    def transaction(self, transaction_id: str) -> dict | None:
        data = self._post(Q_TRANSACTION_BY_ID, {"id": transaction_id})
        return data.get("transaction")

    # ---- mutations -----------------------------------------------------
    def edit_transaction(
        self,
        *,
        transaction_id: str,
        item_id: str,
        account_id: str,
        category_id: str | None = None,
        user_notes: str | None = None,
        name: str | None = None,
        amount: float | None = None,
        date: str | None = None,
        tip_amount: float | None = None,
        is_reviewed: bool | None = None,
        type: str | None = None,
        hidden: bool | None = None,
        tag_ids: list[str] | None = None,
    ) -> dict:
        """Universal edit. Pass only the fields you want to change; None means
        leave alone. To clear a string, pass "" .

        itemId + accountId are required by Copilot's mutation locator triple.
        write_tools.update_transaction looks them up from the local fact
        table so MCP callers don't need to know about them."""
        input_obj: dict = {}
        for k, v in (
            ("categoryId", category_id),
            ("userNotes", user_notes),
            ("name", name),
            ("amount", amount),
            ("date", date),
            ("tipAmount", tip_amount),
            ("isReviewed", is_reviewed),
            ("type", type),
            ("hidden", hidden),
            ("tagIds", tag_ids),
        ):
            if v is not None:
                input_obj[k] = v
        if not input_obj:
            raise ValueError("edit_transaction needs at least one field to change")
        data = self._post(
            M_EDIT_TRANSACTION,
            {"itemId": item_id, "accountId": account_id, "id": transaction_id, "input": input_obj},
        )
        return (data.get("editTransaction") or {}).get("transaction") or {}

    def bulk_edit_transactions(self, *, filter: dict, input: dict) -> dict:
        """Apply `input` to every transaction matching `filter`. Returns
        {updated: [...], failed: [{transaction, error, errorCode}]}."""
        data = self._post(M_BULK_EDIT_TRANSACTIONS, {"filter": filter, "input": input})
        return data.get("bulkEditTransactions") or {}

    def tag_transaction(self, transaction_id: str, tag_ids: list[str]) -> dict:
        """Replace the transaction's tags with the given set. (For batch
        edits use edit_transaction with tagIds.)"""
        data = self._post(M_TAG_TRANSACTION,
                          {"transactionId": transaction_id, "tagIds": tag_ids})
        return (data.get("tagTransaction") or {}).get("transaction") or {}

    def create_tag(self, name: str, color_name: str | None = None) -> dict:
        data = self._post(M_CREATE_TAG, {"name": name, "colorName": color_name})
        return (data.get("createTag") or {}).get("tag") or {}

    def add_transaction_to_recurring(self, transaction_id: str, recurring_id: str) -> dict:
        data = self._post(
            M_ADD_TO_RECURRING,
            {"transactionId": transaction_id, "recurringId": recurring_id},
        )
        return (data.get("addTransactionToRecurring") or {}).get("transaction") or {}

    def exclude_transaction_from_recurring(self, transaction_id: str) -> dict:
        data = self._post(M_EXCLUDE_FROM_RECURRING, {"transactionId": transaction_id})
        return (data.get("excludeTransactionFromRecurring") or {}).get("transaction") or {}


def schema_version() -> str:
    return SCHEMA_VERSION

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


def schema_version() -> str:
    return SCHEMA_VERSION

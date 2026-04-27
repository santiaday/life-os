"""GraphQL client for Copilot Money.

Reverse-engineered endpoint at https://app.copilot.money/api/graphql. Schema
is undocumented so we version our own queries — bump SCHEMA_VERSION when you
edit them, and surface the version in ingestion_runs.metadata so debugging
old logs is possible.

If a known field goes missing in a response, raise SchemaDriftError loudly.
Don't silently drop columns; we'd rather see an explicit failure.
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


class SchemaDriftError(RuntimeError):
    """A query returned data missing required fields. Indicates Copilot
    changed their schema; we need to update SCHEMA_VERSION + queries."""


class CopilotAPIError(RuntimeError):
    pass


# ---- queries ---------------------------------------------------------------
SCHEMA_VERSION = "2026-04-26"

Q_TRANSACTIONS = """
query Transactions($startDate: String!, $endDate: String!) {
  transactions(startDate: $startDate, endDate: $endDate) {
    id
    date
    postedAt
    amount
    currency
    merchant
    description: note
    category { id name parent { id } }
    account { id name institution type currency }
    isPending
    isRecurring
    isExcluded
  }
}
""".strip()

Q_CATEGORIES = """
query Categories {
  categories {
    id
    name
    type
    isHidden
    parent { id }
  }
}
""".strip()

Q_ACCOUNTS = """
query Accounts {
  accounts {
    id
    name
    institution
    type
    currency
    isHidden
  }
}
""".strip()


# ---- client ----------------------------------------------------------------
class GraphQLClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._client = httpx.Client(
            base_url="",
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
        if "errors" in payload:
            raise CopilotAPIError(f"Copilot GraphQL errors: {payload['errors']}")
        return payload.get("data") or {}

    # ---- public ------------------------------------------------------------
    def transactions(self, start: date, end: date) -> list[dict]:
        data = self._post(Q_TRANSACTIONS, {"startDate": start.isoformat(), "endDate": end.isoformat()})
        rows = data.get("transactions")
        if rows is None:
            raise SchemaDriftError("transactions field missing from response")
        for r in rows:
            for required in ("id", "date", "amount"):
                if required not in r:
                    raise SchemaDriftError(f"transaction row missing {required}: {r}")
        return rows

    def categories(self) -> list[dict]:
        data = self._post(Q_CATEGORIES)
        rows = data.get("categories")
        if rows is None:
            raise SchemaDriftError("categories field missing from response")
        return rows

    def accounts(self) -> list[dict]:
        data = self._post(Q_ACCOUNTS)
        rows = data.get("accounts")
        if rows is None:
            raise SchemaDriftError("accounts field missing from response")
        return rows


def schema_version() -> str:
    return SCHEMA_VERSION

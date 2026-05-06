"""Thin httpx client for Whoop's private journal-service API.

GET-only surface, three endpoints:
  /journal-service/v2/journals/behaviors/user/{date}
    Currently-selected behaviors with full metadata.
  /journal-service/v3/journals/behaviors
    Catalog of all 200+ behaviors (the dim table source).
  /journal-service/v3/journals/drafts/mobile/{date}
    The actual data: tracked_behaviors + notes + integrations.

Auth comes from :class:`auth.WhoopAuth`, which only reads tokens — it can't
refresh. A 401 here means the iPhone hasn't refreshed yet (or the token row
is gone); we surface :class:`WhoopAuthExpired` rather than retrying.

The journal-service gateway accepts plain bearer auth — none of the
``x-whoop-*`` iOS headers are required here. Those are only enforced on
``api.prod.whoop.com/auth-service``, which we never call.
"""

from __future__ import annotations

from datetime import date

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ingest_whoop_journal.auth import WhoopAuth, WhoopAuthExpired
from lifeos_core.logging import get_logger

log = get_logger(__name__)

BASE = "https://api-7.whoop.com"
TIMEOUT = 30.0


class WhoopJournalAPIError(RuntimeError):
    pass


class WhoopJournalClient:
    def __init__(self, auth: WhoopAuth | None = None) -> None:
        self._auth = auth or WhoopAuth()
        self._client = httpx.Client(
            base_url=BASE,
            timeout=TIMEOUT,
            headers={"Accept": "application/json"},
        )

    def __enter__(self) -> "WhoopJournalClient":
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=1, max=20),
        reraise=True,
    )
    def _get(self, path: str) -> dict:
        # ensure_fresh() raises WhoopAuthExpired if the row is stale; we
        # propagate so the caller can fail fast instead of hammering 401s.
        resp = self._client.get(path, headers=self._auth.headers())
        if resp.status_code == 401:
            log.warning("whoop_journal.client.401", path=path)
            raise WhoopAuthExpired(
                f"Whoop journal-service rejected the access token at {path}. "
                f"The iPhone Shortcut needs to refresh — check its run log."
            )
        if resp.status_code == 404:
            # Not every day has a journal entry. Treat as empty.
            log.debug("whoop_journal.client.404", path=path)
            return {}
        if resp.status_code >= 400:
            raise WhoopJournalAPIError(
                f"Whoop journal {resp.status_code} {path}: {resp.text[:300]}"
            )
        try:
            return resp.json() or {}
        except ValueError:
            return {}

    # ---- public surface ----------------------------------------------------
    def behaviors_catalog(self) -> list[dict]:
        """Full Whoop behavior dictionary (200+ behaviors)."""
        data = self._get("/journal-service/v3/journals/behaviors")
        if isinstance(data, list):
            return data
        return data.get("behaviors") or data.get("results") or []

    def user_behaviors_for_day(self, day: date) -> list[dict]:
        """Behaviors the user currently has activated."""
        data = self._get(f"/journal-service/v2/journals/behaviors/user/{day.isoformat()}")
        if isinstance(data, list):
            return data
        return data.get("behaviors") or data.get("results") or []

    def journal_draft(self, day: date) -> dict:
        """The day's journal entry: tracked_behaviors[], notes, integrations.
        Returns {} if no entry exists for that day."""
        return self._get(f"/journal-service/v3/journals/drafts/mobile/{day.isoformat()}") or {}

"""Thin httpx client for Whoop's private journal-service API.

Three endpoints, all GET, all returning JSON:
  /journal-service/v2/journals/behaviors/user/{date}
    Currently-selected behaviors with full metadata (~30 active for me).
  /journal-service/v3/journals/behaviors
    Catalog of all 200+ behaviors (the dim table source).
  /journal-service/v3/journals/drafts/mobile/{date}
    The actual data: tracked_behaviors + notes + integrations.

Headers are the minimum set that makes Whoop's gateway happy. The X-WHOOP-*
headers identify us as the iOS app; analytics-only headers (cookies,
sentry-trace, amplitude) are intentionally omitted.
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

from ingest_whoop_journal import auth
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

BASE = "https://api-7.whoop.com"
TIMEOUT = 30.0


class WhoopJournalAPIError(RuntimeError):
    pass


class WhoopJournalClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._client = httpx.Client(
            base_url=BASE,
            timeout=TIMEOUT,
            headers={
                "User-Agent": "iOS",
                "x-whoop-bundle-name": "com.whoop.iphone",
                "x-whoop-ios-version": settings.WHOOP_IOS_VERSION,
                "x-whoop-device-platform": "iOS",
                "x-whoop-time-zone": settings.LOCAL_TZ,
                "Accept": "application/json",
            },
        )

    def __enter__(self) -> "WhoopJournalClient":
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def _auth_header(self, force_refresh: bool = False) -> dict:
        if self._token is None or force_refresh:
            self._token = auth.refresh_access_token()
        return {"Authorization": f"Bearer {self._token}"}

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=1, max=20),
        reraise=True,
    )
    def _get(self, path: str) -> dict:
        resp = self._client.get(path, headers=self._auth_header())
        if resp.status_code == 401:
            log.info("whoop_journal.client.401_refresh", path=path)
            resp = self._client.get(path, headers=self._auth_header(force_refresh=True))
        if resp.status_code == 404:
            # Journal entries don't exist for every day; treat as empty.
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
        """Behaviors the user currently has activated (~30 for me)."""
        data = self._get(f"/journal-service/v2/journals/behaviors/user/{day.isoformat()}")
        if isinstance(data, list):
            return data
        return data.get("behaviors") or data.get("results") or []

    def journal_draft(self, day: date) -> dict:
        """The day's actual journal entry: tracked_behaviors[], notes,
        integrations. Returns {} if no entry exists for that day."""
        return self._get(f"/journal-service/v3/journals/drafts/mobile/{day.isoformat()}") or {}

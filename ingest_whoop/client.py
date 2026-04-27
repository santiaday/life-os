"""Whoop developer-API v2 HTTP client.

Thin wrapper over httpx with:
- automatic OAuth refresh on 401
- pagination via `nextToken`
- tenacity retries on 429/5xx with exponential backoff
- structured logging of every request

Reference: https://developer.whoop.com/api
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ingest_whoop import oauth
from lifeos_core.logging import get_logger

log = get_logger(__name__)

BASE = "https://api.prod.whoop.com/developer"
PAGE_LIMIT = 25  # Whoop's max for v2 paginated endpoints


class WhoopAPIError(Exception):
    """Non-retryable Whoop API error (e.g. 4xx other than 401/429)."""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


class WhoopClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._client = httpx.Client(
            base_url=BASE,
            timeout=30.0,
            headers={"Accept": "application/json"},
        )

    def __enter__(self) -> "WhoopClient":
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def _auth_header(self, force_refresh: bool = False) -> dict:
        if self._token is None or force_refresh:
            self._token = oauth.refresh_access_token()
        return {"Authorization": f"Bearer {self._token}"}

    @retry(
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=1, max=20),
        reraise=True,
    )
    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self._client.get(path, params=params, headers=self._auth_header())
        if resp.status_code == 401:
            # Force a refresh and retry once inline; tenacity handles the rest.
            log.info("whoop.client.401_refresh", path=path)
            resp = self._client.get(path, params=params, headers=self._auth_header(force_refresh=True))
        if resp.status_code >= 400:
            log.warning(
                "whoop.client.http_error",
                path=path,
                status=resp.status_code,
                body=resp.text[:300],
            )
            if not _is_retryable(httpx.HTTPStatusError("", request=resp.request, response=resp)):
                raise WhoopAPIError(f"{resp.status_code} {path}: {resp.text[:200]}")
            resp.raise_for_status()
        return resp.json()

    # ---- Paginated collection iterators ------------------------------------
    def _paged(
        self, path: str, *, start: datetime, end: datetime, extra_params: dict | None = None
    ) -> Iterator[dict]:
        params = {
            "start": _isoformat_z(start),
            "end": _isoformat_z(end),
            "limit": PAGE_LIMIT,
        }
        if extra_params:
            params.update(extra_params)
        while True:
            data = self._get(path, params=params)
            for record in data.get("records", []):
                yield record
            next_token = data.get("next_token")
            if not next_token:
                break
            params = {**params, "nextToken": next_token}

    # ---- Public surface ----------------------------------------------------
    def cycles(self, start: datetime, end: datetime) -> Iterator[dict]:
        yield from self._paged("/v2/cycle", start=start, end=end)

    def recovery(self, start: datetime, end: datetime) -> Iterator[dict]:
        yield from self._paged("/v2/recovery", start=start, end=end)

    def sleep(self, start: datetime, end: datetime) -> Iterator[dict]:
        yield from self._paged("/v2/activity/sleep", start=start, end=end)

    def workouts(self, start: datetime, end: datetime) -> Iterator[dict]:
        yield from self._paged("/v2/activity/workout", start=start, end=end)

    def profile(self) -> dict:
        return self._get("/v2/user/profile/basic")

    def body_measurement(self) -> dict:
        return self._get("/v2/user/measurement/body")


def _isoformat_z(dt: datetime) -> str:
    """Whoop wants RFC 3339 with a 'Z' suffix for UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

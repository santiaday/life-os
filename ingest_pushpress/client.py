"""PushPress private-API client.

PushPress hosts a single GraphQL endpoint at POST /v2/graph/graphql. We only
need two operations:

  GetWorkoutTypes($date: String!)             — list of class types for a date
  GetWorkoutOfDay($date: String!,
                  $classTypeUid: String!)     — programmed workout for that
                                                (date, class_type)

The queries below were reverse-engineered from members.pushpress.com's
Flutter web app (main.dart.js) — Apollo introspection is disabled in prod.
Field names match the bundle's encoded AST and were verified against live
responses on 2026-05-07.

Throttle: 2 req/sec (matches the spec — gentle on a private endpoint we
don't own).
"""

from __future__ import annotations

import time
from datetime import date

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ingest_pushpress.auth import API_BASE, DEFAULT_HEADERS, PushPressAuth
from lifeos_core.logging import get_logger

log = get_logger(__name__)

GRAPHQL_PATH = "/v2/graph/graphql"
THROTTLE_SECONDS = 0.5  # 2 req/sec

QUERY_GET_CLASS_TYPES = """
query GetWorkoutTypes($date: String!) {
  getClassTypes(getClassTypesInput: { date: $date }) {
    name
    uid
    origin
    static
    progressiveProgram
    lastDayNum
  }
}
""".strip()

QUERY_GET_WORKOUT_OF_DAY = """
query GetWorkoutOfDay($date: String!, $classTypeUid: String!) {
  getWorkoutOfDay(getWorkoutOfDayInput: { date: $date, classTypeUid: $classTypeUid }) {
    uid
    id
    origin
    classTypeId
    workoutUid
    workoutState
    title
    publishingDate
    publishingTime
    publishedOn
    createdDate
    updatedDate
    version
    notified
    day
    imageUrl
    imageUrlId
    videoUrlId
    workoutProgramGroupId
    workoutProgramTemplateId
    parts {
      workoutPartUid
      title
      workoutTitle
      description
      scoreType
      scoreCount
      defaultReps
      divisions
      sets
      athletesNotes
      coachesNotes
      rawUnit: unit
    }
  }
}
""".strip()


class PushPressAPIError(RuntimeError):
    """Generic GraphQL/transport failure."""


class PushPressAuthError(PushPressAPIError):
    """401/403 on a request that already has fresh creds — usually means the
    token was rotated server-side. Caller can retry once after a forced
    re-auth, then give up."""


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


class PushPressClient:
    def __init__(self, auth: PushPressAuth | None = None) -> None:
        self._auth = auth or PushPressAuth()
        self._client = httpx.Client(
            base_url=API_BASE,
            timeout=30.0,
            headers=DEFAULT_HEADERS,
        )
        self._last_call_ts: float = 0.0

    def __enter__(self) -> PushPressClient:
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def _throttle(self) -> None:
        delta = time.monotonic() - self._last_call_ts
        if delta < THROTTLE_SECONDS:
            time.sleep(THROTTLE_SECONDS - delta)
        self._last_call_ts = time.monotonic()

    @retry(
        retry=retry_if_exception_type(
            (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=1, max=20),
        reraise=True,
    )
    def _post_graphql(
        self,
        operation_name: str,
        query: str,
        variables: dict,
    ) -> dict:
        self._throttle()
        body = {
            "operationName": operation_name,
            "query": query,
            "variables": variables,
        }
        headers = {**self._auth.headers(), "content-type": "application/json"}
        resp = self._client.post(GRAPHQL_PATH, json=body, headers=headers)

        if resp.status_code in (401, 403):
            raise PushPressAuthError(
                f"{resp.status_code} on {operation_name}: token rejected"
            )
        if resp.status_code >= 400:
            log.warning(
                "pushpress.client.http_error",
                op=operation_name,
                status=resp.status_code,
                body=resp.text[:300],
            )
            if not _retryable(httpx.HTTPStatusError("", request=resp.request, response=resp)):
                raise PushPressAPIError(
                    f"{resp.status_code} {operation_name}: {resp.text[:200]}"
                )
            resp.raise_for_status()

        data = resp.json()
        if data.get("errors"):
            err_msg = "; ".join(
                e.get("message", str(e))[:300] for e in data["errors"]
            )
            log.warning(
                "pushpress.client.graphql_error",
                op=operation_name,
                error=err_msg,
            )
            raise PushPressAPIError(f"{operation_name} GraphQL error: {err_msg}")

        return data.get("data") or {}

    # ---- public surface ----------------------------------------------------
    def class_types(self, ref_date: date | None = None) -> list[dict]:
        """List class types as of a reference date. PushPress sometimes
        scopes class types by date (e.g. seasonal HYROX) — we use today by
        default, matching the web app's behavior."""
        d = (ref_date or date.today()).isoformat()
        data = self._post_graphql(
            "GetWorkoutTypes",
            QUERY_GET_CLASS_TYPES,
            {"date": d},
        )
        return list(data.get("getClassTypes") or [])

    def workout_of_day(self, class_date: date, class_type_uuid: str) -> list[dict]:
        """Return the programmed workout(s) for one (date, class_type). Often
        an empty list (rest day / not yet programmed) or a single item; the
        API keeps it as an array because, in principle, a date can have
        multiple programmings (rare in this gym's setup)."""
        data = self._post_graphql(
            "GetWorkoutOfDay",
            QUERY_GET_WORKOUT_OF_DAY,
            {
                "date": class_date.isoformat(),
                "classTypeUid": class_type_uuid,
            },
        )
        return list(data.get("getWorkoutOfDay") or [])

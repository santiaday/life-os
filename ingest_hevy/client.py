"""Hevy public-API HTTP client.

Hevy uses a static api-key header (no OAuth refresh dance, unlike Whoop), so
this wrapper is mostly httpx + tenacity retries + a couple of pagination
helpers around the endpoints we care about:

  GET /v1/workouts/events?since=ISO8601   change feed (preferred for incrementals)
  GET /v1/workouts?page=N&pageSize=10     paginated workout list (newest first)
  GET /v1/workouts/{id}                   single workout full payload
  GET /v1/workouts/count                  total workout count (sanity check)
  GET /v1/exercise_templates?page=...     exercise template catalog

Reference: https://api.hevyapp.com/docs/
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

BASE = "https://api.hevyapp.com/v1"
DEFAULT_PAGE_SIZE = 10           # Hevy's paginated endpoints cap at 10 for /workouts
EXERCISE_PAGE_SIZE = 100         # /exercise_templates accepts up to 100


class HevyAPIError(Exception):
    """Non-retryable Hevy API error (4xx other than 429)."""


class HevyAuthError(HevyAPIError):
    """401/403 — typically a missing or revoked HEVY_API_KEY."""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


class HevyClient:
    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or settings.HEVY_API_KEY
        if not key:
            raise RuntimeError(
                "HEVY_API_KEY not set. Add it to .env (Hevy app → Settings → "
                "Developer; requires the Pro plan)."
            )
        self._client = httpx.Client(
            base_url=BASE,
            timeout=30.0,
            headers={
                "api-key": key,
                "accept": "application/json",
            },
        )

    def __enter__(self) -> HevyClient:
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    @retry(
        retry=retry_if_exception_type(
            (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=1, max=20),
        reraise=True,
    )
    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self._client.get(path, params=params)
        if resp.status_code in (401, 403):
            log.error("hevy.client.auth_failed", path=path, status=resp.status_code)
            raise HevyAuthError(
                f"{resp.status_code} on {path}: check HEVY_API_KEY in .env"
            )
        if resp.status_code >= 400:
            log.warning(
                "hevy.client.http_error",
                path=path,
                status=resp.status_code,
                body=resp.text[:300],
            )
            if not _is_retryable(
                httpx.HTTPStatusError("", request=resp.request, response=resp)
            ):
                raise HevyAPIError(f"{resp.status_code} {path}: {resp.text[:200]}")
            resp.raise_for_status()
        return resp.json()

    # ---- Public surface ----------------------------------------------------
    def count(self) -> int:
        """Total workout count for the user. Used for backfill sanity checks."""
        data = self._get("/workouts/count")
        return int(data.get("workout_count") or data.get("count") or 0)

    def workout(self, workout_id: str) -> dict:
        """Fetch a single workout by id, full payload."""
        data = self._get(f"/workouts/{workout_id}")
        # Some endpoints wrap the payload as {"workout": {...}} — normalize.
        return data.get("workout", data)

    def create_workout(self, workout: dict) -> dict:
        """POST /v1/workouts — create a new workout. `workout` should match
        Hevy's PostWorkoutsRequestBody.workout shape: title, start_time,
        end_time, exercises[{exercise_template_id, sets[...]}, ...]. Returns
        the created workout payload."""
        return self._send_json("POST", "/workouts", {"workout": workout})

    def update_workout(self, workout_id: str, workout: dict) -> dict:
        """PUT /v1/workouts/{workoutId} — overwrite an existing workout.
        Same body shape as create_workout. Returns the updated payload."""
        return self._send_json(
            "PUT", f"/workouts/{workout_id}", {"workout": workout}
        )

    @retry(
        retry=retry_if_exception_type(
            (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=1, max=20),
        reraise=True,
    )
    def _send_json(self, method: str, path: str, body: dict) -> dict:
        """Shared POST/PUT for write endpoints. Surfaces 4xx body in the
        raised exception so the MCP user sees Hevy's validation error
        verbatim. Caller is responsible for the request-body wrapper
        (e.g. {"workout": ...}, {"routine": ...})."""
        log.info("hevy.client.send_json", method=method, path=path)
        resp = self._client.request(
            method, path, json=body,
            headers={"content-type": "application/json"},
        )
        if resp.status_code in (401, 403):
            raise HevyAuthError(
                f"{resp.status_code} on {method} {path}: check HEVY_API_KEY"
            )
        if resp.status_code >= 400:
            log.warning(
                "hevy.client.write_error",
                method=method, path=path,
                status=resp.status_code,
                body=resp.text[:500],
            )
            if not _is_retryable(
                httpx.HTTPStatusError("", request=resp.request, response=resp)
            ):
                raise HevyAPIError(
                    f"{method} {path} returned {resp.status_code}: {resp.text[:500]}"
                )
            resp.raise_for_status()
        return resp.json()

    def workouts(self, page_size: int = DEFAULT_PAGE_SIZE) -> Iterator[dict]:
        """Iterate every workout, newest first. Each yielded dict is a full
        workout payload (same shape as /workouts/{id}). Caller is responsible
        for stopping early once they hit a workout older than their `since`
        cursor — Hevy doesn't accept a server-side date filter on this
        endpoint."""
        page = 1
        while True:
            data = self._get(
                "/workouts",
                params={"page": page, "pageSize": page_size},
            )
            workouts = data.get("workouts") or []
            if not workouts:
                break
            yield from workouts
            page_count = data.get("page_count") or data.get("pageCount")
            if page_count is not None and page >= int(page_count):
                break
            if len(workouts) < page_size:
                break
            page += 1

    def workout_events(self, since: str, page_size: int = DEFAULT_PAGE_SIZE) -> Iterator[dict]:
        """Iterate change-feed events since the given ISO8601 timestamp.
        Each event is {type: 'updated'|'deleted', workout: {id, ...}} or
        similar. Returns an empty iterator if the endpoint doesn't yet
        return events for this user — caller should fall back to
        `workouts()` if zero events come back on a fresh sync."""
        page = 1
        while True:
            data = self._get(
                "/workouts/events",
                params={"since": since, "page": page, "pageSize": page_size},
            )
            events = data.get("events") or []
            if not events:
                break
            yield from events
            page_count = data.get("page_count") or data.get("pageCount")
            if page_count is not None and page >= int(page_count):
                break
            if len(events) < page_size:
                break
            page += 1

    def exercise_templates(self, page_size: int = EXERCISE_PAGE_SIZE) -> Iterator[dict]:
        """Iterate the full exercise template catalog."""
        page = 1
        while True:
            data = self._get(
                "/exercise_templates",
                params={"page": page, "pageSize": page_size},
            )
            templates = data.get("exercise_templates") or []
            if not templates:
                break
            yield from templates
            page_count = data.get("page_count") or data.get("pageCount")
            if page_count is not None and page >= int(page_count):
                break
            if len(templates) < page_size:
                break
            page += 1

    # ---- Routines ---------------------------------------------------------
    def routines(self, page_size: int = DEFAULT_PAGE_SIZE) -> Iterator[dict]:
        """Iterate every routine (template). Each yielded dict has the same
        shape as /v1/routines/{id} response."""
        page = 1
        while True:
            data = self._get(
                "/routines", params={"page": page, "pageSize": page_size}
            )
            routines = data.get("routines") or []
            if not routines:
                break
            yield from routines
            page_count = data.get("page_count") or data.get("pageCount")
            if page_count is not None and page >= int(page_count):
                break
            if len(routines) < page_size:
                break
            page += 1

    def routine(self, routine_id: str) -> dict:
        data = self._get(f"/routines/{routine_id}")
        return data.get("routine", data)

    def create_routine(self, routine: dict) -> dict:
        return self._send_json("POST", "/routines", {"routine": routine})

    def update_routine(self, routine_id: str, routine: dict) -> dict:
        return self._send_json(
            "PUT", f"/routines/{routine_id}", {"routine": routine}
        )

    # ---- Routine folders --------------------------------------------------
    def routine_folders(self, page_size: int = DEFAULT_PAGE_SIZE) -> Iterator[dict]:
        page = 1
        while True:
            data = self._get(
                "/routine_folders", params={"page": page, "pageSize": page_size}
            )
            # Hevy returns an inconsistent key here — they actually emit
            # "routines" in the response (visible from a live probe), even
            # though semantically these are folders. Accept both.
            folders = data.get("routine_folders") or data.get("routines") or []
            if not folders:
                break
            yield from folders
            page_count = data.get("page_count") or data.get("pageCount")
            if page_count is not None and page >= int(page_count):
                break
            if len(folders) < page_size:
                break
            page += 1

    def routine_folder(self, folder_id: int) -> dict:
        data = self._get(f"/routine_folders/{folder_id}")
        return data.get("routine_folder", data)

    def create_routine_folder(self, title: str) -> dict:
        return self._send_json(
            "POST", "/routine_folders", {"routine_folder": {"title": title}}
        )

    # ---- Exercise history ------------------------------------------------
    def exercise_history(
        self,
        exercise_template_id: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        """Every set you've ever done for one exercise across all workouts.
        Single response, no pagination per the OpenAPI spec."""
        params: dict = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        data = self._get(
            f"/exercise_history/{exercise_template_id}",
            params=params or None,
        )
        return data.get("exercise_history") or []

    # ---- Custom exercise templates ---------------------------------------
    def create_custom_exercise(self, exercise: dict) -> dict:
        """POST /v1/exercise_templates. Hevy's response shape here is
        QUIRKY: instead of returning the full template JSON it returns a
        bare UUID string (raw text body, not even quoted). We detect that
        and rebuild a normal {id, title, ...} dict from the request body
        so callers get a consistent shape."""
        log.info("hevy.client.send_json", method="POST", path="/exercise_templates")
        resp = self._client.request(
            "POST", "/exercise_templates",
            json={"exercise": exercise},
            headers={"content-type": "application/json"},
        )
        if resp.status_code in (401, 403):
            raise HevyAuthError(f"{resp.status_code} on POST /exercise_templates")
        if resp.status_code >= 400:
            log.warning(
                "hevy.client.write_error",
                method="POST", path="/exercise_templates",
                status=resp.status_code, body=resp.text[:500],
            )
            raise HevyAPIError(
                f"POST /exercise_templates returned {resp.status_code}: {resp.text[:300]}"
            )
        body_text = resp.text.strip()
        try:
            data = resp.json()
            tpl = data.get("exercise_template") or data.get("exercise") or data
            if isinstance(tpl, dict) and tpl.get("id"):
                return tpl
        except Exception:
            pass
        # Bare-UUID branch — synthesize a template dict from what we sent.
        new_id = body_text.strip().strip('"')
        if not new_id or len(new_id) < 8:
            raise HevyAPIError(
                f"POST /exercise_templates returned an unexpected body: {body_text[:200]!r}"
            )
        return {
            "id": new_id,
            "title": exercise.get("title"),
            "exercise_type": exercise.get("exercise_type"),
            "equipment_category": exercise.get("equipment_category"),
            "primary_muscle_group": exercise.get("muscle_group"),
            "secondary_muscle_groups": exercise.get("other_muscles") or [],
            "is_custom": True,
        }

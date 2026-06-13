"""Thin httpx client for Whoop's private iOS API (the surface beyond the public
OAuth endpoints in ingest_whoop).

GET-only. Four endpoint families:
  /progression-service/v3/trends/{metric}?endDate={date}
    Per-day metric trend graphs (steps, calories, VO2max, weight, stress, …).
  /coaching-service/v2/sleepneed
    Current sleep-need breakdown (recommended time in bed, debt, strain).
  /behavior-impact-service/v1/impact
    Whoop's causal recovery-impact analysis across journal behaviors.

Auth is shared with the journal ingester: ingest_whoop_journal.auth.WhoopAuth
reads the daily-refreshed oauth_tokens(service='whoop_private') bearer the iPhone
Shortcut maintains. We never refresh — a 401 means the token is stale and the
iPhone Shortcut needs to run; we surface WhoopAuthExpired rather than retry.

Like journal-service, these gateways accept a plain bearer — none of the
x-whoop-* iOS headers are required (those are only enforced on auth-service,
which we never call).
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

BASE = "https://api.prod.whoop.com"
TIMEOUT = 30.0


class WhoopPrivateAPIError(RuntimeError):
    pass


class WhoopPrivateClient:
    def __init__(self, auth: WhoopAuth | None = None) -> None:
        self._auth = auth or WhoopAuth()
        self._client = httpx.Client(
            base_url=BASE,
            timeout=TIMEOUT,
            headers={"Accept": "application/json"},
        )

    def __enter__(self) -> WhoopPrivateClient:
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=1, max=20),
        reraise=True,
    )
    def _get(self, path: str, params: dict | None = None) -> dict:
        # ensure_fresh() (inside headers()) raises WhoopAuthExpired if the row is
        # stale; we propagate so the caller fails fast instead of hammering 401s.
        resp = self._client.get(path, params=params, headers=self._auth.headers())
        if resp.status_code == 401:
            log.warning("whoop_private.client.401", path=path)
            raise WhoopAuthExpired(
                f"Whoop private API rejected the access token at {path}. "
                f"Re-run `python -m ingest_whoop_private login`."
            )
        if resp.status_code == 404:
            log.debug("whoop_private.client.404", path=path)
            return {}
        if resp.status_code >= 400:
            raise WhoopPrivateAPIError(
                f"Whoop private {resp.status_code} {path}: {resp.text[:300]}"
            )
        try:
            return resp.json() or {}
        except ValueError:
            return {}

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=1, max=20),
        reraise=True,
    )
    def post(self, path: str, body: dict) -> dict:
        """POST a JSON body to a private write endpoint (custom-exercise,
        workout-template, workout activity). Raises on >=400; returns the parsed
        JSON receipt (or {} if the body is empty)."""
        resp = self._client.post(path, json=body, headers=self._auth.headers())
        if resp.status_code == 401:
            log.warning("whoop_private.client.401", path=path)
            raise WhoopAuthExpired(
                f"Whoop private API rejected the access token at {path}. "
                f"Re-run `python -m ingest_whoop_private login`."
            )
        if resp.status_code >= 400:
            raise WhoopPrivateAPIError(
                f"Whoop private POST {resp.status_code} {path}: {resp.text[:400]}"
            )
        try:
            return resp.json() or {}
        except ValueError:
            return {}

    # ---- public surface ----------------------------------------------------
    def trend(self, metric: str, end_date: date) -> dict:
        """Graph BFF for one metric ending at end_date. Carries week / month /
        six_month time segments, each with a per-day graph. Returns {} on 404."""
        return self._get(
            f"/progression-service/v3/trends/{metric}",
            params={"endDate": end_date.isoformat()},
        )

    def sleep_need(self) -> dict:
        """Current sleep-need breakdown snapshot. Returns {} on 404."""
        return self._get("/coaching-service/v2/sleepneed")

    def behavior_impact(self) -> dict:
        """Trailing-90d recovery-impact analysis across journal behaviors.
        Returns {} on 404."""
        return self._get("/behavior-impact-service/v1/impact")

    def cardio_details(self, activity_id: str) -> dict:
        """Per-workout detail. For strength workouts the response carries the
        full per-exercise / per-set breakdown under weightlifting_cardio_details
        (plus a large HR curve we discard). Returns {} on 404."""
        return self._get(
            "/core-details-bff/v1/cardio-details", params={"activityId": activity_id}
        )

    def labs_tests(self) -> dict:
        """List of Advanced Labs tests: {records:[{id, test_source, test_date,
        display_name, upload_source, panel_id, ...}], next_token}."""
        return self._get("/advanced-labs-service/v1/biomarker-tests")

    def labs_summary(self, test_id: str) -> dict:
        """One test's full biomarker results: response is flat with
        biomarkers[] (biomarker_name, value, units, status, *_range)."""
        return self._get(f"/advanced-labs-service/v1/biomarker-tests/{test_id}/summary")

    def exercise_library(self) -> dict:
        """All Strength Trainer exercises incl. the user's customs: {exercises:[
        {exercise_id, name, custom_exercise, push_core_name, muscle_groups,
        equipment, movement_pattern, ...}], filter_options}."""
        return self._get("/weightlifting-service/v2/exercise")


# Metrics worth ingesting. The public OAuth ingester (ingest_whoop) already
# captures HRV / RHR / RECOVERY / strain / sleep-stage detail, so we focus on
# the net-new daily series the private trends endpoint exposes. Keeping the list
# here means client and ingest share one source of truth.
METRICS: tuple[str, ...] = (
    "STEPS",
    "CALORIES",
    "VO2_MAX",
    "BODY_COMPOSITION",
    "WEIGHT",
    "RESPIRATORY_RATE",
    "RESTORATIVE_SLEEP",
    "SLEEP_DEBT_POST",
    "TIME_IN_BED",
    "HR_ZONES_1_3",
    "HR_ZONES_4_5",
    "STRENGTH_ACTIVITY_TIME",
    "STRESS",
    "STRESS_DURING_SLEEP",
    "STRESS_DURING_NON_STRAIN",
)

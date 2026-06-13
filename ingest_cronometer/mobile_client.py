"""Cronometer mobile-API HTTP client (write-side).

Talks to mobile.cronometer.com's JSON REST API — the surface the Cronometer
Android/Flutter app uses — by impersonating the app's user-agent and auth
block. Adapted from rwestergren/cronometer-api-mcp (Frida + APK static
analysis of the Dart AOT snapshot).

This is the WRITE path. The existing read pipeline — the `cronometer-export`
Go binary wrapped by `exporter.py`/`ingest.py`, talking to Cronometer's
GWT-RPC API — is untouched. The two paths hit different endpoint families
on different subdomains, so they don't race.

Auth flow:
  1. POST /api/v2/login  → {id: userId, sessionKey: token}
  2. v2 endpoints: POST JSON; payload carries `auth: {userId, token, api, os,
     build, flavour}`
  3. v3 endpoints: `x-crono-session` request header
  4. On 401/403 OR body `{"result": "FAILURE"}`: re-login once and retry

The mobile API is unofficial. Cronometer can change shapes or rate-limit at
any time. Treat all calls as best-effort.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from datetime import date, datetime

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

BASE_URL = "https://mobile.cronometer.com"

# Mimics the Android app's identity claim. The server validates the keys
# are present; the values are inert.
_APP_AUTH = {
    "api": 3,
    "os": "Android",
    "build": "2807",
    "flavour": "free",
}

# Cronometer's per-100g nutrient IDs. Full list (~150) lives in the login
# response; these are the ones the custom-food path writes.
NUTRIENT_IDS = {
    "energy": 208,
    "protein": 203,
    "fat": 204,
    "carbs": 205,
    "fiber": 291,
    "sugar": 269,
    "sodium": 307,
    "alcohol": 221,
    "net_carbs": -1205,
}

# diaryGroup enum. 0 = server decides from time-of-day, which doesn't always
# match the user's intent (e.g. logging breakfast at 11am), so callers should
# pass an explicit value when known.
MEAL_GROUPS: dict[str, int] = {
    "auto": 0,
    "breakfast": 1,
    "lunch": 2,
    "dinner": 3,
    "snacks": 4,
    "snack": 4,
}


class CronometerAuthError(Exception):
    """Login rejected, or session unrecoverable after a refresh attempt."""


class CronometerAPIError(Exception):
    """Non-auth Cronometer API error: 4xx, body FAILURE, or malformed response."""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


class CronometerMobileClient:
    """Stateful, thread-unsafe client. Caches the session token in memory
    and refreshes on 401/403. Use as a context manager so the underlying
    httpx.Client is closed on exit.
    """

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        timezone: str = "America/New_York",
    ) -> None:
        self._username = username or settings.CRONOMETER_USERNAME
        self._password = password or settings.CRONOMETER_PASSWORD
        if not self._username or not self._password:
            raise CronometerAuthError(
                "CRONOMETER_USERNAME and CRONOMETER_PASSWORD must be set in .env "
                "(the same vars the GWT export binary already uses)."
            )
        self._timezone = timezone
        self._user_id: int | None = None
        self._token: str | None = None
        self._http = httpx.Client(
            base_url=BASE_URL,
            timeout=30.0,
            headers={
                # Identify as the Dart Android app so the server returns the
                # response shapes the reference impl captured.
                "user-agent": "Dart/3.9 (dart:io)",
                "accept-encoding": "gzip",
            },
        )

    def __enter__(self) -> CronometerMobileClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ---- auth ---------------------------------------------------------------
    def login(self) -> None:
        payload = {
            "email": self._username,
            "password": self._password,
            "timezone": self._timezone,
            "userCode": None,
            "build": "4.48.2 b2807-a",
            "device": "Android 14 (SDK 34), Google Pixel 6 Pro",
            "firebaseToken": "",
            "features": {
                "food_search_config": '{"newSearch": true, "newSpellcheck": true}',
                "use_gpt_autofill": "true",
            },
            "auth": {"userId": None, "token": None, **_APP_AUTH},
            "lastSeen": 0,
            "config": {"call_version": 2},
        }
        log.info("cronometer.mobile.login")
        resp = self._http.post("/api/v2/login", json=payload)
        if resp.status_code >= 400:
            raise CronometerAuthError(
                f"login HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            data = resp.json()
        except Exception as e:
            raise CronometerAuthError(
                f"login: non-JSON body: {resp.text[:200]}"
            ) from e
        if "sessionKey" not in data or "id" not in data:
            # Cronometer's `result: FAIL` rate-limit response lands here.
            # Surface it as a distinct exception type so callers can detect
            # and back off (instead of treating it as a generic auth fault).
            msg = data.get("error") or str(data)
            if "too many attempts" in msg.lower():
                raise CronometerAuthError(
                    f"Cronometer rate-limited the login endpoint: {msg}. "
                    f"Wait several minutes before retrying."
                )
            raise CronometerAuthError(f"login: server rejected: {data!r}")
        self._user_id = data["id"]
        self._token = data["sessionKey"]
        log.info(
            "cronometer.mobile.login.ok",
            user_id=self._user_id,
            token_prefix=self._token[:6] if self._token else None,
        )

    def _ensure_auth(self) -> None:
        if self._token is None:
            self.login()

    def _auth_block(self) -> dict:
        return {"userId": self._user_id, "token": self._token, **_APP_AUTH}

    # ---- core request paths -------------------------------------------------
    @retry(
        retry=retry_if_exception_type(
            (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=1, max=20),
        reraise=True,
    )
    def _post_v2(self, endpoint: str, payload: dict, *, _relogin: bool = False) -> dict:
        self._ensure_auth()
        body = dict(payload)
        body["auth"] = self._auth_block()
        body.setdefault("lastSeen", 0)
        resp = self._http.post(endpoint, json=body)
        if resp.status_code in (401, 403) and not _relogin:
            log.warning("cronometer.mobile.401_refresh", endpoint=endpoint)
            self._token = None
            self.login()
            return self._post_v2(endpoint, payload, _relogin=True)
        if resp.status_code >= 400:
            if not _is_retryable(
                httpx.HTTPStatusError("", request=resp.request, response=resp)
            ):
                raise CronometerAPIError(
                    f"POST {endpoint} → {resp.status_code}: {resp.text[:300]}"
                )
            resp.raise_for_status()
        try:
            data = resp.json()
        except Exception as e:
            raise CronometerAPIError(
                f"POST {endpoint}: non-JSON body: {resp.text[:200]}"
            ) from e
        if isinstance(data, dict) and data.get("result") == "FAILURE":
            if not _relogin:
                log.warning(
                    "cronometer.mobile.failure_relogin",
                    endpoint=endpoint,
                    body=str(data)[:200],
                )
                self._token = None
                self.login()
                return self._post_v2(endpoint, payload, _relogin=True)
            raise CronometerAPIError(f"{endpoint} returned FAILURE: {data}")
        return data

    def _request_v3(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        _relogin: bool = False,
    ) -> httpx.Response:
        self._ensure_auth()
        url = f"/api/v3/user/{self._user_id}{path}"
        resp = self._http.request(
            method,
            url,
            json=json_body,
            headers={
                "x-crono-session": self._token or "",
                "x-crono-app-os": "android",
                "x-crono-app-build-number": "2807",
                "x-crono-app-version": "4.48.2",
                "content-type": "application/json; charset=utf-8",
            },
        )
        if resp.status_code in (401, 403) and not _relogin:
            log.warning("cronometer.mobile.v3.401_refresh", path=path)
            self._token = None
            self.login()
            return self._request_v3(method, path, json_body=json_body, _relogin=True)
        return resp

    # ---- date helper --------------------------------------------------------
    @staticmethod
    def _format_day(d: date | None = None) -> str:
        """Non-zero-padded YYYY-M-D — Cronometer's exact wire format. ISO
        dates with leading zeros cause silent 'no matching diary' on
        get_diary, which then makes delete_entries report 0 removed."""
        d = d or date.today()
        return f"{d.year}-{d.month}-{d.day}"

    # ---- food search --------------------------------------------------------
    def search_food(self, query: str) -> list[dict]:
        data = self._post_v2(
            "/api/v2/find_food",
            {
                "query": query,
                "tab": "ALL",
                "sources": ["All"],
                "config": {
                    "newSearch": True,
                    "newSpellcheck": True,
                    "call_version": 1,
                },
            },
        )
        return data.get("foods") or []

    # ---- food detail --------------------------------------------------------
    def get_food(self, food_id: int) -> dict:
        return self._post_v2(
            "/api/v2/get_food",
            {"id": food_id, "config": {"call_version": 1}},
        )

    # ---- custom food --------------------------------------------------------
    def create_custom_food(
        self,
        name: str,
        *,
        calories: float,
        protein_g: float,
        fat_g: float,
        carbs_g: float,
        fiber_g: float = 0,
        sugar_g: float = 0,
        sodium_mg: float = 0,
        serving_name: str = "1 serving",
        serving_grams: float = 100.0,
    ) -> dict:
        if serving_grams <= 0:
            raise CronometerAPIError("serving_grams must be > 0")
        # Cronometer stores nutrients per-100g internally — scale from
        # whatever serving size the caller used.
        scale = 100.0 / serving_grams
        net_carbs = max(0.0, carbs_g - fiber_g)
        nutrients = [
            {"id": NUTRIENT_IDS["energy"], "amount": round(calories * scale, 2)},
            {"id": NUTRIENT_IDS["protein"], "amount": round(protein_g * scale, 2)},
            {"id": NUTRIENT_IDS["fat"], "amount": round(fat_g * scale, 2)},
            {"id": NUTRIENT_IDS["carbs"], "amount": round(carbs_g * scale, 2)},
            {"id": NUTRIENT_IDS["fiber"], "amount": round(fiber_g * scale, 2)},
            {"id": NUTRIENT_IDS["sugar"], "amount": round(sugar_g * scale, 2)},
            {"id": NUTRIENT_IDS["sodium"], "amount": round(sodium_mg * scale, 2)},
            # Derived/calculated fields the app includes — match the wire shape.
            {"id": -203, "amount": round(protein_g * scale, 2)},
            {"id": -204, "amount": round(fat_g * scale, 2)},
            {"id": -205, "amount": round(carbs_g * scale, 2)},
            {"id": -221, "amount": 0},
            {"id": NUTRIENT_IDS["net_carbs"], "amount": round(net_carbs * scale, 2)},
        ]
        payload = {
            "data": {
                "id": 0,
                "name": name,
                "category": 0,
                "owner": None,
                "retired": None,
                "source": None,
                "defaultMeasureId": 0,
                "comments": None,
                "alternateId": None,
                "measures": [
                    {
                        "id": 0,
                        "name": serving_name,
                        "value": serving_grams,
                        "amount": 1.0,
                        "type": "Atomic",
                    }
                ],
                "labelType": "AMERICAN_2016",
                "nutrients": nutrients,
                "properties": {},
                "foodTags": [],
            },
            "config": {"call_version": 1},
        }
        data = self._post_v2("/api/v2/add_food", payload)
        if not data.get("id"):
            raise CronometerAPIError(f"create_custom_food: no id in response: {data}")
        return data

    # ---- add serving --------------------------------------------------------
    def add_serving(
        self,
        *,
        food_id: int,
        grams: float,
        measure_id: int | None,
        translation_id: int = 0,
        day: date | None = None,
        diary_group: int = 0,
        eaten_time: datetime | None = None,
    ) -> dict:
        # Force a login before constructing the payload — the serving dict
        # embeds `userId`, and _post_v2's _ensure_auth() fires too late
        # (Cronometer rejects with 'userId is not a int' if the payload was
        # built pre-login).
        self._ensure_auth()
        now = eaten_time or datetime.now()
        target_day = day or now.date()
        day_str = self._format_day(target_day)
        time_str = f"{now.hour}:{now.minute}:{now.second}"
        # Cronometer's `order` encodes the meal group in the high 16 bits and
        # the within-meal index in the low 16. We always write index=1; the
        # server re-orders within the day on the next diary read.
        order = (diary_group << 16) | 1
        serving = {
            "order": order,
            "day": day_str,
            "time": time_str,
            "offset": None,
            "source": None,
            "userId": self._user_id,
            "servingId": None,
            "type": "Serving",
            "foodId": food_id,
            "measureId": measure_id or 0,
            "grams": grams,
            "translationId": translation_id,
        }
        return self._post_v2(
            "/api/v2/add_serving",
            {"serving": serving, "config": {"call_version": 2}},
        )

    # ---- diary (read; used by delete_entries) ------------------------------
    def get_diary(self, day: date | None = None) -> dict:
        return self._post_v2(
            "/api/v2/get_diary",
            {"day": self._format_day(day), "config": {"call_version": 1}},
        )

    # ---- session reset (for test isolation) --------------------------------
    def invalidate_session(self) -> None:
        """Clear the cached token; the next request triggers a fresh login.
        Used by `reset_shared_client()` and on unrecoverable auth errors."""
        self._user_id = None
        self._token = None

    # ---- delete entries -----------------------------------------------------
    def delete_entries(
        self,
        entry_ids: Iterable[int | str],
        day: date | None = None,
    ) -> dict:
        """v3 DELETE wants full serving objects, not just IDs — so we fetch
        the day's diary first, match by servingId, then send the matched
        objects through. Returns {removed, count, missing}."""
        wanted = {str(e) for e in entry_ids}
        diary = self.get_diary(day).get("diary") or []
        # v3 DELETE rejects bare diary-entry rows: each object's deserializer
        # requires an `id` field equal to its `servingId`. get_diary doesn't
        # emit `id`, so we inject it. (The reference impl predates this
        # tightening — bare rows used to work.)
        to_delete = [
            {**e, "id": e.get("servingId")}
            for e in diary
            if str(e.get("servingId")) in wanted
        ]
        if not to_delete:
            return {
                "removed": [],
                "count": 0,
                "missing": sorted(wanted),
            }
        resp = self._request_v3(
            "DELETE",
            "/diary-entries",
            json_body={"diaryEntries": to_delete},
        )
        if resp.status_code == 204:
            removed = [str(e["servingId"]) for e in to_delete]
            return {
                "removed": removed,
                "count": len(removed),
                "missing": sorted(wanted - set(removed)),
            }
        raise CronometerAPIError(
            f"delete_entries: HTTP {resp.status_code}: {resp.text[:300]}"
        )


# ---- module-level shared client --------------------------------------------
# Cronometer rate-limits the login endpoint hard ("Too Many Attempts" after
# ~4 logins in quick succession). Each MCP tool call creating a fresh client
# would burn a login every time, so the write tools share one client across
# calls and let it auto-refresh on 401. Singleton is lazy and process-local;
# MCP server processes are single-tenant and short-lived enough that token
# expiry isn't a concern between restarts. The httpx.Client underneath is
# thread-safe.
_shared_client: CronometerMobileClient | None = None


def get_shared_client() -> CronometerMobileClient:
    """Lazy-singleton accessor for the mobile-API client. Builds on first
    call, then reuses the cached session token across subsequent tool
    invocations until the process restarts (or 401 triggers a re-login)."""
    global _shared_client
    if _shared_client is None:
        _shared_client = CronometerMobileClient()
    return _shared_client


def reset_shared_client() -> None:
    """Drop the shared client (closing the underlying httpx pool). For tests
    and graceful shutdown."""
    global _shared_client
    if _shared_client is not None:
        with contextlib.suppress(Exception):
            _shared_client.close()
    _shared_client = None

"""Cal AI client: Firebase auth + Firestore REST + the api.calai.app/v6 API.

CONFIRMED from the capture:
  * Firebase project `calai-app`; auth is a Firebase ID token (RS256, 1h, iss
    securetoken.google.com/calai-app). Refreshed from a refresh token via
    securetoken.googleapis.com/v1/token?key=<WEB_API_KEY>.
  * api.calai.app/v6/<endpoint> POSTs an envelope {userInfo, data} with a
    Bearer ID token (used for fixFood / health-score / getSubscription / …).
  * Food photos live in Firebase Storage calai-app.appspot.com under
    food_images_user/<uid>/<imageId>.jpg.

STILL NEEDED from a follow-up capture to go live (see RUNBOOK.md):
  * the Firebase Web API key (in the login identitytoolkit/securetoken URL),
  * a refresh token (from the login response), stored in oauth_tokens('calai'),
  * the Firestore collection path + document schema for the diary (firestore.
    googleapis.com was not exercised in the first capture).

This module implements all the standard, capture-independent mechanics so that
finishing is just filling in those three values + the diary query.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from lifeos_core.db import tx
from lifeos_core.logging import get_logger

log = get_logger(__name__)

PROJECT_ID = "calai-app"
STORAGE_BUCKET = "calai-app.appspot.com"
SECURETOKEN_URL = "https://securetoken.googleapis.com/v1/token"
FIRESTORE_BASE = (
    f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/(default)/documents"
)
CALAI_API = "https://api.calai.app/v6"
# Mimic the iOS app so api.calai.app doesn't 403 a non-app UA (from the capture).
APP_USER_AGENT = "Cal%20AI/1779 CFNetwork/3860.600.12 Darwin/25.6.0"
APP_VERSION = "3.3.9"
_SKEW = timedelta(minutes=5)


class CalaiAuthError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("CALAI_FIREBASE_API_KEY")
    if not key:
        raise CalaiAuthError(
            "CALAI_FIREBASE_API_KEY is not set — capture a Cal AI login to recover "
            "the Firebase Web API key (it's the ?key= param on the identitytoolkit/"
            "securetoken request) and put it in .env."
        )
    return key


def _save_tokens(*, id_token: str, refresh_token: str, expires_at: datetime,
                 user_id: str | None) -> None:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO oauth_tokens (service, access_token, refresh_token, id_token,
                                      expires_at, metadata)
            VALUES ('calai', %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (service) DO UPDATE SET
              access_token = EXCLUDED.access_token,
              refresh_token = EXCLUDED.refresh_token,
              id_token = EXCLUDED.id_token,
              expires_at = EXCLUDED.expires_at,
              metadata = EXCLUDED.metadata
            """,
            [id_token, refresh_token, id_token, expires_at,
             f'{{"source":"firebase_refresh","user_id":"{user_id or ""}"}}'],
        )


class CalaiAuth:
    """Loads the Cal AI Firebase token from oauth_tokens('calai') and refreshes
    the 1h ID token from the stored refresh token when stale."""

    def __init__(self) -> None:
        self._id_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: datetime | None = None
        self._user_id: str | None = None
        self._load()

    def _load(self) -> None:
        with tx() as c, c.cursor() as cur:
            cur.execute(
                "SELECT access_token, refresh_token, expires_at, metadata "
                "FROM oauth_tokens WHERE service = 'calai'"
            )
            row = cur.fetchone()
        if row:
            self._id_token = row["access_token"]
            self._refresh_token = row["refresh_token"]
            self._expires_at = row["expires_at"]
            md = row.get("metadata") or {}
            self._user_id = md.get("user_id") if isinstance(md, dict) else None

    @property
    def user_id(self) -> str | None:
        return self._user_id

    def ensure_fresh(self) -> str:
        """Return a valid Firebase ID token, refreshing if within the skew."""
        now = datetime.now(UTC)
        exp = self._expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if self._id_token and exp and exp - _SKEW > now:
            return self._id_token
        if not self._refresh_token:
            raise CalaiAuthError(
                "no Cal AI refresh token stored — run `python -m ingest_calai login` "
                "after capturing a login (see RUNBOOK.md)."
            )
        return self._refresh(self._refresh_token)

    def _refresh(self, refresh_token: str) -> str:
        data = parse_securetoken_response(_securetoken_refresh(refresh_token, _api_key()))
        self._id_token = data["id_token"]
        self._refresh_token = data["refresh_token"]
        self._user_id = data.get("user_id") or self._user_id
        self._expires_at = datetime.now(UTC) + timedelta(seconds=data["expires_in"])
        _save_tokens(id_token=self._id_token, refresh_token=self._refresh_token,
                     expires_at=self._expires_at, user_id=self._user_id)
        log.info("calai.token_refreshed", user_id=self._user_id)
        return self._id_token

    def headers(self, *, app: bool = False) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.ensure_fresh()}",
             "Content-Type": "application/json", "Accept": "*/*"}
        if app:
            h["User-Agent"] = APP_USER_AGENT
        return h

    def user_info(self) -> dict:
        """The userInfo envelope api.calai.app/v6 expects (from the capture)."""
        return {
            "userId": self._user_id, "platform": "iOS", "device": "iPhone",
            "environment": "production", "version": APP_VERSION, "locale": "en",
            "iosVersion": "26.6",
            "remoteConfigExperiment": {
                "features_experiment_layer": "none", "onboarding_experiment_layer": "none",
                "backend_experiment_layer": "none", "misc_experiment_layer": "none",
            },
        }


@retry(retry=retry_if_exception_type(httpx.HTTPError),
       stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=8))
def _securetoken_refresh(refresh_token: str, api_key: str) -> dict:
    resp = httpx.post(
        SECURETOKEN_URL, params={"key": api_key},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=20.0,
    )
    if resp.status_code == 400:
        raise CalaiAuthError(f"Firebase refresh rejected (token expired?): {resp.text[:200]}")
    resp.raise_for_status()
    return resp.json()


def parse_securetoken_response(payload: dict) -> dict:
    """Normalize the securetoken response (snake_case + camelCase both occur)."""
    return {
        "id_token": payload.get("id_token") or payload.get("idToken"),
        "refresh_token": payload.get("refresh_token") or payload.get("refreshToken"),
        "expires_in": int(payload.get("expires_in") or payload.get("expiresIn") or 3600),
        "user_id": payload.get("user_id") or payload.get("userId"),
    }


# --- Firestore REST -------------------------------------------------------
def firestore_run_query(auth: CalaiAuth, structured_query: dict) -> list[dict]:
    """POST a Firestore structuredQuery and return decoded documents.
    `structured_query` is the Firestore `structuredQuery` object (from/where/
    orderBy/limit). Returns a list of {"_name": <doc path>, **fields}."""
    resp = httpx.post(f"{FIRESTORE_BASE}:runQuery", headers=auth.headers(),
                      json={"structuredQuery": structured_query}, timeout=30.0)
    resp.raise_for_status()
    out: list[dict] = []
    for chunk in resp.json():
        doc = chunk.get("document")
        if not doc:
            continue
        out.append(decode_document(doc))
    return out


def decode_document(doc: dict) -> dict:
    """Flatten a Firestore REST document to a plain dict, keeping the doc path."""
    fields = {k: _decode_value(v) for k, v in (doc.get("fields") or {}).items()}
    fields["_name"] = doc.get("name")
    return fields


def _decode_value(v: dict) -> Any:
    """Decode a single Firestore typed value to a native Python value."""
    if "stringValue" in v:
        return v["stringValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "booleanValue" in v:
        return v["booleanValue"]
    if "timestampValue" in v:
        return v["timestampValue"]
    if "nullValue" in v:
        return None
    if "referenceValue" in v:
        return v["referenceValue"]
    if "mapValue" in v:
        return {k: _decode_value(x) for k, x in (v["mapValue"].get("fields") or {}).items()}
    if "arrayValue" in v:
        return [_decode_value(x) for x in (v["arrayValue"].get("values") or [])]
    if "geoPointValue" in v:
        return v["geoPointValue"]
    return None


# --- api.calai.app/v6 (AI + account; optional enrichment) -----------------
def calai_v6(auth: CalaiAuth, endpoint: str, data: dict | None = None) -> dict:
    """Call an api.calai.app/v6 endpoint with the confirmed {userInfo, data}
    envelope. Used for health-score etc.; the diary itself is in Firestore."""
    resp = httpx.post(f"{CALAI_API}/{endpoint}", headers=auth.headers(app=True),
                      json={"userInfo": auth.user_info(), "data": data or {}}, timeout=30.0)
    resp.raise_for_status()
    return resp.json()

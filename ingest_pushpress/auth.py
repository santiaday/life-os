"""PushPress auth: bearer token + rotating refresh token, with login fallback.

PushPress hands out two flavors of tokens:

  - login response: 60-day access + 60-day refresh. POST /v2/auth/login with
    JSON {username, password}. Returns {accessToken, refreshToken, ...}.
  - refresh response: short-lived (~30min) access + a fresh refresh token.
    POST /v2/auth/token/refresh with {refreshToken}. Returns
    {accessToken, refreshToken, expires_at}.

Strategy:
  - oauth_tokens(service='pushpress') stores the latest pair + expires_at.
  - On each request, if access is past expiry (with skew), refresh first.
  - If refresh fails (refresh-token rotated and lost, server-side revocation,
    etc.) AND PUSHPRESS_USERNAME / PUSHPRESS_PASSWORD are set, re-login and
    persist the new pair.
  - The bootstrap CLI (`python -m ingest_pushpress login`) does the same path
    explicitly, so the very first run never has to fall back.

Because access tokens are JWTs we read `exp` directly off the token instead
of trusting clocks; refresh response also returns `expires_at` ISO timestamp
which we round-trip back to Postgres.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx

from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

SERVICE = "pushpress"
API_BASE = "https://api.pushpress.com"
LOGIN_PATH = "/v2/auth/login"
REFRESH_PATH = "/v2/auth/token/refresh"
EXPIRY_SKEW = timedelta(minutes=2)
WEB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://members.pushpress.com",
    "referer": "https://members.pushpress.com/",
    "user-agent": WEB_USER_AGENT,
}


class PushPressAuthError(RuntimeError):
    """Auth failed and we have no fallback. Operator must re-set credentials."""


def _now() -> datetime:
    return datetime.now(UTC)


def _decode_jwt_exp(token: str) -> datetime | None:
    """Pull the `exp` claim off a PushPress JWT. Returns None on any decode
    failure — caller then falls back to the server-supplied expires_at."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            return datetime.fromtimestamp(int(exp), tz=UTC)
    except Exception:
        pass
    return None


def _save(access: str, refresh: str, expires_at: datetime) -> None:
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO oauth_tokens (service, access_token, refresh_token, expires_at, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (service) DO UPDATE SET
              access_token = EXCLUDED.access_token,
              refresh_token = EXCLUDED.refresh_token,
              expires_at = EXCLUDED.expires_at,
              updated_at = now()
            """,
            [SERVICE, access, refresh, expires_at],
        )
    log.info(
        "pushpress.auth.saved",
        access_chars=len(access),
        refresh_chars=len(refresh),
        expires_at=expires_at.isoformat(),
    )


def _load() -> dict | None:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            "SELECT access_token, refresh_token, expires_at FROM oauth_tokens WHERE service = %s",
            [SERVICE],
        )
        return cur.fetchone()


def _login(username: str, password: str) -> dict:
    """POST credentials → access/refresh pair. The login response holds 60-day
    tokens; we treat that as the canonical 'fresh credential' moment."""
    log.info("pushpress.auth.login", username=username)
    with httpx.Client(timeout=20.0, headers=DEFAULT_HEADERS) as client:
        resp = client.post(
            f"{API_BASE}{LOGIN_PATH}",
            json={"username": username, "password": password},
        )
    if resp.status_code != 200:
        raise PushPressAuthError(
            f"PushPress login failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
    data = resp.json()
    access = data.get("accessToken")
    refresh = data.get("refreshToken")
    if not access or not refresh:
        raise PushPressAuthError(
            f"PushPress login response missing tokens: keys={list(data.keys())}"
        )
    exp = _decode_jwt_exp(access) or (_now() + timedelta(days=60))
    _save(access, refresh, exp)
    return {"access_token": access, "refresh_token": refresh, "expires_at": exp}


def _refresh(refresh_token: str) -> dict:
    """Rotate tokens. Returns the new pair. Raises on non-200 — caller decides
    whether to fall back to login."""
    log.info("pushpress.auth.refresh")
    with httpx.Client(timeout=20.0, headers=DEFAULT_HEADERS) as client:
        resp = client.post(
            f"{API_BASE}{REFRESH_PATH}",
            json={"refreshToken": refresh_token},
        )
    if resp.status_code != 200:
        raise PushPressAuthError(
            f"PushPress refresh failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
    data = resp.json()
    access = data.get("accessToken")
    new_refresh = data.get("refreshToken")
    if not access or not new_refresh:
        raise PushPressAuthError(
            f"PushPress refresh response missing tokens: keys={list(data.keys())}"
        )
    # Prefer the JWT's own exp (authoritative); fall back to server-supplied
    # expires_at; finally a 25-minute pessimistic default.
    exp = _decode_jwt_exp(access)
    if exp is None:
        raw_exp = data.get("expires_at")
        if isinstance(raw_exp, str):
            try:
                exp = datetime.fromisoformat(raw_exp.replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=UTC)
            except ValueError:
                exp = None
    if exp is None:
        exp = _now() + timedelta(minutes=25)
    _save(access, new_refresh, exp)
    return {"access_token": access, "refresh_token": new_refresh, "expires_at": exp}


class PushPressAuth:
    """Bearer-token provider. Lazy: only refreshes/logs in when a caller
    actually asks for headers and the cached token is past expiry.

    Caller shape:
        auth = PushPressAuth()
        ... auth.headers() ...   # before each HTTP request
    """

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._username = username or settings.PUSHPRESS_USERNAME
        self._password = password or settings.PUSHPRESS_PASSWORD
        self._access: str | None = None
        self._expires_at: datetime | None = None

    def _hydrate_from_db(self) -> None:
        row = _load()
        if row:
            self._access = row.get("access_token")
            self._expires_at = row.get("expires_at")

    def _ensure(self) -> str:
        """Return a usable access token. Refresh / login as needed.

        Order of attempts:
          1. In-memory cache, if still fresh.
          2. DB row, if still fresh.
          3. Refresh using DB refresh_token.
          4. Login with username/password (only if both env vars set).
        """
        if (
            self._access
            and self._expires_at
            and self._expires_at > _now() + EXPIRY_SKEW
        ):
            return self._access

        if self._access is None:
            self._hydrate_from_db()

        if (
            self._access
            and self._expires_at
            and self._expires_at > _now() + EXPIRY_SKEW
        ):
            return self._access

        # Cached pair is missing or stale. Try refresh first.
        row = _load()
        if row and row.get("refresh_token"):
            try:
                pair = _refresh(row["refresh_token"])
                self._access = pair["access_token"]
                self._expires_at = pair["expires_at"]
                return self._access
            except PushPressAuthError as e:
                log.warning("pushpress.auth.refresh_failed", error=str(e))

        # Refresh exhausted — fall back to login if creds are configured.
        if self._username and self._password:
            pair = _login(self._username, self._password)
            self._access = pair["access_token"]
            self._expires_at = pair["expires_at"]
            return self._access

        raise PushPressAuthError(
            "No usable PushPress token. Either run "
            "`python -m ingest_pushpress login` once, or set "
            "PUSHPRESS_USERNAME and PUSHPRESS_PASSWORD in .env so the "
            "ingester can re-login automatically when the refresh token "
            "expires."
        )

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self._ensure()}"}


def login_with_credentials(username: str, password: str) -> dict:
    """Bootstrap helper used by the `login` CLI subcommand. Persists the new
    pair to oauth_tokens and returns a redacted summary."""
    pair = _login(username, password)
    return {
        "service": SERVICE,
        "access_chars": len(pair["access_token"]),
        "refresh_chars": len(pair["refresh_token"]),
        "expires_at": pair["expires_at"].isoformat(),
    }

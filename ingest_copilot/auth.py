"""Copilot Money JWT auth.

Reverse-engineered: Copilot's app uses Cognito for login and rotates JWTs via
a refresh-token flow. We try refresh first; on 401 we fall back to email/
password login. Refresh tokens persist in oauth_tokens.

Endpoints (all best-effort — Copilot can change these):
  POST https://app.copilot.money/api/auth/login           {email, password}
  POST https://app.copilot.money/api/auth/refresh         {refreshToken}

Both should return {accessToken, refreshToken, expiresIn} (or similar). If
the field names drift, raise CopilotAuthError loudly so we notice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from lifeos_core import oauth_store
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

LOGIN_URL = "https://app.copilot.money/api/auth/login"
REFRESH_URL = "https://app.copilot.money/api/auth/refresh"

REFRESH_BUFFER_MIN = 5


class CopilotAuthError(RuntimeError):
    pass


def _mask(t: str | None) -> str:
    if not t:
        return "<empty>"
    return f"{t[:4]}…{t[-4:]} ({len(t)} chars)"


def login_with_password() -> dict:
    """Email + password → {access, refresh, expires_at}. Persists refresh token."""
    if not settings.COPILOT_EMAIL or not settings.COPILOT_PASSWORD:
        raise RuntimeError("COPILOT_EMAIL and COPILOT_PASSWORD must be set in .env")
    log.info("copilot.auth.login_with_password")
    resp = httpx.post(
        LOGIN_URL,
        json={"email": settings.COPILOT_EMAIL, "password": settings.COPILOT_PASSWORD},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise CopilotAuthError(
            f"login {resp.status_code}: {resp.text[:300]}"
        )
    return _persist(resp.json())


def refresh_access_token() -> str:
    """Return a usable access token. Refresh if expired or missing; on
    refresh failure, fall back to password login."""
    stored = oauth_store.load("copilot")
    now = datetime.now(timezone.utc)

    if (
        stored
        and stored.get("access_token")
        and stored.get("expires_at")
        and stored["expires_at"] > now + timedelta(minutes=REFRESH_BUFFER_MIN)
    ):
        return stored["access_token"]

    if stored and stored.get("refresh_token"):
        try:
            log.info("copilot.auth.refresh", refresh=_mask(stored["refresh_token"]))
            resp = httpx.post(
                REFRESH_URL,
                json={"refreshToken": stored["refresh_token"]},
                timeout=30.0,
            )
            if resp.status_code < 400:
                return _persist(resp.json())["accessToken"]
            log.warning(
                "copilot.auth.refresh_failed",
                status=resp.status_code,
                body=resp.text[:300],
            )
        except Exception as e:  # noqa: BLE001
            log.exception("copilot.auth.refresh_error")

    # Fall through to password login.
    return login_with_password()["accessToken"]


def _persist(payload: dict) -> dict:
    """Pull tokens from a login/refresh response and write to oauth_tokens.

    Tolerates a few field-name spellings since Copilot's surface isn't
    documented and may drift. Raises CopilotAuthError if neither shape works."""
    access = payload.get("accessToken") or payload.get("access_token") or payload.get("idToken")
    refresh = payload.get("refreshToken") or payload.get("refresh_token")
    expires_in = (
        payload.get("expiresIn")
        or payload.get("expires_in")
        or payload.get("accessTokenExpiresIn")
    )
    if not access or not refresh:
        raise CopilotAuthError(
            f"Copilot auth response missing tokens. Keys: {sorted(payload.keys())}"
        )

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        if expires_in
        else None
    )
    oauth_store.save(
        "copilot",
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
    )
    log.info(
        "copilot.auth.saved",
        access=_mask(access),
        refresh=_mask(refresh),
        expires_at=expires_at.isoformat() if expires_at else None,
    )
    return {"accessToken": access, "refreshToken": refresh, "expiresAt": expires_at}

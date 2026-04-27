"""Whoop OAuth bootstrap and refresh.

Three CLI moments (one-time):
  1. `python -m ingest_whoop oauth-init`        prints the authorize URL.
  2. visit URL, approve, get redirected with ?code=...
  3. `python -m ingest_whoop oauth-exchange --code <code>`  exchanges for tokens
     and writes them to oauth_tokens.

Then `refresh_access_token()` is called by the client on every run; it rotates
the refresh token (Whoop returns a new one each time) and persists.

Reference: https://developer.whoop.com/docs/developing/oauth
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx

from lifeos_core import oauth_store
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"

SCOPES = [
    "read:recovery",
    "read:cycles",
    "read:sleep",
    "read:workout",
    "read:profile",
    "read:body_measurement",
    "offline",
]


def authorize_url(state: str = "lifeos") -> str:
    """The URL Santi visits to approve. State is a CSRF token; for a personal
    one-shot flow it's just a string."""
    if not settings.WHOOP_CLIENT_ID or not settings.WHOOP_REDIRECT_URI:
        raise RuntimeError("WHOOP_CLIENT_ID and WHOOP_REDIRECT_URI must be set in .env")
    params = {
        "response_type": "code",
        "client_id": settings.WHOOP_CLIENT_ID,
        "redirect_uri": settings.WHOOP_REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str) -> dict:
    """Trade the authorize code for the first refresh token, persist it."""
    if not settings.WHOOP_CLIENT_SECRET:
        raise RuntimeError("WHOOP_CLIENT_SECRET must be set in .env")

    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.WHOOP_CLIENT_ID,
            "client_secret": settings.WHOOP_CLIENT_SECRET,
            "redirect_uri": settings.WHOOP_REDIRECT_URI,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    tok = resp.json()

    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(tok.get("expires_in", 3600))
    )
    oauth_store.save(
        "whoop",
        access_token=tok["access_token"],
        refresh_token=tok["refresh_token"],
        expires_at=expires_at,
    )
    log.info("whoop.oauth.exchange_ok", expires_at=expires_at.isoformat())
    return tok


def refresh_access_token() -> str:
    """Return a usable access token, refreshing if expired or close to it.
    Whoop rotates refresh tokens — persist the new one every time."""
    stored = oauth_store.load("whoop")
    if stored is None:
        raise RuntimeError(
            "No Whoop refresh token in oauth_tokens. "
            "Run `python -m ingest_whoop oauth-init` first."
        )

    now = datetime.now(timezone.utc)
    expires_at = stored.get("expires_at")
    if (
        stored.get("access_token")
        and expires_at is not None
        and expires_at > now + timedelta(minutes=5)
    ):
        return stored["access_token"]

    log.info("whoop.oauth.refresh")
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": stored["refresh_token"],
            "client_id": settings.WHOOP_CLIENT_ID,
            "client_secret": settings.WHOOP_CLIENT_SECRET,
            "scope": " ".join(SCOPES),
        },
        timeout=30.0,
    )
    if resp.status_code >= 400:
        log.error("whoop.oauth.refresh_failed", status=resp.status_code, body=resp.text[:500])
        resp.raise_for_status()
    tok = resp.json()

    new_expires_at = now + timedelta(seconds=int(tok.get("expires_in", 3600)))
    oauth_store.save(
        "whoop",
        access_token=tok["access_token"],
        refresh_token=tok.get("refresh_token", stored["refresh_token"]),
        expires_at=new_expires_at,
    )
    return tok["access_token"]

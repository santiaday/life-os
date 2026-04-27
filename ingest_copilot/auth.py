"""Copilot Money auth — Firebase refresh-token flow.

Copilot's auth is Firebase under the hood. Sign-in with Email/Google/Apple
all converge on a Firebase refresh token that the web app caches in
IndexedDB. We don't try to drive a browser; instead the user captures the
refresh token once (DevTools → Application → IndexedDB → firebaseLocalStorage)
and stores it via:

    python -m ingest_copilot set-refresh-token --token AMf-...

After that, every ingest call exchanges the refresh token for a fresh ID
token via Google's secure-token endpoint. Firebase rotates refresh tokens
on each exchange, and we persist the new one.

Reference (the protocol is public Firebase Auth, key is the public Web API
key Copilot ships in their JS bundle):
  https://firebase.google.com/docs/reference/rest/auth#section-refresh-token
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from lifeos_core import oauth_store
from lifeos_core.logging import get_logger

log = get_logger(__name__)

# Public Web API key embedded in Copilot's JS bundle. Same value used by
# copilot-money-mcp; safe to commit since it's a client-side identifier, not
# a secret (anyone can grab it from the Copilot web app).
FIREBASE_API_KEY = "AIzaSyAMgjkeOSkHj4J4rlswOkD16N3WQOoNPpk"
TOKEN_URL = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"

REFRESH_BUFFER_MIN = 5


class CopilotAuthError(RuntimeError):
    pass


def _mask(t: str | None) -> str:
    if not t:
        return "<empty>"
    return f"{t[:6]}…{t[-4:]} ({len(t)} chars)"


def store_refresh_token(refresh_token: str) -> None:
    """One-time bootstrap. The user pastes a refresh token captured from
    their browser (Copilot Web → DevTools → Application → IndexedDB →
    firebaseLocalStorageDb → firebaseLocalStorage → entry whose key looks
    like `firebase:authUser:...`; the `stsTokenManager.refreshToken` value
    is what we want). It starts with `AMf-`."""
    if not refresh_token or not refresh_token.startswith("AMf-"):
        raise ValueError(
            "Refresh token doesn't look right. Expected a string starting "
            "with 'AMf-' (Firebase refresh tokens). Got: "
            f"{_mask(refresh_token)}"
        )
    oauth_store.save("copilot", refresh_token=refresh_token, access_token=None, expires_at=None)
    log.info("copilot.auth.refresh_token_stored", token=_mask(refresh_token))


def refresh_access_token() -> str:
    """Return a usable Firebase ID token (passed as Bearer to GraphQL).
    Refresh if expired or missing. Persists the new refresh token Firebase
    returns (they rotate on every exchange)."""
    stored = oauth_store.load("copilot")
    if stored is None or not stored.get("refresh_token"):
        raise CopilotAuthError(
            "No Copilot refresh token stored. Capture one from your browser "
            "(see ingest_copilot/auth.py docstring) and run: "
            "python -m ingest_copilot set-refresh-token --token <AMf-...>"
        )

    now = datetime.now(timezone.utc)
    expires_at = stored.get("expires_at")
    if (
        stored.get("access_token")
        and expires_at is not None
        and expires_at > now + timedelta(minutes=REFRESH_BUFFER_MIN)
    ):
        return stored["access_token"]

    log.info("copilot.auth.refresh", refresh=_mask(stored["refresh_token"]))
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": stored["refresh_token"],
        },
        timeout=30.0,
    )
    if resp.status_code >= 400:
        log.error(
            "copilot.auth.refresh_failed",
            status=resp.status_code,
            body=resp.text[:300],
        )
        # Common case: refresh token revoked/expired. Surface clearly so the
        # user knows to re-capture from browser.
        raise CopilotAuthError(
            f"Firebase token refresh failed ({resp.status_code}). "
            f"Your refresh token may be revoked — capture a fresh one from "
            f"your browser. Body: {resp.text[:200]}"
        )

    payload = resp.json()
    id_token = payload.get("id_token")
    new_refresh = payload.get("refresh_token")
    expires_in = int(payload.get("expires_in", 3600))
    if not id_token or not new_refresh:
        raise CopilotAuthError(
            f"Firebase response missing tokens. Keys: {sorted(payload.keys())}"
        )

    new_expires_at = now + timedelta(seconds=expires_in)
    oauth_store.save(
        "copilot",
        access_token=id_token,
        refresh_token=new_refresh,
        expires_at=new_expires_at,
    )
    log.info(
        "copilot.auth.refreshed",
        id_token=_mask(id_token),
        new_refresh=_mask(new_refresh),
        expires_at=new_expires_at.isoformat(),
    )
    return id_token

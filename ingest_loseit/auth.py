"""Lose It! token management.

`api.loseit.com/account/login` is an OAuth 2.0 token endpoint, not the bespoke
login it looks like -- probing it bare answers `missing grant_type`. It accepts
form-encoded (not JSON) requests on two grants:

    grant_type=password        username + password        -> new token pair
    grant_type=refresh_token   access_token + refresh_token -> new token pair

That second grant is what makes this source self-sustaining. The `liauth`
session cookie is an ES384-signed JWT with a 14-day lifetime; it cannot be
minted or extended locally (the signature needs Lose It's private key), so the
only options were a human re-copying it from DevTools every other week, or
asking Lose It for a new one. This asks.

Note the unusual parameter naming: the refresh grant wants BOTH the current
`access_token` and the `refresh_token`, where most OAuth servers want only the
latter.

Precedence when a token is needed:

  1. A stored pair in `oauth_tokens(service='loseit')` that is still fresh.
  2. Refresh the stored pair. No credentials involved.
  3. `grant_type=password` with LOSEIT_EMAIL / LOSEIT_PASSWORD, if configured.
  4. The static LOSEIT_SESSION_COOKIE, used once to bootstrap the store.

Rotation is assumed: whatever the server returns is persisted immediately,
before the token is used for anything, so a rotated refresh token is never lost.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx

from lifeos_core import oauth_store
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

TOKEN_URL = "https://api.loseit.com/account/login"
SERVICE = "loseit"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.6 Safari/605.1.15"
)

# Copied from the real browser request. The token endpoint lives on
# api.loseit.com but the SPA is served from my.loseit.com, so this is a
# cross-origin call and Origin/Referer are part of what the server sees on a
# legitimate request. Cheap to send, and omitting them is the kind of thing
# that works until it abruptly doesn't.
TOKEN_HEADERS = {
    "User-Agent": USER_AGENT,
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
    "Origin": "https://my.loseit.com",
    "Referer": "https://my.loseit.com/",
}

# Refresh this far ahead of expiry. Generous because the sync runs every two
# hours -- there is no reason to cut it fine, and it leaves room for the job
# to be down for a few days without the token dying.
REFRESH_MARGIN = timedelta(days=4)


class LoseItAuthError(RuntimeError):
    """No usable token, and no configured way to obtain one."""


def jwt_expiry(token: str) -> datetime | None:
    """Read `exp` out of a JWT without verifying it. Verification would need
    Lose It's public key and buys nothing here -- we only want the lifetime."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(claims["exp"], UTC)
    except Exception:
        return None


def _post(data: dict) -> dict:
    with httpx.Client(timeout=30, headers=TOKEN_HEADERS) as c:
        r = c.post(TOKEN_URL, data=data)      # form-encoded; JSON is rejected
    if r.status_code == 429:
        raise LoseItAuthError("Lose It rate-limited the token request (429). Retry later.")
    if r.status_code != 200:
        raise LoseItAuthError(
            f"token request failed: HTTP {r.status_code} {r.text[:200]}")
    try:
        return r.json()
    except ValueError as e:
        raise LoseItAuthError(f"token endpoint returned non-JSON: {r.text[:200]}") from e


def _persist(payload: dict) -> str:
    """Store whatever came back and return the access token.

    Written before the token is used anywhere, so a rotated refresh token can
    never be lost to a later failure.
    """
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    if not access:
        raise LoseItAuthError(f"token response had no access_token: {sorted(payload)}")

    expires_at = jwt_expiry(access)
    if expires_at is None and payload.get("expires_in"):
        expires_at = datetime.now(UTC) + timedelta(seconds=int(payload["expires_in"]))

    oauth_store.save(
        SERVICE,
        # refresh_token is NOT NULL in the table; keep the prior one when the
        # server chooses not to rotate it.
        refresh_token=refresh or (_stored() or {}).get("refresh_token") or "",
        access_token=access,
        expires_at=expires_at,
    )
    log.info("loseit.token.stored",
             expires_at=expires_at.isoformat() if expires_at else None,
             rotated_refresh=bool(refresh))
    return access


def _stored() -> dict | None:
    try:
        return oauth_store.load(SERVICE)
    except Exception as e:
        log.warning("loseit.token.load_failed", error=str(e))
        return None


def _login_with_password() -> str:
    email = settings.LOSEIT_EMAIL
    password = settings.LOSEIT_PASSWORD
    if not (email and password):
        raise LoseItAuthError(
            "no way to obtain a Lose It token: the stored token is expired or "
            "missing, and LOSEIT_EMAIL / LOSEIT_PASSWORD are not set. Either set "
            "those, or paste a fresh `liauth` cookie into LOSEIT_SESSION_COOKIE."
        )
    log.info("loseit.token.password_grant")
    return _persist(_post({
        "grant_type": "password", "username": email, "password": password,
    }))


def access_token(force_refresh: bool = False) -> str:
    """Return a usable access token, refreshing or re-logging in as needed."""
    stored = _stored()

    if stored and stored.get("access_token") and not force_refresh:
        exp = stored.get("expires_at") or jwt_expiry(stored["access_token"])
        if exp and exp - REFRESH_MARGIN > datetime.now(UTC):
            return stored["access_token"]

    # Try the refresh grant. Needs both halves of the pair.
    if stored and stored.get("access_token") and stored.get("refresh_token"):
        try:
            log.info("loseit.token.refresh_grant")
            return _persist(_post({
                "grant_type": "refresh_token",
                "access_token": stored["access_token"],
                "refresh_token": stored["refresh_token"],
            }))
        except LoseItAuthError as e:
            log.warning("loseit.token.refresh_failed", error=str(e))

    # Bootstrap from env. A hand-pasted cookie seeds the store once; pairing it
    # with LOSEIT_REFRESH_TOKEN is what lets the refresh grant take over from
    # the next cycle onward, with no password stored anywhere.
    cookie = settings.LOSEIT_SESSION_COOKIE
    env_refresh = settings.LOSEIT_REFRESH_TOKEN
    if cookie and not force_refresh:
        exp = jwt_expiry(cookie)
        if exp and exp > datetime.now(UTC):
            already = stored and stored.get("access_token") == cookie
            if not already or (env_refresh and not (stored or {}).get("refresh_token")):
                oauth_store.save(SERVICE, refresh_token=env_refresh or "",
                                 access_token=cookie, expires_at=exp)
                log.info("loseit.token.bootstrapped_from_env",
                         expires_at=exp.isoformat(),
                         with_refresh_token=bool(env_refresh))
            return cookie

    # Last resort before giving up: an env refresh token paired with whatever
    # access token we last held.
    if env_refresh and stored and stored.get("access_token"):
        try:
            log.info("loseit.token.refresh_grant_from_env")
            return _persist(_post({
                "grant_type": "refresh_token",
                "access_token": stored["access_token"],
                "refresh_token": env_refresh,
            }))
        except LoseItAuthError as e:
            log.warning("loseit.token.env_refresh_failed", error=str(e))

    return _login_with_password()


def status() -> dict:
    """Human-readable auth state, for `python -m ingest_loseit check`."""
    stored = _stored()
    tok = (stored or {}).get("access_token") or settings.LOSEIT_SESSION_COOKIE
    exp = jwt_expiry(tok) if tok else None
    return {
        "has_stored_token": bool(stored and stored.get("access_token")),
        "has_refresh_token": bool(stored and stored.get("refresh_token")),
        "has_credentials": bool(settings.LOSEIT_EMAIL and settings.LOSEIT_PASSWORD),
        "expires_at": exp.isoformat() if exp else None,
        "days_left": round((exp - datetime.now(UTC)).total_seconds() / 86400, 1)
        if exp else None,
        "self_sustaining": bool(
            (stored and stored.get("refresh_token"))
            or (settings.LOSEIT_EMAIL and settings.LOSEIT_PASSWORD)
        ),
    }

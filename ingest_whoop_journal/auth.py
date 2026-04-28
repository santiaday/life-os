"""AWS Cognito password-flow auth for Whoop's private journal API.

Whoop's mobile app authenticates against their public Cognito user pool via
the standard USER_PASSWORD_AUTH flow. We do the same with boto3, then cache
the resulting AccessToken (24h) and RefreshToken (30d) in oauth_tokens
under service='whoop_private'. RefreshToken-flow is used for subsequent
fetches; only when the refresh token expires (rare) do we fall back to
password login.

Pool + client id are public identifiers extracted from the iOS app and are
the same for every Whoop user — they live in env vars only so they can
rotate without a code change.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from lifeos_core import oauth_store
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

SERVICE = "whoop_private"
ACCESS_TTL_BUFFER_MIN = 5


class WhoopJournalAuthError(RuntimeError):
    pass


def _cognito_client():
    return boto3.client("cognito-idp", region_name=settings.WHOOP_COGNITO_REGION)


def _mask(t: str | None) -> str:
    if not t:
        return "<empty>"
    return f"{t[:6]}…{t[-4:]} ({len(t)} chars)"


def login_with_password() -> dict:
    """Initial bootstrap: trade email+password for tokens. Persists both."""
    if not settings.WHOOP_PRIVATE_EMAIL or not settings.WHOOP_PRIVATE_PASSWORD:
        raise WhoopJournalAuthError(
            "WHOOP_PRIVATE_EMAIL and WHOOP_PRIVATE_PASSWORD must be set in .env"
        )
    log.info("whoop_journal.auth.password_login")
    try:
        resp = _cognito_client().initiate_auth(
            ClientId=settings.WHOOP_COGNITO_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": settings.WHOOP_PRIVATE_EMAIL,
                "PASSWORD": settings.WHOOP_PRIVATE_PASSWORD,
            },
        )
    except ClientError as e:
        raise WhoopJournalAuthError(f"Cognito password login failed: {e}") from e

    return _persist(resp.get("AuthenticationResult") or {})


def refresh_access_token() -> str:
    """Return a usable access token. Refresh via RefreshToken if expired,
    fall back to password login if refresh is rejected."""
    stored = oauth_store.load(SERVICE)
    now = datetime.now(timezone.utc)

    if (
        stored
        and stored.get("access_token")
        and stored.get("expires_at")
        and stored["expires_at"] > now + timedelta(minutes=ACCESS_TTL_BUFFER_MIN)
    ):
        return stored["access_token"]

    if stored and stored.get("refresh_token"):
        try:
            log.info("whoop_journal.auth.refresh", refresh=_mask(stored["refresh_token"]))
            resp = _cognito_client().initiate_auth(
                ClientId=settings.WHOOP_COGNITO_CLIENT_ID,
                AuthFlow="REFRESH_TOKEN_AUTH",
                AuthParameters={"REFRESH_TOKEN": stored["refresh_token"]},
            )
            result = resp.get("AuthenticationResult") or {}
            # Refresh-token flow doesn't return a new RefreshToken; carry
            # the old one forward.
            result["RefreshToken"] = result.get("RefreshToken") or stored["refresh_token"]
            return _persist(result)["AccessToken"]
        except ClientError as e:
            log.warning("whoop_journal.auth.refresh_failed_falling_back",
                        error=str(e)[:200])

    return login_with_password()["AccessToken"]


def _persist(auth_result: dict) -> dict:
    access = auth_result.get("AccessToken")
    refresh = auth_result.get("RefreshToken")
    expires_in = int(auth_result.get("ExpiresIn") or 86400)
    if not access or not refresh:
        raise WhoopJournalAuthError(
            f"Cognito response missing tokens. Keys: {sorted(auth_result.keys())}"
        )
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    oauth_store.save(  # type: ignore[arg-type]
        SERVICE,
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
    )
    log.info(
        "whoop_journal.auth.saved",
        access=_mask(access),
        refresh=_mask(refresh),
        expires_at=expires_at.isoformat(),
    )
    return auth_result

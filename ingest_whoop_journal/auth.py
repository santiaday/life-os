"""AWS Cognito refresh-token-only auth for Whoop's private journal API.

Whoop's mobile/web Cognito client (37365lrcda1js3fapqfe2n40eh) is configured
as a CONFIDENTIAL client — it requires a SECRET_HASH on password-flow calls,
and the client_secret is not publicly known. Reverse-engineering the iOS
binary to extract it is dicey and brittle.

Workaround (same as our Copilot auth): capture a long-lived refresh token
once from your Whoop browser session, store it in oauth_tokens.whoop_private,
then exchange via REFRESH_TOKEN_AUTH on every subsequent call. REFRESH_TOKEN_AUTH
typically doesn't enforce SECRET_HASH the way password-flow does — if your
server-side investigation finds it does, run `--with-secret-hash` once after
adding WHOOP_COGNITO_CLIENT_SECRET to .env.

How to capture the refresh token (3 minutes in the browser):
  1. Open https://app.prod.whoop.com in Chrome/Safari, sign in.
  2. DevTools → Application tab → Storage → Local Storage → app.prod.whoop.com.
  3. Look for a key like `CognitoIdentityServiceProvider.<client_id>.<sub>.refreshToken`.
     Value is the refresh token (a very long string).
  4. Run: `python -m ingest_whoop_journal set-refresh-token --token <paste>`

Whoop's REFRESH_TOKEN_AUTH typically returns rotated AccessToken (24h ttl)
without rotating the RefreshToken itself, so this captured token can power
ingestion for weeks/months until Whoop revokes it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
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


def _secret_hash(username: str) -> str | None:
    """Compute Cognito SECRET_HASH if the client_secret is configured.
    Returns None if no secret is configured."""
    secret = getattr(settings, "WHOOP_COGNITO_CLIENT_SECRET", None)
    if not secret:
        return None
    msg = (username + settings.WHOOP_COGNITO_CLIENT_ID).encode("utf-8")
    digest = _hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def store_refresh_token(refresh_token: str) -> None:
    """One-time bootstrap. Capture from your Whoop browser session and paste."""
    if not refresh_token or len(refresh_token) < 50:
        raise ValueError(
            "Refresh token doesn't look right. Expected a long string from "
            "your Whoop browser session's localStorage. Got: "
            f"{_mask(refresh_token)}"
        )
    oauth_store.save(  # type: ignore[arg-type]
        SERVICE, refresh_token=refresh_token, access_token=None, expires_at=None
    )
    log.info("whoop_journal.auth.refresh_token_stored", token=_mask(refresh_token))


def refresh_access_token() -> str:
    """Return a usable access token. Exchange the captured refresh token via
    REFRESH_TOKEN_AUTH. Persists any new tokens Cognito hands back."""
    stored = oauth_store.load(SERVICE)
    if stored is None or not stored.get("refresh_token"):
        raise WhoopJournalAuthError(
            "No Whoop journal refresh token stored. Capture one from your "
            "Whoop browser session (https://app.prod.whoop.com → DevTools → "
            "Local Storage → CognitoIdentityServiceProvider.*.refreshToken) "
            "and run: python -m ingest_whoop_journal set-refresh-token --token <...>"
        )

    now = datetime.now(timezone.utc)
    if (
        stored.get("access_token")
        and stored.get("expires_at")
        and stored["expires_at"] > now + timedelta(minutes=ACCESS_TTL_BUFFER_MIN)
    ):
        return stored["access_token"]

    log.info("whoop_journal.auth.refresh", refresh=_mask(stored["refresh_token"]))
    auth_params = {"REFRESH_TOKEN": stored["refresh_token"]}
    # Cognito uses the `sub` (the original token's user id) as the username
    # for SECRET_HASH on REFRESH_TOKEN_AUTH. We don't know the sub without
    # a prior login, so we try without first; if it fails with a SECRET_HASH
    # complaint and the user has provided WHOOP_COGNITO_CLIENT_SECRET we
    # retry using their email as the SECRET_HASH input (Whoop's convention,
    # observed in practice).
    try:
        resp = _cognito_client().initiate_auth(
            ClientId=settings.WHOOP_COGNITO_CLIENT_ID,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters=auth_params,
        )
    except ClientError as e:
        msg = str(e)
        if "SECRET_HASH" in msg and settings.WHOOP_PRIVATE_EMAIL:
            sh = _secret_hash(settings.WHOOP_PRIVATE_EMAIL)
            if sh is None:
                raise WhoopJournalAuthError(
                    "Cognito requires SECRET_HASH on refresh-token flow but "
                    "WHOOP_COGNITO_CLIENT_SECRET is not set in .env. The "
                    "client secret isn't publicly published — to obtain it "
                    "you'd need to extract it from the Whoop iOS app binary."
                ) from e
            auth_params["SECRET_HASH"] = sh
            try:
                resp = _cognito_client().initiate_auth(
                    ClientId=settings.WHOOP_COGNITO_CLIENT_ID,
                    AuthFlow="REFRESH_TOKEN_AUTH",
                    AuthParameters=auth_params,
                )
            except ClientError as e2:
                raise WhoopJournalAuthError(
                    f"Cognito refresh failed even with SECRET_HASH: {e2}"
                ) from e2
        else:
            raise WhoopJournalAuthError(
                f"Cognito refresh failed: {msg}. If your captured refresh "
                f"token is older than ~30 days it may have rotated out — "
                f"recapture from your Whoop browser session."
            ) from e

    result = resp.get("AuthenticationResult") or {}
    access = result.get("AccessToken")
    if not access:
        raise WhoopJournalAuthError(
            f"Cognito refresh response missing AccessToken. Keys: {sorted(result.keys())}"
        )
    # Refresh-token flow doesn't return a new RefreshToken; carry the captured
    # one forward so we keep using it until it expires.
    new_refresh = result.get("RefreshToken") or stored["refresh_token"]
    expires_at = now + timedelta(seconds=int(result.get("ExpiresIn") or 86400))

    oauth_store.save(  # type: ignore[arg-type]
        SERVICE, access_token=access, refresh_token=new_refresh, expires_at=expires_at,
    )
    log.info(
        "whoop_journal.auth.refreshed",
        access=_mask(access),
        new_refresh=_mask(new_refresh),
        expires_at=expires_at.isoformat(),
    )
    return access

"""Server-side Whoop private-API auth via Whoop's Cognito proxy.

Whoop's iOS app authenticates against its own proxy at
``/auth-service/v3/whoop/`` rather than AWS Cognito directly. The proxy fills in
the real Cognito ClientId + SECRET_HASH server-side, so we send ``ClientId: ""``
and never need the iOS app's client secret. The ONE thing that matters is
impersonating the iOS AWS-Swift-SDK request headers — that's what gets us past
Cloudflare. With those headers a plain server (no iPhone) can both log in and
refresh.

This replaces the old iPhone-Shortcut bridge: previously the repo believed
Cloudflare hard-blocked servers from auth-service and that a SECRET_HASH was
required (see ingest_whoop_journal/RUNBOOK.md). Neither is true with the right
headers. Reverse-engineered from the `briangaoo/totem` MCP's cognito.ts.

Flow:
  1. ``bootstrap_login`` — USER_PASSWORD_AUTH (+ SMS/EMAIL/TOTP MFA challenge).
     One-time, interactive (needs the MFA code). Returns access + refresh tokens.
  2. ``refresh_session`` — REFRESH_TOKEN_AUTH. No MFA. Mints a fresh access
     token from the long-lived (~30 day) refresh token. Runs unattended.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from lifeos_core.logging import get_logger

log = get_logger(__name__)

ENDPOINT = "https://api.prod.whoop.com/auth-service/v3/whoop/"
# The iOS AWS-Swift-SDK user agent. This (plus the x-amz-* headers) is what
# passes Cloudflare — a generic client gets blocked.
USER_AGENT = (
    "aws-sdk-swift/1.5.86 ua/2.1 api/cognito_identity_provider#1.5.86 "
    "os/ios#26.3.1 lang/swift#5.10 m/D,N,Z,b"
)
TIMEOUT = 30.0

_MFA_CODE_KEY = {
    "SMS_MFA": "SMS_MFA_CODE",
    "SOFTWARE_TOKEN_MFA": "SOFTWARE_TOKEN_MFA_CODE",
    "EMAIL_OTP": "EMAIL_OTP_CODE",
}


class WhoopCognitoError(RuntimeError):
    """A Cognito InitiateAuth / RespondToAuthChallenge call failed."""


def decode_jwt_exp(jwt: str) -> int:
    """Return the `exp` (epoch seconds) from a JWT's payload, or 0 if absent."""
    parts = jwt.split(".")
    if len(parts) < 2:
        return 0
    payload = parts[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)  # pad to a multiple of 4
    try:
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return int(data.get("exp", 0))
    except (ValueError, json.JSONDecodeError):
        return 0


def _headers() -> dict[str, str]:
    return {
        "content-type": "application/x-amz-json-1.1",
        "amz-sdk-request": "attempt=1; max=1",
        "amz-sdk-invocation-id": str(uuid.uuid4()),
        "user-agent": USER_AGENT,
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br",
        "accept-language": "en-US,en;q=0.9",
    }


def _call(target: str, body: dict) -> dict:
    """POST one Cognito action (InitiateAuth | RespondToAuthChallenge)."""
    headers = _headers()
    headers["x-amz-target"] = f"AWSCognitoIdentityProviderService.{target}"
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(ENDPOINT, headers=headers, content=json.dumps(body))
    if resp.status_code >= 400:
        detail = resp.text[:300]
        try:
            j = resp.json()
            detail = f"{j.get('__type', 'error')}: {j.get('message', detail)}"
        except ValueError:
            pass
        raise WhoopCognitoError(f"Cognito {target} failed ({resp.status_code}): {detail}")
    try:
        return resp.json()
    except ValueError as e:
        raise WhoopCognitoError(f"Cognito {target} returned non-JSON") from e


def _tokens_from_auth(ar: dict, *, fallback_refresh: str | None = None) -> dict:
    access = ar["AccessToken"]
    exp = decode_jwt_exp(access)
    return {
        "access_token": access,
        # The refresh-token flow omits RefreshToken — keep reusing the old one.
        "refresh_token": ar.get("RefreshToken") or fallback_refresh or "",
        "id_token": ar.get("IdToken"),
        "expires_at": datetime.fromtimestamp(exp, tz=UTC) if exp else None,
    }


def bootstrap_login(
    email: str,
    password: str,
    mfa_prompt: Callable[[str], str],
) -> dict:
    """Interactive one-time login. Returns a token bundle dict
    {access_token, refresh_token, id_token, expires_at}.

    mfa_prompt(challenge_name) is called when an MFA challenge fires and must
    return the code the user received (SMS/email/authenticator). Not called if
    the account has no MFA.
    """
    init = _call(
        "InitiateAuth",
        {
            "AuthFlow": "USER_PASSWORD_AUTH",
            "AuthParameters": {"USERNAME": email, "PASSWORD": password},
            "ClientId": "",
        },
    )
    if init.get("AuthenticationResult"):
        return _tokens_from_auth(init["AuthenticationResult"])

    challenge = init.get("ChallengeName")
    if challenge in _MFA_CODE_KEY:
        session = init.get("Session")
        if not session:
            raise WhoopCognitoError("MFA challenge missing Session token")
        code = mfa_prompt(challenge).strip()
        resp = _call(
            "RespondToAuthChallenge",
            {
                "ClientId": "",
                "ChallengeName": challenge,
                "Session": session,
                "ChallengeResponses": {
                    "USERNAME": email,
                    _MFA_CODE_KEY[challenge]: code,
                },
            },
        )
        ar = resp.get("AuthenticationResult")
        if not ar:
            raise WhoopCognitoError(f"MFA did not return tokens: {json.dumps(resp)[:200]}")
        return _tokens_from_auth(ar)

    raise WhoopCognitoError(f"Unexpected Cognito challenge: {challenge or '<none>'}")


def refresh_session(refresh_token: str) -> dict:
    """Mint a fresh access token from the refresh token (no MFA). Returns the
    same token-bundle dict. Cognito usually does NOT rotate the refresh token on
    this flow, so the returned refresh_token falls back to the one passed in."""
    resp = _call(
        "InitiateAuth",
        {
            "AuthFlow": "REFRESH_TOKEN_AUTH",
            "AuthParameters": {"REFRESH_TOKEN": refresh_token},
            "ClientId": "",
        },
    )
    ar = resp.get("AuthenticationResult")
    if not ar:
        raise WhoopCognitoError(f"Refresh did not return tokens: {json.dumps(resp)[:200]}")
    return _tokens_from_auth(ar, fallback_refresh=refresh_token)

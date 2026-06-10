"""Bearer-token auth + per-user resolution for the lifelog HTTP surface.

Single shared token today; per-user JWT later. Generate the token once:

    python -c "import secrets; print(secrets.token_urlsafe(32))"

Store in .env as LIFELOG_API_TOKEN and copy to the iOS app's Keychain.

The dependency `current_user_id` is what every route should use to scope
its DB queries. Today it always returns `settings.LIFELOG_USER_ID` (i.e.
'santi'). When we move to multi-tenant, swap this for a JWT subject
extractor — schema already carries user_id on every row, so routes don't
change.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Depends, Header, HTTPException, status

from lifeos_core.settings import settings


class _TokenStore:
    """Indirection so missing-token handling is deferred to first request,
    not import time. Lets the rest of LifeOS import cleanly when
    LIFELOG_API_TOKEN isn't configured (e.g. local dev where you just want
    MCP)."""

    @staticmethod
    def get() -> str | None:
        return os.environ.get("LIFELOG_API_TOKEN")


async def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = _TokenStore.get()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LIFELOG_API_TOKEN is not configured on the server.",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )
    token = authorization[len("Bearer "):]
    # Constant-time compare so a token-byte oracle leaks nothing.
    if not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
        )


async def current_user_id(_: None = Depends(require_token)) -> str:
    """Resolve the bearer to a user_id. Single-tenant today (always
    returns the configured LIFELOG_USER_ID); multi-tenant rewrite swaps
    this for a JWT-subject lookup or a tokens-table query."""
    return settings.LIFELOG_USER_ID

"""Bearer-token auth for the lifelog HTTP surface.

Single shared token. Generate once with:

    python -c "import secrets; print(secrets.token_urlsafe(32))"

Store in .env as LIFELOG_API_TOKEN and copy to the iOS app's Keychain.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, status


class _TokenStore:
    """Indirection so missing-token handling deferred to first request, not
    import time. Lets the rest of LifeOS import cleanly when LIFELOG_API_TOKEN
    isn't configured (e.g. local dev where you just want MCP)."""

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

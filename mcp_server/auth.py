"""Bearer-token auth for the MCP HTTP server.

Single static API key in `Authorization: Bearer <MCP_API_KEY>`. Compared with
constant-time `hmac.compare_digest` so a timing oracle can't extract bytes.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from lifeos_core.settings import settings

# Health check is exposed unauthenticated so an external uptime monitor can
# poll without holding the API key. Webhook paths use their own per-source
# signature schemes (HMAC for Whoop) so the bearer doesn't apply.
PUBLIC_PATHS = {"/health"}
PUBLIC_PREFIXES = ("/webhooks/",)


def require_bearer(request: Request) -> None:
    """FastAPI dependency. Raises 401 if Bearer token is missing or wrong."""
    path = request.url.path
    if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return

    key = settings.MCP_API_KEY
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MCP_API_KEY not configured on server",
        )

    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = auth[7:].strip()
    if not hmac.compare_digest(presented.encode(), key.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

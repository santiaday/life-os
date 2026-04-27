"""Auth for the MCP HTTP server.

Path-prefix secret. Claude.ai's custom-connector UI doesn't surface a bearer
or custom-header field, but it does take the URL verbatim — so we put the API
key in the path itself and strip it before handing the request to the MCP
ASGI app:

    https://lifeos.ledion.io/mcp/<MCP_API_KEY>/...

Compared via constant-time hmac.compare_digest. Pre-stripping is done in the
ASGI middleware in server.py so the MCP app sees a clean `/mcp/...` path
regardless of which key the request came in on.

Public paths (no auth required):
  /health        — uptime monitors
  /webhooks/*    — sources sign their own deliveries (HMAC for Whoop)
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from lifeos_core.settings import settings

PUBLIC_PATHS = {"/health"}
PUBLIC_PREFIXES = ("/webhooks/",)
MCP_MOUNT = "/mcp"


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)


def extract_and_validate_token(path: str) -> str | None:
    """If `path` is `/mcp/<key>` or `/mcp/<key>/...`, validate the key against
    MCP_API_KEY and return the *rewritten* path (with the key stripped) on
    success. Returns None on auth failure."""
    if not settings.MCP_API_KEY:
        return None
    prefix = MCP_MOUNT + "/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    # Split off the key (first path segment after /mcp/).
    if "/" in rest:
        key, tail = rest.split("/", 1)
        rewritten = f"{MCP_MOUNT}/{tail}"
    else:
        key = rest
        rewritten = MCP_MOUNT
    if not hmac.compare_digest(key.encode(), settings.MCP_API_KEY.encode()):
        return None
    return rewritten


def require_bearer(request: Request) -> None:
    """Backwards-compat name. Real check is "URL contains valid path-secret"
    handled by the middleware in server.py — this just rejects anything that
    fell through without auth."""
    if is_public(request.url.path):
        return
    # The middleware in server.py rewrites the path on success, so by the time
    # this runs the path-secret has already been validated. Anything still
    # under /mcp without the prefix-key is unauthenticated.
    if request.url.path.startswith(MCP_MOUNT):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized — use the secret-path URL.",
    )

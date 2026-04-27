"""Auth for the MCP HTTP server.

Cloudflare Access sits in front of the public hostname. CF authenticates the
user via Google SSO, then injects a signed JWT in the `Cf-Access-Jwt-Assertion`
header on every request that survives the access policy.

We don't need to verify the JWT signature ourselves — Cloudflare won't let any
request through without one — but we do reject requests that lack the header
entirely, since that means traffic bypassed Cloudflare (e.g. someone hitting
the droplet's IP directly).

Public paths (no auth required):
  /health        — uptime monitors
  /webhooks/*    — sources sign their own deliveries (HMAC for Whoop)
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

PUBLIC_PATHS = {"/health"}
PUBLIC_PREFIXES = ("/webhooks/",)

CF_ACCESS_JWT_HEADER = "cf-access-jwt-assertion"


def require_bearer(request: Request) -> None:
    """FastAPI dependency. Raises 401 if the request didn't come through
    Cloudflare Access. Name preserved for backwards compatibility with the
    existing middleware in server.py — the actual check is "Cloudflare JWT
    present", not bearer."""
    path = request.url.path
    if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return

    if not request.headers.get(CF_ACCESS_JWT_HEADER):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Missing Cf-Access-Jwt-Assertion header. "
                "All non-webhook traffic must arrive via Cloudflare Access."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )

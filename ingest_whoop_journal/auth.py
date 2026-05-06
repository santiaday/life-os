"""Read-only token broker for the Whoop private journal API.

iPhone-bridge architecture: the iPhone Shortcut is the only thing that talks
to Whoop's auth-service. It runs REFRESH_TOKEN_AUTH on a schedule, gets fresh
tokens back, and POSTs them to our refresh-callback webhook
(``/lifelog/whoop/refresh-callback``). The webhook persists them to
``oauth_tokens(service='whoop_private')``.

This module is the *consumer* side. It only reads from ``oauth_tokens`` and
hands the bearer token to the ingester. It must NEVER:

  - call cognito-idp.us-west-2.amazonaws.com (we don't have SECRET_HASH)
  - POST to api.prod.whoop.com/auth-service/* (Cloudflare hard-blocks servers)
  - prompt for or store a password
  - run a refresh flow

If the token is past its ``expires_at`` (with a small skew buffer),
``ensure_fresh`` raises ``WhoopAuthExpired`` and the operator's job is to
re-trigger the iPhone Shortcut (or run ``bootstrap_from_capture`` again).

The ``save_tokens`` helper is the only writer in this module — used by the
refresh webhook and the bootstrap CLI. It writes all five columns
(access_token, refresh_token, id_token, expires_at, metadata) so we don't
silently null any of them out on subsequent saves.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from lifeos_core.db import tx
from lifeos_core.logging import get_logger

log = get_logger(__name__)

SERVICE = "whoop_private"
# Treat tokens as expired this far before their nominal expires_at so we never
# hand a near-dead token to a long-running ingest.
EXPIRY_SKEW = timedelta(minutes=5)


# ---- exceptions ------------------------------------------------------------
class WhoopAuthError(RuntimeError):
    """Generic auth failure (e.g. no token row at all)."""


class WhoopAuthExpired(WhoopAuthError):
    """Token row exists but is past expires_at. Re-run the iPhone Shortcut
    or bootstrap_from_capture to seed a fresh token."""


# ---- helpers ---------------------------------------------------------------
def _mask(t: str | None) -> str:
    if not t:
        return "<empty>"
    return f"{t[:6]}…{t[-4:]} ({len(t)} chars)"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- writer (used by webhook + bootstrap) ---------------------------------
def save_tokens(
    *,
    access_token: str,
    refresh_token: str,
    id_token: str | None,
    expires_at: datetime,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Persist a fresh token bundle for the whoop_private service. Five
    explicit columns so we never partial-update and silently null a sibling.

    Caller is responsible for upstream validation (JWT shape, expires_at in
    the future, refresh_token non-empty). This function only validates the
    minimum needed to keep the table consistent."""
    if not access_token or not refresh_token:
        raise ValueError("access_token and refresh_token are both required")
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    md = metadata or {}

    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO oauth_tokens
                (service, access_token, refresh_token, id_token, expires_at, metadata, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, now())
            ON CONFLICT (service) DO UPDATE SET
                access_token  = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                id_token      = EXCLUDED.id_token,
                expires_at    = EXCLUDED.expires_at,
                metadata      = EXCLUDED.metadata,
                updated_at    = now()
            """,
            [SERVICE, access_token, refresh_token, id_token, expires_at, json.dumps(md)],
        )
    log.info(
        "whoop_journal.auth.tokens_saved",
        access=_mask(access_token),
        refresh=_mask(refresh_token),
        id_token=_mask(id_token),
        expires_at=expires_at.isoformat(),
        metadata=md,
    )


# ---- reader (used by the ingester) ---------------------------------------
class WhoopAuth:
    """Read-only auth helper. Loads tokens from ``oauth_tokens`` and returns
    Authorization headers. Has NO refresh logic — refresh is the iPhone's job.

    Usage:
        auth = WhoopAuth()
        auth.ensure_fresh()          # raises WhoopAuthExpired if stale
        ... auth.headers() ...       # before each HTTP request

    Loads once per instance; re-instantiate if you need to pick up a fresh
    token persisted mid-run (e.g. the webhook fired during a long backfill).
    """

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._expires_at: datetime | None = None

    def _load(self) -> None:
        """Read-only load from oauth_tokens. Idempotent."""
        with tx() as c, c.cursor() as cur:
            cur.execute(
                "SELECT access_token, expires_at FROM oauth_tokens WHERE service = %s",
                [SERVICE],
            )
            row = cur.fetchone()
        if row is None or not row.get("access_token"):
            raise WhoopAuthError(
                "No Whoop token row in oauth_tokens. Bootstrap once with "
                "`python -m ingest_whoop_journal.bootstrap_from_capture <flow-file>` "
                "or wait for the iPhone Shortcut to POST a refresh."
            )
        self._access_token = row["access_token"]
        self._expires_at = row["expires_at"]

    def ensure_fresh(self) -> str:
        """Return a usable access token. Raises WhoopAuthExpired if the row
        is past expiry. Cheap when called repeatedly within one process —
        only re-reads from the DB if the in-memory copy is stale or absent.
        """
        if self._access_token is None or self._expires_at is None:
            self._load()
        assert self._expires_at is not None and self._access_token is not None
        if self._expires_at <= _now() + EXPIRY_SKEW:
            log.warning(
                "whoop_journal.auth.expired",
                expires_at=self._expires_at.isoformat(),
                now=_now().isoformat(),
            )
            raise WhoopAuthExpired(
                f"Whoop token expired at {self._expires_at.isoformat()}. "
                f"Trigger the iPhone Shortcut (or re-run bootstrap_from_capture) "
                f"to seed a fresh token."
            )
        return self._access_token

    def headers(self) -> dict:
        """Bearer header for journal-service GETs. The iOS-app x-whoop-* and
        x-amz-* headers are NOT needed on /journal-service — the gateway
        only requires a valid bearer there."""
        return {"Authorization": f"Bearer {self.ensure_fresh()}"}

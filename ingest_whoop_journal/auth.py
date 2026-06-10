"""Token broker for the Whoop private API (journal + trends + lifts).

Server-side Cognito auth (current): ``WhoopAuth.ensure_fresh`` reads the stored
token from ``oauth_tokens(service='whoop_private')`` and, when the access token
is stale, refreshes it itself via :mod:`lifeos_core.whoop_cognito` (Whoop's
Cognito proxy at ``/auth-service/v3/whoop/`` with the iOS-SDK headers — no
SECRET_HASH, no iPhone). Bootstrap once with ``python -m ingest_whoop_private
login`` (email + password + MFA); after that refresh is unattended.

This replaces the old iPhone-Shortcut bridge, which existed because the repo
believed Cloudflare hard-blocked servers from auth-service and that a Cognito
SECRET_HASH was required. With the right request headers neither holds, so the
warehouse no longer needs the iPhone to broker tokens. The
``/lifelog/whoop/refresh-callback`` webhook still works as a fallback writer.

The ``save_tokens`` helper is the writer — used by the login bootstrap, the
in-process refresh, and the refresh webhook. It writes all five columns
(access_token, refresh_token, id_token, expires_at, metadata) so we never
silently null one out on a subsequent save.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.whoop_cognito import WhoopCognitoError, refresh_session

log = get_logger(__name__)

SERVICE = "whoop_private"
# Treat tokens as expired this far before their nominal expires_at so we never
# hand a near-dead token to a long-running ingest.
EXPIRY_SKEW = timedelta(minutes=5)


# ---- exceptions ------------------------------------------------------------
class WhoopAuthError(RuntimeError):
    """Generic auth failure (e.g. no token row at all)."""


class WhoopAuthExpired(WhoopAuthError):
    """Access token is stale and couldn't be refreshed — either no refresh
    token is stored or the refresh token itself expired (~30 days). Re-run
    ``python -m ingest_whoop_private login`` to seed a fresh bundle."""


# ---- helpers ---------------------------------------------------------------
def _mask(t: str | None) -> str:
    if not t:
        return "<empty>"
    return f"{t[:6]}…{t[-4:]} ({len(t)} chars)"


def _now() -> datetime:
    return datetime.now(UTC)


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
        expires_at = expires_at.replace(tzinfo=UTC)
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
    """Auth helper. Loads tokens from ``oauth_tokens`` and returns Authorization
    headers, refreshing the access token server-side via Cognito when it's stale
    (see :mod:`lifeos_core.whoop_cognito`). No iPhone required.

    Usage:
        auth = WhoopAuth()
        auth.ensure_fresh()          # refreshes if stale; raises only if it can't
        ... auth.headers() ...       # before each HTTP request

    Caches in memory per instance and persists any refreshed token back to the
    DB, so a long backfill refreshes once and re-instantiation picks it up.
    """

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: datetime | None = None

    def _load(self) -> None:
        """Load the token bundle from oauth_tokens. Idempotent."""
        with tx() as c, c.cursor() as cur:
            cur.execute(
                "SELECT access_token, refresh_token, expires_at "
                "FROM oauth_tokens WHERE service = %s",
                [SERVICE],
            )
            row = cur.fetchone()
        if row is None or not row.get("access_token"):
            raise WhoopAuthError(
                "No Whoop token row in oauth_tokens. Bootstrap once with "
                "`python -m ingest_whoop_private login` (email + password + MFA)."
            )
        self._access_token = row["access_token"]
        self._refresh_token = row.get("refresh_token")
        self._expires_at = row["expires_at"]

    def ensure_fresh(self) -> str:
        """Return a usable access token, refreshing server-side if the stored
        one is within the skew window of expiry. Raises WhoopAuthExpired only
        when there's no refresh token or the refresh itself fails (refresh
        tokens last ~30 days; past that, re-run the login bootstrap)."""
        if self._access_token is None or self._expires_at is None:
            self._load()
        assert self._expires_at is not None and self._access_token is not None
        if self._expires_at > _now() + EXPIRY_SKEW:
            return self._access_token
        return self._refresh()

    def _refresh(self) -> str:
        """Mint a fresh access token via Cognito and persist it."""
        if not self._refresh_token:
            raise WhoopAuthExpired(
                "Whoop access token is stale and no refresh token is stored. "
                "Run `python -m ingest_whoop_private login` to re-authenticate."
            )
        try:
            bundle = refresh_session(self._refresh_token)
        except WhoopCognitoError as e:
            log.warning("whoop.auth.refresh_failed", error=str(e))
            raise WhoopAuthExpired(
                f"Whoop token refresh failed: {e}. The refresh token may be "
                f"expired (~30 days) — run `python -m ingest_whoop_private login`."
            ) from e

        access = bundle["access_token"]
        refresh = bundle["refresh_token"] or self._refresh_token
        expires_at = bundle["expires_at"] or (_now() + timedelta(hours=23))
        save_tokens(
            access_token=access,
            refresh_token=refresh,
            id_token=bundle.get("id_token"),
            expires_at=expires_at,
            metadata={"source": "server_cognito_refresh"},
        )
        self._access_token = access
        self._refresh_token = refresh
        self._expires_at = expires_at
        log.info("whoop.auth.refreshed", expires_at=expires_at.isoformat())
        return access

    def headers(self) -> dict:
        """Bearer header for journal-service GETs. The iOS-app x-whoop-* and
        x-amz-* headers are NOT needed on /journal-service — the gateway
        only requires a valid bearer there."""
        return {"Authorization": f"Bearer {self.ensure_fresh()}"}

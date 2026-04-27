"""Persistent OAuth token storage.

Refresh tokens rotate on every use for some providers (Whoop, Google), so we
can't trust .env. The `oauth_tokens` table is the source of truth.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from lifeos_core.db import tx

Service = Literal["whoop", "google", "copilot"]


def load(service: Service) -> dict | None:
    """Return {access_token, refresh_token, expires_at} or None if not set up."""
    with tx() as c, c.cursor() as cur:
        cur.execute(
            "SELECT access_token, refresh_token, expires_at FROM oauth_tokens WHERE service = %s",
            [service],
        )
        return cur.fetchone()


def save(
    service: Service,
    *,
    refresh_token: str,
    access_token: str | None = None,
    expires_at: datetime | None = None,
) -> None:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO oauth_tokens (service, access_token, refresh_token, expires_at, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (service) DO UPDATE SET
              access_token = EXCLUDED.access_token,
              refresh_token = EXCLUDED.refresh_token,
              expires_at = EXCLUDED.expires_at,
              updated_at = now()
            """,
            [service, access_token, refresh_token, expires_at],
        )

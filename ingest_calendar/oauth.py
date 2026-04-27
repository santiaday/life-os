"""Google Calendar OAuth bootstrap and refresh.

Same pattern as ingest_whoop.oauth, using google-auth + google-auth-oauthlib.
Refresh tokens are persisted to oauth_tokens (Google may rotate them).

Reference:
  https://developers.google.com/identity/protocols/oauth2/web-server
  https://developers.google.com/calendar/api/quickstart/python
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from lifeos_core import oauth_store
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_URL = "https://oauth2.googleapis.com/token"


def _client_config() -> dict:
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise RuntimeError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env")
    return {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URL,
            "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
        }
    }


def authorize_url(state: str = "lifeos-bootstrap") -> str:
    """URL to visit for one-time consent. `prompt=consent` ensures Google
    issues a refresh_token (it omits one on subsequent grants otherwise).

    PKCE is explicitly disabled. We're a confidential client (the server holds
    GOOGLE_CLIENT_SECRET); PKCE is for public clients that can't keep a secret.
    Recent google-auth-oauthlib versions auto-enable PKCE which breaks our
    two-process bootstrap (the verifier from `oauth-init` is gone by the time
    `oauth-exchange` runs)."""
    flow = Flow.from_client_config(
        _client_config(), scopes=SCOPES, autogenerate_code_verifier=False,
    )
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return url


def exchange_code(code: str) -> dict:
    flow = Flow.from_client_config(
        _client_config(), scopes=SCOPES, autogenerate_code_verifier=False,
    )
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    flow.fetch_token(code=code)
    creds: Credentials = flow.credentials
    if not creds.refresh_token:
        raise RuntimeError(
            "Google didn't return a refresh_token. "
            "Revoke the app at https://myaccount.google.com/permissions and re-run "
            "oauth-init — it only issues one on first consent."
        )
    oauth_store.save(
        "google",
        access_token=creds.token,
        refresh_token=creds.refresh_token,
        expires_at=_aware(creds.expiry),
    )
    log.info("calendar.oauth.exchange_ok", expires_at=str(creds.expiry))
    return {"refresh_token": creds.refresh_token, "expires_at": creds.expiry}


def credentials() -> Credentials:
    """Return a Credentials object usable by googleapiclient.discovery.build.
    Refreshes if expired and persists the new access token."""
    stored = oauth_store.load("google")
    if stored is None:
        raise RuntimeError(
            "No Google refresh token in oauth_tokens. "
            "Run `python -m ingest_calendar oauth-init` first."
        )

    creds = Credentials(
        token=stored.get("access_token"),
        refresh_token=stored["refresh_token"],
        token_uri=TOKEN_URL,
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
        expiry=_naive(stored.get("expires_at")),
    )

    if not creds.valid:
        log.info("calendar.oauth.refresh")
        creds.refresh(Request())
        oauth_store.save(
            "google",
            access_token=creds.token,
            refresh_token=creds.refresh_token or stored["refresh_token"],
            expires_at=_aware(creds.expiry),
        )

    return creds


def _aware(dt: datetime | None) -> datetime | None:
    """Google's library returns naive expiry; we store tz-aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _naive(dt: datetime | None) -> datetime | None:
    """Google's library wants naive UTC for `expiry`."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

"""Single source of truth for runtime config.

Settings load from process env (which docker-compose populates from .env).
Service-specific settings can extend `Settings` if they need extra fields, but
in practice everything fits comfortably here for a personal monorepo.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ---- DB ----------------------------------------------------------------
    # Not strictly required at import-time so unit tests / local lint runs can
    # import lifeos_core without a configured environment. db.pool() raises a
    # clear error if it's empty when actually used.
    SUPABASE_DB_URL: str = Field(
        default="", description="Pooled (port 6543, transaction mode) connection URL."
    )
    SUPABASE_DB_URL_DIRECT: str | None = Field(
        default=None,
        description="Direct (port 5432, session mode) URL. Required for migrations.",
    )
    MCP_DB_PASSWORD: str | None = Field(
        default=None, description="Password for the lifeos_mcp read-only role."
    )
    LIFEOS_READER_DB_URL: str | None = Field(
        default=None, description="Read-only DSN used by ask_sql."
    )
    LOCAL_TZ: str = "America/New_York"

    # ---- Whoop -------------------------------------------------------------
    WHOOP_CLIENT_ID: str | None = None
    WHOOP_CLIENT_SECRET: str | None = None
    WHOOP_REDIRECT_URI: str | None = None
    WHOOP_WEBHOOK_SECRET: str | None = None

    # ---- Whoop private journal API (iPhone-bridge architecture) -------------
    # The iPhone Shortcut runs REFRESH_TOKEN_AUTH against Whoop's auth-service
    # and POSTs fresh tokens to /lifelog/whoop/refresh-callback. The server
    # never talks to Whoop's auth-service or Cognito directly — Cloudflare
    # blocks one and we don't have the client_secret for the other.
    # See ingest_whoop_journal/RUNBOOK.md.
    #
    # Shared secret: 32-byte url-safe random, set on both this server and
    # the iOS Shortcut's "Get Contents of URL → Headers" config. Generate:
    #   python -c "import secrets; print(secrets.token_urlsafe(32))"
    WHOOP_REFRESH_WEBHOOK_SECRET: str | None = None
    # Legacy fields (unused now). Kept so existing .env files don't trip
    # pydantic-settings on first run after the rewrite.
    WHOOP_PRIVATE_EMAIL: str | None = None
    WHOOP_PRIVATE_PASSWORD: str | None = None
    WHOOP_COGNITO_REGION: str = "us-west-2"
    WHOOP_COGNITO_USER_POOL: str = "us-west-2_rYv1jhSC3"
    WHOOP_COGNITO_CLIENT_ID: str = "37365lrcda1js3fapqfe2n40eh"
    WHOOP_COGNITO_CLIENT_SECRET: str | None = None
    WHOOP_IOS_VERSION: str = "5.49.2"

    # ---- Google Calendar ---------------------------------------------------
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GOOGLE_REDIRECT_URI: str | None = None
    GOOGLE_CALENDAR_IDS: str = "primary"  # comma-separated
    INTERNAL_EMAIL_DOMAINS: str = ""  # comma-separated

    # ---- Lifelog calendar sync --------------------------------------------
    # JSON map from event.category → Google Calendar id. Calendars must be
    # created manually in Google Calendar UI; ids look like
    # "abc123@group.calendar.google.com" or "primary".
    # Example:
    #   {"Sleep":"abc@...","Workout":"abc@...",
    #    "DoorLoop work":"def@...","Personal work":"ghi@..."}
    LIFELOG_CALENDAR_MAP_JSON: str = "{}"
    LIFELOG_SYNC_BATCH_SIZE: int = 200
    # If True, calendar events render as "free" rather than blocking.
    LIFELOG_EVENTS_TRANSPARENT: bool = True

    # ---- Cronometer --------------------------------------------------------
    CRONOMETER_USERNAME: str | None = None
    CRONOMETER_PASSWORD: str | None = None

    # ---- Hevy --------------------------------------------------------------
    # Static API key from the Hevy mobile app's Settings → Developer (Pro plan).
    # Hevy's API uses a long-lived key — no OAuth — so we read it straight from
    # env rather than oauth_tokens.
    HEVY_API_KEY: str | None = None

    # ---- Copilot -----------------------------------------------------------
    COPILOT_EMAIL: str | None = None
    COPILOT_PASSWORD: str | None = None

    # ---- MCP ---------------------------------------------------------------
    MCP_API_KEY: str | None = None
    MCP_PUBLIC_BASE_URL: str | None = None
    MCP_BIND_HOST: str = "0.0.0.0"
    MCP_BIND_PORT: int = 8080

    # ---- Lifelog iOS app ---------------------------------------------------
    # Bearer token shared with the iOS app (stored there in the Keychain).
    # Generate once: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
    # The iOS app calls /lifelog/* endpoints with `Authorization: Bearer <token>`.
    # Auth is enforced per-route in lifelog_api.auth — Settings is just a doc
    # entry here; lifelog_api.auth reads os.environ directly so the token can
    # be rotated without bouncing the whole settings cache.
    LIFELOG_API_TOKEN: str | None = None

    # ---- Observability -----------------------------------------------------
    LOG_LEVEL: str = "INFO"
    SENTRY_DSN: str | None = None
    # Optional OTLP exporter for MCP tool spans. Compatible with Langfuse
    # Cloud (free tier) and Grafana Cloud (free tier). Leave empty to disable.
    # The Postgres-side mcp_tool_log table works regardless.
    LIFEOS_OTLP_ENDPOINT: str | None = None
    LIFEOS_OTLP_HEADERS: str | None = None  # 'k=v,k2=v2' for Authorization etc.
    LIFEOS_SERVICE_NAME: str = "life-os-mcp"

    # ---- Couples-split workflow ---------------------------------------------
    # Tag names used to mark who owns a transaction. Case-insensitive on read.
    COUPLE_TAG_ME: str = "me"
    COUPLE_TAG_PARTNER: str = "paulina"
    COUPLE_TAG_JOINT: str = "joint"
    # Joint expense split. Should sum to 1.0.
    COUPLE_SPLIT_ME: float = 0.5
    COUPLE_SPLIT_PARTNER: float = 0.5
    # Comma-separated lists of dim_account.account_id values mapping each
    # account to its primary owner. Used to derive who paid for what.
    # Account ids not listed here are treated as "unknown owner".
    COUPLE_ACCOUNTS_ME: str = ""
    COUPLE_ACCOUNTS_PARTNER: str = ""
    COUPLE_ACCOUNTS_JOINT: str = ""

    # ---- Convenience -------------------------------------------------------
    @property
    def calendar_ids(self) -> list[str]:
        return [c.strip() for c in self.GOOGLE_CALENDAR_IDS.split(",") if c.strip()]

    @property
    def lifelog_calendar_map(self) -> dict[str, str]:
        """{category: google_calendar_id}. Empty dict if unconfigured —
        calendar_sync will refuse to run with a clear error rather than
        spamming events into the wrong place."""
        import json

        try:
            parsed = json.loads(self.LIFELOG_CALENDAR_MAP_JSON or "{}")
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"LIFELOG_CALENDAR_MAP_JSON is not valid JSON: {e}"
            ) from e
        if not isinstance(parsed, dict):
            raise RuntimeError("LIFELOG_CALENDAR_MAP_JSON must be a JSON object")
        return {str(k): str(v) for k, v in parsed.items()}

    @property
    def internal_email_domains(self) -> list[str]:
        return [
            d.strip().lower() for d in self.INTERNAL_EMAIL_DOMAINS.split(",") if d.strip()
        ]

    def couple_account_ownership(self) -> dict[str, str]:
        """Return {account_id: 'me'|'partner'|'joint'} from the COUPLE_ACCOUNTS_*
        env vars. Used to derive who paid for what."""
        out: dict[str, str] = {}
        for owner, raw in (
            ("me", self.COUPLE_ACCOUNTS_ME),
            ("partner", self.COUPLE_ACCOUNTS_PARTNER),
            ("joint", self.COUPLE_ACCOUNTS_JOINT),
        ):
            for aid in raw.split(","):
                aid = aid.strip()
                if aid:
                    out[aid] = owner
        return out


@lru_cache(maxsize=1)
def _load() -> Settings:
    return Settings()  # type: ignore[call-arg]


# Module-level singleton, used everywhere.
settings: Settings = _load()

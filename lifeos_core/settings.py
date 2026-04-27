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

    # ---- Google Calendar ---------------------------------------------------
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GOOGLE_REDIRECT_URI: str | None = None
    GOOGLE_CALENDAR_IDS: str = "primary"  # comma-separated
    INTERNAL_EMAIL_DOMAINS: str = ""  # comma-separated

    # ---- Cronometer --------------------------------------------------------
    CRONOMETER_USERNAME: str | None = None
    CRONOMETER_PASSWORD: str | None = None

    # ---- Copilot -----------------------------------------------------------
    COPILOT_EMAIL: str | None = None
    COPILOT_PASSWORD: str | None = None

    # ---- MCP ---------------------------------------------------------------
    MCP_API_KEY: str | None = None
    MCP_PUBLIC_BASE_URL: str | None = None
    MCP_BIND_HOST: str = "0.0.0.0"
    MCP_BIND_PORT: int = 8080

    # ---- Observability -----------------------------------------------------
    LOG_LEVEL: str = "INFO"
    SENTRY_DSN: str | None = None

    # ---- Convenience -------------------------------------------------------
    @property
    def calendar_ids(self) -> list[str]:
        return [c.strip() for c in self.GOOGLE_CALENDAR_IDS.split(",") if c.strip()]

    @property
    def internal_email_domains(self) -> list[str]:
        return [
            d.strip().lower() for d in self.INTERNAL_EMAIL_DOMAINS.split(",") if d.strip()
        ]


@lru_cache(maxsize=1)
def _load() -> Settings:
    return Settings()  # type: ignore[call-arg]


# Module-level singleton, used everywhere.
settings: Settings = _load()

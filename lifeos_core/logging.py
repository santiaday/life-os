"""structlog configuration shared across services.

Logs are JSON in production (parseable from `docker compose logs`) and
console-pretty when stdout is a TTY (local dev).
"""

from __future__ import annotations

import logging
import sys

import structlog

from lifeos_core.settings import settings

_CONFIGURED = False


def configure_logging() -> None:
    """Idempotent. Safe to call multiple times — only configures on first call."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    is_tty = sys.stdout.isatty()
    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer(colors=True)
        if is_tty
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )

    # Optional Sentry integration. Install in the same shot — it costs nothing
    # if SENTRY_DSN is empty.
    if settings.SENTRY_DSN:
        try:
            import sentry_sdk

            sentry_sdk.init(
                dsn=settings.SENTRY_DSN,
                traces_sample_rate=0.0,  # we don't need APM, just errors
                send_default_pii=False,
            )
        except ImportError:
            pass

    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)

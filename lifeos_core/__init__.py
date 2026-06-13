"""Shared utilities for life-os services.

Every ingester, the mart refresher, and the MCP server import from here.
Keep this small and dependency-light: psycopg, pydantic-settings, structlog.
"""

from lifeos_core.logging import configure_logging, get_logger
from lifeos_core.settings import settings

__all__ = ["configure_logging", "get_logger", "settings"]

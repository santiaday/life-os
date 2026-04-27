"""Subprocess wrapper around the `cronometer-export` Go binary.

The binary lives at /usr/local/bin/cronometer-export inside the container
(installed by the multi-stage Dockerfile). For local development without
Docker, install it manually or set CRONOMETER_EXPORT_BINARY env var.

Auth fragility: Cronometer's GWT API breaks when they update their app. The
binary returns non-zero in that case; we surface stderr verbatim into
ingestion_runs.error_message and let the caller decide whether to abort or
continue with other data types.
"""

from __future__ import annotations

import os
import subprocess
from datetime import date

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

DEFAULT_BINARY = "/usr/local/bin/cronometer-export"
EXPORT_TIMEOUT_SEC = 90

# data_type values accepted by the binary's `-t` flag.
DATA_TYPES = ("servings", "daily-nutrition", "exercises", "biometrics", "notes")


class ExporterError(RuntimeError):
    """Non-zero exit from the cronometer-export binary."""

    def __init__(self, returncode: int, stderr: str, cmd: list[str]) -> None:
        super().__init__(
            f"cronometer-export exited {returncode}: {stderr.strip()[:500]}"
        )
        self.returncode = returncode
        self.stderr = stderr
        self.cmd = cmd


def binary_path() -> str:
    return os.environ.get("CRONOMETER_EXPORT_BINARY", DEFAULT_BINARY)


def export(data_type: str, start: date, end: date) -> str:
    """Run `cronometer-export -t <type> -s <start> -e <end>` and return CSV.

    Returns the binary's stdout (CSV text). Raises ExporterError on non-zero exit.
    """
    if data_type not in DATA_TYPES:
        raise ValueError(f"data_type must be one of {DATA_TYPES}, got {data_type!r}")
    if not settings.CRONOMETER_USERNAME or not settings.CRONOMETER_PASSWORD:
        raise RuntimeError("CRONOMETER_USERNAME and CRONOMETER_PASSWORD must be set in .env")

    # The binary parses -s/-e as either RFC3339 timestamps or `-Nd/w/m/y`
    # shorthand. A bare ISO date (YYYY-MM-DD) silently fails with exit 1
    # and empty stderr, so we always send full RFC3339 with UTC midnight.
    cmd = [
        binary_path(),
        "-u", settings.CRONOMETER_USERNAME,
        "-p", settings.CRONOMETER_PASSWORD,
        "-t", data_type,
        "-s", f"{start.isoformat()}T00:00:00Z",
        "-e", f"{end.isoformat()}T23:59:59Z",
    ]
    log.info("cronometer.export.start", data_type=data_type, start=str(start), end=str(end))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=EXPORT_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ExporterError(-1, f"timeout after {EXPORT_TIMEOUT_SEC}s", cmd) from e
    except FileNotFoundError as e:
        raise ExporterError(-1, f"binary not found at {cmd[0]}", cmd) from e

    if result.returncode != 0:
        log.error(
            "cronometer.export.failed",
            data_type=data_type,
            returncode=result.returncode,
            stderr=result.stderr[:500],
        )
        raise ExporterError(result.returncode, result.stderr, cmd)

    log.info(
        "cronometer.export.ok",
        data_type=data_type,
        bytes=len(result.stdout),
    )
    return result.stdout

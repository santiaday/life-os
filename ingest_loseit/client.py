"""Lose It! session client.

Design note -- why this does NOT speak GWT-RPC:

Lose It's web app talks to `www.loseit.com/web/service` over GWT-RPC, and every
call has to carry a policy hash and permutation strong-name that are pinned to
the currently-deployed JS bundle. Those change on every Lose It web deploy, so
a GWT client breaks silently and without warning, several times a year.

The data-export endpoint has no such coupling. It returns the same CSV bundle
the account-settings "Export data" button produces -- which is *richer* than
the RPC surface anyway (per-item macros, not just daily totals) -- and it needs
nothing but a valid session cookie. So the ongoing sync downloads that bundle
and feeds it through the exact same loader used for the manual export
(`ingest_files.loseit`), which is idempotent by construction.

Auth: Lose It's session lives in the `liauth` cookie. It is long-lived but not
eternal. Put it in LOSEIT_SESSION_COOKIE. When it expires the export endpoint
answers with an HTML login page instead of a zip, which this client detects and
reports as `LoseItAuthError` rather than writing garbage to the warehouse.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

BASE = "https://www.loseit.com"

# Observed export entry points, tried in order. The first that returns a zip
# wins; the rest exist because Lose It has moved this path before.
EXPORT_PATHS = (
    "/export/data",
    "/export/download",
    "/account/export",
)

ZIP_MAGIC = b"PK\x03\x04"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class LoseItError(RuntimeError):
    pass


class LoseItAuthError(LoseItError):
    """Session cookie missing, expired, or rejected."""


class LoseItClient:
    def __init__(self, session_cookie: str | None = None,
                 timeout: float = 120.0) -> None:
        # Read through settings, not os.environ: every other service in this
        # repo does, and pydantic-settings is what loads .env when the process
        # wasn't started by docker-compose.
        self.session_cookie = session_cookie or settings.LOSEIT_SESSION_COOKIE
        if not self.session_cookie:
            raise LoseItAuthError(
                "LOSEIT_SESSION_COOKIE is not set. Sign in at loseit.com, copy the "
                "value of the `liauth` cookie from your browser's dev tools, and put "
                "it in .env as LOSEIT_SESSION_COOKIE."
            )
        self._client = httpx.Client(
            base_url=BASE,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            cookies={"liauth": self.session_cookie},
        )

    def __enter__(self) -> LoseItClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch_export(self) -> bytes:
        """Download the CSV export bundle. Returns raw zip bytes."""
        errors: list[str] = []
        for path in EXPORT_PATHS:
            try:
                resp = self._client.get(path)
            except httpx.HTTPError as e:
                errors.append(f"{path}: {type(e).__name__}: {e}")
                continue
            if resp.status_code != 200:
                errors.append(f"{path}: HTTP {resp.status_code}")
                continue
            body = resp.content
            if body[:4] == ZIP_MAGIC:
                log.info("loseit.export.ok", path=path, bytes=len(body))
                return body
            # A login page means the cookie is dead. Say so plainly rather
            # than letting an HTML blob reach the CSV parser.
            head = body[:400].decode("utf-8", "replace").lower()
            if "login" in head or "sign in" in head or "<!doctype html" in head:
                raise LoseItAuthError(
                    f"{path} returned an HTML page, not a zip -- the LOSEIT_SESSION_COOKIE "
                    f"has expired. Re-copy the `liauth` cookie from loseit.com."
                )
            errors.append(f"{path}: unexpected content-type "
                          f"{resp.headers.get('content-type')!r}")
        raise LoseItError("no export endpoint returned a zip: " + "; ".join(errors))

    def download_to(self, target_dir: str | Path) -> Path:
        """Fetch and unzip the export into `target_dir`. Returns the directory
        that actually holds the CSVs (the archive may nest one level)."""
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(self.fetch_export())) as zf:
            zf.extractall(target)
        if (target / "food-logs.csv").exists():
            return target
        for child in target.iterdir():
            if child.is_dir() and (child / "food-logs.csv").exists():
                return child
        raise LoseItError(
            f"export archive did not contain food-logs.csv (extracted to {target})")

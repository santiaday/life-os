"""One-time bootstrap: extract Whoop tokens from a mitmproxy capture.

Use case: you ran mitmproxy on your iPhone (or in front of the Whoop
simulator), logged into the Whoop app, and saved the flow with mitmproxy's
``--save-stream-file`` flag (or "Save Flows" in the GUI). This CLI parses
that file, finds the ``RespondToAuthChallenge`` response containing the
``AuthenticationResult`` (i.e. the moment Cognito hands tokens back after
your password + SMS code), and:

  1. Persists ``AccessToken`` / ``RefreshToken`` / ``IdToken`` / ``ExpiresIn``
     to ``oauth_tokens(service='whoop_private')`` via :func:`auth.save_tokens`.
  2. Writes the bare refresh token to ``./whoop_refresh_token.txt`` so the
     iPhone Shortcut can read it from iCloud Drive and use it as the seed
     for unattended REFRESH_TOKEN_AUTH calls.

Run:

    python -m ingest_whoop_journal.bootstrap_from_capture path/to/Mitmproxy_Flows

Multiple matching flows (e.g. you re-authed during the capture)? We pick
the most recent by ``request.timestamp_start`` and log how many we ignored.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ingest_whoop_journal import auth
from lifeos_core.logging import configure_logging, get_logger

log = get_logger(__name__)

CHALLENGE_TARGET = "AWSCognitoIdentityProviderService.RespondToAuthChallenge"
SIDECAR_FILENAME = "whoop_refresh_token.txt"


class BootstrapError(RuntimeError):
    pass


def _read_flows(path: Path) -> list:
    """Open a mitmproxy flow file. Lazy-imports mitmproxy so the rest of the
    package doesn't pull it in. Mitmproxy is heavy; this CLI is the only
    thing that needs it."""
    try:
        from mitmproxy import io as mitm_io
    except ImportError as e:  # pragma: no cover - import error path
        raise BootstrapError(
            "mitmproxy is required for bootstrap_from_capture. Install with "
            "`pip install mitmproxy` (or run inside the ingest_whoop_journal "
            "Docker image, which has it pinned)."
        ) from e

    if not path.exists():
        raise BootstrapError(f"flow file not found: {path}")

    with path.open("rb") as f:
        # FlowReader yields HTTPFlow objects (and some non-HTTP we ignore).
        return list(mitm_io.FlowReader(f).stream())


def _is_challenge_flow(flow: Any) -> bool:
    req = getattr(flow, "request", None)
    resp = getattr(flow, "response", None)
    if req is None or resp is None:
        return False
    target = req.headers.get("x-amz-target") or req.headers.get("X-Amz-Target")
    return target == CHALLENGE_TARGET


def _flow_timestamp(flow: Any) -> float:
    """Sort key. Mitmproxy versions differ on attribute name; try both."""
    return float(
        getattr(flow.request, "timestamp_start", 0.0)
        or getattr(flow, "timestamp_created", 0.0)
        or 0.0
    )


def _parse_auth_result(flow: Any) -> dict | None:
    body = flow.response.get_text()
    if not body:
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    result = payload.get("AuthenticationResult")
    if not isinstance(result, dict):
        return None
    if not (result.get("AccessToken") and result.get("RefreshToken")):
        return None
    return result


def _extract_latest_auth_result(flows: list) -> tuple[dict, int]:
    """Walk the flow list, return (most-recent AuthenticationResult, count_ignored)."""
    candidates = []
    for f in flows:
        if not _is_challenge_flow(f):
            continue
        result = _parse_auth_result(f)
        if result is None:
            continue
        candidates.append((_flow_timestamp(f), result))

    if not candidates:
        raise BootstrapError(
            "No RespondToAuthChallenge flow with AuthenticationResult found "
            "in the capture. Did you log in to the Whoop app while mitmproxy "
            "was attached? Re-capture and try again."
        )

    candidates.sort(key=lambda t: t[0], reverse=True)
    latest = candidates[0][1]
    ignored = len(candidates) - 1
    return latest, ignored


def _write_sidecar(refresh_token: str, target_dir: Path) -> Path:
    """Write the bare refresh token to a sidecar text file.

    No trailing newline — the iOS Shortcut "Get Text from Input" treats the
    entire file content verbatim, including newlines, and a trailing \\n
    invalidates the REFRESH_TOKEN parameter on the Cognito call."""
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / SIDECAR_FILENAME
    out.write_text(refresh_token, encoding="utf-8")
    return out.resolve()


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="ingest_whoop_journal.bootstrap_from_capture")
    p.add_argument(
        "flow_file",
        type=Path,
        help="Path to a mitmproxy flow file (binary, from --save-stream-file).",
    )
    p.add_argument(
        "--sidecar-dir",
        type=Path,
        default=Path.cwd(),
        help=f"Directory to write {SIDECAR_FILENAME} into (default: cwd).",
    )
    args = p.parse_args(argv)

    try:
        flows = _read_flows(args.flow_file)
        result, ignored = _extract_latest_auth_result(flows)
    except BootstrapError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    expires_in = int(result.get("ExpiresIn") or 86400)
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    auth.save_tokens(
        access_token=result["AccessToken"],
        refresh_token=result["RefreshToken"],
        id_token=result.get("IdToken"),
        expires_at=expires_at,
        metadata={
            "source": "bootstrap_from_capture",
            "flow_file": str(args.flow_file),
            "ignored_older_flows": ignored,
        },
    )
    sidecar = _write_sidecar(result["RefreshToken"], args.sidecar_dir)

    print("OK. Tokens persisted to oauth_tokens(service='whoop_private').")
    print(f"AccessToken expires at: {expires_at.isoformat()}")
    if ignored:
        print(f"Note: ignored {ignored} older RespondToAuthChallenge flow(s); "
              f"used the most recent.")
    print()
    print("Refresh-token sidecar written to:")
    print(f"  {sidecar}")
    print()
    print("Next: drop that file into iCloud Drive (drag/drop, or `mv` to a")
    print("synced folder). The iPhone Shortcut reads it on each refresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

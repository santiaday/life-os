"""ingest_whoop_private CLI.

Pulls Whoop's private iOS-API surface (daily metric trends, sleep-need,
behavior-impact) using the shared whoop_private bearer the iPhone Shortcut keeps
fresh. Default mode is ``ingest`` so the scheduler can invoke
``python -m ingest_whoop_private`` cleanly.

Usage:
    python -m ingest_whoop_private                            # incremental (today anchor)
    python -m ingest_whoop_private ingest --backfill 365      # walk end_date back in 182d strides
    python -m ingest_whoop_private ingest --data-type trend
    python -m ingest_whoop_private ingest --data-type sleep_need
    python -m ingest_whoop_private ingest --metric STEPS --metric CALORIES
"""

from __future__ import annotations

import argparse
import json
import sys

from ingest_whoop_private import ingest
from ingest_whoop_private.client import METRICS
from lifeos_core.logging import configure_logging


def _cmd_ingest(args) -> int:
    if args.metric:
        # Single-pipeline trend run scoped to specific metrics.
        from ingest_whoop_journal.auth import WhoopAuth
        from ingest_whoop_private.client import WhoopPrivateClient

        auth = WhoopAuth()
        auth.ensure_fresh()
        with WhoopPrivateClient(auth=auth) as client:
            n = ingest.ingest_trends(
                client, metrics=tuple(args.metric), backfill_days=args.backfill
            )
        out = {"trend": n}
    else:
        out = ingest.run_all(backfill_days=args.backfill, data_type=args.data_type)

    print(json.dumps(out, indent=2, default=str))
    return 1 if any(isinstance(v, str) and v.startswith("FAILED") for v in out.values()) else 0


def _cmd_login(args) -> int:
    """Interactive one-time Whoop login (email + password + MFA). Mints a fresh
    token bundle server-side via Cognito and writes it to
    oauth_tokens(service='whoop_private'). After this, WhoopAuth auto-refreshes —
    no iPhone Shortcut needed."""
    import getpass

    from ingest_whoop_journal.auth import save_tokens
    from lifeos_core.settings import settings
    from lifeos_core.whoop_cognito import bootstrap_login

    email = args.email or getattr(settings, "WHOOP_PRIVATE_EMAIL", None) or input("Whoop email: ").strip()
    password = getattr(settings, "WHOOP_PRIVATE_PASSWORD", None) or getpass.getpass("Whoop password: ")

    def _mfa(challenge: str) -> str:
        return input(f"Enter Whoop {challenge.replace('_', ' ').title()} code: ").strip()

    bundle = bootstrap_login(email, password, _mfa)
    save_tokens(
        access_token=bundle["access_token"],
        refresh_token=bundle["refresh_token"],
        id_token=bundle.get("id_token"),
        expires_at=bundle["expires_at"],
        metadata={"source": "server_cognito_login"},
    )
    print(json.dumps({
        "ok": True,
        "access_token_expires_at": bundle["expires_at"].isoformat() if bundle["expires_at"] else None,
        "refresh_token_chars": len(bundle["refresh_token"]),
        "note": "Token saved. WhoopAuth will now auto-refresh — iPhone Shortcut no longer required.",
    }, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="ingest_whoop_private")
    # `ingest` is an optional positional so both `python -m ingest_whoop_private`
    # and `... ingest` work (parity with ingest_whoop). `login` re-bootstraps auth.
    p.add_argument("cmd", nargs="?", default="ingest", choices=["ingest", "login"])
    p.add_argument(
        "--email", default=None,
        help="Whoop login email for `login` (else WHOOP_PRIVATE_EMAIL or prompt).",
    )
    p.add_argument(
        "--backfill", type=int, default=None,
        help="Days back to fetch. Omit for the incremental (today anchor) window.",
    )
    p.add_argument(
        "--data-type",
        choices=["trend", "sleep_need", "behavior_impact", "lift", "labs"],
        default=None,
        help="Limit to one pipeline. Omit to run all.",
    )
    p.add_argument(
        "--metric", action="append", default=None, metavar="METRIC",
        help=f"Trend metric(s) to fetch (repeatable). One of: {', '.join(METRICS)}.",
    )
    args = p.parse_args(argv)
    if args.cmd == "login":
        return _cmd_login(args)
    return _cmd_ingest(args)


if __name__ == "__main__":
    sys.exit(main())

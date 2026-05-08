"""CLI entrypoint for ingest_pushpress.

Subcommands:
    ingest               Default: refresh class types + ±7-day workout window.
        --past N           Days before today to pull (default 7).
        --future N         Days after today to pull (default 7).
        --skip-class-types Skip the class-type registry refresh.
    classes              One-shot: refresh dim_pushpress_class_type only.
    login                Bootstrap auth: persist a fresh access/refresh pair.
        --username, --password   Override env (PUSHPRESS_USERNAME / PUSHPRESS_PASSWORD).

Run:
    python -m ingest_pushpress ingest
    python -m ingest_pushpress ingest --past 14 --future 7
    python -m ingest_pushpress login
    python -m ingest_pushpress classes
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from ingest_pushpress import auth, ingest
from ingest_pushpress.client import PushPressClient
from lifeos_core.logging import configure_logging, get_logger

log = get_logger(__name__)


def _cmd_ingest(args) -> int:
    results = ingest.run_all(
        days_past=args.past,
        days_future=args.future,
        skip_class_types=args.skip_class_types,
    )
    print(json.dumps(results, indent=2, default=str))
    failures = [k for k, v in results.items() if isinstance(v, str)]
    return 1 if failures else 0


def _cmd_classes(_args) -> int:
    with PushPressClient() as client:
        n = ingest.ingest_class_types(client)
    print(json.dumps({"class_types": n}))
    return 0


def _cmd_login(args) -> int:
    user = args.username or os.environ.get("PUSHPRESS_USERNAME")
    pwd = args.password or os.environ.get("PUSHPRESS_PASSWORD")
    if not user:
        user = input("PushPress username/email: ").strip()
    if not pwd:
        pwd = getpass.getpass("PushPress password: ")
    out = auth.login_with_credentials(user, pwd)
    print(json.dumps(out, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="ingest_pushpress")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="Run the daily sync (class types + ±7-day window).")
    ing.add_argument("--past", type=int, default=None, help="Days before today (default 7).")
    ing.add_argument("--future", type=int, default=None, help="Days after today (default 7).")
    ing.add_argument("--skip-class-types", action="store_true",
                     help="Don't refresh dim_pushpress_class_type this run.")
    ing.set_defaults(func=_cmd_ingest)

    cls = sub.add_parser("classes", help="Refresh dim_pushpress_class_type only.")
    cls.set_defaults(func=_cmd_classes)

    lg = sub.add_parser("login", help="Persist a fresh access/refresh pair.")
    lg.add_argument("--username", help="PushPress username/email (default: env).")
    lg.add_argument("--password", help="PushPress password (default: env / prompt).")
    lg.set_defaults(func=_cmd_login)

    # Backwards-compat: bare `python -m ingest_pushpress` runs `ingest`.
    args = p.parse_args(argv) if argv is not None or len(sys.argv) > 1 else p.parse_args(["ingest"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

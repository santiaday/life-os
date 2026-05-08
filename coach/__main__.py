"""CLI entrypoint for coach.

Subcommands:
    parse              Parse pending PushPress sessions (default).
        --past N         Days before today (default 1).
        --future N       Days after today (default 14).
        --force          Re-parse sessions even if parsed_at is set.
    recompute          Re-run the load recommender (no parser, no Hevy push).
        --past N, --future N, --force
    sync               POST/PUT routines to Hevy.
        --past N, --future N
        --dry-run        Build payloads but don't call Hevy.
    run                Full chain: parse + recompute + sync. Default cron path.
        --past N, --future N
        --no-sync        Skip the Hevy push (parse + recompute only).
        --force-parse

Run:
    python -m coach run
    python -m coach parse --future 7 --force
    python -m coach sync --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys

from coach import orchestrator
from lifeos_core.logging import configure_logging, get_logger

log = get_logger(__name__)


def _cmd_parse(args) -> int:
    out = orchestrator.parse_pending_sessions(
        days_past=args.past, days_future=args.future, force=args.force,
    )
    print(json.dumps(out, indent=2, default=str))
    return 1 if out.get("errors") else 0


def _cmd_recompute(args) -> int:
    out = orchestrator.recompute_loads(
        days_past=args.past, days_future=args.future, force=args.force,
    )
    print(json.dumps(out, indent=2, default=str))
    return 0


def _cmd_sync(args) -> int:
    out = orchestrator.sync_to_hevy(
        days_past=args.past, days_future=args.future, dry_run=args.dry_run,
    )
    print(json.dumps(out, indent=2, default=str))
    return 1 if out.get("errors") else 0


def _cmd_run(args) -> int:
    out = orchestrator.run_all(
        days_past=args.past, days_future=args.future,
        force_parse=args.force_parse, sync_hevy=not args.no_sync,
    )
    print(json.dumps(out, indent=2, default=str))
    failures = [k for k, v in out.items() if isinstance(v, str) and v.startswith("FAILED")]
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="coach")
    sub = p.add_subparsers(dest="cmd", required=True)

    parse_p = sub.add_parser("parse", help="Parse pending PushPress sessions.")
    parse_p.add_argument("--past", type=int, default=1)
    parse_p.add_argument("--future", type=int, default=14)
    parse_p.add_argument("--force", action="store_true")
    parse_p.set_defaults(func=_cmd_parse)

    rec_p = sub.add_parser("recompute", help="Re-run the load recommender.")
    rec_p.add_argument("--past", type=int, default=0)
    rec_p.add_argument("--future", type=int, default=14)
    rec_p.add_argument("--force", action="store_true")
    rec_p.set_defaults(func=_cmd_recompute)

    sync_p = sub.add_parser("sync", help="Push routines to Hevy.")
    sync_p.add_argument("--past", type=int, default=1)
    sync_p.add_argument("--future", type=int, default=14)
    sync_p.add_argument("--dry-run", action="store_true")
    sync_p.set_defaults(func=_cmd_sync)

    run_p = sub.add_parser("run", help="Full chain: parse + recompute + sync.")
    run_p.add_argument("--past", type=int, default=1)
    run_p.add_argument("--future", type=int, default=14)
    run_p.add_argument("--no-sync", action="store_true")
    run_p.add_argument("--force-parse", action="store_true")
    run_p.set_defaults(func=_cmd_run)

    # Backwards-compat: bare `python -m coach` runs the full chain.
    args = p.parse_args(argv) if argv is not None or len(sys.argv) > 1 else p.parse_args(["run"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

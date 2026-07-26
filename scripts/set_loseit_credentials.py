"""Set the Lose It credentials locally and on the droplet, then verify.

    python scripts/set_loseit_credentials.py

Prompts for the password with getpass -- it is never echoed, never placed on a
command line, and never interpolated through a shell. That matters more than it
sounds: passwords with `!` trigger history expansion in interactive bash/zsh,
and `&`, `|` or `\\` are metacharacters to `sed`, so the obvious one-liner
silently writes a corrupted value.

Writes `.env` locally, copies just the two lines to the droplet over ssh stdin,
restarts the affected containers, and runs `ingest_loseit check` on both sides.
"""

from __future__ import annotations

import argparse
import getpass
import pathlib
import subprocess
import sys

DROPLET = "root@198.199.64.234"
REMOTE_DIR = "/opt/life-os"
KEYS = ("LOSEIT_EMAIL", "LOSEIT_PASSWORD")


def set_env_values(path: pathlib.Path, values: dict[str, str]) -> None:
    """Rewrite KEY=value lines in place, preserving everything else.

    Line-oriented and literal — no regex substitution, so no value can be
    reinterpreted as a pattern.
    """
    lines = path.read_text().splitlines(keepends=True) if path.exists() else []
    remaining = dict(values)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}\n")
        else:
            out.append(line)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    for key, val in remaining.items():
        out.append(f"{key}={val}\n")
    path.write_text("".join(out))


def push_to_droplet(values: dict[str, str]) -> int:
    """Send the lines over ssh stdin so they never appear in argv or history."""
    payload = "".join(f"{k}={v}\n" for k, v in values.items())
    remote = (
        f"cd {REMOTE_DIR} && "
        f"sed -i '/^LOSEIT_EMAIL=/d;/^LOSEIT_PASSWORD=/d' .env && "
        f"cat >> .env && "
        f"docker compose up -d scheduler mcp >/dev/null 2>&1 && "
        f"echo remote-ok"
    )
    p = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=25", "-o", "BatchMode=yes", DROPLET, remote],
        input=payload, text=True, capture_output=True,
    )
    print("  droplet:", (p.stdout or p.stderr).strip()[:200])
    return p.returncode


def check(local: bool = True) -> None:
    if local:
        cmd = [sys.executable, "-m", "ingest_loseit", "check"]
        p = subprocess.run(cmd, capture_output=True, text=True)
    else:
        p = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=40", "-o", "BatchMode=yes", DROPLET,
             f"cd {REMOTE_DIR} && docker compose exec -T scheduler "
             f"python -m ingest_loseit check"],
            capture_output=True, text=True,
        )
    body = p.stdout + p.stderr
    for line in body.splitlines():
        if any(k in line for k in ("self_sustaining", "has_credentials",
                                   "days_left", "warning", "ok\"", "AUTH")):
            print("   ", line.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="santiagoaday7@gmail.com")
    ap.add_argument("--local-only", action="store_true")
    args = ap.parse_args()

    password = getpass.getpass("Lose It password (not echoed): ")
    if not password:
        print("no password entered; nothing changed")
        return 1
    values = {"LOSEIT_EMAIL": args.email, "LOSEIT_PASSWORD": password}

    env = pathlib.Path(__file__).resolve().parent.parent / ".env"
    set_env_values(env, values)
    print(f"local .env updated ({env})")

    if not args.local_only and push_to_droplet(values) != 0:
        print("droplet update FAILED — local .env was still updated")
        return 1

    print("\nlocal check:")
    check(local=True)
    if not args.local_only:
        print("droplet check:")
        check(local=False)
    print("\nWant `self_sustaining: true`. If so, the 14-day expiry no longer "
          "matters and you can rotate the password in Lose It safely.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

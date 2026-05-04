#!/usr/bin/env bash
# install.sh — wire up the aw_sync launchd agent on a Mac laptop.
#
# Run from the LifeOS repo root:
#     bash aw_sync/install.sh
#
# Prereqs the script does NOT do for you:
#   1. ActivityWatch installed and aw-watcher-afk running on this machine.
#      (https://activitywatch.net — `brew install --cask activitywatch`)
#   2. uv venv created at .venv with all repo deps:
#         uv sync   (or: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)
#   3. .env populated with SUPABASE_DB_URL, AW_HOSTNAME_CATEGORY at minimum.
#      Example AW_HOSTNAME_CATEGORY:
#         AW_HOSTNAME_CATEGORY={"Santiago-Aday":"DoorLoop work","Santiago-Personal":"Personal work"}

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${REPO}/aw_sync/io.lifeos.awsync.plist"
TARGET="${HOME}/Library/LaunchAgents/io.lifeos.awsync.plist"
PY="${REPO}/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "ERR: ${PY} not found. Create the venv first (uv sync, or pip install)." >&2
  exit 1
fi

mkdir -p "${HOME}/Library/LaunchAgents"
mkdir -p "${HOME}/Library/Logs"

# Substitute placeholders into the plist template.
sed \
  -e "s|__LIFEOS_VENV_PYTHON__|${PY}|g" \
  -e "s|__LIFEOS_REPO__|${REPO}|g" \
  -e "s|__HOME__|${HOME}|g" \
  "$TEMPLATE" > "$TARGET"

echo "Wrote $TARGET"

# Reload the agent. `launchctl bootout` is the modern replacement for
# `unload`; ignore non-zero exit if the agent wasn't loaded yet.
launchctl bootout "gui/$(id -u)/io.lifeos.awsync" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TARGET"

echo "Loaded launchd agent. Logs: ${HOME}/Library/Logs/aw_sync.log"
echo "Force a run now to verify:"
echo "    launchctl kickstart -k gui/$(id -u)/io.lifeos.awsync"

#!/usr/bin/env bash
# install_lite.sh — set up the standalone aw_sync/lite.py daemon.
#
# Usage on each Mac (work + personal):
#
#   1. Copy three files to the laptop (anywhere; e.g. ~/aw-sync/):
#        aw_sync/lite.py
#        aw_sync/lite.plist
#        aw_sync/install_lite.sh
#
#      From the LifeOS-having Mac:
#        scp aw_sync/lite.{py,plist} aw_sync/install_lite.sh user@<other-mac>:~/aw-sync/
#
#   2. On each laptop, create ~/.config/aw-sync.env with:
#        SUPABASE_URL=https://<project-ref>.supabase.co
#        SUPABASE_SERVICE_KEY=<service_role JWT from Supabase dashboard>
#        AW_HOSTNAME_CATEGORY={"<work-host>":"DoorLoop work","<personal-host>":"Personal work"}
#
#      Find the JWT at: Supabase dashboard → Project Settings → API → service_role.
#
#   3. Make sure ActivityWatch is installed and the AFK watcher is running:
#        brew install --cask activitywatch
#        open /Applications/ActivityWatch.app
#
#   4. Run this installer:
#        bash ~/aw-sync/install_lite.sh
#
#   The plist will run lite.py every 5 min via launchd. Logs land at
#   ~/Library/Logs/aw_sync.log.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${SCRIPT_DIR}/lite.py"
TEMPLATE="${SCRIPT_DIR}/lite.plist"
TARGET="${HOME}/Library/LaunchAgents/io.lifeos.awsync.plist"

if [[ ! -f "$SCRIPT" ]]; then
  echo "ERR: lite.py not found at $SCRIPT" >&2
  exit 1
fi
if [[ ! -f "$TEMPLATE" ]]; then
  echo "ERR: lite.plist not found at $TEMPLATE" >&2
  exit 1
fi
if [[ ! -f "${HOME}/.config/aw-sync.env" ]]; then
  echo "ERR: ~/.config/aw-sync.env missing — see install_lite.sh header for the required keys." >&2
  exit 1
fi

chmod +x "$SCRIPT"

mkdir -p "${HOME}/Library/LaunchAgents" "${HOME}/Library/Logs"

# Substitute placeholders into the plist.
sed \
  -e "s|__HOME__|${HOME}|g" \
  -e "s|__SCRIPT__|${SCRIPT}|g" \
  "$TEMPLATE" > "$TARGET"

echo "Wrote $TARGET"

# Reload the agent. bootout returns non-zero if it wasn't loaded yet, ignore.
launchctl bootout "gui/$(id -u)/io.lifeos.awsync" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TARGET"

echo "Loaded launchd agent. Logs: ${HOME}/Library/Logs/aw_sync.log"
echo
echo "Force a run now to verify:"
echo "    launchctl kickstart -k gui/$(id -u)/io.lifeos.awsync"
echo "Then check the log:"
echo "    tail -f ${HOME}/Library/Logs/aw_sync.log"

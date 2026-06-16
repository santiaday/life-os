#!/usr/bin/env bash
# Cal AI nutrition sync (macOS, local).
#
# Cal AI is local-first: the authoritative diary lives in the app's CoreData
# store, which syncs to a Firestore location your user token can't read over
# REST. So the warehouse is refreshed from the iPhone's local backup instead.
#
# Two modes:
#   1) MANUAL  (default): reads the newest Finder/iTunes backup in
#      ~/Library/Application Support/MobileSync/Backup  (needs Full Disk Access
#      for whatever runs this — Terminal, or the python binary).
#   2) UNATTENDED: set CALAI_USE_IDEVICEBACKUP=1 to make a fresh backup with
#      libimobiledevice into $CALAI_BACKUP_ROOT (a NON-protected dir, so no Full
#      Disk Access needed) and ingest that. Requires:  brew install libimobiledevice
#
# Idempotent — only new/changed meals are written (keyed on Cal AI's entry id).
set -euo pipefail

# launchd starts with a minimal PATH; ensure Homebrew (idevicebackup2) is found.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

REPO="${LIFEOS_REPO:-/Users/santiagoaday/Documents/LifeOS}"
PY="${LIFEOS_PY:-$REPO/.venv/bin/python}"
USER_ID="${CALAI_USER_ID:-f5Q08GHjoJUdUp8jUO4u5qPZZII2}"
LOG="${CALAI_SYNC_LOG:-$HOME/Library/Logs/calai_sync.log}"
mkdir -p "$(dirname "$LOG")"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') calai_sync start ===" >>"$LOG"

cd "$REPO"
set -a; [ -f .env ] && . ./.env; set +a

if [ "${CALAI_USE_IDEVICEBACKUP:-0}" = "1" ]; then
  export CALAI_BACKUP_ROOT="${CALAI_BACKUP_ROOT:-$HOME/CalAIBackups}"
  mkdir -p "$CALAI_BACKUP_ROOT"
  echo "idevicebackup2 -> $CALAI_BACKUP_ROOT" >>"$LOG"
  # Encryption OFF so Manifest.db is plaintext; full backup keeps app data.
  idevicebackup2 backup --target "$CALAI_BACKUP_ROOT" >>"$LOG" 2>&1 || {
    echo "idevicebackup2 failed (phone unreachable / not trusted?)" >>"$LOG"; exit 1; }
fi

"$PY" -m ingest_calai local --from-backup --user-id "$USER_ID" >>"$LOG" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') calai_sync done ===" >>"$LOG"

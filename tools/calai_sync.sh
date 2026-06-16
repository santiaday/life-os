#!/usr/bin/env bash
# Cal AI nutrition sync (macOS, local).
#
# Cal AI is local-first: the authoritative diary lives in the app's CoreData
# store, which syncs to a Firestore location your user token can't read over
# REST. So the warehouse is refreshed from the iPhone's local backup instead.
#
# iOS will only back up an UNLOCKED device, so this is designed to be polled
# (hourly): it quietly skips when the phone is absent or locked, and syncs the
# first time the phone is connected + unlocked each day. A freshness guard keeps
# it to ~one backup/day. Idempotent — only new/changed meals are written.
#
# Modes:
#   UNATTENDED (CALAI_USE_IDEVICEBACKUP=1): make a fresh backup with
#     libimobiledevice into $CALAI_BACKUP_ROOT (a NON-protected dir, no Full Disk
#     Access). Requires: brew install libimobiledevice; backup encryption OFF.
#   MANUAL (default): ingest the newest Finder/iTunes backup in
#     ~/Library/Application Support/MobileSync/Backup (needs Full Disk Access).
set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

REPO="${LIFEOS_REPO:-/Users/santiagoaday/Documents/LifeOS}"
PY="${LIFEOS_PY:-$REPO/.venv/bin/python}"
USER_ID="${CALAI_USER_ID:-f5Q08GHjoJUdUp8jUO4u5qPZZII2}"
LOG="${CALAI_SYNC_LOG:-$HOME/Library/Logs/calai_sync.log}"
FRESH_SECS="${CALAI_FRESH_SECS:-64800}"   # skip if synced < 18h ago
mkdir -p "$(dirname "$LOG")"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"; }

cd "$REPO" || { log "repo missing: $REPO"; exit 1; }
set -a; [ -f .env ] && . ./.env; set +a

if [ "${CALAI_USE_IDEVICEBACKUP:-0}" = "1" ]; then
  export CALAI_BACKUP_ROOT="${CALAI_BACKUP_ROOT:-$HOME/CalAIBackups}"
  mkdir -p "$CALAI_BACKUP_ROOT"

  # 1) phone present?
  if ! idevice_id -l 2>/dev/null | grep -q .; then
    log "skip: no iPhone reachable"; exit 0
  fi
  # 2) already fresh today?
  STAMP="$CALAI_BACKUP_ROOT/.last_sync"
  if [ -f "$STAMP" ]; then
    age=$(( $(date +%s) - $(stat -f %m "$STAMP" 2>/dev/null || echo 0) ))
    if [ "$age" -lt "$FRESH_SECS" ]; then log "skip: synced ${age}s ago"; exit 0; fi
  fi
  # 3) back up (needs the device UNLOCKED). Locked -> clean skip, retry next tick.
  log "idevicebackup2 -> $CALAI_BACKUP_ROOT"
  out=$(idevicebackup2 backup "$CALAI_BACKUP_ROOT" 2>&1); rc=$?
  echo "$out" | tail -5 >>"$LOG"
  if [ "$rc" -ne 0 ]; then
    if echo "$out" | grep -qiE '208|Device locked|Waiting for passcode'; then
      log "skip: iPhone locked — will retry next tick"; exit 0
    fi
    log "ERROR: idevicebackup2 failed (rc=$rc)"; exit 1
  fi
fi

log "ingest: ingest_calai local --from-backup"
if "$PY" -m ingest_calai local --from-backup --user-id "$USER_ID" >>"$LOG" 2>&1; then
  [ "${CALAI_USE_IDEVICEBACKUP:-0}" = "1" ] && touch "${CALAI_BACKUP_ROOT}/.last_sync"
  log "done"
else
  log "ERROR: ingest failed"; exit 1
fi

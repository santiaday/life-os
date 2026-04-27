#!/usr/bin/env bash
# Daily pg_dump → DigitalOcean Spaces (or any s3-compatible bucket).
#
# Usage (from droplet, e.g. via cron at 5am):
#   /opt/life-os/scripts/backup.sh
#
# Required env (read from /opt/life-os/.env):
#   SUPABASE_DB_URL_DIRECT     (port 5432, session-mode connection)
#   BACKUP_S3_BUCKET           e.g. life-os-backups
#   BACKUP_S3_ENDPOINT         e.g. https://nyc3.digitaloceanspaces.com
#   BACKUP_S3_ACCESS_KEY_ID
#   BACKUP_S3_SECRET_ACCESS_KEY
#   BACKUP_RETENTION_DAYS      defaults to 30
#
# Requires: postgresql-client, awscli (or s3cmd).

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${SUPABASE_DB_URL_DIRECT:?SUPABASE_DB_URL_DIRECT must be set in .env}"
: "${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET must be set in .env}"
: "${BACKUP_S3_ENDPOINT:?BACKUP_S3_ENDPOINT must be set in .env}"
: "${BACKUP_S3_ACCESS_KEY_ID:?BACKUP_S3_ACCESS_KEY_ID must be set in .env}"
: "${BACKUP_S3_SECRET_ACCESS_KEY:?BACKUP_S3_SECRET_ACCESS_KEY must be set in .env}"

RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP_DUMP="$(mktemp -t lifeos-backup-XXXXXX.sql.gz)"
trap 'rm -f "$TMP_DUMP"' EXIT

echo "[backup] dumping to $TMP_DUMP"
pg_dump --no-owner --no-privileges "$SUPABASE_DB_URL_DIRECT" \
  | gzip -9 > "$TMP_DUMP"

DUMP_BYTES=$(stat -c%s "$TMP_DUMP" 2>/dev/null || stat -f%z "$TMP_DUMP")
echo "[backup] dump size: $DUMP_BYTES bytes"
if [[ "$DUMP_BYTES" -lt 10000 ]]; then
  echo "[backup] FAIL: dump suspiciously small (<10kB). Aborting upload." >&2
  exit 2
fi

REMOTE_KEY="lifeos/${TIMESTAMP}.sql.gz"
echo "[backup] uploading s3://${BACKUP_S3_BUCKET}/${REMOTE_KEY}"

AWS_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY_ID" \
AWS_SECRET_ACCESS_KEY="$BACKUP_S3_SECRET_ACCESS_KEY" \
aws --endpoint-url "$BACKUP_S3_ENDPOINT" \
    s3 cp "$TMP_DUMP" "s3://${BACKUP_S3_BUCKET}/${REMOTE_KEY}" \
    --no-progress

# Prune old objects
CUTOFF_EPOCH=$(date -u -d "${RETENTION_DAYS} days ago" +%s 2>/dev/null \
            || date -u -v-"${RETENTION_DAYS}"d +%s)

echo "[backup] pruning objects older than ${RETENTION_DAYS} days (cutoff: ${CUTOFF_EPOCH})"
AWS_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY_ID" \
AWS_SECRET_ACCESS_KEY="$BACKUP_S3_SECRET_ACCESS_KEY" \
aws --endpoint-url "$BACKUP_S3_ENDPOINT" \
    s3api list-objects-v2 \
    --bucket "$BACKUP_S3_BUCKET" \
    --prefix "lifeos/" \
    --query 'Contents[].[Key,LastModified]' \
    --output text 2>/dev/null \
| while read -r KEY LAST_MOD; do
    [[ -z "$KEY" || -z "$LAST_MOD" ]] && continue
    OBJ_EPOCH=$(date -u -d "$LAST_MOD" +%s 2>/dev/null \
              || date -u -j -f "%Y-%m-%dT%H:%M:%S+00:00" "${LAST_MOD%.*}+00:00" +%s)
    if [[ "$OBJ_EPOCH" -lt "$CUTOFF_EPOCH" ]]; then
      echo "[backup] prune $KEY"
      AWS_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY_ID" \
      AWS_SECRET_ACCESS_KEY="$BACKUP_S3_SECRET_ACCESS_KEY" \
      aws --endpoint-url "$BACKUP_S3_ENDPOINT" \
          s3 rm "s3://${BACKUP_S3_BUCKET}/${KEY}" || true
    fi
done

echo "[backup] OK ${REMOTE_KEY}"

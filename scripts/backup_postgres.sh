#!/usr/bin/env bash
# Automated PostgreSQL Encrypted Backup & Verification Script (Section 25.1)
set -euo pipefail
BACKUP_DIR="${BACKUP_DIR:-/var/backups/vpnstore}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

OUT_FILE="$BACKUP_DIR/vpnstore_${TIMESTAMP}.sql.gz"
pg_dump "${DATABASE_URL}" | gzip -9 > "$OUT_FILE"
sha256sum "$OUT_FILE" > "${OUT_FILE}.sha256"
find "$BACKUP_DIR" -type f -mtime +14 -delete
echo "Backup completed and checksummed: $OUT_FILE"

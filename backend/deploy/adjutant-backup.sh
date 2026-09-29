#!/usr/bin/env bash
# Consistent SQLite snapshot with .backup, 7-day retention.
set -euo pipefail
DB="${DATABASE_PATH:-/var/lib/adjutant/adjutant.db}"
DEST=/var/backups/adjutant
[ -f "$DB" ] || { echo "no database at $DB yet, nothing to back up"; exit 0; }
OUT="$DEST/adjutant-$(date -u +%Y%m%dT%H%M%SZ).db"
sqlite3 "$DB" ".backup '$OUT'"
gzip -f "$OUT"
find "$DEST" -maxdepth 1 -name 'adjutant-*.db.gz' -mtime +7 -delete
echo "backup written: $OUT.gz"

#!/usr/bin/env bash
set -euo pipefail
umask 077

backup_root=${BACKUP_ROOT:-/var/backups/floorball-bot}
media_root=${MEDIA_ROOT:-/var/lib/floorball-bot/media}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
daily_dir="$backup_root/daily"
weekly_dir="$backup_root/weekly"
mkdir -p "$daily_dir" "$weekly_dir"

exec 9>"$backup_root/.backup.lock"
flock -n 9

: "${PGDATABASE:?PGDATABASE is required}"
pg_dump --format=custom --file="$daily_dir/database-$timestamp.dump"
if [[ -d "$media_root" ]]; then
    tar --create --gzip --file="$daily_dir/media-$timestamp.tar.gz" \
        --directory="$media_root" .
fi

sha256sum "$daily_dir"/*"$timestamp"* > "$daily_dir/manifest-$timestamp.sha256"

if [[ $(date -u +%u) == 7 ]]; then
    cp --preserve=timestamps "$daily_dir/database-$timestamp.dump" "$weekly_dir/"
    [[ ! -f "$daily_dir/media-$timestamp.tar.gz" ]] || \
        cp --preserve=timestamps "$daily_dir/media-$timestamp.tar.gz" "$weekly_dir/"
fi

find "$daily_dir" -type f -mtime +7 -delete
find "$weekly_dir" -type f -mtime +28 -delete


#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
umask 077
restore_source="$(realpath "${1:?Usage: restore.sh BACKUP_DIRECTORY}")"
[[ "${PROMO_RESTORE_CONFIRMED:-}" == 'RESTORE_MIESZKO_PROMO' ]] || { echo 'Restores replace this app database and files. First back up the current app. Set PROMO_RESTORE_CONFIRMED=RESTORE_MIESZKO_PROMO to proceed.'; exit 1; }
(cd "$restore_source" && sha256sum -c SHA256SUMS)
tar -tzf "$restore_source/files.tar.gz" >/dev/null
# Accept only a trusted backup produced by backup.sh.
backup_root="${BACKUP_DIR:-$PWD/backups}"
mkdir -p "$backup_root"
exec 9>"$backup_root/.lock"
flock -n 9 || { echo 'A backup or restore is already running'; exit 1; }
docker compose stop web worker
docker compose exec -T db pg_restore -U promo -d promo --clean --if-exists --no-owner --exit-on-error < "$restore_source/database.dump"
docker compose run --rm --no-deps -T --entrypoint python web -c 'from pathlib import Path; import shutil; root=Path("/data"); [(shutil.rmtree(p) if p.is_dir() else p.unlink()) for p in root.iterdir()]'
docker compose run --rm --no-deps -T --entrypoint tar web -C /data -xzf - < "$restore_source/files.tar.gz"
docker compose run --rm migrate
docker compose up -d web worker
echo 'Restore completed. Verify login, product images and saved PDFs before reopening access.'

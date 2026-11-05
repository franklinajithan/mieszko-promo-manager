#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
umask 077
backup_root="${BACKUP_DIR:-$PWD/backups}"
mkdir -p "$backup_root"
exec 9>"$backup_root/.lock"
flock -n 9 || { echo 'A backup or restore is already running'; exit 1; }
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
staging="$(mktemp -d "$backup_root/.incomplete-XXXXXX")"
cleanup() { ./scripts/compose.sh start web worker >/dev/null; rm -rf "$staging"; }
trap cleanup EXIT
# Quiesce BOTH writers so the database and original/thumbnail/print files align.
./scripts/compose.sh stop web worker
# pg_dump writes no passwords to command arguments or logs.
./scripts/compose.sh exec -T db pg_dump -U promo -d promo -Fc > "$staging/database.dump"
./scripts/compose.sh run --rm --no-deps -T --entrypoint tar web -C /data -czf - . > "$staging/files.tar.gz"
./scripts/compose.sh exec -T db pg_restore --list < "$staging/database.dump" >/dev/null
tar -tzf "$staging/files.tar.gz" >/dev/null
(cd "$staging" && sha256sum database.dump files.tar.gz > SHA256SUMS)
printf '%s\n' "$(git rev-parse HEAD)" > "$staging/commit.txt"
mv "$staging" "$backup_root/$stamp"
printf 'Backup created: %s\n' "$backup_root/$stamp"
# No automatic deletion; configure retention after an off-host restore test.

#!/usr/bin/env bash
# Run ONLY this application's Compose project with its own configuration.
set -Eeuo pipefail
promo_root="$(cd "$(dirname "$0")/.." && pwd)"
[[ -f "$promo_root/.env" && ! -L "$promo_root/.env" ]] || { echo 'Create this app configuration first; .env must be a regular file.' >&2; exit 1; }
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES
unset POSTGRES_PASSWORD PROMO_APP_PASSWORD ALLOWED_ORIGIN COOKIE_SECURE PROMO_PORT
exec docker compose --project-name mieszko-promo --project-directory "$promo_root" \
    --file "$promo_root/docker-compose.yml" --env-file "$promo_root/.env" "$@"

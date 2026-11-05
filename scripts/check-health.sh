#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
for service in db web worker; do
    container="$(docker compose ps -q "$service")"
    [[ -n "$container" ]] || { echo "$service: missing"; exit 1; }
    state="$(docker inspect --format '{{.State.Health.Status}}' "$container")"
    [[ "$state" == healthy ]] || { echo "$service: $state"; exit 1; }
done
echo 'All promo services healthy'

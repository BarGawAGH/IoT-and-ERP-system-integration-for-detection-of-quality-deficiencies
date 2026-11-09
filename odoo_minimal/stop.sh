#!/bin/bash
# Stop containers without deleting the database, filestore or secrets.
set -euo pipefail
poc_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
command -v docker >/dev/null 2>&1 || { printf 'Nie znaleziono Docker.\n' >&2; exit 1; }
if [ -n "${DOCKER_HOST:-}" ] && [[ "$DOCKER_HOST" != unix://* ]]; then
  printf 'DOCKER_HOST nie wskazuje lokalnego Docker Desktop. Przerwano.\n' >&2
  exit 1
fi
poc_endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}')"
[[ "$poc_endpoint" == unix://* ]] || { printf 'Wybierz lokalny kontekst Docker Desktop.\n' >&2; exit 1; }
docker compose --project-name odoo-iot-minimal --file "$poc_dir/compose.yaml" stop
printf 'Kontenery zatrzymane. Dane i hasła pozostały zachowane. Ponowny start: bash start.sh\n'

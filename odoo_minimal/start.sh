#!/bin/bash
# macOS Bash 3.2 compatible. Secrets are generated on the user's Mac only.
set -euo pipefail
umask 077

poc_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
poc_runtime="$poc_dir/.runtime"
poc_image="odoo:20.0-20261015"
poc_project="odoo-iot-minimal"
poc_stage=""
poc_locked=0
poc_compose=(docker compose --project-name "$poc_project" --file "$poc_dir/compose.yaml")

fail() {
  printf '\nBŁĄD: %s\n' "$1" >&2
  exit 1
}

cleanup() {
  if [ -n "$poc_stage" ] && [ -d "$poc_stage" ]; then
    rm -rf "$poc_stage"
  fi
  if [ "$poc_locked" = 1 ]; then
    rmdir "$poc_dir/.start-lock" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

command -v docker >/dev/null 2>&1 || fail "Nie znaleziono Docker. Zainstaluj i uruchom Docker Desktop, potem ponów: bash start.sh"
command -v openssl >/dev/null 2>&1 || fail "Nie znaleziono openssl dostarczanego z macOS."
docker compose version >/dev/null 2>&1 || fail "Brak polecenia docker compose. Użyj aktualnego Docker Desktop."

# Do not send the local secrets to a remote Docker host/context by accident.
if [ -n "${DOCKER_HOST:-}" ] && [[ "$DOCKER_HOST" != unix://* ]]; then
  fail "DOCKER_HOST wskazuje zdalne połączenie. Wybierz lokalny Docker Desktop przed uruchomieniem tego pakietu."
fi
poc_endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}')"
[[ "$poc_endpoint" == unix://* ]] || fail "Bieżący kontekst Docker nie wskazuje lokalnego gniazda. Wybierz lokalny kontekst Docker Desktop."
docker info >/dev/null 2>&1 || fail "Docker Desktop nie jest gotowy. Otwórz aplikację Docker i poczekaj, aż uruchomi silnik."
poc_up_help="$(docker compose up --help)"
[[ "$poc_up_help" == *--wait-timeout* ]] || fail "Ta wersja Compose nie obsługuje oczekiwania na gotowość. Zaktualizuj Docker Desktop."

mkdir "$poc_dir/.start-lock" 2>/dev/null || fail "Inny start jest w toku albo pozostał katalog .start-lock po przerwaniu. Szczegóły: README, sekcja rozwiązywania problemów."
poc_locked=1

if [ -e "$poc_runtime" ] && [ ! -d "$poc_runtime" ]; then
  fail ".runtime istnieje, ale nie jest katalogiem. Nie zmieniono jego zawartości."
fi

if [ ! -d "$poc_runtime" ]; then
  # Never manufacture a new PostgreSQL password for an existing data volume.
  poc_volumes="$(docker volume ls --format '{{.Name}}')"
  while IFS= read -r poc_volume; do
    case "$poc_volume" in
      odoo_iot_minimal_postgres|odoo_iot_minimal_filestore)
        fail "Istnieją już dane tego projektu, ale brakuje katalogu .runtime. Wróć do pierwotnego folderu albo odzyskaj jego .runtime z kopii. Nie wygenerowano nowych haseł ani nie usunięto danych."
        ;;
    esac
  done <<< "$poc_volumes"

  printf 'Pierwsze uruchomienie: przygotowanie obrazu Odoo i lokalnych haseł.\n'
  if ! docker image inspect "$poc_image" >/dev/null 2>&1; then
    docker pull "$poc_image"
  fi
  poc_stage="$(mktemp -d "$poc_dir/.runtime-init.XXXXXX")"
  chmod 700 "$poc_stage"
  openssl rand -hex 32 > "$poc_stage/postgres-password.txt"
  openssl rand -hex 32 > "$poc_stage/master-password.txt"

  # Use the exact Odoo image's password hasher; no host Python installation.
  # Password is passed on stdin, never as an argument or environment variable.
  # Hashing runs without network and without access to application data volumes.
  poc_master_hash="$(docker run --rm -i --network none --entrypoint python3 "$poc_image" -c 'import sys; from odoo.tools.config import crypt_context; secret = sys.stdin.read().strip(); assert len(secret) == 64; print(crypt_context.hash(secret))' < "$poc_stage/master-password.txt")"
  [[ "$poc_master_hash" == '$pbkdf2-sha512$'* ]] || fail "Obraz nie zwrócił oczekiwanego skrótu hasła Odoo. Nie uruchomiono bazy."
  [[ "$poc_master_hash" != *$'\n'* ]] || fail "Niepoprawny format skrótu hasła. Nie uruchomiono bazy."

  cat > "$poc_stage/odoo.conf" <<EOF
[options]
admin_passwd = $poc_master_hash
addons_path = /mnt/extra-addons
data_dir = /var/lib/odoo
db_host = db
db_port = 5432
db_user = odoo
dbfilter = ^odoo_iot_minimal$
list_db = True
http_interface = 0.0.0.0
http_port = 8069
proxy_mode = False
workers = 0
max_cron_threads = 1
log_level = info
EOF
  printf '%s\n' "$poc_image" > "$poc_stage/image-version.txt"

  # The parent directory is private (0700). Only these two individual files
  # are mounted into containers; 0444 lets their non-root users read them.
  chmod 444 "$poc_stage/postgres-password.txt" "$poc_stage/odoo.conf"
  chmod 600 "$poc_stage/master-password.txt" "$poc_stage/image-version.txt"
  mv "$poc_stage" "$poc_runtime"
  poc_stage=""
  unset poc_master_hash
else
  for poc_file in postgres-password.txt master-password.txt odoo.conf image-version.txt; do
    [ -s "$poc_runtime/$poc_file" ] || fail "Brakuje pliku .runtime/$poc_file lub jest pusty. Nie wygenerowano zastępczych haseł. Przywróć pełny katalog .runtime z pierwotnego folderu/kopii."
  done
  [ "$(cat "$poc_runtime/image-version.txt")" = "$poc_image" ] || fail "Ten katalog .runtime pochodzi z innej wersji pakietu. Wymagane jest świadome sprawdzenie konfiguracji przed zmianą wersji."
  chmod 700 "$poc_runtime"
fi

cd "$poc_dir"
printf '\nUruchamianie PostgreSQL i Odoo. Pierwsze pobieranie obrazów może potrwać kilka minut.\n'
if "${poc_compose[@]}" up -d --pull missing --wait --wait-timeout 180; then
  printf '\nOdoo odpowiada, a PostgreSQL jest gotowy.\n'
  printf 'Otwórz: http://127.0.0.1:8070/web/database/manager\n'
  printf 'Przy pierwszym uruchomieniu utwórz bazę: odoo_iot_minimal\n'
  printf 'Hasło główne menedżera baz odczytasz poleceniem:\n'
  printf '  cat %q\n' "$poc_runtime/master-password.txt"
  printf 'Login oraz hasło użytkownika Odoo wybierzesz samodzielnie w formularzu.\n'
else
  printf '\nNie potwierdzono gotowości usług. To NIE jest komunikat udanego uruchomienia.\n' >&2
  "${poc_compose[@]}" ps >&2 || true
  "${poc_compose[@]}" logs --no-color --tail=50 db odoo >&2 || true
  printf '\nZachowaj powyższy błąd. Nie usuwaj wolumenów ani katalogu .runtime.\n' >&2
  exit 1
fi

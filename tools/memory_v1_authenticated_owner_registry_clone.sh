#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
container=${POSTGRES_CONTAINER:-brains-postgres-1}
source_db=${POSTGRES_SOURCE_DB:-memory}
clone_db="memory_universal_auth_v2_$$"
migration="$repo_root/ops/sql/20260727_memory_v1_authenticated_owner_registry_v1.sql"
test_sql="$repo_root/tests/memory_v1_authenticated_owner_registry_v1.sql"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists "$clone_db" >/dev/null
}
trap cleanup EXIT

test -f "$migration"
test -f "$test_sql"
migration_sha256=$(sha256sum "$migration" | awk '{print $1}')
test_sha256=$(sha256sum "$test_sql" | awk '{print $1}')

docker exec "$container" createdb -U sage "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" \
  --schema-only --format=custom \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db" \
      --exit-on-error
docker exec -i "$container" psql -U sage -d "$clone_db" \
  -X -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone_db" \
  -X -v ON_ERROR_STOP=1 <"$test_sql" >/dev/null

printf 'authenticated_owner_registry_clone=PASS\n'
printf 'migration_sha256=%s\n' "$migration_sha256"
printf 'test_sha256=%s\n' "$test_sha256"

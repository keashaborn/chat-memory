#!/usr/bin/env bash
set -euo pipefail

expected_live=${1:?expected live commit required}
target_commit=${2:?target commit required}
live=/opt/chat-memory
worktree=/home/ubuntu/chat-memory-memory-v1-v5-2-projection-review
migration=ops/sql/20260724_memory_v1_governed_entity_scope_reader_v1.sql
security_test=tests/memory_v1_governed_entity_scope_reader_v1.sql
live_probe=scripts/memory_v1_entity_scope_v2_live_probe.py
expected_migration_sha=66f0f72c49dedbf0cf3d43e098ca12a56230503c4a10a044fbb238ecdaf17b35
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_tmp=$(mktemp /tmp/memory-v1-entity-scope-production.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-entity-scope-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-entity-scope-after.XXXXXX.tsv)
timer_state=$(mktemp /tmp/memory-v1-entity-scope-timers.XXXXXX.txt)
backup=/var/backups/verbalsage/memory-v1-entity-scope-pre-${timestamp}.dump
git_live=(git -c safe.directory="$live" -C "$live")
timers_restored=0

restore_timers() {
  if [[ "$timers_restored" == 0 ]]; then
    while IFS= read -r timer; do
      [[ -z "$timer" ]] || systemctl start "$timer"
    done <"$timer_state"
    timers_restored=1
  fi
}

cleanup() {
  restore_timers
  rm -f "$backup_tmp" "$before" "$after" "$timer_state"
}
trap cleanup EXIT

psql_sage() {
  docker exec -i brains-postgres-1 \
    psql -X -v ON_ERROR_STOP=1 -U sage -d memory "$@"
}

scalar() {
  docker exec brains-postgres-1 \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory -c "$1"
}

capture_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(
        public.digest(
          convert_to(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'UTF8'),
          'sha256'
        ),
        'hex'
      )
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
        FROM memory.\"$table\" AS table_row
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(scalar "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
}

qdrant_count() {
  /opt/chat-memory/venv/bin/python - <<'PY'
import os
from rag_engine.qdrant_compat import make_qdrant_client
client = make_qdrant_client(url=os.environ["QDRANT_URL"], timeout=15.0)
try:
    print(
        client.get_collection(
            os.getenv("MEMORY_V1_COLLECTION", "memory_claim_v1")
        ).points_count
    )
finally:
    client.close()
PY
}

set -a
source /opt/chat-memory/.env
source /etc/verbalsage/brains.env
set +a

[[ "$("${git_live[@]}" rev-parse HEAD)" == "$expected_live" ]]
[[ -z "$("${git_live[@]}" status --porcelain)" ]]
"${git_live[@]}" merge-base --is-ancestor "$expected_live" "$target_commit"
[[ "$(sha256sum "$worktree/$migration" | cut -d' ' -f1)" == "$expected_migration_sha" ]]
[[ -f "$worktree/$security_test" && -f "$worktree/$live_probe" ]]

systemctl list-units --type=timer --state=active --no-legend \
  'memory-v1*.timer' \
  | sed -n 's/^[[:space:]]*\([^[:space:]]*\.timer\).*/\1/p' \
  | sort -u >"$timer_state"
while IFS= read -r timer; do
  [[ -z "$timer" ]] || systemctl stop "$timer"
done <"$timer_state"

for _ in $(seq 1 30); do
  running=$(
    systemctl list-units --type=service --state=running,activating \
      --no-legend 'memory-v1*.service' \
      | sed -n 's/^[[:space:]]*\([^[:space:]]*\.service\).*/\1/p' \
      | rg -v '^memory-v1-v5-local-inference-tunnel\.service$' \
      || true
  )
  [[ -z "$running" ]] && break
  sleep 1
done
[[ -z "${running:-}" ]]

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup_tmp"
[[ -s "$backup_tmp" ]]
install -m 0600 -o root -g root "$backup_tmp" "$backup"
pg_restore -l "$backup" >/dev/null
backup_sha=$(sha256sum "$backup" | cut -d' ' -f1)

capture_state "$before"
qdrant_before=$(qdrant_count)

psql_sage <"$worktree/$migration"
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  -v target_owner_user_id="$owner" \
  -v other_owner_user_id="$other_owner" \
  <"$worktree/$security_test"

"${git_live[@]}" merge --ff-only "$target_commit"
systemctl restart brains.service
for _ in $(seq 1 30); do
  if curl -fsS -H "x-vs-service-token: ${VS_SERVICE_TOKEN}" \
      http://127.0.0.1:8088/healthz >/dev/null 2>&1 \
     && curl -fsS -H "x-vs-service-token: ${VS_SERVICE_TOKEN}" \
      http://127.0.0.1:8088/readyz >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS -H "x-vs-service-token: ${VS_SERVICE_TOKEN}" \
  http://127.0.0.1:8088/healthz >/dev/null
curl -fsS -H "x-vs-service-token: ${VS_SERVICE_TOKEN}" \
  http://127.0.0.1:8088/readyz >/dev/null

cd "$live"
/opt/chat-memory/venv/bin/python "$live_probe" \
  --dsn "$POSTGRES_DSN" \
  --qdrant-url "$QDRANT_URL" \
  --collection "${MEMORY_V1_COLLECTION:-memory_claim_v1}" \
  --owner-user-id "$owner"

capture_state "$after"
diff -u "$before" "$after"
qdrant_after=$(qdrant_count)
[[ "$qdrant_before" == "$qdrant_after" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

restore_timers
diff -u "$timer_state" <(
  systemctl list-units --type=timer --state=active --no-legend \
    'memory-v1*.timer' \
    | sed -n 's/^[[:space:]]*\([^[:space:]]*\.timer\).*/\1/p' \
    | sort -u
)

printf 'PRODUCTION_INSTALL=pass\\n'
printf 'HEAD=%s\\n' "$("${git_live[@]}" rev-parse HEAD)"
printf 'BACKUP=%s\\n' "$backup"
printf 'BACKUP_SHA256=%s\\n' "$backup_sha"
printf 'MIGRATION_SHA256=%s\\n' "$expected_migration_sha"
printf 'QDRANT_POINTS=%s\\n' "$qdrant_after"
printf 'TIMERS_RESTORED=%s\\n' "$(wc -l <"$timer_state")"

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies the V5.2 exact-packet/deferral stage guard to a
# disposable production clone, runs rollback-only security tests, and proves
# exact function restoration.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
if [[ -f .env ]]; then
  source .env
elif [[ -f /opt/chat-memory/.env ]]; then
  source /opt/chat-memory/.env
else
  printf '%s\n' 'missing chat-memory .env' >&2
  exit 1
fi
set +a

container=brains-postgres-1
production=memory
clone="memory_v5_2_exact_stage_guard_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
authority=78ca7a3e-e136-535a-9fe6-1d83aefff806
superseded=e21e39bb-20fe-5b95-bc72-77fd774f0faf
migration=ops/sql/20260724_memory_v1_v5_2_exact_packet_stage_guard.sql
rollback=ops/sql/20260724_memory_v1_v5_2_exact_packet_stage_guard_rollback.sql
test_sql=tests/memory_v1_v5_2_exact_packet_stage_guard.sql
backup=$(mktemp /tmp/memory-v5-2-exact-stage-guard.XXXXXX.dump)
before=$(mktemp /tmp/memory-v5-2-exact-stage-guard.XXXXXX.before)
after=$(mktemp /tmp/memory-v5-2-exact-stage-guard.XXXXXX.after)
chmod 0600 "$backup" "$before" "$after"

cleanup() {
  rc=$?
  trap - EXIT
  docker exec "$container" dropdb -U sage --if-exists "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup" "$before" "$after"
  exit "$rc"
}
trap cleanup EXIT

sha_function() {
  local database=$1 signature=$2
  docker exec "$container" psql -U sage -d "$database" -X -Atqc \
    "SELECT encode(public.digest(convert_to(
       pg_get_functiondef('$signature'::regprocedure),'UTF8'
     ),'sha256'),'hex')"
}

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

sha_function "$clone" \
  'memory.guard_v5_2_terminal_evidence_from_stage_v1()' >"$before"

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" -v other_owner="$other" \
  -v authority_packet="$authority" -v superseded_packet="$superseded" \
  <"$test_sql" >/dev/null

[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT position(
     'controlled atom-level admission' in
     pg_get_functiondef(
       'memory.guard_v5_2_terminal_evidence_from_stage_v1()'::regprocedure
     )
   )>0")" == t ]]

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$rollback" >/dev/null
sha_function "$clone" \
  'memory.guard_v5_2_terminal_evidence_from_stage_v1()' >"$after"
cmp -s "$before" "$after"

printf '%s\n' 'memory_v1_v5_2_exact_packet_stage_guard_clone: PASS'

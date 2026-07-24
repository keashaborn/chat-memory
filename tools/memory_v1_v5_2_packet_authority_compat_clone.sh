#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a disposable production clone and verifies
# provenance-leaf selection, ambiguity fail-closed behavior, ACLs, stage
# rejection, idempotent installation, and exact rollback.

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
clone="memory_v5_2_packet_authority_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
evidence=22bd0732-3539-4180-8f89-8f84114131c0
prior=00a75a14-db47-50b7-bb19-40cfa046816c
middle=e21e39bb-20fe-5b95-bc72-77fd774f0faf
authority=78ca7a3e-e136-535a-9fe6-1d83aefff806
migration=ops/sql/20260724_memory_v1_v5_2_packet_authority_compat.sql
rollback=ops/sql/20260724_memory_v1_v5_2_packet_authority_compat_rollback.sql
test_sql=tests/memory_v1_v5_2_packet_authority_compat.sql
worker=scripts/memory_v1_v5_2_local_packet_router.py
python_bin=venv/bin/python
if [[ ! -x "$python_bin" ]]; then
  python_bin=/opt/chat-memory/venv/bin/python
fi
backup=$(mktemp /tmp/memory-v5-2-packet-authority.XXXXXX.dump)
dry=$(mktemp /tmp/memory-v5-2-packet-authority.XXXXXX.json)
before=$(mktemp /tmp/memory-v5-2-packet-authority.XXXXXX.before)
after=$(mktemp /tmp/memory-v5-2-packet-authority.XXXXXX.after)
chmod 0600 "$backup" "$dry" "$before" "$after"

cleanup() {
  rc=$?
  trap - EXIT
  docker exec "$container" dropdb -U sage --if-exists "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup" "$dry" "$before" "$after"
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

{
  sha_function "$clone" \
    'memory.plan_owner_v5_2_local_packet_route_v1(integer)'
  sha_function "$clone" \
    'memory.guard_v5_2_terminal_evidence_from_stage_v1()'
} >"$before"

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" -v other_owner="$other" \
  -v evidence_id="$evidence" -v prior_packet="$prior" \
  -v middle_packet="$middle" -v authority_packet="$authority" \
  <"$test_sql" >/dev/null

clone_dsn=$("$python_bin" -c \
  'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
  "$POSTGRES_DSN" "$clone")
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  "$python_bin" "$worker" --owner-user-id "$owner" >"$dry"
if ! jq -e '
  .apply==false and .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0 and
  (.plans|length)==1 and
  .plans[0].packet_id_sha256==null and
  .plans[0].route=="no_work"
' "$dry" >/dev/null; then
  jq 'del(.plans[]?.artifact_path)' "$dry" >&2
  docker exec "$container" psql -U sage -d "$clone" -X -P pager=off \
    -c \
    "BEGIN;
     SET LOCAL ROLE brains_app;
     SELECT set_config('app.user_id', '$owner', true);
     SELECT * FROM memory.plan_owner_v5_2_local_packet_route_v1(25);
     ROLLBACK;" >&2
  exit 1
fi

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$rollback" >/dev/null
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT to_regprocedure(
     'memory.authoritative_owner_v5_2_packet_id_v1(uuid)'
   ) IS NULL")" == t ]]
{
  sha_function "$clone" \
    'memory.plan_owner_v5_2_local_packet_route_v1(integer)'
  sha_function "$clone" \
    'memory.guard_v5_2_terminal_evidence_from_stage_v1()'
} >"$after"
cmp -s "$before" "$after"

printf '%s\n' 'memory_v1_v5_2_packet_authority_compat_clone: PASS'

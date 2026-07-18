#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Builds a manual-review fixture in an isolated production
# clone, then proves restricted artifact creation, append-only registration,
# replay, account isolation, and zero claim/vector/prompt writes.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_LOCAL_PACKET_ROUTER_CLONE_PORT:-55465}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(docker compose -p memoryv1v5localrouterclone \
  -f docker-compose.ci.yml -f docker-compose.stage-batch-clone.yml)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
foundation=ops/sql/20260718_memory_v1_v5_local_packet_disposition.sql
visibility=ops/sql/20260718_memory_v1_v5_local_packet_disposition_stage_visibility.sql
review_read=ops/sql/20260718_memory_v1_v5_local_packet_review_read.sql
migration=ops/sql/20260718_memory_v1_v5_local_packet_review_artifact.sql
rollback=ops/sql/20260718_memory_v1_v5_local_packet_review_artifact_rollback.sql
sql_test=tests/memory_v1_v5_local_packet_review_artifact.sql
router=scripts/memory_v1_v5_local_packet_router.py
router_test=scripts/memory_v1_v5_local_packet_router_test.py
reviewer=scripts/memory_v1_v5_review_local_packet.py
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=2fa0db2a-0636-5353-9f3f-3f952bd4632b

declare -A expected_sha256=(
  ["$foundation"]="f09e7c2eb6aaa75ea085c352f480bee9daf1258b43bbe3444fd8829c020841c3"
  ["$visibility"]="40969eb59726a5fe7cb8f002972ee8f17fb9021eab2a4b6bde735ec192dc68da"
  ["$review_read"]="ab2fdddb8c398418607e64def9281c2e85dc0967b4128a0c6dcbe3a86dba2bd7"
  ["$migration"]="9a3cf7ade161ee2dbd6a64c2b6fb680c0240373f1277569f9f56c9a83b8789d9"
  ["$rollback"]="b95bbbf8544fb115c953b2135ea11d4aef03a61bed31f953b8f94dae2b4e63e7"
  ["$sql_test"]="3e2cab31f68a40a5d911719b47c5f78e01b4cca911cc09a2a7109cace5a79bc7"
  ["$router"]="08f7434514805a17897447b89235efa6e3495a59795016ce66ce5c5855afa9a5"
  ["$router_test"]="105fcdd63765e9fbd7a073b14aed0ac43927d5a9dab1be044c50f6cad4e88407"
  ["$reviewer"]="49a765ab9e671ee0adf1f74e12f9e696bc82846abbb8e4497b8aec4851edee94"
)

backup=$(mktemp /tmp/memory-v1-v5-local-router.XXXXXX.dump)
table_list=$(mktemp /tmp/memory-v1-v5-local-router-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-local-router-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-local-router-after.XXXXXX.tsv)
dry=$(mktemp /tmp/memory-v1-v5-local-router-dry.XXXXXX.json)
applied=$(mktemp /tmp/memory-v1-v5-local-router-applied.XXXXXX.json)
replayed=$(mktemp /tmp/memory-v1-v5-local-router-replayed.XXXXXX.json)
review_root=$(mktemp -d /tmp/memory-v1-v5-local-router-review.XXXXXX)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$table_list" "$before" "$after" "$dry" "$applied" "$replayed"
  find "$review_root" -type f -delete
  rmdir "$review_root"
}
trap cleanup EXIT
chmod 0600 "$backup" "$table_list" "$before" "$after" "$dry" "$applied" "$replayed"
chmod 0700 "$review_root"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1" | tr -d '[:space:]'
}

query_rows() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

capture_state() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM \"$schema\".\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$table_list"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for required in "${!expected_sha256[@]}"; do
  [[ -f "$repo_root/$required" ]]
  [[ "$(sha256sum "$repo_root/$required" | awk '{print $1}')" \
      == "${expected_sha256[$required]}" ]]
done
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$repo_root/$router_test"

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  'CREATE ROLE memory_evidence_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_review_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_trace_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_intake_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_queue_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_worker_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_retry_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_scheduler_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_inference_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_review_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_disposition_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' \
  'GRANT USAGE ON SCHEMA memory TO brains_app;' \
  'GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app;' \
  'GRANT EXECUTE ON FUNCTION public.unaccent(text) TO brains_app;' \
  'GRANT SELECT ON memory.entity,memory.entity_alias TO brains_app;' \
  | run_sql
run_sql <"$repo_root/$foundation" >/dev/null
run_sql <"$repo_root/$visibility" >/dev/null
run_sql <"$repo_root/$review_read" >/dev/null

run_sql -c "
  ALTER TABLE memory.relational_stage_batch DISABLE TRIGGER USER;
  DELETE FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id=(
      SELECT evidence_id FROM memory.evidence_extraction_packet_v5_local
      WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    );
  ALTER TABLE memory.relational_stage_batch ENABLE TRIGGER USER;
" >/dev/null
[[ "$(scalar "
  SELECT count(*) FROM memory.relational_stage_batch AS stage
  JOIN memory.evidence_extraction_packet_v5_local AS packet
    ON packet.owner_user_id=stage.owner_user_id
   AND packet.evidence_id=stage.evidence_id
  WHERE packet.owner_user_id='$owner'::uuid AND packet.packet_id='$packet'::uuid
")" == 0 ]]

query_rows "
  SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
    AND NOT (table_schema='memory'
      AND table_name='v5_local_packet_review_artifact')
  ORDER BY table_schema,table_name
" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)

run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"
packet_storage_sha256=$(scalar "
  SELECT packet_storage_sha256 FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
")
run_sql \
  -v owner_user_id="$owner" -v other_owner_user_id="$other" \
  -v packet_id="$packet" \
  -v packet_storage_sha256="$packet_storage_sha256" \
  -v auto_link_count=0 -v manual_review_count=2 \
  -v deferred_count=0 -v rejected_count=0 -v blocking_code_count=1 \
  <"$repo_root/$sql_test"
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_review_artifact')" == 0 ]]

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$router" \
  --owner-user-id "$owner" --review-root "$review_root" >"$dry"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
MEMORY_V1_V5_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_local_packet_router_apply_v1 \
  /opt/chat-memory/venv/bin/python "$repo_root/$router" \
  --owner-user-id "$owner" --review-root "$review_root" --apply >"$applied"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
MEMORY_V1_V5_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_local_packet_router_apply_v1 \
  /opt/chat-memory/venv/bin/python "$repo_root/$router" \
  --owner-user-id "$owner" --review-root "$review_root" --apply >"$replayed"

DRY="$dry" APPLIED="$applied" REPLAYED="$replayed" python3 - <<'PY'
import json
import os
from pathlib import Path

dry=json.loads(Path(os.environ['DRY']).read_text())
applied=json.loads(Path(os.environ['APPLIED']).read_text())
replayed=json.loads(Path(os.environ['REPLAYED']).read_text())
assert dry['plans'][0]['route']=='manual_review'
assert applied['outcome']=='manual_review_artifact_ready'
assert applied['write_counts']['packet_route_events']==1
assert applied['write_counts']['restricted_review_artifacts']==2
assert applied['zero_write_replay_proved'] is True
assert replayed['outcome']=='no_work'
assert replayed['write_counts']['packet_route_events']==0
assert 'source_text' not in json.dumps([dry,applied,replayed])
PY
[[ "$(find "$review_root" -maxdepth 1 -type f -perm 0600 | wc -l)" -eq 2 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_local_packet_review_artifact
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
")" == 1 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_local_packet_review_artifact
  WHERE owner_user_id='$other'::uuid
")" == 0 ]]
capture_state "$after"
cmp -s "$before" "$after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT to_regclass(
  'memory.v5_local_packet_review_artifact'
) IS NULL")" == t ]]
capture_state "$after"
cmp -s "$before" "$after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
printf '%s\n' 'memory_v1_v5_local_packet_router_clone: PASS'

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into an isolated PostgreSQL clone and
# proves exact-link-only resolution admission, atomic apply, replay, isolation,
# append-only enforcement, and zero entity/claim/vector influence.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_LOCAL_AUTO_RESOLUTION_CLONE_PORT:-55468}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(docker compose -p memoryv1v5localautoresolutionclone \
  -f docker-compose.ci.yml -f docker-compose.stage-batch-clone.yml)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
owner=11111111-1111-4111-8111-111111111111
other=22222222-2222-4222-8222-222222222222

staging=ops/sql/20260715_memory_v1_relational_staging_v5.sql
writer=ops/sql/20260715_memory_v1_relational_writer_v5.sql
component=ops/sql/20260717_memory_v1_v5_project_components.sql
preflight=ops/sql/20260716_memory_v1_v5_stage_preflight_api.sql
source_compat=ops/sql/20260718_memory_v1_v5_stage_source_id_compat.sql
foundation=ops/sql/20260718_memory_v1_v5_local_packet_disposition.sql
visibility=ops/sql/20260718_memory_v1_v5_local_packet_disposition_stage_visibility.sql
review_read=ops/sql/20260718_memory_v1_v5_local_packet_review_read.sql
artifact=ops/sql/20260718_memory_v1_v5_local_packet_review_artifact.sql
migration=ops/sql/20260718_memory_v1_v5_local_auto_stage_admission.sql
rollback=ops/sql/20260718_memory_v1_v5_local_auto_stage_admission_rollback.sql
sql_test=tests/memory_v1_v5_local_auto_stage_admission.sql
resolution_migration=ops/sql/20260718_memory_v1_v5_local_auto_resolution.sql
resolution_rollback=ops/sql/20260718_memory_v1_v5_local_auto_resolution_rollback.sql
resolution_sql_test=tests/memory_v1_v5_local_auto_resolution.sql
stage_seed=tests/memory_v1_v5_stage_batch_seed.sql
auto_seed=tests/memory_v1_v5_local_auto_stage_seed.sql
fixture=scripts/memory_v1_v5_local_auto_stage_fixture.py
worker=scripts/memory_v1_v5_local_auto_stage.py
worker_test=scripts/memory_v1_v5_local_auto_stage_test.py
resolution_worker=scripts/memory_v1_v5_local_auto_resolution.py
resolution_worker_test=scripts/memory_v1_v5_local_auto_resolution_test.py

declare -A expected_sha256=(
  ["$staging"]="5aea037eb9b2b5fa21de65336550d4e445833f6c128bea53ea88a96642f03678"
  ["$writer"]="111b303332bb16425faeffa593215e0e843e4b4c8518bc33bbb67211f2eafb8f"
  ["$component"]="2af75c7bc45785052c1e9567ea3047f5c61efa8077105d1bdad075e0fd8eb1c4"
  ["$preflight"]="ad540a12f577882e7336f899bd6a15699a8e9f32df5ef54f958b90c8e04a74f4"
  ["$source_compat"]="a45ec57f3c5ecb0838fb019d4d485d25b34110688cc6d4bb25cbea32b17a9383"
  ["$foundation"]="f09e7c2eb6aaa75ea085c352f480bee9daf1258b43bbe3444fd8829c020841c3"
  ["$visibility"]="40969eb59726a5fe7cb8f002972ee8f17fb9021eab2a4b6bde735ec192dc68da"
  ["$review_read"]="ab2fdddb8c398418607e64def9281c2e85dc0967b4128a0c6dcbe3a86dba2bd7"
  ["$artifact"]="9a3cf7ade161ee2dbd6a64c2b6fb680c0240373f1277569f9f56c9a83b8789d9"
  ["$stage_seed"]="ffc2f7a9301f1e23273e647e20848bb6e8b575bc69a46526340398b2d42ef705"
  ["$migration"]="c33463cd68972a22e62514126db363d02294aa0937073ef321f02394c23f9ad8"
  ["$rollback"]="96b25afe673d8fdefb6f9a9dc9fdf7dfc79a436f3822055159e62e01007461af"
  ["$sql_test"]="7cc3c36a4355cd582a386439c5d03c23ee76c4f582006bded8f036ca1206f82c"
  ["$auto_seed"]="de1a50d4bfd35497bc71c3ae0a2ec758f7f08e6d662bd0f29cd09617754a0207"
  ["$fixture"]="370199e031e9f2e5ee923facd72fd3a50640e60d5d33208bae386bd70f381ad0"
  ["$worker"]="704aba13305c7ad0d9636b3757c4823f5ac98ecaa82df4e984fa05c38eb3c145"
  ["$worker_test"]="95d80242145c49fcbaf358fba9abfffa4a77cc75066b9806492456bcb712f1f0"
  ["$resolution_migration"]="923402b80ffee590ecc8ca61a6a39bd05e61791b0bf8eaf1d5e333e7f09be63c"
  ["$resolution_rollback"]="f0465eee496c73eca1f73bbf7404483145f2acf8638dbf4a00f62ba7ce11834b"
  ["$resolution_sql_test"]="bbee2d7e89038edd32fa301585550563e2305db05d1af13f46b71c660c4c0296"
  ["$resolution_worker"]="71d6defed5727c40acb136b839d313f51a4394e391fa46915e2e663ec267b548"
  ["$resolution_worker_test"]="ce4ba77fa3971c44f2f751b86aaa95e53303c83f4f8530f050aeb060926e9f0f"
)

backup=$(mktemp /tmp/memory-v1-v5-local-auto-resolution.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-v1-v5-local-auto-resolution.XXXXXX)
review_root="$work/reviews"
metadata="$work/metadata.json"
dry="$work/dry.json"
tampered="$work/tampered.json"
applied="$work/applied.json"
replayed="$work/replayed.json"
other_dry="$work/other.json"
resolution_dry="$work/resolution-dry.json"
resolution_applied="$work/resolution-applied.json"
resolution_replayed="$work/resolution-replayed.json"
resolution_other="$work/resolution-other.json"
table_list="$work/table-list.tsv"
protected_before="$work/protected-before.tsv"
protected_after="$work/protected-after.tsv"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup"
  rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup"

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

capture_protected() {
  local output=$1 table has_owner predicate state
  : >"$output"
  while IFS= read -r table; do
    has_owner=$(scalar "SELECT EXISTS(
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='memory' AND table_name='$table'
        AND column_name='owner_user_id')")
    predicate=
    [[ "$has_owner" == t ]] && predicate="WHERE owner_user_id<>'$owner'::uuid"
    state=$(scalar "
      SELECT count(*)::text || ':' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value $predicate
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
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
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$repo_root/$worker_test"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$resolution_worker_test"

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

run_sql <"$repo_root/$staging" >/dev/null
run_sql <"$repo_root/$writer" >/dev/null
run_sql <"$repo_root/$component" >/dev/null
run_sql <"$repo_root/$preflight" >/dev/null
run_sql <"$repo_root/$source_compat" >/dev/null
run_sql <"$repo_root/$foundation" >/dev/null
run_sql <"$repo_root/$visibility" >/dev/null
run_sql <"$repo_root/$review_read" >/dev/null
run_sql <"$repo_root/$artifact" >/dev/null
run_sql <"$repo_root/$stage_seed" >/dev/null

mkdir -m 0700 "$review_root"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$fixture" --review-root "$review_root" --output "$metadata"

run_sql <"$repo_root/$migration" >/dev/null
run_sql <"$repo_root/$migration" >/dev/null
run_sql <"$repo_root/$resolution_migration" >/dev/null
run_sql <"$repo_root/$resolution_migration" >/dev/null

args=(
  -v owner_user_id="$(jq -r .owner_user_id "$metadata")"
  -v packet_id="$(jq -r .packet_id "$metadata")"
  -v job_id="$(jq -r .job_id "$metadata")"
  -v terminal_id="$(jq -r .terminal_id "$metadata")"
  -v artifact_id="$(jq -r .artifact_id "$metadata")"
  -v artifact_operation_id="$(jq -r .artifact_operation_id "$metadata")"
  -v review_id="$(jq -r .review_id "$metadata")"
  -v request_id="$(jq -r .request_id "$metadata")"
  -v evidence_id="$(jq -r .evidence_id "$metadata")"
  -v evidence_content_sha256="$(jq -r .evidence_content_sha256 "$metadata")"
  -v normalized_packet="$(jq -c .normalized_packet "$metadata")"
  -v validator_packet_sha256="$(jq -r .validator_packet_sha256 "$metadata")"
  -v review_report_sha256="$(jq -r .review_report_sha256 "$metadata")"
  -v stage_bundle_sha256="$(jq -r .stage_bundle_sha256 "$metadata")"
  -v repository_commit="$(jq -r .repository_commit "$metadata")"
)
run_sql "${args[@]}" <"$repo_root/$auto_seed" >/dev/null
packet_storage_sha256=$(scalar "SELECT packet_storage_sha256
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND packet_id='$(jq -r .packet_id "$metadata")'::uuid")
run_sql \
  -v owner_user_id="$owner" -v other_owner_user_id="$other" \
  -v artifact_id="$(jq -r .artifact_id "$metadata")" \
  -v review_report_sha256="$(jq -r .review_report_sha256 "$metadata")" \
  -v stage_bundle_sha256="$(jq -r .stage_bundle_sha256 "$metadata")" \
  -v packet_storage_sha256="$packet_storage_sha256" \
  <"$repo_root/$sql_test" >/dev/null
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_stage_admission')" == 0 ]]
run_sql -v owner_user_id="$owner" \
  <"$repo_root/$resolution_sql_test" >/dev/null
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_auto_resolution_admission')" == 0 ]]

run_sql <"$repo_root/$resolution_rollback" >/dev/null
[[ "$(scalar "SELECT to_regclass('memory.v5_local_auto_resolution_admission') IS NULL")" == t ]]
run_sql <"$repo_root/$rollback" >/dev/null
[[ "$(scalar "SELECT to_regclass('memory.v5_local_packet_stage_admission') IS NULL")" == t ]]
run_sql <"$repo_root/$migration" >/dev/null
run_sql <"$repo_root/$resolution_migration" >/dev/null

query_rows "SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name" >"$table_list"
capture_protected "$protected_before"
qdrant_before=$(qdrant_signature)
claim_before=$(scalar 'SELECT count(*) FROM memory.claim')
projection_before=$(scalar 'SELECT count(*) FROM memory.projection_apply_event')

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$owner" --review-root "$review_root" >"$dry"
jq -e '.apply==false and .plans[0].route=="auto_stage_eligible" and
  .database_writes==0 and .qdrant_writes==0 and .external_model_calls==0' \
  "$dry" >/dev/null

bundle=$(jq -r .stage_bundle "$metadata")
cp "$bundle" "$bundle.original"
printf ' ' >>"$bundle"
if MEMORY_V1_V5_LOCAL_AUTO_STAGE_APPLY=memory_v1_v5_local_auto_stage_apply_v1 \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$owner" --review-root "$review_root" --apply \
  >"$tampered" 2>/dev/null; then
  echo 'tampered local stage bundle unexpectedly applied' >&2
  exit 1
fi
mv "$bundle.original" "$bundle"
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_stage_admission')" == 0 ]]

MEMORY_V1_V5_LOCAL_AUTO_STAGE_APPLY=memory_v1_v5_local_auto_stage_apply_v1 \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$owner" --review-root "$review_root" --apply >"$applied"
MEMORY_V1_V5_LOCAL_AUTO_STAGE_APPLY=memory_v1_v5_local_auto_stage_apply_v1 \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$owner" --review-root "$review_root" --apply >"$replayed"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$other" --review-root "$review_root" >"$other_dry"

jq -e '.outcome=="staged_for_entity_resolution_review" and
  .database_rows_created==8 and .write_counts.admission==1 and
  .write_counts.claims==0 and .write_counts.projections==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .zero_write_replay_proved==true and .external_model_calls==0' \
  "$applied" >/dev/null
jq -e '.outcome=="no_work" and .database_rows_created==0' "$replayed" >/dev/null
jq -e '.apply==false and .plans[0].route=="no_work"' "$other_dry" >/dev/null

[[ "$(scalar "SELECT (
  (SELECT count(*) FROM memory.v5_local_packet_stage_admission
    WHERE owner_user_id='$owner'::uuid)=1
  AND (SELECT count(*) FROM memory.relational_stage_batch
    WHERE owner_user_id='$owner'::uuid)=1
  AND (SELECT count(*) FROM memory.relational_operation_request
    WHERE owner_user_id='$owner'::uuid AND operation='stage_packet')=1
  AND (SELECT count(*) FROM memory.entity_mention
    WHERE owner_user_id='$owner'::uuid)=1
  AND (SELECT count(*) FROM memory.entity_resolution_plan
    WHERE owner_user_id='$owner'::uuid)=1
  AND (SELECT count(*) FROM memory.entity_resolution_candidate
    WHERE owner_user_id='$owner'::uuid)=1
  AND (SELECT count(*) FROM memory.observation
    WHERE owner_user_id='$owner'::uuid)=1
  AND (SELECT count(*) FROM memory.observation_temporal
    WHERE owner_user_id='$owner'::uuid)=1
  AND (SELECT count(*) FROM memory.entity_resolution_apply
    WHERE owner_user_id='$owner'::uuid)=0
  AND (SELECT count(*) FROM memory.projection_plan
    WHERE owner_user_id='$owner'::uuid)=0
)::int")" == 1 ]]

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$resolution_worker" \
  --owner-user-id "$owner" >"$resolution_dry"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$resolution_worker" \
  --owner-user-id "$other" >"$resolution_other"
jq -e '.apply==false and .plans[0].route=="auto_apply_exact_existing_link" and
  .database_writes==0 and .external_model_calls==0 and .qdrant_writes==0' \
  "$resolution_dry" >/dev/null
jq -e '.apply==false and .plans[0].route=="no_work"' \
  "$resolution_other" >/dev/null

entity_before=$(scalar "SELECT count(*) FROM memory.entity")
review_before=$(scalar "SELECT count(*) FROM memory.entity_resolution_review")
MEMORY_V1_V5_LOCAL_AUTO_RESOLUTION_APPLY=memory_v1_v5_local_auto_resolution_apply_v1 \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$resolution_worker" \
  --owner-user-id "$owner" --apply >"$resolution_applied"
MEMORY_V1_V5_LOCAL_AUTO_RESOLUTION_APPLY=memory_v1_v5_local_auto_resolution_apply_v1 \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$resolution_worker" \
  --owner-user-id "$owner" --apply >"$resolution_replayed"

jq -e '.outcome=="exact_existing_link_applied" and
  .database_rows_created==4 and .bindings_created==1 and
  .write_counts.resolution_admission==1 and
  .write_counts.resolution_apply_and_request==2 and
  .write_counts.entity_bindings==1 and .write_counts.entities==0 and
  .write_counts.claims==0 and .write_counts.projections==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .zero_write_replay_proved==true and .external_model_calls==0' \
  "$resolution_applied" >/dev/null
jq -e '.outcome=="no_work" and .database_rows_created==0' \
  "$resolution_replayed" >/dev/null
[[ "$(scalar "SELECT count(*) FROM memory.entity")" == "$entity_before" ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_review")" == "$review_before" ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_auto_resolution_admission
  WHERE owner_user_id='$owner'::uuid")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$owner'::uuid")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.observation_entity_binding
  WHERE owner_user_id='$owner'::uuid")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_auto_resolution_admission
  WHERE owner_user_id='$other'::uuid")" == 0 ]]

capture_protected "$protected_after"
cmp -s "$protected_before" "$protected_after"
[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$claim_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.projection_apply_event')" == "$projection_before" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

if run_sql <"$repo_root/$resolution_rollback" >/dev/null 2>&1; then
  echo 'nonempty auto-resolution admission rollback unexpectedly succeeded' >&2
  exit 1
fi
if run_sql <"$repo_root/$rollback" >/dev/null 2>&1; then
  echo 'nonempty auto-stage admission rollback unexpectedly succeeded' >&2
  exit 1
fi

printf '%s\n' 'memory_v1_v5_local_auto_resolution_clone: PASS'

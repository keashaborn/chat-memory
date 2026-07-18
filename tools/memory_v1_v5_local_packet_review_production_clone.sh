#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into an isolated clone and verifies
# the local-packet review API and reviewer without production writes.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_LOCAL_REVIEW_CLONE_PORT:-55460}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v5localreviewclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260718_memory_v1_v5_local_packet_review_read.sql
rollback=ops/sql/20260718_memory_v1_v5_local_packet_review_read_rollback.sql
test_sql=tests/memory_v1_v5_local_packet_review_read.sql
reviewer=scripts/memory_v1_v5_review_local_packet.py
unit_test=scripts/memory_v1_v5_review_local_packet_test.py
migration_sha=ab2fdddb8c398418607e64def9281c2e85dc0967b4128a0c6dcbe3a86dba2bd7
rollback_sha=42347434ad10ce1de3b7c6e019b53bc6cdbd07f4a29ffe66cc08fa5cd1c232b8
test_sha=eb890f851a6c1c17b8172b4f237e632ccc3082ead8544ad117fb00cb0dbf05cd
reviewer_sha=49a765ab9e671ee0adf1f74e12f9e696bc82846abbb8e4497b8aec4851edee94
unit_test_sha=ae43f75c0a835d4b885406ca74f985bfc36d1afb7abb6692a7144d404f6f4166
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
target_packet=2fa0db2a-0636-5353-9f3f-3f952bd4632b
other_owner=557ea042-cb82-48f8-9429-472e96c957ef

backup=$(mktemp /tmp/memory-v1-v5-local-review.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-v5-local-review-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-local-review-after.XXXXXX.tsv)
test_log=$(mktemp /tmp/memory-v1-v5-local-review-test.XXXXXX.log)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$before" "$after" "$test_log"
}
trap cleanup EXIT
chmod 0600 "$backup" "$before" "$after" "$test_log"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1" | tr -d '[:space:]'
}

capture_state() {
  local output=$1
  : >"$output"
  for table in evidence evidence_extraction_job \
    evidence_extraction_packet_v5_local relational_stage_batch \
    entity entity_alias; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done
}

for required in "$migration" "$rollback" "$test_sql" "$reviewer" "$unit_test"; do
  [[ -f "$repo_root/$required" ]]
done
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == "$migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | cut -d' ' -f1)" == "$rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$test_sha" ]]
[[ "$(sha256sum "$repo_root/$reviewer" | cut -d' ' -f1)" == "$reviewer_sha" ]]
[[ "$(sha256sum "$repo_root/$unit_test" | cut -d' ' -f1)" == "$unit_test_sha" ]]

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$unit_test"

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]

"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  'CREATE ROLE memory_evidence_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_queue_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_retry_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_extraction_worker_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_intake_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_review_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_extraction_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_trace_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_local_inference_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' \
  'GRANT USAGE ON SCHEMA memory TO brains_app;' \
  'GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app;' \
  | run_sql

[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$target_owner'::uuid
    AND packet_id='$target_packet'::uuid")" == 1 ]]
capture_state "$before"

run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"
run_sql -v target_owner="$target_owner" -v target_packet="$target_packet" \
  -v other_owner="$other_owner" <"$repo_root/$test_sql" >"$test_log"
grep -q 'memory_v1_v5_local_packet_review_read: PASS' "$test_log"
! grep -q '^ERROR:' "$test_log"

[[ "$(scalar "SELECT (
  to_regprocedure(
    'memory.read_owner_v5_local_packet_review_v1(uuid)'
  ) IS NOT NULL
  AND pg_get_userbyid((
    SELECT proowner FROM pg_proc
    WHERE oid='memory.read_owner_v5_local_packet_review_v1(uuid)'::regprocedure
  ))='memory_v5_local_review_reader'
  AND has_function_privilege(
    'brains_app',
    'memory.read_owner_v5_local_packet_review_v1(uuid)',
    'EXECUTE'
  )
  AND NOT has_table_privilege(
    'brains_app','memory.evidence_extraction_packet_v5_local','SELECT'
  )
)::integer")" == 1 ]]

capture_state "$after"
cmp -s "$before" "$after"

run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT (
  to_regprocedure(
    'memory.read_owner_v5_local_packet_review_v1(uuid)'
  ) IS NULL
  AND to_regrole('memory_v5_local_review_reader') IS NULL
)::integer")" == 1 ]]
capture_state "$after"
cmp -s "$before" "$after"

printf '%s\n' 'memory_v1_v5_local_packet_review_production_clone: PASS'

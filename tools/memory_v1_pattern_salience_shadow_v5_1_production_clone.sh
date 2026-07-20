#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores an owner-preserving production clone, installs
# the restricted shadow-input API, and proves deterministic zero-write output.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
port=${MEMORY_V1_PATTERN_SALIENCE_SHADOW_CLONE_PORT:-55474}
project=memoryv1patternsalienceshadowclone
compose=(docker compose -p "$project" -f docker-compose.ci.yml \
  -f docker-compose.stage-batch-clone.yml)
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
migration=ops/sql/20260720_memory_v1_pattern_salience_shadow_input_v5_1.sql
rollback=ops/sql/20260720_memory_v1_pattern_salience_shadow_input_v5_1_rollback.sql
sql_test=tests/memory_v1_pattern_salience_shadow_input_v5_1.sql
worker=scripts/memory_v1_pattern_salience_shadow_v5_1.py
python_test=tests/test_memory_v1_pattern_salience_shadow_v5_1.py
migration_sha=c8b2eeb30c58a624cc7ba247af350530aa23e53195bfa728fd68de6ec4e98f96
rollback_sha=0d793328f1a82070a32c7bce9b321899f02c34fc376a47772216ea232601aba1
sql_test_sha=44787c4cb821400434191cdfacf0179fedd0a9071c96685f63a03efd1f3443d7
worker_sha=16c5cb75589804c41f8286ab1bb28e06b54f0b96ec37298ceb4e421490768e52
python_test_sha=d94d7e3c728aed6b4e84f1c92c1f35712fabe597089bb53827041c63ae5e6342

backup=$(mktemp /tmp/memory-v1-pattern-salience-shadow.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-v1-pattern-salience-shadow.XXXXXX)
table_list="$work/tables.tsv"
before_rows="$work/before-rows.tsv"
after_rows="$work/after-rows.tsv"
before_schema="$work/before-schema.sql"
after_schema="$work/after-schema.sql"
shadow_report="$work/shadow-report.json"

cleanup() {
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" down -v \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup"

run_sql() {
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
    psql -X -v ON_ERROR_STOP=1 -U sage -d memory "$@"
}

scalar() {
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory -c "$1" \
    | tr -d '[:space:]'
}

capture_rows() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "SELECT count(*)::text || ':' ||
      encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
        ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM \"$schema\".\"$table\" AS value) rows")
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

for pair in \
  "$migration:$migration_sha" \
  "$rollback:$rollback_sha" \
  "$sql_test:$sql_test_sha" \
  "$worker:$worker_sha" \
  "$python_test:$python_test_sha"; do
  path=${pair%%:*}
  expected=${pair#*:}
  [[ -f "$path" ]]
  [[ "$(sha256sum "$path" | cut -d' ' -f1)" == "$expected" ]]
done

PYTHONPATH="$repo_root" venv/bin/python -m unittest \
  tests/test_memory_v1_pattern_salience_shadow_v5_1.py \
  tests/test_memory_v1_pattern_salience_policy_v5_1.py \
  tests/test_memory_v1_pattern_review_contract_v5_1.py

docker exec brains-postgres-1 pg_dump -U sage -d memory -Fc >"$backup"
[[ -s "$backup" ]]
MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" up -d --wait postgres
{
  printf '%s\n' "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;"
  docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c \
    "SELECT format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;',rolname) FROM pg_roles WHERE rolname LIKE 'memory%' ORDER BY rolname;"
} | run_sql >/dev/null
MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
  pg_restore -U sage -d memory --clean --if-exists <"$backup"
baseline_loader=$(scalar "SELECT to_regprocedure('memory.load_pattern_salience_shadow_inputs_v5_1(integer,integer)') IS NOT NULL")

run_sql -At -c "SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables WHERE table_type='BASE TABLE'
    AND table_schema IN ('memory','public')
  ORDER BY table_schema,table_name" >"$table_list"
capture_rows "$before_rows"
MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
  pg_dump -U sage -d memory --schema-only --no-owner --no-privileges \
  >"$before_schema"
qdrant_before=$(qdrant_signature)

run_sql <"$migration" >/dev/null
run_sql <"$migration" >/dev/null
run_sql <"$sql_test" >/dev/null

POSTGRES_DSN="postgresql://brains_app:clone_only_brains_password@127.0.0.1:$port/memory" \
PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner" --as-of-date 2026-07-20 \
  --output "$shadow_report" >/dev/null

jq -e '
  .contract_version=="memory_v1_pattern_salience_shadow_report_v5_1" and
  .owner_user_id_sha256=="9f5d6523a63c8ff7391ecf514fab572af530874832cddc9f56eb3db93cf45b15" and
  (.pattern_proposals|length)==0 and
  (.target_snapshot_candidates|length)>0 and
  .proof.database_writes==0 and
  .proof.external_model_calls==0 and
  .proof.qdrant_writes==0 and
  .proof.prompt_influence==0 and
  .proof.literal_pattern_identity_fail_closed==true
' "$shadow_report" >/dev/null

capture_rows "$after_rows"
cmp -s "$before_rows" "$after_rows"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

if [[ "$baseline_loader" == f ]]; then
  run_sql <"$rollback" >/dev/null
  [[ "$(scalar "SELECT to_regprocedure('memory.load_pattern_salience_shadow_inputs_v5_1(integer,integer)') IS NULL")" == t ]]
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
    pg_dump -U sage -d memory --schema-only --no-owner --no-privileges \
    >"$after_schema"
  if ! diff -I '^\\restrict ' -I '^\\unrestrict ' -q \
    "$before_schema" "$after_schema" >/dev/null; then
    diff -I '^\\restrict ' -I '^\\unrestrict ' -u \
      "$before_schema" "$after_schema" | sed -n '1,240p' >&2
    printf '%s\n' 'shadow input rollback did not restore clone schema' >&2
    exit 1
  fi
else
  [[ "$(scalar "SELECT pg_get_userbyid(proowner)='memory_v5_epistemic_writer' FROM pg_proc WHERE oid='memory.load_pattern_salience_shadow_inputs_v5_1(integer,integer)'::regprocedure")" == t ]]
fi

printf '%s\n' 'memory_v1_pattern_salience_shadow_v5_1_production_clone: PASS'

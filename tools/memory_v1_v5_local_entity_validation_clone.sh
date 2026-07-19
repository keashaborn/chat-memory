#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into an isolated PostgreSQL clone,
# exercises private new-entity validation and deterministic staging, and proves
# replay, owner isolation, downstream entailment admission, and no Qdrant write.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
port=${MEMORY_V1_V5_LOCAL_ENTITY_VALIDATION_CLONE_PORT:-55471}
project=memoryv1v5localentityvalidationclone
compose=(docker compose -p "$project" -f docker-compose.ci.yml \
  -f docker-compose.stage-batch-clone.yml)
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
entity_migration=ops/sql/20260719_memory_v1_v5_local_entity_validation.sql
entity_rollback=ops/sql/20260719_memory_v1_v5_local_entity_validation_rollback.sql
entailment_migration=ops/sql/20260719_memory_v1_v5_local_entailment_validated_stage.sql
entailment_rollback=ops/sql/20260719_memory_v1_v5_local_entailment_validated_stage_rollback.sql
sql_test=tests/memory_v1_v5_local_entity_validation.sql
provider=scripts/memory_v1_v5_local_entity_validation_provider.py
worker=scripts/memory_v1_v5_local_entity_validation.py
provider_test=scripts/memory_v1_v5_local_entity_validation_test.py
scheduler_test=scripts/memory_v1_v5_local_entity_validation_scheduler_test.py
smoke=scripts/memory_v1_v5_local_entity_validation_smoke.py
entailment_worker=scripts/memory_v1_v5_local_entailment_scheduler.py
service=ops/systemd/memory-v1-v5-local-entity-validation.service
timer=ops/systemd/memory-v1-v5-local-entity-validation.timer

backup=$(mktemp /tmp/memory-v1-v5-local-entity-validation.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-v1-v5-local-entity-validation.XXXXXX)
table_list="$work/tables.tsv"
before="$work/non-target-before.tsv"
after="$work/non-target-after.tsv"
owner_dry="$work/owner-dry.json"
other_dry="$work/other-dry.json"
first_raw="$work/first.raw"
first_json="$work/first.json"
second_raw="$work/second.raw"
second_json="$work/second.json"
entailment_dry="$work/entailment-dry.json"

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

capture_non_target_state() {
  local output=$1 schema table scoped state
  : >"$output"
  while IFS=$'\t' read -r schema table scoped; do
    if [[ "$scoped" == t ]]; then
      state=$(scalar "SELECT count(*)::text || ':' ||
        encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
          ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
        FROM (SELECT to_jsonb(value)::text AS row_json
          FROM \"$schema\".\"$table\" AS value
          WHERE owner_user_id <> '$owner'::uuid) rows")
    else
      state=$(scalar "SELECT count(*)::text || ':' ||
        encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
          ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
        FROM (SELECT to_jsonb(value)::text AS row_json
          FROM \"$schema\".\"$table\" AS value) rows")
    fi
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

for required in "$entity_migration" "$entity_rollback" \
  "$entailment_migration" "$entailment_rollback" "$sql_test" "$provider" \
  "$worker" "$provider_test" "$scheduler_test" "$smoke" \
  "$entailment_worker" "$service" "$timer"; do
  [[ -f "$required" ]]
done
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$provider_test"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$scheduler_test"
python3 -m py_compile "$provider" "$worker" "$smoke"
systemd-analyze verify "$service" "$timer"

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" up -d --wait postgres
{
  printf '%s\n' "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;"
  docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c \
    "SELECT format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;',rolname) FROM pg_roles WHERE rolname LIKE 'memory%' ORDER BY rolname;"
} | run_sql >/dev/null
MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
  pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' 'GRANT USAGE ON SCHEMA memory TO brains_app;' | run_sql >/dev/null

run_sql <"$entity_migration" >/dev/null
run_sql <"$entailment_migration" >/dev/null
run_sql <"$entity_migration" >/dev/null
run_sql <"$entailment_migration" >/dev/null
run_sql -v owner_user_id="$owner" <"$sql_test" >/dev/null

# Rollback is verified before the clone receives any intentional test writes.
run_sql <"$entailment_rollback" >/dev/null
run_sql <"$entity_rollback" >/dev/null
[[ "$(scalar "SELECT to_regclass('memory.v5_local_entity_validation_assessment') IS NULL")" == t ]]
[[ "$(scalar "SELECT to_regclass('memory.v5_local_validated_stage_admission') IS NULL")" == t ]]
run_sql <"$entity_migration" >/dev/null
run_sql <"$entailment_migration" >/dev/null
run_sql -v owner_user_id="$owner" <"$sql_test" >/dev/null

run_sql -At -c "SELECT t.table_schema || E'\\t' || t.table_name || E'\\t' ||
    EXISTS (SELECT 1 FROM information_schema.columns c
      WHERE c.table_schema=t.table_schema AND c.table_name=t.table_name
        AND c.column_name='owner_user_id')
  FROM information_schema.tables t
  WHERE t.table_type='BASE TABLE' AND t.table_schema IN ('memory','public')
  ORDER BY t.table_schema,t.table_name" >"$table_list"
capture_non_target_state "$before"
qdrant_before=$(qdrant_signature)

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" --owner-user-id "$owner" \
  >"$owner_dry"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" --owner-user-id "$other" \
  >"$other_dry"
jq -e '.apply==false and .plans[0].route=="validate_new_entity" and
  .database_writes==0 and .local_model_calls==0 and
  .external_model_calls==0 and .qdrant_writes==0 and
  .claim_writes==0 and .prompt_influence==0' "$owner_dry" >/dev/null
jq -e '.apply==false and .plans[0].route=="no_work" and
  .database_writes==0 and .local_model_calls==0' "$other_dry" >/dev/null

sudo -n systemd-run --wait --pipe --collect \
  --unit="memory-v1-v5-local-entity-validation-clone-first-$$" \
  -p User=ubuntu -p Group=ubuntu -p WorkingDirectory=/opt/chat-memory \
  -p Environment=PYTHONPATH=/opt/chat-memory \
  -p Environment=POSTGRES_DSN="$dsn" \
  -p Environment=MEMORY_V1_V5_LOCAL_ENTITY_VALIDATION_APPLY=memory_v1_v5_local_entity_validation_apply_v1 \
  -p LoadCredential=local_api_key:/etc/memory-v1-local-inference/api-key \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$owner" --apply >"$first_raw"
tr -d '\r' <"$first_raw" | grep '^{' | tail -n 1 >"$first_json"
jq -e '.outcome=="entity_validation_recorded" and
  .governed_decision=="accepted" and .database_rows_created==1 and
  .local_model_calls==1 and .external_model_calls==0 and
  .entity_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .prompt_influence==0 and .zero_write_replay_proved==true' \
  "$first_json" >/dev/null

sudo -n systemd-run --wait --pipe --collect \
  --unit="memory-v1-v5-local-entity-validation-clone-second-$$" \
  -p User=ubuntu -p Group=ubuntu -p WorkingDirectory=/opt/chat-memory \
  -p Environment=PYTHONPATH=/opt/chat-memory \
  -p Environment=POSTGRES_DSN="$dsn" \
  -p Environment=MEMORY_V1_V5_LOCAL_ENTITY_VALIDATION_APPLY=memory_v1_v5_local_entity_validation_apply_v1 \
  -p LoadCredential=local_api_key:/etc/memory-v1-local-inference/api-key \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --owner-user-id "$owner" --apply >"$second_raw"
tr -d '\r' <"$second_raw" | grep '^{' | tail -n 1 >"$second_json"
jq -e '.outcome=="validated_entity_staged_and_resolved" and
  .database_rows_created==20 and .bindings_created==1 and
  .local_model_calls==0 and .external_model_calls==0 and
  .entity_writes==1 and .claim_writes==0 and .qdrant_writes==0 and
  .prompt_influence==0 and .zero_write_replay_proved==true' \
  "$second_json" >/dev/null

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$entailment_worker" \
  --owner-user-id "$owner" >"$entailment_dry"
jq -e '.apply==false and
  .plans[0].route=="private_entailment_assessment" and
  .database_writes==0 and .local_model_calls==0 and
  .external_model_calls==0 and .qdrant_writes==0 and
  .claim_writes==0 and .projection_writes==0 and
  .prompt_influence==0' "$entailment_dry" >/dev/null

[[ "$(scalar "SELECT count(*) FROM memory.v5_local_entity_validation_assessment WHERE owner_user_id='$owner'::uuid")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_entity_validation_assessment WHERE owner_user_id<>'$owner'::uuid")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_validated_stage_admission WHERE owner_user_id='$owner'::uuid")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_validated_stage_admission WHERE owner_user_id<>'$owner'::uuid")" == 0 ]]
capture_non_target_state "$after"
cmp -s "$before" "$after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf '%s\n' 'memory_v1_v5_local_entity_validation_clone: PASS'

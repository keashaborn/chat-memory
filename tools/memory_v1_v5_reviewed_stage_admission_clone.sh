#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Tests reviewed-stage admission and its private-entailment
# handoff on an isolated production clone. Production is read-only.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source .env
set +a

port=${MEMORY_V1_V5_REVIEWED_STAGE_CLONE_PORT:-55474}
project="memoryv1v5reviewedstage${$}"
compose=(docker compose -p "$project" -f docker-compose.ci.yml \
  -f docker-compose.stage-batch-clone.yml)
owner=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
other=557ea042-cb82-48f8-9429-472e96c957ef
migration=ops/sql/20260719_memory_v1_v5_reviewed_stage_admission.sql
rollback=ops/sql/20260719_memory_v1_v5_reviewed_stage_admission_rollback.sql
worker=scripts/memory_v1_v5_reviewed_stage_admission.py
entailment=scripts/memory_v1_v5_local_entailment_scheduler.py

backup=$(mktemp /tmp/memory-v1-v5-reviewed-stage.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-v1-v5-reviewed-stage.XXXXXX)
dry="$work/dry.json"
other_dry="$work/other.json"
applied="$work/applied.json"
entailment_dry="$work/entailment.json"
chmod 0600 "$backup"
phase=initialization

cleanup() {
  rc=$?
  if [[ "$rc" -ne 0 ]]; then
    printf 'memory_v1_v5_reviewed_stage_admission_clone: FAIL phase=%s exit=%s\n' \
      "$phase" "$rc" >&2
    for diagnostic in "$dry" "$other_dry" "$applied" "$entailment_dry"; do
      if [[ -s "$diagnostic" ]]; then
        jq -c . "$diagnostic" >&2 || true
      fi
    done
  fi
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" down -v \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  rm -rf "$work"
  exit "$rc"
}
trap cleanup EXIT

run_sql() {
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
    psql -X -v ON_ERROR_STOP=1 -U sage -d memory "$@"
}

scalar() {
  MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d memory -c "$1" \
    | tr -d '[:space:]'
}

for required in "$migration" "$rollback" "$worker" "$entailment"; do
  [[ -f "$required" ]]
done
phase=compile
python3 -m py_compile "$worker"
phase=backup
docker exec brains-postgres-1 pg_dump -U sage -d memory -Fc >"$backup"
[[ -s "$backup" ]]
phase=start_clone
MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" up -d --wait postgres
phase=restore_roles
{
  printf '%s\n' \
    "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;"
  docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c \
    "SELECT format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;',rolname) FROM pg_roles WHERE rolname LIKE 'memory%' ORDER BY rolname;"
} | run_sql >/dev/null
phase=restore_database
MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port" "${compose[@]}" exec -T postgres \
  pg_restore -U sage -d memory --clean --if-exists <"$backup"

clone_dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
phase=install_migration
run_sql <"$migration" >/dev/null
phase=idempotent_migration
run_sql <"$migration" >/dev/null

phase=direct_plan_probe
printf "BEGIN; SET LOCAL SESSION AUTHORIZATION brains_app; SELECT set_config('app.user_id','%s',true); SELECT * FROM memory.plan_owner_v5_reviewed_stage_admission_v1(1); ROLLBACK;\n" \
  "$owner" | run_sql -A -t >/dev/null

phase=dry_owner
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner" >"$dry"
jq -e '
  .apply==false and .database_writes==0 and
  .plans[0].route=="admit_reviewed_stage" and
  .external_model_calls==0 and .local_model_calls==0 and
  .claims==0 and .qdrant==0 and .prompt_influence==0
' "$dry" >/dev/null
phase=dry_other
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$other" >"$other_dry"
jq -e '.plans[0].route=="no_work" and .database_writes==0' \
  "$other_dry" >/dev/null
phase=security_checks
if scalar "BEGIN; SET LOCAL SESSION AUTHORIZATION brains_app; SELECT set_config('app.user_id','$other',true); SELECT count(*) FROM memory.v5_reviewed_stage_source_v1('f2c93b4c-e354-58af-b2db-5af3ce37e89d'::uuid); ROLLBACK;" >/dev/null 2>&1; then
  echo 'internal reviewed-stage source helper is directly executable' >&2
  exit 1
fi
[[ "$(scalar "SELECT NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls AND NOT rolinherit FROM pg_roles WHERE rolname='memory_v5_reviewed_stage_maintainer'")" == t ]]

phase=rollback
run_sql <"$rollback" >/dev/null
[[ "$(scalar "SELECT to_regclass('memory.v5_local_reviewed_stage_admission') IS NULL")" == t ]]
[[ "$(scalar "SELECT position('reviewed_entity_stage' IN pg_get_constraintdef(oid))=0 FROM pg_constraint WHERE conrelid='memory.v5_local_packet_stage_admission'::regclass AND conname='v5_local_packet_stage_admission_policy_decision_check'")" == t ]]
phase=reinstall
run_sql <"$migration" >/dev/null

phase=apply
MEMORY_V1_V5_REVIEWED_STAGE_ADMISSION_APPLY=memory_v1_v5_reviewed_stage_admission_apply_v1 \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner" --apply >"$applied"
jq -e '
  .apply==true and .outcome=="reviewed_stage_admitted" and
  .database_rows_created==2 and .zero_write_replay_proved==true and
  .write_counts.packet_stage_admission==1 and
  .write_counts.reviewed_stage_audit==1 and
  .claims==0 and .qdrant==0 and .prompt_influence==0
' "$applied" >/dev/null
phase=apply_checks
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_reviewed_stage_admission WHERE owner_user_id='$owner'::uuid AND decision='reviewed_entity_stage' AND resolution_count=2 AND approved_review_count=1 AND binding_count=4")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_packet_stage_admission WHERE owner_user_id='$owner'::uuid AND decision='reviewed_entity_stage'")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_reviewed_stage_admission WHERE owner_user_id<>'$owner'::uuid")" == 0 ]]

phase=entailment_handoff
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" venv/bin/python "$entailment" \
  --owner-user-id "$owner" >"$entailment_dry"
jq -e '
  .apply==false and .database_writes==0 and
  .plans[0].route=="private_entailment_assessment" and
  .external_model_calls==0 and .local_model_calls==0 and
  .claim_writes==0 and .qdrant_writes==0 and .prompt_influence==0
' "$entailment_dry" >/dev/null

phase=complete
printf '%s\n' 'memory_v1_v5_reviewed_stage_admission_clone: PASS'

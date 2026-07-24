#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the additive V5.2 reconciliation compatibility
# functions and materializes exactly one reviewed, owner-scoped reported stance.
# Qdrant projection, retrieval, and prompt influence remain disabled.

if [[ "${MEMORY_V1_V5_2_RECONCILED_STANCE_PRODUCTION_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_RECONCILED_STANCE_PRODUCTION_APPLY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=49ff31f4c8586e90d2b90382e31db8e5c24272d6
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
evidence=22bd0732-3539-4180-8f89-8f84114131c0
primary=807fa195-669a-4a7d-bb60-d8a50ea22bbb
context=560261e2-7ac6-435d-bcb6-934315b78472
migration=ops/sql/20260724_memory_v1_v5_2_reconciled_stance_projection.sql
stage_runner=scripts/memory_v1_v5_2_reconciled_stance_stage.py
review_manifest_runner=scripts/memory_v1_v5_2_reconciled_stance_review_manifest.py
review_runner=scripts/memory_v1_v5_2_projection_review_batch.py
apply_manifest_runner=scripts/memory_v1_v5_2_reconciled_stance_apply_manifest.py
apply_runner=scripts/memory_v1_v5_claim_projection_apply_batch.py
python_bin="$repo_root/venv/bin/python"
review_root=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_reconciled_stance.lock
phase=initialization
timers_quiesced=0
brains_quiesced=0
brains_state_before=
artifact_dir=
status_file=
run_id=
timer_state=$(mktemp /tmp/memory-v1-v5-2-reconciled-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-2-reconciled-tables.XXXXXX)

declare -A expected_delta=(
  [projection_plan]=1
  [projection_plan_item]=1
  [projection_claim_payload]=1
  [projection_plan_observation]=2
  [projection_review]=1
  [claim]=1
  [claim_revision]=2
  [claim_observation]=2
  [projection_apply_event]=1
  [projection_dispatch_v5]=1
  [claim_assessment_review_v5]=1
  [claim_assessment]=1
  [claim_assessment_apply_v5]=1
  [relational_operation_request]=2
)

file_sha() {
  sha256sum "$repo_root/$1" | awk '{print $1}'
}

psql_row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

authenticated_health() {
  set -a
  source "$repo_root/.env"
  set +a
  [[ -n "${VS_SERVICE_TOKEN:-}" ]]
  for _attempt in $(seq 1 30); do
    if [[ "$(systemctl is-active brains.service)" == active ]] \
       && curl --fail --silent --max-time 5 \
          -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
          http://127.0.0.1:8088/healthz \
          | jq -e '.status=="ok"' >/dev/null \
       && curl --fail --silent --max-time 5 \
          -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
          http://127.0.0.1:8088/readyz \
          | jq -e '.ok==true and .postgres==true' >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

restore_runtime() {
  if [[ "$brains_quiesced" -eq 1 ]]; then
    if [[ "$brains_state_before" == active ]]; then
      sudo -n systemctl start brains.service
    else
      sudo -n systemctl stop brains.service
    fi
    [[ "$(systemctl is-active brains.service)" == "$brains_state_before" ]]
    brains_quiesced=0
  fi
  restore_timers
}

record_exit() {
  code=$?
  if [[ "$code" -eq 0 && "$phase" != complete ]]; then
    code=1
  fi
  restore_runtime || code=1
  rm -f "$timer_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_id=%s\n' "$run_id"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$code"
}
trap record_exit EXIT

capture_partition() {
  local partition=$1 output=$2 table has_owner predicate state
  : >"$output"
  while IFS=$'\t' read -r table has_owner; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    if [[ "$has_owner" == t ]]; then
      if [[ "$partition" == target ]]; then
        predicate="owner_user_id='$owner'::uuid"
      else
        predicate="owner_user_id IS DISTINCT FROM '$owner'::uuid"
      fi
    elif [[ "$partition" == target ]]; then
      continue
    else
      predicate=true
    fi
    state=$(psql_row "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
        WHERE $predicate
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

verify_target_delta() {
  local before=$1 after=$2 expected_file=$artifact_dir/expected-deltas.tsv
  : >"$expected_file"
  for table in "${!expected_delta[@]}"; do
    printf '%s\t%s\n' "$table" "${expected_delta[$table]}" >>"$expected_file"
  done
  sort -o "$expected_file" "$expected_file"
  BEFORE="$before" AFTER="$after" EXPECTED="$expected_file" python3 - <<'PY'
import os
from pathlib import Path

def states(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        table, count, digest = line.split("\t")
        result[table] = (int(count), digest)
    return result

before = states(os.environ["BEFORE"])
after = states(os.environ["AFTER"])
expected = {}
for line in Path(os.environ["EXPECTED"]).read_text().splitlines():
    table, count = line.split("\t")
    expected[table] = int(count)
if before.keys() != after.keys():
    raise SystemExit("target-owner table set changed")
for table, old_state in before.items():
    wanted = expected.get(table, 0)
    actual = after[table][0] - old_state[0]
    if actual != wanted:
        raise SystemExit(f"unexpected target delta {table}: {actual} != {wanted}")
    if wanted == 0 and after[table][1] != old_state[1]:
        raise SystemExit(f"unexpected target mutation {table}")
PY
  chmod 0600 "$expected_file"
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
head=$(git -C "$repo_root" rev-parse HEAD)
[[ -x "$python_bin" ]]
[[ "$(file_sha "$migration")" == e056c14ec13d9d52aa2ab325d403b0caa2444edf0a051d9d3be9f552acd98ca2 ]]
[[ "$(file_sha "$stage_runner")" == f674ff6f1e94f97cc31cce288cc0f16935a5818fca83deabc1423f1a0a10443e ]]
[[ "$(file_sha "$review_manifest_runner")" == b1c1f640a0fcd4636a983f480eb901c904159f781d29a9f5e4f559b0fb3854c0 ]]
[[ "$(file_sha "$apply_manifest_runner")" == 0bf128e41d9f971418e01bc345de0b993897b0cdcd15e3fa85ad11e61c064123 ]]
[[ "$(file_sha "$apply_runner")" == 6c43b4ee43b1b1eef87fd678099368169bf1be7b1207cc0a582375b55cd2a1f5 ]]
[[ "$(file_sha "$review_runner")" == 7f07c379c85ae9bd933152d95d341e1ff9f6e98d9e089eabc29733c1ae7b4db3 ]]

set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
authenticated_health

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_${head:0:12}"
artifact_dir="$review_root/reconciled-production-$run_id"
status_file="$snapshot_root/memory_v1_v5_2_reconciled_stance_${run_id}.status"
mkdir -p "$artifact_dir"
chmod 0700 "$artifact_dir"

phase=capture_timer_state
: >"$timer_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$timer_state" ]]
chmod 0600 "$timer_state"

phase=quiesce_timers
while IFS=$'\t' read -r unit _enabled active; do
  [[ "$active" != active ]] || sudo -n systemctl stop "$unit"
done <"$timer_state"
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done <"$timer_state"

phase=quiesce_brains
brains_state_before=$(systemctl is-active brains.service)
if [[ "$brains_state_before" == active ]]; then
  sudo -n systemctl stop brains.service
fi
brains_quiesced=1
for _attempt in $(seq 1 30); do
  systemctl is-active --quiet brains.service || break
  sleep 1
done
! systemctl is-active --quiet brains.service

phase=capture_baseline
docker exec "$container" psql -X -A -F $'\t' -t -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    SELECT table_name,
      EXISTS (
        SELECT 1 FROM information_schema.columns AS column_row
        WHERE column_row.table_schema='memory'
          AND column_row.table_name=table_row.table_name
          AND column_row.column_name='owner_user_id'
      )
    FROM information_schema.tables AS table_row
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  " >"$table_list"
target_before="$artifact_dir/target-before.tsv"
target_schema="$artifact_dir/target-after-schema.tsv"
target_materialized="$artifact_dir/target-materialized.tsv"
target_after="$artifact_dir/target-after.tsv"
target_replay="$artifact_dir/target-replay.tsv"
non_target_before="$artifact_dir/non-target-before.tsv"
non_target_schema="$artifact_dir/non-target-after-schema.tsv"
non_target_materialized="$artifact_dir/non-target-materialized.tsv"
non_target_after="$artifact_dir/non-target-after.tsv"
non_target_replay="$artifact_dir/non-target-replay.tsv"
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)

phase=backup
backup_partial="$snapshot_root/.memory_pre_v5_2_reconciled_stance_${run_id}.dump.partial"
backup="$snapshot_root/memory_pre_v5_2_reconciled_stance_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=install_schema
docker exec -i "$container" psql -X -q -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$migration"
capture_partition target "$target_schema"
capture_partition non_target "$non_target_schema"
cmp -s "$target_before" "$target_schema"
cmp -s "$non_target_before" "$non_target_schema"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

stage_manifest="$artifact_dir/stage-manifest.json"
stage_authorization="$artifact_dir/stage-authorization.json"
stage_apply="$artifact_dir/stage-apply.json"
stage_replay="$artifact_dir/stage-replay.json"
isolation_result="$artifact_dir/isolation.json"
review_decisions="$artifact_dir/review-decisions.json"
review_manifest="$artifact_dir/review-manifest.json"
review_apply="$artifact_dir/review-apply.json"
review_replay="$artifact_dir/review-replay.json"
apply_manifest="$artifact_dir/apply-manifest.json"
materialize_apply="$artifact_dir/materialize-apply.json"
materialize_replay="$artifact_dir/materialize-replay.json"
env_common=(
  "POSTGRES_DSN=$POSTGRES_DSN"
  "PYTHONPATH=$repo_root/scripts"
  "MEMORY_V1_REQUIRED_HEAD=$head"
)

phase=zero_write_preflight
env "${env_common[@]}" "$python_bin" "$repo_root/$stage_runner" manifest \
  --owner "$owner" --evidence "$evidence" --primary "$primary" \
  --context "$context" --required-head "$head" --output "$stage_manifest"
env "${env_common[@]}" "$python_bin" "$repo_root/$stage_runner" authorize \
  --manifest "$stage_manifest" --output "$stage_authorization"
capture_partition target "$target_schema"
capture_partition non_target "$non_target_schema"
cmp -s "$target_before" "$target_schema"
cmp -s "$non_target_before" "$non_target_schema"

phase=transactional_stage
env "${env_common[@]}" "$python_bin" "$repo_root/$stage_runner" apply \
  --manifest "$stage_manifest" --authorization "$stage_authorization" \
  --confirm STAGE_ONE_OWNER_V5_2_RECONCILED_STANCE_ONLY \
  --output "$stage_apply"
[[ "$(jq -er '.rows_written' "$stage_apply")" == 5 ]]

phase=stage_replay
env "${env_common[@]}" "$python_bin" "$repo_root/$stage_runner" replay \
  --manifest "$stage_manifest" --authorization "$stage_authorization" \
  --confirm STAGE_ONE_OWNER_V5_2_RECONCILED_STANCE_ONLY \
  --output "$stage_replay"
[[ "$(jq -er '.rows_written' "$stage_replay")" == 0 ]]

phase=cross_owner_probe
env "${env_common[@]}" "$python_bin" "$repo_root/$stage_runner" cross-owner \
  --manifest "$stage_manifest" --other-owner "$other_owner" \
  --output "$isolation_result"
[[ "$(jq -er '.cross_owner_rejected' "$isolation_result")" == true ]]

phase=transactional_review
env "${env_common[@]}" "$python_bin" "$repo_root/$review_manifest_runner" \
  --owner "$owner" --required-head "$head" \
  --stage-manifest "$stage_manifest" \
  --decisions-output "$review_decisions" --output "$review_manifest"
env "${env_common[@]}" MEMORY_V1_V5_2_PROJECTION_REVIEW_APPLY=authorized \
  "$python_bin" "$repo_root/$review_runner" --mode apply \
  --manifest "$review_manifest" --output "$review_apply"
[[ "$(jq -er '.rows_written' "$review_apply")" == 1 ]]
env "${env_common[@]}" "$python_bin" "$repo_root/$review_runner" --mode replay \
  --manifest "$review_manifest" --output "$review_replay"
[[ "$(jq -er '.rows_written' "$review_replay")" == 0 ]]

phase=transactional_materialization
env "${env_common[@]}" "$python_bin" "$repo_root/$apply_manifest_runner" \
  --owner "$owner" --required-head "$head" \
  --stage-manifest "$stage_manifest" \
  --review-manifest "$review_manifest" --review-result "$review_apply" \
  --output "$apply_manifest"
env "${env_common[@]}" MEMORY_V1_CLAIM_PROJECTION_APPLY_BATCH=authorized \
  "$python_bin" "$repo_root/$apply_runner" --mode apply \
  --manifest "$apply_manifest" --output "$materialize_apply"
[[ "$(jq -er '.insert_rows' "$materialize_apply")" == 12 ]]
[[ "$(jq -er '.mutated_rows' "$materialize_apply")" == 13 ]]
[[ "$(jq -er '.projection_outbox_deferred' "$materialize_apply")" == true ]]
capture_partition target "$target_materialized"
capture_partition non_target "$non_target_materialized"
qdrant_materialized=$(qdrant_signature)
[[ "$qdrant_materialized" == "$qdrant_before" ]]

phase=materialization_replay
env "${env_common[@]}" "$python_bin" "$repo_root/$apply_runner" --mode replay \
  --manifest "$apply_manifest" --apply-result "$materialize_apply" \
  --output "$materialize_replay"
[[ "$(jq -er '.insert_rows' "$materialize_replay")" == 0 ]]
[[ "$(jq -er '.mutated_rows' "$materialize_replay")" == 0 ]]
capture_partition target "$target_after"
capture_partition non_target "$non_target_after"
cmp -s "$target_materialized" "$target_after"
cmp -s "$non_target_materialized" "$non_target_after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=verify_exact_state
plan_id=$(jq -er '.plan_id' "$stage_manifest")
claim_id=$(jq -er '.outcomes[0].claim_id' "$materialize_apply")
canonical_text=$(psql_row "
  SELECT canonical_text FROM memory.claim
  WHERE owner_user_id='$owner'::uuid AND claim_id='$claim_id'::uuid
")
[[ "$canonical_text" == \
  'The user reports this position: "I believe we can’t read the future so worrying about what’s gonna happen next month doesn’t really matter."' ]]
verification=$(psql_row "
  SELECT
    (SELECT count(*) FROM memory.projection_plan
     WHERE owner_user_id='$owner'::uuid AND plan_id='$plan_id'::uuid)::text || '|' ||
    (SELECT count(*) FROM memory.projection_plan_item
     WHERE owner_user_id='$owner'::uuid AND plan_id='$plan_id'::uuid)::text || '|' ||
    (SELECT count(*) FROM memory.projection_claim_payload
     WHERE owner_user_id='$owner'::uuid AND plan_id='$plan_id'::uuid)::text || '|' ||
    (SELECT count(*) FROM memory.projection_plan_observation
     WHERE owner_user_id='$owner'::uuid AND plan_id='$plan_id'::uuid)::text || '|' ||
    (SELECT count(*) FROM memory.projection_review
     WHERE owner_user_id='$owner'::uuid AND plan_id='$plan_id'::uuid)::text || '|' ||
    (SELECT count(*) FROM memory.claim
     WHERE owner_user_id='$owner'::uuid AND claim_id='$claim_id'::uuid
       AND status='supported')::text || '|' ||
    (SELECT count(*) FROM memory.claim_revision
     WHERE owner_user_id='$owner'::uuid AND claim_id='$claim_id'::uuid)::text || '|' ||
    (SELECT count(*) FROM memory.claim_observation
     WHERE owner_user_id='$owner'::uuid AND claim_id='$claim_id'::uuid)::text || '|' ||
    (SELECT count(*) FROM memory.projection_apply_event
     WHERE owner_user_id='$owner'::uuid AND plan_id='$plan_id'::uuid)::text || '|' ||
    (SELECT count(*) FROM memory.projection_dispatch_v5
     WHERE owner_user_id='$owner'::uuid
       AND resulting_claim_id='$claim_id'::uuid)::text || '|' ||
    (SELECT count(*) FROM memory.projection_outbox
     WHERE owner_user_id='$owner'::uuid AND aggregate_id='$claim_id'::uuid)::text
")
[[ "$verification" == "1|1|1|2|1|1|2|2|1|1|0" ]]
stances=$(psql_row "
  SELECT string_agg(
    link.observation_id::text || ':' || link.stance::text,
    ',' ORDER BY CASE link.stance WHEN 'supports' THEN 0 ELSE 1 END
  )
  FROM memory.projection_plan_observation AS link
  WHERE link.owner_user_id='$owner'::uuid
    AND link.plan_id='$plan_id'::uuid
")
[[ "$stances" == "$primary:supports,$context:context" ]]

verify_target_delta "$target_before" "$target_after"
cmp -s "$non_target_before" "$non_target_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=verify_zero_write_replay
capture_partition target "$target_replay"
capture_partition non_target "$non_target_replay"
cmp -s "$target_after" "$target_replay"
cmp -s "$non_target_after" "$non_target_replay"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=restore_runtime
restore_runtime
authenticated_health

phase=report
report="$artifact_dir/report.json"
jq -n \
  --arg head "$head" \
  --arg plan_id "$plan_id" \
  --arg claim_id "$claim_id" \
  --arg canonical_text "$canonical_text" \
  --arg backup "$backup" \
  --arg qdrant_sha256 "$qdrant_after" \
  --arg stage_manifest_sha256 "$(jq -er '.manifest_sha256' "$stage_manifest")" \
  --arg apply_manifest_sha256 "$(jq -er '.manifest_sha256' "$apply_manifest")" \
  '{
    contract_version:"memory_v1_v5_2_reconciled_stance_production_report_v1",
    head_commit:$head,
    owner_user_id:"1240822d-ac9a-4096-95aa-e2b24d36ef50",
    plan_id:$plan_id,
    claim_id:$claim_id,
    canonical_text:$canonical_text,
    backup:$backup,
    observation_links:{supports:1,context:1},
    schema_row_changes:0,
    stage_rows:5,
    review_rows:1,
    materialization_insert_rows:12,
    materialization_mutated_rows:13,
    projection_outbox_deferred:true,
    stage_replay_rows:0,
    review_replay_rows:0,
    materialization_replay_rows:0,
    cross_owner_rejected:true,
    non_target_unchanged:true,
    qdrant_sha256:$qdrant_sha256,
    qdrant_unchanged:true,
    stage_manifest_sha256:$stage_manifest_sha256,
    apply_manifest_sha256:$apply_manifest_sha256,
    timers_restored_exactly:true,
    brains_service_restored:true,
    retrieval_activated:false,
    prompt_influence_activated:false
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'backup=%s\n' "$backup"
printf 'report=%s\n' "$report"
printf 'report_sha256=%s\n' "$(awk '{print $1}' "$report.sha256")"
printf 'memory_v1_v5_2_reconciled_stance_production_apply: PASS\n'

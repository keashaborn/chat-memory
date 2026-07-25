#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Transactionally stages four exact reviewed V5.2 packets.
# It creates only relational staging rows and stops before entity application,
# claims, Qdrant projection, retrieval, or prompt influence.

if [[ "${MEMORY_V1_V5_2_MIXED_STAGE_PRODUCTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_MIXED_STAGE_PRODUCTION=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-/opt/chat-memory/.env}"
set +a

container=brains-postgres-1
database=memory
python_bin=/opt/chat-memory/venv/bin/python
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
manifest_source=manifests/memory_v1_v5_2_mixed_relational_stage_batch_20260725.json
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
authorizer=tests/memory_v1_v5_2_stage_batch_fixture.py
evidence_ids=(
  3e331222-976f-5983-88e2-6e5a8f7b148b
  33126656-fc5a-5fc1-a035-246b14576ee5
  405fcdb1-a4d2-53ff-91ad-542b258cea03
  4550d3a1-7649-5d1b-aff8-f2504e36f869
)
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_mixed_stage.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
unit_state=$(mktemp /tmp/memory-v1-v5-2-mixed-stage-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-2-mixed-stage-tables.XXXXXX)

psql_row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
}

assert_equal() {
  local label=$1 actual=$2 expected=$3
  if [[ "$actual" != "$expected" ]]; then
    printf 'ASSERTION_FAILED=%s\nexpected=%s\nactual=%s\n' \
      "$label" "$expected" "$actual" >&2
    exit 1
  fi
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

restore_timers() {
  [[ "$units_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    if [[ "$(systemctl is-enabled "$unit")" != "$enabled" ]]; then
      if [[ "$enabled" == enabled ]]; then
        systemctl enable "$unit"
      else
        systemctl disable "$unit"
      fi
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
}

record_exit() {
  local exit_code=$?
  trap - EXIT
  if [[ "$exit_code" -eq 0 && "$phase" != complete ]]; then
    exit_code=1
  fi
  restore_timers || exit_code=1
  rm -f "$unit_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

capture_partition() {
  local partition=$1 output=$2 table has_owner predicate state
  : >"$output"
  while IFS=$'\t' read -r table has_owner; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    if [[ "$has_owner" == t ]]; then
      if [[ "$partition" == target ]]; then
        predicate="owner_user_id='$target_owner'::uuid"
      else
        predicate="owner_user_id<>'$target_owner'::uuid"
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
      ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

verify_target_delta() {
  BEFORE="$target_before" AFTER="$target_after" "$python_bin" - <<'PY'
import os
from pathlib import Path

def load(path):
    rows = {}
    for line in Path(path).read_text().splitlines():
        table, count, digest = line.split("\t")
        rows[table] = (int(count), digest)
    return rows

before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
expected = {
    "relational_stage_batch": 4,
    "entity_mention": 5,
    "entity_resolution_plan": 5,
    "entity_resolution_candidate": 5,
    "observation": 5,
    "observation_temporal": 5,
    "relational_operation_request": 4,
}
if before.keys() != after.keys():
    raise SystemExit("target-owner table set changed")
for table in before:
    delta = after[table][0] - before[table][0]
    wanted = expected.get(table, 0)
    if delta != wanted:
        raise SystemExit(f"unexpected target delta {table}: {delta} != {wanted}")
    if wanted == 0 and before[table][1] != after[table][1]:
        raise SystemExit(f"unexpected target mutation {table}")
if sum(expected.values()) != 33:
    raise SystemExit("target row budget is not 33")
PY
}

hash_lock() {
  assert_equal "$2" "$(sha256sum "$1" | awk '{print $1}')" "$3"
}

[[ "$(id -u)" -eq 0 ]]
[[ "$repo_root" == /opt/chat-memory ]]
[[ -z "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain)" ]]
head=$(git rev-parse HEAD)
git merge-base --is-ancestor f5016d70deb31f68768f9faf74b42ba19f32fff3 "$head"
hash_lock "$manifest_source" manifest_sha \
  e1d8b2e574c79efb6583a95096e06eb8f8a4779604fc046b872ef95d32332c2d
hash_lock "$stage_runner" stage_runner_sha \
  3dddef3dbf71862fc42252d6263075ad8d407bc292a4f06760866c816c3699b9
hash_lock "$authorizer" authorizer_sha \
  96461968b5afa0ec2a8aa207ec7ec2096278d851cd51d4e0a7c90541ab5a54ac
[[ "$(systemctl is-active brains.service)" == active ]]
docker exec "$container" pg_isready -U sage -d "$database" >/dev/null

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_mixed_stage_${run_tag}.status"
work="$review_root/mixed-relational-stage-production-$run_tag"
mkdir -m 0700 "$work"

phase=quiesce
: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$unit_state" ]]
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$unit_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$unit_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_2_mixed_stage_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_mixed_stage_${run_tag}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -X -A -t -F $'\t' -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    SELECT table_name, EXISTS (
      SELECT 1 FROM information_schema.columns AS column_info
      WHERE column_info.table_schema='memory'
        AND column_info.table_name=tables.table_name
        AND column_info.column_name='owner_user_id'
    )
    FROM information_schema.tables AS tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name" >"$table_list"
[[ -s "$table_list" ]]
target_before="$work/target-before.tsv"
target_after="$work/target-after.tsv"
non_target_before="$work/non-target-before.tsv"
non_target_after="$work/non-target-after.tsv"
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)
entities_before=$(psql_row "
  SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")
claims_before=$(psql_row "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")
assert_equal initial_target_stage_rows "$(psql_row "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$target_owner'::uuid
    AND evidence_id IN (
      '${evidence_ids[0]}'::uuid,'${evidence_ids[1]}'::uuid,
      '${evidence_ids[2]}'::uuid,'${evidence_ids[3]}'::uuid
    )")" 0

phase=plan
cp "$manifest_source" "$work/stage-manifest.json"
chmod 0600 "$work/stage-manifest.json"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$stage_runner" plan \
  --manifest "$work/stage-manifest.json" \
  --review-root "$review_root" \
  --output "$work/stage-plan.json"
"$python_bin" "$authorizer" authorize \
  --plan "$work/stage-plan.json" \
  --output "$work/stage-authorization.json" \
  --head "$head"

phase=stage
MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized \
  POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$stage_runner" apply \
  --plan "$work/stage-plan.json" \
  --authorization "$work/stage-authorization.json" \
  --review-root "$review_root" \
  --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
  --output "$work/stage-apply.json"
assert_equal stage_rows \
  "$(jq -r '.database_rows_created' "$work/stage-apply.json")" 33
assert_equal replay_rows \
  "$(jq -r '.checks.replay_rows_written' "$work/stage-apply.json")" 0

phase=postflight
assert_equal entities_unchanged "$(psql_row "
  SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")" "$entities_before"
assert_equal claims_unchanged "$(psql_row "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")" "$claims_before"
cross_owner_visible=$(psql "$POSTGRES_DSN" -X -A -t -v ON_ERROR_STOP=1 -c "
  BEGIN;
  SELECT set_config('app.user_id','$other_owner',true);
  SELECT count(*) FROM memory.observation
  WHERE evidence_id IN (
    '${evidence_ids[0]}'::uuid,'${evidence_ids[1]}'::uuid,
    '${evidence_ids[2]}'::uuid,'${evidence_ids[3]}'::uuid
  );
  ROLLBACK;" | sed -n '3p')
assert_equal cross_owner_visible "$cross_owner_visible" 0
capture_partition target "$target_after"
capture_partition non_target "$non_target_after"
cmp -s "$non_target_before" "$non_target_after"
verify_target_delta
qdrant_after=$(qdrant_signature)
assert_equal qdrant_unchanged "$qdrant_after" "$qdrant_before"
[[ "$(systemctl is-active brains.service)" == active ]]
docker exec "$container" pg_isready -U sage -d "$database" >/dev/null

restore_timers

phase=report
report="$work/report.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$head" --arg owner_user_id "$target_owner" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg work_root "$work" \
  --arg target_before "$target_before" --arg target_after "$target_after" \
  --arg non_target_before "$non_target_before" \
  --arg non_target_after "$non_target_after" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:"memory_v1_v5_2_mixed_relational_stage_production_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    owner_user_id:$owner_user_id,
    backup:{path:$backup,sha256:$backup_sha256},work_root:$work_root,
    rows:{stage_total:33,batches:4,mentions:5,resolutions:5,
      candidates:5,observations:5,temporals:5,operation_requests:4,
      entities:0,claims:0},
    evidence:{target_before:$target_before,target_after:$target_after,
      non_target_before:$non_target_before,non_target_after:$non_target_after,
      qdrant_sha256:$qdrant_sha256},
    checks:{fresh_backup:true,hash_locked_inputs:true,
      one_owner_transaction:true,exact_target_owner_deltas:true,
      zero_write_replay:true,account_isolation:true,
      non_target_and_global_rows_unchanged:true,qdrant_unchanged:true,
      timers_restored:true,service_healthy:true,external_model_calls:0,
      retrieval_changes:0,prompt_changes:0},
    hard_stop:"before_entity_review_apply_claims_projection_qdrant_retrieval_or_prompt_influence"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf '%s\n' \
  'MEMORY_V1_V5_2_MIXED_RELATIONAL_STAGE_BATCH_PRODUCTION=PASS' \
  "head=$head" \
  "report=$report" \
  "backup=$backup" \
  'stage_rows_created=33' \
  'entity_rows_created=0' \
  'claim_rows_created=0' \
  'qdrant_writes=0' \
  'retrieval_changes=0' \
  'prompt_changes=0'

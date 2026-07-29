#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the historical-pet renderer compatibility and
# transactionally materializes exactly 10 reviewed pet-profile claims. The
# existing Neko relationship claim is left untouched for temporal reconciliation.
# Qdrant, retrieval, and prompt influence remain unchanged.

if [[ "${MEMORY_V1_V5_2_PET_PROFILE_CLAIM_PRODUCTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_PET_PROFILE_CLAIM_PRODUCTION=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=93a6cc741a745aa2de6bf862e8ac360819325baf
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
deferred_observation=bc8866ad-95e8-4413-832e-813f601eece6
existing_neko_claim=bd20dd0a-9fa0-4a21-8a93-e828c8044150
python_bin=/opt/chat-memory/venv/bin/python
review_root=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_pet_profile_claim.lock
resume_from=${MEMORY_V1_V5_2_PET_PROFILE_CLAIM_RESUME_FROM:-}

migration=ops/sql/20260729_memory_v1_v5_2_historical_pet_claim_projection.sql
rollback=ops/sql/20260729_memory_v1_v5_2_historical_pet_claim_projection_rollback.sql
security_test=tests/memory_v1_v5_2_historical_pet_claim_projection_security.sql
migration_sha=8a9dbf5df97b72c431c0ce8e49c1aa271adffe21face722a62ccbc1a44e39c4d
rollback_sha=efd498e460bb0d4a0a75dab6aa392f9816da93ff4dbb1f201d0b74c6f8f9984e
security_sha=0cf1e008abe6bab81fa0084d1a0d6e97b4832bd4b66c2b1ee2ac6dff426c05fa

stage_runner=scripts/memory_v1_v5_2_pet_profile_claim_stage.py
review_manifest_runner=scripts/memory_v1_v5_2_pet_profile_claim_review_manifest.py
review_runner=scripts/memory_v1_v5_2_pet_profile_claim_review_batch.py
apply_manifest_runner=scripts/memory_v1_v5_2_pet_profile_claim_apply_manifest.py
apply_runner=scripts/memory_v1_v5_claim_projection_apply_batch.py

phase=initialization
timers_quiesced=0
brains_quiesced=0
brains_state_before=
artifact_dir=
status_file=
run_id=
timer_state=$(mktemp /tmp/memory-v1-pet-claim-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-pet-claim-tables.XXXXXX)

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
  source /opt/chat-memory/.env
  set +a
  [[ -n "${VS_SERVICE_TOKEN:-}" ]]
  for _attempt in $(seq 1 30); do
    if [[ "$(systemctl is-active brains.service)" == active ]] \
       && curl --fail --silent --max-time 5 \
          -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
          http://127.0.0.1:8088/healthz \
          | jq -e '.status=="ok"' >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
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
  if [[ "$timers_quiesced" -eq 1 ]]; then
    while IFS=$'\t' read -r unit enabled active; do
      [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
      if [[ "$active" == active ]]; then
        sudo -n systemctl start "$unit"
      else
        sudo -n systemctl stop "$unit"
      fi
      [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
      [[ "$(systemctl is-active "$unit")" == "$active" ]]
    done <"$timer_state"
    timers_quiesced=0
  fi
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
  local before=$1 after=$2 expected_file=$3
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
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
head=$(git -C "$repo_root" rev-parse HEAD)
[[ -x "$python_bin" ]]
for path in \
  "$migration" "$rollback" "$security_test" "$stage_runner" \
  "$review_manifest_runner" "$review_runner" "$apply_manifest_runner" \
  "$apply_runner"; do
  [[ -f "$repo_root/$path" ]]
done
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == "$rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$security_test" | awk '{print $1}')" == "$security_sha" ]]

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
authenticated_health
if [[ -n "$resume_from" ]]; then
  resume_from=$(realpath "$resume_from")
  [[ "$resume_from" == "$review_root"/pet-profile-claim-production-* ]]
  for name in stage-manifest.json stage-authorization.json \
    stage-cross-owner.json stage-apply.json stage-replay.json; do
    [[ -f "$resume_from/$name" ]]
    [[ "$(stat -c %a "$resume_from/$name")" == 600 ]]
  done
  [[ "$(jq -er '.rows_written' "$resume_from/stage-apply.json")" == 60 ]]
  [[ "$(jq -er '.rows_written' "$resume_from/stage-replay.json")" == 0 ]]
  [[ "$(jq -er '.cross_owner_rejected' \
    "$resume_from/stage-cross-owner.json")" == true ]]
fi

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_${head:0:12}"
artifact_dir="$review_root/pet-profile-claim-production-$run_id"
status_file="$snapshot_root/memory_v1_pet_profile_claim_${run_id}.status"
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
cp "$timer_state" "$artifact_dir/timer-state-before.tsv"
chmod 0600 "$artifact_dir/timer-state-before.tsv"

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
done <"$timer_state"

phase=quiesce_brains
brains_state_before=$(systemctl is-active brains.service)
if [[ "$brains_state_before" == active ]]; then
  sudo -n systemctl stop brains.service
fi
brains_quiesced=1
! systemctl is-active --quiet brains.service

phase=capture_baseline
docker exec "$container" psql -X -A -F $'\t' -t -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    SELECT table_name, EXISTS (
      SELECT 1 FROM information_schema.columns AS column_row
      WHERE column_row.table_schema='memory'
        AND column_row.table_name=table_row.table_name
        AND column_row.column_name='owner_user_id'
    )
    FROM information_schema.tables AS table_row
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  " >"$table_list"
[[ -s "$table_list" ]]
target_before="$artifact_dir/target-before.tsv"
target_schema="$artifact_dir/target-after-schema.tsv"
target_after="$artifact_dir/target-after.tsv"
target_replay="$artifact_dir/target-after-replay.tsv"
non_target_before="$artifact_dir/non-target-before.tsv"
non_target_schema="$artifact_dir/non-target-after-schema.tsv"
non_target_after="$artifact_dir/non-target-after.tsv"
non_target_replay="$artifact_dir/non-target-after-replay.tsv"
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)
neko_before=$(psql_row "
  SELECT encode(public.digest(convert_to(to_jsonb(claim_row)::text,'UTF8'),
    'sha256'),'hex')
  FROM memory.claim AS claim_row
  WHERE owner_user_id='$owner'::uuid
    AND claim_id='$existing_neko_claim'::uuid")
[[ -n "$neko_before" ]]

phase=backup
backup_partial="$snapshot_root/.memory_pre_pet_profile_claim_${run_id}.dump.partial"
backup="$snapshot_root/memory_pre_pet_profile_claim_${run_id}.dump"
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

phase=install_compatibility
docker exec -i "$container" psql -X -q -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$migration"
docker exec -i "$container" psql -X -q -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$migration"
docker exec -i "$container" psql -X -q -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$security_test"
capture_partition target "$target_schema"
capture_partition non_target "$non_target_schema"
cmp -s "$target_before" "$target_schema"
cmp -s "$non_target_before" "$non_target_schema"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

observations=(
  2e668700-7ae6-4752-ab6a-423e6c6a8c6d
  eed6cae7-33c9-43e9-8736-98861a057fa5
  e5939e3f-b732-4f72-b7c4-b60ee7c55244
  37c05103-3e18-4444-9722-4ab384896aa7
  978c1972-82d5-4d19-a32f-b45c7f4cfbaa
  cb711085-b49b-4b9c-be3b-c34d2d8456da
  6db9a4fd-e109-48ed-8d95-c97eef75382c
  8c4476ae-b917-4678-8d63-f4949982e196
  6151f123-d05f-404d-b3f6-d7079de6b4e6
  82c87916-a90c-4af8-b4cb-fbd2981f9f96
)
observation_args=()
for observation in "${observations[@]}"; do
  observation_args+=(--observation "$observation")
done

stage_manifest="${resume_from:+$resume_from/stage-manifest.json}"
stage_authorization="${resume_from:+$resume_from/stage-authorization.json}"
stage_cross_owner="$artifact_dir/stage-cross-owner.json"
stage_apply="${resume_from:+$resume_from/stage-apply.json}"
stage_replay="${resume_from:+$resume_from/stage-replay.json}"

phase=stage_manifest
if [[ -z "$resume_from" ]]; then
  stage_manifest="$artifact_dir/stage-manifest.json"
  stage_authorization="$artifact_dir/stage-authorization.json"
  stage_apply="$artifact_dir/stage-apply.json"
  stage_replay="$artifact_dir/stage-replay.json"
  POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root:$repo_root/scripts" \
    "$python_bin" "$repo_root/$stage_runner" manifest \
    --owner "$owner" "${observation_args[@]}" \
    --required-head "$head" --output "$stage_manifest"
  PYTHONPATH="$repo_root:$repo_root/scripts" \
    "$python_bin" "$repo_root/$stage_runner" authorize \
    --manifest "$stage_manifest" --output "$stage_authorization"
else
  [[ "$(psql_row "
    SELECT count(DISTINCT link.observation_id)
    FROM memory.projection_plan_observation AS link
    JOIN memory.projection_plan AS plan
      USING(owner_user_id,plan_id)
    WHERE link.owner_user_id='$owner'::uuid
      AND link.observation_id=ANY(ARRAY[
        '2e668700-7ae6-4752-ab6a-423e6c6a8c6d'::uuid,
        'eed6cae7-33c9-43e9-8736-98861a057fa5'::uuid,
        'e5939e3f-b732-4f72-b7c4-b60ee7c55244'::uuid,
        '37c05103-3e18-4444-9722-4ab384896aa7'::uuid,
        '978c1972-82d5-4d19-a32f-b45c7f4cfbaa'::uuid,
        'cb711085-b49b-4b9c-be3b-c34d2d8456da'::uuid,
        '6db9a4fd-e109-48ed-8d95-c97eef75382c'::uuid,
        '8c4476ae-b917-4678-8d63-f4949982e196'::uuid,
        '6151f123-d05f-404d-b3f6-d7079de6b4e6'::uuid,
        '82c87916-a90c-4af8-b4cb-fbd2981f9f96'::uuid
      ])")" == 10 ]]
fi
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root:$repo_root/scripts" \
  "$python_bin" "$repo_root/$stage_runner" cross-owner \
  --manifest "$stage_manifest" --other-owner "$other_owner" \
  --output "$stage_cross_owner"
[[ "$(jq -er '.cross_owner_rejected' "$stage_cross_owner")" == true ]]

phase=stage_apply
if [[ -z "$resume_from" ]]; then
  MEMORY_V1_REQUIRED_HEAD="$head" \
  MEMORY_V1_V5_2_PET_PROFILE_CLAIM_STAGE_APPLY=authorized \
  POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root:$repo_root/scripts" \
    "$python_bin" "$repo_root/$stage_runner" apply \
    --manifest "$stage_manifest" --authorization "$stage_authorization" \
    --confirm STAGE_EXACT_TEN_PET_PROFILE_CLAIM_CANDIDATES_ONLY \
    --output "$stage_apply"
  [[ "$(jq -er '.rows_written' "$stage_apply")" == 60 ]]
  MEMORY_V1_REQUIRED_HEAD="$head" \
  MEMORY_V1_V5_2_PET_PROFILE_CLAIM_STAGE_APPLY=authorized \
  POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root:$repo_root/scripts" \
    "$python_bin" "$repo_root/$stage_runner" replay \
    --manifest "$stage_manifest" --authorization "$stage_authorization" \
    --confirm STAGE_EXACT_TEN_PET_PROFILE_CLAIM_CANDIDATES_ONLY \
    --output "$stage_replay"
  [[ "$(jq -er '.rows_written' "$stage_replay")" == 0 ]]
fi

review_decisions="$artifact_dir/review-decisions.json"
review_manifest="$artifact_dir/review-manifest.json"
review_preflight="$artifact_dir/review-preflight.json"
review_apply="$artifact_dir/review-apply.json"
review_replay="$artifact_dir/review-replay.json"

phase=review_manifest
MANIFEST="$stage_manifest" OUTPUT="$review_decisions" \
PYTHONPATH="$repo_root:$repo_root/scripts" "$python_bin" - <<'PY'
import json
import os
from pathlib import Path
from scripts.memory_v1_projection_v5_contract_test import sha256

stage = json.loads(Path(os.environ["MANIFEST"]).read_text())
value = {
    "contract_version": "memory_v1_v5_2_pet_profile_claim_review_decisions_v1",
    "owner_user_id": stage["owner_user_id"],
    "evidence_ids": stage["evidence_ids"],
    "decisions": [],
}
for item in stage["items"]:
    codes = [
        "bound_entities_reviewed",
        "pet_profile_semantics_reviewed",
        (
            "historical_interval_reviewed"
            if item["state_relation"] == "historical"
            else "observation_time_reviewed"
        ),
    ]
    value["decisions"].append({
        "observation_id": item["observation_id"],
        "decision": "authorized",
        "reason": (
            "Reviewed atomic pet-profile observation with bound owner-scoped "
            "entities, exact source evidence, and deterministic temporal rendering."
        ),
        "reason_codes": codes,
    })
value["decisions_sha256"] = sha256(value)
path = Path(os.environ["OUTPUT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root:$repo_root/scripts" \
  "$python_bin" "$repo_root/$review_manifest_runner" \
  --owner "$owner" --required-head "$head" \
  --stage-manifest "$stage_manifest" --decisions "$review_decisions" \
  --output "$review_manifest"
MEMORY_V1_REQUIRED_HEAD="$head" \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root:$repo_root/scripts" \
  "$python_bin" "$repo_root/$review_runner" \
  --mode preflight --manifest "$review_manifest" --output "$review_preflight"
[[ "$(jq -er '.rows_written' "$review_preflight")" == 0 ]]

phase=review_apply
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_PET_PROFILE_CLAIM_REVIEW_APPLY=authorized \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root:$repo_root/scripts" \
  "$python_bin" "$repo_root/$review_runner" \
  --mode apply --manifest "$review_manifest" --output "$review_apply"
[[ "$(jq -er '.rows_written' "$review_apply")" == 10 ]]
[[ "$(jq -er '.decision_counts.authorized' "$review_apply")" == 10 ]]
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_PET_PROFILE_CLAIM_REVIEW_APPLY=authorized \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root:$repo_root/scripts" \
  "$python_bin" "$repo_root/$review_runner" \
  --mode replay --manifest "$review_manifest" --output "$review_replay"
[[ "$(jq -er '.rows_written' "$review_replay")" == 0 ]]

claim_manifest="$artifact_dir/claim-apply-manifest.json"
claim_preflight="$artifact_dir/claim-preflight.json"
claim_apply="$artifact_dir/claim-apply.json"
claim_replay="$artifact_dir/claim-replay.json"

phase=claim_manifest
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root:$repo_root/scripts" \
  "$python_bin" "$repo_root/$apply_manifest_runner" \
  --owner "$owner" --required-head "$head" \
  --review-manifest "$review_manifest" --review-result "$review_apply" \
  --output "$claim_manifest"
[[ "$(jq -er '.items | length' "$claim_manifest")" == 10 ]]
[[ "$(jq -er '.defer_projection_outbox' "$claim_manifest")" == true ]]
MEMORY_V1_REQUIRED_HEAD="$head" \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root:$repo_root/scripts" \
  "$python_bin" "$repo_root/$apply_runner" \
  --mode preflight --manifest "$claim_manifest" --output "$claim_preflight"
[[ "$(jq -er '.insert_rows' "$claim_preflight")" == 0 ]]

phase=claim_apply
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_CLAIM_PROJECTION_APPLY_BATCH=authorized \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root:$repo_root/scripts" \
  "$python_bin" "$repo_root/$apply_runner" \
  --mode apply --manifest "$claim_manifest" --output "$claim_apply"
[[ "$(jq -er '.insert_rows' "$claim_apply")" == 110 ]]
[[ "$(jq -er '.mutated_rows' "$claim_apply")" == 120 ]]
MEMORY_V1_REQUIRED_HEAD="$head" \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root:$repo_root/scripts" \
  "$python_bin" "$repo_root/$apply_runner" \
  --mode replay --manifest "$claim_manifest" \
  --apply-result "$claim_apply" --output "$claim_replay"
[[ "$(jq -er '.insert_rows' "$claim_replay")" == 0 ]]
[[ "$(jq -er '.mutated_rows' "$claim_replay")" == 0 ]]

phase=verify
expected="$artifact_dir/expected-deltas.tsv"
printf '%s\n' \
  $'claim\t10' \
  $'claim_assessment\t10' \
  $'claim_assessment_apply_v5\t10' \
  $'claim_assessment_review_v5\t10' \
  $'claim_observation\t10' \
  $'claim_revision\t20' \
  $'observation_entailment_v5\t10' \
  $'projection_apply_event\t10' \
  $'projection_claim_payload\t10' \
  $'projection_dispatch_v5\t10' \
  $'projection_plan\t10' \
  $'projection_plan_item\t10' \
  $'projection_plan_observation\t10' \
  $'projection_review\t10' \
  $'relational_operation_request\t30' \
  >"$expected"
if [[ -n "$resume_from" ]]; then
  printf '%s\n' \
    $'claim\t10' \
    $'claim_assessment\t10' \
    $'claim_assessment_apply_v5\t10' \
    $'claim_assessment_review_v5\t10' \
    $'claim_observation\t10' \
    $'claim_revision\t20' \
    $'projection_apply_event\t10' \
    $'projection_dispatch_v5\t10' \
    $'projection_review\t10' \
    $'relational_operation_request\t20' \
    >"$expected"
fi
chmod 0600 "$expected"
capture_partition target "$target_after"
capture_partition non_target "$non_target_after"
verify_target_delta "$target_before" "$target_after" "$expected"
cmp -s "$non_target_before" "$non_target_after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(psql_row "
  SELECT encode(public.digest(convert_to(to_jsonb(claim_row)::text,'UTF8'),
    'sha256'),'hex')
  FROM memory.claim AS claim_row
  WHERE owner_user_id='$owner'::uuid
    AND claim_id='$existing_neko_claim'::uuid")" == "$neko_before" ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim_observation
  WHERE owner_user_id='$owner'::uuid
    AND observation_id='$deferred_observation'::uuid")" == 0 ]]
claim_ids=$(jq -r '[.outcomes[].claim_id] | join(",")' "$claim_apply")
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$owner'::uuid
    AND claim_id=ANY(string_to_array('$claim_ids',',')::uuid[])
    AND status='supported'")" == 10 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$owner'::uuid
    AND claim_id=ANY(string_to_array('$claim_ids',',')::uuid[])
    AND predicate='relationship.has_pet'
    AND canonical_text LIKE 'The user formerly had a pet named %'")" == 2 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.projection_outbox
  WHERE owner_user_id='$owner'::uuid
    AND aggregate_id=ANY(string_to_array('$claim_ids',',')::uuid[])")" == 0 ]]

capture_partition target "$target_replay"
capture_partition non_target "$non_target_replay"
cmp -s "$target_after" "$target_replay"
cmp -s "$non_target_after" "$non_target_replay"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=restore_runtime
restore_runtime
authenticated_health

report="$artifact_dir/production-report.json"
HEAD_VALUE="$head" BACKUP="$backup" BACKUP_SHA="$backup.sha256" \
MIGRATION_SHA="$migration_sha" ROLLBACK_SHA="$rollback_sha" \
STAGE="$stage_apply" REVIEW="$review_apply" CLAIM="$claim_apply" \
QDRANT="$qdrant_before" REPORT="$report" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

stage = json.loads(Path(os.environ["STAGE"]).read_text())
review = json.loads(Path(os.environ["REVIEW"]).read_text())
claim = json.loads(Path(os.environ["CLAIM"]).read_text())
value = {
    "contract_version": "memory_v1_v5_2_pet_profile_claim_production_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD_VALUE"],
    "owner_user_id": stage["owner_user_id"],
    "backup_path": os.environ["BACKUP"],
    "backup_sha256_path": os.environ["BACKUP_SHA"],
    "migration_sha256": os.environ["MIGRATION_SHA"],
    "rollback_sha256": os.environ["ROLLBACK_SHA"],
    "new_claim_count": 10,
    "supported_claim_count": 10,
    "deferred_temporal_reconciliation_count": 1,
    "stage_rows_written": stage["rows_written"],
    "review_rows_written": review["rows_written"],
    "claim_insert_rows": claim["insert_rows"],
    "claim_mutated_rows": claim["mutated_rows"],
    "qdrant_sha256": os.environ["QDRANT"],
    "qdrant_unchanged": True,
    "account_isolation_verified": True,
    "non_target_records_unchanged": True,
    "zero_write_replays_verified": True,
    "retrieval_activated": False,
    "prompt_influence_activated": False,
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

phase=complete
printf 'report=%s\n' "$report"
printf 'backup=%s\n' "$backup"
printf 'memory_v1_v5_2_pet_profile_claim_pipeline_production: PASS\n'

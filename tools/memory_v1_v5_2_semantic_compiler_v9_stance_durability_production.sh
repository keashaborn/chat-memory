#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Materializes one reviewed compiler-v9 stance through
# atom admission, relational staging, trusted-self resolution, and observation
# binding. It stops before claims, projection, Qdrant, retrieval, and prompts.

if [[ "${MEMORY_V1_V5_2_V9_STANCE_DURABILITY_PRODUCTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_V9_STANCE_DURABILITY_PRODUCTION=authorized is required' >&2
  exit 1
fi

[[ "$(id -u)" -eq 0 ]]
repo_root=$(git rev-parse --show-toplevel)
[[ "$repo_root" == /opt/chat-memory ]]
cd "$repo_root"

container=brains-postgres-1
database=memory
python_bin=/opt/chat-memory/venv/bin/python
snapshot_root=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_v9_stance_durability.lock
required_ancestor=9763c5d53859a2a2fe3b60a5c6be1af0354ee3ac
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
self_entity=35029129-27bd-457b-8cb5-82dd37ba32ba
packet=50405677-84aa-5d72-b800-89418fc606e4
evidence=61d4fb6f-b211-491e-8edc-d160efefe17e
proposal=e4b17ef6-5012-5f1a-ae2a-90681fd71d8f
review=aa43ff43-3d9d-5eaf-8972-229f6f2e261c
apply=237451bc-2fcc-5272-b357-9dd2f6452e0d
manifest=manifests/memory_v1_v5_2_semantic_compiler_v9_stance_atom_admission_20260730.json
atom_runner=scripts/memory_v1_v5_2_atom_admission_apply_v2.py
bundle_builder=scripts/memory_v1_v5_2_atom_stage_bundle_v2.py
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
stage_fixture=tests/memory_v1_v5_2_stage_batch_fixture.py
entity_runner=scripts/memory_v1_v5_2_entity_resolution_batch.py
entity_fixture=tests/memory_v1_v5_2_entity_resolution_batch_fixture.py

declare -A expected_sha=(
  ["$manifest"]=9ed0a6a5b726da412ca1404ffe6a9746f4e97167099ed4c54196edf2e3ba366a
  ["$atom_runner"]=721184f8eebf5f4d14b553eb0cf0134960456d8834029263da1d10963868c5e7
  ["$bundle_builder"]=5a611180ecbb3fa3b9220459ef870879e74a8d487847ef76084b379c32ac30bb
  ["$stage_runner"]=3dddef3dbf71862fc42252d6263075ad8d407bc292a4f06760866c816c3699b9
  ["$stage_fixture"]=96461968b5afa0ec2a8aa207ec7ec2096278d851cd51d4e0a7c90541ab5a54ac
  ["$entity_runner"]=7c9c16ba640101acea04b8911fd179d15a13166b4a8069b129e6aa8e4d6487bb
  ["$entity_fixture"]=9d913127b681c0e781daae045357e24cd000a05e3702699d72caefe9f5f682e7
)

declare -A expected_delta=(
  [v5_2_atom_admission_proposal]=1
  [v5_2_atom_admission_review]=1
  [v5_2_atom_admission_apply]=1
  [v5_2_atom_admission_operation]=3
  [relational_stage_batch]=1
  [relational_operation_request]=2
  [entity_mention]=1
  [entity_resolution_plan]=1
  [entity_resolution_candidate]=1
  [observation]=1
  [observation_temporal]=1
  [entity_resolution_apply]=1
  [observation_entity_binding]=1
)

phase=initialization
run_id=
artifact_dir=
status_file=
timers_quiesced=0
brains_quiesced=0
brains_state_before=
timer_state=$(mktemp /tmp/memory-v1-v5-2-v9-stance-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-2-v9-stance-tables.XXXXXX)

fail() {
  printf 'ASSERTION_FAILED=%s\n' "$1" >&2
  exit 1
}

equal() {
  [[ "$2" == "$3" ]] || {
    printf 'ASSERTION_FAILED=%s\nexpected=%s\nactual=%s\n' \
      "$1" "$3" "$2" >&2
    exit 1
  }
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
  source /opt/chat-memory/.env
  set +a
  [[ -n "${VS_SERVICE_TOKEN:-}" ]]
  [[ "$(systemctl is-active brains.service)" == active ]]
  docker exec "$container" pg_isready -U sage -d "$database" >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/readyz \
    | jq -e '.ok==true and .postgres==true' >/dev/null
}

restore_runtime() {
  if [[ "$brains_quiesced" -eq 1 ]]; then
    if [[ "$brains_state_before" == active ]]; then
      systemctl start brains.service
    else
      systemctl stop brains.service
    fi
    [[ "$(systemctl is-active brains.service)" == "$brains_state_before" ]]
    brains_quiesced=0
  fi
  if [[ "$timers_quiesced" -eq 1 ]]; then
    while IFS=$'\t' read -r unit enabled active; do
      [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
      [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
      if [[ "$active" == active ]]; then
        systemctl start "$unit"
      else
        systemctl stop "$unit"
      fi
      [[ "$(systemctl is-active "$unit")" == "$active" ]]
    done <"$timer_state"
    timers_quiesced=0
  fi
}

record_exit() {
  code=$?
  trap - EXIT
  set +e
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
  local before=$1 after=$2 expected_file="$artifact_dir/expected-deltas.tsv"
  : >"$expected_file"
  for table in "${!expected_delta[@]}"; do
    printf '%s\t%s\n' "$table" "${expected_delta[$table]}" >>"$expected_file"
  done
  sort -o "$expected_file" "$expected_file"
  BEFORE="$before" AFTER="$after" EXPECTED="$expected_file" "$python_bin" - <<'PY'
import os
from pathlib import Path

def load(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        table, count, digest = line.split("\t")
        result[table] = (int(count), digest)
    return result

before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
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
    if wanted == 0 and old_state[1] != after[table][1]:
        raise SystemExit(f"unexpected target mutation {table}")
if sum(expected.values()) != 16:
    raise SystemExit("expected row budget is not 16")
PY
  chmod 0600 "$expected_file"
}

[[ -z "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain)" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
head=$(git rev-parse HEAD)
for path in "${!expected_sha[@]}"; do
  [[ -f "$path" ]]
  equal "sha:$path" "$(sha256sum "$path" | awk '{print $1}')" \
    "${expected_sha[$path]}"
done
equal manifest_contract \
  "$(jq -r .manifest_sha256 "$manifest")" \
  8ea964ad2dd9cf2bb6024de775e21f05c96fd4e949b0ca5b8908418a038b54af
authenticated_health

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_${head:0:12}"
artifact_dir="$review_root/v9-stance-durability-production-$run_id"
status_file="$snapshot_root/memory_v1_v5_2_v9_stance_durability_${run_id}.status"
mkdir -m 0700 "$artifact_dir"

phase=preflight_state
equal initial_target_state "$(psql_row "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal
     WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.relational_stage_batch
     WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_mention
     WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.observation
     WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.claim_evidence
     WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid)
  )
")" 0,0,0,0,0

phase=capture_runtime
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
  [[ "$active" != active ]] || systemctl stop "$unit"
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
  systemctl stop brains.service
fi
brains_quiesced=1
! systemctl is-active --quiet brains.service

phase=backup
backup_partial="$snapshot_root/.memory_pre_v9_stance_durability_${run_id}.dump.partial"
backup="$snapshot_root/memory_pre_v9_stance_durability_${run_id}.dump"
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
target_after="$artifact_dir/target-after.tsv"
non_target_before="$artifact_dir/non-target-before.tsv"
non_target_after="$artifact_dir/non-target-after.tsv"
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)

phase=atom_preflight
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  "$python_bin" "$atom_runner" \
  --mode preflight --manifest "$manifest" \
  --output "$artifact_dir/atom-preflight.json"
equal atom_preflight_writes \
  "$(jq -r .persistent_writes "$artifact_dir/atom-preflight.json")" 0

phase=atom_apply
MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY_V2=authorized \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  "$python_bin" "$atom_runner" \
  --mode apply --manifest "$manifest" \
  --output "$artifact_dir/atom-apply.json"
equal atom_apply_writes \
  "$(jq -r .persistent_writes "$artifact_dir/atom-apply.json")" 6

POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  "$python_bin" "$atom_runner" \
  --mode replay --manifest "$manifest" \
  --output "$artifact_dir/atom-replay.json"
equal atom_replay_writes \
  "$(jq -r .persistent_writes "$artifact_dir/atom-replay.json")" 0

phase=stage_build
apply_manifest_sha=$(psql_row "
  SELECT apply_manifest_sha256
  FROM memory.v5_2_atom_admission_apply
  WHERE owner_user_id='$owner'::uuid AND apply_id='$apply'::uuid
")
stage_projection_sha=$(psql_row "
  SELECT stage_projection_sha256
  FROM memory.v5_2_atom_admission_apply
  WHERE owner_user_id='$owner'::uuid AND apply_id='$apply'::uuid
")
jq -n \
  --arg owner "$owner" --arg apply "$apply" \
  --arg proposal "$proposal" --arg review "$review" \
  --arg packet "$packet" --arg evidence "$evidence" \
  --arg apply_manifest_sha "$apply_manifest_sha" \
  --arg proposal_sha 4b0508eb56cc9db8454363834b8eb873ad653d55908a6428cb17ab07f77b8894 \
  --arg stage_projection_sha "$stage_projection_sha" \
  --arg self_entity "$self_entity" \
  '{
    contract_version:"memory_v1_v5_2_atom_stage_build_manifest_v2",
    target_server:"seebx",owner_user_id:$owner,
    case_id:"semantic_compiler_v9_human_being_as_fractal_stance",
    apply_id:$apply,proposal_id:$proposal,review_id:$review,
    packet_id:$packet,evidence_id:$evidence,
    apply_manifest_sha256:$apply_manifest_sha,
    proposal_sha256:$proposal_sha,
    stage_projection_sha256:$stage_projection_sha,
    expected_counts:{entity_mentions:1,observations:1,comparison_hints:0,deferrals:0},
    expected_resolutions:{
      e00:{action:"link_existing",decision_state:"auto_link_eligible",
        selected_entity_id:$self_entity,proposed_entity:null,
        review_reason_codes:["trusted_owner_self_binding"]}
    }
  }' >"$artifact_dir/stage-build-manifest.json"
chmod 0600 "$artifact_dir/stage-build-manifest.json"

POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$bundle_builder" build \
  --manifest "$artifact_dir/stage-build-manifest.json" \
  --output-root "$artifact_dir" >"$artifact_dir/stage-build-report.json"
equal stage_build_writes \
  "$(jq -r .database_writes "$artifact_dir/stage-build-report.json")" 0
equal stage_expected_rows \
  "$(jq -r .expected_new_rows "$artifact_dir/stage-build-report.json")" 7

phase=stage_security_probe
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$bundle_builder" probe \
  --bundle "$artifact_dir/bundle.json" \
  --apply-id "$apply" --other-owner-user-id "$other_owner" \
  >"$artifact_dir/stage-probe.json"
equal stage_cross_owner \
  "$(jq -r .cross_owner_rejection "$artifact_dir/stage-probe.json")" P0002
equal stage_tamper \
  "$(jq -r .tampered_projection_rejection "$artifact_dir/stage-probe.json")" 23514

phase=stage_plan
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$stage_runner" plan \
  --manifest "$artifact_dir/stage-manifest.json" \
  --review-root "$artifact_dir" \
  --output "$artifact_dir/stage-plan.json"
PYTHONPATH="$repo_root" "$python_bin" "$stage_fixture" authorize \
  --plan "$artifact_dir/stage-plan.json" \
  --output "$artifact_dir/stage-authorization.json" --head "$head"

phase=stage_apply
MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$stage_runner" apply \
  --plan "$artifact_dir/stage-plan.json" \
  --authorization "$artifact_dir/stage-authorization.json" \
  --review-root "$artifact_dir" \
  --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
  --output "$artifact_dir/stage-apply.json"
equal stage_rows \
  "$(jq -r .database_rows_created "$artifact_dir/stage-apply.json")" 7
equal stage_replay \
  "$(jq -r '.replayed[0].outcome' "$artifact_dir/stage-apply.json")" replayed

phase=entity_plan
resolution=$(psql_row "
  SELECT resolution_id::text FROM memory.entity_resolution_plan
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid
")
observation=$(psql_row "
  SELECT observation_id::text FROM memory.observation
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid
")
jq -n --arg owner "$owner" --arg resolution "$resolution" \
  '{
    contract_version:"memory_v1_v5_2_entity_resolution_batch_manifest_v1",
    target_server:"seebx",owner_user_id:$owner,
    expected_total_bindings:1,expected_new_rows:3,
    items:[{resolution_id:$resolution,operation:"auto_apply",
      expected_action:"link_existing",
      expected_decision_state:"auto_link_eligible",review_reason:null}]
  }' >"$artifact_dir/entity-manifest.json"
chmod 0600 "$artifact_dir/entity-manifest.json"

POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$entity_runner" plan \
  --manifest "$artifact_dir/entity-manifest.json" \
  --review-root "$artifact_dir" \
  --output "$artifact_dir/entity-plan.json"
jq --arg owner "$other_owner" '.owner_user_id=$owner' \
  "$artifact_dir/entity-manifest.json" >"$artifact_dir/entity-cross-owner.json"
chmod 0600 "$artifact_dir/entity-cross-owner.json"
if POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$entity_runner" plan \
  --manifest "$artifact_dir/entity-cross-owner.json" \
  --review-root "$artifact_dir" \
  --output "$artifact_dir/entity-cross-owner-plan.json" >/dev/null 2>&1; then
  fail cross_owner_entity_plan_was_not_rejected
fi
PYTHONPATH="$repo_root" "$python_bin" "$entity_fixture" \
  --plan "$artifact_dir/entity-plan.json" \
  --output "$artifact_dir/entity-authorization.json" --head "$head"

phase=entity_apply
MEMORY_V1_V5_2_ENTITY_RESOLUTION_BATCH_APPLY=authorized \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$entity_runner" apply \
  --plan "$artifact_dir/entity-plan.json" \
  --authorization "$artifact_dir/entity-authorization.json" \
  --review-root "$artifact_dir" \
  --confirm RECONCILE_REVIEW_AND_APPLY_OWNER_V5_2_ENTITY_RESOLUTIONS_ONLY \
  --output "$artifact_dir/entity-apply.json"
equal entity_rows \
  "$(jq -r .database_rows_created "$artifact_dir/entity-apply.json")" 3
equal binding_rows \
  "$(jq -r .bindings_created "$artifact_dir/entity-apply.json")" 1
equal entity_replay \
  "$(jq -r '.replayed[0].apply_outcome' "$artifact_dir/entity-apply.json")" replayed

phase=postflight
equal exact_semantics "$(psql_row "
  SELECT concat_ws('|',
    resolution.action::text,resolution.decision_state::text,
    resolution.selected_entity_id::text,observation.predicate,
    binding.subject_entity_id::text,
    (SELECT count(*)::text FROM memory.claim_observation AS linked
     WHERE linked.owner_user_id=observation.owner_user_id
       AND linked.observation_id=observation.observation_id)
  )
  FROM memory.entity_resolution_plan AS resolution
  JOIN memory.observation
    ON observation.owner_user_id=resolution.owner_user_id
   AND observation.evidence_id=resolution.evidence_id
  JOIN memory.observation_entity_binding AS binding
    ON binding.owner_user_id=observation.owner_user_id
   AND binding.observation_id=observation.observation_id
  WHERE resolution.owner_user_id='$owner'::uuid
    AND resolution.resolution_id='$resolution'::uuid
    AND observation.observation_id='$observation'::uuid
")" "link_existing|auto_link_eligible|$self_entity|stance.reported|$self_entity|0"

capture_partition target "$target_after"
capture_partition non_target "$non_target_after"
verify_target_delta "$target_before" "$target_after"
cmp -s "$non_target_before" "$non_target_after"
equal qdrant_unchanged "$(qdrant_signature)" "$qdrant_before"

phase=restore
restore_runtime
authenticated_health
equal qdrant_after_restore "$(qdrant_signature)" "$qdrant_before"
[[ -z "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain)" ]]

phase=report
report="$artifact_dir/report.json"
REPORT="$report" HEAD="$head" OWNER="$owner" PACKET="$packet" \
EVIDENCE="$evidence" RESOLUTION="$resolution" OBSERVATION="$observation" \
BACKUP="$backup" BACKUP_SHA="$backup_sha" QDRANT="$qdrant_before" \
ARTIFACT_DIR="$artifact_dir" "$python_bin" - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_v5_2_semantic_compiler_v9_stance_durability_production_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": os.environ["OWNER"],
    "packet_id": os.environ["PACKET"],
    "evidence_id": os.environ["EVIDENCE"],
    "resolution_id": os.environ["RESOLUTION"],
    "observation_id": os.environ["OBSERVATION"],
    "backup": {
        "path": os.environ["BACKUP"],
        "sha256": os.environ["BACKUP_SHA"],
    },
    "artifact_dir": os.environ["ARTIFACT_DIR"],
    "rows": {
        "atom_admission_phase": 6,
        "relational_staging_phase": 7,
        "entity_resolution_and_binding_phase": 3,
        "total_unique_rows": 16,
        "claims": 0,
        "projections": 0,
    },
    "row_breakdown": {
        "operation_requests": 2,
        "entity_resolution_apply": 1,
        "observation_binding": 1,
    },
    "checks": {
        "fresh_backup": True,
        "hash_locked_inputs": True,
        "transactional_phase_applies": True,
        "zero_write_replays": True,
        "cross_owner_rejected": True,
        "exact_target_owner_deltas": True,
        "non_target_and_global_rows_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "external_model_calls": 0,
        "claims_written": 0,
        "retrieval_changes": 0,
        "prompt_changes": 0,
        "timers_restored": True,
        "service_healthy": True,
    },
    "hard_stop": "before_claim_candidate_generation_or_durable_claims_or_projection",
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf '%s\n' \
  'MEMORY_V1_V5_2_SEMANTIC_COMPILER_V9_STANCE_DURABILITY_PRODUCTION=PASS' \
  "head=$head" \
  "report=$report" \
  "backup=$backup" \
  'rows_created=16' \
  'claim_rows=0' \
  'qdrant_writes=0' \
  'model_calls=0' \
  'retrieval_changes=0' \
  'prompt_changes=0'

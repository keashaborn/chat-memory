#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the narrow disposed-evidence compatibility guard
# and transactionally stages one exact, previously admitted V5.2 stance atom.
# It stops before entity apply, observation binding, claims, Qdrant, retrieval,
# or prompt influence.

if [[ "${MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STAGE_PRODUCTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STAGE_PRODUCTION=authorized is required' >&2
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
review_base=/home/ubuntu/memory-v1-reviews
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
apply_id=2fca9e6a-e5db-5568-bcd1-5e43b7e2a5d3
evidence_id=049205b4-9a6c-5e1a-bb8f-2ab9f05f8964
self_entity_id=35029129-27bd-457b-8cb5-82dd37ba32ba
build_manifest=manifests/memory_v1_v5_2_evidence_context_atom_stage_20260726.json
bundle_builder=scripts/memory_v1_v5_2_atom_stage_bundle_v2.py
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
stage_fixture=tests/memory_v1_v5_2_stage_batch_fixture.py
compat_migration=ops/sql/20260726_memory_v1_v5_2_disposition_atom_projection_compat.sql
compat_rollback=ops/sql/20260726_memory_v1_v5_2_disposition_atom_projection_compat_rollback.sql
compat_test=tests/memory_v1_v5_2_disposition_atom_projection_compat.sql
clone_test=tools/memory_v1_v5_2_evidence_context_atom_stage_clone.sh
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_evidence_context_stage.lock

phase=initialization
run_tag=
status_file=
units_quiesced=0
compat_installed=0
stage_applied=0
unit_state=$(mktemp /tmp/memory-v1-v5-2-evidence-context-stage-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-2-evidence-context-stage-tables.XXXXXX)
clone_output=$(mktemp /tmp/memory-v1-v5-2-evidence-context-stage-clone.XXXXXX)

psql_row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
}

run_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$database"
}

assert_equal() {
  local label=$1 actual=$2 expected=$3
  if [[ "$actual" != "$expected" ]]; then
    printf 'ASSERTION_FAILED=%s\nexpected=%s\nactual=%s\n' \
      "$label" "$expected" "$actual" >&2
    exit 1
  fi
}

hash_lock() {
  assert_equal "$2" "$(sha256sum "$1" | awk '{print $1}')" "$3"
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
  if [[ "$exit_code" -ne 0 && "$compat_installed" -eq 1 \
        && "$stage_applied" -eq 0 ]]; then
    run_sql <"$compat_rollback" || exit_code=1
    compat_installed=0
  fi
  restore_timers || exit_code=1
  rm -f "$unit_state" "$table_list" "$clone_output"
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
    "relational_stage_batch": 1,
    "relational_operation_request": 1,
    "entity_mention": 1,
    "entity_resolution_plan": 1,
    "entity_resolution_candidate": 1,
    "observation": 1,
    "observation_temporal": 1,
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
if sum(expected.values()) != 7:
    raise SystemExit("target row budget is not seven")
PY
}

target_stage_counts() {
  psql_row "
    SELECT concat_ws(',',
      (SELECT count(*) FROM memory.relational_stage_batch
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.relational_operation_request
       WHERE owner_user_id='$target_owner'::uuid
         AND target_key='$evidence_id'),
      (SELECT count(*) FROM memory.entity_mention
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_plan
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_candidate AS candidate
       JOIN memory.entity_resolution_plan AS resolution
         USING(owner_user_id,resolution_id)
       WHERE resolution.owner_user_id='$target_owner'::uuid
         AND resolution.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.observation
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.observation_temporal AS temporal
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$target_owner'::uuid
         AND observation.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_review AS review
       JOIN memory.entity_resolution_plan AS resolution
         USING(owner_user_id,resolution_id)
       WHERE resolution.owner_user_id='$target_owner'::uuid
         AND resolution.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_apply AS applied
       JOIN memory.entity_resolution_plan AS resolution
         USING(owner_user_id,resolution_id)
       WHERE resolution.owner_user_id='$target_owner'::uuid
         AND resolution.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.observation_entity_binding AS binding
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$target_owner'::uuid
         AND observation.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.claim_observation AS linked
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$target_owner'::uuid
         AND observation.evidence_id='$evidence_id'::uuid)
    )"
}

[[ "$(id -u)" -eq 0 ]]
[[ "$repo_root" == /opt/chat-memory ]]
[[ -z "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain)" ]]
head=$(git rev-parse HEAD)
git merge-base --is-ancestor 0bf20a7bd0155be0d0184bf63185ecb057f256e1 "$head"
hash_lock "$build_manifest" build_manifest_sha \
  222e0c61bef072cbcc9c9235f9a226386c6cb26a941e28c0cc4a22c833833c6a
hash_lock "$bundle_builder" bundle_builder_sha \
  5a611180ecbb3fa3b9220459ef870879e74a8d487847ef76084b379c32ac30bb
hash_lock "$stage_runner" stage_runner_sha \
  3dddef3dbf71862fc42252d6263075ad8d407bc292a4f06760866c816c3699b9
hash_lock "$stage_fixture" stage_fixture_sha \
  96461968b5afa0ec2a8aa207ec7ec2096278d851cd51d4e0a7c90541ab5a54ac
hash_lock "$compat_migration" compat_migration_sha \
  ad2bf4951fb54d3677ae85be6b4e312dee8a6cd54984d7036cba1e75c22ac1dc
hash_lock "$compat_rollback" compat_rollback_sha \
  5864d554c04428056c682c9e097976bbe5b1a80c2387d84d6de450de30671c5e
hash_lock "$compat_test" compat_test_sha \
  fed522ee5ed740c62f4b65edfe2cd75444498d8c3189d34421d18b99814adc6d
hash_lock "$clone_test" clone_test_sha \
  d57d991f3fb73c64e2a17b7cc03243ca016d10271c90ab39101ebbee36efefbb
[[ "$(systemctl is-active brains.service)" == active ]]
docker exec "$container" pg_isready -U sage -d "$database" >/dev/null

phase=clone_gate
"$clone_test" >"$clone_output"
grep -Fxq 'MEMORY_V1_V5_2_EVIDENCE_CONTEXT_ATOM_STAGE_CLONE=PASS' \
  "$clone_output"

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_evidence_context_stage_${run_tag}.status"
reviews="$review_base/evidence-context-stage-$run_tag"
mkdir -m 0700 "$reviews"

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
backup_partial="$snapshot_dir/.memory_pre_v5_2_evidence_context_stage_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_evidence_context_stage_${run_tag}.dump"
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
target_before="$reviews/target-before.tsv"
target_after="$reviews/target-after.tsv"
non_target_before="$reviews/non-target-before.tsv"
non_target_after="$reviews/non-target-after.tsv"
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)
entities_before=$(psql_row "
  SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")
claims_before=$(psql_row "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")
assert_equal initial_target_stage "$(target_stage_counts)" \
  '0,0,0,0,0,0,0,0,0,0,0'
assert_equal old_terminal_disposition "$(psql_row "
  SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$target_owner'::uuid
    AND evidence_id='$evidence_id'::uuid
    AND disposition='terminal_no_stage'
    AND NOT promotion_eligible")" 1
assert_equal exact_atom_apply "$(psql_row "
  SELECT count(*)
  FROM memory.v5_2_atom_admission_apply
  WHERE owner_user_id='$target_owner'::uuid
    AND evidence_id='$evidence_id'::uuid
    AND apply_id='$apply_id'::uuid
    AND stage_projection_sha256=
      '2e8dbe82a0174e4198beaa6f45a467e752d820782ce78df3075c21d227040e7f'")" 1

phase=install
run_sql <"$compat_migration"
compat_installed=1
run_sql <"$compat_migration"
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 -f "$compat_test"

phase=build
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$bundle_builder" build \
  --manifest "$build_manifest" --output-root "$reviews" \
  >"$reviews/build-report.json"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$bundle_builder" probe \
  --bundle "$reviews/bundle.json" --apply-id "$apply_id" \
  --other-owner-user-id "$other_owner" >"$reviews/probe-report.json"
assert_equal expected_stage_rows \
  "$(jq -r '.expected_new_rows' "$reviews/build-report.json")" 7
assert_equal cross_owner_rejection \
  "$(jq -r '.cross_owner_rejection' "$reviews/probe-report.json")" P0002
assert_equal tampered_projection_rejection \
  "$(jq -r '.tampered_projection_rejection' "$reviews/probe-report.json")" 23514

POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$stage_runner" plan \
  --manifest "$reviews/stage-manifest.json" \
  --review-root "$reviews" --output "$reviews/stage-plan.json"
"$python_bin" "$stage_fixture" authorize \
  --plan "$reviews/stage-plan.json" \
  --output "$reviews/stage-authorization.json" --head "$head"

phase=stage
MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized \
  POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$stage_runner" apply \
  --plan "$reviews/stage-plan.json" \
  --authorization "$reviews/stage-authorization.json" \
  --review-root "$reviews" \
  --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
  --output "$reviews/stage-apply.json"
stage_applied=1
assert_equal stage_rows \
  "$(jq -r '.database_rows_created' "$reviews/stage-apply.json")" 7
assert_equal replay_rows \
  "$(jq -r '.checks.replay_rows_written' "$reviews/stage-apply.json")" 0

phase=postflight
assert_equal exact_stage_counts "$(target_stage_counts)" \
  '1,1,1,1,1,1,1,0,0,0,0'
assert_equal self_resolution "$(psql_row "
  SELECT concat_ws('|',
    action::text,decision_state::text,selected_entity_id::text,
    review_reason_codes::text)
  FROM memory.entity_resolution_plan
  WHERE owner_user_id='$target_owner'::uuid
    AND evidence_id='$evidence_id'::uuid")" \
  "link_existing|auto_link_eligible|$self_entity_id|[\"trusted_owner_self_binding\"]"
assert_equal observation_predicate "$(psql_row "
  SELECT predicate FROM memory.observation
  WHERE owner_user_id='$target_owner'::uuid
    AND evidence_id='$evidence_id'::uuid")" stance.reported
assert_equal entities_unchanged "$(psql_row "
  SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")" "$entities_before"
assert_equal claims_unchanged "$(psql_row "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")" "$claims_before"
assert_equal old_terminal_disposition_preserved "$(psql_row "
  SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$target_owner'::uuid
    AND evidence_id='$evidence_id'::uuid
    AND disposition='terminal_no_stage'
    AND NOT promotion_eligible")" 1

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
report="$snapshot_dir/memory_v1_v5_2_evidence_context_stage_${run_tag}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$head" --arg owner_user_id "$target_owner" \
  --arg evidence_id "$evidence_id" --arg apply_id "$apply_id" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg review_root "$reviews" \
  --arg target_before "$target_before" --arg target_after "$target_after" \
  --arg non_target_before "$non_target_before" \
  --arg non_target_after "$non_target_after" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:"memory_v1_v5_2_evidence_context_stage_production_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    owner_user_id:$owner_user_id,evidence_id:$evidence_id,apply_id:$apply_id,
    backup:{path:$backup,sha256:$backup_sha256},review_root:$review_root,
    rows:{total:7,batches:1,operation_requests:1,mentions:1,
      resolutions:1,candidates:1,observations:1,temporals:1,
      entity_reviews:0,entity_applies:0,bindings:0,claims:0},
    evidence:{target_before:$target_before,target_after:$target_after,
      non_target_before:$non_target_before,non_target_after:$non_target_after,
      qdrant_sha256:$qdrant_sha256},
    checks:{clone_gate:true,fresh_backup:true,hash_locked_inputs:true,
      compatibility_security_test:true,transactional_stage:true,
      zero_write_replay:true,account_isolation:true,
      exact_target_owner_deltas:true,non_target_and_global_rows_unchanged:true,
      qdrant_unchanged:true,timers_restored:true,service_healthy:true,
      legacy_terminal_disposition_preserved:true,external_model_calls:0,
      retrieval_changes:0,prompt_changes:0},
    hard_stop:"before_entity_apply_observation_binding_claims_projection_qdrant_retrieval_or_prompt_influence"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf '%s\n' \
  'MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STAGE_PRODUCTION=PASS' \
  "head=$head" \
  "report=$report" \
  "backup=$backup" \
  'stage_rows_created=7' \
  'same_run_replay_rows=0' \
  'self_resolution=trusted_owner_self_binding' \
  'legacy_terminal_disposition_preserved=true' \
  'entity_apply_rows=0' \
  'observation_binding_rows=0' \
  'claim_rows_created=0' \
  'qdrant_writes=0' \
  'retrieval_changes=0' \
  'prompt_changes=0'

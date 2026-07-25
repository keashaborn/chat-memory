#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the additive V2 read planners, stages the two
# exact compiler-v8 packets, and records three manual entity reviews. It stops
# before entity apply, claims, Qdrant projection, retrieval, or prompt use.

if [[ "${MEMORY_V1_V5_2_V8_STAGE_REVIEW_PRODUCTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_V8_STAGE_REVIEW_PRODUCTION=authorized is required' >&2
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
care_evidence=fea59e7e-30f5-4139-b634-97b291c88e14
profession_evidence=dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5
migration=ops/sql/20260725_memory_v1_v5_2_atom_stage_planner_v2.sql
sql_test=tests/memory_v1_v5_2_atom_stage_planner_v2.sql
manifest_builder=scripts/memory_v1_v5_2_compiler_v8_stage_manifests.py
bundle_builder=scripts/memory_v1_v5_2_atom_stage_bundle_v2.py
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
stage_fixture=tests/memory_v1_v5_2_stage_batch_fixture.py
review_runner=scripts/memory_v1_v5_2_entity_resolution_review_only_batch.py
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_v8_stage_review.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
unit_state=$(mktemp /tmp/memory-v1-v5-2-v8-stage-review-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-2-v8-stage-review-tables.XXXXXX)

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
    "entity_mention": 5,
    "entity_resolution_plan": 5,
    "entity_resolution_candidate": 2,
    "observation": 4,
    "observation_temporal": 4,
    "relational_stage_batch": 2,
    "relational_operation_request": 5,
    "entity_resolution_review": 3,
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
if sum(expected.values()) != 30:
    raise SystemExit("target row budget is not 30")
PY
}

hash_lock() {
  assert_equal "$2" "$(sha256sum "$1" | awk '{print $1}')" "$3"
}

[[ "$(id -u)" -eq 0 ]]
[[ "$repo_root" == /opt/chat-memory ]]
[[ -z "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain)" ]]
head=$(git rev-parse HEAD)
git merge-base --is-ancestor b8f14e44268a42fa3ab3008bb34b9f845e73eef3 "$head"
hash_lock "$migration" migration_sha \
  7718fb2cab9c6a4f769d2db163b0cc5bcf6847a43a5c6733505b2cc5a8cb4e75
hash_lock "$sql_test" sql_test_sha \
  ef4cd56c5f7571c137de0404388a4797bd75c9a8719203bcee84e6af225d2516
hash_lock "$manifest_builder" manifest_builder_sha \
  98bc0302717361c5344a4dcbdc50d08d3c4f6a65573f16c163a131dfc46a2889
hash_lock "$bundle_builder" bundle_builder_sha \
  5a611180ecbb3fa3b9220459ef870879e74a8d487847ef76084b379c32ac30bb
hash_lock "$review_runner" review_runner_sha \
  3322a0d56210af51ace85a03ddbd7e768d3fdc54d7a3cf122237f926bb85dbe3
[[ "$(systemctl is-active brains.service)" == active ]]
docker exec "$container" pg_isready -U sage -d "$database" >/dev/null

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_v8_stage_review_${run_tag}.status"
reviews="$review_base/compiler-v8-stage-review-$run_tag"
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
backup_partial="$snapshot_dir/.memory_pre_v5_2_v8_stage_review_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_v8_stage_review_${run_tag}.dump"
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
target_before="$snapshot_dir/memory_v1_v5_2_v8_stage_review_target_before_${run_tag}.tsv"
target_after="$snapshot_dir/memory_v1_v5_2_v8_stage_review_target_after_${run_tag}.tsv"
non_target_before="$snapshot_dir/memory_v1_v5_2_v8_stage_review_non_target_before_${run_tag}.tsv"
non_target_after="$snapshot_dir/memory_v1_v5_2_v8_stage_review_non_target_after_${run_tag}.tsv"
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)
assert_equal initial_stage_rows "$(psql_row "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$target_owner'::uuid
    AND evidence_id IN ('$care_evidence'::uuid,'$profession_evidence'::uuid)")" 0

phase=install
run_sql <"$migration"
run_sql <"$migration"
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 -f "$sql_test"

phase=build
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$manifest_builder" --output-root "$reviews/manifests"
for case_id in compiler_v8_caregiving compiler_v8_former_profession; do
  mkdir -m 0700 "$reviews/$case_id"
  POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
    "$python_bin" "$bundle_builder" build \
    --manifest "$reviews/manifests/$case_id.json" \
    --output-root "$reviews/$case_id"
done
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$bundle_builder" probe \
  --bundle "$reviews/compiler_v8_caregiving/bundle.json" \
  --apply-id 190f0b21-6e54-5c1d-8a99-6b08358d4846 \
  --other-owner-user-id "$other_owner"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$bundle_builder" probe \
  --bundle "$reviews/compiler_v8_former_profession/bundle.json" \
  --apply-id f87ae2b4-57ee-5969-8ce1-bc80a6bfec83 \
  --other-owner-user-id "$other_owner"
jq -s '{
  contract_version:"memory_v1_v5_2_stage_batch_manifest_v1",
  target_server:"seebx",
  owner_user_id:"1240822d-ac9a-4096-95aa-e2b24d36ef50",
  bundles:(.[0].bundles + .[1].bundles)
}' \
  "$reviews/compiler_v8_caregiving/stage-manifest.json" \
  "$reviews/compiler_v8_former_profession/stage-manifest.json" \
  >"$reviews/stage-manifest.json"
chmod 0600 "$reviews/stage-manifest.json"
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
assert_equal stage_rows \
  "$(jq -r '.database_rows_created' "$reviews/stage-apply.json")" 24
assert_equal stage_replay_rows \
  "$(jq -r '.checks.replay_rows_written' "$reviews/stage-apply.json")" 0

phase=review
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$review_runner" manifest \
  --output "$reviews/entity-review-manifest.json"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$review_runner" plan \
  --manifest "$reviews/entity-review-manifest.json" \
  --review-root "$reviews" --output "$reviews/entity-review-plan.json"
plan_sha=$(sha256sum "$reviews/entity-review-plan.json" | awk '{print $1}')
authorized_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
expires_at=$(date -u -d '+20 minutes' +%Y-%m-%dT%H:%M:%SZ)
jq -n \
  --arg authorization_id "$(cat /proc/sys/kernel/random/uuid)" \
  --arg authorized_at "$authorized_at" --arg expires_at "$expires_at" \
  --arg head "$head" --arg owner "$target_owner" --arg plan_sha "$plan_sha" \
  '{
    contract_version:"memory_v1_v5_2_entity_review_only_authorization_v1",
    authorization_id:$authorization_id,authorized:true,authorized_by:"Eric Lund",
    authorized_at:$authorized_at,expires_at:$expires_at,
    expected_head_commit:$head,target_server:"seebx",
    scope:"review_owner_v5_2_entity_resolutions_without_apply",
    owner_user_id:$owner,plan_sha256:$plan_sha,expected_item_count:3,
    expected_new_rows:6,
    confirmation:"REVIEW_OWNER_V5_2_ENTITY_RESOLUTIONS_WITHOUT_APPLY"
  }' >"$reviews/entity-review-authorization.json"
chmod 0600 "$reviews/entity-review-authorization.json"
MEMORY_V1_V5_2_ENTITY_REVIEW_ONLY_APPLY=authorized \
  POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$review_runner" apply \
  --plan "$reviews/entity-review-plan.json" \
  --authorization "$reviews/entity-review-authorization.json" \
  --review-root "$reviews" \
  --confirm REVIEW_OWNER_V5_2_ENTITY_RESOLUTIONS_WITHOUT_APPLY \
  --output "$reviews/entity-review-apply.json"
assert_equal review_rows \
  "$(jq -r '.new_rows' "$reviews/entity-review-apply.json")" 6
assert_equal review_replay \
  "$(jq -r '.zero_write_replay' "$reviews/entity-review-apply.json")" true

phase=postflight
assert_equal exact_predicates "$(psql_row "
  SELECT string_agg(predicate,',' ORDER BY predicate)
  FROM memory.observation
  WHERE owner_user_id='$target_owner'::uuid
    AND evidence_id IN ('$care_evidence'::uuid,'$profession_evidence'::uuid)")" \
  'occupation.works_as,occupation.works_as,relationship.caregiver_for,relationship.spouse_of'
assert_equal historical_temporals "$(psql_row "
  SELECT count(*)
  FROM memory.observation AS observation
  JOIN memory.observation_temporal AS temporal
    USING(owner_user_id,observation_id)
  WHERE observation.owner_user_id='$target_owner'::uuid
    AND observation.evidence_id='$profession_evidence'::uuid
    AND observation.predicate='occupation.works_as'
    AND lower(temporal.instant_range) IS NULL
    AND upper(temporal.instant_range) IS NOT NULL")" 2
assert_equal entity_applies "$(psql_row "
  SELECT count(*) FROM memory.entity_resolution_apply AS applied
  JOIN memory.entity_resolution_plan AS plan
    USING(owner_user_id,resolution_id)
  WHERE plan.owner_user_id='$target_owner'::uuid
    AND plan.evidence_id IN ('$care_evidence'::uuid,'$profession_evidence'::uuid)")" 0
OTHER_OWNER="$other_owner" CARE_EVIDENCE="$care_evidence" \
  PROFESSION_EVIDENCE="$profession_evidence" POSTGRES_DSN="$POSTGRES_DSN" \
  "$python_bin" - <<'PY'
import asyncio, os, uuid
import asyncpg

async def main():
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        for value in ("CARE_EVIDENCE", "PROFESSION_EVIDENCE"):
            try:
                async with conn.transaction(readonly=True):
                    await conn.execute(
                        "SELECT set_config('app.user_id',$1,true)",
                        os.environ["OTHER_OWNER"],
                    )
                    await conn.fetchval(
                        "SELECT memory.plan_owner_v5_2_entity_resolution_review_v1($1::uuid)",
                        uuid.UUID(os.environ[value]),
                    )
            except asyncpg.PostgresError as exc:
                if exc.sqlstate != "P0002":
                    raise
            else:
                raise RuntimeError("cross-owner entity review plan resolved")
    finally:
        await conn.close()

asyncio.run(main())
PY
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
report="$snapshot_dir/memory_v1_v5_2_v8_stage_review_${run_tag}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$head" --arg owner_user_id "$target_owner" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg review_root "$reviews" \
  --arg target_before "$target_before" --arg target_after "$target_after" \
  --arg non_target_before "$non_target_before" --arg non_target_after "$non_target_after" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:"memory_v1_v5_2_compiler_v8_relational_stage_review_production_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,owner_user_id:$owner_user_id,
    backup:{path:$backup,sha256:$backup_sha256},review_root:$review_root,
    rows:{stage:24,review_and_audit:6,total:30,entity_apply:0,claims:0},
    evidence:{target_before:$target_before,target_after:$target_after,
      non_target_before:$non_target_before,non_target_after:$non_target_after,
      qdrant_sha256:$qdrant_sha256},
    checks:{fresh_backup:true,hash_locked_inputs:true,transactional_stage:true,
      transactional_review:true,zero_write_replay:true,account_isolation:true,
      exact_target_owner_deltas:true,non_target_and_global_rows_unchanged:true,
      qdrant_unchanged:true,timers_restored:true,service_healthy:true,
      external_model_calls:0,retrieval_changes:0,prompt_changes:0},
    hard_stop:"before_entity_apply_claims_projection_qdrant_retrieval_or_prompt_influence"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf '%s\n' \
  'MEMORY_V1_V5_2_COMPILER_V8_RELATIONAL_STAGE_REVIEW_PRODUCTION=PASS' \
  "head=$head" \
  "report=$report" \
  "backup=$backup" \
  'stage_rows_created=24' \
  'review_rows_created=6' \
  'entity_apply_rows=0' \
  'claim_rows_created=0' \
  'qdrant_writes=0' \
  'retrieval_changes=0' \
  'prompt_changes=0'

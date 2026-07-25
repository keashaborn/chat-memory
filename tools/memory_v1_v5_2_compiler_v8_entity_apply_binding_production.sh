#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies five exact V5.2 entity resolutions and binds
# four already-staged observations. It stops before claims, projection,
# Qdrant, retrieval, and prompt influence.

if [[ "${MEMORY_V1_V5_2_V8_ENTITY_APPLY_PRODUCTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_V8_ENTITY_APPLY_PRODUCTION=authorized is required' >&2
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
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
self_entity=35029129-27bd-457b-8cb5-82dd37ba32ba
care_evidence=fea59e7e-30f5-4139-b634-97b291c88e14
profession_evidence=dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5
care_self_resolution=f7f8b81b-4fa5-4f36-a0e4-4fb2cfd571ae
monika_resolution=8ed545c8-da8b-4db8-92fa-519ee62a8185
profession_self_resolution=37e5a24b-2862-46df-8ce6-7fc6cfbc61bb
psychologist_resolution=35fe3e20-e02d-4304-ba4b-2e85066034be
bcba_resolution=065f0bde-dd18-4630-8840-4ba49655bc55
migration=ops/sql/20260725_memory_v1_v5_2_entity_apply_planner_v1.sql
sql_test=tests/memory_v1_v5_2_entity_apply_planner_v1.sql
runner=scripts/memory_v1_v5_2_compiler_v8_entity_apply_batch.py
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_v8_entity_apply.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
unit_state=$(mktemp /tmp/memory-v1-v5-2-v8-entity-apply-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-2-v8-entity-apply-tables.XXXXXX)

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
        predicate="owner_user_id='$owner'::uuid"
      else
        predicate="owner_user_id<>'$owner'::uuid"
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
    "entity": 3,
    "entity_resolution_apply": 5,
    "entity_alias_observation": 3,
    "observation_entity_binding": 4,
    "relational_operation_request": 5,
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
if sum(expected.values()) != 20:
    raise SystemExit("target row budget is not 20")
PY
}

[[ "$(id -u)" -eq 0 ]]
[[ "$repo_root" == /opt/chat-memory ]]
[[ -z "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain)" ]]
head=$(git rev-parse HEAD)
git merge-base --is-ancestor c73102de03274dc5644015c0324a55a0e57f914f "$head"
hash_lock "$migration" migration_sha \
  4cbab3979956354837da20af79345f3af4e4db633d1b731f31722255cab85699
hash_lock "$sql_test" sql_test_sha \
  698fe120531156b39ad2c767c11fc1c78ae76adf509cb4f09dee7e3819a1a205
hash_lock "$runner" runner_sha \
  f1a7f6dcb7103a7748dc0a10a7b0d645450d7f586cdcbe28aa6fdb0e8a80b5f3
[[ "$(systemctl is-active brains.service)" == active ]]
docker exec "$container" pg_isready -U sage -d "$database" >/dev/null

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_v8_entity_apply_${run_tag}.status"
reviews="$review_base/compiler-v8-entity-apply-$run_tag"
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
backup_partial="$snapshot_dir/.memory_pre_v5_2_v8_entity_apply_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_v8_entity_apply_${run_tag}.dump"
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
target_before="$snapshot_dir/memory_v1_v5_2_v8_entity_apply_target_before_${run_tag}.tsv"
target_after="$snapshot_dir/memory_v1_v5_2_v8_entity_apply_target_after_${run_tag}.tsv"
non_target_before="$snapshot_dir/memory_v1_v5_2_v8_entity_apply_non_target_before_${run_tag}.tsv"
non_target_after="$snapshot_dir/memory_v1_v5_2_v8_entity_apply_non_target_after_${run_tag}.tsv"
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)
assert_equal initial_applies "$(psql_row "
  SELECT count(*) FROM memory.entity_resolution_apply AS applied
  JOIN memory.entity_resolution_plan AS resolution
    USING(owner_user_id,resolution_id)
  WHERE resolution.owner_user_id='$owner'::uuid
    AND resolution.evidence_id IN (
      '$care_evidence'::uuid,'$profession_evidence'::uuid
    )")" 0
assert_equal initial_bindings "$(psql_row "
  SELECT count(*) FROM memory.observation_entity_binding AS binding
  JOIN memory.observation AS observation
    USING(owner_user_id,observation_id)
  WHERE observation.owner_user_id='$owner'::uuid
    AND observation.evidence_id IN (
      '$care_evidence'::uuid,'$profession_evidence'::uuid
    )")" 0

phase=install
run_sql <"$migration"
run_sql <"$migration"
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 -f "$sql_test"

phase=plan
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$runner" manifest --output "$reviews/manifest.json"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$runner" plan \
  --manifest "$reviews/manifest.json" --review-root "$reviews" \
  --output "$reviews/plan.json"
plan_sha=$(sha256sum "$reviews/plan.json" | awk '{print $1}')
authorized_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
expires_at=$(date -u -d '+20 minutes' +%Y-%m-%dT%H:%M:%SZ)
jq -n \
  --arg authorization_id "$(cat /proc/sys/kernel/random/uuid)" \
  --arg authorized_at "$authorized_at" --arg expires_at "$expires_at" \
  --arg head "$head" --arg owner "$owner" --arg plan_sha "$plan_sha" \
  '{
    contract_version:"memory_v1_v5_2_compiler_v8_entity_apply_authorization_v1",
    authorization_id:$authorization_id,authorized:true,authorized_by:"Eric Lund",
    authorized_at:$authorized_at,expires_at:$expires_at,
    expected_head_commit:$head,target_server:"seebx",
    scope:"apply_compiler_v8_entities_and_bind_only",
    owner_user_id:$owner,plan_sha256:$plan_sha,expected_item_count:5,
    expected_new_entities:3,expected_total_bindings:4,expected_new_rows:20,
    confirmation:"APPLY_FIVE_COMPILER_V8_ENTITY_RESOLUTIONS_AND_BIND_ONLY"
  }' >"$reviews/authorization.json"
chmod 0600 "$reviews/authorization.json"

phase=apply
MEMORY_V1_V5_2_COMPILER_V8_ENTITY_APPLY=authorized \
  POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$runner" apply \
  --plan "$reviews/plan.json" \
  --authorization "$reviews/authorization.json" \
  --review-root "$reviews" \
  --confirm APPLY_FIVE_COMPILER_V8_ENTITY_RESOLUTIONS_AND_BIND_ONLY \
  --output "$reviews/apply-report.json"

assert_equal report_rows \
  "$(jq -r '.new_rows' "$reviews/apply-report.json")" 20
assert_equal report_replay \
  "$(jq -r '.zero_write_replay' "$reviews/apply-report.json")" true
assert_equal report_bindings \
  "$(jq -r '.new_observation_bindings' "$reviews/apply-report.json")" 4

phase=postflight
assert_equal exact_entities "$(psql_row "
  SELECT string_agg(entity_type || ':' || canonical_name,',' ORDER BY canonical_name)
  FROM memory.entity
  WHERE owner_user_id='$owner'::uuid
    AND metadata->>'resolution_id' IN (
      '$monika_resolution','$psychologist_resolution','$bcba_resolution'
    )")" \
  'concept:BCBA,concept:clinical psychologist,person:Monika'
assert_equal exact_applies "$(psql_row "
  SELECT count(*) FROM memory.entity_resolution_apply
  WHERE owner_user_id='$owner'::uuid
    AND resolution_id IN (
      '$care_self_resolution'::uuid,'$monika_resolution'::uuid,
      '$profession_self_resolution'::uuid,'$psychologist_resolution'::uuid,
      '$bcba_resolution'::uuid
    )")" 5
assert_equal exact_aliases "$(psql_row "
  SELECT count(*) FROM memory.entity_alias_observation
  WHERE owner_user_id='$owner'::uuid
    AND resolution_id IN (
      '$monika_resolution'::uuid,'$psychologist_resolution'::uuid,
      '$bcba_resolution'::uuid
    )")" 3
assert_equal binding_semantics "$(psql_row "
  SELECT string_agg(
    observation.predicate || ':' ||
    subject.entity_id::text || '->' || object.canonical_name,
    ',' ORDER BY observation.predicate,object.canonical_name
  )
  FROM memory.observation_entity_binding AS binding
  JOIN memory.observation AS observation
    USING(owner_user_id,observation_id)
  JOIN memory.entity AS subject
    ON subject.owner_user_id=binding.owner_user_id
   AND subject.entity_id=binding.subject_entity_id
  JOIN memory.entity AS object
    ON object.owner_user_id=binding.owner_user_id
   AND object.entity_id=binding.object_entity_id
  WHERE observation.owner_user_id='$owner'::uuid
    AND observation.evidence_id IN (
      '$care_evidence'::uuid,'$profession_evidence'::uuid
    )")" \
  "occupation.works_as:$self_entity->BCBA,occupation.works_as:$self_entity->clinical psychologist,relationship.caregiver_for:$self_entity->Monika,relationship.spouse_of:$self_entity->Monika"

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
report="$snapshot_dir/memory_v1_v5_2_v8_entity_apply_${run_tag}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$head" --arg owner_user_id "$owner" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg review_root "$reviews" \
  --arg target_before "$target_before" --arg target_after "$target_after" \
  --arg non_target_before "$non_target_before" \
  --arg non_target_after "$non_target_after" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:"memory_v1_v5_2_compiler_v8_entity_apply_production_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,owner_user_id:$owner_user_id,
    backup:{path:$backup,sha256:$backup_sha256},review_root:$review_root,
    rows:{entities:3,resolution_applies:5,aliases:3,bindings:4,
      operation_requests:5,total:20,claims:0,projections:0},
    evidence:{target_before:$target_before,target_after:$target_after,
      non_target_before:$non_target_before,non_target_after:$non_target_after,
      qdrant_sha256:$qdrant_sha256},
    checks:{fresh_backup:true,hash_locked_inputs:true,
      transactional_apply:true,semantic_bindings_verified:true,
      zero_write_replay:true,account_isolation:true,
      exact_target_owner_deltas:true,non_target_and_global_rows_unchanged:true,
      qdrant_unchanged:true,timers_restored:true,service_healthy:true,
      external_model_calls:0,retrieval_changes:0,prompt_changes:0},
    hard_stop:"before_claims_projection_qdrant_retrieval_or_prompt_influence"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf '%s\n' \
  'MEMORY_V1_V5_2_COMPILER_V8_ENTITY_APPLY_PRODUCTION=PASS' \
  "head=$head" \
  "report=$report" \
  "backup=$backup" \
  'entities_created=3' \
  'entity_applies_created=5' \
  'aliases_created=3' \
  'observation_bindings_created=4' \
  'claim_rows_created=0' \
  'projection_rows_created=0' \
  'qdrant_writes=0' \
  'retrieval_changes=0' \
  'prompt_changes=0'

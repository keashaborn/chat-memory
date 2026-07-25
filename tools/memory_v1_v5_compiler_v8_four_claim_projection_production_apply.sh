#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Admits and projects exactly four reviewed claims, then
# performs owner-only shadow retrieval and a zero-call, zero-write replay.
# The path never enables answer retrieval or prompt influence.

if [[ "${MEMORY_V1_COMPILER_V8_FOUR_CLAIM_PROJECTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_COMPILER_V8_FOUR_CLAIM_PROJECTION=authorized is required' >&2
  exit 1
fi

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
plan="$repo_root/evals/memory_v1_v5_compiler_v8_four_claim_projection_plan.json"
expected_plan_sha=0d0f382259a279d7a50fe06492f8971dcbd56ba4832957efbc7a7a901dd163f0
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
admission_runner=scripts/memory_v1_v5_deferred_projection_admission.py
project_runner=scripts/memory_v1_v5_claim_projection_controlled_project.py
python_bin="$repo_root/venv/bin/python"
review_root=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_compiler_v8_four_claim_projection.lock
phase=initialization
run_id=
artifact_dir=
status_file=
timers_quiesced=0
brains_quiesced=0
brains_state_before=
timer_state=$(mktemp /tmp/memory-v1-compiler-v8-project-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-compiler-v8-project-tables.XXXXXX)

psql_row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
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
      [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
      if [[ "$active" == active ]]; then
        sudo -n systemctl start "$unit"
      else
        sudo -n systemctl stop "$unit"
      fi
      [[ "$(systemctl is-active "$unit")" == "$active" ]]
    done <"$timer_state"
    timers_quiesced=0
  fi
}

record_exit() {
  code=$?
  if [[ "$code" -eq 0 && "$phase" != complete ]]; then code=1; fi
  restore_runtime || code=1
  rm -f "$timer_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    printf 'run_id=%s\nphase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$run_id" "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$code"
}
trap record_exit EXIT

capture_memory_non_outbox() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    state=$(psql_row "
      SELECT count(*)::text || E'\t' ||
             encode(public.digest(convert_to(
               coalesce(string_agg(row_json,E'\n' ORDER BY row_json),''),
               'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

capture_non_target_outbox() {
  local output=$1
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "
    SELECT count(*)::text || E'\t' ||
           encode(public.digest(convert_to(
             coalesce(string_agg(row_json,E'\n' ORDER BY row_json),''),
             'UTF8'),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(value)::text AS row_json
      FROM memory.projection_outbox AS value
      WHERE owner_user_id IS DISTINCT FROM '$owner'::uuid
         OR aggregate_id <> ALL(string_to_array('$claim_csv',',')::uuid[])
    ) AS rows
  " >"$output"
  chmod 0600 "$output"
}

capture_qdrant() {
  local target=$1 non_target=$2 response
  response=$(mktemp /tmp/memory-v1-compiler-v8-qdrant.XXXXXX)
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    >"$response"
  jq -cS --arg owner "$owner" \
    '.result.points|sort_by(.id|tostring)|.[]|
     select(.payload.owner_user_id==$owner)' "$response" >"$target"
  jq -cS --arg owner "$owner" \
    '.result.points|sort_by(.id|tostring)|.[]|
     select(.payload.owner_user_id!=$owner)' "$response" >"$non_target"
  chmod 0600 "$target" "$non_target"
  rm -f "$response"
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ "$(sha256sum "$plan" | awk '{print $1}')" == "$expected_plan_sha" ]]
git -C "$repo_root" merge-base --is-ancestor \
  "$(jq -er '.required_ancestor_commit' "$plan")" HEAD
head=$(git -C "$repo_root" rev-parse HEAD)
[[ "$(jq -er '.owner_user_id' "$plan")" == "$owner" ]]
[[ "$(jq -er '.items|length' "$plan")" == 4 ]]
[[ "$(jq -er '.projection.maximum_embedding_requests' "$plan")" == 4 ]]
[[ "$(jq -er '.projection.automatic_http_retries' "$plan")" == 0 ]]
[[ "$(jq -er '.projection.vector_size' "$plan")" == 3072 ]]
[[ "$(jq -er '.projection.embedding_model' "$plan")" == text-embedding-3-large ]]
apply_result=$(jq -er '.source.apply_result_path' "$plan")
apply_manifest=$(jq -er '.source.apply_manifest_path' "$plan")
[[ "$(sha256sum "$apply_result" | awk '{print $1}')" == \
  "$(jq -er '.source.apply_result_file_sha256' "$plan")" ]]
[[ "$(sha256sum "$apply_manifest" | awk '{print $1}')" == \
  "$(jq -er '.source.apply_manifest_file_sha256' "$plan")" ]]
[[ "$(jq -er '.result_sha256' "$apply_result")" == \
  "$(jq -er '.source.apply_result_sha256' "$plan")" ]]
[[ "$(jq -er '.manifest_sha256' "$apply_manifest")" == \
  "$(jq -er '.source.apply_manifest_sha256' "$plan")" ]]
required_head=$(jq -er '.required_head_commit' "$apply_manifest")
claim_csv=$(jq -r '[.items[].claim_id]|join(",")' "$plan")

[[ "$(psql_row "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$owner'::uuid
    AND claim_id=ANY(string_to_array('$claim_csv',',')::uuid[])
    AND status='supported'
")" == 4 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.projection_outbox
  WHERE owner_user_id='$owner'::uuid
    AND aggregate_id=ANY(string_to_array('$claim_csv',',')::uuid[])
")" == 0 ]]
[[ "$(curl --fail --silent --show-error --max-time 30 \
  -H 'content-type: application/json' \
  -d "{\"ids\":[$(jq -r '[.items[].claim_id|@json]|join(",")' "$plan")],
       \"with_payload\":true,\"with_vector\":false}" \
  http://127.0.0.1:6333/collections/memory_claim_v1/points \
  | jq -er '.result|length')" == 0 ]]

set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${QDRANT_URL:-}" && -n "${OPENAI_API_KEY:-}" ]]
[[ "${EMBED_MODEL:-text-embedding-3-large}" == text-embedding-3-large ]]
authenticated_health
exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_${head:0:12}"
artifact_dir="$review_root/compiler-v8-four-claim-projection-$run_id"
status_file="$snapshot_root/memory_v1_compiler_v8_four_claim_projection_${run_id}.status"
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

phase=quiesce
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
brains_state_before=$(systemctl is-active brains.service)
[[ "$brains_state_before" != active ]] || sudo -n systemctl stop brains.service
brains_quiesced=1
for _attempt in $(seq 1 30); do
  systemctl is-active --quiet brains.service || break
  sleep 1
done
! systemctl is-active --quiet brains.service

phase=backup
backup_partial="$snapshot_root/.memory_pre_compiler_v8_projection_${run_id}.dump.partial"
backup="$snapshot_root/memory_pre_compiler_v8_projection_${run_id}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -X -A -t -U sage -d "$database" -c "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name <> 'projection_outbox'
  ORDER BY table_name
" >"$table_list"
memory_before="$artifact_dir/non-outbox-memory-before.tsv"
memory_after="$artifact_dir/non-outbox-memory-after.tsv"
outbox_other_before="$artifact_dir/non-target-outbox-before.tsv"
outbox_other_after="$artifact_dir/non-target-outbox-after.tsv"
q_owner_before="$artifact_dir/qdrant-owner-before.jsonl"
q_owner_after="$artifact_dir/qdrant-owner-after.jsonl"
q_other_before="$artifact_dir/qdrant-other-before.jsonl"
q_other_after="$artifact_dir/qdrant-other-after.jsonl"
capture_memory_non_outbox "$memory_before"
capture_non_target_outbox "$outbox_other_before"
capture_qdrant "$q_owner_before" "$q_other_before"

preflight="$artifact_dir/admission-preflight.json"
admission="$artifact_dir/admission-apply.json"
admission_replay="$artifact_dir/admission-replay.json"
project="$artifact_dir/project-apply.json"
project_replay="$artifact_dir/project-replay.json"
env_common=(
  "POSTGRES_DSN=$POSTGRES_DSN"
  "PYTHONPATH=$repo_root/scripts:$repo_root"
  "MEMORY_V1_REQUIRED_HEAD=$required_head"
)

phase=admission_preflight
env "${env_common[@]}" "$python_bin" "$repo_root/$admission_runner" \
  --mode preflight --apply-result "$apply_result" \
  --apply-manifest "$apply_manifest" --output "$preflight"
[[ "$(jq -er '.rows_written' "$preflight")" == 0 ]]

phase=admission_apply
env "${env_common[@]}" MEMORY_V1_DEFERRED_PROJECTION_ADMISSION=authorized \
  "$python_bin" "$repo_root/$admission_runner" \
  --mode apply --apply-result "$apply_result" \
  --apply-manifest "$apply_manifest" --output "$admission"
[[ "$(jq -er '.rows_written' "$admission")" == 4 ]]

phase=admission_replay
env "${env_common[@]}" "$python_bin" "$repo_root/$admission_runner" \
  --mode replay --apply-result "$apply_result" \
  --apply-manifest "$apply_manifest" --prior-result "$admission" \
  --output "$admission_replay"
[[ "$(jq -er '.rows_written' "$admission_replay")" == 0 ]]

phase=controlled_projection
MEMORY_V1_CONTROLLED_PROJECTION=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$project_runner" \
  --mode apply --apply-result "$apply_result" \
  --admission-result "$admission" --output "$project"
[[ "$(jq -er '.mode' "$project")" == apply ]]
[[ "$(jq -er '.embedding_requests' "$project")" == 4 ]]
[[ "$(jq -er '.automatic_http_retries' "$project")" == 0 ]]
[[ "$(jq -er '.qdrant_writes' "$project")" == 4 ]]
[[ "$(jq -er '.shadow_tests|length' "$project")" == 4 ]]
[[ "$(jq -er '[.shadow_tests[]|select(
  .other_owner_target_present==false and
  .other_owner_database_record_count==0 and .selected_count>=1 and
  .prompt_influence==false)]|length' "$project")" == 4 ]]

phase=zero_write_projection_replay
unset OPENAI_API_KEY
MEMORY_V1_CONTROLLED_PROJECTION_REPLAY=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$project_runner" \
  --mode replay --apply-result "$apply_result" \
  --admission-result "$admission" --output "$project_replay"
[[ "$(jq -er '.mode' "$project_replay")" == replay ]]
[[ "$(jq -er '.embedding_requests' "$project_replay")" == 0 ]]
[[ "$(jq -er '.qdrant_writes' "$project_replay")" == 0 ]]
[[ "$(jq -er '[.completed[]|select(.replay_verified==true)]|length' \
  "$project_replay")" == 4 ]]
[[ "$(jq -er '.shadow_tests|length' "$project_replay")" == 4 ]]
[[ "$(jq -er '[.shadow_tests[]|select(
  .other_owner_target_present==false and
  .other_owner_database_record_count==0 and .selected_count>=1 and
  .prompt_influence==false)]|length' "$project_replay")" == 4 ]]

phase=verify
[[ "$(psql_row "
  SELECT count(*) FROM memory.projection_outbox
  WHERE owner_user_id='$owner'::uuid
    AND aggregate_id=ANY(string_to_array('$claim_csv',',')::uuid[])
    AND status='done' AND attempts=1
")" == 4 ]]
capture_memory_non_outbox "$memory_after"
capture_non_target_outbox "$outbox_other_after"
cmp -s "$memory_before" "$memory_after"
cmp -s "$outbox_other_before" "$outbox_other_after"
capture_qdrant "$q_owner_after" "$q_other_after"
cmp -s "$q_other_before" "$q_other_after"

PLAN="$plan" BEFORE="$q_owner_before" AFTER="$q_owner_after" \
PROJECT="$project" OWNER="$owner" "$python_bin" - <<'PY'
import json
import os
from pathlib import Path


def load(path: str) -> dict[str, dict[str, object]]:
    result = {}
    for line in Path(path).read_text().splitlines():
        value = json.loads(line)
        result[str(value["id"])] = value
    return result


plan = json.loads(Path(os.environ["PLAN"]).read_text())
before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
project = json.loads(Path(os.environ["PROJECT"]).read_text())
ids = {item["claim_id"] for item in plan["items"]}
if {item["claim_id"] for item in project["completed"]} != ids:
    raise SystemExit("project result claim set differs")
if ids & before.keys() or after.keys() != before.keys() | ids:
    raise SystemExit("unexpected owner Qdrant point delta")
for point_id, value in before.items():
    if after[point_id] != value:
        raise SystemExit("existing owner Qdrant point changed")
for item in plan["items"]:
    point = after[item["claim_id"]]
    payload = point.get("payload") or {}
    vector = point.get("vector")
    if (
        payload.get("owner_user_id") != os.environ["OWNER"]
        or payload.get("claim_id") != item["claim_id"]
        or payload.get("predicate") != item["predicate"]
        or payload.get("status") != "supported"
        or payload.get("revision_number") != 2
        or payload.get("schema_version") != "memory_claim_projection_v1"
        or not isinstance(vector, list)
        or len(vector) != 3072
    ):
        raise SystemExit("Qdrant projection contract mismatch")
PY

phase=restore_runtime
restore_runtime
authenticated_health

phase=report
report="$artifact_dir/report.json"
jq -n \
  --arg head "$head" --arg backup "$backup" \
  --arg plan_sha256 "$expected_plan_sha" \
  --arg admission_result_sha256 "$(jq -er '.result_sha256' "$admission")" \
  --arg project_result_sha256 "$(jq -er '.result_sha256' "$project")" \
  --arg replay_result_sha256 "$(jq -er '.result_sha256' "$project_replay")" \
  '{
    contract_version:"memory_v1_v5_compiler_v8_four_claim_projection_report_v1",
    head_commit:$head,
    owner_user_id:"1240822d-ac9a-4096-95aa-e2b24d36ef50",
    plan_sha256:$plan_sha256,
    backup:$backup,
    admission_result_sha256:$admission_result_sha256,
    project_result_sha256:$project_result_sha256,
    replay_result_sha256:$replay_result_sha256,
    verification:{
      claims_projected:4,
      admission_rows_written:4,
      admission_replay_rows_written:0,
      embedding_requests:4,
      automatic_http_retries:0,
      qdrant_points_created:4,
      owner_shadow_tests_passed:4,
      cross_owner_candidates:0,
      replay_embedding_requests:0,
      replay_qdrant_writes:0,
      replay_shadow_tests_passed:4,
      non_target_postgres_unchanged:true,
      non_target_qdrant_unchanged:true,
      existing_owner_qdrant_unchanged:true,
      account_isolation:true,
      timers_restored_exactly:true,
      brains_service_restored:true,
      retrieval_activated:false,
      prompt_influence_activated:false,
      general_account_activation:false
    },
    hard_stop:"before_prompt_influence_or_general_account_activation"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'backup=%s\nreport=%s\nproject=%s\nreplay=%s\n' \
  "$backup" "$report" "$project" "$project_replay"
printf 'report_sha256=%s\n' "$(awk '{print $1}' "$report.sha256")"
printf 'memory_v1_v5_compiler_v8_four_claim_projection_production_apply: PASS\n'

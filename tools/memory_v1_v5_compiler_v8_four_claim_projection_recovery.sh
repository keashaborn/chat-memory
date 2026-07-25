#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Recovers the already-completed four-claim projection:
# installs the corrected read contract, rewrites only the four Qdrant payloads
# while preserving their vectors, and runs zero-call owner-only shadow replay.
# It never enables prompt influence.

if [[ "${MEMORY_V1_COMPILER_V8_PROJECTION_RECOVERY:-}" != authorized ]]; then
  echo 'MEMORY_V1_COMPILER_V8_PROJECTION_RECOVERY=authorized is required' >&2
  exit 1
fi
[[ "$EUID" -eq 0 ]]

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
plan="$repo_root/evals/memory_v1_v5_compiler_v8_four_claim_projection_plan.json"
expected_plan_sha=0d0f382259a279d7a50fe06492f8971dcbd56ba4832957efbc7a7a901dd163f0
migration="$repo_root/ops/sql/20260725_memory_v1_v5_shadow_claim_contract.sql"
rollback="$repo_root/ops/sql/20260725_memory_v1_v5_shadow_claim_contract_rollback.sql"
security_test="$repo_root/tests/memory_v1_v5_shadow_claim_contract.sql"
expected_migration_sha=17957ba743ce3f05709c18af19c2c9d602d4ec77dfcd350126fac176ce9bc50f
expected_rollback_sha=ba867c1d9c6c12b8a5ec2fc6339100dee2cda1d382bc514b3475b8192ded7f8b
failed_admission=/home/ubuntu/memory-v1-reviews/compiler-v8-four-claim-projection-20260725T160857Z_f2d878a8071d/admission-apply.json
expected_admission_file_sha=9f531395820d905a2575c4931b612a1312a7e2c2b2613165bcc0dcf801bd5084
expected_admission_result_sha=877615f221a6b02810925238234db63693519582a318be9f9a34ae07513a219a
payload_repair="$repo_root/scripts/memory_v1_v5_claim_projection_payload_repair.py"
projector="$repo_root/scripts/memory_v1_v5_claim_projection_controlled_project.py"
python_bin="$repo_root/venv/bin/python"
review_root=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_compiler_v8_projection_recovery.lock
timer_state=$(mktemp /tmp/memory-v1-compiler-v8-recovery-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-compiler-v8-recovery-tables.XXXXXX)
phase=initialization
run_id=
artifact_dir=
status_file=
timers_quiesced=0
brains_quiesced=0
brains_state_before=
migration_installed=0
migration_verified=0

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
          http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null \
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
  if [[ "$code" -ne 0 && "$migration_installed" -eq 1 \
        && "$migration_verified" -eq 0 ]]; then
    docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
      -U sage -d "$database" <"$rollback" >/dev/null || code=1
  fi
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

capture_memory_rows() {
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

capture_qdrant() {
  local target=$1 non_target=$2 response
  response=$(mktemp /tmp/memory-v1-compiler-v8-recovery-qdrant.XXXXXX)
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    >"$response"
  jq -cS --argjson ids "$claim_ids_json" \
    '.result.points|sort_by(.id|tostring)|.[]|
     select((.id|tostring) as $id|$ids|index($id))' "$response" >"$target"
  jq -cS --argjson ids "$claim_ids_json" \
    '.result.points|sort_by(.id|tostring)|.[]|
     select((.id|tostring) as $id|($ids|index($id)|not))' "$response" \
    >"$non_target"
  chmod 0600 "$target" "$non_target"
  rm -f "$response"
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ "$(sha256sum "$plan" | awk '{print $1}')" == "$expected_plan_sha" ]]
[[ "$(sha256sum "$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sudo -n sha256sum "$failed_admission" | awk '{print $1}')" == \
  "$expected_admission_file_sha" ]]
[[ "$(sudo -n jq -er '.result_sha256' "$failed_admission")" == \
  "$expected_admission_result_sha" ]]
[[ "$(jq -er '.owner_user_id' "$plan")" == "$owner" ]]
[[ "$(jq -er '.items|length' "$plan")" == 4 ]]
claim_csv=$(jq -r '[.items[].claim_id]|join(",")' "$plan")
claim_ids_json=$(jq -c '[.items[].claim_id]' "$plan")
apply_result=$(jq -er '.source.apply_result_path' "$plan")

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
    AND status='done' AND attempts=1
")" == 4 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$other_owner'::uuid
    AND claim_id=ANY(string_to_array('$claim_csv',',')::uuid[])
")" == 0 ]]

exec 9>"$lock_file"
flock -n 9
umask 077
head=$(git -C "$repo_root" rev-parse HEAD)
run_id="$(date -u +%Y%m%dT%H%M%SZ)_${head:0:12}"
artifact_dir="$review_root/compiler-v8-four-claim-projection-recovery-$run_id"
status_file="$snapshot_root/memory_v1_compiler_v8_projection_recovery_${run_id}.status"
mkdir -p "$artifact_dir"
chmod 0700 "$artifact_dir"

phase=capture_timer_state
: >"$timer_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
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
backup_partial="$snapshot_root/.memory_pre_compiler_v8_projection_recovery_${run_id}.dump.partial"
backup="$snapshot_root/memory_pre_compiler_v8_projection_recovery_${run_id}.dump"
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
  ORDER BY table_name
" >"$table_list"
memory_before="$artifact_dir/memory-before.tsv"
memory_after="$artifact_dir/memory-after.tsv"
q_target_before="$artifact_dir/qdrant-target-before.jsonl"
q_target_after="$artifact_dir/qdrant-target-after.jsonl"
q_other_before="$artifact_dir/qdrant-other-before.jsonl"
q_other_after="$artifact_dir/qdrant-other-after.jsonl"
capture_memory_rows "$memory_before"
capture_qdrant "$q_target_before" "$q_other_before"
[[ "$(wc -l <"$q_target_before" | tr -d ' ')" == 4 ]]

phase=install_contract
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$migration" >"$artifact_dir/migration.log"
migration_installed=1
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$security_test" >"$artifact_dir/security.log"
grep -F 'memory_v1_v5_shadow_claim_contract: PASS' \
  "$artifact_dir/security.log" >/dev/null
migration_verified=1

phase=repair_projection_payloads
set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${QDRANT_URL:-}" ]]
unset OPENAI_API_KEY OPENAI_BASE_URL
plan_private="$artifact_dir/plan.json"
cp "$plan" "$plan_private"
chmod 0600 "$plan_private"
repair_result="$artifact_dir/payload-repair.json"
MEMORY_V1_PROJECTION_PAYLOAD_REPAIR=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$payload_repair" \
  --plan "$plan_private" --admission-result "$failed_admission" \
  --output "$repair_result"
[[ "$(jq -er '.embedding_requests' "$repair_result")" == 0 ]]
[[ "$(jq -er '.qdrant_writes' "$repair_result")" == 4 ]]
[[ "$(jq -er '.database_writes' "$repair_result")" == 0 ]]

phase=owner_shadow_replay
replay_result="$artifact_dir/project-replay.json"
MEMORY_V1_CONTROLLED_PROJECTION_REPLAY=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$projector" \
  --mode replay --apply-result "$apply_result" \
  --admission-result "$failed_admission" --output "$replay_result"
[[ "$(jq -er '.embedding_requests' "$replay_result")" == 0 ]]
[[ "$(jq -er '.qdrant_writes' "$replay_result")" == 0 ]]
[[ "$(jq -er '.shadow_tests|length' "$replay_result")" == 4 ]]
[[ "$(jq -er '[.shadow_tests[]|select(
  .other_owner_target_present==false and
  .other_owner_database_record_count==0 and .selected_count>=1 and
  .prompt_influence==false)]|length' "$replay_result")" == 4 ]]

phase=verify
capture_memory_rows "$memory_after"
cmp -s "$memory_before" "$memory_after"
capture_qdrant "$q_target_after" "$q_other_after"
cmp -s "$q_other_before" "$q_other_after"

PLAN="$plan" BEFORE="$q_target_before" AFTER="$q_target_after" \
OWNER="$owner" "$python_bin" - <<'PY'
import json
import os
from pathlib import Path


def load(path: str) -> dict[str, dict[str, object]]:
    return {
        str(value["id"]): value
        for value in (
            json.loads(line)
            for line in Path(path).read_text().splitlines()
            if line
        )
    }


expected_surface = {
    "2c3ab91d-c423-43d9-8d55-19e4f1069028": (
        "direct_or_relevant",
        False,
    ),
    "3073b518-1f12-4fa3-93d6-eaf0b22f864c": (
        "direct_or_relevant",
        False,
    ),
    "b93c565d-6511-4648-b5e4-5c441e3373f8": (
        "direct_or_relevant",
        False,
    ),
    "f4688838-7193-4e0e-961f-c9bbbf9904c7": (
        "explicit_recall_only",
        True,
    ),
}
plan = json.loads(Path(os.environ["PLAN"]).read_text())
ids = {item["claim_id"] for item in plan["items"]}
before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
if before.keys() != ids or after.keys() != ids:
    raise SystemExit("target Qdrant ID set differs")
for claim_id in sorted(ids):
    if before[claim_id].get("vector") != after[claim_id].get("vector"):
        raise SystemExit("existing embedding vector changed")
    old_payload = dict(before[claim_id].get("payload") or {})
    payload = dict(after[claim_id].get("payload") or {})
    surface, requires_explicit = expected_surface[claim_id]
    if (
        payload.get("owner_user_id") != os.environ["OWNER"]
        or payload.get("claim_id") != claim_id
        or payload.get("surface") != surface
        or payload.get("requires_explicit") is not requires_explicit
        or payload.get("schema_version") != "memory_claim_projection_v1"
    ):
        raise SystemExit("corrected target projection payload differs")
    for key in ("surface", "requires_explicit"):
        old_payload.pop(key, None)
        payload.pop(key, None)
    if payload != old_payload:
        raise SystemExit("non-policy target payload changed")
PY

[[ "$(psql_row "
  SELECT count(*) FROM memory.projection_outbox
  WHERE owner_user_id='$owner'::uuid
    AND aggregate_id=ANY(string_to_array('$claim_csv',',')::uuid[])
    AND status='done' AND attempts=1
")" == 4 ]]

phase=restore_runtime
restore_runtime
authenticated_health

phase=report
report="$artifact_dir/report.json"
jq -n \
  --arg head "$head" \
  --arg backup "$backup" \
  --arg migration_sha256 "$expected_migration_sha" \
  --arg payload_repair_sha256 "$(jq -er '.result_sha256' "$repair_result")" \
  --arg replay_sha256 "$(jq -er '.result_sha256' "$replay_result")" \
  '{
    contract_version:"memory_v1_v5_compiler_v8_projection_recovery_report_v1",
    head_commit:$head,
    owner_user_id:"1240822d-ac9a-4096-95aa-e2b24d36ef50",
    backup:$backup,
    migration_sha256:$migration_sha256,
    original_embedding_requests:4,
    recovery_embedding_requests:0,
    automatic_http_retries:0,
    preserved_vector_count:4,
    corrected_payload_count:4,
    owner_shadow_tests:4,
    owner_shadow_selected_count:4,
    cross_owner_target_count:0,
    database_rows_changed:0,
    non_target_qdrant_unchanged:true,
    payload_repair_result_sha256:$payload_repair_sha256,
    replay_result_sha256:$replay_sha256,
    retrieval_activated:false,
    prompt_influence_activated:false,
    service_health:"passed",
    timer_state:"exactly_restored"
  }' >"$report"
chmod 0600 "$artifact_dir"/*
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'report=%s\n' "$report"
printf 'report_sha256=%s\n' "$(awk '{print $1}' "$report.sha256")"
printf 'memory_v1_v5_compiler_v8_four_claim_projection_recovery: PASS\n'

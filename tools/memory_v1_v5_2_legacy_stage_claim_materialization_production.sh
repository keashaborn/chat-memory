#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Stages, reviews, and materializes exactly 19 reviewed
# owner-scoped V5.2 claims. It makes no model or Qdrant calls and restores the
# exact Memory V1 timer, worker, and Brains states.

if [[ "${MEMORY_V1_V5_2_LEGACY_CLAIM_PRODUCTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_LEGACY_CLAIM_PRODUCTION=authorized is required' >&2
  exit 1
fi
if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=440c4014112a1046fc74f3c5ea04f8cf539edae8
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
expected_target_sha256=a8400e1d233c9ecde4a22420e6705d31166a14e4013cffbc487e13bbaa951885
review_root=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
container=brains-postgres-1
database=memory
python_bin=/opt/chat-memory/venv/bin/python
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_legacy_claim_materialization.lock
clone_artifact_dir="$review_root/legacy-stage-claim-materialization-clone-20260730T170010Z_440c4014112a_1651254"
clone_report="$clone_artifact_dir/clone-report.json"
review_report="$clone_artifact_dir/claim-target-review.json"
clone_report_file_sha256=69809fcc98a5047391f90087318d4ae24a86f02494498e033d81ff29c6656871
review_report_file_sha256=3568ce9f8c5846c6541c6b5511c024de6f78b15f5c724c0e79643f52afb5514d

phase=initialization
run_tag=
status_file=
units_quiesced=0
workers_quiesced=0
brains_quiesced=0
brains_state_before=
unit_state=$(mktemp /tmp/memory-legacy-claim-units.XXXXXX)
worker_state=$(mktemp /tmp/memory-legacy-claim-workers.XXXXXX)
table_list=$(mktemp /tmp/memory-legacy-claim-tables.XXXXXX)

restore_timers() {
  [[ "$units_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
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

restore_workers() {
  [[ "$workers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r service prior_state; do
    [[ "$service" =~ ^memory-v1-[a-z0-9-]+\.service$ ]]
    if [[ "$prior_state" == active || "$prior_state" == activating ]]; then
      systemctl start --no-block "$service"
    fi
  done <"$worker_state"
  workers_quiesced=0
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
  restore_timers
  restore_workers
}

record_exit() {
  code=$?
  if [[ "$brains_quiesced" -eq 1 || "$units_quiesced" -eq 1 \
    || "$workers_quiesced" -eq 1 ]]; then
    restore_runtime || code=1
  fi
  rm -f "$unit_state" "$worker_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    printf 'run_tag=%s\nphase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$run_tag" "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$code"
}
trap record_exit EXIT

authenticated_health() {
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

psql_row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
}

capture_partition() {
  local partition=$1
  local output=$2
  local table has_owner predicate state
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
    state=$(
      psql_row \
        "SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex') FROM (SELECT to_jsonb(value)::text AS row_json FROM memory.\"$table\" AS value WHERE $predicate) AS rows"
    )
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | cut -d' ' -f1
}

verify_target_delta() {
  BEFORE="$1" AFTER="$2" EXPECTED="$3" python3 - <<'PY'
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
    raise SystemExit("target table set changed")
for table in before:
    wanted = expected.get(table, 0)
    delta = after[table][0] - before[table][0]
    if delta != wanted:
        raise SystemExit(f"unexpected target delta {table}: {delta} != {wanted}")
    if wanted == 0 and before[table][1] != after[table][1]:
        raise SystemExit(f"unexpected target mutation {table}")
PY
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" "$head"
[[ -f "$clone_report" && "$(stat -c '%a' "$clone_report")" == 600 ]]
[[ -f "$review_report" && "$(stat -c '%a' "$review_report")" == 600 ]]
[[ "$(sha256sum "$clone_report" | cut -d' ' -f1)" == \
  "$clone_report_file_sha256" ]]
[[ "$(sha256sum "$review_report" | cut -d' ' -f1)" == \
  "$review_report_file_sha256" ]]
[[ "$(jq -er '.report_sha256' "$clone_report")" == \
  7bc421c19c06498d15b334fbe5c328e4609a06e647666b2ca73d5c23250894ba ]]
[[ "$(jq -er '.target_set_sha256' "$clone_report")" == \
  "$expected_target_sha256" ]]
[[ "$(jq -er '.counts.create_targets' "$clone_report")" == 19 ]]
[[ "$(jq -er '.proofs.production_postgres_unchanged' "$clone_report")" == true ]]
[[ "$(jq -er '.proofs.qdrant_unchanged' "$clone_report")" == true ]]
[[ "$(jq -er '.item_count' "$review_report")" == 19 ]]
[[ "$(jq -er '.action_counts.create' "$review_report")" == 19 ]]
[[ "$(jq -er '.proofs.disposable_clone_verified' "$review_report")" == true ]]

if [[ -r "$repo_root/.env" ]]; then
  set -a
  source "$repo_root/.env"
  set +a
fi
[[ -n "${POSTGRES_DSN:-}" && -n "${VS_SERVICE_TOKEN:-}" ]]
[[ "$(stat -c '%a' "$review_root")" == 700 ]]
[[ -d "$snapshot_root" ]]

exec 9>"$lock_file"
flock -n 9 || {
  echo 'another legacy claim materialization holds the lock' >&2
  exit 1
}

run_tag="$(date -u +%Y%m%dT%H%M%SZ)_${head:0:12}"
artifact_dir="$review_root/legacy-stage-claim-materialization-production-$run_tag"
install -d -m 0700 "$artifact_dir"
status_file="$snapshot_root/memory_v1_v5_2_legacy_claim_${run_tag}.status"
stage_manifest="$artifact_dir/stage-manifest.json"
stage_authorization="$artifact_dir/stage-authorization.json"
stage_cross_owner="$artifact_dir/stage-cross-owner.json"
stage_apply="$artifact_dir/stage-apply.json"
stage_replay="$artifact_dir/stage-replay.json"
review_manifest="$artifact_dir/review-manifest.json"
review_authorization="$artifact_dir/review-authorization.json"
review_cross_owner="$artifact_dir/review-cross-owner.json"
review_apply="$artifact_dir/review-apply.json"
review_replay="$artifact_dir/review-replay.json"
claim_manifest="$artifact_dir/claim-apply-manifest.json"
claim_preflight="$artifact_dir/claim-preflight.json"
claim_apply="$artifact_dir/claim-apply.json"
claim_replay="$artifact_dir/claim-replay.json"
expected="$artifact_dir/expected-deltas.tsv"
target_before="$artifact_dir/target-before.tsv"
target_after="$artifact_dir/target-after.tsv"
target_replay="$artifact_dir/target-replay.tsv"
other_before="$artifact_dir/other-before.tsv"
other_after="$artifact_dir/other-after.tsv"
other_replay="$artifact_dir/other-replay.tsv"
report="$artifact_dir/production-report.json"
backup_partial="$snapshot_root/.memory_pre_legacy_claim_${run_tag}.dump.partial"
backup="${backup_partial%.partial}"

phase=capture_timer_state
: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" >>"$unit_state"
done < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
[[ -s "$unit_state" ]]
cp "$unit_state" "$artifact_dir/timer-state-before.tsv"
chmod 0600 "$unit_state" "$artifact_dir/timer-state-before.tsv"

phase=quiesce_timers
while IFS=$'\t' read -r unit _enabled active; do
  [[ "$active" != active ]] || systemctl stop "$unit"
done <"$unit_state"
units_quiesced=1
: >"$worker_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  state=$(systemctl show --property=ActiveState --value "$service")
  printf '%s\t%s\n' "$service" "$state" >>"$worker_state"
  if [[ "$state" != inactive && "$state" != failed ]]; then
    systemctl stop "$service"
  fi
  for _attempt in $(seq 1 30); do
    state=$(systemctl show --property=ActiveState --value "$service")
    [[ "$state" == inactive || "$state" == failed ]] && break
    sleep 1
  done
  [[ "$state" == inactive || "$state" == failed ]]
done <"$unit_state"
chmod 0600 "$worker_state"
workers_quiesced=1

phase=quiesce_brains
brains_state_before=$(systemctl is-active brains.service)
[[ "$brains_state_before" != active ]] || systemctl stop brains.service
brains_quiesced=1
! systemctl is-active --quiet brains.service

phase=capture_baseline
docker exec "$container" psql -X -A -t -F $'\t' -U sage -d "$database" \
  -v ON_ERROR_STOP=1 -c \
  "SELECT table_name,EXISTS(SELECT 1 FROM information_schema.columns AS column_info WHERE column_info.table_schema='memory' AND column_info.table_name=tables.table_name AND column_info.column_name='owner_user_id') FROM information_schema.tables AS tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" \
  >"$table_list"
[[ -s "$table_list" ]]
capture_partition target "$target_before"
capture_partition other "$other_before"
qdrant_before=$(qdrant_signature)

phase=backup
docker exec "$container" pg_dump -U sage -d "$database" -Fc \
  --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
mv "$backup_partial" "$backup"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup" "$backup.sha256"

runtime=(
  env
  POSTGRES_DSN="$POSTGRES_DSN"
  PYTHONPATH="$repo_root:$repo_root/scripts"
)

phase=stage_manifest
"${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" manifest \
  --owner "$owner" --review-report "$review_report" \
  --required-head "$head" --output "$stage_manifest"
[[ "$(jq -er '.stage_item_count' "$stage_manifest")" == 19 ]]
[[ "$(jq -er '.held_item_count' "$stage_manifest")" == 0 ]]
[[ "$(jq -er '.expected_rows' "$stage_manifest")" == 76 ]]
PYTHONPATH="$repo_root:$repo_root/scripts" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" authorize \
  --manifest "$stage_manifest" --output "$stage_authorization"
"${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" cross-owner \
  --manifest "$stage_manifest" --other-owner "$other_owner" \
  --output "$stage_cross_owner"
[[ "$(jq -er '.cross_owner_rejected' "$stage_cross_owner")" == true ]]

phase=stage_apply
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CLAIM_TARGET_STAGE_APPLY=authorized \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" apply \
  --manifest "$stage_manifest" --authorization "$stage_authorization" \
  --confirm STAGE_REVIEWED_V5_2_CLAIM_TARGETS_ONLY --output "$stage_apply"
[[ "$(jq -er '.rows_written' "$stage_apply")" == 76 ]]
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CLAIM_TARGET_STAGE_APPLY=authorized \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" replay \
  --manifest "$stage_manifest" --authorization "$stage_authorization" \
  --confirm STAGE_REVIEWED_V5_2_CLAIM_TARGETS_ONLY --output "$stage_replay"
[[ "$(jq -er '.rows_written' "$stage_replay")" == 0 ]]

phase=review_manifest
"${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_review_batch.py" manifest \
  --stage-manifest "$stage_manifest" --required-head "$head" \
  --output "$review_manifest"
[[ "$(jq -er '.expected_new_rows' "$review_manifest")" == 19 ]]
PYTHONPATH="$repo_root:$repo_root/scripts" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_review_batch.py" authorize \
  --manifest "$review_manifest" --output "$review_authorization"
"${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_review_batch.py" cross-owner \
  --manifest "$review_manifest" --other-owner "$other_owner" \
  --output "$review_cross_owner"
[[ "$(jq -er '.cross_owner_rejected' "$review_cross_owner")" == true ]]

phase=review_apply
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CLAIM_TARGET_REVIEW_APPLY=authorized \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_review_batch.py" apply \
  --manifest "$review_manifest" --authorization "$review_authorization" \
  --confirm REVIEW_EXACT_STAGED_V5_2_CLAIM_TARGETS_ONLY \
  --output "$review_apply"
[[ "$(jq -er '.rows_written' "$review_apply")" == 19 ]]
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CLAIM_TARGET_REVIEW_APPLY=authorized \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_review_batch.py" replay \
  --manifest "$review_manifest" --authorization "$review_authorization" \
  --confirm REVIEW_EXACT_STAGED_V5_2_CLAIM_TARGETS_ONLY \
  --output "$review_replay"
[[ "$(jq -er '.rows_written' "$review_replay")" == 0 ]]

phase=claim_manifest
"${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_reviewed_claim_apply_manifest.py" \
  --owner "$owner" --required-head "$head" \
  --review-manifest "$review_manifest" --review-result "$review_apply" \
  --output "$claim_manifest"
[[ "$(jq -er '.items|length' "$claim_manifest")" == 19 ]]
[[ "$(jq -er '.action_counts.create' "$claim_manifest")" == 19 ]]
[[ "$(jq -er '.expected_insert_rows' "$claim_manifest")" == 209 ]]
[[ "$(jq -er '.expected_mutated_rows' "$claim_manifest")" == 228 ]]
MEMORY_V1_REQUIRED_HEAD="$head" \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_reviewed_claim_apply_batch.py" \
  --mode preflight --manifest "$claim_manifest" --output "$claim_preflight"
[[ "$(jq -er '.insert_rows' "$claim_preflight")" == 0 ]]

phase=claim_apply
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_REVIEWED_CLAIM_APPLY=authorized \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_reviewed_claim_apply_batch.py" \
  --mode apply --manifest "$claim_manifest" --output "$claim_apply"
[[ "$(jq -er '.insert_rows' "$claim_apply")" == 209 ]]
[[ "$(jq -er '.mutated_rows' "$claim_apply")" == 228 ]]
[[ "$(jq -er '.qdrant_writes' "$claim_apply")" == 0 ]]
[[ "$(jq -er '.projection_outbox_rows_written' "$claim_apply")" == 0 ]]
MEMORY_V1_REQUIRED_HEAD="$head" \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_reviewed_claim_apply_batch.py" \
  --mode replay --manifest "$claim_manifest" --apply-result "$claim_apply" \
  --output "$claim_replay"
[[ "$(jq -er '.insert_rows' "$claim_replay")" == 0 ]]
[[ "$(jq -er '.mutated_rows' "$claim_replay")" == 0 ]]

phase=verify
printf '%s\n' \
  $'claim\t19' \
  $'claim_revision\t38' \
  $'claim_observation\t19' \
  $'claim_assessment_review_v5\t19' \
  $'claim_assessment\t19' \
  $'claim_assessment_apply_v5\t19' \
  $'projection_plan\t19' \
  $'projection_plan_item\t19' \
  $'projection_claim_payload\t19' \
  $'projection_plan_observation\t19' \
  $'projection_review\t19' \
  $'projection_apply_event\t19' \
  $'projection_dispatch_v5\t19' \
  $'relational_operation_request\t38' \
  $'projection_outbox\t0' >"$expected"
chmod 0600 "$expected"
capture_partition target "$target_after"
capture_partition other "$other_after"
verify_target_delta "$target_before" "$target_after" "$expected"
cmp -s "$other_before" "$other_after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

claim_ids=$(jq -r '[.outcomes[].claim_id]|join(",")' "$claim_apply")
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$owner'::uuid
    AND status='supported'
    AND claim_id=ANY(string_to_array('$claim_ids',',')::uuid[])")" == 19 ]]

probe_plan=$(jq -er '.items[0].plan_id' "$claim_manifest")
probe_review=$(jq -er '.items[0].review_id' "$claim_manifest")
psql "$POSTGRES_DSN" -X -q -v ON_ERROR_STOP=1 \
  -v other_owner="$other_owner" -v probe_plan="$probe_plan" \
  -v probe_review="$probe_review" <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id', :'other_owner', true);
SELECT set_config('test.probe_plan', :'probe_plan', true);
SELECT set_config('test.probe_review', :'probe_review', true);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_projection_apply_v5(
      current_setting('test.probe_plan')::uuid,
      'p01',
      current_setting('test.probe_review')::uuid
    );
    RAISE EXCEPTION 'cross-owner projection preflight unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN NULL;
  END;
END
$isolation$;
ROLLBACK;
SQL

capture_partition target "$target_replay"
capture_partition other "$other_replay"
cmp -s "$target_after" "$target_replay"
cmp -s "$other_after" "$other_replay"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=restore_runtime
restore_runtime
authenticated_health

phase=write_report
REPORT="$report" HEAD_VALUE="$head" BACKUP="$backup" \
STAGE="$stage_manifest" REVIEW="$review_manifest" CLAIM="$claim_manifest" \
APPLY="$claim_apply" TARGET_SHA="$expected_target_sha256" python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

def load(name):
    return json.loads(Path(os.environ[name]).read_text())

stage = load("STAGE")
review = load("REVIEW")
claim = load("CLAIM")
apply = load("APPLY")
report = {
    "contract_version":
        "memory_v1_v5_2_legacy_stage_claim_materialization_report_v1",
    "head_commit": os.environ["HEAD_VALUE"],
    "owner_user_id": stage["owner_user_id"],
    "target_set_sha256": os.environ["TARGET_SHA"],
    "stage_manifest_sha256": stage["manifest_sha256"],
    "review_manifest_sha256": review["manifest_sha256"],
    "claim_manifest_sha256": claim["manifest_sha256"],
    "backup_path": os.environ["BACKUP"],
    "counts": {
        "source_targets": 20,
        "surface_policy_holds": 1,
        "claims_created": 19,
        "stage_rows": 76,
        "review_rows": 19,
        "claim_insert_rows": apply["insert_rows"],
        "claim_mutated_rows": apply["mutated_rows"],
    },
    "proofs": {
        "fresh_backup": True,
        "zero_write_replay": True,
        "cross_owner_rejected": True,
        "non_target_records_unchanged": True,
        "qdrant_unchanged": True,
        "model_calls": 0,
        "projection_outbox_rows": 0,
        "retrieval_changes": 0,
        "prompt_influence": 0,
        "timers_restored_exactly": True,
        "service_health": True,
    },
    "hard_stop": "before Qdrant projection or retrieval activation",
}
canonical = json.dumps(report, sort_keys=True, separators=(",", ":"))
report["report_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

phase=complete
printf '%s\n' \
  'MEMORY_V1_V5_2_LEGACY_STAGE_CLAIM_MATERIALIZATION=PASS' \
  "HEAD=$head" \
  "TARGET_SET_SHA256=$expected_target_sha256" \
  "STAGE_MANIFEST_SHA256=$(jq -er '.manifest_sha256' "$stage_manifest")" \
  "REVIEW_MANIFEST_SHA256=$(jq -er '.manifest_sha256' "$review_manifest")" \
  "CLAIM_MANIFEST_SHA256=$(jq -er '.manifest_sha256' "$claim_manifest")" \
  'CLAIMS_CREATED=19' \
  'SURFACE_POLICY_HOLDS=1' \
  'STAGE_ROWS=76' \
  'REVIEW_ROWS=19' \
  'CLAIM_INSERT_ROWS=209' \
  'CLAIM_MUTATED_ROWS=228' \
  'REPLAY_ROWS=0' \
  'MODEL_CALLS=0' \
  'QDRANT_WRITES=0' \
  "BACKUP=$backup" \
  "REPORT=$report"

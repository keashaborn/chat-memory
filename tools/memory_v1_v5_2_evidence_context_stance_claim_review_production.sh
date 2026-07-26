#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Stages and reviews one exact owner-attributed stance.
# It never creates a durable claim, writes Qdrant, or changes retrieval/prompts.

if [[ "${MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STANCE_CLAIM_PRODUCTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STANCE_CLAIM_PRODUCTION=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=fb94c5a7bc967e9b4d600ba2b9a53cd9be774ade
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
observation=87ce1a11-01ae-4d6f-80ea-8e62b5b43cff
stage_runner=scripts/memory_v1_v5_2_evidence_context_stance_claim_stage.py
stage_test=tests/test_memory_v1_v5_2_evidence_context_stance_claim_stage.py
review_manifest_runner=scripts/memory_v1_v5_2_compiler_v8_claim_review_manifest.py
review_runner=scripts/memory_v1_v5_2_evidence_context_stance_claim_review_batch.py
python_bin=/opt/chat-memory/venv/bin/python
review_root=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_evidence_context_stance_claim.lock
phase=initialization
timers_quiesced=0
brains_quiesced=0
brains_state_before=
artifact_dir=
status_file=
run_id=
timer_state=$(mktemp /tmp/memory-v1-v5-2-evidence-context-stance-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-2-evidence-context-stance-tables.XXXXXX)

declare -A expected_delta=(
  [observation_entailment_v5]=1
  [relational_operation_request]=1
  [projection_plan]=1
  [projection_plan_item]=1
  [projection_claim_payload]=1
  [projection_plan_observation]=1
  [projection_review]=1
)

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
      ) AS rows")
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

[[ -z "$(GIT_OPTIONAL_LOCKS=0 git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
head=$(git -C "$repo_root" rev-parse HEAD)
for path in "$stage_runner" "$stage_test" "$review_manifest_runner" "$review_runner"; do
  [[ -f "$repo_root/$path" ]]
done
[[ -x "$python_bin" ]]

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
authenticated_health

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_${head:0:12}"
artifact_dir="$review_root/evidence-context-stance-claim-production-$run_id"
status_file="$snapshot_root/memory_v1_v5_2_evidence_context_stance_claim_${run_id}.status"
mkdir -p "$artifact_dir"
chmod 0700 "$artifact_dir"

phase=quiesce
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
if [[ "$brains_state_before" == active ]]; then
  sudo -n systemctl stop brains.service
fi
brains_quiesced=1
! systemctl is-active --quiet brains.service

phase=backup
backup_partial="$snapshot_root/.memory_pre_v5_2_evidence_context_stance_claim_${run_id}.dump.partial"
backup="$snapshot_root/memory_pre_v5_2_evidence_context_stance_claim_${run_id}.dump"
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

phase=baseline
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
    ORDER BY table_name" >"$table_list"
target_before="$artifact_dir/target-before.tsv"
target_after="$artifact_dir/target-after.tsv"
target_replay="$artifact_dir/target-replay.tsv"
non_target_before="$artifact_dir/non-target-before.tsv"
non_target_after="$artifact_dir/non-target-after.tsv"
non_target_replay="$artifact_dir/non-target-replay.tsv"
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)

manifest="$artifact_dir/stage-manifest.json"
authorization="$artifact_dir/stage-authorization.json"
cross_result="$artifact_dir/stage-cross-owner.json"
stage_apply="$artifact_dir/stage-apply.json"
stage_replay="$artifact_dir/stage-replay.json"
decisions="$artifact_dir/review-decisions.json"
review_manifest="$artifact_dir/review-manifest.json"
review_preflight="$artifact_dir/review-preflight.json"
review_apply="$artifact_dir/review-apply.json"
review_replay="$artifact_dir/review-replay.json"
report="$artifact_dir/report.json"

phase=plan
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$stage_test"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$stage_runner" manifest \
  --owner "$owner" --observation "$observation" \
  --required-head "$head" --output "$manifest"
PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$stage_runner" authorize \
  --manifest "$manifest" --output "$authorization"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$stage_runner" cross-owner \
  --manifest "$manifest" --other-owner "$other_owner" --output "$cross_result"
[[ "$(jq -er '.cross_owner_rejected' "$cross_result")" == true ]]
[[ "$(jq -er '.items[0].canonical_text' "$manifest")" == \
  'The user reports this position: "Fractal Monism will help people in life."' ]]

phase=apply
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STANCE_CLAIM_STAGE_APPLY=authorized \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$stage_runner" apply \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_EXACT_ONE_EVIDENCE_CONTEXT_STANCE_CANDIDATE_ONLY \
  --output "$stage_apply"
[[ "$(jq -er '.rows_written' "$stage_apply")" == 6 ]]

MANIFEST="$manifest" OUTPUT="$decisions" PYTHONPATH="$repo_root" \
  "$python_bin" - <<'PY'
import json
import os
from pathlib import Path
from scripts.memory_v1_projection_v5_contract_test import sha256

stage = json.loads(Path(os.environ["MANIFEST"]).read_text())
value = {
    "contract_version": "memory_v1_v5_2_compiler_v8_claim_review_decisions_v1",
    "owner_user_id": stage["owner_user_id"],
    "evidence_ids": stage["evidence_ids"],
    "decisions": [{
        "observation_id": stage["items"][0]["observation_id"],
        "decision": "authorized",
        "reason": (
            "Direct owner-attributed reported stance. Preserve it as the "
            "user's stated belief, not as an externally established fact."
        ),
        "reason_codes": [
            "attributed_reported_stance",
            "belief_not_external_fact",
            "bound_self_entity",
        ],
    }],
}
value["decisions_sha256"] = sha256(value)
path = Path(os.environ["OUTPUT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$review_manifest_runner" \
  --owner "$owner" --required-head "$head" \
  --stage-manifest "$manifest" --decisions "$decisions" \
  --output "$review_manifest"
MEMORY_V1_REQUIRED_HEAD="$head" \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$review_runner" \
  --mode preflight --manifest "$review_manifest" --output "$review_preflight"
[[ "$(jq -er '.rows_written' "$review_preflight")" == 0 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STANCE_CLAIM_REVIEW_APPLY=authorized \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$review_runner" \
  --mode apply --manifest "$review_manifest" --output "$review_apply"
[[ "$(jq -er '.rows_written' "$review_apply")" == 1 ]]
[[ "$(jq -er '.decision_counts.authorized' "$review_apply")" == 1 ]]

capture_partition target "$target_after"
capture_partition non_target "$non_target_after"
verify_target_delta "$target_before" "$target_after"
cmp -s "$non_target_before" "$non_target_after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=replay
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STANCE_CLAIM_STAGE_APPLY=authorized \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$stage_runner" replay \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_EXACT_ONE_EVIDENCE_CONTEXT_STANCE_CANDIDATE_ONLY \
  --output "$stage_replay"
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STANCE_CLAIM_REVIEW_APPLY=authorized \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root/scripts:$repo_root" \
  "$python_bin" "$repo_root/$review_runner" \
  --mode replay --manifest "$review_manifest" --output "$review_replay"
[[ "$(jq -er '.rows_written' "$stage_replay")" == 0 ]]
[[ "$(jq -er '.rows_written' "$review_replay")" == 0 ]]
capture_partition target "$target_replay"
capture_partition non_target "$non_target_replay"
cmp -s "$target_after" "$target_replay"
cmp -s "$non_target_after" "$non_target_replay"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=postflight
[[ "$(psql_row "
  SELECT count(*)
  FROM memory.projection_claim_payload AS payload
  JOIN memory.projection_plan_item AS item
    ON item.owner_user_id=payload.owner_user_id
   AND item.plan_id=payload.plan_id
   AND item.projection_ref=payload.projection_ref
  JOIN memory.projection_plan_observation AS link
    ON link.owner_user_id=item.owner_user_id
   AND link.plan_id=item.plan_id
   AND link.projection_ref=item.projection_ref
  JOIN memory.projection_review AS review
    ON review.owner_user_id=item.owner_user_id
   AND review.plan_id=item.plan_id
   AND review.projection_ref=item.projection_ref
  WHERE payload.owner_user_id='$owner'
    AND link.observation_id='$observation'::uuid
    AND payload.claim_class='reported_stance'
    AND payload.canonical_text='The user reports this position: \"Fractal Monism will help people in life.\"'
    AND payload.surface_policy='relevant_recall_or_explicit_recall'
    AND item.review_state='manual_review_required'
    AND review.decision='authorized'")" == 1 ]]
[[ "$(psql_row "SELECT count(*) FROM memory.claim_observation
  WHERE owner_user_id='$owner'::uuid
    AND observation_id='$observation'::uuid")" == 0 ]]

phase=restore
restore_runtime
authenticated_health
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ -z "$(GIT_OPTIONAL_LOCKS=0 git -C "$repo_root" status --porcelain)" ]]

REPORT="$report" BACKUP="$backup" MANIFEST="$manifest" REVIEW="$review_manifest" \
STAGE="$stage_apply" REVIEW_RESULT="$review_apply" HEAD="$head" \
QDRANT="$qdrant_before" python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

stage = json.loads(Path(os.environ["MANIFEST"]).read_text())
review = json.loads(Path(os.environ["REVIEW"]).read_text())
backup = Path(os.environ["BACKUP"])
value = {
    "contract_version": "memory_v1_v5_2_evidence_context_stance_claim_review_production_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": stage["owner_user_id"],
    "evidence_id": stage["items"][0]["evidence_id"],
    "observation_id": stage["items"][0]["observation_id"],
    "canonical_text": stage["items"][0]["canonical_text"],
    "backup": str(backup),
    "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
    "stage_manifest_sha256": stage["manifest_sha256"],
    "review_manifest_sha256": review["manifest_sha256"],
    "verification": {
        "candidate_rows_written": 6,
        "review_rows_written": 1,
        "zero_write_replay": True,
        "cross_owner_rejected": True,
        "non_target_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "claims_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
        "timers_restored": True,
        "service_healthy": True,
    },
    "results": {
        "stage_result_sha256": json.loads(Path(os.environ["STAGE"]).read_text())["result_sha256"],
        "review_result_sha256": json.loads(Path(os.environ["REVIEW_RESULT"]).read_text())["result_sha256"],
    },
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

phase=complete
printf 'report=%s\n' "$report"
printf 'backup=%s\n' "$backup"
printf 'memory_v1_v5_2_evidence_context_stance_claim_review_production: PASS\n'

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Completes verification/reporting after a V5.1
# relationship staging run that applied and replayed successfully but stopped
# while waiting for Brains readiness. This script is read-only except for its
# immutable report and live-state evidence files.

if [[ "${MEMORY_V1_RELATIONSHIP_CLAIM_V5_1_RECOVERY_VERIFY:-}" != authorized ]]; then
  echo 'MEMORY_V1_RELATIONSHIP_CLAIM_V5_1_RECOVERY_VERIFY=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 7 ]]; then
  echo 'usage: recovery_verify.sh BUNDLE PREFLIGHT APPLY REPLAY RUN_ID INSTALL_REPORT OUTPUT' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
bundle=$(realpath "$1")
preflight_result=$(realpath "$2")
apply_result=$(realpath "$3")
replay_result=$(realpath "$4")
run_id=$5
install_report=$(realpath "$6")
output=$(realpath -m "$7")
review_root=/home/ubuntu/memory-v1-reviews
snapshot_dir=/home/ubuntu/brains/snapshots
container=brains-postgres-1
database=memory

[[ "$run_id" =~ ^[0-9]{8}T[0-9]{6}Z_[0-9a-f]{12}$ ]]
for input in "$bundle" "$preflight_result" "$apply_result" "$replay_result"; do
  [[ "$input" == "$review_root"/* && -f "$input" && "$(stat -c '%a' "$input")" == 600 ]]
done
[[ "$install_report" == "$snapshot_dir"/* && -f "$install_report" ]]
[[ "$output" == "$snapshot_dir"/* && ! -e "$output" ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]

status="$snapshot_dir/memory_v1_relationship_claim_v5_1_stage_${run_id}.status"
backup="$snapshot_dir/memory_pre_relationship_claim_v5_1_stage_${run_id}.dump"
cross_owner="$snapshot_dir/memory_v1_relationship_claim_v5_1_stage_cross_owner_${run_id}.log"
target_before="$snapshot_dir/memory_v1_relationship_claim_v5_1_stage_target_before_${run_id}.tsv"
target_preflight="$snapshot_dir/memory_v1_relationship_claim_v5_1_stage_target_preflight_${run_id}.tsv"
target_after="$snapshot_dir/memory_v1_relationship_claim_v5_1_stage_target_after_${run_id}.tsv"
target_replay="$snapshot_dir/memory_v1_relationship_claim_v5_1_stage_target_replay_${run_id}.tsv"
non_target_before="$snapshot_dir/memory_v1_relationship_claim_v5_1_stage_non_target_before_${run_id}.tsv"
non_target_preflight="$snapshot_dir/memory_v1_relationship_claim_v5_1_stage_non_target_preflight_${run_id}.tsv"
non_target_after="$snapshot_dir/memory_v1_relationship_claim_v5_1_stage_non_target_after_${run_id}.tsv"
non_target_replay="$snapshot_dir/memory_v1_relationship_claim_v5_1_stage_non_target_replay_${run_id}.tsv"
for evidence in "$status" "$backup" "$backup.sha256" "$backup.catalog" \
  "$cross_owner" "$target_before" "$target_preflight" "$target_after" \
  "$target_replay" "$non_target_before" "$non_target_preflight" \
  "$non_target_after" "$non_target_replay"; do
  [[ -f "$evidence" ]]
done
sha256sum -c "$backup.sha256" >/dev/null
grep -qx 'phase=restore_runtime' "$status"
grep -qx 'exit_code=4' "$status"
rg -q 'complete accepted owner-scoped V5.1 relationship source not found' \
  "$cross_owner"
cmp -s "$target_before" "$target_preflight"
cmp -s "$non_target_before" "$non_target_preflight"
cmp -s "$target_after" "$target_replay"
cmp -s "$non_target_after" "$non_target_replay"
cmp -s "$non_target_before" "$non_target_after"

BUNDLE="$bundle" PREFLIGHT="$preflight_result" APPLY="$apply_result" \
REPLAY="$replay_result" BEFORE="$target_before" AFTER="$target_after" \
python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

def stable(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def digest(value):
    material = value if isinstance(value, str) else stable(value)
    return hashlib.sha256(material.encode()).hexdigest()

bundle = json.loads(Path(os.environ["BUNDLE"]).read_text())
if bundle["contract_version"] != "memory_v1_relationship_claim_stage_bundle_v5_1":
    raise SystemExit("bundle contract mismatch")
if bundle["expected_new_rows"] != 4 or bundle["predicate"] != "relationship.parent_of":
    raise SystemExit("bundle boundary mismatch")
if bundle["bundle_sha256"] != digest({k: v for k, v in bundle.items() if k != "bundle_sha256"}):
    raise SystemExit("bundle hash mismatch")

expected_results = {
    "PREFLIGHT": ("preflight", "preflight", 0),
    "APPLY": ("apply", "applied", 4),
    "REPLAY": ("replay", "replayed", 0),
}
for env, expected in expected_results.items():
    result = json.loads(Path(os.environ[env]).read_text())
    if (result["mode"], result["outcome"], result["rows_written"]) != expected:
        raise SystemExit(f"{env.lower()} result mismatch")
    if result["bundle_sha256"] != bundle["bundle_sha256"]:
        raise SystemExit(f"{env.lower()} bundle mismatch")
    if result["result_sha256"] != digest({k: v for k, v in result.items() if k != "result_sha256"}):
        raise SystemExit(f"{env.lower()} hash mismatch")

def load(path):
    rows = {}
    for line in Path(path).read_text().splitlines():
        table, count, sha = line.split("\t")
        rows[table] = (int(count), sha)
    return rows

expected_delta = {
    "projection_plan": 1,
    "projection_plan_item": 1,
    "projection_claim_payload": 1,
    "projection_plan_observation": 1,
}
before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
if before.keys() != after.keys():
    raise SystemExit("target table set changed")
for table in before:
    wanted = expected_delta.get(table, 0)
    if after[table][0] - before[table][0] != wanted:
        raise SystemExit(f"unexpected target delta for {table}")
    if wanted == 0 and after[table][1] != before[table][1]:
        raise SystemExit(f"unexpected target mutation for {table}")
PY

owner=$(jq -er '.owner_user_id' "$bundle")
plan=$(jq -er '.plan_id' "$bundle")
[[ "$owner" =~ ^[0-9a-f-]{36}$ && "$plan" =~ ^[0-9a-f-]{36}$ ]]
db_state=$(docker exec "$container" psql -X -A -t -F '|' \
  -v ON_ERROR_STOP=1 -U sage -d "$database" -c "
    SELECT
      (SELECT count(*) FROM memory.projection_plan
        WHERE owner_user_id='$owner'::uuid AND plan_id='$plan'::uuid),
      (SELECT count(*) FROM memory.projection_plan_item
        WHERE owner_user_id='$owner'::uuid AND plan_id='$plan'::uuid),
      (SELECT count(*) FROM memory.projection_claim_payload
        WHERE owner_user_id='$owner'::uuid AND plan_id='$plan'::uuid),
      (SELECT count(*) FROM memory.projection_plan_observation
        WHERE owner_user_id='$owner'::uuid AND plan_id='$plan'::uuid),
      (SELECT count(*) FROM memory.claim AS claim
        JOIN memory.projection_plan_item AS item
          ON item.owner_user_id=claim.owner_user_id
         AND claim.canonical_key='v5:'||item.semantic_key_sha256
        WHERE item.owner_user_id='$owner'::uuid AND item.plan_id='$plan'::uuid)
  " | tr -d '[:space:]')
[[ "$db_state" == '1|1|1|1|0' ]]

qdrant_expected=$(jq -er '.verification.qdrant_after_sha256' "$install_report")
qdrant_current=$(curl --fail --silent --show-error --max-time 30 \
  -H 'content-type: application/json' \
  -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
  http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
  | jq -cS '.result.points | sort_by(.id|tostring)' \
  | sha256sum | awk '{print $1}')
[[ "$qdrant_current" == "$qdrant_expected" ]]

timer_expected=$(jq -er '.evidence.timer_state_after' "$install_report")
[[ -f "$timer_expected" ]]
timer_current="$snapshot_dir/memory_v1_relationship_claim_v5_1_stage_timer_recovery_${run_id}.tsv"
: >"$timer_current"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_current"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
chmod 0600 "$timer_current"
cmp -s "$timer_expected" "$timer_current"

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
    break
  fi
  [[ "$_attempt" -lt 30 ]]
  sleep 1
done

REPORT="$output" BUNDLE="$bundle" PREFLIGHT="$preflight_result" \
APPLY="$apply_result" REPLAY="$replay_result" STATUS="$status" \
BACKUP="$backup" TARGET_BEFORE="$target_before" TARGET_AFTER="$target_after" \
NON_TARGET_BEFORE="$non_target_before" NON_TARGET_AFTER="$non_target_after" \
CROSS_OWNER="$cross_owner" TIMER_EXPECTED="$timer_expected" \
TIMER_CURRENT="$timer_current" QDRANT="$qdrant_current" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

bundle = json.loads(Path(os.environ["BUNDLE"]).read_text())
report = {
    "contract_version": "memory_v1_relationship_claim_v5_1_stage_recovery_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "verification_head_commit": os.environ["HEAD"],
    "apply_head_commit": bundle["required_head_commit"],
    "owner_user_id": bundle["owner_user_id"],
    "plan_id": bundle["plan_id"],
    "bundle_sha256": bundle["bundle_sha256"],
    "recovered_failure": {
        "status": os.environ["STATUS"],
        "phase": "restore_runtime",
        "cause": "brains_readiness_checked_before_listener_was_ready",
    },
    "backup": os.environ["BACKUP"],
    "evidence": {
        "preflight_result": os.environ["PREFLIGHT"],
        "apply_result": os.environ["APPLY"],
        "replay_result": os.environ["REPLAY"],
        "target_before": os.environ["TARGET_BEFORE"],
        "target_after": os.environ["TARGET_AFTER"],
        "non_target_before": os.environ["NON_TARGET_BEFORE"],
        "non_target_after": os.environ["NON_TARGET_AFTER"],
        "cross_owner_rejection": os.environ["CROSS_OWNER"],
        "timer_expected": os.environ["TIMER_EXPECTED"],
        "timer_current": os.environ["TIMER_CURRENT"],
    },
    "verification": {
        "preflight_rows_written": 0,
        "apply_rows_written": 4,
        "replay_rows_written": 0,
        "exact_target_delta": True,
        "live_plan_rows_exact": True,
        "claims_written": 0,
        "cross_owner_rejected": True,
        "non_target_and_global_memory_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "timers_restored_exactly": True,
        "brains_health_ready": True,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    },
    "hard_stop": "before_relationship_plan_review_or_claim_materialization",
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
sha = hashlib.sha256(path.read_bytes()).hexdigest()
Path(str(path) + ".sha256").write_text(f"{sha}  {path}\n")
Path(str(path) + ".sha256").chmod(0o600)
PY

printf 'report=%s\n' "$output"
printf 'memory_v1_relationship_claim_stage_v5_1_recovery_verify: PASS\n'

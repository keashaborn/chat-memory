#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Read-only postflight recovery for the successful durable
# self-name atom transaction whose first isolation probe lacked table access.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

required_head=2430e2ec3c49d2cd1cc5e968a03ebd83a4e926f3
run_tag=20260730T074819Z_7140d441718b
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
review_dir=/home/ubuntu/memory-v1-reviews/self-name-atom-$run_tag
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=9dd7426d-77eb-4765-9db2-13e33ad7444d
packet=1f7fe393-afc3-5982-b69c-c90877659ef6
status_file="$snapshot_dir/memory_v1_v5_2_self_name_atom_${run_tag}.status"
timer_state="$snapshot_dir/memory_v1_v5_2_self_name_atom_timers_${run_tag}.tsv"
target_before="$snapshot_dir/memory_v1_v5_2_self_name_atom_target_before_${run_tag}.tsv"
target_after="$snapshot_dir/memory_v1_v5_2_self_name_atom_target_after_${run_tag}.tsv"
non_target_before="$snapshot_dir/memory_v1_v5_2_self_name_atom_non_target_before_${run_tag}.tsv"
non_target_after="$snapshot_dir/memory_v1_v5_2_self_name_atom_non_target_after_${run_tag}.tsv"
backup="$snapshot_dir/memory_pre_v5_2_self_name_atom_${run_tag}.dump"
manifest="$review_dir/manifest.json"
preflight="$review_dir/preflight.json"
apply="$review_dir/apply.json"
replay="$review_dir/replay.json"
report="$snapshot_dir/memory_v1_v5_2_self_name_atom_${run_tag}_postflight_recovery.json"
current_timers=$(mktemp /tmp/memory-v1-v5-2-self-name-current-timers.XXXXXX)
trap 'rm -f "$current_timers"' EXIT

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

[[ "$(id -u)" == 0 ]]
[[ "$(git rev-parse HEAD)" == "$required_head" ]]
[[ -z "$(git status --porcelain)" ]]
for file in \
  "$status_file" "$timer_state" "$target_before" "$target_after" \
  "$non_target_before" "$non_target_after" "$backup" "$backup.sha256" \
  "$manifest" "$preflight" "$apply" "$replay"; do
  [[ -f "$file" ]]
done
[[ ! -e "$report" ]]
grep -qx 'phase=owner_isolation' "$status_file"
grep -qx 'exit_code=1' "$status_file"
grep -qx 'durable_rows_present=1' "$status_file"
(cd "$(dirname "$backup")" && sha256sum -c "$(basename "$backup.sha256")")

manifest_sha=$(jq -er '.manifest_sha256' "$manifest")
for output in "$preflight" "$apply" "$replay"; do
  [[ "$(jq -er '.manifest_sha256' "$output")" == "$manifest_sha" ]]
done
jq -e '
  .mode=="preflight" and .persistent_writes==0 and
  (.results|length)==1 and .results[0].apply_outcome=="applied" and
  (.same_transaction_replay|length)==1 and
  .same_transaction_replay[0].apply_outcome=="replayed"
' "$preflight" >/dev/null
jq -e '
  .mode=="apply" and .persistent_writes==6 and
  (.results|length)==1 and .results[0].apply_outcome=="applied" and
  (.same_transaction_replay|length)==1 and
  .same_transaction_replay[0].apply_outcome=="replayed"
' "$apply" >/dev/null
jq -e '
  .mode=="replay" and .persistent_writes==0 and
  (.results|length)==1 and .results[0].apply_outcome=="replayed" and
  (.same_transaction_replay|length)==0
' "$replay" >/dev/null

cmp -s "$non_target_before" "$non_target_after"
BEFORE="$target_before" AFTER="$target_after" python3 - <<'PY'
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
if before.keys() != after.keys():
    raise SystemExit("target-owner table set changed")
expected = {
    "v5_2_atom_admission_proposal": 1,
    "v5_2_atom_admission_review": 1,
    "v5_2_atom_admission_apply": 1,
    "v5_2_atom_admission_operation": 3,
}
for table in before:
    delta = after[table][0] - before[table][0]
    wanted = expected.get(table, 0)
    if delta != wanted:
        raise SystemExit(f"unexpected target-owner delta {table}: {delta} != {wanted}")
    if wanted == 0 and before[table][1] != after[table][1]:
        raise SystemExit(f"unexpected target-owner mutation {table}")
PY

proposal_operation=$(jq -er '.items[0].proposal_operation_id' "$manifest")
review_operation=$(jq -er '.items[0].review_operation_id' "$manifest")
apply_operation=$(jq -er '.items[0].apply_operation_id' "$manifest")
[[ "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal
      WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review AS review
      JOIN memory.v5_2_atom_admission_proposal AS proposal
        ON proposal.owner_user_id=review.owner_user_id
       AND proposal.proposal_id=review.proposal_id
      WHERE proposal.owner_user_id='$owner'::uuid
        AND proposal.packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply
      WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation
      WHERE owner_user_id='$owner'::uuid
        AND operation_id IN (
          '$proposal_operation'::uuid,
          '$review_operation'::uuid,
          '$apply_operation'::uuid
        ))
  )
")" == '1,1,1,3' ]]

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <<SQL >/dev/null
BEGIN READ ONLY;
SET LOCAL ROLE memory_v5_2_atom_admission_maintainer;
SELECT set_config('app.user_id','$other',true);
DO \$probe\$
BEGIN
  IF (
    SELECT count(*)
    FROM memory.v5_2_atom_admission_proposal
    WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
  ) <> 0 THEN
    RAISE EXCEPTION 'cross-owner atom proposal became visible';
  END IF;
END
\$probe\$;
ROLLBACK;
SQL

: >"$current_timers"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$current_timers"
done < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
cmp -s "$timer_state" "$current_timers"

set -a
source /opt/chat-memory/.env
set +a
[[ "$(systemctl is-active brains.service)" == active ]]
curl --fail --silent --show-error --max-time 10 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
qdrant_sha=$(qdrant_signature)
backup_sha=$(sha256sum "$backup" | awk '{print $1}')

jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head "$required_head" --arg run_tag "$run_tag" \
  --arg manifest "$manifest" --arg manifest_sha256 "$manifest_sha" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_sha" \
  '{
    contract_version:"memory_v1_v5_2_self_identity_name_atom_postflight_recovery_v1",
    completed_at:$completed_at,head:$head,run_tag:$run_tag,
    manifest:{path:$manifest,sha256:$manifest_sha256},
    backup:{path:$backup,sha256:$backup_sha256},
    results:{
      durable_rows:{proposal:1,review:1,apply:1,operations:3},
      zero_write_replay:true,
      exact_target_owner_delta:true,
      non_target_and_global_memory_unchanged:true,
      original_run_qdrant_gate_passed_before_failed_isolation_probe:true,
      corrected_restricted_role_isolation_probe:true,
      timers_restored_exactly:true,
      service_healthy:true,
      relational_staging_rows_created:0,
      claim_rows_created:0,
      external_model_calls:0,
      retrieval_or_prompt_influence:false
    },
    current_qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_relational_staging_or_claims_or_projection_or_retrieval"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

printf '%s\n' \
  'memory_v1_v5_2_self_identity_name_atom_postflight_recovery: PASS' \
  "report=$report" \
  "manifest_sha256=$manifest_sha" \
  'rows=1,1,1,3' \
  'stage=0 claims=0 qdrant_writes=0 prompt_influence=0'

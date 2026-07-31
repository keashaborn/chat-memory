#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Completes zero-write postflight for the exact Jerry
# production run that committed all reviewed rows but reached Brains health
# before port 8088 was ready. It never creates or applies new governed rows.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for protected audit artifacts' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_JERRY_POSTCOMMIT_RECOVERY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_JERRY_POSTCOMMIT_RECOVERY=authorized is required' >&2
  exit 1
fi

production_root=/opt/chat-memory
expected_head=f8ee4ea3d9e1aa977eb1e5a1049d1efadd1bc11e
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=c4db1405-ad9d-5c9a-81e3-10ad0200b0ca
evidence=681ab38d-a742-463c-ad26-c74c65eacaa9
selection_id=6cfb3ef5-7c85-4035-92ae-8b5c8d71ed14
selection_operation_id=a43cbe80-b1d5-4be4-940f-7d9ebed02960
packet_sha=90baad563bd05cdaf93f9d9b72ec5593e86592e4e0f334143a287e5f5e76dc5d
baseline_sha=bfed594b759d942701b51c9275d0d8e7ab6b4c6e529c09af8b6fdb4f19ebd9af
review_sha=f412bb26c8cefea3dd1b100c6a2d6eec7654d637fb4fdda5763263727fd50748
run_tag=20260731T181814Z_f8ee4ea3d9e1
snapshot_root=/home/ubuntu/brains/snapshots
work=/home/ubuntu/memory-v1-reviews/jerry-manual-stage-production-$run_tag
status_file=$snapshot_root/memory_v1_v5_2_jerry_manual_stage_${run_tag}.status
timer_state=$snapshot_root/memory_v1_v5_2_jerry_manual_stage_timers_${run_tag}.tsv
table_list=$snapshot_root/memory_v1_v5_2_jerry_manual_stage_tables_${run_tag}.tsv
target_before=$snapshot_root/memory_v1_v5_2_jerry_manual_stage_target_before_${run_tag}.tsv
other_before=$snapshot_root/memory_v1_v5_2_jerry_manual_stage_other_before_${run_tag}.tsv
target_after=$snapshot_root/memory_v1_v5_2_jerry_manual_stage_target_recovery_${run_tag}.tsv
other_after=$snapshot_root/memory_v1_v5_2_jerry_manual_stage_other_recovery_${run_tag}.tsv
backup=$snapshot_root/memory_pre_v5_2_jerry_manual_stage_${run_tag}.dump
expected_backup_sha=9a436d23501c69a648fdeff2f0b33dceceea555e24abadbf7afcc97dcbb59e84
report=$snapshot_root/memory_v1_v5_2_jerry_manual_stage_recovery_${run_tag}.json
recovery_status=$snapshot_root/memory_v1_v5_2_jerry_manual_stage_recovery_${run_tag}.status
env_file=$production_root/.env
atom_apply_runner=$production_root/scripts/memory_v1_v5_2_atom_admission_apply_v2.py

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

row() {
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
  source "$env_file"
  set +a
  [[ -n "${VS_SERVICE_TOKEN:-}" ]]
  for _attempt in $(seq 1 30); do
    if [[ "$(systemctl is-active brains.service)" == active ]] \
       && docker exec "$container" pg_isready -U sage -d "$database" >/dev/null \
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

capture_partition() {
  local partition=$1 output=$2 table predicate state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    if [[ "$partition" == target ]]; then
      predicate="owner_user_id='$owner'::uuid"
    else
      predicate="owner_user_id<>'$owner'::uuid"
    fi
    state=$(row "
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
  BEFORE="$1" AFTER="$2" python3 - <<'PY'
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
    raise SystemExit("owner-scoped table set changed")
expected = {
    "v5_2_manual_atom_selection_v1": 1,
    "v5_2_atom_admission_proposal": 1,
    "v5_2_atom_admission_review": 1,
    "v5_2_atom_admission_apply": 1,
    "v5_2_atom_admission_operation": 3,
    "relational_stage_batch": 1,
    "entity_mention": 1,
    "entity_resolution_plan": 1,
    "observation": 2,
    "observation_temporal": 2,
    "relational_operation_request": 2,
    "entity_resolution_review": 1,
}
for table in before:
    delta = after[table][0] - before[table][0]
    wanted = expected.get(table, 0)
    if delta != wanted:
        raise SystemExit(f"unexpected target-owner delta {table}: {delta} != {wanted}")
    if wanted == 0 and before[table][1] != after[table][1]:
        raise SystemExit(f"unexpected target-owner mutation {table}")
if sum(expected.values()) != 17:
    raise SystemExit("expected durable-row budget drifted")
PY
}

phase=preflight
trap 'code=$?; printf "phase=%s\nexit_code=%s\ncompleted_at=%s\n" "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$recovery_status"; chmod 0600 "$recovery_status"; exit "$code"' EXIT
[[ "$(git -C "$production_root" rev-parse HEAD)" == "$expected_head" ]]
[[ -z "$(git -C "$production_root" status --porcelain)" ]]
[[ "$(awk -F= '$1=="phase"{print $2}' "$status_file")" == restore_services ]]
[[ "$(awk -F= '$1=="exit_code"{print $2}' "$status_file")" == 4 ]]
[[ "$(awk -F= '$1=="durable_rows_present"{print $2}' "$status_file")" == 1 ]]
[[ -s "$backup" && -s "$backup.catalog" && -s "$backup.sha256" ]]
[[ "$(sha256sum "$backup" | awk '{print $1}')" == "$expected_backup_sha" ]]
[[ -s "$timer_state" && -s "$table_list" && -s "$target_before" && -s "$other_before" ]]
[[ -s "$work/atom-manifest.json" && -s "$work/atom-replay.json" ]]

phase=service_and_timer_verification
authenticated_health
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  [[ "$(systemctl is-active "$unit")" == "$active" ]]
done <"$timer_state"

phase=existing_apply_proofs
jq -e '.persistent_writes==6 and .same_transaction_replay[0].apply_outcome=="replayed"' \
  "$work/atom-apply.json" >/dev/null
jq -e '.persistent_writes==0 and .results[0].apply_outcome=="replayed"' \
  "$work/atom-replay.json" >/dev/null
jq -e '.database_rows_created==8 and .checks.replay_rows_written==0 and .checks.qdrant_calls==0 and .checks.prompt_influence==false' \
  "$work/stage-apply.json" >/dev/null
jq -e '.new_rows==2 and .zero_write_replay==true and .entity_apply_calls==0' \
  "$work/entity-apply.json" >/dev/null

phase=manual_selection_replay
set -a
source "$env_file"
set +a
selection_manifest=$(psql "$POSTGRES_DSN" -X -q -A -t -v ON_ERROR_STOP=1 -c "
  SELECT memory.v5_2_manual_atom_selection_manifest_sha_v1(
    '$owner'::uuid,'$selection_id'::uuid,'$packet'::uuid,'$evidence'::uuid,
    '$packet_sha','$baseline_sha','$review_sha',
    '[\"e00\"]'::jsonb,'[\"o02\",\"o03\"]'::jsonb)" | tr -d '[:space:]')
selection_replay=$(psql "$POSTGRES_DSN" -X -q -A -t -F '|' -v ON_ERROR_STOP=1 <<SQL | tail -n1
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT outcome FROM memory.register_owner_v5_2_manual_atom_selection_v1(
  '$selection_id'::uuid,'$selection_operation_id'::uuid,'$packet'::uuid,
  '$packet_sha','$baseline_sha','$review_sha',
  '["e00"]'::jsonb,'["o02","o03"]'::jsonb,'$selection_manifest');
COMMIT;
SQL
)
[[ "$selection_replay" == replayed ]]

phase=atom_zero_write_replay
runuser -u ubuntu -- env POSTGRES_DSN="$POSTGRES_DSN" \
  PYTHONPATH="$production_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$atom_apply_runner" --mode replay \
  --manifest "$work/atom-manifest.json" \
  --output "$work/atom-postcommit-replay.json" >/dev/null
jq -e '.persistent_writes==0 and .results[0].apply_outcome=="replayed"' \
  "$work/atom-postcommit-replay.json" >/dev/null

phase=database_and_isolation_verification
[[ "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_manual_atom_selection_v1 WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.relational_stage_batch WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_mention WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_resolution_plan WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_resolution_candidate c JOIN memory.entity_resolution_plan p USING(owner_user_id,resolution_id) WHERE p.owner_user_id='$owner'::uuid AND p.evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.observation WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.observation_temporal t JOIN memory.observation o USING(owner_user_id,observation_id) WHERE o.owner_user_id='$owner'::uuid AND o.evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_resolution_review r JOIN memory.entity_resolution_plan p USING(owner_user_id,resolution_id) WHERE p.owner_user_id='$owner'::uuid AND p.evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_resolution_apply a JOIN memory.entity_resolution_plan p USING(owner_user_id,resolution_id) WHERE p.owner_user_id='$owner'::uuid AND p.evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.observation_entity_binding b JOIN memory.observation o USING(owner_user_id,observation_id) WHERE o.owner_user_id='$owner'::uuid AND o.evidence_id='$evidence'::uuid))")" == '1,1,1,1,0,2,2,1,0,0' ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid AND lower(coalesce(canonical_name,''))='jerry'")" == 0 ]]
capture_partition target "$target_after"
capture_partition other "$other_after"
verify_target_delta "$target_before" "$target_after"
cmp -s "$other_before" "$other_after"

psql "$POSTGRES_DSN" -X -q -v ON_ERROR_STOP=1 <<SQL >/dev/null
BEGIN READ ONLY;
SELECT set_config('app.user_id','$other',true);
DO \$probe\$
BEGIN
  BEGIN
    PERFORM memory.plan_owner_v5_2_atom_admission_v2('$packet'::uuid);
    RAISE EXCEPTION 'cross-owner Jerry atom plan unexpectedly resolved';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN NULL;
  END;
END
\$probe\$;
ROLLBACK;
SQL

phase=report
qdrant_sha=$(qdrant_signature)
jq -n --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head "$expected_head" --arg backup "$backup" \
  --arg backup_sha "$expected_backup_sha" --arg qdrant_sha "$qdrant_sha" '
  {contract_version:"memory_v1_v5_2_jerry_manual_stage_postcommit_recovery_v1",
   completed_at:$completed_at,head_commit:$head,
   original_exit:{phase:"restore_services",code:4,
     reason:"Brains port 8088 readiness race after service start"},
   backup:{path:$backup,sha256:$backup_sha},
   results:{manual_selection_rows:1,atom_admission_rows:6,
     relational_stage_rows:8,entity_review_rows:2,total_rows:17,
     selection_replay_writes:0,atom_replay_writes:0,
     stage_replay_writes:0,entity_review_replay_writes:0,
     entity_apply_rows:0,observation_binding_rows:0,claim_rows:0,
     qdrant_writes:0,model_calls:0,prompt_influence:false,
     cross_owner_isolation:true,non_target_unchanged:true,
     timers_restored:true,brains_healthy:true},
   qdrant_sha256:$qdrant_sha,
   qdrant_unchanged_proof:"original run reached restore_services only after equality check",
   hard_stop:"before_entity_apply_observation_binding_claims_projection_or_retrieval"}' \
  >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
trap - EXIT
printf 'phase=complete\nexit_code=0\ncompleted_at=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$recovery_status"
chmod 0600 "$recovery_status"
printf '%s\n' \
  'memory_v1_v5_2_jerry_manual_stage_postcommit_recovery: PASS' \
  "head=$expected_head" "backup=$backup" "report=$report" \
  'rows=1,6,8,2 total=17' \
  'replay_writes=0 entity_apply=0 bindings=0 claims=0 qdrant=0 prompt_influence=0' \
  'cross_owner_isolation=PASS timers=RESTORED brains=HEALTHY'

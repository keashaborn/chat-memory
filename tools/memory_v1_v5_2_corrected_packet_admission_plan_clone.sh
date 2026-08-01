#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Production Postgres and Qdrant remain read-only.
[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_2_correction_plan_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
stance_packet=2c3b60f7-8985-52bc-bdd3-1f4e449c561e
father_packet=720ad658-67e6-54f5-aa6d-5a297a360fd9
migration=ops/sql/20260801_memory_v1_v5_2_corrected_packet_admission_plan_v1.sql
rollback=ops/sql/20260801_memory_v1_v5_2_corrected_packet_admission_plan_v1_rollback.sql
artifact_dir="/home/ubuntu/memory-v1-reviews/corrected-packet-plan-clone-$(date -u +%Y%m%dT%H%M%SZ)"
dump_file=''
memory_acl_list=''

for path in "$migration" "$rollback"; do
  [[ -f "$repo_root/$path" ]]
done
mkdir -p -m 0700 "$artifact_dir"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
  rm -f "$dump_file" "$memory_acl_list"
}
trap cleanup EXIT

scalar() {
  local database=$1
  local sql=$2
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$sql" | sed -n '1p'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

protected_signature() {
  local database=$1
  scalar "$database" "
    SELECT md5(jsonb_build_object(
      'evidence',(SELECT count(*) FROM memory.evidence),
      'entity',(SELECT count(*) FROM memory.entity),
      'mention',(SELECT count(*) FROM memory.entity_mention),
      'resolution',(SELECT count(*) FROM memory.entity_resolution_plan),
      'resolution_apply',(SELECT count(*) FROM memory.entity_resolution_apply),
      'observation',(SELECT count(*) FROM memory.observation),
      'binding',(SELECT count(*) FROM memory.observation_entity_binding),
      'temporal',(SELECT count(*) FROM memory.observation_temporal),
      'entailment',(SELECT count(*) FROM memory.observation_entailment_v5),
      'claim',(SELECT count(*) FROM memory.claim),
      'claim_revision',(SELECT count(*) FROM memory.claim_revision),
      'projection_outbox',(SELECT count(*) FROM memory.projection_outbox),
      'answer_binding',(SELECT count(*)
        FROM memory.final_answer_memory_binding_v1)
    )::text)"
}

acl_signature() {
  local database=$1
  scalar "$database" "
    SELECT md5(jsonb_build_object(
      'evidence',has_table_privilege(
        'memory_v5_2_atom_admission_maintainer','memory.evidence','SELECT'),
      'packet',has_table_privilege(
        'memory_v5_2_atom_admission_maintainer',
        'memory.evidence_extraction_packet_v5_local','SELECT'),
      'supersession',has_table_privilege(
        'memory_v5_2_atom_admission_maintainer',
        'memory.v5_local_packet_supersession','SELECT'),
      'route',has_table_privilege(
        'memory_v5_2_atom_admission_maintainer',
        'memory.v5_2_local_packet_route_event','SELECT'),
      'entity',has_table_privilege(
        'memory_v5_2_atom_admission_maintainer','memory.entity','SELECT'),
      'observation',has_table_privilege(
        'memory_v5_2_atom_admission_maintainer','memory.observation','SELECT'),
      'stage',has_table_privilege(
        'memory_v5_2_atom_admission_maintainer',
        'memory.relational_stage_batch','SELECT'),
      'normalize',has_function_privilege(
        'memory_v5_2_atom_admission_maintainer',
        'memory.normalize_entity_name_v5(text)','EXECUTE'),
      'semantic_slot',has_function_privilege(
        'memory_v5_2_atom_admission_maintainer',
        'memory.v5_2_observation_semantic_slot_v1(text,jsonb)','EXECUTE')
    )::text)"
}

production_activity() {
  scalar "$source_db" "
    SELECT jsonb_build_object(
      'packets',(SELECT count(*)
        FROM memory.evidence_extraction_packet_v5_local),
      'stages',(SELECT count(*) FROM memory.relational_stage_batch),
      'entities',(SELECT count(*) FROM memory.entity),
      'observations',(SELECT count(*) FROM memory.observation),
      'claims',(SELECT count(*) FROM memory.claim),
      'outbox',(SELECT count(*) FROM memory.projection_outbox),
      'bindings',(SELECT count(*) FROM memory.final_answer_memory_binding_v1)
    )"
}

qdrant_before=$(qdrant_signature)
production_before=$(production_activity)

dump_file=$(mktemp /tmp/memory-v5-2-correction-plan.XXXXXX.dump)
memory_acl_list=$(mktemp /tmp/memory-v5-2-correction-plan-acl.XXXXXX.list)
chmod 0600 "$dump_file" "$memory_acl_list"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc >"$dump_file"
pg_restore -l "$dump_file" \
  | grep -E ' ACL memory | ACL - SCHEMA memory ' \
  >"$memory_acl_list"
[[ -s "$memory_acl_list" ]]
! grep -q 'lifeswitch_chat' "$memory_acl_list"

docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec -i "$container" pg_restore --exit-on-error --no-acl \
  -U sage -d "$clone_db" <"$dump_file"
pg_restore --exit-on-error --use-list="$memory_acl_list" -f - "$dump_file" \
  | docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
      -U sage -d "$clone_db" >/dev/null

protected_before=$(protected_signature "$clone_db")
acl_before=$(acl_signature "$clone_db")
docker exec -i "$container" psql -X -U sage -d "$clone_db" \
  -v ON_ERROR_STOP=1 <"$repo_root/$migration" \
  | tee "$artifact_dir/migration.txt"

contract_report=$(scalar "$clone_db" "
  SELECT jsonb_build_object(
    'table_present',to_regclass(
      'memory.v5_2_corrected_packet_admission_proposal_v1') IS NOT NULL,
    'planner_present',to_regprocedure(
      'memory.plan_owner_v5_2_corrected_packet_admission_v1(uuid)'
    ) IS NOT NULL,
    'recorder_present',to_regprocedure(
      'memory.record_owner_v5_2_corrected_packet_admission_proposal_v1(uuid,uuid,uuid,text,text)'
    ) IS NOT NULL,
    'brains_plan',has_function_privilege('brains_app',
      'memory.plan_owner_v5_2_corrected_packet_admission_v1(uuid)',
      'EXECUTE'),
    'public_plan',has_function_privilege('public',
      'memory.plan_owner_v5_2_corrected_packet_admission_v1(uuid)',
      'EXECUTE'),
    'forced_rls',(SELECT relforcerowsecurity FROM pg_class
      WHERE oid='memory.v5_2_corrected_packet_admission_proposal_v1'::regclass)
  )")
printf 'CLONE_CONTRACT_REPORT=%s\n' "$contract_report"
jq -e '.table_present and .planner_present and .recorder_present and
  .brains_plan and (.public_plan|not) and .forced_rls' \
  <<<"$contract_report" >/dev/null

plan_packet() {
  local packet_id=$1
  docker exec -i "$container" psql -X -q -A -t \
    -v ON_ERROR_STOP=1 -U sage -d "$clone_db" <<SQL | tr -d '\n'
BEGIN READ ONLY;
SET LOCAL SESSION AUTHORIZATION brains_app;
SET LOCAL app.user_id='$owner';
SELECT memory.plan_owner_v5_2_corrected_packet_admission_v1(
  '$packet_id'::uuid
);
ROLLBACK;
SQL
}

stance_plan=$(plan_packet "$stance_packet")
father_plan=$(plan_packet "$father_packet")
printf '%s\n' "$stance_plan" >"$artifact_dir/stance-plan.json"
printf '%s\n' "$father_plan" >"$artifact_dir/father-plan.json"

jq -e '
  .contract_version=="memory_v1_v5_2_corrected_packet_admission_plan_v1" and
  .counts.entity_reuse_count==1 and
  .counts.entity_manual_count==0 and
  .counts.observation_count==1 and
  .counts.admit_new_count==0 and
  .counts.repair_existing_count==1 and
  .counts.reuse_existing_count==0 and
  .counts.temporal_hold_count==0 and
  (.observation_decisions|length)==1 and
  .observation_decisions[0].observation_ref=="o00" and
  .observation_decisions[0].storage_action=="repair_existing_provenance" and
  .observation_decisions[0].governance_action=="controlled_review_eligible" and
  .observation_decisions[0].prior_observation_id==
    "d3ad7249-5f5d-4835-b2cb-051712269e37"
' <<<"$stance_plan" >/dev/null

jq -e '
  .contract_version=="memory_v1_v5_2_corrected_packet_admission_plan_v1" and
  .counts.entity_reuse_count==1 and
  .counts.entity_manual_count==0 and
  .counts.observation_count==4 and
  .counts.admit_new_count==2 and
  .counts.repair_existing_count==1 and
  .counts.reuse_existing_count==1 and
  .counts.temporal_hold_count==1 and
  ([.observation_decisions[]|select(
    .observation_ref=="o00" and
    .storage_action=="admit_new_observation" and
    .governance_action=="controlled_review_eligible")]|length)==1 and
  ([.observation_decisions[]|select(
    .observation_ref=="o01" and
    .storage_action=="admit_new_observation" and
    .governance_action=="manual_sensitive_review")]|length)==1 and
  ([.observation_decisions[]|select(
    .observation_ref=="o02" and
    .storage_action=="reuse_existing_observation" and
    .governance_action=="manual_sensitive_review")]|length)==1 and
  ([.observation_decisions[]|select(
    .observation_ref=="o03" and
    .storage_action=="repair_existing_provenance" and
    .governance_action=="hold_possible_temporal_update" and
    .conflict_count==1)]|length)==1
' <<<"$father_plan" >/dev/null

stance_sha=$(jq -r '.plan_sha256' <<<"$stance_plan")
father_sha=$(jq -r '.plan_sha256' <<<"$father_plan")
stance_storage=$(jq -r '.packet_storage_sha256' <<<"$stance_plan")
father_storage=$(jq -r '.packet_storage_sha256' <<<"$father_plan")

record_report=$(docker exec -i "$container" psql -X -q -A -t \
  -v ON_ERROR_STOP=1 -U sage -d "$clone_db" <<SQL | sed '/^$/d'
BEGIN;
SET LOCAL SESSION AUTHORIZATION brains_app;
SET LOCAL app.user_id='$owner';
SELECT row_to_json(value) FROM
  memory.record_owner_v5_2_corrected_packet_admission_proposal_v1(
    'c6a6ed79-d429-5ba6-a684-6550201acb9c',
    'be046c5d-ef78-5958-b936-346eec15c6a6',
    '$stance_packet','$stance_storage','$stance_sha'
  ) AS value;
SELECT row_to_json(value) FROM
  memory.record_owner_v5_2_corrected_packet_admission_proposal_v1(
    '7eb090a7-e0b1-5faa-855d-40d08b224643',
    'e21758f0-1d82-593b-b8b5-bda29a1d39b0',
    '$father_packet','$father_storage','$father_sha'
  ) AS value;
COMMIT;
SQL
)
printf '%s\n' "$record_report" >"$artifact_dir/record-report.jsonl"
jq -s -e 'length==2 and all(.[]; .outcome=="applied")' \
  "$artifact_dir/record-report.jsonl" >/dev/null

replay_report=$(docker exec -i "$container" psql -X -q -A -t \
  -v ON_ERROR_STOP=1 -U sage -d "$clone_db" <<SQL | sed '/^$/d'
BEGIN;
SET LOCAL SESSION AUTHORIZATION brains_app;
SET LOCAL app.user_id='$owner';
SELECT row_to_json(value) FROM
  memory.record_owner_v5_2_corrected_packet_admission_proposal_v1(
    'c6a6ed79-d429-5ba6-a684-6550201acb9c',
    'be046c5d-ef78-5958-b936-346eec15c6a6',
    '$stance_packet','$stance_storage','$stance_sha'
  ) AS value;
SELECT row_to_json(value) FROM
  memory.record_owner_v5_2_corrected_packet_admission_proposal_v1(
    '7eb090a7-e0b1-5faa-855d-40d08b224643',
    'e21758f0-1d82-593b-b8b5-bda29a1d39b0',
    '$father_packet','$father_storage','$father_sha'
  ) AS value;
COMMIT;
SQL
)
printf '%s\n' "$replay_report" >"$artifact_dir/replay-report.jsonl"
jq -s -e 'length==2 and all(.[]; .outcome=="replayed")' \
  "$artifact_dir/replay-report.jsonl" >/dev/null
[[ "$(scalar "$clone_db" "SELECT count(*) FROM memory.v5_2_corrected_packet_admission_proposal_v1")" == 2 ]]

if docker exec "$container" psql -X -q -A -t -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" -c "
    BEGIN READ ONLY;
    SET LOCAL SESSION AUTHORIZATION brains_app;
    SET LOCAL app.user_id='$other_owner';
    SELECT memory.plan_owner_v5_2_corrected_packet_admission_v1(
      '$stance_packet'::uuid
    );
    ROLLBACK;
  " >"$artifact_dir/cross-owner.txt" 2>&1; then
  echo 'cross-owner packet plan unexpectedly succeeded' >&2
  exit 1
fi
grep -q 'owner-authoritative corrected packet is unavailable' \
  "$artifact_dir/cross-owner.txt"

protected_after=$(protected_signature "$clone_db")
[[ "$protected_after" == "$protected_before" ]]

docker exec -i "$container" psql -X -U sage -d "$clone_db" \
  -v ON_ERROR_STOP=1 <"$repo_root/$rollback" \
  | tee "$artifact_dir/rollback.txt"
rollback_report=$(scalar "$clone_db" "
  SELECT jsonb_build_object(
    'table_absent',to_regclass(
      'memory.v5_2_corrected_packet_admission_proposal_v1') IS NULL,
    'planner_absent',to_regprocedure(
      'memory.plan_owner_v5_2_corrected_packet_admission_v1(uuid)'
    ) IS NULL,
    'recorder_absent',to_regprocedure(
      'memory.record_owner_v5_2_corrected_packet_admission_proposal_v1(uuid,uuid,uuid,text,text)'
    ) IS NULL
  )")
printf 'CLONE_ROLLBACK_REPORT=%s\n' "$rollback_report"
jq -e '.table_absent and .planner_absent and .recorder_absent' \
  <<<"$rollback_report" >/dev/null
[[ "$(protected_signature "$clone_db")" == "$protected_before" ]]
acl_after_rollback=$(acl_signature "$clone_db")
printf 'CLONE_ACL_SIGNATURE=%s->%s\n' "$acl_before" "$acl_after_rollback"
[[ "$acl_after_rollback" == "$acl_before" ]]

qdrant_after=$(qdrant_signature)
production_after=$(production_activity)
[[ "$qdrant_after" == "$qdrant_before" ]]
[[ "$production_after" == "$production_before" ]]

jq -n \
  --arg contract_version memory_v1_v5_2_corrected_packet_plan_clone_v1 \
  --arg migration_sha256 "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" \
  --arg rollback_sha256 "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" \
  --arg qdrant_sha256 "$qdrant_after" \
  --argjson production_before "$production_before" \
  --argjson production_after "$production_after" \
  '{contract_version:$contract_version,clone_passed:true,
    exact_packet_count:2,entity_reuse_count:2,
    admit_new_count:2,repair_existing_count:2,
    reuse_existing_count:1,temporal_hold_count:1,
    stance_provenance_repair:true,
    father_existing_resolution_reused:true,
    duplicate_entity_creation_blocked:true,
    sensitive_atoms_remain_manual:true,
    cross_owner_isolated:true,append_only_proposals:2,
    zero_write_replay:true,zero_model_calls:true,
    protected_stores_unchanged:true,qdrant_unchanged:true,
    acl_rollback_exact:true,
    production_unchanged:($production_before==$production_after),
    rollback_passed:true,migration_sha256:$migration_sha256,
    rollback_sha256:$rollback_sha256,qdrant_sha256:$qdrant_sha256}' \
  | tee "$artifact_dir/report.json"

printf 'ARTIFACT_DIR=%s\n' "$artifact_dir"

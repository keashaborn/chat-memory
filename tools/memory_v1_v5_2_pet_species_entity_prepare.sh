#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Builds a zero-write, owner-scoped entity review/apply
# manifest and plan for the already staged Max/Neko/Keasha packets.

if [[ "${MEMORY_V1_V5_2_PET_SPECIES_ENTITY_PREPARE:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_PET_SPECIES_ENTITY_PREPARE=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 1 ]]; then
  echo 'usage: memory_v1_v5_2_pet_species_entity_prepare.sh STAGE_WORK_ROOT' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-/opt/chat-memory/.env}"
set +a

python_bin=/opt/chat-memory/venv/bin/python
review_root=/home/ubuntu/memory-v1-reviews
work=$(realpath "$1")
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
neko_entity=09308a2b-3019-4f59-8fc3-bb1fe1408a0d
max_evidence=5e938057-f012-5e54-8b37-148f3cc3ab9c
neko_evidence=ca637d00-7ff6-5147-8f6b-a82386dbc1c1
keasha_evidence=3eb237da-4eec-5fa4-83fb-51360356dba6
runner=scripts/memory_v1_v5_2_entity_resolution_batch.py
authorizer=tests/memory_v1_v5_2_entity_resolution_batch_fixture.py

[[ "$(id -u)" -eq 0 ]]
[[ "$repo_root" == /opt/chat-memory ]]
[[ "$work" == "$review_root"/pet-species-stage-production-* ]]
[[ -d "$work" && "$(stat -c '%a' "$work")" == 700 ]]
[[ -f "$work/report.json" && "$(stat -c '%a' "$work/report.json")" == 600 ]]
[[ "$(jq -er '.contract_version' "$work/report.json")" == \
  memory_v1_v5_2_pet_species_relational_stage_production_report_v1 ]]
[[ "$(jq -er '.owner_user_id' "$work/report.json")" == "$owner" ]]
[[ "$(jq -er '.rows.stage_total' "$work/report.json")" == 44 ]]
[[ -z "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain)" ]]
head=$(git rev-parse HEAD)
[[ "$(jq -er '.head_commit' "$work/report.json")" == "$head" ]]

psql_row() {
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1" | sed -n '1p'
}

resolution_id() {
  local evidence=$1 entity_ref=$2
  psql_row "
    SELECT plan.resolution_id
    FROM memory.entity_resolution_plan AS plan
    JOIN memory.entity_mention AS mention USING(owner_user_id,mention_id)
    WHERE plan.owner_user_id='$owner'::uuid
      AND plan.evidence_id='$evidence'::uuid
      AND plan.predicate_registry_version='memory_predicate_registry_v5_2'
      AND mention.entity_ref='$entity_ref'"
}

max_pet=$(resolution_id "$max_evidence" e00)
max_self=$(resolution_id "$max_evidence" e01)
neko_pet=$(resolution_id "$neko_evidence" e00)
neko_self=$(resolution_id "$neko_evidence" e01)
keasha_pet=$(resolution_id "$keasha_evidence" e00)
keasha_self=$(resolution_id "$keasha_evidence" e01)
for value in "$max_pet" "$max_self" "$neko_pet" "$neko_self" "$keasha_pet" "$keasha_self"; do
  [[ "$value" =~ ^[0-9a-f-]{36}$ ]]
done

expected_state=$(psql_row "
  SELECT string_agg(
    mention.name_text || '|' || plan.action::text || '|' ||
    plan.decision_state::text || '|' ||
    coalesce(plan.selected_entity_id::text,'') || '|' ||
    coalesce(plan.proposed_entity->>'canonical_name',''),
    ';' ORDER BY plan.evidence_id,mention.entity_ref
  )
  FROM memory.entity_resolution_plan AS plan
  JOIN memory.entity_mention AS mention USING(owner_user_id,mention_id)
  WHERE plan.owner_user_id='$owner'::uuid
    AND plan.evidence_id IN (
      '$max_evidence'::uuid,'$neko_evidence'::uuid,'$keasha_evidence'::uuid
    )")
[[ "$expected_state" == *"Max|create_new|manual_review_required||Max"* ]]
[[ "$expected_state" == *"Neko|link_existing|manual_review_required|$neko_entity|"* ]]
[[ "$expected_state" == *"Keasha von Steffen Haus|create_new|manual_review_required||Keasha von Steffen Haus"* ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.entity_resolution_apply AS applied
  JOIN memory.entity_resolution_plan AS plan USING(owner_user_id,resolution_id)
  WHERE plan.owner_user_id='$owner'::uuid
    AND plan.evidence_id IN (
      '$max_evidence'::uuid,'$neko_evidence'::uuid,'$keasha_evidence'::uuid
    )")" == 0 ]]

jq -n \
  --arg owner "$owner" \
  --arg max_pet "$max_pet" --arg max_self "$max_self" \
  --arg neko_pet "$neko_pet" --arg neko_self "$neko_self" \
  --arg keasha_pet "$keasha_pet" --arg keasha_self "$keasha_self" \
  --arg neko_entity "$neko_entity" \
  '{
    contract_version:"memory_v1_v5_2_entity_resolution_batch_manifest_v1",
    target_server:"seebx",owner_user_id:$owner,
    expected_total_bindings:11,expected_new_rows:34,
    items:[
      {resolution_id:$max_pet,operation:"manual_create_new_and_apply",
       expected_action:"create_new",expected_decision_state:"manual_review_required",
       review_reason:"The packet explicitly names Max as the owner'\''s dog and has no owner-scoped animal match; approve creation of the reviewed named animal."},
      {resolution_id:$max_self,operation:"auto_apply",
       expected_action:"link_existing",expected_decision_state:"auto_link_eligible",
       review_reason:null},
      {resolution_id:$neko_pet,operation:"manual_link_existing_and_apply",
       expected_action:"link_existing",expected_decision_state:"manual_review_required",
       expected_entity_id:$neko_entity,
       review_reason:"The packet explicitly names Neko and exactly one active owner-scoped animal entity has canonical name Neko; approve the reviewed link."},
      {resolution_id:$neko_self,operation:"auto_apply",
       expected_action:"link_existing",expected_decision_state:"auto_link_eligible",
       review_reason:null},
      {resolution_id:$keasha_pet,operation:"manual_create_new_and_apply",
       expected_action:"create_new",expected_decision_state:"manual_review_required",
       review_reason:"The packet explicitly names Keasha von Steffen Haus as the owner'\''s dog and has no owner-scoped animal match; approve creation of the reviewed named animal."},
      {resolution_id:$keasha_self,operation:"auto_apply",
       expected_action:"link_existing",expected_decision_state:"auto_link_eligible",
       review_reason:null}
    ]
  }' >"$work/entity-manifest.json"
chmod 0600 "$work/entity-manifest.json"

POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$runner" plan \
  --manifest "$work/entity-manifest.json" \
  --review-root "$review_root" \
  --output "$work/entity-plan.json"
PYTHONPATH="$repo_root" "$python_bin" "$authorizer" \
  --plan "$work/entity-plan.json" \
  --output "$work/entity-authorization.json" \
  --head "$head"

jq --arg owner "$other_owner" '.owner_user_id=$owner' \
  "$work/entity-manifest.json" >"$work/entity-cross-owner-manifest.json"
chmod 0600 "$work/entity-cross-owner-manifest.json"
if POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  "$python_bin" "$runner" plan \
  --manifest "$work/entity-cross-owner-manifest.json" \
  --review-root "$review_root" \
  --output "$work/entity-cross-owner-plan.json" >/dev/null 2>&1; then
  echo 'cross-owner entity plan unexpectedly passed' >&2
  exit 1
fi

printf '%s\n' \
  'MEMORY_V1_V5_2_PET_SPECIES_ENTITY_PREPARE=PASS' \
  "manifest=$work/entity-manifest.json" \
  "manifest_sha256=$(sha256sum "$work/entity-manifest.json" | awk '{print $1}')" \
  "plan=$work/entity-plan.json" \
  "plan_sha256=$(sha256sum "$work/entity-plan.json" | awk '{print $1}')" \
  "authorization=$work/entity-authorization.json" \
  'database_writes=0' \
  'qdrant_writes=0' \
  'cross_owner_plan_rejected=true'

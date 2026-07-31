#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the manual atom-selection compatibility path in
# a disposable production clone, admits and stages only Jerry plus o02/o03,
# reviews the new Jerry entity plan, and stops before entity apply or claims.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for the disposable clone' >&2
  exit 1
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(git -C "$script_dir/.." rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
production=memory
clone="memory_v5_2_jerry_manual_stage_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=c4db1405-ad9d-5c9a-81e3-10ad0200b0ca
evidence=681ab38d-a742-463c-ad26-c74c65eacaa9
prior_role_resolution=fa0e7209-67c4-4cf3-b808-e8d8c2dc35ef
selection_id=6cfb3ef5-7c85-4035-92ae-8b5c8d71ed14
selection_operation_id=a43cbe80-b1d5-4be4-940f-7d9ebed02960
packet_sha=90baad563bd05cdaf93f9d9b72ec5593e86592e4e0f334143a287e5f5e76dc5d
baseline_sha=bfed594b759d942701b51c9275d0d8e7ab6b4c6e529c09af8b6fdb4f19ebd9af
review_sha=f412bb26c8cefea3dd1b100c6a2d6eec7654d637fb4fdda5763263727fd50748
migration=ops/sql/20260731_memory_v1_v5_2_manual_atom_selection_v1.sql
rollback=ops/sql/20260731_memory_v1_v5_2_manual_atom_selection_v1_rollback.sql
security_test=tests/memory_v1_v5_2_manual_atom_selection_v1.sql
atom_spec=manifests/memory_v1_v5_2_jerry_manual_atom_admission_spec_20260731.json
entity_spec=manifests/memory_v1_v5_2_jerry_entity_review_spec_20260731.json
atom_manifest_runner=scripts/memory_v1_v5_2_atom_admission_manifest_batch.py
atom_apply_runner=scripts/memory_v1_v5_2_atom_admission_apply_v2.py
bundle_builder=scripts/memory_v1_v5_2_atom_stage_bundle_v2.py
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
stage_fixture=tests/memory_v1_v5_2_stage_batch_fixture.py
entity_review_runner=scripts/memory_v1_v5_2_entity_resolution_review_batch.py
review_root=/home/ubuntu/memory-v1-reviews
work=$(runuser -u ubuntu -- mktemp -d "$review_root/jerry-manual-stage-clone.XXXXXX")
backup=$(mktemp /tmp/memory-v5-2-jerry-manual-stage.XXXXXX.dump)
chmod 0600 "$backup"
chmod 0700 "$work"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  rm -rf "$work"
}
trap cleanup EXIT

scalar() {
  local database=$1 query=$2
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$query" | tr -d '[:space:]'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for file in "$migration" "$rollback" "$security_test" "$atom_spec" \
  "$entity_spec" "$atom_manifest_runner" "$atom_apply_runner" \
  "$bundle_builder" "$stage_runner" "$stage_fixture" \
  "$entity_review_runner"; do
  [[ -f "$file" ]]
done
bash -n "$0"
git diff --check
[[ -z "$(git status --short)" ]]

review_report=/home/ubuntu/brains/reviews/memory-v11-jerry-manual-review-v2/review-c2a6a11.json
[[ "$(sha256sum "$review_report" | awk '{print $1}')" == "$review_sha" ]]
jq -e --arg packet "$packet" --arg evidence "$evidence" '
  .contract_version=="memory_v1_v5_2_manual_packet_review_report_v2" and
  .packet_id==$packet and .evidence_id==$evidence and
  .review.disposition=="approve_restricted_partial_stage" and
  .review.approved_observation_refs==["o02","o03"] and
  .review.care_setting_deferred==true
' "$review_report" >/dev/null

production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)
production_status_before=$(git -C /opt/chat-memory status --porcelain)
production_claims_before=$(scalar "$production" 'SELECT count(*) FROM memory.claim')
production_entities_before=$(scalar "$production" 'SELECT count(*) FROM memory.entity')
production_stage_before=$(scalar "$production" 'SELECT count(*) FROM memory.relational_stage_batch')
production_target_before=$(scalar "$production" "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid")
qdrant_before=$(qdrant_signature)
[[ "$production_status_before" == '' ]]
[[ "$production_target_before" == 0 ]]

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" <"$backup"

set -a
source /opt/chat-memory/.env
set +a
clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone" python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit
value = urlsplit(os.environ['SOURCE_DSN'])
print(urlunsplit((value.scheme, value.netloc, '/' + os.environ['CLONE_DB'], value.query, value.fragment)))
PY
)

original_planner_sha=$(scalar "$clone" "
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.plan_owner_v5_2_atom_admission_v2(uuid)'::regprocedure
  ),'UTF8'),'sha256'),'hex')")
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration"
psql "$clone_dsn" -X -v ON_ERROR_STOP=1 \
  -f "$security_test" >/dev/null
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$rollback"
[[ "$(scalar "$clone" "
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.plan_owner_v5_2_atom_admission_v2(uuid)'::regprocedure
  ),'UTF8'),'sha256'),'hex')")" == "$original_planner_sha" ]]
[[ "$(scalar "$clone" "SELECT to_regclass('memory.v5_2_manual_atom_selection_v1') IS NULL")" == t ]]
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration"

selection_manifest=$(PGCONNECT_TIMEOUT=10 psql "$clone_dsn" -X -A -t \
  -v ON_ERROR_STOP=1 -c "
    SELECT memory.v5_2_manual_atom_selection_manifest_sha_v1(
      '$owner'::uuid,'$selection_id'::uuid,'$packet'::uuid,'$evidence'::uuid,
      '$packet_sha','$baseline_sha','$review_sha',
      '[\"e00\"]'::jsonb,'[\"o02\",\"o03\"]'::jsonb
    )" | tr -d '[:space:]')
PGCONNECT_TIMEOUT=10 psql "$clone_dsn" -X -v ON_ERROR_STOP=1 <<SQL >/dev/null
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT * FROM memory.register_owner_v5_2_manual_atom_selection_v1(
  '$selection_id'::uuid,'$selection_operation_id'::uuid,'$packet'::uuid,
  '$packet_sha','$baseline_sha','$review_sha',
  '["e00"]'::jsonb,'["o02","o03"]'::jsonb,'$selection_manifest'
);
COMMIT;
SQL
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.v5_2_manual_atom_selection_v1
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")" == 1 ]]

runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$atom_manifest_runner" --spec "$repo_root/$atom_spec" \
  --output "$work/atom-manifest.json" >/dev/null
runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$atom_apply_runner" --mode preflight \
  --manifest "$work/atom-manifest.json" --output "$work/atom-preflight.json" >/dev/null
runuser -u ubuntu -- env MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY_V2=authorized \
  POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$repo_root/$atom_apply_runner" --mode apply \
  --manifest "$work/atom-manifest.json" --output "$work/atom-apply.json" >/dev/null
runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$atom_apply_runner" --mode replay \
  --manifest "$work/atom-manifest.json" --output "$work/atom-replay.json" >/dev/null
jq -e '.persistent_writes==6' "$work/atom-apply.json" >/dev/null
jq -e '.persistent_writes==0' "$work/atom-replay.json" >/dev/null

apply_id=$(jq -r '.items[0].apply_id' "$work/atom-manifest.json")
proposal_id=$(jq -r '.items[0].proposal_id' "$work/atom-manifest.json")
review_id=$(jq -r '.items[0].review_id' "$work/atom-manifest.json")
stage_plan=$(PGCONNECT_TIMEOUT=10 psql "$clone_dsn" -X -q -A -t \
  -v ON_ERROR_STOP=1 -c "BEGIN; SET LOCAL app.user_id='$owner';
    SELECT memory.plan_owner_v5_2_atom_stage_v2('$apply_id'::uuid); COMMIT;")
install -d -o ubuntu -g ubuntu -m 0700 "$work/stage"
jq -n --arg owner "$owner" --arg packet "$packet" --arg evidence "$evidence" \
  --arg apply "$apply_id" --arg proposal "$proposal_id" --arg review "$review_id" \
  --argjson plan "$stage_plan" '
  {
    contract_version:"memory_v1_v5_2_atom_stage_build_manifest_v2",
    target_server:"seebx",owner_user_id:$owner,
    case_id:"compiler_v11_jerry_restricted_health_stage",
    apply_id:$apply,proposal_id:$proposal,review_id:$review,
    packet_id:$packet,evidence_id:$evidence,
    apply_manifest_sha256:$plan.apply_manifest_sha256,
    proposal_sha256:$plan.proposal_sha256,
    stage_projection_sha256:$plan.stage_projection_sha256,
    expected_counts:$plan.counts,
    expected_resolutions:{
      e00:{
        action:"create_new",decision_state:"manual_review_required",
        selected_entity_id:null,
        proposed_entity:{entity_type:"person",display_label:"Jerry",
          canonical_name:"Jerry",identity_state:"named",
          creation_reason:"new_named_entity_no_exact_owner_match"},
        review_reason_codes:["new_named_entity_requires_review"]
      }
    }
  }' >"$work/stage-build-manifest.json"
chmod 0600 "$work/stage-build-manifest.json"
chown ubuntu:ubuntu "$work/stage-build-manifest.json"

runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python "$repo_root/$bundle_builder" \
  build --manifest "$work/stage-build-manifest.json" --output-root "$work/stage" >/dev/null
runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python "$repo_root/$bundle_builder" \
  probe --bundle "$work/stage/bundle.json" --apply-id "$apply_id" \
  --other-owner-user-id "$other" >/dev/null
runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" \
  plan --manifest "$work/stage/stage-manifest.json" --review-root "$work" \
  --output "$work/stage-plan.json" >/dev/null
runuser -u ubuntu -- /opt/chat-memory/venv/bin/python "$repo_root/$stage_fixture" \
  authorize --plan "$work/stage-plan.json" --output "$work/stage-authorization.json" \
  --head "$(git rev-parse HEAD)" >/dev/null
runuser -u ubuntu -- env MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized \
  POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" apply \
  --plan "$work/stage-plan.json" --authorization "$work/stage-authorization.json" \
  --review-root "$work" --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
  --output "$work/stage-apply.json" >/dev/null
jq -e '.database_rows_created==8 and .checks.replay_rows_written==0' \
  "$work/stage-apply.json" >/dev/null

runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python "$repo_root/$entity_review_runner" \
  manifest --spec "$repo_root/$entity_spec" --output "$work/entity-manifest.json" >/dev/null
runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python "$repo_root/$entity_review_runner" \
  plan --manifest "$work/entity-manifest.json" --review-root "$work" \
  --output "$work/entity-plan.json" >/dev/null
entity_plan_sha=$(sha256sum "$work/entity-plan.json" | awk '{print $1}')
authorized_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
expires_at=$(date -u -d '+20 minutes' +%Y-%m-%dT%H:%M:%SZ)
jq -n --arg authorization_id "a550e170-92bd-4a51-962e-9054c0d0eaff" \
  --arg authorized_at "$authorized_at" --arg expires_at "$expires_at" \
  --arg head "$(git rev-parse HEAD)" --arg owner "$owner" \
  --arg plan_sha "$entity_plan_sha" '
  {contract_version:"memory_v1_v5_2_entity_review_authorization_v1",
   authorization_id:$authorization_id,authorized:true,authorized_by:"Eric Lund",
   authorized_at:$authorized_at,expires_at:$expires_at,expected_head_commit:$head,
   target_server:"seebx",scope:"review_owner_v5_2_entity_resolutions_without_apply",
   owner_user_id:$owner,plan_sha256:$plan_sha,expected_item_count:1,
   expected_new_rows:2,confirmation:"REVIEW_OWNER_V5_2_ENTITY_RESOLUTIONS_WITHOUT_APPLY"}' \
  >"$work/entity-authorization.json"
chmod 0600 "$work/entity-authorization.json"
chown ubuntu:ubuntu "$work/entity-authorization.json"
runuser -u ubuntu -- env MEMORY_V1_V5_2_ENTITY_REVIEW_ONLY_APPLY=authorized \
  POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$repo_root/$entity_review_runner" apply \
  --plan "$work/entity-plan.json" --authorization "$work/entity-authorization.json" \
  --review-root "$work" --confirm REVIEW_OWNER_V5_2_ENTITY_RESOLUTIONS_WITHOUT_APPLY \
  --output "$work/entity-apply.json" >/dev/null
jq -e '.new_rows==2 and .zero_write_replay==true and .entity_apply_calls==0' \
  "$work/entity-apply.json" >/dev/null

[[ "$(scalar "$clone" "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.relational_stage_batch WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_mention WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_resolution_plan WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_resolution_candidate c JOIN memory.entity_resolution_plan p USING(owner_user_id,resolution_id) WHERE p.owner_user_id='$owner'::uuid AND p.evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.observation WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.observation_temporal t JOIN memory.observation o USING(owner_user_id,observation_id) WHERE o.owner_user_id='$owner'::uuid AND o.evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_resolution_review r JOIN memory.entity_resolution_plan p USING(owner_user_id,resolution_id) WHERE p.owner_user_id='$owner'::uuid AND p.evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_resolution_apply a JOIN memory.entity_resolution_plan p USING(owner_user_id,resolution_id) WHERE p.owner_user_id='$owner'::uuid AND p.evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.observation_entity_binding b JOIN memory.observation o USING(owner_user_id,observation_id) WHERE o.owner_user_id='$owner'::uuid AND o.evidence_id='$evidence'::uuid)
  )")" == '1,1,1,0,2,2,1,0,0' ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.entity_resolution_plan
  WHERE owner_user_id='$owner'::uuid AND resolution_id='$prior_role_resolution'::uuid
    AND action='defer' AND decision_state='deferred'")" == 1 ]]
[[ "$(scalar "$clone" "SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid AND lower(coalesce(canonical_name,''))='jerry'")" == 0 ]]
[[ "$(scalar "$clone" "SELECT count(*) FROM memory.claim WHERE owner_user_id='$owner'::uuid")" == "$(scalar "$production" "SELECT count(*) FROM memory.claim WHERE owner_user_id='$owner'::uuid")" ]]
[[ "$(scalar "$clone" "SELECT count(*) FROM memory.observation_entity_binding WHERE owner_user_id='$owner'::uuid")" == "$(scalar "$production" "SELECT count(*) FROM memory.observation_entity_binding WHERE owner_user_id='$owner'::uuid")" ]]

[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]
[[ "$(git -C /opt/chat-memory status --porcelain)" == "$production_status_before" ]]
[[ "$(scalar "$production" 'SELECT count(*) FROM memory.claim')" == "$production_claims_before" ]]
[[ "$(scalar "$production" 'SELECT count(*) FROM memory.entity')" == "$production_entities_before" ]]
[[ "$(scalar "$production" 'SELECT count(*) FROM memory.relational_stage_batch')" == "$production_stage_before" ]]
[[ "$(scalar "$production" "SELECT count(*) FROM memory.relational_stage_batch WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid")" == "$production_target_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

jq -n --arg head "$(git rev-parse HEAD)" --arg owner "$owner" \
  --arg packet "$packet" --arg evidence "$evidence" \
  --arg migration_sha "$(sha256sum "$migration" | awk '{print $1}')" \
  --arg rollback_sha "$(sha256sum "$rollback" | awk '{print $1}')" '
  {contract_version:"memory_v1_v5_2_jerry_manual_stage_clone_report_v1",
   repository_commit:$head,owner_user_id:$owner,packet_id:$packet,evidence_id:$evidence,
   migration_sha256:$migration_sha,rollback_sha256:$rollback_sha,
   manual_selection_rows:1,atom_admission_rows:6,relational_stage_rows:8,
   entity_review_rows:2,staged_entity_mentions:1,staged_observations:2,
   entity_apply_rows:0,observation_binding_rows:0,claim_rows:0,qdrant_writes:0,
   model_calls:0,cross_owner_rejection:true,zero_write_replay:true,
   rollback_verified:true,production_unchanged:true,
   hard_stop:"before_entity_apply_observation_binding_claims_projection_or_retrieval"}' \
  >"$work/final-report.json"
cat "$work/final-report.json"
printf 'memory_v1_v5_2_jerry_manual_stage_clone: PASS\n'

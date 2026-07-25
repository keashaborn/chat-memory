#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable clone, installs the
# additive reinforcement API, stages and reviews exactly one Neko correction,
# then links it as supporting evidence for the existing claim. Production and
# Qdrant are read-only.

if [[ "$#" -ne 1 ]]; then
  echo 'usage: ..._reinforcement_clone.sh ARTIFACT_DIR' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
artifact_dir=$(realpath -m "$1")
review_root=/home/ubuntu/memory-v1-reviews
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
observation=5261da41-f863-42cd-8e3f-6e947f9743f2
claim=8e3f4d82-8c21-4bbd-bbe8-91dd585f6fc9
stage_runner=scripts/memory_v1_v5_2_neko_correction_reinforcement_stage.py
review_manifest_runner=scripts/memory_v1_v5_2_neko_correction_reinforcement_review_manifest.py
review_runner=scripts/memory_v1_v5_2_neko_correction_reinforcement_review_batch.py
apply_manifest_runner=scripts/memory_v1_v5_2_neko_correction_reinforcement_apply_manifest.py
apply_runner=scripts/memory_v1_v5_2_neko_correction_reinforcement_apply_batch.py
migration=ops/sql/20260725_memory_v1_v5_2_projection_reinforcement.sql
security_test=tests/memory_v1_v5_2_projection_reinforcement_security.sql
port=${MEMORY_V1_V5_2_REINFORCEMENT_FULL_CLONE_PORT:-55500}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52reinforcementfullclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
backup=$(mktemp /tmp/memory-v1-v5-2-reinforcement.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-reinforcement-roles.XXXXXX.sql)
source_backup=${MEMORY_V1_V5_2_REINFORCEMENT_SOURCE_BACKUP:-}
reset_applied_clone=${MEMORY_V1_V5_2_REINFORCEMENT_RESET_APPLIED_CLONE:-}
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
head=$(git -C "$repo_root" rev-parse HEAD)

manifest="$artifact_dir/stage-manifest.json"
authorization="$artifact_dir/stage-authorization.json"
cross_result="$artifact_dir/stage-cross-owner.json"
stage_apply="$artifact_dir/stage-apply.json"
stage_replay="$artifact_dir/stage-replay.json"
stage_post_apply_replay="$artifact_dir/stage-post-apply-replay.json"
decisions="$artifact_dir/review-decisions.json"
review_manifest="$artifact_dir/review-manifest.json"
review_preflight="$artifact_dir/review-preflight.json"
review_apply="$artifact_dir/review-apply.json"
review_replay="$artifact_dir/review-replay.json"
apply_manifest="$artifact_dir/apply-manifest.json"
apply_preflight="$artifact_dir/apply-preflight.json"
apply_result="$artifact_dir/apply-result.json"
apply_replay="$artifact_dir/apply-replay.json"
report="$artifact_dir/clone-report.json"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$role_sql"
}
trap cleanup EXIT

[[ "$artifact_dir" == "$review_root"/* ]]
[[ ! -e "$artifact_dir" ]]
mkdir -m 0700 "$artifact_dir"
chmod 0600 "$backup" "$role_sql"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

production_scalar() {
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

production_signature() {
  production_scalar "
    SELECT md5(jsonb_build_object(
      'requests',(SELECT count(*) FROM memory.relational_operation_request),
      'entailments',(SELECT count(*) FROM memory.observation_entailment_v5),
      'plans',(SELECT count(*) FROM memory.projection_plan),
      'items',(SELECT count(*) FROM memory.projection_plan_item),
      'payloads',(SELECT count(*) FROM memory.projection_claim_payload),
      'links',(SELECT count(*) FROM memory.projection_plan_observation),
      'reviews',(SELECT count(*) FROM memory.projection_review),
      'apply_events',(SELECT count(*) FROM memory.projection_apply_event),
      'dispatches',(SELECT count(*) FROM memory.projection_dispatch_v5),
      'claims',(SELECT count(*) FROM memory.claim),
      'revisions',(SELECT count(*) FROM memory.claim_revision),
      'claim_links',(SELECT count(*) FROM memory.claim_observation)
    )::text)"
}

qdrant_before=$(qdrant_signature)
production_before=$(production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)

if [[ -n "$source_backup" ]]; then
  source_backup=$(realpath "$source_backup")
  [[ "$source_backup" == /home/ubuntu/brains/snapshots/* ]]
  [[ -s "$source_backup" ]]
  [[ -f "$source_backup.sha256" ]]
  (
    cd "$(dirname "$source_backup")"
    sha256sum --check "$(basename "$source_backup").sha256" >/dev/null
  )
  cp "$source_backup" "$backup"
else
  docker exec brains-postgres-1 pg_dump -U sage -d memory -Fc >"$backup"
fi
[[ -s "$backup" ]]
docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c "
  SELECT format(
    'CREATE ROLE %I %s %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;',
    rolname,
    CASE WHEN rolcanlogin THEN 'LOGIN' ELSE 'NOLOGIN' END,
    CASE WHEN rolinherit THEN 'INHERIT' ELSE 'NOINHERIT' END
  )
  FROM pg_roles
  WHERE rolname NOT LIKE 'pg\\_%' ESCAPE '\\'
    AND rolname NOT IN ('sage','postgres')
  ORDER BY rolname" >"$role_sql"
[[ -s "$role_sql" ]]

"${compose[@]}" up -d --wait postgres
run_sql <"$role_sql"
printf '%s\n' "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists <"$backup"
if [[ "$reset_applied_clone" == 1 ]]; then
  run_sql <<'SQL'
DO $verify$
DECLARE
  owner_id constant uuid := '1240822d-ac9a-4096-95aa-e2b24d36ef50';
  observation_id constant uuid := '5261da41-f863-42cd-8e3f-6e947f9743f2';
  target_claim_id constant uuid := '8e3f4d82-8c21-4bbd-bbe8-91dd585f6fc9';
  plan_id_value uuid;
BEGIN
  SELECT item.plan_id INTO STRICT plan_id_value
  FROM memory.projection_plan_item AS item
  JOIN memory.projection_plan_observation AS link
    USING(owner_user_id,plan_id,projection_ref)
  WHERE item.owner_user_id=owner_id
    AND link.observation_id=observation_id
    AND item.target_action='reinforce';
  IF plan_id_value<>'b036d5a9-a39d-5b51-8f6c-5c91d778fe3b'
     OR (SELECT count(*) FROM memory.claim_observation AS claim_link
         WHERE claim_link.owner_user_id=owner_id
           AND claim_link.claim_id=target_claim_id
           AND claim_link.observation_id=observation_id)<>1
     OR (SELECT count(*) FROM memory.projection_review AS review
         WHERE review.owner_user_id=owner_id
           AND review.plan_id=plan_id_value)<>1
     OR (SELECT count(*) FROM memory.projection_apply_event AS event
         WHERE event.owner_user_id=owner_id
           AND event.plan_id=plan_id_value)<>1
     OR (SELECT count(*) FROM memory.projection_dispatch_v5 AS dispatch
         JOIN memory.projection_apply_event AS event
           ON event.owner_user_id=dispatch.owner_user_id
          AND event.event_id=dispatch.apply_event_id
         WHERE event.owner_user_id=owner_id
           AND event.plan_id=plan_id_value)<>1
     OR (SELECT count(*) FROM memory.observation_entailment_v5 AS entailment
         WHERE entailment.owner_user_id=owner_id
           AND entailment.observation_id=observation_id)<>1
     OR (SELECT count(*) FROM memory.relational_operation_request AS request
         WHERE request.owner_user_id=owner_id
           AND request.operation='record_observation_entailment_v5'
           AND request.target_key=observation_id::text)<>1 THEN
    RAISE EXCEPTION 'applied clone reset boundary is not exact';
  END IF;
END
$verify$;

ALTER TABLE memory.projection_dispatch_v5 DISABLE TRIGGER USER;
ALTER TABLE memory.projection_apply_event DISABLE TRIGGER USER;
ALTER TABLE memory.claim_observation DISABLE TRIGGER USER;
ALTER TABLE memory.projection_review DISABLE TRIGGER USER;
ALTER TABLE memory.projection_plan_observation DISABLE TRIGGER USER;
ALTER TABLE memory.projection_claim_payload DISABLE TRIGGER USER;
ALTER TABLE memory.projection_plan_item DISABLE TRIGGER USER;
ALTER TABLE memory.projection_plan DISABLE TRIGGER USER;
ALTER TABLE memory.observation_entailment_v5 DISABLE TRIGGER USER;
ALTER TABLE memory.relational_operation_request DISABLE TRIGGER USER;

DELETE FROM memory.projection_dispatch_v5 AS dispatch
USING memory.projection_apply_event AS event,
      memory.projection_plan_observation AS link
WHERE dispatch.owner_user_id=event.owner_user_id
  AND dispatch.apply_event_id=event.event_id
  AND link.owner_user_id=event.owner_user_id
  AND link.plan_id=event.plan_id
  AND link.projection_ref=event.projection_ref
  AND event.owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
  AND link.observation_id='5261da41-f863-42cd-8e3f-6e947f9743f2';
DELETE FROM memory.projection_apply_event AS event
USING memory.projection_plan_observation AS link
WHERE link.owner_user_id=event.owner_user_id
  AND link.plan_id=event.plan_id
  AND link.projection_ref=event.projection_ref
  AND event.owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
  AND link.observation_id='5261da41-f863-42cd-8e3f-6e947f9743f2';
DELETE FROM memory.claim_observation
WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
  AND claim_id='8e3f4d82-8c21-4bbd-bbe8-91dd585f6fc9'
  AND observation_id='5261da41-f863-42cd-8e3f-6e947f9743f2';
DELETE FROM memory.projection_review AS review
USING memory.projection_plan_observation AS link
WHERE link.owner_user_id=review.owner_user_id
  AND link.plan_id=review.plan_id
  AND link.projection_ref=review.projection_ref
  AND review.owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
  AND link.observation_id='5261da41-f863-42cd-8e3f-6e947f9743f2';
DELETE FROM memory.projection_plan_observation
WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
  AND observation_id='5261da41-f863-42cd-8e3f-6e947f9743f2';
DELETE FROM memory.projection_claim_payload
WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
  AND plan_id='b036d5a9-a39d-5b51-8f6c-5c91d778fe3b';
DELETE FROM memory.projection_plan_item
WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
  AND plan_id='b036d5a9-a39d-5b51-8f6c-5c91d778fe3b';
DELETE FROM memory.projection_plan
WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
  AND plan_id='b036d5a9-a39d-5b51-8f6c-5c91d778fe3b';
DELETE FROM memory.observation_entailment_v5
WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
  AND observation_id='5261da41-f863-42cd-8e3f-6e947f9743f2';
DELETE FROM memory.relational_operation_request
WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
  AND operation='record_observation_entailment_v5'
  AND target_key='5261da41-f863-42cd-8e3f-6e947f9743f2';

ALTER TABLE memory.projection_dispatch_v5 ENABLE TRIGGER USER;
ALTER TABLE memory.projection_apply_event ENABLE TRIGGER USER;
ALTER TABLE memory.claim_observation ENABLE TRIGGER USER;
ALTER TABLE memory.projection_review ENABLE TRIGGER USER;
ALTER TABLE memory.projection_plan_observation ENABLE TRIGGER USER;
ALTER TABLE memory.projection_claim_payload ENABLE TRIGGER USER;
ALTER TABLE memory.projection_plan_item ENABLE TRIGGER USER;
ALTER TABLE memory.projection_plan ENABLE TRIGGER USER;
ALTER TABLE memory.observation_entailment_v5 ENABLE TRIGGER USER;
ALTER TABLE memory.relational_operation_request ENABLE TRIGGER USER;
SQL
fi
run_sql <"$migration"
run_sql <"$migration"
run_sql <"$security_test"

PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python \
  -m unittest "$repo_root/tests/test_memory_v1_v5_2_projection_dispatch.py"

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" manifest \
  --owner "$target_owner" --observation "$observation" \
  --required-head "$head" --output "$manifest"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$stage_runner" authorize \
  --manifest "$manifest" --output "$authorization"

before_requests=$(scalar "SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id='$target_owner'")
before_entailments=$(scalar "SELECT count(*) FROM memory.observation_entailment_v5 WHERE owner_user_id='$target_owner'")
before_plans=$(scalar "SELECT count(*) FROM memory.projection_plan WHERE owner_user_id='$target_owner'")
before_items=$(scalar "SELECT count(*) FROM memory.projection_plan_item WHERE owner_user_id='$target_owner'")
before_payloads=$(scalar "SELECT count(*) FROM memory.projection_claim_payload WHERE owner_user_id='$target_owner'")
before_links=$(scalar "SELECT count(*) FROM memory.projection_plan_observation WHERE owner_user_id='$target_owner'")
before_reviews=$(scalar "SELECT count(*) FROM memory.projection_review WHERE owner_user_id='$target_owner'")
before_apply_events=$(scalar "SELECT count(*) FROM memory.projection_apply_event WHERE owner_user_id='$target_owner'")
before_dispatches=$(scalar "SELECT count(*) FROM memory.projection_dispatch_v5 WHERE owner_user_id='$target_owner'")
before_claims=$(scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner'")
before_revisions=$(scalar "SELECT count(*) FROM memory.claim_revision WHERE owner_user_id='$target_owner'")
before_claim_links=$(scalar "SELECT count(*) FROM memory.claim_observation WHERE owner_user_id='$target_owner'")

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" cross-owner \
  --manifest "$manifest" --other-owner "$other_owner" --output "$cross_result"
[[ "$(jq -er '.cross_owner_rejected' "$cross_result")" == true ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_NEKO_CORRECTION_REINFORCEMENT_STAGE_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" apply \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_EXACT_NEKO_CORRECTION_REINFORCEMENT_ONLY \
  --output "$stage_apply"
[[ "$(jq -er '.rows_written' "$stage_apply")" == 6 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_NEKO_CORRECTION_REINFORCEMENT_STAGE_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" replay \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_EXACT_NEKO_CORRECTION_REINFORCEMENT_ONLY \
  --output "$stage_replay"
[[ "$(jq -er '.rows_written' "$stage_replay")" == 0 ]]

MANIFEST="$manifest" OUTPUT="$decisions" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
from scripts.memory_v1_projection_v5_contract_test import sha256

stage = json.loads(Path(os.environ["MANIFEST"]).read_text())
if len(stage["items"]) != 1:
    raise SystemExit("exactly one reinforcement candidate is required")
item = stage["items"][0]
projection = item["packet"]["projections"][0]
if (
    item["observation_id"] != "5261da41-f863-42cd-8e3f-6e947f9743f2"
    or item["predicate"] != "identity.name_canonical"
    or item["canonical_text"] != "Neko's canonical name is Neko."
    or projection["target"]["action"] != "reinforce"
    or projection["target"]["aggregate_id"]
       != "8e3f4d82-8c21-4bbd-bbe8-91dd585f6fc9"
    or projection["target"]["expected_revision_number"] != 2
):
    raise SystemExit("reinforcement candidate semantics drifted")
value = {
    "contract_version":
        "memory_v1_v5_2_neko_correction_reinforcement_review_decisions_v1",
    "owner_user_id": stage["owner_user_id"],
    "evidence_ids": stage["evidence_ids"],
    "decisions": [{
        "observation_id": item["observation_id"],
        "decision": "authorized",
        "reason": (
            "Reviewed corrective evidence for the existing Neko canonical-name "
            "claim; link the immutable observation without revising claim text."
        ),
        "reason_codes": [
            "correction_target_reconciled",
            "existing_supported_claim_exact_match",
            "additional_supporting_observation",
        ],
    }],
}
value["decisions_sha256"] = sha256(value)
path = Path(os.environ["OUTPUT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_manifest_runner" \
  --owner "$target_owner" --required-head "$head" \
  --stage-manifest "$manifest" --decisions "$decisions" \
  --output "$review_manifest"

MEMORY_V1_REQUIRED_HEAD="$head" \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode preflight --manifest "$review_manifest" --output "$review_preflight"
[[ "$(jq -er '.rows_written' "$review_preflight")" == 0 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_NEKO_CORRECTION_REINFORCEMENT_REVIEW_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode apply --manifest "$review_manifest" --output "$review_apply"
[[ "$(jq -er '.rows_written' "$review_apply")" == 1 ]]
[[ "$(jq -er '.decision_counts.authorized' "$review_apply")" == 1 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_NEKO_CORRECTION_REINFORCEMENT_REVIEW_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode replay --manifest "$review_manifest" --output "$review_replay"
[[ "$(jq -er '.rows_written' "$review_replay")" == 0 ]]

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$apply_manifest_runner" \
  --owner "$target_owner" --required-head "$head" \
  --review-manifest "$review_manifest" --review-result "$review_apply" \
  --output "$apply_manifest"

MEMORY_V1_REQUIRED_HEAD="$head" \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$apply_runner" \
  --mode preflight --manifest "$apply_manifest" --output "$apply_preflight"
[[ "$(jq -er '.rows_written' "$apply_preflight")" == 0 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_NEKO_CORRECTION_REINFORCEMENT_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$apply_runner" \
  --mode apply --manifest "$apply_manifest" --output "$apply_result"
[[ "$(jq -er '.rows_written' "$apply_result")" == 3 ]]
[[ "$(jq -er '.claim_observation_links_written' "$apply_result")" == 1 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_NEKO_CORRECTION_REINFORCEMENT_STAGE_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" replay \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_EXACT_NEKO_CORRECTION_REINFORCEMENT_ONLY \
  --output "$stage_post_apply_replay"
[[ "$(jq -er '.rows_written' "$stage_post_apply_replay")" == 0 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$apply_runner" \
  --mode replay --manifest "$apply_manifest" \
  --apply-result "$apply_result" --output "$apply_replay"
[[ "$(jq -er '.rows_written' "$apply_replay")" == 0 ]]

[[ "$(scalar "SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id='$target_owner'")" == "$((before_requests + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.observation_entailment_v5 WHERE owner_user_id='$target_owner'")" == "$((before_entailments + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan WHERE owner_user_id='$target_owner'")" == "$((before_plans + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan_item WHERE owner_user_id='$target_owner'")" == "$((before_items + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_claim_payload WHERE owner_user_id='$target_owner'")" == "$((before_payloads + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan_observation WHERE owner_user_id='$target_owner'")" == "$((before_links + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_review WHERE owner_user_id='$target_owner'")" == "$((before_reviews + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_apply_event WHERE owner_user_id='$target_owner'")" == "$((before_apply_events + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_dispatch_v5 WHERE owner_user_id='$target_owner'")" == "$((before_dispatches + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner'")" == "$before_claims" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim_revision WHERE owner_user_id='$target_owner'")" == "$before_revisions" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim_observation WHERE owner_user_id='$target_owner'")" == "$((before_claim_links + 1))" ]]

[[ "$(scalar "SELECT count(*) FROM memory.projection_plan_item i JOIN memory.projection_claim_payload p USING(owner_user_id,plan_id,projection_ref) JOIN memory.projection_plan_observation l USING(owner_user_id,plan_id,projection_ref) WHERE i.owner_user_id='$target_owner' AND l.observation_id='$observation'::uuid AND i.target_action='reinforce' AND i.expected_revision_number=2 AND p.target_claim_id='$claim'::uuid AND p.claim_class='direct_claim' AND p.canonical_text='Neko''s canonical name is Neko.' AND p.surface_policy='direct_or_relevant' AND i.target_reason_codes='[\"additional_supporting_observation\",\"canonical_name_correction_normalized\"]'::jsonb")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim_observation WHERE owner_user_id='$target_owner' AND claim_id='$claim'::uuid AND observation_id='$observation'::uuid AND stance='supports'")" == 1 ]]

[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(production_signature)" == "$production_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]

REPORT="$report" HEAD="$head" MANIFEST="$manifest" \
REVIEW="$review_manifest" APPLY="$apply_manifest" QDRANT="$qdrant_before" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

stage = json.loads(Path(os.environ["MANIFEST"]).read_text())
review = json.loads(Path(os.environ["REVIEW"]).read_text())
apply = json.loads(Path(os.environ["APPLY"]).read_text())
value = {
    "contract_version":
        "memory_v1_v5_2_neko_correction_reinforcement_full_clone_report_v1",
    "completed_at":
        dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00","Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": stage["owner_user_id"],
    "stage_manifest_sha256": stage["manifest_sha256"],
    "review_manifest_sha256": review["manifest_sha256"],
    "apply_manifest_sha256": apply["manifest_sha256"],
    "target_claim_id": "8e3f4d82-8c21-4bbd-bbe8-91dd585f6fc9",
    "target_revision_number": 2,
    "verification": {
        "stage_rows_written": 6,
        "stage_replay_zero_write": True,
        "stage_post_apply_replay_zero_write": True,
        "review_rows_written": 1,
        "review_replay_zero_write": True,
        "apply_rows_written": 3,
        "apply_replay_zero_write": True,
        "cross_owner_rejected": True,
        "claims_written": 0,
        "claim_revisions_written": 0,
        "claim_observation_links_written": 1,
        "projection_apply_events_written": 1,
        "projection_dispatch_rows_written": 1,
        "projection_outbox_rows_written": 0,
        "production_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    },
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(value,indent=2,sort_keys=True)+"\n")
path.chmod(0o600)
PY

printf 'report=%s\n' "$report"
printf 'memory_v1_v5_2_neko_correction_reinforcement_full_clone: PASS\n'

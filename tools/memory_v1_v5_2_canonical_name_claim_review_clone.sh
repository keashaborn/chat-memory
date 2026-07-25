#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable clone, installs the
# narrow canonical-name source compatibility migration, stages exactly one
# Neko canonical-name claim candidate, records one review, proves
# replay/isolation, then removes the clone. Production data and Qdrant remain
# read-only.

if [[ "$#" -ne 1 ]]; then
  echo 'usage: ..._claim_review_clone.sh ARTIFACT_DIR' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
artifact_dir=$(realpath -m "$1")
review_root=/home/ubuntu/memory-v1-reviews
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
observation=93024235-89a8-49d5-88fa-7e4a143b68f3
stage_runner=scripts/memory_v1_v5_2_canonical_name_claim_stage.py
review_manifest_runner=scripts/memory_v1_v5_2_canonical_name_claim_review_manifest.py
review_runner=scripts/memory_v1_v5_2_canonical_name_claim_review_batch.py
migration=ops/sql/20260725_memory_v1_v5_2_canonical_name_claim_source.sql
security_test=tests/memory_v1_v5_2_canonical_name_claim_source_security.sql
port=${MEMORY_V1_V5_2_CANONICAL_NAME_CLAIM_CLONE_PORT:-55497}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52canonicalnameclaimclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
backup=$(mktemp /tmp/memory-v1-v5-2-canonical-name-claim.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-canonical-name-claim-roles.XXXXXX.sql)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
head=$(git -C "$repo_root" rev-parse HEAD)

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
      'claims',(SELECT count(*) FROM memory.claim),
      'revisions',(SELECT count(*) FROM memory.claim_revision)
    )::text)"
}

qdrant_before=$(qdrant_signature)
production_before=$(production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)

docker exec brains-postgres-1 pg_dump -U sage -d memory -Fc >"$backup"
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
run_sql <"$migration"
run_sql <"$migration"
run_sql <"$security_test"

PYTHONPATH="$repo_root/scripts:$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/tests/test_memory_v1_v5_2_compiler_v8_claim_stage.py"

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" manifest \
  --owner "$target_owner" \
  --observation "$observation" \
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
before_claims=$(scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner'")
before_revisions=$(scalar "SELECT count(*) FROM memory.claim_revision WHERE owner_user_id='$target_owner'")

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" cross-owner \
  --manifest "$manifest" --other-owner "$other_owner" --output "$cross_result"
[[ "$(jq -er '.cross_owner_rejected' "$cross_result")" == true ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CANONICAL_NAME_CLAIM_STAGE_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" apply \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_EXACT_NEKO_CANONICAL_NAME_CLAIM_CANDIDATE_ONLY \
  --output "$stage_apply"
[[ "$(jq -er '.rows_written' "$stage_apply")" == 4 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CANONICAL_NAME_CLAIM_STAGE_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" replay \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_EXACT_NEKO_CANONICAL_NAME_CLAIM_CANDIDATE_ONLY \
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
    raise SystemExit("exactly one canonical-name candidate is required")
item = stage["items"][0]
if (
    item["predicate"] != "identity.name_canonical"
    or item["canonical_text"] != "Neko's canonical name is Neko."
):
    raise SystemExit("canonical-name candidate semantics drifted")
value = {
    "contract_version": "memory_v1_v5_2_canonical_name_claim_review_decisions_v1",
    "owner_user_id": stage["owner_user_id"],
    "evidence_ids": stage["evidence_ids"],
    "decisions": [{
        "observation_id": item["observation_id"],
        "decision": "authorized",
        "reason": (
            "Reviewed canonical-name claim for the reconciled Neko animal "
            "entity; the source correction remains immutable evidence."
        ),
        "reason_codes": [
            "correction_target_reconciled",
            "canonical_name_normalized",
            "owner_entity_binding_reviewed",
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
MEMORY_V1_V5_2_CANONICAL_NAME_CLAIM_REVIEW_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode apply --manifest "$review_manifest" --output "$review_apply"
[[ "$(jq -er '.rows_written' "$review_apply")" == 1 ]]
[[ "$(jq -er '.decision_counts.authorized' "$review_apply")" == 1 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CANONICAL_NAME_CLAIM_REVIEW_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode replay --manifest "$review_manifest" --output "$review_replay"
[[ "$(jq -er '.rows_written' "$review_replay")" == 0 ]]

[[ "$(scalar "SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id='$target_owner'")" == "$before_requests" ]]
[[ "$(scalar "SELECT count(*) FROM memory.observation_entailment_v5 WHERE owner_user_id='$target_owner'")" == "$before_entailments" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan WHERE owner_user_id='$target_owner'")" == "$((before_plans + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan_item WHERE owner_user_id='$target_owner'")" == "$((before_items + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_claim_payload WHERE owner_user_id='$target_owner'")" == "$((before_payloads + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan_observation WHERE owner_user_id='$target_owner'")" == "$((before_links + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_review WHERE owner_user_id='$target_owner'")" == "$((before_reviews + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner'")" == "$before_claims" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim_revision WHERE owner_user_id='$target_owner'")" == "$before_revisions" ]]

[[ "$(scalar "SELECT count(*) FROM memory.projection_claim_payload p JOIN memory.projection_plan_item i ON i.owner_user_id=p.owner_user_id AND i.plan_id=p.plan_id AND i.projection_ref=p.projection_ref JOIN memory.projection_plan_observation l ON l.owner_user_id=i.owner_user_id AND l.plan_id=i.plan_id AND l.projection_ref=i.projection_ref JOIN memory.projection_review r ON r.owner_user_id=i.owner_user_id AND r.plan_id=i.plan_id AND r.projection_ref=i.projection_ref WHERE p.owner_user_id='$target_owner' AND l.observation_id='$observation'::uuid AND i.predicate='identity.name_canonical' AND i.review_state='manual_review_required' AND p.claim_class='direct_claim' AND p.canonical_text='Neko''s canonical name is Neko.' AND r.decision='authorized'")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim_observation WHERE owner_user_id='$target_owner' AND observation_id='$observation'::uuid")" == 0 ]]

[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(production_signature)" == "$production_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]

REPORT="$report" HEAD="$head" MANIFEST="$manifest" REVIEW="$review_manifest" \
QDRANT="$qdrant_before" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

stage = json.loads(Path(os.environ["MANIFEST"]).read_text())
review = json.loads(Path(os.environ["REVIEW"]).read_text())
value = {
    "contract_version": "memory_v1_v5_2_canonical_name_claim_review_clone_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": stage["owner_user_id"],
    "stage_manifest_sha256": stage["manifest_sha256"],
    "review_manifest_sha256": review["manifest_sha256"],
    "candidates": [
        {
            "observation_id": item["observation_id"],
            "predicate": item["predicate"],
            "state_relation": item["state_relation"],
            "canonical_text": item["canonical_text"],
        }
        for item in stage["items"]
    ],
    "verification": {
        "stage_rows_written": 4,
        "existing_entailment_reused": True,
        "review_rows_written": 1,
        "stage_replay_zero_write": True,
        "review_replay_zero_write": True,
        "cross_owner_rejected": True,
        "production_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "claims_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    },
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

printf 'report=%s\n' "$report"
printf 'memory_v1_v5_2_canonical_name_claim_review_clone: PASS\n'

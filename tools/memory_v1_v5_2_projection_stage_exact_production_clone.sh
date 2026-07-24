#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores the current production database into a
# disposable clone, then applies and replays one exact four-item V5.2
# reported-stance projection-stage manifest. Production remains read-only.

if [[ "$#" -ne 3 ]]; then
  echo 'usage: ..._exact_production_clone.sh MANIFEST AUTHORIZATION REPORT' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
manifest=$(realpath "$1")
authorization=$(realpath "$2")
report=$(realpath -m "$3")
review_root=/home/ubuntu/memory-v1-reviews
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
target_evidence=22bd0732-3539-4180-8f89-8f84114131c0
runner=scripts/memory_v1_v5_2_projection_stage_batch.py
port=${MEMORY_V1_V5_2_PROJECTION_STAGE_CLONE_PORT:-55495}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52projectionstageclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
backup=$(mktemp /tmp/memory-v1-v5-2-projection-stage.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-projection-stage-roles.XXXXXX.sql)
work=$(mktemp -d /tmp/memory-v1-v5-2-projection-stage.XXXXXX)
apply_result="$work/apply.json"
replay_result="$work/replay.json"
cross_result="$work/cross-owner.json"
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$role_sql"
  rm -rf "$work"
}
trap cleanup EXIT

for input in "$manifest" "$authorization"; do
  [[ "$input" == "$review_root"/* ]]
  [[ -f "$input" && "$(stat -c '%a' "$input")" == 600 ]]
done
[[ "$report" == "$review_root"/* && ! -e "$report" ]]
[[ "$(jq -er '.target_server' "$manifest")" == seebx ]]
[[ "$(jq -er '.owner_user_id' "$manifest")" == "$target_owner" ]]
[[ "$(jq -er '.evidence_id' "$manifest")" == "$target_evidence" ]]
[[ "$(jq -er '.items|length' "$manifest")" == 4 ]]
[[ "$(jq -er '.expected_new_rows' "$manifest")" == 24 ]]
[[ "$(jq -er '.required_head_commit' "$manifest")" == \
  "$(git -C "$repo_root" rev-parse HEAD)" ]]

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
      'plan_items',(SELECT count(*) FROM memory.projection_plan_item),
      'payloads',(SELECT count(*) FROM memory.projection_claim_payload),
      'plan_observations',(SELECT count(*) FROM memory.projection_plan_observation),
      'claims',(SELECT count(*) FROM memory.claim),
      'revisions',(SELECT count(*) FROM memory.claim_revision)
    )::text)"
}

qdrant_before=$(qdrant_signature)
production_before=$(production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner >"$backup"
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
printf '%s\n' \
  "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner <"$backup"

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/tests/test_memory_v1_v5_2_projection_dispatch.py"

before_requests=$(scalar "SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id='$target_owner'::uuid")
before_entailments=$(scalar "SELECT count(*) FROM memory.observation_entailment_v5
  WHERE owner_user_id='$target_owner'::uuid")
before_plans=$(scalar "SELECT count(*) FROM memory.projection_plan
  WHERE owner_user_id='$target_owner'::uuid")
before_items=$(scalar "SELECT count(*) FROM memory.projection_plan_item
  WHERE owner_user_id='$target_owner'::uuid")
before_payloads=$(scalar "SELECT count(*) FROM memory.projection_claim_payload
  WHERE owner_user_id='$target_owner'::uuid")
before_links=$(scalar "SELECT count(*) FROM memory.projection_plan_observation
  WHERE owner_user_id='$target_owner'::uuid")
before_claims=$(scalar "SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")
before_revisions=$(scalar "SELECT count(*) FROM memory.claim_revision
  WHERE owner_user_id='$target_owner'::uuid")

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$runner" cross-owner \
  --manifest "$manifest" --other-owner "$other_owner" --output "$cross_result"
[[ "$(jq -er '.cross_owner_rejected' "$cross_result")" == true ]]
[[ "$(jq -er '.rows_written' "$cross_result")" == 0 ]]

head=$(git -C "$repo_root" rev-parse HEAD)
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_PROJECTION_STAGE_BATCH_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$runner" apply \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_OWNER_V5_2_REPORTED_STANCE_PROJECTIONS_ONLY \
  --output "$apply_result"
[[ "$(jq -er '.rows_written' "$apply_result")" == 24 ]]
[[ "$(jq -er '.item_count' "$apply_result")" == 4 ]]
[[ "$(jq -er '[.outcomes[].rows_written]|add' "$apply_result")" == 24 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_PROJECTION_STAGE_BATCH_APPLY=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$runner" replay \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_OWNER_V5_2_REPORTED_STANCE_PROJECTIONS_ONLY \
  --output "$replay_result"
[[ "$(jq -er '.rows_written' "$replay_result")" == 0 ]]
[[ "$(jq -er '[.outcomes[].entailment_outcome]|unique|join(\",\")' \
  "$replay_result")" == replayed ]]
[[ "$(jq -er '[.outcomes[].projection_outcome]|unique|join(\",\")' \
  "$replay_result")" == replayed ]]

[[ "$(scalar "SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id='$target_owner'::uuid")" == "$((before_requests + 4))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.observation_entailment_v5
  WHERE owner_user_id='$target_owner'::uuid")" == "$((before_entailments + 4))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan
  WHERE owner_user_id='$target_owner'::uuid")" == "$((before_plans + 4))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan_item
  WHERE owner_user_id='$target_owner'::uuid")" == "$((before_items + 4))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_claim_payload
  WHERE owner_user_id='$target_owner'::uuid")" == "$((before_payloads + 4))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan_observation
  WHERE owner_user_id='$target_owner'::uuid")" == "$((before_links + 4))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")" == "$before_claims" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim_revision
  WHERE owner_user_id='$target_owner'::uuid")" == "$before_revisions" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan AS plan
  JOIN memory.projection_plan_observation AS link
    ON link.owner_user_id=plan.owner_user_id AND link.plan_id=plan.plan_id
  JOIN memory.observation AS observation
    ON observation.owner_user_id=link.owner_user_id
   AND observation.observation_id=link.observation_id
  WHERE plan.owner_user_id='$target_owner'::uuid
    AND observation.evidence_id='$target_evidence'::uuid
    AND plan.status='pending_review'
    AND plan.predicate_registry_version='memory_predicate_registry_v5_2'")" == 4 ]]

[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(production_signature)" == "$production_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]

REPORT="$report" MANIFEST="$manifest" APPLY="$apply_result" \
REPLAY="$replay_result" CROSS="$cross_result" HEAD="$head" \
QDRANT="$qdrant_before" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

manifest = json.loads(Path(os.environ["MANIFEST"]).read_text())
value = {
    "contract_version": "memory_v1_v5_2_projection_stage_clone_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": manifest["owner_user_id"],
    "evidence_id": manifest["evidence_id"],
    "manifest_sha256": manifest["manifest_sha256"],
    "verification": {
        "items": 4,
        "rows_written": 24,
        "zero_write_replay": True,
        "cross_owner_rejected": True,
        "production_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "claims_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    },
    "evidence": {
        "apply_result_sha256": json.loads(Path(os.environ["APPLY"]).read_text())["result_sha256"],
        "replay_result_sha256": json.loads(Path(os.environ["REPLAY"]).read_text())["result_sha256"],
        "cross_owner_result_sha256": json.loads(Path(os.environ["CROSS"]).read_text())["result_sha256"],
    },
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

printf 'report=%s\n' "$report"
printf 'memory_v1_v5_2_projection_stage_exact_production_clone: PASS\n'

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable clone, installs the
# additive reinforcement API, and stages exactly one Neko correction as
# supporting evidence for the existing claim. Production and Qdrant are
# read-only.

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
migration=ops/sql/20260725_memory_v1_v5_2_projection_reinforcement.sql
security_test=tests/memory_v1_v5_2_projection_reinforcement_security.sql
port=${MEMORY_V1_V5_2_REINFORCEMENT_CLONE_PORT:-55499}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52reinforcementclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
backup=$(mktemp /tmp/memory-v1-v5-2-reinforcement.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-reinforcement-roles.XXXXXX.sql)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
head=$(git -C "$repo_root" rev-parse HEAD)

manifest="$artifact_dir/stage-manifest.json"
authorization="$artifact_dir/stage-authorization.json"
cross_result="$artifact_dir/stage-cross-owner.json"
stage_apply="$artifact_dir/stage-apply.json"
stage_replay="$artifact_dir/stage-replay.json"
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
      'claims',(SELECT count(*) FROM memory.claim),
      'revisions',(SELECT count(*) FROM memory.claim_revision),
      'claim_links',(SELECT count(*) FROM memory.claim_observation)
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

[[ "$(scalar "SELECT count(*) FROM memory.relational_operation_request WHERE owner_user_id='$target_owner'")" == "$((before_requests + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.observation_entailment_v5 WHERE owner_user_id='$target_owner'")" == "$((before_entailments + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan WHERE owner_user_id='$target_owner'")" == "$((before_plans + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan_item WHERE owner_user_id='$target_owner'")" == "$((before_items + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_claim_payload WHERE owner_user_id='$target_owner'")" == "$((before_payloads + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.projection_plan_observation WHERE owner_user_id='$target_owner'")" == "$((before_links + 1))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner'")" == "$before_claims" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim_revision WHERE owner_user_id='$target_owner'")" == "$before_revisions" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim_observation WHERE owner_user_id='$target_owner'")" == "$before_claim_links" ]]

[[ "$(scalar "SELECT count(*) FROM memory.projection_plan_item i JOIN memory.projection_claim_payload p USING(owner_user_id,plan_id,projection_ref) JOIN memory.projection_plan_observation l USING(owner_user_id,plan_id,projection_ref) WHERE i.owner_user_id='$target_owner' AND l.observation_id='$observation'::uuid AND i.target_action='reinforce' AND i.expected_revision_number=2 AND p.target_claim_id='$claim'::uuid AND p.claim_class='direct_claim' AND p.canonical_text='Neko''s canonical name is Neko.' AND p.surface_policy='direct_or_relevant' AND i.target_reason_codes='[\"additional_supporting_observation\",\"canonical_name_correction_normalized\"]'::jsonb")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim_observation WHERE owner_user_id='$target_owner' AND observation_id='$observation'::uuid")" == 0 ]]

[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(production_signature)" == "$production_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]

REPORT="$report" HEAD="$head" MANIFEST="$manifest" QDRANT="$qdrant_before" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

stage = json.loads(Path(os.environ["MANIFEST"]).read_text())
value = {
    "contract_version":
        "memory_v1_v5_2_neko_correction_reinforcement_clone_report_v1",
    "completed_at":
        dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00","Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": stage["owner_user_id"],
    "stage_manifest_sha256": stage["manifest_sha256"],
    "target_claim_id": "8e3f4d82-8c21-4bbd-bbe8-91dd585f6fc9",
    "target_revision_number": 2,
    "verification": {
        "stage_rows_written": 6,
        "stage_replay_zero_write": True,
        "cross_owner_rejected": True,
        "claims_written": 0,
        "claim_revisions_written": 0,
        "claim_observation_links_written": 0,
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
printf 'memory_v1_v5_2_neko_correction_reinforcement_clone: PASS\n'

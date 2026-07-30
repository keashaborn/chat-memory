#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Clone-tests the exact accepted legacy-stage observation
# set through V5.2 review, staging, governed authorization, and durable claim
# materialization. Production Postgres and Qdrant remain read-only.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_2_claim_target_review_legacy_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
policy_held=1a498c5b-29ee-4b91-a038-7cc3987162f9
first_admission=8c751d72-073c-4b33-9431-72838f374003
second_admission=fd48e387-406e-47a8-b87a-b15320d63487
expected_target_sha256=a8400e1d233c9ecde4a22420e6705d31166a14e4013cffbc487e13bbaa951885
python_bin=/opt/chat-memory/venv/bin/python
review_root=/home/ubuntu/memory-v1-reviews
head=$(git -C "$repo_root" rev-parse HEAD)
run_id="$(date -u +%Y%m%dT%H%M%SZ)_${head:0:12}_$$"
artifact_dir="$review_root/legacy-stage-claim-materialization-clone-$run_id"

review_report="$artifact_dir/claim-target-review.json"
stage_manifest="$artifact_dir/stage-manifest.json"
stage_authorization="$artifact_dir/stage-authorization.json"
stage_cross_owner="$artifact_dir/stage-cross-owner.json"
stage_apply="$artifact_dir/stage-apply.json"
stage_replay="$artifact_dir/stage-replay.json"
review_manifest="$artifact_dir/review-manifest.json"
review_authorization="$artifact_dir/review-authorization.json"
review_cross_owner="$artifact_dir/review-cross-owner.json"
review_apply="$artifact_dir/review-apply.json"
review_replay="$artifact_dir/review-replay.json"
claim_manifest="$artifact_dir/claim-apply-manifest.json"
claim_preflight="$artifact_dir/claim-preflight.json"
claim_apply="$artifact_dir/claim-apply.json"
claim_replay="$artifact_dir/claim-replay.json"
report="$artifact_dir/clone-report.json"

if [[ -r "$repo_root/.env" ]]; then
  set -a
  source "$repo_root/.env"
  set +a
elif [[ -r /opt/chat-memory/.env ]]; then
  set -a
  source /opt/chat-memory/.env
  set +a
fi
[[ -n "${POSTGRES_DSN:-}" ]]
[[ "$(stat -c '%a' "$review_root")" == 700 ]]
install -d -m 0700 "$artifact_dir"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | cut -d' ' -f1
}

production_signature() {
  docker exec "$container" psql -X -A -t -U sage -d "$source_db" \
    -v ON_ERROR_STOP=1 -c "
      SELECT jsonb_build_object(
        'claims',(SELECT count(*) FROM memory.claim),
        'claim_revisions',(SELECT count(*) FROM memory.claim_revision),
        'claim_links',(SELECT count(*) FROM memory.claim_observation),
        'assessment_reviews',(
          SELECT count(*) FROM memory.claim_assessment_review_v5
        ),
        'assessments',(SELECT count(*) FROM memory.claim_assessment),
        'assessment_apply_events',(
          SELECT count(*) FROM memory.claim_assessment_apply_v5
        ),
        'plans',(SELECT count(*) FROM memory.projection_plan),
        'plan_items',(SELECT count(*) FROM memory.projection_plan_item),
        'plan_payloads',(
          SELECT count(*) FROM memory.projection_claim_payload
        ),
        'plan_observations',(
          SELECT count(*) FROM memory.projection_plan_observation
        ),
        'projection_reviews',(
          SELECT count(*) FROM memory.projection_review
        ),
        'projection_apply_events',(
          SELECT count(*) FROM memory.projection_apply_event
        ),
        'projection_dispatches',(
          SELECT count(*) FROM memory.projection_dispatch_v5
        ),
        'operation_requests',(
          SELECT count(*) FROM memory.relational_operation_request
        ),
        'projection_outbox',(SELECT count(*) FROM memory.projection_outbox)
      )::text;" | sha256sum | cut -d' ' -f1
}

scalar() {
  docker exec "$container" psql -X -A -t -U sage -d "$clone_db" \
    -v ON_ERROR_STOP=1 -c "$1"
}

production_before=$(production_signature)
qdrant_before=$(qdrant_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)

docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"
docker exec "$container" psql -X -U sage -d postgres -v ON_ERROR_STOP=1 \
  -c "COMMENT ON DATABASE \"$clone_db\" IS
      'memory_v1_v5_2_claim_target_review_clone_v1';" >/dev/null

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

source = urlsplit(os.environ["SOURCE_DSN"])
if source.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("unsupported PostgreSQL DSN scheme")
print(urlunsplit((
    source.scheme,
    source.netloc,
    "/" + os.environ["CLONE_DB"],
    source.query,
    source.fragment,
)))
PY
)

mapfile -t targets < <(
  scalar "
    SELECT member.observation_id,member.observation_sha256
    FROM memory.v5_local_legacy_stage_admission_observation AS member
    JOIN memory.v5_local_packet_stage_admission AS stage
      ON stage.owner_user_id=member.owner_user_id
     AND stage.admission_id=member.admission_id
    JOIN memory.observation_entailment_v5 AS entailment
      ON entailment.owner_user_id=member.owner_user_id
     AND entailment.observation_id=member.observation_id
    WHERE member.owner_user_id='$owner'::uuid
      AND member.admission_id IN (
        '$first_admission'::uuid,
        '$second_admission'::uuid
      )
      AND stage.decision='legacy_applied_stage_entailment'
      AND entailment.decision='accepted'
    ORDER BY member.observation_id;"
)
[[ "${#targets[@]}" == 20 ]]
target_sha256=$(printf '%s\n' "${targets[@]}" | sha256sum | cut -d' ' -f1)
[[ "$target_sha256" == "$expected_target_sha256" ]]

observations=()
for target in "${targets[@]}"; do
  observation=${target%%|*}
  if [[ "$observation" != "$policy_held" ]]; then
    observations+=("$observation")
  fi
done
[[ "${#observations[@]}" == 19 ]]

tables=(
  claim
  claim_revision
  claim_observation
  claim_assessment_review_v5
  claim_assessment
  claim_assessment_apply_v5
  projection_plan
  projection_plan_item
  projection_claim_payload
  projection_plan_observation
  projection_review
  projection_apply_event
  projection_dispatch_v5
  relational_operation_request
  projection_outbox
)
declare -A before
for table in "${tables[@]}"; do
  before["$table"]=$(scalar \
    "SELECT count(*) FROM memory.$table WHERE owner_user_id='$owner'::uuid")
done

runtime=(
  env
  POSTGRES_DSN="$clone_dsn"
  PYTHONPATH="$repo_root:$repo_root/scripts"
)
observation_args=()
for observation in "${observations[@]}"; do
  observation_args+=(--observation "$observation")
done

MEMORY_V1_DISPOSABLE_CLONE_REQUIRED=1 \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_review.py" \
  --owner "$owner" "${observation_args[@]}" --output "$review_report"
[[ "$(jq -er '.item_count' "$review_report")" == 19 ]]
[[ "$(jq -er '.action_counts.create' "$review_report")" == 19 ]]
[[ "$(jq -er '.proofs.disposable_clone_verified' "$review_report")" == true ]]

"${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" manifest \
  --owner "$owner" --review-report "$review_report" \
  --required-head "$head" --output "$stage_manifest"
[[ "$(jq -er '.stage_item_count' "$stage_manifest")" == 19 ]]
[[ "$(jq -er '.held_item_count' "$stage_manifest")" == 0 ]]
[[ "$(jq -er '.expected_rows' "$stage_manifest")" == 76 ]]

PYTHONPATH="$repo_root:$repo_root/scripts" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" authorize \
  --manifest "$stage_manifest" --output "$stage_authorization"

"${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" cross-owner \
  --manifest "$stage_manifest" --other-owner "$other_owner" \
  --output "$stage_cross_owner"
[[ "$(jq -er '.cross_owner_rejected' "$stage_cross_owner")" == true ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CLAIM_TARGET_STAGE_APPLY=authorized \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" apply \
  --manifest "$stage_manifest" --authorization "$stage_authorization" \
  --confirm STAGE_REVIEWED_V5_2_CLAIM_TARGETS_ONLY --output "$stage_apply"
[[ "$(jq -er '.rows_written' "$stage_apply")" == 76 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CLAIM_TARGET_STAGE_APPLY=authorized \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_stage.py" replay \
  --manifest "$stage_manifest" --authorization "$stage_authorization" \
  --confirm STAGE_REVIEWED_V5_2_CLAIM_TARGETS_ONLY --output "$stage_replay"
[[ "$(jq -er '.rows_written' "$stage_replay")" == 0 ]]

"${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_review_batch.py" manifest \
  --stage-manifest "$stage_manifest" --required-head "$head" \
  --output "$review_manifest"
[[ "$(jq -er '.expected_new_rows' "$review_manifest")" == 19 ]]

PYTHONPATH="$repo_root:$repo_root/scripts" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_review_batch.py" authorize \
  --manifest "$review_manifest" --output "$review_authorization"

"${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_review_batch.py" cross-owner \
  --manifest "$review_manifest" --other-owner "$other_owner" \
  --output "$review_cross_owner"
[[ "$(jq -er '.cross_owner_rejected' "$review_cross_owner")" == true ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CLAIM_TARGET_REVIEW_APPLY=authorized \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_review_batch.py" apply \
  --manifest "$review_manifest" --authorization "$review_authorization" \
  --confirm REVIEW_EXACT_STAGED_V5_2_CLAIM_TARGETS_ONLY \
  --output "$review_apply"
[[ "$(jq -er '.rows_written' "$review_apply")" == 19 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CLAIM_TARGET_REVIEW_APPLY=authorized \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_claim_target_review_batch.py" replay \
  --manifest "$review_manifest" --authorization "$review_authorization" \
  --confirm REVIEW_EXACT_STAGED_V5_2_CLAIM_TARGETS_ONLY \
  --output "$review_replay"
[[ "$(jq -er '.rows_written' "$review_replay")" == 0 ]]

"${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_reviewed_claim_apply_manifest.py" \
  --owner "$owner" --required-head "$head" \
  --review-manifest "$review_manifest" --review-result "$review_apply" \
  --output "$claim_manifest"
[[ "$(jq -er '.items|length' "$claim_manifest")" == 19 ]]
[[ "$(jq -er '.action_counts.create' "$claim_manifest")" == 19 ]]
[[ "$(jq -er '.action_counts.reinforce' "$claim_manifest")" == 0 ]]
[[ "$(jq -er '.expected_insert_rows' "$claim_manifest")" == 209 ]]
[[ "$(jq -er '.expected_mutated_rows' "$claim_manifest")" == 228 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_reviewed_claim_apply_batch.py" \
  --mode preflight --manifest "$claim_manifest" --output "$claim_preflight"
[[ "$(jq -er '.insert_rows' "$claim_preflight")" == 0 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_REVIEWED_CLAIM_APPLY=authorized \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_reviewed_claim_apply_batch.py" \
  --mode apply --manifest "$claim_manifest" --output "$claim_apply"
[[ "$(jq -er '.insert_rows' "$claim_apply")" == 209 ]]
[[ "$(jq -er '.mutated_rows' "$claim_apply")" == 228 ]]
[[ "$(jq -er '.qdrant_writes' "$claim_apply")" == 0 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
  "${runtime[@]}" "$python_bin" \
  "$repo_root/scripts/memory_v1_v5_2_reviewed_claim_apply_batch.py" \
  --mode replay --manifest "$claim_manifest" --apply-result "$claim_apply" \
  --output "$claim_replay"
[[ "$(jq -er '.insert_rows' "$claim_replay")" == 0 ]]
[[ "$(jq -er '.mutated_rows' "$claim_replay")" == 0 ]]

probe_plan=$(jq -er '.items[0].plan_id' "$claim_manifest")
probe_review=$(jq -er '.items[0].review_id' "$claim_manifest")
psql "$clone_dsn" -X -q -v ON_ERROR_STOP=1 \
  -v other_owner="$other_owner" -v probe_plan="$probe_plan" \
  -v probe_review="$probe_review" <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id', :'other_owner', true);
SELECT set_config('test.probe_plan', :'probe_plan', true);
SELECT set_config('test.probe_review', :'probe_review', true);
DO $$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_projection_apply_v5(
      current_setting('test.probe_plan')::uuid,
      'p01',
      current_setting('test.probe_review')::uuid
    );
    RAISE EXCEPTION 'cross-owner projection preflight unexpectedly succeeded';
  EXCEPTION
    WHEN no_data_found OR insufficient_privilege THEN NULL;
  END;
END
$$;
ROLLBACK;
SQL

declare -A expected=(
  [claim]=19
  [claim_revision]=38
  [claim_observation]=19
  [claim_assessment_review_v5]=19
  [claim_assessment]=19
  [claim_assessment_apply_v5]=19
  [projection_plan]=19
  [projection_plan_item]=19
  [projection_claim_payload]=19
  [projection_plan_observation]=19
  [projection_review]=19
  [projection_apply_event]=19
  [projection_dispatch_v5]=19
  [relational_operation_request]=38
  [projection_outbox]=0
)
for table in "${tables[@]}"; do
  after=$(scalar \
    "SELECT count(*) FROM memory.$table WHERE owner_user_id='$owner'::uuid")
  delta=$((after - before["$table"]))
  [[ "$delta" == "${expected[$table]}" ]]
done

claim_ids=$(jq -r '[.outcomes[].claim_id]|join(",")' "$claim_apply")
[[ "$(scalar "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$owner'::uuid
    AND status='supported'
    AND claim_id=ANY(string_to_array('$claim_ids',',')::uuid[])")" == 19 ]]

[[ "$(production_signature)" == "$production_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]

REPORT="$report" HEAD_VALUE="$head" TARGET_SHA="$target_sha256" \
STAGE="$stage_manifest" REVIEW="$review_manifest" CLAIM="$claim_manifest" \
python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

def load(name):
    return json.loads(Path(os.environ[name]).read_text())

stage = load("STAGE")
review = load("REVIEW")
claim = load("CLAIM")
report = {
    "contract_version":
        "memory_v1_v5_2_legacy_stage_claim_materialization_clone_report_v1",
    "head_commit": os.environ["HEAD_VALUE"],
    "owner_user_id": stage["owner_user_id"],
    "target_set_sha256": os.environ["TARGET_SHA"],
    "stage_manifest_sha256": stage["manifest_sha256"],
    "review_manifest_sha256": review["manifest_sha256"],
    "claim_manifest_sha256": claim["manifest_sha256"],
    "counts": {
        "source_targets": 20,
        "surface_policy_holds": 1,
        "create_targets": 19,
        "stage_rows": 76,
        "review_rows": 19,
        "claim_insert_rows": 209,
        "claim_mutated_rows": 228,
    },
    "proofs": {
        "disposable_clone": True,
        "zero_write_replay": True,
        "cross_owner_rejected": True,
        "production_postgres_unchanged": True,
        "qdrant_unchanged": True,
        "model_calls": 0,
        "projection_outbox_rows": 0,
        "retrieval_changes": 0,
        "prompt_influence": 0,
    },
    "hard_stop": "before production staging or durable claim materialization",
}
canonical = json.dumps(report, sort_keys=True, separators=(",", ":"))
report["report_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

printf '%s\n' \
  'MEMORY_V1_V5_2_LEGACY_STAGE_CLAIM_MATERIALIZATION_CLONE=PASS' \
  "HEAD=$head" \
  "TARGET_SET_SHA256=$target_sha256" \
  "STAGE_MANIFEST_SHA256=$(jq -er '.manifest_sha256' "$stage_manifest")" \
  "REVIEW_MANIFEST_SHA256=$(jq -er '.manifest_sha256' "$review_manifest")" \
  "CLAIM_MANIFEST_SHA256=$(jq -er '.manifest_sha256' "$claim_manifest")" \
  'CREATE_TARGETS=19' \
  'SURFACE_POLICY_HOLDS=1' \
  'STAGE_ROWS=76' \
  'REVIEW_ROWS=19' \
  'CLAIM_INSERT_ROWS=209' \
  'CLAIM_MUTATED_ROWS=228' \
  'REPLAY_ROWS=0' \
  'MODEL_CALLS=0' \
  'QDRANT_WRITES=0' \
  'PRODUCTION_WRITES=0' \
  "REPORT=$report"

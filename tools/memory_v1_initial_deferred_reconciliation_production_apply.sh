#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Reconciles one contradictory deferred entailment into the
# governed claim-assessment path. It does not delete data, write Qdrant, stage
# projection, activate retrieval, modify prompts, or touch another owner.

if [[ "${MEMORY_V1_INITIAL_DEFERRED_RECONCILIATION_APPLY:-}" != "authorized" ]]; then
  echo "MEMORY_V1_INITIAL_DEFERRED_RECONCILIATION_APPLY=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=366c4b28a6d6d1122ed32c80326236990ba21e8d
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
claim=50ebf1af-b072-4bf9-badc-2df7585f12c6
decision=bd374be5-e908-43f5-8c5d-76a8d041cd75
reconciliation=44000000-0000-4000-8000-000000000001
review_request=44000000-0000-4000-8000-000000000002
apply_request=44000000-0000-4000-8000-000000000003
support_state_sha=6ddbff9a64423b118a0f3457d8df834bf29b7f4b944766f6eb877d5f04f99780
review_manifest=d75532e9ae3ed0083b3c963388706a9a4dff7ced7a3cb66b7bcff73eeeb68a17
reconciliation_manifest=7d1d285c45372679457b3d04c7c141b67c6b7f63566e52072e66faed5ce2b28e
container=${MEMORY_V1_DB_CONTAINER:-brains-postgres-1}
database=memory
snapshot_dir=${MEMORY_V1_SNAPSHOT_DIR:-/home/ubuntu/brains/snapshots}
lock_file=${MEMORY_V1_APPLY_LOCK_FILE:-/home/ubuntu/brains/.memory_v1_initial_deferred_reconciliation_apply.lock}
qdrant_url=${MEMORY_V1_QDRANT_URL:-http://127.0.0.1:6333}
phase=initialization
status_file=

if [[ "$container" == "brains-postgres-1" ]]; then
  [[ -z "$(git -C "$repo_root" status --porcelain)" ]] || {
    echo "production apply requires a clean Git worktree" >&2
    exit 1
  }
fi
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD

mkdir -p "$snapshot_dir"
exec 9>"$lock_file"
flock -n 9 || {
  echo "another initial deferred reconciliation holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_initial_deferred_reconciliation_${run_id}.status"

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}
record_exit() {
  code=$?
  printf 'run_id=%s\nphase=%s\nexit_code=%s\ncompleted_at=%s\n' \
    "$run_id" "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"$status_file"
  chmod 0600 "$status_file"
}
trap record_exit EXIT

qdrant_signature() {
  curl --fail --silent --show-error \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    "$qdrant_url/collections/memory_claim_v1/points/scroll" \
    | jq -cS '.result.points | sort_by(.id)' \
    | sha256sum | awk '{print $1}'
}
table_state() {
  local table=$1
  local predicate=${2:-true}
  [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || exit 1
  psql_scalar "
    SELECT count(*)::text || E'\\t' ||
           encode(digest(coalesce(string_agg(row_json,E'\\n'
             ORDER BY row_json),''),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(table_row)::text row_json
      FROM memory.\"$table\" AS table_row
      WHERE $predicate
    ) rows
  "
}
capture_unchanged_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    printf '%s\t%s\n' "$table" "$(table_state "$table")" >>"$output"
  done < <(psql_scalar "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
      AND table_name NOT IN (
        'claim','claim_assessment','claim_revision',
        'claim_assessment_review_v5','claim_assessment_apply_v5',
        'relational_operation_request',
        'claim_entailment_reconciliation_v5'
      )
    ORDER BY table_name
  ")
  printf 'claim_other\t%s\n' \
    "$(table_state claim "claim_id<>'$claim'::uuid")" >>"$output"
  printf 'claim_assessment_other\t%s\n' \
    "$(table_state claim_assessment "claim_id<>'$claim'::uuid")" >>"$output"
  printf 'claim_revision_other\t%s\n' \
    "$(table_state claim_revision "claim_id<>'$claim'::uuid")" >>"$output"
  printf 'claim_assessment_review_v5_other\t%s\n' \
    "$(table_state claim_assessment_review_v5 "claim_id<>'$claim'::uuid")" \
    >>"$output"
  printf 'claim_assessment_apply_v5_other\t%s\n' \
    "$(table_state claim_assessment_apply_v5 "claim_id<>'$claim'::uuid")" \
    >>"$output"
  printf 'operation_request_other\t%s\n' \
    "$(table_state relational_operation_request \
      "request_id NOT IN ('$review_request'::uuid,'$apply_request'::uuid)")" \
    >>"$output"
  printf 'reconciliation_other\t%s\n' \
    "$(table_state claim_entailment_reconciliation_v5 \
      "reconciliation_id<>'$reconciliation'::uuid")" >>"$output"
  chmod 0600 "$output"
}
capture_other_owner_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(table_state "$table" "owner_user_id IS DISTINCT FROM '$owner'::uuid")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(psql_scalar "
    SELECT table_name FROM information_schema.columns
    WHERE table_schema='memory' AND column_name='owner_user_id'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}
actor_preflight() {
  docker exec -i "$container" psql -X -q -A -t -F '|' \
    -v ON_ERROR_STOP=1 -U sage -d "$database" <<SQL
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','$owner',true) \\gset
SELECT claim_id,observation_id,decision_id,from_status,target_status,
       current_revision_number,support_state_sha256,
       review_authorization_manifest_sha256,
       reconciliation_manifest_sha256
FROM memory.preflight_deferred_entailment_reconciliation_v5(
  '$claim'::uuid,'$decision'::uuid
);
ROLLBACK;
RESET SESSION AUTHORIZATION;
SQL
}

phase=preflight
expected_preflight="$claim|9bf1e6b2-1840-4524-98dc-142567ebe013|$decision|candidate|retracted|1|$support_state_sha|$review_manifest|$reconciliation_manifest"
[[ "$(actor_preflight)" == "$expected_preflight" ]] || {
  echo "live deferred-entailment reconciliation preflight drifted" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM memory.claim_entailment_reconciliation_v5
    WHERE reconciliation_id='$reconciliation'::uuid)=0
  AND (SELECT count(*) FROM memory.claim_assessment_review_v5
    WHERE claim_id='$claim'::uuid)=0
  AND (SELECT count(*) FROM memory.claim_assessment_apply_v5
    WHERE claim_id='$claim'::uuid)=0
  AND (SELECT count(*) FROM memory.relational_operation_request
    WHERE request_id IN (
      '$review_request'::uuid,'$apply_request'::uuid
    ))=0
  AND (SELECT count(*) FROM memory.claim
    WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid
      AND status='candidate' AND confidence=0.500)=1
)::int")" == "1" ]]

phase=backup
partial="$snapshot_dir/.memory_pre_initial_deferred_reconciliation_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_initial_deferred_reconciliation_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
docker exec -i "$container" pg_restore -l <"$partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline_capture
baseline="$snapshot_dir/memory_v1_initial_deferred_reconciliation_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_initial_deferred_reconciliation_post_${run_id}.tsv"
other_before="$snapshot_dir/memory_v1_initial_deferred_reconciliation_other_before_${run_id}.tsv"
other_after="$snapshot_dir/memory_v1_initial_deferred_reconciliation_other_after_${run_id}.tsv"
capture_unchanged_state "$baseline"
capture_other_owner_state "$other_before"
before_assessments=$(psql_scalar "SELECT count(*) FROM memory.claim_assessment")
before_revisions=$(psql_scalar "SELECT count(*) FROM memory.claim_revision")
before_reviews=$(psql_scalar "SELECT count(*) FROM memory.claim_assessment_review_v5")
before_applies=$(psql_scalar "SELECT count(*) FROM memory.claim_assessment_apply_v5")
before_requests=$(psql_scalar "SELECT count(*) FROM memory.relational_operation_request")
before_reconciliations=$(psql_scalar "SELECT count(*) FROM memory.claim_entailment_reconciliation_v5")
qdrant_before=$(qdrant_signature)

phase=transactional_apply
log="$snapshot_dir/memory_v1_initial_deferred_reconciliation_apply_${run_id}.log"
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" \
  -v owner="$owner" -v other_owner="$other_owner" \
  -v claim="$claim" -v decision="$decision" \
  -v reconciliation="$reconciliation" \
  -v review_request="$review_request" -v apply_request="$apply_request" \
  -v reconciliation_manifest="$reconciliation_manifest" \
  >"$log" 2>&1 <<'SQL'
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', true);

SELECT * FROM memory.reconcile_deferred_entailment_claim_v5(
  :'reconciliation'::uuid,:'review_request'::uuid,
  :'apply_request'::uuid,:'claim'::uuid,:'decision'::uuid,
  :'reconciliation_manifest'
) \gset applied_
SELECT 1 / ((:'applied_outcome'='applied')::integer);
SELECT 1 / ((:'applied_rows_written'::integer=8)::integer);
SELECT 1 / ((:'applied_resulting_revision_number'::integer=2)::integer);

SELECT * FROM memory.reconcile_deferred_entailment_claim_v5(
  :'reconciliation'::uuid,:'review_request'::uuid,
  :'apply_request'::uuid,:'claim'::uuid,:'decision'::uuid,
  :'reconciliation_manifest'
) \gset replay_
SELECT 1 / ((:'replay_outcome'='replayed')::integer);
SELECT 1 / ((:'replay_rows_written'::integer=0)::integer);
SELECT 1 / ((:'replay_reconciliation_id'
  =:'applied_reconciliation_id')::integer);

SELECT set_config('app.user_id', :'other_owner', true);
DO $cross_owner$
BEGIN
  BEGIN
    PERFORM * FROM
      memory.preflight_deferred_entailment_reconciliation_v5(
        '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
        'bd374be5-e908-43f5-8c5d-76a8d041cd75'::uuid
      );
    RAISE EXCEPTION 'cross-owner reconciliation unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;
SELECT 1 / (((
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id=:'owner'::uuid AND claim_id=:'claim'::uuid
    AND status='retracted' AND confidence=0
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.claim_entailment_reconciliation_v5
  WHERE owner_user_id=:'owner'::uuid
    AND reconciliation_id=:'reconciliation'::uuid
    AND decision_id=:'decision'::uuid
)=1)::integer);
COMMIT;
RESET SESSION AUTHORIZATION;
\echo review_id=:'applied_review_id'
\echo apply_event_id=:'applied_apply_event_id'
\echo assessment_id=:'applied_assessment_id'
SQL
chmod 0600 "$log"

phase=postflight
capture_unchanged_state "$post"
capture_other_owner_state "$other_after"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo "unrelated Memory V1 rows changed" >&2
  exit 1
}
cmp -s "$other_before" "$other_after" || {
  diff -u "$other_before" "$other_after" >&2 || true
  echo "another owner's Memory V1 rows changed" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM memory.claim_assessment)=$before_assessments+1
  AND (SELECT count(*) FROM memory.claim_revision)=$before_revisions+1
  AND (SELECT count(*) FROM memory.claim_assessment_review_v5)
    =$before_reviews+1
  AND (SELECT count(*) FROM memory.claim_assessment_apply_v5)
    =$before_applies+1
  AND (SELECT count(*) FROM memory.relational_operation_request)
    =$before_requests+2
  AND (SELECT count(*) FROM memory.claim_entailment_reconciliation_v5)
    =$before_reconciliations+1
  AND (SELECT count(*) FROM memory.claim_revision
    WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid
      AND revision_number=2
      AND snapshot->>'status'='retracted'
      AND (snapshot->>'confidence')::numeric=0)=1
  AND (SELECT count(*) FROM memory.claim_entailment_reconciliation_v5
    WHERE reconciliation_id='$reconciliation'::uuid
      AND reconciliation_manifest_sha256='$reconciliation_manifest'
      AND prior_revision_number=1
      AND resulting_revision_number=2)=1
)::int")" == "1" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=report
report="$snapshot_dir/memory_v1_initial_deferred_reconciliation_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
OTHER_BEFORE="$other_before" OTHER_AFTER="$other_after" LOG="$log" \
REPORT="$report" QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_initial_deferred_reconciliation_report_v1",
    "instruction_source": "user_build_and_apply_generic_reconciliation_20260716",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "evidence": {
        "baseline": os.environ["BASELINE"],
        "post": os.environ["POST"],
        "other_owner_before": os.environ["OTHER_BEFORE"],
        "other_owner_after": os.environ["OTHER_AFTER"],
        "apply_log": os.environ["LOG"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "writes": {
        "claim_update": 1,
        "claim_assessment": 1,
        "claim_revision": 1,
        "claim_assessment_review_v5": 1,
        "claim_assessment_apply_v5": 1,
        "relational_operation_request": 2,
        "claim_entailment_reconciliation_v5": 1,
        "qdrant": 0,
    },
    "checks": {
        "claim_retracted": True,
        "resulting_revision_number": 2,
        "decision_to_assessment_provenance_bound": True,
        "exact_replay_zero_write": True,
        "cross_owner_rejected": True,
        "other_owner_rows_unchanged": True,
        "unrelated_memory_rows_unchanged": True,
        "qdrant_unchanged": True,
        "projection_staged": False,
        "retrieval_activated": False,
        "prompt_influence": False,
    },
    "hard_stop": "before_projection_or_retrieval_activation",
}
Path(os.environ["REPORT"]).write_text(
    json.dumps(value, indent=2, sort_keys=True) + "\n"
)
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'memory_v1_initial_deferred_reconciliation_production_apply: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"

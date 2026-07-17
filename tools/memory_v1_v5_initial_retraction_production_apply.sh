#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Retracts the single overstated V5 occupation candidate.
# It does not create a replacement claim, write Qdrant, activate retrieval,
# change prompts, or touch another owner's rows.

if [[ "${MEMORY_V1_V5_INITIAL_RETRACTION_APPLY:-}" != "authorized" ]]; then
  echo "MEMORY_V1_V5_INITIAL_RETRACTION_APPLY=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=5c038350c6e620b4dc76de550bab52f4f941149e
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
claim=50ebf1af-b072-4bf9-badc-2df7585f12c6
review_request=0d8a6afa-a8d0-4c8c-9fab-10ba1e230b35
apply_request=bcc252cf-1193-46ce-816e-ccf7d359142c
review_manifest=653e1e55d42b8695a71c8a0bb17b28b01f876b0094b36c205ee375754a474fd7
reason_codes='["explicit_source_negation","projection_semantic_overreach","replacement_predicate_unavailable"]'
rationale='The source explicitly says personal training is not done for a living; the occupation projection overstates it.'
reviewer_ref=memory_v1_v5_claim_assessment_initial_retraction
container=${MEMORY_V1_DB_CONTAINER:-brains-postgres-1}
database=memory
snapshot_dir=${MEMORY_V1_SNAPSHOT_DIR:-/home/ubuntu/brains/snapshots}
lock_file=${MEMORY_V1_APPLY_LOCK_FILE:-/home/ubuntu/brains/.memory_v1_v5_initial_retraction_apply.lock}
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
  echo "another V5 initial retraction apply holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_initial_retraction_${run_id}.status"

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

record_exit() {
  exit_code=$?
  {
    printf 'run_id=%s\n' "$run_id"
    printf 'phase=%s\n' "$phase"
    printf 'exit_code=%s\n' "$exit_code"
    printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$status_file"
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

capture_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || exit 1
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(digest(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(t)::text row_json FROM memory.\"$table\" t
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(psql_scalar "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

capture_other_owner_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || exit 1
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(digest(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(t)::text row_json
        FROM memory.\"$table\" t
        WHERE owner_user_id IS DISTINCT FROM '$owner'::uuid
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(psql_scalar "
    SELECT table_name FROM information_schema.columns
    WHERE table_schema='memory' AND column_name='owner_user_id'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

phase=preflight
[[ "$(psql_scalar "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$owner'::uuid
    AND claim_id='$claim'::uuid
    AND status='candidate'
    AND confidence=0.500
")" == "1" ]]
[[ "$(psql_scalar "
  SELECT (
    (SELECT count(*) FROM memory.claim_assessment_review_v5
      WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid)=0
    AND
    (SELECT count(*) FROM memory.claim_assessment_apply_v5
      WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid)=0
    AND
    (SELECT count(*) FROM memory.relational_operation_request
      WHERE owner_user_id='$owner'::uuid
        AND request_id IN ('$review_request'::uuid,'$apply_request'::uuid))=0
  )::int
")" == "1" ]]

preflight_output=$(docker exec -i "$container" psql \
  -X -A -F '|' -t -v ON_ERROR_STOP=1 -U sage -d "$database" \
  -v owner="$owner" -v claim="$claim" \
  -v reason_codes="$reason_codes" -v rationale="$rationale" \
  -v reviewer_ref="$reviewer_ref" <<'SQL'
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', true);
SELECT * FROM memory.preflight_claim_assessment_review_v5(
  :'claim'::uuid,'retract'::memory.claim_assessment_action_v5,
  0,1,0,1,:'reason_codes'::jsonb,:'rationale','system',:'reviewer_ref'
);
ROLLBACK;
RESET SESSION AUTHORIZATION;
SQL
)
preflight_line=$(printf '%s\n' "$preflight_output" | grep "^$claim|")
[[ "$preflight_line" == "$claim|candidate|retracted|1|"*"|$review_manifest" ]]

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_initial_retraction_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_initial_retraction_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline_capture
baseline="$snapshot_dir/memory_v1_v5_initial_retraction_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_v5_initial_retraction_post_${run_id}.tsv"
other_before="$snapshot_dir/memory_v1_v5_initial_retraction_other_before_${run_id}.tsv"
other_after="$snapshot_dir/memory_v1_v5_initial_retraction_other_after_${run_id}.tsv"
capture_state "$baseline"
capture_other_owner_state "$other_before"
qdrant_before=$(qdrant_signature)

phase=transactional_apply
apply_log="$snapshot_dir/memory_v1_v5_initial_retraction_apply_${run_id}.log"
docker exec -i "$container" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
  -v owner="$owner" -v other_owner="$other_owner" -v claim="$claim" \
  -v review_request="$review_request" -v apply_request="$apply_request" \
  -v review_manifest="$review_manifest" -v reason_codes="$reason_codes" \
  -v rationale="$rationale" -v reviewer_ref="$reviewer_ref" \
  >"$apply_log" 2>&1 <<'SQL'
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', true);

SELECT * FROM memory.review_claim_assessment_v5(
  :'review_request'::uuid,:'claim'::uuid,
  'retract'::memory.claim_assessment_action_v5,
  0,1,0,1,:'reason_codes'::jsonb,:'rationale',
  'system',:'reviewer_ref',:'review_manifest'
) \gset review_
SELECT 1 / ((:'review_outcome'='applied')::integer);

SELECT * FROM memory.review_claim_assessment_v5(
  :'review_request'::uuid,:'claim'::uuid,
  'retract'::memory.claim_assessment_action_v5,
  0,1,0,1,:'reason_codes'::jsonb,:'rationale',
  'system',:'reviewer_ref',:'review_manifest'
) \gset review_replay_
SELECT 1 / ((:'review_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'review_replay_review_id'=:'review_review_id')::integer);

SELECT * FROM memory.preflight_claim_assessment_apply_v5(
  :'claim'::uuid,:'review_review_id'::uuid
) \gset apply_preflight_
SELECT 1 / ((:'apply_preflight_from_status'='candidate')::integer);
SELECT 1 / ((:'apply_preflight_target_status'='retracted')::integer);
SELECT 1 / ((:'apply_preflight_prior_revision_number'::integer=1)::integer);

SELECT * FROM memory.apply_claim_assessment_v5(
  :'apply_request'::uuid,:'claim'::uuid,:'review_review_id'::uuid,
  :'apply_preflight_apply_manifest_sha256'
) \gset apply_
SELECT 1 / ((:'apply_outcome'='applied')::integer);
SELECT 1 / ((:'apply_rows_written'::integer=5)::integer);
SELECT 1 / ((:'apply_resulting_revision_number'::integer=2)::integer);

SELECT * FROM memory.apply_claim_assessment_v5(
  :'apply_request'::uuid,:'claim'::uuid,:'review_review_id'::uuid,
  :'apply_preflight_apply_manifest_sha256'
) \gset apply_replay_
SELECT 1 / ((:'apply_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'apply_replay_rows_written'::integer=0)::integer);
SELECT 1 / ((:'apply_replay_event_id'=:'apply_event_id')::integer);

SELECT set_config('app.user_id', :'other_owner', true);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_claim_assessment_review_v5(
      '50ebf1af-b072-4bf9-badc-2df7585f12c6'::uuid,
      'retract'::memory.claim_assessment_action_v5,
      0,1,0,1,
      '["explicit_source_negation","projection_semantic_overreach","replacement_predicate_unavailable"]'::jsonb,
      'The source explicitly says personal training is not done for a living; the occupation projection overstates it.',
      'system','memory_v1_v5_claim_assessment_initial_retraction'
    );
    RAISE EXCEPTION 'cross-owner assessment unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$isolation$;

RESET SESSION AUTHORIZATION;
SELECT 1 / (((
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id=:'owner'::uuid AND claim_id=:'claim'::uuid
    AND status='retracted' AND confidence=0
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.claim_assessment_review_v5
  WHERE owner_user_id=:'owner'::uuid AND claim_id=:'claim'::uuid
    AND review_id=:'review_review_id'::uuid
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.claim_assessment_apply_v5
  WHERE owner_user_id=:'owner'::uuid AND claim_id=:'claim'::uuid
    AND event_id=:'apply_event_id'::uuid
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id=:'owner'::uuid
    AND request_id IN (:'review_request'::uuid,:'apply_request'::uuid)
)=2)::integer);
COMMIT;
RESET SESSION AUTHORIZATION;
\echo review_id=:'review_review_id'
\echo apply_manifest_sha256=:'apply_preflight_apply_manifest_sha256'
\echo event_id=:'apply_event_id'
\echo assessment_id=:'apply_assessment_id'
SQL
chmod 0600 "$apply_log"

phase=postflight
capture_state "$post"
capture_other_owner_state "$other_after"
cmp -s "$other_before" "$other_after" || {
  diff -u "$other_before" "$other_after" >&2 || true
  echo "another owner's Memory V1 rows changed" >&2
  exit 1
}
BASELINE="$baseline" POST="$post" python3 - <<'PY'
import os
from pathlib import Path

def load(name):
    rows = {}
    for line in Path(os.environ[name]).read_text().splitlines():
        table, count, digest = line.split("\t")
        rows[table] = (int(count), digest)
    return rows

before = load("BASELINE")
after = load("POST")
if before.keys() != after.keys():
    raise SystemExit("memory table set changed")
changed = {table for table in before if before[table] != after[table]}
expected = {
    "claim",
    "claim_assessment",
    "claim_assessment_apply_v5",
    "claim_assessment_review_v5",
    "claim_revision",
    "relational_operation_request",
}
if changed != expected:
    raise SystemExit(f"unexpected changed tables: {sorted(changed)}")
deltas = {
    "claim": 0,
    "claim_assessment": 1,
    "claim_assessment_apply_v5": 1,
    "claim_assessment_review_v5": 1,
    "claim_revision": 1,
    "relational_operation_request": 2,
}
for table, delta in deltas.items():
    actual = after[table][0] - before[table][0]
    if actual != delta:
        raise SystemExit(f"{table} row delta {actual}, expected {delta}")
PY

[[ "$(psql_scalar "
  SELECT (
    (SELECT count(*) FROM memory.claim
      WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid
        AND status='retracted' AND confidence=0)=1
    AND
    (SELECT count(*) FROM memory.claim_assessment_review_v5
      WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid)=1
    AND
    (SELECT count(*) FROM memory.claim_assessment_apply_v5
      WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid)=1
  )::int
")" == "1" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]] || {
  echo "Qdrant changed during V5 initial retraction apply" >&2
  exit 1
}

phase=report
report="$snapshot_dir/memory_v1_v5_initial_retraction_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
OTHER_BEFORE="$other_before" OTHER_AFTER="$other_after" LOG="$apply_log" \
REPORT="$report" QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" OWNER="$owner" CLAIM="$claim" \
REVIEW_REQUEST="$review_request" APPLY_REQUEST="$apply_request" \
REVIEW_MANIFEST="$review_manifest" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_v5_initial_retraction_apply_report_v1",
    "instruction_source": "explicit_user_confirmation_required",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": os.environ["OWNER"],
    "claim_id": os.environ["CLAIM"],
    "review_request_id": os.environ["REVIEW_REQUEST"],
    "apply_request_id": os.environ["APPLY_REQUEST"],
    "review_manifest_sha256": os.environ["REVIEW_MANIFEST"],
    "backup": {"path": os.environ["BACKUP"], "catalog": os.environ["CATALOG"]},
    "evidence": {
        "baseline": os.environ["BASELINE"],
        "post": os.environ["POST"],
        "other_owners_before": os.environ["OTHER_BEFORE"],
        "other_owners_after": os.environ["OTHER_AFTER"],
        "apply_log": os.environ["LOG"],
        "qdrant_before_sha256": os.environ["QDRANT_BEFORE"],
        "qdrant_after_sha256": os.environ["QDRANT_AFTER"],
    },
    "checks": {
        "source_overstatement_retracted": True,
        "review_and_apply_hash_locked": True,
        "zero_write_replay": True,
        "cross_owner_rejected": True,
        "other_owner_rows_unchanged": True,
        "qdrant_unchanged": True,
        "replacement_claim_created": False,
        "retrieval_activated": False,
        "prompt_influence": False,
    },
    "hard_stop": "before_corrected_projection_or_live_v5_retrieval",
}
Path(os.environ["REPORT"]).write_text(
    json.dumps(value, indent=2, sort_keys=True) + "\n"
)
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'memory_v1_v5_initial_retraction_production_apply: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Persists one reviewed deterministic V5.1 entailment
# decision for the Memory V1 project-current-state observation. No projection,
# Qdrant, retrieval, prompt, claim, evidence, or cross-owner writes occur.

if [[ "${MEMORY_V1_PROJECT_ENTAILMENT_APPLY:-}" != "authorized" ]]; then
  echo "MEMORY_V1_PROJECT_ENTAILMENT_APPLY=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=532eba132c3956f44d7c4c2700e6ee925fb7a32b
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
observation=258d8d96-2cbd-4296-b878-769c90533fae
observation_sha=0cd39e7738adf6b35e0e9adbd762d1619efedabdf7f393a5560ceb957d300471
evidence=d91edb55-9355-426b-b191-573a55fe7ab2
evidence_sha=ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778
request=19beddb5-e645-52b9-af9b-2dbe74313d9a
manifest=6154ba22e45d7f4bc12a27688fb3f5eb38f9cc5cf0fda1c1e2f2141c71a9e089
spans='[{"start":47,"end":140,"span_sha256":"37333a4515d5b148abb3222e31d7966b56160f112bc20c2ad0847e777b29fafd"}]'
assessor_ref=memory_v1_predicate_entailment_v5_1
container=${MEMORY_V1_DB_CONTAINER:-brains-postgres-1}
database=memory
snapshot_dir=${MEMORY_V1_SNAPSHOT_DIR:-/home/ubuntu/brains/snapshots}
lock_file=${MEMORY_V1_APPLY_LOCK_FILE:-/home/ubuntu/brains/.memory_v1_observation_entailment_v5_1_apply.lock}
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
  echo "another V5.1 entailment apply holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_project_entailment_apply_${run_id}.status"

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
    WHERE table_schema='memory'
      AND table_type='BASE TABLE'
      AND table_name NOT IN (
        'observation_entailment_v5','relational_operation_request'
      )
    ORDER BY table_name
  ")
  printf 'observation_entailment_v5_other\t%s\n' \
    "$(table_state observation_entailment_v5 \
      "observation_id<>'$observation'::uuid")" >>"$output"
  printf 'relational_operation_request_other\t%s\n' \
    "$(table_state relational_operation_request \
      "request_id<>'$request'::uuid")" >>"$output"
  chmod 0600 "$output"
}

capture_other_owner_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    printf '%s\t%s\n' "$table" \
      "$(table_state "$table" "owner_user_id IS DISTINCT FROM '$owner'::uuid")" \
      >>"$output"
  done < <(psql_scalar "
    SELECT table_name
    FROM information_schema.columns
    WHERE table_schema='memory' AND column_name='owner_user_id'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

actor_preflight() {
  docker exec -i "$container" psql -X -q -A -t -F '|' \
    -v ON_ERROR_STOP=1 -U sage -d "$database" \
    -v owner="$owner" -v observation="$observation" -v spans="$spans" \
    -v assessor_ref="$assessor_ref" <<'SQL'
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', true) \gset
SELECT * FROM memory.preflight_observation_entailment_v5(
  :'observation'::uuid,
  'accepted'::memory.observation_entailment_decision_v5,
  'predicate_entailment_v5_1_accepted',:'spans'::jsonb,
  'system',:'assessor_ref'
);
ROLLBACK;
RESET SESSION AUTHORIZATION;
SQL
}

phase=preflight
expected_preflight="$observation|$observation_sha|$evidence|$evidence_sha|memory_v1_predicate_entailment_v5_1|accepted|$manifest"
[[ "$(actor_preflight)" == "$expected_preflight" ]] || {
  echo "project observation entailment preflight drifted" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM memory.observation_entailment_v5
    WHERE owner_user_id='$owner'::uuid
      AND observation_id='$observation'::uuid)=0
  AND
  (SELECT count(*) FROM memory.relational_operation_request
    WHERE owner_user_id='$owner'::uuid
      AND request_id='$request'::uuid)=0
)::int")" == "1" ]]

phase=backup
backup_partial="$snapshot_dir/.memory_pre_project_entailment_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_project_entailment_${run_id}.dump"
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
baseline="$snapshot_dir/memory_v1_project_entailment_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_project_entailment_post_${run_id}.tsv"
other_before="$snapshot_dir/memory_v1_project_entailment_other_before_${run_id}.tsv"
other_after="$snapshot_dir/memory_v1_project_entailment_other_after_${run_id}.tsv"
capture_unchanged_state "$baseline"
capture_other_owner_state "$other_before"
before_decisions=$(psql_scalar "SELECT count(*) FROM memory.observation_entailment_v5")
before_requests=$(psql_scalar "SELECT count(*) FROM memory.relational_operation_request")
qdrant_before=$(qdrant_signature)

phase=transactional_apply
apply_log="$snapshot_dir/memory_v1_project_entailment_apply_${run_id}.log"
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" \
  -v owner="$owner" -v other_owner="$other_owner" \
  -v observation="$observation" -v observation_sha="$observation_sha" \
  -v request="$request" -v manifest="$manifest" -v spans="$spans" \
  -v assessor_ref="$assessor_ref" >"$apply_log" 2>&1 <<'SQL'
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', true);

SELECT * FROM memory.record_observation_entailment_v5(
  :'request'::uuid,:'observation'::uuid,
  'accepted'::memory.observation_entailment_decision_v5,
  'predicate_entailment_v5_1_accepted',:'spans'::jsonb,
  'system',:'assessor_ref',:'manifest'
) \gset applied_
SELECT 1 / ((:'applied_outcome'='applied')::integer);
SELECT 1 / ((:'applied_rows_written'::integer=2)::integer);

SELECT * FROM memory.record_observation_entailment_v5(
  :'request'::uuid,:'observation'::uuid,
  'accepted'::memory.observation_entailment_decision_v5,
  'predicate_entailment_v5_1_accepted',:'spans'::jsonb,
  'system',:'assessor_ref',:'manifest'
) \gset replay_
SELECT 1 / ((:'replay_outcome'='replayed')::integer);
SELECT 1 / ((:'replay_rows_written'::integer=0)::integer);
SELECT 1 / ((:'replay_decision_id'=:'applied_decision_id')::integer);
SELECT 1 / ((memory.observation_entailment_allows_projection_v5(
  :'observation'::uuid,:'observation_sha'
))::integer);

SELECT set_config('app.user_id', :'other_owner', true);
SELECT 1 / ((NOT memory.observation_entailment_allows_projection_v5(
  :'observation'::uuid,:'observation_sha'
))::integer);
DO $cross_owner$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_observation_entailment_v5(
      :'observation'::uuid,
      'accepted'::memory.observation_entailment_decision_v5,
      'predicate_entailment_v5_1_accepted',:'spans'::jsonb,
      'system',:'assessor_ref'
    );
    RAISE EXCEPTION 'cross-owner entailment preflight unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;
SELECT 1 / (((SELECT count(*) FROM memory.observation_entailment_v5
  WHERE owner_user_id=:'owner'::uuid
    AND observation_id=:'observation'::uuid)=1)::integer);
SELECT 1 / (((SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id=:'owner'::uuid
    AND request_id=:'request'::uuid
    AND operation='record_observation_entailment_v5')=1)::integer);
COMMIT;
RESET SESSION AUTHORIZATION;
\echo decision_id=:'applied_decision_id'
SQL
chmod 0600 "$apply_log"

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
  (SELECT count(*) FROM memory.observation_entailment_v5)=$before_decisions+1
  AND
  (SELECT count(*) FROM memory.relational_operation_request)=$before_requests+1
  AND
  (SELECT count(*) FROM memory.observation_entailment_v5
    WHERE owner_user_id='$owner'::uuid
      AND observation_id='$observation'::uuid
      AND observation_sha256='$observation_sha'
      AND evidence_id='$evidence'::uuid
      AND evidence_content_sha256='$evidence_sha'
      AND decision='accepted'
      AND reason_code='predicate_entailment_v5_1_accepted'
      AND authorization_manifest_sha256='$manifest')=1
  AND
  (SELECT count(*) FROM memory.relational_operation_request
    WHERE owner_user_id='$owner'::uuid
      AND request_id='$request'::uuid
      AND operation='record_observation_entailment_v5')=1
)::int")" == "1" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]] || {
  echo "Qdrant changed during project entailment apply" >&2
  exit 1
}

phase=report
report="$snapshot_dir/memory_v1_project_entailment_apply_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
OTHER_BEFORE="$other_before" OTHER_AFTER="$other_after" LOG="$apply_log" \
REPORT="$report" QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" REQUEST="$request" \
OBSERVATION="$observation" MANIFEST="$manifest" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_project_observation_entailment_apply_report_v1",
    "instruction_source": "user_continue_20260717",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "request_id": os.environ["REQUEST"],
    "observation_id": os.environ["OBSERVATION"],
    "authorization_manifest_sha256": os.environ["MANIFEST"],
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
        "observation_entailment_v5": 1,
        "relational_operation_request": 1,
        "all_other_tables": 0,
        "qdrant": 0,
    },
    "checks": {
        "deterministic_policy_accepted": True,
        "exact_replay_zero_write": True,
        "cross_owner_rejected": True,
        "other_owner_rows_unchanged": True,
        "unrelated_memory_rows_unchanged": True,
        "qdrant_unchanged": True,
        "projection_staged": False,
        "retrieval_activated": False,
        "prompt_influence": False,
    },
    "hard_stop": "before_project_projection_staging",
}
Path(os.environ["REPORT"]).write_text(
    json.dumps(value, indent=2, sort_keys=True) + "\n"
)
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_project_observation_entailment_apply: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Persists exactly two reviewed V5.1 observation-entailment
# decisions for one owner. It does not stage/apply projection, modify claims,
# write Qdrant, activate retrieval, alter prompts, or touch another owner.

if [[ "${MEMORY_V1_OBSERVATION_ENTAILMENT_APPLY:-}" != "authorized" ]]; then
  echo "MEMORY_V1_OBSERVATION_ENTAILMENT_APPLY=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=8f158ae9afa405f356ce04850cd92301260e019c
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
occupation_observation=9bf1e6b2-1840-4524-98dc-142567ebe013
occupation_request=42000000-0000-4000-8000-000000000001
occupation_manifest=09e5a9879471aa8de4ef943dac0664bf242d12a747999a71b0c2665aee9e2f97
occupation_spans='[{"start":0,"end":242,"span_sha256":"1b0aabe1bee3fe85ca6c77ffbd9517aa4a38c3a079aa6a6cfe57927aecd59bf8"}]'
correction_observation=93024235-89a8-49d5-88fa-7e4a143b68f3
correction_request=42000000-0000-4000-8000-000000000002
correction_manifest=3eb618da143b84a30ef56dc9c2a809b893caab89e60e4f1086deb9199e315089
correction_spans='[{"start":66,"end":70,"span_sha256":"016526330aaf250542e5acc9103d9f663a8a5bb00d1b8607a1b170b6d93d6401"},{"start":146,"end":159,"span_sha256":"059fef53c30e4fa6b50dd4cd3a086f67259fae08a268d9cf26dfe081782babbc"}]'
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
  echo "another V5.1 observation-entailment apply holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_observation_entailment_v5_1_apply_${run_id}.status"

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
        'observation_entailment_v5',
        'relational_operation_request'
      )
    ORDER BY table_name
  ")
  printf 'observation_entailment_v5_other\t%s\n' \
    "$(table_state observation_entailment_v5 \
      "observation_id NOT IN ('$occupation_observation'::uuid,'$correction_observation'::uuid)")" \
    >>"$output"
  printf 'relational_operation_request_other\t%s\n' \
    "$(table_state relational_operation_request \
      "request_id NOT IN ('$occupation_request'::uuid,'$correction_request'::uuid)")" \
    >>"$output"
  chmod 0600 "$output"
}

capture_other_owner_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(table_state "$table" "owner_user_id IS DISTINCT FROM '$owner'::uuid")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
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
    -v owner="$owner" \
    -v occupation_observation="$occupation_observation" \
    -v occupation_spans="$occupation_spans" \
    -v correction_observation="$correction_observation" \
    -v correction_spans="$correction_spans" \
    -v assessor_ref="$assessor_ref" <<'SQL'
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', true) \gset
SELECT 'occupation',preflight.*
FROM memory.preflight_observation_entailment_v5(
  :'occupation_observation'::uuid,
  'deferred'::memory.observation_entailment_decision_v5,
  'source_contradicts_predicate',:'occupation_spans'::jsonb,
  'system',:'assessor_ref'
) AS preflight;
SELECT 'correction',preflight.*
FROM memory.preflight_observation_entailment_v5(
  :'correction_observation'::uuid,
  'accepted'::memory.observation_entailment_decision_v5,
  'predicate_entailment_v5_1_accepted',:'correction_spans'::jsonb,
  'system',:'assessor_ref'
) AS preflight;
ROLLBACK;
RESET SESSION AUTHORIZATION;
SQL
}

phase=preflight
expected_preflight="occupation|$occupation_observation|8a6ebf42194c1cb1f73edb1db3e1339db5adcc8d455ff0f4c77c1ddabc29501d|fca9e5dc-83c2-4456-8db8-1fe6102eb74d|d41de5228017def6a31b3286cfb4033b8c26a251e73b3c16af67990bfb276de6|memory_v1_predicate_entailment_v5_1|deferred|$occupation_manifest
correction|$correction_observation|28b4cb5db9082ca6b718cb7588a1740b1d9dbb6ddd5486d90758facfe00fb675|36e92633-08fd-441f-8d1d-27f16c2a3479|bd2e59f74888b232822500c0ac4fae12a96f85b1121da4507a0f5efc9b4bb9ce|memory_v1_predicate_entailment_v5_1|accepted|$correction_manifest"
[[ "$(actor_preflight)" == "$expected_preflight" ]] || {
  echo "observation-entailment preflight drifted" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM memory.observation_entailment_v5
    WHERE owner_user_id='$owner'::uuid
      AND observation_id IN (
        '$occupation_observation'::uuid,'$correction_observation'::uuid
      ))=0
  AND
  (SELECT count(*) FROM memory.relational_operation_request
    WHERE owner_user_id='$owner'::uuid
      AND request_id IN (
        '$occupation_request'::uuid,'$correction_request'::uuid
      ))=0
)::int")" == "1" ]]

phase=backup
backup_partial="$snapshot_dir/.memory_pre_observation_entailment_v5_1_apply_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_observation_entailment_v5_1_apply_${run_id}.dump"
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
baseline="$snapshot_dir/memory_v1_observation_entailment_v5_1_apply_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_observation_entailment_v5_1_apply_post_${run_id}.tsv"
other_before="$snapshot_dir/memory_v1_observation_entailment_v5_1_other_before_${run_id}.tsv"
other_after="$snapshot_dir/memory_v1_observation_entailment_v5_1_other_after_${run_id}.tsv"
capture_unchanged_state "$baseline"
capture_other_owner_state "$other_before"
before_decisions=$(psql_scalar "SELECT count(*) FROM memory.observation_entailment_v5")
before_requests=$(psql_scalar "SELECT count(*) FROM memory.relational_operation_request")
qdrant_before=$(qdrant_signature)

phase=transactional_apply
apply_log="$snapshot_dir/memory_v1_observation_entailment_v5_1_apply_${run_id}.log"
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" \
  -v owner="$owner" -v other_owner="$other_owner" \
  -v occupation_observation="$occupation_observation" \
  -v occupation_request="$occupation_request" \
  -v occupation_manifest="$occupation_manifest" \
  -v occupation_spans="$occupation_spans" \
  -v correction_observation="$correction_observation" \
  -v correction_request="$correction_request" \
  -v correction_manifest="$correction_manifest" \
  -v correction_spans="$correction_spans" \
  -v assessor_ref="$assessor_ref" \
  >"$apply_log" 2>&1 <<'SQL'
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', true);

SELECT * FROM memory.record_observation_entailment_v5(
  :'occupation_request'::uuid,:'occupation_observation'::uuid,
  'deferred'::memory.observation_entailment_decision_v5,
  'source_contradicts_predicate',:'occupation_spans'::jsonb,
  'system',:'assessor_ref',:'occupation_manifest'
) \gset occupation_
SELECT 1 / ((:'occupation_outcome'='applied')::integer);
SELECT 1 / ((:'occupation_rows_written'::integer=2)::integer);

SELECT * FROM memory.record_observation_entailment_v5(
  :'occupation_request'::uuid,:'occupation_observation'::uuid,
  'deferred'::memory.observation_entailment_decision_v5,
  'source_contradicts_predicate',:'occupation_spans'::jsonb,
  'system',:'assessor_ref',:'occupation_manifest'
) \gset occupation_replay_
SELECT 1 / ((:'occupation_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'occupation_replay_rows_written'::integer=0)::integer);
SELECT 1 / ((:'occupation_replay_decision_id'
  =:'occupation_decision_id')::integer);
SELECT 1 / ((NOT memory.observation_entailment_allows_projection_v5(
  :'occupation_observation'::uuid,
  '8a6ebf42194c1cb1f73edb1db3e1339db5adcc8d455ff0f4c77c1ddabc29501d'
))::integer);

SELECT * FROM memory.record_observation_entailment_v5(
  :'correction_request'::uuid,:'correction_observation'::uuid,
  'accepted'::memory.observation_entailment_decision_v5,
  'predicate_entailment_v5_1_accepted',:'correction_spans'::jsonb,
  'system',:'assessor_ref',:'correction_manifest'
) \gset correction_
SELECT 1 / ((:'correction_outcome'='applied')::integer);
SELECT 1 / ((:'correction_rows_written'::integer=2)::integer);

SELECT * FROM memory.record_observation_entailment_v5(
  :'correction_request'::uuid,:'correction_observation'::uuid,
  'accepted'::memory.observation_entailment_decision_v5,
  'predicate_entailment_v5_1_accepted',:'correction_spans'::jsonb,
  'system',:'assessor_ref',:'correction_manifest'
) \gset correction_replay_
SELECT 1 / ((:'correction_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'correction_replay_rows_written'::integer=0)::integer);
SELECT 1 / ((:'correction_replay_decision_id'
  =:'correction_decision_id')::integer);
SELECT 1 / ((memory.observation_entailment_allows_projection_v5(
  :'correction_observation'::uuid,
  '28b4cb5db9082ca6b718cb7588a1740b1d9dbb6ddd5486d90758facfe00fb675'
))::integer);

SELECT set_config('app.user_id', :'other_owner', true);
SELECT 1 / ((NOT memory.observation_entailment_allows_projection_v5(
  :'correction_observation'::uuid,
  '28b4cb5db9082ca6b718cb7588a1740b1d9dbb6ddd5486d90758facfe00fb675'
))::integer);
DO $cross_owner$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_observation_entailment_v5(
      '93024235-89a8-49d5-88fa-7e4a143b68f3'::uuid,
      'accepted'::memory.observation_entailment_decision_v5,
      'predicate_entailment_v5_1_accepted',
      '[{"start":66,"end":70,"span_sha256":"016526330aaf250542e5acc9103d9f663a8a5bb00d1b8607a1b170b6d93d6401"},{"start":146,"end":159,"span_sha256":"059fef53c30e4fa6b50dd4cd3a086f67259fae08a268d9cf26dfe081782babbc"}]'::jsonb,
      'system','memory_v1_predicate_entailment_v5_1'
    );
    RAISE EXCEPTION 'cross-owner entailment preflight unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;
SELECT 1 / (((
  SELECT count(*) FROM memory.observation_entailment_v5
  WHERE owner_user_id=:'owner'::uuid
    AND observation_id IN (
      :'occupation_observation'::uuid,:'correction_observation'::uuid
    )
)=2)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id=:'owner'::uuid
    AND request_id IN (
      :'occupation_request'::uuid,:'correction_request'::uuid
    )
    AND operation='record_observation_entailment_v5'
)=2)::integer);
COMMIT;
RESET SESSION AUTHORIZATION;
\echo occupation_decision_id=:'occupation_decision_id'
\echo correction_decision_id=:'correction_decision_id'
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
  (SELECT count(*) FROM memory.observation_entailment_v5)
    =$before_decisions+2
  AND
  (SELECT count(*) FROM memory.relational_operation_request)
    =$before_requests+2
  AND
  (SELECT count(*) FROM memory.observation_entailment_v5
    WHERE owner_user_id='$owner'::uuid
      AND observation_id='$occupation_observation'::uuid
      AND decision='deferred'
      AND reason_code='source_contradicts_predicate'
      AND authorization_manifest_sha256='$occupation_manifest')=1
  AND
  (SELECT count(*) FROM memory.observation_entailment_v5
    WHERE owner_user_id='$owner'::uuid
      AND observation_id='$correction_observation'::uuid
      AND decision='accepted'
      AND reason_code='predicate_entailment_v5_1_accepted'
      AND authorization_manifest_sha256='$correction_manifest')=1
  AND
  (SELECT count(*) FROM memory.relational_operation_request
    WHERE owner_user_id='$owner'::uuid
      AND request_id IN (
        '$occupation_request'::uuid,'$correction_request'::uuid
      )
      AND operation='record_observation_entailment_v5')=2
)::int")" == "1" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]] || {
  echo "Qdrant changed during V5.1 entailment apply" >&2
  exit 1
}

phase=report
report="$snapshot_dir/memory_v1_observation_entailment_v5_1_apply_${run_id}.json"
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
OTHER_BEFORE="$other_before" OTHER_AFTER="$other_after" LOG="$apply_log" \
REPORT="$report" QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

value = {
    "contract_version": "memory_v1_observation_entailment_v5_1_apply_report_v1",
    "instruction_source": "user_continue_20260716",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "backup": {
        "path": os.environ["BACKUP"],
        "catalog": os.environ["CATALOG"],
    },
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
        "observation_entailment_v5": 2,
        "relational_operation_request": 2,
        "all_other_tables": 0,
        "qdrant": 0,
    },
    "checks": {
        "occupation_deferred": True,
        "correction_accepted": True,
        "exact_replay_zero_write": True,
        "cross_owner_rejected": True,
        "other_owner_rows_unchanged": True,
        "unrelated_memory_rows_unchanged": True,
        "qdrant_unchanged": True,
        "projection_staged": False,
        "retrieval_activated": False,
        "prompt_influence": False,
        "frontend_modified": False,
    },
    "hard_stop": "before_claim_cleanup_or_projection",
}
Path(os.environ["REPORT"]).write_text(
    json.dumps(value, indent=2, sort_keys=True) + "\n"
)
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_observation_entailment_v5_1_initial_apply: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"

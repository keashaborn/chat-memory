#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable database and
# verifies future chat capture -> contextual spans -> extraction jobs.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-/opt/chat-memory/.env}"
set +a

container=brains-postgres-1
production=memory
clone="memory_contextual_bridge_v1_${$}"
migration=ops/sql/20260730_memory_v1_contextual_chat_capture_bridge_v1.sql
rollback=ops/sql/20260730_memory_v1_contextual_chat_capture_bridge_v1_rollback.sql
capture=scripts/memory_v1_v5_chat_capture.py
bridge=scripts/memory_v1_contextual_evidence_intake_dispatcher_v3.py
python_bin=/opt/chat-memory/venv/bin/python
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=673d64a3-c4ba-4d1c-89e3-e0c579022fad

backup=$(mktemp /tmp/contextual-bridge-v1.XXXXXX.dump)
capture_report=$(mktemp /tmp/contextual-bridge-capture.XXXXXX.json)
bridge_report=$(mktemp /tmp/contextual-bridge-dispatch.XXXXXX.json)
replay_report=$(mktemp /tmp/contextual-bridge-replay.XXXXXX.json)
bad_bridge_output=$(mktemp /tmp/contextual-bridge-bad-dispatch.XXXXXX.log)
chmod 0600 \
  "$backup" "$capture_report" "$bridge_report" "$replay_report" \
  "$bad_bridge_output"

cleanup() {
  rc=$?
  trap - EXIT
  sudo -n docker exec "$container" \
    dropdb -U sage --if-exists --force "$clone" >/dev/null 2>&1 || true
  rm -f \
    "$backup" "$capture_report" "$bridge_report" "$replay_report" \
    "$bad_bridge_output"
  exit "$rc"
}
trap cleanup EXIT

for file in "$migration" "$rollback" "$capture" "$bridge"; do
  test -f "$file"
done

clone_scalar() {
  sudo -n docker exec "$container" psql -U sage -d "$clone" \
    -X -Atq -v ON_ERROR_STOP=1 -c "$1"
}

production_signature() {
  sudo -n docker exec "$container" psql -U sage -d "$production" \
    -X -Atq -v ON_ERROR_STOP=1 -c "
      SELECT concat_ws('|',
        (SELECT count(*) FROM memory.evidence),
        (SELECT count(*) FROM memory.evidence_contextual_span_v2),
        (SELECT count(*) FROM memory.evidence_intake_terminal),
        (SELECT count(*) FROM memory.evidence_extraction_job),
        (SELECT count(*) FROM memory.evidence_extraction_packet_v5_local),
        (SELECT count(*) FROM memory.claim)
      )
    "
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

production_before=$(production_signature)
qdrant_before=$(qdrant_signature)
sudo -n docker exec "$container" pg_dump -U sage -d "$production" -Fc \
  >"$backup"
test -s "$backup"
sudo -n docker exec "$container" createdb -U sage -T template0 "$clone"
sudo -n docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

clone_dsn=$(
  "$python_bin" -c \
    'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
    "$POSTGRES_DSN" "$clone"
)

sudo -n docker exec -i "$container" psql -U sage -d "$clone" \
  -X -v ON_ERROR_STOP=1 <"$migration" >/dev/null
test "$(clone_scalar "
  SELECT to_regprocedure(
    'memory.plan_owner_contextual_chat_capture_v1(text,integer)'
  ) IS NOT NULL
")" = t

sudo -n docker exec -i "$container" psql -U sage -d "$clone" \
  -X -v ON_ERROR_STOP=1 <"$rollback" >/dev/null
test "$(clone_scalar "
  SELECT to_regprocedure(
    'memory.plan_owner_contextual_chat_capture_v1(text,integer)'
  ) IS NULL
")" = t
test "$(clone_scalar "
  SELECT NOT has_table_privilege(
    'memory_context_rebind_maintainer',
    'memory.evidence_contextual_span_v2','SELECT'
  ) AND NOT has_table_privilege(
    'memory_context_rebind_maintainer',
    'memory.evidence_intake_terminal','SELECT'
  )
")" = t

sudo -n docker exec -i "$container" psql -U sage -d "$clone" \
  -X -v ON_ERROR_STOP=1 <"$migration" >/dev/null

thread=$(
  clone_scalar "
    SELECT id
    FROM public.threads
    WHERE owner_user_id='$owner'::uuid
    ORDER BY created_at,id
    LIMIT 1
  "
)
test -n "$thread"
source_id=$("$python_bin" -c 'import uuid; print(uuid.uuid4())')
request_id=$("$python_bin" -c 'import uuid; print(uuid.uuid4())')
text='My sister Rowan lives nearby. She enjoys gardening.'
clone_scalar "
  INSERT INTO public.chat_log(
    id,user_id,source,text,tags,created_at,thread_id,request_id,owner_user_id
  ) VALUES (
    '$source_id'::uuid,'$owner','frontend/chat:user','$text',
    ARRAY['contextual-bridge-clone'],clock_timestamp(),
    '$thread'::uuid,'$request_id','$owner'::uuid
  )
  RETURNING id
" >/dev/null

POSTGRES_DSN="$clone_dsn" \
MEMORY_V1_V5_CHAT_CAPTURE_APPLY=enabled \
PYTHONPATH="$repo_root" "$python_bin" "$capture" \
  --owner-user-id "$owner" --limit 20 --apply \
  --report-path "$capture_report" >/dev/null
test "$(jq -r '.owners[0].planned_count' "$capture_report")" -eq 1

POSTGRES_DSN="$clone_dsn" \
MEMORY_V1_CONTEXTUAL_INTAKE_APPLY=memory_v1_contextual_evidence_intake_apply_v3 \
PYTHONPATH="$repo_root" "$python_bin" "$bridge" \
  --owner-user-id "$owner" --limit 20 --apply \
  --report-path "$bridge_report" >/dev/null
POSTGRES_DSN="$clone_dsn" \
MEMORY_V1_CONTEXTUAL_INTAKE_APPLY=memory_v1_contextual_evidence_intake_apply_v3 \
PYTHONPATH="$repo_root" "$python_bin" "$bridge" \
  --owner-user-id "$owner" --limit 20 --apply \
  --report-path "$replay_report" >/dev/null

parent=$(
  clone_scalar "
    SELECT evidence_id
    FROM memory.evidence
    WHERE owner_user_id='$owner'::uuid
      AND source_system='public.chat_log'
      AND external_id='$source_id'
  "
)
test -n "$parent"
spans=$(
  clone_scalar "
    SELECT count(*)
    FROM memory.evidence_contextual_span_v2
    WHERE owner_user_id='$owner'::uuid
      AND parent_evidence_id='$parent'::uuid
  "
)
test "$spans" -eq 2
test "$(clone_scalar "
  SELECT count(*)
  FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id='$parent'::uuid
")" -eq 0
child_jobs=$(
  clone_scalar "
    SELECT count(*)
    FROM memory.evidence_contextual_span_v2 AS span
    JOIN memory.evidence_extraction_job AS job
      ON job.owner_user_id=span.owner_user_id
     AND job.evidence_id=span.child_evidence_id
    WHERE span.owner_user_id='$owner'::uuid
      AND span.parent_evidence_id='$parent'::uuid
      AND job.status='pending'
  "
)
test "$child_jobs" -eq 2
test "$(clone_scalar "
  SELECT count(*)
  FROM memory.evidence_intake_terminal
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id='$parent'::uuid
    AND selector_version='20260729_v4_contextual_resplit'
    AND outcome='skipped'
    AND reason_code='contextual_split_parent'
")" -eq 1
test "$(jq -r '.owners[0].rows' "$bridge_report")" -eq 1
test "$(jq -r '.owners[0].rows' "$replay_report")" -eq 0
test "$(jq -r '.model_calls' "$bridge_report")" -eq 0
test "$(jq -r '.claim_writes' "$bridge_report")" -eq 0
test "$(jq -r '.qdrant_writes' "$bridge_report")" -eq 0
test "$(jq -r '.prompt_influence' "$bridge_report")" -eq 0

cross=$(
  clone_scalar "
    SET SESSION AUTHORIZATION brains_app;
    SELECT set_config('app.user_id','$other_owner',false);
    SELECT count(*) FROM memory.evidence
    WHERE evidence_id='$parent'::uuid
  " | tail -1
)
test "$cross" -eq 0

bad_source_id=$("$python_bin" -c 'import uuid; print(uuid.uuid4())')
bad_request_id=$("$python_bin" -c 'import uuid; print(uuid.uuid4())')
bad_content='My brother Taylor lives nearby.'
bad_sha=$(printf '%s' "$bad_content" | sha256sum | awk '{print $1}')
bad_parent=$(
  clone_scalar "
    SET SESSION AUTHORIZATION brains_app;
    SELECT set_config('app.user_id','$owner',false);
    SELECT evidence_id
    FROM memory.record_owner_evidence_v1(
      'user_statement'::memory.evidence_kind,
      'public.chat_log',
      '$bad_source_id',
      '$bad_content',
      clock_timestamp(),
      1,1,
      'contextual-bridge-clone:$thread',
      'high'::memory.sensitivity_level,
      jsonb_build_object(
        'capture_version',
          'memory_v1_v5_chat_capture_20260730_v2_contextual',
        'source_type','frontend/chat:user',
        'source_id','$bad_source_id',
        'source_external_id','$bad_source_id',
        'source_content_sha256','$bad_sha',
        'source_char_start',0,
        'source_char_end',length('$bad_content'),
        'thread_id','$thread',
        'request_id','$bad_request_id',
        'primary_lane','unclassified_user_statement',
        'epistemic_role','user_report_unclassified',
        'span_origin','raw_chat_turn_v1',
        'context_needed',false,
        'semantic_processing','pending'
      )
    )
  " | tail -1
)
test -n "$bad_parent"
test "$(clone_scalar "
  SET SESSION AUTHORIZATION brains_app;
  SELECT set_config('app.user_id','$owner',false);
  SELECT source_bound
  FROM memory.plan_owner_contextual_chat_capture_v1(
    '20260729_v4_contextual_resplit',20
  )
  WHERE evidence_id='$bad_parent'::uuid
" | tail -1)" = f
set +e
POSTGRES_DSN="$clone_dsn" \
MEMORY_V1_CONTEXTUAL_INTAKE_APPLY=memory_v1_contextual_evidence_intake_apply_v3 \
PYTHONPATH="$repo_root" "$python_bin" "$bridge" \
  --owner-user-id "$owner" --limit 20 --apply \
  --report-path /tmp/contextual-bridge-should-not-exist.json \
  >"$bad_bridge_output" 2>&1
bad_rc=$?
set -e
test "$bad_rc" -ne 0
grep -q 'contextual split raw source binding is invalid' "$bad_bridge_output"
test "$(clone_scalar "
  SELECT count(*) FROM memory.evidence_contextual_span_v2
  WHERE owner_user_id='$owner'::uuid
    AND parent_evidence_id='$bad_parent'::uuid
")" -eq 0

test "$(production_signature)" = "$production_before"
test "$(qdrant_signature)" = "$qdrant_before"
printf '%s\n' \
  'memory_v1_contextual_chat_capture_bridge_v1_clone: PASS' \
  'captured_parents=1' \
  "contextual_spans=$spans" \
  "child_jobs=$child_jobs" \
  'parent_jobs=0' \
  'replay_rows=0' \
  'cross_owner_visible=0' \
  'tampered_source_rejected=true' \
  'model_calls=0' \
  'claim_writes=0' \
  'qdrant_writes=0' \
  'prompt_influence=0' \
  'production_unchanged=true' \
  'clone_removed_on_exit=true'

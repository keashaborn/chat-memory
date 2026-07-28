#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable database, verifies
# the contextual intake schema, and atomizes exactly two synthetic chat turns.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-/opt/chat-memory/.env}"
set +a

container=brains-postgres-1
production=memory
clone="memory_contextual_intake_v2_${$}"
migration=ops/sql/20260728_memory_v1_contextual_intake_v2.sql
rollback=ops/sql/20260728_memory_v1_contextual_intake_v2_rollback.sql
security_test=tests/memory_v1_contextual_intake_v2_security.sql
verifier=tests/memory_v1_contextual_intake_v2_clone_verify.py
context_verifier=tests/memory_v1_evidence_context_v2_clone_verify.py
dispatcher=scripts/memory_v1_contextual_evidence_intake_dispatcher_v2.py
python_bin=/opt/chat-memory/venv/bin/python
backup=$(mktemp /tmp/memory-contextual-intake-v2.XXXXXX.dump)
table_list=$(mktemp /tmp/memory-contextual-intake-v2-tables.XXXXXX)
protected_before=$(mktemp /tmp/memory-contextual-intake-v2-before.XXXXXX)
protected_after=$(mktemp /tmp/memory-contextual-intake-v2-after.XXXXXX)
seed_report=$(mktemp /tmp/memory-contextual-intake-v2-seed.XXXXXX.json)
dispatch_report=$(mktemp /tmp/memory-contextual-intake-v2-dispatch.XXXXXX.json)
verify_report=$(mktemp /tmp/memory-contextual-intake-v2-verify.XXXXXX.json)
context_report=$(mktemp /tmp/memory-contextual-intake-v2-context.XXXXXX.json)
chmod 0600 \
  "$backup" "$table_list" "$protected_before" "$protected_after" \
  "$seed_report" "$dispatch_report" "$verify_report" "$context_report"

cleanup() {
  rc=$?
  trap - EXIT
  sudo -n docker exec "$container" \
    dropdb -U sage --if-exists --force "$clone" >/dev/null 2>&1 || true
  rm -f \
    "$backup" "$table_list" "$protected_before" "$protected_after" \
    "$seed_report" "$dispatch_report" "$verify_report" "$context_report"
  exit "$rc"
}
trap cleanup EXIT

for file in \
  "$migration" "$rollback" "$security_test" "$verifier" \
  "$context_verifier" "$dispatcher"
do
  test -f "$file"
done

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

clone_scalar() {
  sudo -n docker exec "$container" psql -U sage -d "$clone" \
    -X -Atqc "$1"
}

capture_protected() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    test -n "$table"
    state=$(clone_scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
          FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
}

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

clone_scalar "
  SELECT table_name
    FROM information_schema.tables
   WHERE table_schema='memory'
     AND table_type='BASE TABLE'
     AND table_name NOT IN (
       'evidence',
       'evidence_intake_terminal',
       'evidence_extraction_job',
       'evidence_extraction_event',
       'evidence_contextual_span_v2'
     )
   ORDER BY table_name
" >"$table_list"
test -s "$table_list"
capture_protected "$protected_before"
qdrant_before=$(qdrant_signature)

evidence_before=$(clone_scalar "SELECT count(*) FROM memory.evidence")
terminal_before=$(
  clone_scalar "SELECT count(*) FROM memory.evidence_intake_terminal"
)
job_before=$(
  clone_scalar "SELECT count(*) FROM memory.evidence_extraction_job"
)
event_before=$(
  clone_scalar "SELECT count(*) FROM memory.evidence_extraction_event"
)

sudo -n docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
psql "$clone_dsn" -X -v ON_ERROR_STOP=1 <"$security_test" >/dev/null

sudo -n docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$rollback" >/dev/null
test "$(clone_scalar "
  SELECT to_regclass('memory.evidence_contextual_span_v2') IS NULL
    AND to_regprocedure(
      'memory.plan_owner_evidence_intake_v2(text,integer,uuid)'
    ) IS NULL
")" = t

sudo -n docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
psql "$clone_dsn" -X -v ON_ERROR_STOP=1 <"$security_test" >/dev/null
capture_protected "$protected_after"
cmp -s "$protected_before" "$protected_after"

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" "$python_bin" \
  "$verifier" --phase seed --report-path "$seed_report" >/dev/null

mapfile -t evidence_ids < <(
  jq -r '.fixtures[].evidence_id' "$seed_report"
)
test "${#evidence_ids[@]}" -eq 2

POSTGRES_DSN="$clone_dsn" \
MEMORY_V1_CONTEXTUAL_INTAKE_APPLY=memory_v1_contextual_evidence_intake_apply_v2 \
PYTHONPATH="$repo_root" "$python_bin" "$dispatcher" \
  --owner-user-id 1240822d-ac9a-4096-95aa-e2b24d36ef50 \
  --evidence-id "${evidence_ids[0]}" \
  --evidence-id "${evidence_ids[1]}" \
  --selector-version 20260728_v3_contextual \
  --limit 2 \
  --apply \
  --report-path "$dispatch_report" >/dev/null

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" "$python_bin" \
  "$verifier" --phase verify --report-path "$verify_report" >/dev/null
POSTGRES_DSN="$clone_dsn" REPORT_PATH="$context_report" \
PYTHONPATH="$repo_root" "$python_bin" "$context_verifier" >/dev/null

span_count=$(jq -r '.span_count' "$verify_report")
test "$span_count" -ge 3
test "$(clone_scalar "
  SELECT count(*) FROM memory.evidence_contextual_span_v2
   WHERE owner_user_id=
     '1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
")" -eq "$span_count"
test "$(clone_scalar "SELECT count(*) FROM memory.evidence")" \
  -eq "$((evidence_before + 2 + span_count))"
test "$(clone_scalar "
  SELECT count(*) FROM memory.evidence_intake_terminal
")" -eq "$((terminal_before + 2 + span_count))"
test "$(clone_scalar "
  SELECT count(*) FROM memory.evidence_extraction_job
")" -eq "$((job_before + span_count))"
test "$(clone_scalar "
  SELECT count(*) FROM memory.evidence_extraction_event
")" -eq "$((event_before + span_count))"

capture_protected "$protected_after"
cmp -s "$protected_before" "$protected_after"
qdrant_after=$(qdrant_signature)
test "$qdrant_after" = "$qdrant_before"

test "$(jq -r '.model_calls' "$dispatch_report")" -eq 0
test "$(jq -r '.claim_writes' "$dispatch_report")" -eq 0
test "$(jq -r '.qdrant_writes' "$dispatch_report")" -eq 0
test "$(jq -r '.prompt_influence' "$dispatch_report")" -eq 0
test "$(jq -r '.cross_owner_rejected' "$context_report")" = true
test "$(jq -r '.assertion_origin_count' "$context_report")" -eq 1
test "$(jq -r '.prior_turn_assertion_origin_count' "$context_report")" -eq 0
test "$(jq -r '.model_calls' "$context_report")" -eq 0
test "$(jq -r '.claim_writes' "$context_report")" -eq 0
test "$(jq -r '.qdrant_writes' "$context_report")" -eq 0
test "$(jq -r '.prompt_influence' "$context_report")" = false

printf '%s\n' \
  "memory_v1_contextual_intake_v2_clone: PASS" \
  "span_count=$span_count" \
  "context_needed_count=$(jq -r '.context_needed_count' "$verify_report")" \
  "queued_child_count=$(jq -r '.queued_child_count' "$verify_report")" \
  "prior_turn_count=$(jq -r '.prior_turn_count' "$context_report")" \
  "qdrant_unchanged=true" \
  "protected_tables_unchanged=true" \
  "cross_owner_visible=0" \
  "clone_removed_on_exit=true"

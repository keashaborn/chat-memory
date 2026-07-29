#!/usr/bin/env bash
set -euo pipefail

repo=${REPO_ROOT:-/tmp/chat-memory-context-generation-loader-v1}
production_repo=/opt/chat-memory
container=brains-postgres-1
source_db=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=673d64a3-c4ba-4d1c-89e3-e0c579022fad
source_selector=20260729_v4_contextual_resplit
retry_selector=20260729_v5_context_generation_retry_v1
expected_count=22
clone_db="memory_context_generation_clone_$(date -u +%Y%m%dT%H%M%SZ)_$$"
artifact_dir=$(mktemp -d /tmp/memory-context-generation-clone.XXXXXX)
migration=$repo/ops/sql/20260729_memory_v1_context_generation_selector_v1.sql
rollback=$repo/ops/sql/20260729_memory_v1_context_generation_selector_v1_rollback.sql
selector_test=$repo/tests/memory_v1_context_generation_selector_v1.sql
retry_test=$repo/tests/memory_v1_context_generation_retry_v1.sql
worker=$repo/scripts/memory_v1_context_generation_retry_v1.py
diagnostic=$repo/scripts/memory_v1_context_generation_loader_diagnostic_v1.py
python_bin=/opt/chat-memory/venv/bin/python

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
  rm -rf "$artifact_dir"
}
trap cleanup EXIT

test "$(id -u)" -eq 0
test -z "$(git -C "$production_repo" status --porcelain)"
test -z "$(git -C "$repo" status --porcelain)"
test "$(systemctl is-active brains.service)" = active
test -f "$migration"
test -f "$rollback"
test -f "$selector_test"
test -f "$retry_test"
test -x "$worker"
test -x "$diagnostic"

set -a
. "$production_repo/.env"
set +a
test -n "${POSTGRES_DSN:-}"

docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc |
  docker exec -i "$container" pg_restore -U sage -d "$clone_db"

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" \
  "$python_bin" - <<'PY'
from urllib.parse import urlsplit, urlunsplit
import os

source = urlsplit(os.environ["SOURCE_DSN"])
if source.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("source DSN scheme is invalid")
if source.hostname not in {"127.0.0.1", "::1", "localhost"}:
    raise SystemExit("source DSN is not loopback")
print(urlunsplit((
    source.scheme,
    source.netloc,
    "/" + os.environ["CLONE_DB"],
    source.query,
    source.fragment,
)))
PY
)

schema_apply() {
  docker exec -i "$container" psql -X -U sage -d "$clone_db" \
    -v ON_ERROR_STOP=1 <"$migration" >/dev/null
}

schema_rollback() {
  docker exec -i "$container" psql -X -U sage -d "$clone_db" \
    -v ON_ERROR_STOP=1 <"$rollback" >/dev/null
}

query_clone() {
  docker exec "$container" psql -X -U sage -d "$clone_db" "$@"
}

protected_snapshot() {
  local output=$1
  docker exec -i "$container" psql -X -U sage -d "$clone_db" \
    -Atq -v ON_ERROR_STOP=1 >"$output" <<'SQL'
CREATE TEMP TABLE protected_state(
  table_name text PRIMARY KEY,
  row_count bigint NOT NULL,
  content_sha256 text NOT NULL
);
DO $snapshot$
DECLARE
  item record;
  item_count bigint;
  item_sha text;
BEGIN
  FOR item IN
    SELECT namespace.nspname AS schema_name,class.relname AS table_name
    FROM pg_class AS class
    JOIN pg_namespace AS namespace ON namespace.oid=class.relnamespace
    WHERE namespace.nspname='memory'
      AND class.relkind IN ('r','p')
      AND class.relname NOT IN (
        'evidence_extraction_job',
        'evidence_intake_terminal',
        'evidence_extraction_event'
      )
    ORDER BY class.relname
  LOOP
    EXECUTE format('SELECT count(*) FROM %I.%I',
                   item.schema_name,item.table_name)
      INTO item_count;
    EXECUTE format(
      $format$
      SELECT encode(
        public.digest(
          convert_to(
            coalesce(
              jsonb_agg(to_jsonb(value) ORDER BY to_jsonb(value)::text)::text,
              '[]'
            ),
            'UTF8'
          ),
          'sha256'
        ),
        'hex'
      )
      FROM %I.%I AS value
      $format$,
      item.schema_name,item.table_name
    ) INTO item_sha;
    INSERT INTO protected_state VALUES (
      item.table_name,item_count,item_sha
    );
  END LOOP;
END
$snapshot$;
SELECT table_name||E'\t'||row_count||E'\t'||content_sha256
FROM protected_state
ORDER BY table_name;
SQL
}

schema_apply

IFS=$'\t' read -r source_job target_evidence target_hash target_parent \
  target_splitter target_plan generation_count < <(
    query_clone -At -F $'\t' -v ON_ERROR_STOP=1 -c "
      SELECT job.job_id,
             job.evidence_id,
             job.evidence_content_sha256,
             span.parent_evidence_id,
             span.splitter_version,
             span.plan_sha256,
             (
               SELECT count(*)
               FROM memory.evidence_contextual_span_v2 AS sibling
               WHERE sibling.owner_user_id=span.owner_user_id
                 AND sibling.parent_evidence_id=span.parent_evidence_id
                 AND sibling.source_id=span.source_id
                 AND sibling.source_content_sha256=
                       span.source_content_sha256
                 AND sibling.splitter_version=span.splitter_version
                 AND sibling.plan_sha256=span.plan_sha256
                 AND sibling.span_origin=span.span_origin
             )
      FROM memory.evidence_extraction_job AS job
      JOIN memory.evidence_contextual_span_v2 AS span
        ON span.owner_user_id=job.owner_user_id
       AND span.child_evidence_id=job.evidence_id
      WHERE job.owner_user_id='$owner'::uuid
        AND job.selector_version='$source_selector'
      ORDER BY job.evidence_id,job.job_id
      LIMIT 1;
    "
  )

test -n "$source_job"
test "$generation_count" -ge 1
test "$generation_count" -le 32

query_clone -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v target_evidence="$target_evidence" \
  -v expected_count="$generation_count" \
  -v expected_splitter="$target_splitter" \
  -v expected_plan_sha256="$target_plan" \
  -v other_owner="$other_owner" \
  -f /dev/stdin <"$selector_test" >/dev/null

query_clone -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v source_job_id="$source_job" \
  -v target_evidence="$target_evidence" \
  -v content_sha256="$target_hash" \
  -v other_owner="$other_owner" \
  -f /dev/stdin <"$retry_test" >/dev/null

schema_rollback
test "$(
  query_clone -Atq -c "
    SELECT count(*)
    FROM pg_proc
    WHERE oid IN (
      to_regprocedure(
        'memory.select_owner_contextual_generation_v1(uuid,integer)'
      ),
      to_regprocedure(
        'memory.plan_owner_context_generation_retry_v1(uuid)'
      ),
      to_regprocedure(
        'memory.enqueue_owner_context_generation_retry_v1(uuid,uuid,uuid,uuid,text,text)'
      )
    );
  "
)" = 0

schema_apply
query_clone -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v target_evidence="$target_evidence" \
  -v expected_count="$generation_count" \
  -v expected_splitter="$target_splitter" \
  -v expected_plan_sha256="$target_plan" \
  -v other_owner="$other_owner" \
  -f /dev/stdin <"$selector_test" >/dev/null
query_clone -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v source_job_id="$source_job" \
  -v target_evidence="$target_evidence" \
  -v content_sha256="$target_hash" \
  -v other_owner="$other_owner" \
  -f /dev/stdin <"$retry_test" >/dev/null

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" "$python_bin" "$diagnostic" \
  --owner-user-id "$owner" \
  --selector-version "$source_selector" \
  --expected-count "$expected_count" \
  >"$artifact_dir/source-loader-diagnostic.json"

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" "$python_bin" "$diagnostic" \
  --owner-user-id "$other_owner" \
  --selector-version "$source_selector" \
  --expected-count 0 \
  >"$artifact_dir/cross-owner-loader-diagnostic.json"

protected_snapshot "$artifact_dir/protected-before.tsv"
before_jobs=$(query_clone -Atq -c \
  "SELECT count(*) FROM memory.evidence_extraction_job;")
before_terminals=$(query_clone -Atq -c \
  "SELECT count(*) FROM memory.evidence_intake_terminal;")
before_events=$(query_clone -Atq -c \
  "SELECT count(*) FROM memory.evidence_extraction_event;")

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" "$python_bin" "$worker" \
  --owner-user-id "$owner" \
  --expected-count "$expected_count" \
  --report-path "$artifact_dir/retry-dry.json" \
  >"$artifact_dir/retry-dry.out"
plan_sha=$("$python_bin" -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["plan_sha256"])' \
  "$artifact_dir/retry-dry.json")

MEMORY_V1_CONTEXT_GENERATION_RETRY_APPLY=memory_v1_context_generation_retry_apply_v1 \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" "$python_bin" "$worker" \
  --owner-user-id "$owner" \
  --expected-count "$expected_count" \
  --expected-plan-sha256 "$plan_sha" \
  --apply \
  --report-path "$artifact_dir/retry-apply.json" \
  >"$artifact_dir/retry-apply.out"

test "$(
  query_clone -Atq -c "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND selector_version='$retry_selector'
      AND status='pending'
      AND attempts=0;
  "
)" = "$expected_count"
test "$(
  query_clone -Atq -c "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND selector_version='$source_selector'
      AND status='skipped'
      AND attempts=1
      AND result #>> '{final,payload,reason_code}'='context_missing';
  "
)" = "$expected_count"
post_apply_plan_count=$(
  psql "$clone_dsn" -X -Atq -v ON_ERROR_STOP=1 <<SQL | tail -n 1
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT count(*)
FROM memory.plan_owner_context_generation_retry_v1(NULL);
ROLLBACK;
SQL
)
test "$post_apply_plan_count" = 0

after_jobs=$(query_clone -Atq -c \
  "SELECT count(*) FROM memory.evidence_extraction_job;")
after_terminals=$(query_clone -Atq -c \
  "SELECT count(*) FROM memory.evidence_intake_terminal;")
after_events=$(query_clone -Atq -c \
  "SELECT count(*) FROM memory.evidence_extraction_event;")
test "$after_jobs" -eq "$((before_jobs + expected_count))"
test "$after_terminals" -eq "$((before_terminals + expected_count))"
test "$after_events" -eq "$((before_events + expected_count))"

protected_snapshot "$artifact_dir/protected-after.tsv"
cmp "$artifact_dir/protected-before.tsv" "$artifact_dir/protected-after.tsv"

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" "$python_bin" "$diagnostic" \
  --owner-user-id "$owner" \
  --selector-version "$retry_selector" \
  --expected-count "$expected_count" \
  >"$artifact_dir/retry-loader-diagnostic.json"

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" "$python_bin" "$diagnostic" \
  --owner-user-id "$other_owner" \
  --selector-version "$retry_selector" \
  --expected-count 0 \
  >"$artifact_dir/cross-owner-retry-diagnostic.json"

"$python_bin" - "$artifact_dir" "$plan_sha" <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
plan_sha = sys.argv[2]
source = json.loads((root / "source-loader-diagnostic.json").read_text())
retry = json.loads((root / "retry-loader-diagnostic.json").read_text())
cross_source = json.loads(
    (root / "cross-owner-loader-diagnostic.json").read_text()
)
cross_retry = json.loads(
    (root / "cross-owner-retry-diagnostic.json").read_text()
)
applied = json.loads((root / "retry-apply.json").read_text())
assert source["outcome"] == "pass"
assert source["job_count"] == 22
assert source["span_origin_counts"].keys() == {"contextual_split_v3"}
assert retry["outcome"] == "pass"
assert retry["job_count"] == 22
assert retry["envelope_set_sha256"] == source["envelope_set_sha256"]
assert cross_source["job_count"] == 0
assert cross_retry["job_count"] == 0
assert applied["outcome"] == "queued"
assert applied["plan_sha256"] == plan_sha
assert applied["write_counts"] == {
    "events": 22,
    "jobs": 22,
    "terminals": 22,
}
assert applied["zero_write_replay_proved"] is True
print(json.dumps({
    "outcome": "pass",
    "source_job_count": source["job_count"],
    "retry_job_count": retry["job_count"],
    "generation_origin_counts": source["span_origin_counts"],
    "span_count_distribution": source["span_count_distribution"],
    "envelope_set_sha256": source["envelope_set_sha256"],
    "retry_plan_sha256": plan_sha,
    "append_only_write_counts": applied["write_counts"],
    "cross_owner_visible": 0,
    "model_calls": 0,
    "claims": 0,
    "qdrant": 0,
    "prompt_influence": 0,
    "protected_store_unchanged": True,
    "zero_write_replay_proved": True,
}, sort_keys=True, separators=(",", ":")))
PY

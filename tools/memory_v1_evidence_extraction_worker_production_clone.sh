#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_EVIDENCE_EXTRACTION_WORKER_CLONE_PORT:-55444}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1evidenceextractionworkerclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
lifecycle_migration=ops/sql/20260713_memory_v1_evidence_lifecycle.sql
writer_migration=ops/sql/20260716_memory_v1_record_evidence_api.sql
privilege_migration=ops/sql/20260716_memory_v1_revoke_direct_evidence_insert.sql
selector_migration=ops/sql/20260716_memory_v1_evidence_intake_selector.sql
queue_migration=ops/sql/20260716_memory_v1_evidence_extraction_queue.sql
migration=ops/sql/20260716_memory_v1_evidence_extraction_worker.sql
rollback=ops/sql/20260716_memory_v1_evidence_extraction_worker_rollback.sql
test_sql=tests/memory_v1_evidence_extraction_worker.sql
worker=scripts/memory_v1_evidence_extraction_fixture_worker.py
worker_test=scripts/memory_v1_evidence_extraction_fixture_worker_test.py
provider=scripts/memory_v1_relational_extraction_v5_provider.py
provider_test=scripts/memory_v1_relational_extraction_v5_provider_test.py
openai_provider=scripts/memory_v1_relational_extraction_v5_openai_provider.py
openai_provider_test=scripts/memory_v1_relational_extraction_v5_openai_provider_test.py
observable_provider=scripts/memory_v1_relational_extraction_v5_observable_provider.py
observable_provider_test=scripts/memory_v1_relational_extraction_v5_observable_provider_test.py
external_preflight=scripts/memory_v1_relational_extraction_v5_external_preflight.py
external_preflight_test=scripts/memory_v1_relational_extraction_v5_external_preflight_test.py
external_preflight_manifest=ops/manifests/memory_v1_relational_extraction_v5_external_preflight_20260716.json
external_smoke=scripts/memory_v1_relational_extraction_v5_external_smoke.py
external_smoke_test=scripts/memory_v1_relational_extraction_v5_external_smoke_test.py
external_smoke_manifest=ops/manifests/memory_v1_relational_extraction_v5_external_smoke_20260716.json
external_smoke_v2=scripts/memory_v1_relational_extraction_v5_external_smoke_v2.py
external_smoke_v2_test=scripts/memory_v1_relational_extraction_v5_external_smoke_v2_test.py
external_smoke_v2_manifest=ops/manifests/memory_v1_relational_extraction_v5_external_smoke_v2_candidate_20260716.json
fixture=tests/fixtures/memory_v1_evidence_extraction_fixture_v1.json
fixture_seed=tests/memory_v1_evidence_extraction_fixture_seed.sql
backup=$(mktemp /tmp/memory-v1-evidence-extraction-worker.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-evidence-extraction-worker-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-evidence-extraction-worker-after.XXXXXX.tsv)
rolled_back=$(mktemp /tmp/memory-v1-evidence-extraction-worker-rollback.XXXXXX.tsv)
plan_report=$(mktemp /tmp/memory-v1-evidence-extraction-worker-plan.XXXXXX.json)
apply_report=$(mktemp /tmp/memory-v1-evidence-extraction-worker-apply.XXXXXX.json)
replay_report=$(mktemp /tmp/memory-v1-evidence-extraction-worker-replay.XXXXXX.json)
cross_report=$(mktemp /tmp/memory-v1-evidence-extraction-worker-cross.XXXXXX.json)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f \
    "$backup" "$before" "$after" "$rolled_back" \
    "$plan_report" "$apply_report" "$replay_report" "$cross_report"
}
trap cleanup EXIT
chmod 0600 \
  "$backup" "$before" "$after" "$rolled_back" \
  "$plan_report" "$apply_report" "$replay_report" "$cross_report"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

capture_logical_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(
               digest(
                 coalesce(
                   string_agg(row_json,E'\\n' ORDER BY row_json),
                   ''
                 ),
                 'sha256'
               ),
               'hex'
             )
      FROM (
        SELECT (
          CASE
            WHEN '$table'='evidence_extraction_job'
              THEN to_jsonb(table_row)
                - 'checkpoint_sequence'
                - 'checkpoint_sha256'
            WHEN '$table'='evidence_extraction_event'
              THEN to_jsonb(table_row)-'operation_id'
            ELSE to_jsonb(table_row)
          END
        )::text AS row_json
        FROM memory.\"$table\" AS table_row
      ) rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(scalar "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
}

for required in \
  "$lifecycle_migration" \
  "$writer_migration" \
  "$privilege_migration" \
  "$selector_migration" \
  "$queue_migration" \
  "$migration" \
  "$rollback" \
  "$test_sql" \
  "$worker" \
  "$worker_test" \
  "$provider" \
  "$provider_test" \
  "$openai_provider" \
  "$openai_provider_test" \
  "$observable_provider" \
  "$observable_provider_test" \
  "$external_preflight" \
  "$external_preflight_test" \
  "$external_preflight_manifest" \
  "$external_smoke" \
  "$external_smoke_test" \
  "$external_smoke_manifest" \
  "$external_smoke_v2" \
  "$external_smoke_v2_test" \
  "$external_smoke_v2_manifest" \
  "$fixture" \
  "$fixture_seed"; do
  [[ -f "$repo_root/$required" ]]
done

if rg -n '(^|[^a-zA-Z])(OpenAI|responses\.create|chat\.completions)' \
  "$repo_root/$migration" \
  "$repo_root/$test_sql" \
  "$repo_root/$worker" \
  "$repo_root/$worker_test" \
  "$repo_root/$provider" \
  "$repo_root/$provider_test"; then
  echo "evidence extraction worker database patch contains a model caller" >&2
  exit 1
fi

if rg -n 'memory_v1_relational_extraction_v5_openai_provider' \
  "$repo_root/$worker" \
  "$repo_root/$worker_test" \
  "$repo_root/$provider" \
  "$repo_root/$provider_test"; then
  echo "fixture worker path imports the disabled external provider" >&2
  exit 1
fi

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]

"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  'CREATE ROLE memory_evidence_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_review_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_intake_maintainer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"
printf '%s\n' 'GRANT USAGE ON SCHEMA memory TO brains_app;' | run_sql
printf '%s\n' \
  "COMMENT ON DATABASE memory IS 'memory_v1_disposable_clone_worker_v1';" \
  | run_sql

run_sql <"$lifecycle_migration"
run_sql <"$writer_migration"
run_sql <"$privilege_migration"
run_sql <"$selector_migration"
run_sql <"$queue_migration"

[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job")" == "0" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event")" == "0" ]]
capture_logical_state "$before"

run_sql <"$migration"
run_sql <"$migration"
run_sql <"$test_sql"

[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job")" == "0" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event")" == "0" ]]
capture_logical_state "$after"
cmp -s "$before" "$after"

run_sql <"$rollback"
[[ "$(scalar "
  SELECT (
    to_regclass('memory.evidence_extraction_job') IS NOT NULL
    AND to_regclass('memory.evidence_extraction_event') IS NOT NULL
    AND to_regprocedure(
      'memory.enqueue_owner_evidence_extraction_v1(uuid,text,text,text,text)'
    ) IS NOT NULL
    AND to_regprocedure(
      'memory.claim_owner_evidence_extraction_job_v1(uuid,text,text,integer,integer)'
    ) IS NULL
    AND to_regprocedure(
      'memory.checkpoint_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,integer,text,jsonb,integer)'
    ) IS NULL
    AND to_regprocedure(
      'memory.finish_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,text,text,jsonb)'
    ) IS NULL
    AND to_regprocedure(
      'memory.fail_owner_evidence_extraction_job_v1(uuid,uuid,uuid,text,text,text,text,integer)'
    ) IS NULL
    AND to_regprocedure(
      'memory.resolve_owner_evidence_extraction_review_v1(uuid,uuid,text,text,text,jsonb)'
    ) IS NULL
    AND to_regrole('memory_extraction_worker_maintainer') IS NULL
    AND NOT EXISTS (
      SELECT 1
      FROM information_schema.columns
      WHERE table_schema='memory'
        AND (
          (
            table_name='evidence_extraction_job'
            AND column_name IN ('checkpoint_sequence','checkpoint_sha256')
          )
          OR
          (
            table_name='evidence_extraction_event'
            AND column_name='operation_id'
          )
        )
    )
  )::integer
")" == "1" ]]
capture_logical_state "$rolled_back"
cmp -s "$before" "$rolled_back"

run_sql <"$migration"
PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$provider_test"
PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$openai_provider_test"
PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$observable_provider_test"
PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$external_smoke_v2_test"
PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$external_preflight_test"
PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$external_smoke_test"
PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker_test"

fixture_sha256=$(sha256sum "$repo_root/$fixture" | awk '{print $1}')
worker_common=(
  /opt/chat-memory/venv/bin/python
  "$repo_root/$worker"
  --fixture "$repo_root/$fixture"
  --expected-fixture-sha256 "$fixture_sha256"
  --owner-user-id e1111111-1111-4111-8111-111111111111
  --route relational_extraction
  --run-id f1111111-1111-4111-8111-111111111111
  --worker-id fixture-worker-test
  --lease-seconds 300
  --max-attempts 3
  --max-jobs 1
)

PYTHONPATH="$repo_root" "${worker_common[@]}" >"$plan_report"
PLAN_REPORT="$plan_report" python3 - <<'PY'
import json
import os
from pathlib import Path

report = json.loads(Path(os.environ["PLAN_REPORT"]).read_text())
assert report["apply"] is False
assert report["fixture_only"] is True
assert report["model_calls"] == 0
assert report["candidate_writes"] == 0
assert report["claim_writes"] == 0
assert report["staging_writes"] == 0
assert report["qdrant_writes"] == 0
PY

printf '%s\n' \
  "COMMENT ON DATABASE memory IS 'not_an_authorized_disposable_clone';" \
  | run_sql
if POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  "${worker_common[@]}" --apply-fixture >/dev/null 2>&1; then
  echo "fixture worker accepted an unmarked database" >&2
  exit 1
fi
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job")" == "0" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event")" == "0" ]]
printf '%s\n' \
  "COMMENT ON DATABASE memory IS 'memory_v1_disposable_clone_worker_v1';" \
  | run_sql

candidate_count_before=$(scalar "SELECT count(*) FROM memory.candidate")
claim_count_before=$(scalar "SELECT count(*) FROM memory.claim")
run_sql <"$fixture_seed"
[[ "$(scalar "
  SELECT count(*)
  FROM memory.evidence_extraction_job
  WHERE owner_user_id='e1111111-1111-4111-8111-111111111111'
    AND selector_version='20260716_fixture_worker_v1'
    AND status='pending'
    AND evidence_content_sha256=
      'e7a9aa3849aee9aed7c5914fe1ff9140f7b30d5ee3e5b92e0553c821a436d29f'
")" == "1" ]]

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  "${worker_common[@]}" --apply-fixture --verify-replay >"$apply_report"
APPLY_REPORT="$apply_report" python3 - <<'PY'
import json
import os
from pathlib import Path

report = json.loads(Path(os.environ["APPLY_REPORT"]).read_text())
assert report["apply"] is True
assert report["disposable_clone_verified"] is True
assert report["claimed"] == 1
assert report["checkpointed"] == 1
assert report["review_required"] == 1
assert report["claim_replayed"] == 0
assert report["checkpoint_replayed"] == 1
assert report["finish_replayed"] == 1
assert report["model_calls"] == 0
assert report["candidate_writes"] == 0
assert report["claim_writes"] == 0
assert report["staging_writes"] == 0
assert report["qdrant_writes"] == 0
PY
[[ "$(scalar "
  SELECT count(*)
  FROM memory.evidence_extraction_job
  WHERE owner_user_id='e1111111-1111-4111-8111-111111111111'
    AND selector_version='20260716_fixture_worker_v1'
    AND status='review_required'
    AND checkpoint_sequence=1
")" == "1" ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.evidence_extraction_event
  WHERE owner_user_id='e1111111-1111-4111-8111-111111111111'
")" == "4" ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.evidence_extraction_event
  WHERE owner_user_id='e1111111-1111-4111-8111-111111111111'
    AND operation_id IS NOT NULL
")" == "3" ]]
[[ "$(scalar "SELECT count(*) FROM memory.candidate")" == "$candidate_count_before" ]]
[[ "$(scalar "SELECT count(*) FROM memory.claim")" == "$claim_count_before" ]]

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  "${worker_common[@]}" --apply-fixture --verify-replay >"$replay_report"
REPLAY_REPORT="$replay_report" python3 - <<'PY'
import json
import os
from pathlib import Path

report = json.loads(Path(os.environ["REPLAY_REPORT"]).read_text())
assert report["claimed"] == 0
assert report["checkpointed"] == 0
assert report["review_required"] == 0
assert report["claim_replayed"] == 1
assert report["checkpoint_replayed"] == 2
assert report["finish_replayed"] == 2
PY
[[ "$(scalar "
  SELECT count(*)
  FROM memory.evidence_extraction_event
  WHERE owner_user_id='e1111111-1111-4111-8111-111111111111'
")" == "4" ]]

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$worker" \
  --fixture "$repo_root/$fixture" \
  --expected-fixture-sha256 "$fixture_sha256" \
  --owner-user-id f2222222-2222-4222-8222-222222222222 \
  --route relational_extraction \
  --run-id f2222222-2222-4222-8222-222222222222 \
  --worker-id fixture-worker-cross-owner-test \
  --apply-fixture \
  >"$cross_report"
CROSS_REPORT="$cross_report" python3 - <<'PY'
import json
import os
from pathlib import Path

report = json.loads(Path(os.environ["CROSS_REPORT"]).read_text())
assert report["claimed"] == 0
assert report["checkpointed"] == 0
assert report["review_required"] == 0
PY

echo "memory_v1_evidence_extraction_worker_production_clone: PASS"

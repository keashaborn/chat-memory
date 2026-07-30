#!/usr/bin/env bash
set -Eeuo pipefail

# seebx backend only. Exercises queue reconciliation against a fresh,
# ownership-faithful disposable clone. Production receives only a read-only
# backup.

repo=/opt/chat-memory
worktree=/home/ubuntu/chat-memory-queue-reconciliation-v1
expected_production_head=d9dc01a3d134550a004fa238577124bb171d66ce
migration=$worktree/ops/sql/20260730_memory_v1_queue_reconciliation_v1.sql
rollback=$worktree/ops/sql/20260730_memory_v1_queue_reconciliation_v1_rollback.sql
worker=$worktree/scripts/memory_v1_queue_reconciliation_v1.py
stamp=$(date -u +%Y%m%dT%H%M%SZ)
clone=memory_queue_reconciliation_"${stamp//[^0-9]/}"
work=$(mktemp -d /tmp/memory-queue-reconciliation.XXXXXX)
backup=$work/production.dump

cleanup() {
  docker exec brains-postgres-1 dropdb -U sage --if-exists "$clone" \
    >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
trap 'printf "QUEUE_RECONCILIATION_CLONE_FAILED line=%s\n" "$LINENO" >&2' ERR

test "$(git -C "$repo" rev-parse HEAD)" = "$expected_production_head"
test -z "$(git -C "$repo" status --short)"
git -C "$worktree" merge-base --is-ancestor \
  "$expected_production_head" HEAD
test -f "$migration"
test -f "$rollback"
test -f "$worker"

set -a
source "$repo/.env"
set +a
readarray -t database_values < <(
  "$repo/venv/bin/python" - "$POSTGRES_DSN" "$clone" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit

parsed = urlsplit(sys.argv[1])
if parsed.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("invalid database DSN")
source_database = parsed.path.lstrip("/")
if not source_database:
    raise SystemExit("source database is absent")
clone_database = sys.argv[2]
clone_dsn = urlunsplit(
    (
        parsed.scheme,
        parsed.netloc,
        f"/{clone_database}",
        parsed.query,
        parsed.fragment,
    )
)
print(source_database)
print(clone_dsn)
PY
)
source_database=${database_values[0]}
clone_dsn=${database_values[1]}

docker exec brains-postgres-1 pg_dump -U sage -Fc "$source_database" \
  >"$backup"
test -s "$backup"
docker exec brains-postgres-1 createdb -U sage "$clone"
docker exec -i brains-postgres-1 pg_restore -U sage -d "$clone" \
  <"$backup"
docker exec -i brains-postgres-1 psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration"

scalar() {
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone" -c "$1"
}

capture_counts() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    printf '%s\t%s\n' "$table" \
      "$(scalar "SELECT count(*) FROM memory.\"$table\"")" \
      >>"$output"
  done < <(
    scalar "
      SELECT table_name
      FROM information_schema.tables
      WHERE table_schema='memory' AND table_type='BASE TABLE'
      ORDER BY table_name
    "
  )
}

capture_protected() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    local table_hash
    table_hash=$(
      scalar "
        SELECT md5(coalesce(string_agg(
          md5(to_jsonb(record_row)::text),''
          ORDER BY md5(to_jsonb(record_row)::text)
        ),''))
        FROM memory.\"$table\" AS record_row
      "
    )
    test "${#table_hash}" -eq 32
    printf '%s\t%s\n' "$table" "$table_hash" >>"$output"
  done < <(
    scalar "
      SELECT table_name
      FROM information_schema.tables
      WHERE table_schema='memory'
        AND table_type='BASE TABLE'
        AND table_name NOT IN (
          'evidence',
          'evidence_context_queue_reconciliation_v1',
          'evidence_context_rebind_v1',
          'evidence_extraction_event',
          'evidence_extraction_job',
          'evidence_intake_terminal',
          'v5_local_inference_event'
        )
      ORDER BY table_name
    "
  )
}

capture_qdrant() {
  local output=$1
  : >"$output"
  while IFS= read -r collection; do
    curl -fsS "http://127.0.0.1:6333/collections/$collection" |
      jq -r --arg name "$collection" \
        '[$name, (.result.points_count|tostring),
          (.result.indexed_vectors_count|tostring)] | @tsv' \
        >>"$output"
  done < <(
    curl -fsS http://127.0.0.1:6333/collections |
      jq -r '.result.collections[].name' | sort
  )
}

PYTHONPATH="$worktree" POSTGRES_DSN="$clone_dsn" \
  "$repo/venv/bin/python" "$worker" --limit 100 >"$work/plan.json"
test "$(jq -r '.outcome' "$work/plan.json")" = planned
test "$(jq -r '.owner_count' "$work/plan.json")" -eq 7
test "$(jq -r '.candidate_count' "$work/plan.json")" -eq 56
test "$(jq -r '.candidate_counts.superseded' "$work/plan.json")" -eq 21
test "$(jq -r '.candidate_counts.context_rebind' "$work/plan.json")" -eq 34
test "$(jq -r '.candidate_counts.orphan_recovery' "$work/plan.json")" -eq 1
test "$(jq -r '.new_local_model_calls' "$work/plan.json")" -eq 0
test "$(jq -r '.external_model_calls' "$work/plan.json")" -eq 0
plan_sha=$(jq -r '.plan_sha256' "$work/plan.json")
test "${#plan_sha}" -eq 64

capture_counts "$work/before-counts.tsv"
capture_protected "$work/before-protected.tsv"
capture_qdrant "$work/before-qdrant.tsv"

MEMORY_V1_QUEUE_RECONCILIATION_APPLY=memory_v1_queue_reconciliation_apply_v1 \
  PYTHONPATH="$worktree" POSTGRES_DSN="$clone_dsn" \
  "$repo/venv/bin/python" "$worker" --limit 100 \
  --expected-plan-sha256 "$plan_sha" --apply >"$work/apply.json"

test "$(jq -r '.outcome' "$work/apply.json")" = applied
test "$(jq -r '.candidate_count' "$work/apply.json")" -eq 56
test "$(jq -r '.applied_counts.superseded' "$work/apply.json")" -eq 21
test "$(jq -r '.applied_counts.context_rebind' "$work/apply.json")" -eq 34
test "$(jq -r '.applied_counts.orphan_recovery' "$work/apply.json")" -eq 1
test "$(jq -r '.new_local_model_calls' "$work/apply.json")" -eq 0
test "$(jq -r '.external_model_calls' "$work/apply.json")" -eq 0
test "$(jq -r '.claim_writes' "$work/apply.json")" -eq 0
test "$(jq -r '.qdrant_writes' "$work/apply.json")" -eq 0
test "$(jq -r '.prompt_influence' "$work/apply.json")" -eq 0
test "$(jq -r '.zero_write_replay_proved' "$work/apply.json")" = true

capture_counts "$work/after-counts.tsv"
capture_protected "$work/after-protected.tsv"
capture_qdrant "$work/after-qdrant.tsv"
cmp -s "$work/before-protected.tsv" "$work/after-protected.tsv"
cmp -s "$work/before-qdrant.tsv" "$work/after-qdrant.tsv"

join "$work/before-counts.tsv" "$work/after-counts.tsv" \
  >"$work/deltas.tsv"
awk '
  function delta() {return $3-$2}
  $1 == "evidence" && delta() != 34 {failed=1}
  $1 == "evidence_context_queue_reconciliation_v1" &&
    delta() != 21 {failed=1}
  $1 == "evidence_context_rebind_v1" && delta() != 34 {failed=1}
  $1 == "evidence_extraction_event" && delta() != 90 {failed=1}
  $1 == "evidence_extraction_job" && delta() != 34 {failed=1}
  $1 == "evidence_intake_terminal" && delta() != 34 {failed=1}
  $1 == "v5_local_inference_event" && delta() != 1 {failed=1}
  $1 != "evidence" &&
    $1 != "evidence_context_queue_reconciliation_v1" &&
    $1 != "evidence_context_rebind_v1" &&
    $1 != "evidence_extraction_event" &&
    $1 != "evidence_extraction_job" &&
    $1 != "evidence_intake_terminal" &&
    $1 != "v5_local_inference_event" &&
    delta() != 0 {failed=1}
  END {exit failed}
' "$work/deltas.tsv"

PYTHONPATH="$worktree" POSTGRES_DSN="$clone_dsn" \
  "$repo/venv/bin/python" "$worker" --limit 100 >"$work/replay-plan.json"
test "$(jq -r '.candidate_count' "$work/replay-plan.json")" -eq 0

admin=1240822d-ac9a-4096-95aa-e2b24d36ef50
new_owner=9dd7426d-77eb-4765-9db2-13e33ad7444d
for owner in "$admin" "$new_owner"; do
  PYTHONPATH="$worktree" POSTGRES_DSN="$clone_dsn" \
    "$repo/venv/bin/python" \
    "$worktree/scripts/memory_v1_v5_local_inference_scheduler.py" \
    --owner-user-id "$owner" --max-attempts 1 \
    >"$work/scheduler-$owner.json"
done
test "$(
  jq -r '.plans[0].context_ready_count' "$work/scheduler-$admin.json"
)" -eq 24
test "$(
  jq -r '.plans[0].context_ready_count' "$work/scheduler-$new_owner.json"
)" -eq 10
test "$(
  jq -r '.plans[0].context_rebind_required_count' \
    "$work/scheduler-$admin.json"
)" -eq 0
test "$(
  jq -r '.plans[0].context_rebind_required_count' \
    "$work/scheduler-$new_owner.json"
)" -eq 0

read -r source_owner reconciliation_id operation_id source_job \
  source_evidence terminal_job terminal_evidence envelope < <(
  scalar "
    SELECT concat_ws(' ',owner_user_id,reconciliation_id,operation_id,
           source_job_id,source_evidence_id,terminal_job_id,
           terminal_evidence_id,source_envelope_sha256)
    FROM memory.evidence_context_queue_reconciliation_v1
    ORDER BY owner_user_id,source_job_id LIMIT 1
  "
)
other_owner=$(
  scalar "
    SELECT owner_user_id
    FROM memory.authenticated_owner_registry_v1
    WHERE owner_user_id<>'$source_owner'::uuid
    ORDER BY owner_user_id LIMIT 1
  "
)
if docker exec -i brains-postgres-1 psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" >/dev/null 2>&1 <<SQL
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','$other_owner',false);
SELECT *
FROM memory.finalize_owner_context_superseded_v1(
  '$reconciliation_id','$operation_id','$source_job','$source_evidence',
  '$terminal_job','$terminal_evidence','$envelope'
);
SQL
then
  echo "cross-owner queue reconciliation unexpectedly succeeded" >&2
  exit 1
fi

test "$(scalar "
  SELECT (
    relrowsecurity
    AND relforcerowsecurity
    AND pg_get_userbyid(relowner)='sage'
  )::int
  FROM pg_class
  WHERE oid=
    'memory.evidence_context_queue_reconciliation_v1'::regclass
")" -eq 1

if docker exec -i brains-postgres-1 psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$rollback" >/dev/null 2>&1
then
  echo "rollback unexpectedly removed reconciliation history" >&2
  exit 1
fi

printf 'QUEUE_RECONCILIATION_CLONE=PASS candidates=56 owners=7\n'
printf 'SUPERSEDED=21 CONTEXT_REBOUND=34 ORPHAN_RECOVERED=1\n'
printf 'CONTEXT_READY_AFTER=34\n'
printf 'PLAN_SHA256=%s\n' "$plan_sha"
printf 'MIGRATION_SHA256=%s\n' \
  "$(sha256sum "$migration" | cut -d' ' -f1)"
printf 'ROLLBACK_SHA256=%s\n' \
  "$(sha256sum "$rollback" | cut -d' ' -f1)"
printf 'QDRANT_UNCHANGED=true\n'
printf 'PROTECTED_STORES_UNCHANGED=true\n'
printf 'REPLAY_ZERO_WRITE=true\n'
printf 'CROSS_OWNER_REJECTED=true\n'

docker exec brains-postgres-1 dropdb -U sage "$clone"
trap - EXIT
rm -rf "$work"
printf 'CLONE_REMOVED=%s\n' "$clone"

#!/usr/bin/env bash
set -Eeuo pipefail

# seebx backend only. Installs and exercises the exact legacy full-turn
# context-rebind path on a disposable ownership-faithful production clone.

repo=/opt/chat-memory
worktree=/tmp/chat-memory-legacy-context-rebind-v1
expected_production_head=821dc348d9e674b2e16c55bdfc3e24022294740b
migration="$worktree/sql/memory_v1_legacy_context_rebind_v1.sql"
rollback="$worktree/sql/memory_v1_legacy_context_rebind_v1_rollback.sql"
worker="$worktree/scripts/memory_v1_legacy_context_rebind_v1.py"
backup=${MEMORY_CONTEXT_REBIND_BACKUP:-/var/backups/chat-memory/context-ready-scheduler-20260728T011945Z/postgres-before.dump}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
clone=memory_context_rebind_clone_"${stamp//[^0-9]/}"
work=$(mktemp -d /tmp/memory-context-rebind-clone.XXXXXX)

cleanup() {
  docker exec brains-postgres-1 dropdb -U sage --if-exists "$clone" \
    >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

test "$(git -C "$repo" rev-parse HEAD)" = "$expected_production_head"
test -z "$(git -C "$repo" status --short)"
git -C "$worktree" merge-base --is-ancestor \
  "$expected_production_head" HEAD
test -z "$(git -C "$worktree" status --short)"
test -s "$backup"
test -f "$migration"
test -f "$rollback"
test -f "$worker"

docker exec brains-postgres-1 createdb -U sage "$clone"
docker exec -i brains-postgres-1 pg_restore -U sage -d "$clone" \
  <"$backup"

docker exec -i brains-postgres-1 psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration"

test "$(
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone" -c "
      SELECT has_function_privilege(
        'memory_context_rebind_maintainer',
        'memory.current_actor_user_id()',
        'EXECUTE'
      )::int
    "
)" -eq 1

set -a
source "$repo/.env"
set +a
clone_dsn=$(
  "$repo/venv/bin/python" - "$POSTGRES_DSN" "$clone" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit

parsed = urlsplit(sys.argv[1])
print(
    urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"/{sys.argv[2]}",
            parsed.query,
            parsed.fragment,
        )
    )
)
PY
)

run_sage() {
  docker exec brains-postgres-1 psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$clone" "$@"
}

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
      WHERE table_schema='memory'
        AND table_type='BASE TABLE'
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
  "$repo/venv/bin/python" "$worker" --limit 10 \
  >"$work/plan.json"

test "$(jq -r '.outcome' "$work/plan.json")" = planned
test "$(jq -r '.candidate_count' "$work/plan.json")" -eq 10
test "$(jq -r '.owner_count' "$work/plan.json")" -eq 6
test "$(jq -r '.external_model_calls' "$work/plan.json")" -eq 0
test "$(jq -r '.local_model_calls' "$work/plan.json")" -eq 0
plan_sha=$(jq -r '.plan_sha256' "$work/plan.json")
test "${#plan_sha}" -eq 64

capture_counts "$work/before.tsv"
capture_qdrant "$work/qdrant-before.tsv"

MEMORY_V1_LEGACY_CONTEXT_REBIND_APPLY=memory_v1_legacy_context_rebind_apply_v1 \
  PYTHONPATH="$worktree" POSTGRES_DSN="$clone_dsn" \
  "$repo/venv/bin/python" "$worker" \
  --limit 10 --expected-plan-sha256 "$plan_sha" --apply \
  >"$work/apply.json"

test "$(jq -r '.outcome' "$work/apply.json")" = applied
test "$(jq -r '.candidate_count' "$work/apply.json")" -eq 10
test "$(jq -r '.external_model_calls' "$work/apply.json")" -eq 0
test "$(jq -r '.local_model_calls' "$work/apply.json")" -eq 0
test "$(jq -r '.claim_writes' "$work/apply.json")" -eq 0
test "$(jq -r '.qdrant_writes' "$work/apply.json")" -eq 0
test "$(jq -r '.prompt_influence' "$work/apply.json")" -eq 0

capture_counts "$work/after.tsv"
capture_qdrant "$work/qdrant-after.tsv"
cmp -s "$work/qdrant-before.tsv" "$work/qdrant-after.tsv"

join "$work/before.tsv" "$work/after.tsv" >"$work/joined.tsv"
awk '
  function delta(table) {
    if ($1 == table) return $3 - $2
    return -999999
  }
  $1 == "evidence" && delta("evidence") != 10 {failed=1}
  $1 == "evidence_context_rebind_v1" &&
    delta("evidence_context_rebind_v1") != 10 {failed=1}
  $1 == "evidence_extraction_event" &&
    delta("evidence_extraction_event") != 20 {failed=1}
  $1 == "evidence_extraction_job" &&
    delta("evidence_extraction_job") != 10 {failed=1}
  $1 == "evidence_extraction_packet_v5_local" &&
    delta("evidence_extraction_packet_v5_local") != 0 {failed=1}
  $1 == "evidence_intake_terminal" &&
    delta("evidence_intake_terminal") != 10 {failed=1}
  $1 != "evidence" &&
    $1 != "evidence_context_rebind_v1" &&
    $1 != "evidence_extraction_event" &&
    $1 != "evidence_extraction_job" &&
    $1 != "evidence_intake_terminal" &&
    ($3 - $2) != 0 {failed=1}
  END {exit failed}
' "$work/joined.tsv"

test "$(scalar "
  SELECT count(*)
  FROM memory.evidence_context_rebind_v1 AS rebind
  JOIN memory.evidence_extraction_job AS source_job
    ON source_job.owner_user_id=rebind.owner_user_id
   AND source_job.job_id=rebind.source_job_id
  JOIN memory.evidence_extraction_job AS rebound_job
    ON rebound_job.owner_user_id=rebind.owner_user_id
   AND rebound_job.job_id=rebind.rebound_job_id
  JOIN memory.evidence AS rebound
    ON rebound.owner_user_id=rebind.owner_user_id
   AND rebound.evidence_id=rebind.rebound_evidence_id
  WHERE source_job.status='skipped'
    AND source_job.result#>>'{final,payload,reason_code}'='context_rebound'
    AND rebound_job.status='pending'
    AND rebound_job.selector_version=
      '20260728_v5_2_legacy_context_rebind_v1'
    AND rebound.metadata->>'source_id'=rebind.raw_source_id::text
    AND rebound.metadata->>'source_content_sha256'=
      rebind.source_content_sha256
    AND rebound.metadata->>'source_char_start'='0'
    AND rebound.metadata->>'source_char_end'=
      char_length(rebound.content)::text
    AND rebound.metadata->>'primary_lane'=
      'unclassified_user_statement'
    AND rebound.metadata->>'epistemic_role'=
      'user_report_unclassified'
    AND rebound.metadata->>'span_origin'=
      'legacy_full_turn_rebind_v1'
")" -eq 10

run_sage -A -t -F $'\t' -c "
  SELECT
    owner_user_id,
    rebind_id,
    source_job_id,
    rebound_evidence_id,
    rebound_job_id,
    raw_source_id,
    source_content_sha256,
    selector_version
  FROM memory.evidence_context_rebind_v1
  ORDER BY owner_user_id,rebind_id
" >"$work/replays.tsv"

before_replay=$(sha256sum "$work/after.tsv" | cut -d' ' -f1)
while IFS=$'\t' read -r owner rebind source_job rebound_evidence \
  rebound_job raw_source content_sha selector; do
  docker exec -i brains-postgres-1 psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$clone" >/dev/null <<SQL
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','$owner',false);
SELECT apply_outcome
FROM memory.finalize_owner_legacy_context_rebind_v1(
  '$rebind','$source_job','$rebound_evidence','$rebound_job',
  '$raw_source','$content_sha','$selector'
)
WHERE apply_outcome='replayed';
SQL
done <"$work/replays.tsv"
capture_counts "$work/replayed.tsv"
test "$(sha256sum "$work/replayed.tsv" | cut -d' ' -f1)" = "$before_replay"

target=$(head -1 "$work/replays.tsv")
target_owner=$(cut -f1 <<<"$target")
other_owner=$(
  scalar "
    SELECT owner_user_id
    FROM memory.authenticated_owner_registry_v1
    WHERE owner_user_id<>'$target_owner'::uuid
    ORDER BY owner_user_id
    LIMIT 1
  "
)
IFS=$'\t' read -r _ rebind source_job rebound_evidence rebound_job \
  raw_source content_sha selector <<<"$target"
if docker exec -i brains-postgres-1 psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" >/dev/null 2>&1 <<SQL
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','$other_owner',false);
SELECT *
FROM memory.finalize_owner_legacy_context_rebind_v1(
  '$rebind','$source_job','$rebound_evidence','$rebound_job',
  '$raw_source','$content_sha','$selector'
);
SQL
then
  echo "cross-owner context rebind unexpectedly succeeded" >&2
  exit 1
fi

test "$(scalar "
  SELECT (
    relrowsecurity
    AND relforcerowsecurity
    AND pg_get_userbyid(relowner)='memory_context_rebind_maintainer'
  )::int
  FROM pg_class
  WHERE oid='memory.evidence_context_rebind_v1'::regclass
")" -eq 1

if docker exec -i brains-postgres-1 psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$rollback" >/dev/null 2>&1
then
  echo "rollback unexpectedly removed durable rebind history" >&2
  exit 1
fi

printf 'LEGACY_CONTEXT_REBIND_CLONE=PASS candidates=10 owners=6\n'
printf 'PLAN_SHA256=%s\n' "$plan_sha"
printf 'MIGRATION_SHA256=%s\n' "$(sha256sum "$migration" | cut -d' ' -f1)"
printf 'ROLLBACK_SHA256=%s\n' "$(sha256sum "$rollback" | cut -d' ' -f1)"
printf 'QDRANT_UNCHANGED=true\n'
printf 'PROTECTED_STORES_UNCHANGED=true\n'
printf 'REPLAY_ZERO_WRITE=true\n'
printf 'CROSS_OWNER_REJECTED=true\n'

docker exec brains-postgres-1 dropdb -U sage "$clone"
trap - EXIT
rm -rf "$work"
printf 'CLONE_REMOVED=%s\n' "$clone"

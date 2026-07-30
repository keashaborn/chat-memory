#!/usr/bin/env bash
set -Eeuo pipefail

# seebx backend only. Installs and applies the exact clone-tested,
# owner-scoped queue reconciliation plan.

repo=/opt/chat-memory
worktree=/home/ubuntu/chat-memory-queue-reconciliation-v1
base_commit=${MEMORY_QUEUE_RECONCILIATION_BASE_COMMIT:-d9dc01a3d134550a004fa238577124bb171d66ce}
expected_commit=${MEMORY_QUEUE_RECONCILIATION_COMMIT:-}
expected_plan_sha=${MEMORY_QUEUE_RECONCILIATION_PLAN_SHA256:-06337c87741dfc0ea5dc9b1ce53222cff4f339fcd49281bc1fd1c8929af72565}
expected_superseded=${MEMORY_QUEUE_RECONCILIATION_SUPERSEDED:-21}
expected_rebind=${MEMORY_QUEUE_RECONCILIATION_REBIND:-34}
expected_orphan=${MEMORY_QUEUE_RECONCILIATION_ORPHAN:-1}
install_migration=${MEMORY_QUEUE_RECONCILIATION_INSTALL_MIGRATION:-true}
expected_migration_sha=42316622327ee91bc95f8e901eaf5acd390a3bf9a8d9a1dd4f7ba5f586df4611
expected_rollback_sha=68c70fd0dbfc31277e063f2bf7699b7d2efce3eaf409e11dc508919e6c2857c5
expected_worker_sha=2ec1779eae2c11d566df533340a399014b869bd37f1617350d1cdc243045f6fe
migration_rel=ops/sql/20260730_memory_v1_queue_reconciliation_v1.sql
rollback_rel=ops/sql/20260730_memory_v1_queue_reconciliation_v1_rollback.sql
worker_rel=scripts/memory_v1_queue_reconciliation_v1.py
stamp=$(date -u +%Y%m%dT%H%M%SZ)
report_dir=/var/backups/chat-memory/queue-reconciliation-"$stamp"
timer_state=$report_dir/timers.tsv
timers_restored=false

if [[ ! $expected_commit =~ ^[0-9a-f]{40}$ ]]; then
  echo "exact queue reconciliation commit is required" >&2
  exit 1
fi
if [[ ! $base_commit =~ ^[0-9a-f]{40}$
      || ! $expected_plan_sha =~ ^[0-9a-f]{64}$
      || ! $expected_superseded =~ ^[0-9]+$
      || ! $expected_rebind =~ ^[0-9]+$
      || ! $expected_orphan =~ ^[0-9]+$
      || $((expected_superseded+expected_rebind+expected_orphan)) -gt 100
      || $((expected_superseded+expected_rebind+expected_orphan)) -lt 1
      || ! $install_migration =~ ^(true|false)$ ]]; then
  echo "queue reconciliation deployment inputs are invalid" >&2
  exit 1
fi
expected_total=$((expected_superseded+expected_rebind+expected_orphan))

restore_timers() {
  if [[ ! -s $timer_state ]] || $timers_restored = true; then
    return
  fi
  while IFS=$'\t' read -r timer enabled active; do
    case "$enabled" in
      enabled) systemctl enable "$timer" >/dev/null ;;
      disabled) systemctl disable "$timer" >/dev/null ;;
      masked) systemctl mask "$timer" >/dev/null ;;
      *) echo "unsupported timer enable state: $timer $enabled" >&2 ;;
    esac
    if [[ $active = active ]]; then
      systemctl start "$timer"
    else
      systemctl stop "$timer"
    fi
  done <"$timer_state"
  timers_restored=true
}
trap restore_timers EXIT
trap 'printf "QUEUE_RECONCILIATION_PRODUCTION_FAILED line=%s\n" "$LINENO" >&2' ERR

install -d -m 0700 "$report_dir"
set -a
source "$repo/.env"
set +a
test "$(git -C "$repo" rev-parse HEAD)" = "$base_commit"
test -z "$(git -C "$repo" status --short)"
test "$(git -C "$worktree" rev-parse HEAD)" = "$expected_commit"
test -z "$(git -C "$worktree" status --short)"
git -C "$worktree" merge-base --is-ancestor "$base_commit" "$expected_commit"
test "$(sha256sum "$worktree/$migration_rel" | cut -d' ' -f1)" = \
  "$expected_migration_sha"
test "$(sha256sum "$worktree/$rollback_rel" | cut -d' ' -f1)" = \
  "$expected_rollback_sha"
test "$(sha256sum "$worktree/$worker_rel" | cut -d' ' -f1)" = \
  "$expected_worker_sha"
test "$(systemctl is-active brains.service)" = active
curl -fsS -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz |
  jq -e '.status=="ok"' >/dev/null

mapfile -t timers < <(
  systemctl list-unit-files 'memory-v1*.timer' --no-legend --no-pager |
    awk '{print $1}' | sort
)
test "${#timers[@]}" -eq 17
: >"$timer_state"
for timer in "${timers[@]}"; do
  enabled=$(systemctl is-enabled "$timer" 2>/dev/null || true)
  active=$(systemctl is-active "$timer" 2>/dev/null || true)
  printf '%s\t%s\t%s\n' "$timer" "$enabled" "$active" >>"$timer_state"
done

for timer in "${timers[@]}"; do
  systemctl stop "$timer"
done
for _ in $(seq 1 120); do
  running=0
  for timer in "${timers[@]}"; do
    service=${timer%.timer}.service
    if systemctl is-active --quiet "$service"; then
      running=1
      break
    fi
  done
  [[ $running -eq 0 ]] && break
  sleep 1
done
test "$running" -eq 0

source_database=$(
  "$repo/venv/bin/python" - "$POSTGRES_DSN" <<'PY'
import sys
from urllib.parse import urlsplit

parsed = urlsplit(sys.argv[1])
if parsed.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("invalid database DSN")
database = parsed.path.lstrip("/")
if not database:
    raise SystemExit("source database is absent")
print(database)
PY
)
backup=$report_dir/postgres-before.dump
docker exec brains-postgres-1 pg_dump -U sage -Fc "$source_database" \
  >"$backup"
test -s "$backup"
backup_sha=$(sha256sum "$backup" | cut -d' ' -f1)
printf '%s  %s\n' "$backup_sha" "$(basename "$backup")" \
  >"$report_dir/SHA256SUMS"
cp "$worktree/$rollback_rel" "$report_dir/"

sudo -u ubuntu git -C "$repo" merge --ff-only "$expected_commit"
test "$(git -C "$repo" rev-parse HEAD)" = "$expected_commit"
test -z "$(git -C "$repo" status --short)"
if [[ $install_migration = true ]]; then
  docker exec -i brains-postgres-1 psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$source_database" <"$repo/$migration_rel"
else
  test "$(docker exec brains-postgres-1 psql -X -A -t \
    -v ON_ERROR_STOP=1 -U sage -d "$source_database" -c \
    "SELECT (to_regprocedure('memory.plan_owner_context_superseded_v1(integer)') IS NOT NULL)::int")" \
    -eq 1
fi

scalar() {
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$source_database" -c "$1"
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

PYTHONPATH="$repo" POSTGRES_DSN="$POSTGRES_DSN" \
  "$repo/venv/bin/python" "$repo/$worker_rel" --limit 100 \
  >"$report_dir/plan.json"
test "$(jq -r '.candidate_count' "$report_dir/plan.json")" \
  -eq "$expected_total"
test "$(jq -r '.candidate_counts.superseded' "$report_dir/plan.json")" \
  -eq "$expected_superseded"
test "$(jq -r '.candidate_counts.context_rebind' "$report_dir/plan.json")" \
  -eq "$expected_rebind"
test "$(jq -r '.candidate_counts.orphan_recovery' "$report_dir/plan.json")" \
  -eq "$expected_orphan"
test "$(jq -r '.plan_sha256' "$report_dir/plan.json")" = \
  "$expected_plan_sha"

capture_counts "$report_dir/before-counts.tsv"
capture_protected "$report_dir/before-protected.tsv"
capture_qdrant "$report_dir/before-qdrant.tsv"

MEMORY_V1_QUEUE_RECONCILIATION_APPLY=memory_v1_queue_reconciliation_apply_v1 \
  PYTHONPATH="$repo" POSTGRES_DSN="$POSTGRES_DSN" \
  "$repo/venv/bin/python" "$repo/$worker_rel" --limit 100 \
  --expected-plan-sha256 "$expected_plan_sha" --apply \
  >"$report_dir/apply.json"
test "$(jq -r '.outcome' "$report_dir/apply.json")" = applied
test "$(jq -r '.applied_counts.superseded' "$report_dir/apply.json")" \
  -eq "$expected_superseded"
test "$(jq -r '.applied_counts.context_rebind' "$report_dir/apply.json")" \
  -eq "$expected_rebind"
test "$(jq -r '.applied_counts.orphan_recovery' "$report_dir/apply.json")" \
  -eq "$expected_orphan"
test "$(jq -r '.new_local_model_calls' "$report_dir/apply.json")" -eq 0
test "$(jq -r '.external_model_calls' "$report_dir/apply.json")" -eq 0
test "$(jq -r '.claim_writes' "$report_dir/apply.json")" -eq 0
test "$(jq -r '.qdrant_writes' "$report_dir/apply.json")" -eq 0
test "$(jq -r '.prompt_influence' "$report_dir/apply.json")" -eq 0

capture_counts "$report_dir/after-counts.tsv"
capture_protected "$report_dir/after-protected.tsv"
capture_qdrant "$report_dir/after-qdrant.tsv"
cmp -s "$report_dir/before-protected.tsv" \
  "$report_dir/after-protected.tsv"
cmp -s "$report_dir/before-qdrant.tsv" "$report_dir/after-qdrant.tsv"

join "$report_dir/before-counts.tsv" "$report_dir/after-counts.tsv" \
  >"$report_dir/deltas.tsv"
awk -v superseded="$expected_superseded" \
  -v rebind="$expected_rebind" -v orphan="$expected_orphan" '
  function delta() {return $3-$2}
  $1 == "evidence" && delta() != rebind {failed=1}
  $1 == "evidence_context_queue_reconciliation_v1" &&
    delta() != superseded {failed=1}
  $1 == "evidence_context_rebind_v1" && delta() != rebind {failed=1}
  $1 == "evidence_extraction_event" &&
    delta() != superseded+(2*rebind)+orphan {failed=1}
  $1 == "evidence_extraction_job" && delta() != rebind {failed=1}
  $1 == "evidence_intake_terminal" && delta() != rebind {failed=1}
  $1 == "v5_local_inference_event" && delta() != orphan {failed=1}
  $1 != "evidence" &&
    $1 != "evidence_context_queue_reconciliation_v1" &&
    $1 != "evidence_context_rebind_v1" &&
    $1 != "evidence_extraction_event" &&
    $1 != "evidence_extraction_job" &&
    $1 != "evidence_intake_terminal" &&
    $1 != "v5_local_inference_event" &&
    delta() != 0 {failed=1}
  END {exit failed}
' "$report_dir/deltas.tsv"

PYTHONPATH="$repo" POSTGRES_DSN="$POSTGRES_DSN" \
  "$repo/venv/bin/python" "$repo/$worker_rel" --limit 100 \
  >"$report_dir/replay-plan.json"
test "$(jq -r '.candidate_count' "$report_dir/replay-plan.json")" -eq 0

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
test "$(systemctl is-active brains.service)" = active
curl -fsS -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz |
  jq -e '.status=="ok"' >/dev/null

restore_timers
while IFS=$'\t' read -r timer enabled active; do
  test "$(systemctl is-enabled "$timer" 2>/dev/null || true)" = "$enabled"
  test "$(systemctl is-active "$timer" 2>/dev/null || true)" = "$active"
done <"$timer_state"

jq -n \
  --arg status pass \
  --arg commit "$expected_commit" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg plan_sha256 "$expected_plan_sha" \
  --arg migration_sha256 "$expected_migration_sha" \
  --arg rollback_sha256 "$expected_rollback_sha" \
  --argjson superseded "$expected_superseded" \
  --argjson context_rebound "$expected_rebind" \
  --argjson orphan_recovered "$expected_orphan" \
  '{
    status:$status,
    production_commit:$commit,
    backup:$backup,
    backup_sha256:$backup_sha256,
    plan_sha256:$plan_sha256,
    migration_sha256:$migration_sha256,
    rollback_sha256:$rollback_sha256,
    superseded:$superseded,
    context_rebound:$context_rebound,
    orphan_recovered:$orphan_recovered,
    context_ready_created:$context_rebound,
    new_model_calls:0,
    qdrant_unchanged:true,
    protected_stores_unchanged:true,
    replay_zero_write:true,
    timers_restored:true
  }' >"$report_dir/report.json"
chmod 0600 "$report_dir"/*
report_sha=$(sha256sum "$report_dir/report.json" | cut -d' ' -f1)

printf 'QUEUE_RECONCILIATION_PRODUCTION=PASS commit=%s\n' \
  "$expected_commit"
printf 'SUPERSEDED=%s CONTEXT_REBOUND=%s ORPHAN_RECOVERED=%s\n' \
  "$expected_superseded" "$expected_rebind" "$expected_orphan"
printf 'CONTEXT_READY_CREATED=%s\n' "$expected_rebind"
printf 'BACKUP=%s SHA256=%s\n' "$backup" "$backup_sha"
printf 'REPORT=%s SHA256=%s\n' "$report_dir/report.json" "$report_sha"
printf 'QDRANT_UNCHANGED=true PROTECTED_STORES_UNCHANGED=true\n'
printf 'REPLAY_ZERO_WRITE=true TIMERS_RESTORED=true\n'

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs one hash-locked function compatibility change,
# runs rollback-only owner/isolation tests, and performs no extraction call.

repo_root=$(git rev-parse --show-toplevel)
manifest=ops/manifests/memory_v1_v5_bound_exact_job_claim_retry4_install_20260717.json
manifest_sha=df1aca683016d58ecc758312ef9dfe8829cfdd9f1fe92be7705ef22739e8ca14
container=brains-postgres-1
snapshot_dir=/home/ubuntu/brains/snapshots
function_signature='memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer)'
function_regprocedure='memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer)'
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
job_id=788d0258-7227-46e2-8382-d13e6a122722
fourth_claim_operation=438b3995-3f7c-5bd5-b96d-1f48cb369c23
units=(
  memory-v1-projection.timer
  memory-v1-governance.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-v5-chat-capture.timer
  memory-v1-deferred-reconciliation-scan.timer
)

jq_manifest() { jq -r "$1" "$repo_root/$manifest"; }
required_commit=$(jq_manifest '.authorization.required_commit')
migration=$(jq_manifest '.artifacts.migration.path')
migration_sha=$(jq_manifest '.artifacts.migration.sha256')
rollback=$(jq_manifest '.artifacts.rollback.path')
rollback_sha=$(jq_manifest '.artifacts.rollback.sha256')
test_sql=$(jq_manifest '.artifacts.rollback_test.path')
test_sha=$(jq_manifest '.artifacts.rollback_test.sha256')
before_function_sha=$(jq_manifest '.function.before_sha256')
after_function_sha=$(jq_manifest '.function.after_sha256')

stamp=$(date -u +%Y%m%dT%H%M%SZ)
execution_id=$(cat /proc/sys/kernel/random/uuid)
backup="$snapshot_dir/memory_pre_v5_exact_claim_retry4_install_${stamp}_${execution_id}.dump"
report="$snapshot_dir/memory_v1_v5_exact_claim_retry4_install_${stamp}_${execution_id}.json"
lock=/run/lock/memory-v1-v5-exact-claim-retry4-install.lock
tables=$(mktemp /tmp/memory-v1-v5-retry4-install-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-retry4-install-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-retry4-install-after.XXXXXX)
timer_state=$(mktemp /tmp/memory-v1-v5-retry4-install-timers.XXXXXX)
qdrant_before=$(mktemp /tmp/memory-v1-v5-retry4-install-qdrant-before.XXXXXX)
qdrant_after=$(mktemp /tmp/memory-v1-v5-retry4-install-qdrant-after.XXXXXX)
quiesced=false

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

function_sha() {
  scalar "SELECT encode(public.digest(convert_to(pg_get_functiondef(
    '$function_regprocedure'::regprocedure
  ),'UTF8'),'sha256'),'hex')"
}

restore_timers() {
  [[ "$quiesced" == true ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  quiesced=false
}

cleanup() {
  status=$?
  restore_timers || status=1
  rm -f "$tables" "$before" "$after" "$timer_state" \
    "$qdrant_before" "$qdrant_after"
  exit "$status"
}
trap cleanup EXIT

capture_tables() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(
        convert_to(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'UTF8'),
        'sha256'),'hex')
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
        FROM memory.\"$table\" AS table_row
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$tables"
}

capture_qdrant() {
  local output=$1
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -X POST http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    -d '{"limit":10000,"with_payload":true,"with_vector":false}' \
    | jq -e -S -c \
      '{points:(.result.points|sort_by(.id|tostring)),next_page_offset:.result.next_page_offset}' \
    >"$output"
}

exec 9>"$lock"
flock -n 9

for artifact in "$manifest" "$migration" "$rollback" "$test_sql"; do
  [[ -f "$repo_root/$artifact" ]]
done
[[ "$(sha256sum "$repo_root/$manifest" | cut -d' ' -f1)" == "$manifest_sha" ]]
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == "$migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | cut -d' ' -f1)" == "$rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$test_sha" ]]
git merge-base --is-ancestor "$required_commit" HEAD
[[ -z "$(git status --short)" ]]
[[ "$(jq_manifest '.authorization.schema_install_authorized')" == true ]]
[[ "$(function_sha)" == "$before_function_sha" ]]
[[ "$(scalar "SELECT proowner::regrole::text='memory_extraction_worker_maintainer'
  AND prosecdef AND provolatile='v'
  FROM pg_proc WHERE oid='$function_regprocedure'::regprocedure")" == t ]]
[[ "$(scalar "SELECT has_function_privilege('brains_app',
  '$function_signature','EXECUTE')
  AND NOT has_function_privilege('public','$function_signature','EXECUTE')")" == t ]]

preflight=$(scalar "SELECT jsonb_build_object(
  'target_pending',(SELECT count(*) FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner' AND job_id='$job_id'
      AND status='pending' AND attempts=3),
  'fourth_claim_absent',(SELECT count(*)=0 FROM memory.evidence_extraction_event
    WHERE owner_user_id='$owner' AND operation_id='$fourth_claim_operation'),
  'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5)
)::text")
[[ "$(jq -r '.target_pending' <<<"$preflight")" == 1 ]]
[[ "$(jq -r '.fourth_claim_absent' <<<"$preflight")" == true ]]
[[ "$(jq -r '.packets' <<<"$preflight")" == 0 ]]

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]

: >"$timer_state"
for unit in "${units[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done
quiesced=true
for unit in "${units[@]}"; do
  sudo -n systemctl stop "$unit"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done

umask 077
docker exec "$container" pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
chmod 0600 "$backup"
[[ -s "$backup" ]]
docker exec -i "$container" pg_restore -l <"$backup" >/dev/null
backup_sha=$(sha256sum "$backup" | cut -d' ' -f1)

scalar "SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name" >"$tables"
capture_tables "$before"
capture_qdrant "$qdrant_before"
qdrant_before_sha=$(sha256sum "$qdrant_before" | cut -d' ' -f1)

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d memory <"$repo_root/$migration" >/dev/null
[[ "$(function_sha)" == "$after_function_sha" ]]

psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  -f "$repo_root/$test_sql" >/dev/null

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d memory <"$repo_root/$rollback" >/dev/null
[[ "$(function_sha)" == "$before_function_sha" ]]
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d memory <"$repo_root/$migration" >/dev/null
[[ "$(function_sha)" == "$after_function_sha" ]]

capture_tables "$after"
capture_qdrant "$qdrant_after"
qdrant_after_sha=$(sha256sum "$qdrant_after" | cut -d' ' -f1)
cmp -s "$before" "$after"
[[ "$qdrant_before_sha" == "$qdrant_after_sha" ]]

restore_timers

jq -n \
  --arg execution_id "$execution_id" \
  --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg commit "$(git rev-parse HEAD)" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg backup_path "$backup" --arg backup_sha256 "$backup_sha" \
  --arg function_sha256 "$after_function_sha" \
  --arg qdrant_sha256 "$qdrant_after_sha" \
  '{
    report_version:"memory_v1_v5_exact_claim_retry4_install_v1",
    status:"passed",execution_id:$execution_id,created_at:$created_at,
    commit:$commit,manifest_sha256:$manifest_sha256,
    backup:{path:$backup_path,sha256:$backup_sha256},
    function_sha256:$function_sha256,
    proofs:{rollback_test_passed:true,rollback_migration_passed:true,
      function_reapplied:true,memory_rows_unchanged:true,
      account_isolation:true,qdrant_unchanged:true,
      qdrant_sha256:$qdrant_sha256,timers_restored:true,
      external_model_calls:0,packet_writes:0,candidate_writes:0,
      claim_writes:0,projection_writes:0,prompt_influence:false}
  }' >"$report"
chmod 0600 "$report"

printf 'memory_v1_v5_exact_claim_retry4_install: PASS\n'
printf 'backup=%s\n' "$backup"
printf 'report=%s\n' "$report"

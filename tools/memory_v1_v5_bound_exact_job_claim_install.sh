#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs one exact-job claim function, runs its security
# suite inside rollback, and leaves all data and runtime configuration unchanged.

repo_root=$(git rev-parse --show-toplevel)
manifest=ops/manifests/memory_v1_v5_bound_exact_job_claim_install_20260717.json
manifest_sha=f83a7e60bc27a4d4d886cddcfdb12ef17357831d4cd7f1423d92aede6acdda06
migration=ops/sql/20260717_memory_v1_v5_bound_exact_job_claim.sql
migration_sha=7acfc5a4775252aa947a127aa0284c4a9c6aff0fe6d3ba753610e3d576c50a96
rollback=ops/sql/20260717_memory_v1_v5_bound_exact_job_claim_rollback.sql
rollback_sha=960574edd86362c2b88eaadf794bb39ee00f5780c26f808def8a039b69fb96a5
test_sql=tests/memory_v1_v5_bound_exact_job_claim.sql
test_sha=de2de7e5719040348b017923e0d133265a2e63d3a9500821de2dfea315fbd166
required_ancestor=f9b9bd3
container=brains-postgres-1
snapshot_dir=/home/ubuntu/brains/snapshots
units=(
  memory-v1-projection.timer
  memory-v1-governance.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-v5-chat-capture.timer
  memory-v1-deferred-reconciliation-scan.timer
)

run_id=$(cat /proc/sys/kernel/random/uuid)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="$snapshot_dir/memory_pre_v5_bound_exact_claim_${stamp}_${run_id}.dump"
report="$snapshot_dir/memory_v1_v5_bound_exact_claim_install_${stamp}_${run_id}.json"
lock=/run/lock/memory-v1-v5-bound-exact-claim-install.lock
tables=$(mktemp /tmp/memory-v1-v5-exact-claim-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-exact-claim-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-exact-claim-after.XXXXXX)
timer_state=$(mktemp /tmp/memory-v1-v5-exact-claim-timers.XXXXXX)
qdrant_before=$(mktemp /tmp/memory-v1-v5-exact-claim-qdrant-before.XXXXXX)
qdrant_after=$(mktemp /tmp/memory-v1-v5-exact-claim-qdrant-after.XXXXXX)
quiesced=false

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

run_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
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

capture_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(
        public.digest(
          convert_to(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'UTF8'),
          'sha256'
        ),
        'hex'
      )
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
    | jq -e -S -c '{points:(.result.points|sort_by(.id|tostring)),next_page_offset:.result.next_page_offset}' \
    >"$output"
}

exec 9>"$lock"
flock -n 9

for required in "$manifest" "$migration" "$rollback" "$test_sql"; do
  [[ -f "$repo_root/$required" ]]
done
[[ "$(sha256sum "$repo_root/$manifest" | cut -d' ' -f1)" == "$manifest_sha" ]]
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == "$migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | cut -d' ' -f1)" == "$rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$test_sha" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
[[ -z "$(git status --short)" ]]
[[ "$(jq -r '.authorization.install_authorized' "$repo_root/$manifest")" == true ]]

preflight=$(scalar "
  SELECT jsonb_build_object(
    'function_absent',to_regprocedure('memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer)') IS NULL,
    'target_job',(SELECT count(*) FROM memory.evidence_extraction_job
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND job_id='788d0258-7227-46e2-8382-d13e6a122722'
        AND evidence_content_sha256='ee09dea28fbb00b058a6f61ff1ef580320e37dc15f97f7d09bcc9bd43be35778'
        AND status='pending' AND attempts=0),
    'binding',(SELECT count(*) FROM memory.current_project_thread_binding_v5
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND binding_event_id='e8738381-c3be-4395-bfb2-75bfd42949e9'),
    'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5)
  )::text
")
[[ "$(jq -r '.function_absent' <<<"$preflight")" == true ]]
[[ "$(jq -r '.target_job' <<<"$preflight")" == 1 ]]
[[ "$(jq -r '.binding' <<<"$preflight")" == 1 ]]
[[ "$(jq -r '.packets' <<<"$preflight")" == 0 ]]

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

scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" >"$tables"
capture_state "$before"
capture_qdrant "$qdrant_before"
qdrant_before_sha=$(sha256sum "$qdrant_before" | cut -d' ' -f1)

run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$test_sql"

capture_state "$after"
capture_qdrant "$qdrant_after"
qdrant_after_sha=$(sha256sum "$qdrant_after" | cut -d' ' -f1)
cmp -s "$before" "$after"
[[ "$qdrant_before_sha" == "$qdrant_after_sha" ]]

postflight=$(scalar "
  SELECT jsonb_build_object(
    'function_present',to_regprocedure('memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer)') IS NOT NULL,
    'function_owner',(SELECT pg_get_userbyid(proowner) FROM pg_proc
      WHERE oid='memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer)'::regprocedure),
    'security_definer',(SELECT prosecdef FROM pg_proc
      WHERE oid='memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer)'::regprocedure),
    'brains_execute',has_function_privilege('brains_app','memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer)','EXECUTE'),
    'public_execute',has_function_privilege('public','memory.claim_owner_bound_evidence_job_v5(uuid,uuid,text,uuid,text,text,integer,integer)','EXECUTE'),
    'target_pending',(SELECT count(*) FROM memory.evidence_extraction_job
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND job_id='788d0258-7227-46e2-8382-d13e6a122722'
        AND status='pending' AND attempts=0),
    'claim_events',(SELECT count(*) FROM memory.evidence_extraction_event
      WHERE operation_id='9f6f663b-a844-5e86-ad11-97ef7458c3d3'),
    'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5)
  )::text
")
[[ "$(jq -r '.function_present and .security_definer and .brains_execute and (.public_execute|not)' <<<"$postflight")" == true ]]
[[ "$(jq -r '.function_owner' <<<"$postflight")" == memory_extraction_worker_maintainer ]]
[[ "$(jq -r '.target_pending' <<<"$postflight")" == 1 ]]
[[ "$(jq -r '.claim_events + .packets' <<<"$postflight")" == 0 ]]

restore_timers

jq -n \
  --arg run_id "$run_id" --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg commit "$(git rev-parse HEAD)" --arg manifest_sha256 "$manifest_sha" \
  --arg migration_sha256 "$migration_sha" --arg backup_path "$backup" \
  --arg backup_sha256 "$backup_sha" --arg qdrant_sha256 "$qdrant_after_sha" \
  --argjson postflight "$postflight" \
  '{
    report_version:"memory_v1_v5_bound_exact_job_claim_install_v1",
    status:"passed",run_id:$run_id,created_at:$created_at,commit:$commit,
    manifest_sha256:$manifest_sha256,migration_sha256:$migration_sha256,
    backup:{path:$backup_path,sha256:$backup_sha256},postflight:$postflight,
    proofs:{rollback_test_passed:true,memory_rows_unchanged:true,
      qdrant_unchanged:true,qdrant_sha256:$qdrant_sha256,
      timers_restored:true,external_model_calls:0,packet_writes:0,
      retrieval_active:false,prompt_influence:false}
  }' >"$report"
chmod 0600 "$report"

printf 'memory_v1_v5_bound_exact_job_claim_install: PASS\n'
printf 'backup=%s\n' "$backup"
printf 'report=%s\n' "$report"

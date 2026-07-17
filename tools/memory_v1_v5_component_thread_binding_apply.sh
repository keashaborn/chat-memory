#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies one hash-locked owner-scoped project-thread
# binding. It does not run extraction or contact an external provider.

repo_root=$(git rev-parse --show-toplevel)
manifest=ops/manifests/memory_v1_v5_component_thread_binding_apply_20260717.json
manifest_sha=ce19dbd1c41a9831ceaae18509a94d2d255ef06d18890aa84f453294f49c8d1f
selector_report=/home/ubuntu/brains/snapshots/memory_v1_v5_component_binding_selector_20260717T190429Z.json
selector_report_sha=b414ac64a2480c58949fd5d7a2f31976e556d3201cbcbda352142ab1598e5842
apply_sql=ops/sql/20260717_memory_v1_v5_component_thread_binding_apply.sql
apply_sql_sha=72baa74bade65bbbdffebc59784fc56c1e6b538afdeac6050bc0b7531865e28d
required_ancestor=c5e6ac3
container=brains-postgres-1
database=memory
maintenance_role=sage
snapshot_dir=/home/ubuntu/brains/snapshots
qdrant_url=http://127.0.0.1:6333
qdrant_collection=memory_claim_v1
units=(
  memory-v1-projection.timer
  memory-v1-governance.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-v5-chat-capture.timer
  memory-v1-deferred-reconciliation-scan.timer
)

run_id=$(cat /proc/sys/kernel/random/uuid)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="$snapshot_dir/memory_pre_v5_component_thread_binding_${stamp}_${run_id}.dump"
report="$snapshot_dir/memory_v1_v5_component_thread_binding_${stamp}_${run_id}.json"
lock=/run/lock/memory-v1-v5-component-thread-binding.lock
tables=$(mktemp /tmp/memory-v1-v5-binding-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-binding-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-binding-after.XXXXXX)
timer_state=$(mktemp /tmp/memory-v1-v5-binding-timers.XXXXXX)
qdrant_before=$(mktemp /tmp/memory-v1-v5-binding-qdrant-before.XXXXXX)
qdrant_after=$(mktemp /tmp/memory-v1-v5-binding-qdrant-after.XXXXXX)
quiesced=false

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U "$maintenance_role" -d "$database" -c "$1"
}

run_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U "$maintenance_role" -d "$database"
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

capture_memory_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_scalar "
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
    -X POST "$qdrant_url/collections/$qdrant_collection/points/scroll" \
    -d '{"limit":10000,"with_payload":true,"with_vector":false}' \
    | jq -e -S -c '{points:(.result.points|sort_by(.id|tostring)),next_page_offset:.result.next_page_offset}' \
    >"$output"
}

exec 9>"$lock"
flock -n 9

for required in "$manifest" "$apply_sql"; do
  [[ -f "$repo_root/$required" ]]
done
[[ -f "$selector_report" ]]
[[ "$(sha256sum "$repo_root/$manifest" | cut -d' ' -f1)" == "$manifest_sha" ]]
[[ "$(sha256sum "$repo_root/$apply_sql" | cut -d' ' -f1)" == "$apply_sql_sha" ]]
[[ "$(sha256sum "$selector_report" | cut -d' ' -f1)" == "$selector_report_sha" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
[[ -z "$(git status --short)" ]]

[[ "$(jq -r '.authorization.apply_authorized' "$repo_root/$manifest")" == true ]]
[[ "$(jq -r '.controls.external_model_calls' "$repo_root/$manifest")" == 0 ]]
[[ "$(jq -r '.controls.packet_writes' "$repo_root/$manifest")" == 0 ]]
[[ "$(jq -r '.actions | length' "$selector_report")" == 1 ]]
[[ "$(jq -r '.actions[0].action' "$selector_report")" == propose_bind ]]

preflight=$(psql_scalar "
  SELECT jsonb_build_object(
    'current_user',current_user,
    'database',current_database(),
    'binding_rows',(SELECT count(*) FROM memory.project_thread_binding_event),
    'target_thread',(SELECT count(*) FROM public.threads
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND id='d776c8ef-7f3d-45b2-8820-4be87b7ca19d'),
    'target_project',(SELECT count(*) FROM memory.project_space
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'),
    'binding_api',has_function_privilege('brains_app',
      'memory.apply_owner_project_thread_binding_v5(uuid,uuid,uuid,text,text)','EXECUTE'),
    'forced_rls',(SELECT relrowsecurity AND relforcerowsecurity
      FROM pg_class WHERE oid='memory.project_thread_binding_event'::regclass)
  )::text
")
[[ "$(jq -r '.current_user' <<<"$preflight")" == sage ]]
[[ "$(jq -r '.database' <<<"$preflight")" == memory ]]
[[ "$(jq -r '.binding_rows' <<<"$preflight")" == 0 ]]
[[ "$(jq -r '.target_thread' <<<"$preflight")" == 1 ]]
[[ "$(jq -r '.target_project' <<<"$preflight")" == 1 ]]
[[ "$(jq -r '.binding_api and .forced_rls' <<<"$preflight")" == true ]]

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
docker exec "$container" pg_dump -U "$maintenance_role" -d "$database" \
  -Fc --no-owner --no-privileges >"$backup"
chmod 0600 "$backup"
[[ -s "$backup" ]]
docker exec -i "$container" pg_restore -l <"$backup" >/dev/null
backup_sha=$(sha256sum "$backup" | cut -d' ' -f1)

psql_scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' AND table_name<>'project_thread_binding_event' ORDER BY table_name" >"$tables"
capture_memory_state "$before"
capture_qdrant "$qdrant_before"
qdrant_before_sha=$(sha256sum "$qdrant_before" | cut -d' ' -f1)

run_sql <"$repo_root/$apply_sql"

capture_memory_state "$after"
capture_qdrant "$qdrant_after"
qdrant_after_sha=$(sha256sum "$qdrant_after" | cut -d' ' -f1)
cmp -s "$before" "$after"
[[ "$qdrant_before_sha" == "$qdrant_after_sha" ]]

postflight=$(psql_scalar "
  SELECT jsonb_build_object(
    'binding_rows',(SELECT count(*) FROM memory.project_thread_binding_event
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND operation_id='a8dc45c0-7a5c-5885-ad3b-5a696dbb33d6'
        AND thread_id='d776c8ef-7f3d-45b2-8820-4be87b7ca19d'
        AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
        AND action='bind'),
    'current_binding_rows',(SELECT count(*) FROM memory.current_project_thread_binding_v5
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND thread_id='d776c8ef-7f3d-45b2-8820-4be87b7ca19d'
        AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'),
    'other_owner_rows',(SELECT count(*) FROM memory.project_thread_binding_event
      WHERE owner_user_id<>'1240822d-ac9a-4096-95aa-e2b24d36ef50'),
    'packet_rows',(SELECT count(*) FROM memory.evidence_extraction_packet_v5)
  )::text
")
[[ "$(jq -r '.binding_rows' <<<"$postflight")" == 1 ]]
[[ "$(jq -r '.current_binding_rows' <<<"$postflight")" == 1 ]]
[[ "$(jq -r '.other_owner_rows' <<<"$postflight")" == 0 ]]
[[ "$(jq -r '.packet_rows' <<<"$postflight")" == 0 ]]

restore_timers

jq -n \
  --arg run_id "$run_id" \
  --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg commit "$(git rev-parse HEAD)" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg apply_sql_sha256 "$apply_sql_sha" \
  --arg selector_report_sha256 "$selector_report_sha" \
  --arg backup_path "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after_sha" \
  --argjson postflight "$postflight" \
  '{
    report_version:"memory_v1_v5_component_thread_binding_apply_v1",
    status:"passed",run_id:$run_id,created_at:$created_at,commit:$commit,
    manifest_sha256:$manifest_sha256,
    apply_sql_sha256:$apply_sql_sha256,
    selector_report_sha256:$selector_report_sha256,
    backup:{path:$backup_path,sha256:$backup_sha256},
    postflight:$postflight,
    proofs:{
      replay_zero_write:true,cross_owner_rejected:true,
      non_target_memory_unchanged:true,qdrant_unchanged:true,
      qdrant_sha256:$qdrant_sha256,timers_restored:true,
      external_model_calls:0,packet_writes:0,
      retrieval_active:false,prompt_influence:false
    }
  }' >"$report"
chmod 0600 "$report"

printf 'memory_v1_v5_component_thread_binding_apply: PASS\n'
printf 'backup=%s\n' "$backup"
printf 'report=%s\n' "$report"

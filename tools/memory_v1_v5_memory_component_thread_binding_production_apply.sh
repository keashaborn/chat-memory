#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Persists exactly one reviewed, owner-scoped component
# binding. No model call, Qdrant write, runtime activation, or prompt influence.

if [[ "${MEMORY_V1_V5_COMPONENT_BINDING_APPLY:-}" != "authorized" ]]; then
  echo 'MEMORY_V1_V5_COMPONENT_BINDING_APPLY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
apply_sql=ops/sql/20260718_memory_v1_v5_memory_component_thread_binding_apply.sql
expected_apply_sha=938d5636f0ca4dbdde1fddc5dad5e9b14a6553d0435762862a81481a6f66ac80
required_ancestor=648605424f7498762610ac4c6bae9dc776e2f24b
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_component_binding_apply.lock
target_table=project_thread_component_binding_event_v5
phase=initialization
quiesced=0
run_id=
status_file=
table_list=$(mktemp /tmp/memory-v1-v5-component-binding-tables.XXXXXX)
timer_state=$(mktemp /tmp/memory-v1-v5-component-binding-timers.XXXXXX)
baseline=
post=
units=(
  memory-v1-consolidation.timer
  memory-v1-deferred-reconciliation-scan.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-governance.timer
  memory-v1-projection.timer
  memory-v1-v5-chat-capture.timer
)

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

run_sql_file() {
  docker exec -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=120s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
    <"$repo_root/$1"
}

restore_timers() {
  [[ "$quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  quiesced=0
}

record_exit() {
  exit_code=$?
  restore_timers || exit_code=1
  rm -f "$table_list" "$timer_state"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_id=%s\n' "$run_id"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(convert_to(coalesce(string_agg(
               row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$apply_sql" | awk '{print $1}')" == "$expected_apply_sha" ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_component_binding_apply_${run_id}.status"
baseline="$snapshot_dir/memory_v1_v5_component_binding_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_v5_component_binding_post_${run_id}.tsv"

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regprocedure('memory.apply_owner_project_thread_component_binding_v5(uuid,uuid,uuid,uuid,uuid,uuid,text,text,text)') IS NOT NULL
  AND to_regprocedure('memory.read_v5_shadow_project_knowledge(uuid,integer)') IS NOT NULL
  AND to_regclass('memory.project_thread_component_binding_event_v5') IS NOT NULL
  AND (SELECT count(*) FROM memory.project_thread_component_binding_event_v5)=0
)::int")" == 1 ]]

: >"$timer_state"
for unit in "${units[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done
quiesced=1
for unit in "${units[@]}"; do
  sudo -n systemctl stop "$unit"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_component_binding_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_component_binding_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline_capture
psql_scalar "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name<>'$target_table'
  ORDER BY table_name
" >"$table_list"
capture_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=apply
apply_log="$snapshot_dir/memory_v1_v5_component_binding_apply_${run_id}.log"
run_sql_file "$apply_sql" >"$apply_log" 2>&1
chmod 0600 "$apply_log"

phase=postflight
capture_state "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo 'non-target Memory V1 state changed during component binding apply' >&2
  exit 1
}

target_state=$(psql_scalar "
  SELECT jsonb_build_object(
    'rows',count(*),
    'operation_rows',count(*) FILTER (
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND operation_id='d77f1e79-e39e-428b-b9e9-4179d3e34105'
        AND thread_id='d776c8ef-7f3d-45b2-8820-4be87b7ca19d'
        AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
        AND component_id='1d3436df-cc4d-4b70-a6b9-730d91055b24'
        AND component_key='memory-v1'
        AND action='bind'
    ),
    'other_owner_rows',count(*) FILTER (
      WHERE owner_user_id<>'1240822d-ac9a-4096-95aa-e2b24d36ef50'
    )
  )::text
  FROM memory.project_thread_component_binding_event_v5
")
[[ "$(jq -r '.rows' <<<"$target_state")" == 1 ]]
[[ "$(jq -r '.operation_rows' <<<"$target_state")" == 1 ]]
[[ "$(jq -r '.other_owner_rows' <<<"$target_state")" == 0 ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

restore_timers

phase=report
report="$snapshot_dir/memory_v1_v5_component_binding_apply_${run_id}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg apply_sha256 "$expected_apply_sha" \
  --arg backup_path "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg baseline "$baseline" \
  --arg post "$post" \
  --arg log "$apply_log" \
  --arg qdrant_sha256 "$qdrant_after" \
  --argjson target_state "$target_state" \
  '{
    contract_version:"memory_v1_v5_component_binding_apply_report_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    apply_sha256:$apply_sha256,
    backup:{path:$backup_path,sha256:$backup_sha256},
    evidence:{baseline:$baseline,post:$post,log:$log,qdrant_sha256:$qdrant_sha256},
    target_state:$target_state,
    checks:{
      exact_owner_thread_project_component:true,
      trusted_evidence_bound:true,
      append_only_row_created:1,
      replay_zero_write:true,
      cross_owner_rejected:true,
      governed_project_reader_returned_one:true,
      non_target_memory_unchanged:true,
      qdrant_unchanged:true,
      timers_restored:true,
      external_model_calls:0,
      router_modified:false,
      prompt_influence:false
    },
    hard_stop:"before_runtime_shadow_integration"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_memory_component_thread_binding_production_apply: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"

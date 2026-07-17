#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Performs the single authorized Memory V1 component
# registration, with backup, quiescence, bounded state proofs, and replay.

repo_root=$(git rev-parse --show-toplevel)
manifest=ops/manifests/memory_v1_v5_memory_component_registration_apply_20260717.json
manifest_sha=9c185a315d4b1ed7e2c509560c1ef9b74840607467236514f9a0c7c848c63d76
dry_run_manifest=ops/manifests/memory_v1_v5_memory_component_registration_dry_run_20260717.json
dry_run_manifest_sha=1af8e98955860b82957ff462155d2e40865523a6bfc66fc7ff156587444693ea
apply_sql=ops/sql/20260717_memory_v1_v5_memory_component_registration_apply.sql
apply_sql_sha=028c1751908f39b4957672130f19fb150c3cd8bfee5e3c6cadaa9634fa7a3c71
required_ancestor=b14231e
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
backup="$snapshot_dir/memory_pre_v5_memory_component_registration_${stamp}_${run_id}.dump"
report="$snapshot_dir/memory_v1_v5_memory_component_registration_${stamp}_${run_id}.json"
lock=/run/lock/memory-v1-v5-memory-component-registration.lock
tables=$(mktemp /tmp/memory-v1-v5-component-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-component-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-component-after.XXXXXX)
timer_state=$(mktemp /tmp/memory-v1-v5-component-timers.XXXXXX)
qdrant_before=$(mktemp /tmp/memory-v1-v5-component-qdrant-before.XXXXXX)
qdrant_after=$(mktemp /tmp/memory-v1-v5-component-qdrant-after.XXXXXX)
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
    [[ -n "$unit" && -n "$enabled" && -n "$active" ]]
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

for required in "$manifest" "$dry_run_manifest" "$apply_sql"; do
  [[ -f "$repo_root/$required" ]]
done
[[ "$(sha256sum "$repo_root/$manifest" | cut -d' ' -f1)" == "$manifest_sha" ]]
[[ "$(sha256sum "$repo_root/$dry_run_manifest" | cut -d' ' -f1)" == "$dry_run_manifest_sha" ]]
[[ "$(sha256sum "$repo_root/$apply_sql" | cut -d' ' -f1)" == "$apply_sql_sha" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
[[ -z "$(git status --short)" ]]

[[ "$(jq -r '.authorization.apply_authorized' "$repo_root/$manifest")" == true ]]
[[ "$(jq -r '.expected_writes["memory.project_component_v5"]' "$repo_root/$manifest")" == 1 ]]
[[ "$(jq -r '.expected_writes["memory.project_component_alias_v5"]' "$repo_root/$manifest")" == 1 ]]
[[ "$(jq -r '.expected_writes["memory.project_component_registration_event_v5"]' "$repo_root/$manifest")" == 1 ]]

preflight=$(psql_scalar "
  SELECT jsonb_build_object(
    'current_user',current_user,
    'database',current_database(),
    'project_rows',(SELECT count(*) FROM memory.project_space
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
        AND project_key='verbal-sage'),
    'component_rows',(SELECT count(*) FROM memory.project_component_v5),
    'alias_rows',(SELECT count(*) FROM memory.project_component_alias_v5),
    'event_rows',(SELECT count(*) FROM memory.project_component_registration_event_v5),
    'component_api',has_function_privilege('brains_app',
      'memory.apply_owner_project_component_v5(uuid,uuid,text,text,uuid,text[],jsonb)','EXECUTE'),
    'read_api',has_function_privilege('brains_app',
      'memory.read_owner_project_components_v5(uuid)','EXECUTE'),
    'forced_rls',(SELECT count(*)=3 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='memory'
        AND c.relname IN ('project_component_v5','project_component_alias_v5','project_component_registration_event_v5')
        AND c.relrowsecurity AND c.relforcerowsecurity)
  )::text
")
[[ "$(jq -r '.current_user' <<<"$preflight")" == sage ]]
[[ "$(jq -r '.database' <<<"$preflight")" == memory ]]
[[ "$(jq -r '.project_rows' <<<"$preflight")" == 1 ]]
[[ "$(jq -r '.component_rows + .alias_rows + .event_rows' <<<"$preflight")" == 0 ]]
[[ "$(jq -r '.component_api and .read_api and .forced_rls' <<<"$preflight")" == true ]]

: >"$timer_state"
for unit in "${units[@]}"; do
  enabled=$(systemctl is-enabled "$unit")
  active=$(systemctl is-active "$unit")
  printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active" >>"$timer_state"
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

psql_scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' AND table_name NOT IN ('project_component_v5','project_component_alias_v5','project_component_registration_event_v5') ORDER BY table_name" >"$tables"
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
    'component_rows',(SELECT count(*) FROM memory.project_component_v5
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
        AND component_key='memory-v1'
        AND display_name='Memory V1'
        AND parent_component_id IS NULL
        AND metadata='{\"component_kind\":\"memory_architecture\",\"registration_source\":\"controlled_registry_v1\"}'::jsonb),
    'alias_rows',(SELECT count(*) FROM memory.project_component_alias_v5
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND project_id='08cd6a8a-5599-43d5-8d5c-b59401df8ccc'
        AND normalized_alias='memory-v1'),
    'event_rows',(SELECT count(*) FROM memory.project_component_registration_event_v5
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND operation_id='9698390e-cfd7-4db6-9af0-df7afb016ffb'
        AND outcome='created'),
    'other_owner_rows',(SELECT count(*) FROM memory.project_component_v5
      WHERE owner_user_id<>'1240822d-ac9a-4096-95aa-e2b24d36ef50')
  )::text
")
[[ "$(jq -r '.component_rows' <<<"$postflight")" == 1 ]]
[[ "$(jq -r '.alias_rows' <<<"$postflight")" == 1 ]]
[[ "$(jq -r '.event_rows' <<<"$postflight")" == 1 ]]
[[ "$(jq -r '.other_owner_rows' <<<"$postflight")" == 0 ]]

restore_timers

jq -n \
  --arg run_id "$run_id" \
  --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg commit "$(git rev-parse HEAD)" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg apply_sql_sha256 "$apply_sql_sha" \
  --arg backup_path "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after_sha" \
  --argjson postflight "$postflight" \
  '{
    report_version:"memory_v1_v5_memory_component_registration_apply_v1",
    status:"passed",
    run_id:$run_id,
    created_at:$created_at,
    commit:$commit,
    manifest_sha256:$manifest_sha256,
    apply_sql_sha256:$apply_sql_sha256,
    backup:{path:$backup_path,sha256:$backup_sha256},
    postflight:$postflight,
    proofs:{
      replay_zero_write:true,
      cross_owner_rejected:true,
      non_target_memory_unchanged:true,
      qdrant_unchanged:true,
      qdrant_sha256:$qdrant_sha256,
      timers_restored:true,
      retrieval_active:false,
      prompt_influence:false
    }
  }' >"$report"
chmod 0600 "$report"

printf 'memory_v1_v5_memory_component_registration_apply: PASS\n'
printf 'backup=%s\n' "$backup"
printf 'report=%s\n' "$report"

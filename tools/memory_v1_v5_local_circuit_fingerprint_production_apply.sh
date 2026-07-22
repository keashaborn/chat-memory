#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the compiler-fingerprinted local circuit breaker
# after a fresh backup and rollback-only test. No memory rows or vectors change.

if [[ "${MEMORY_V1_V5_LOCAL_CIRCUIT_FINGERPRINT_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_LOCAL_CIRCUIT_FINGERPRINT_APPLY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260722_memory_v1_v5_local_circuit_fingerprint.sql
test_sql=tests/memory_v1_v5_local_circuit_fingerprint.sql
manifest=ops/manifests/memory_v1_v5_local_circuit_fingerprint_20260722.json
expected_migration_sha=aedf946bfef685e02680dfd128a20cd662b0ec6b694e6ccfad0adecded2fcfd6
expected_test_sha=650af54da557562d9373d9287cf006225b43447c60a466f3150c4999d423debf
expected_manifest_sha=d13f01598a40dc3f8d0ac5c50ec7030afd02e4b5613c14190946beca289473bd
required_ancestor=a98858355cb4a4c8c83f8de4e9db72a756ab6519
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
job=20ba21a9-855b-4606-93fb-28065e2e13d1
content_sha=70a0cef5245b571bc8fceaa4c744cc273b488bbacac08acb2718f831a2b920a1
blocked_run=d8d9ddb5-d6f4-4092-b489-d4d1c1ec8fc8
blocked_event=1365a304-d972-48a6-8a7b-9bec2ffff46b
old_function_sha=9d10f1507b2a619ad011c7033076af8c52b811fc3cdaae5c2f9b79a6f3fb56fb
new_function_sha=01052eb7ea5a2add2a9795bcc960408a7702fdc1baee83e17b12722889307970
container=brains-postgres-1
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_local_circuit_fingerprint.lock
phase=initialization
run_tag=
status_file=
units_quiesced=0
unit_state=$(mktemp /tmp/memory-v1-v5-local-circuit-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-local-circuit-tables.XXXXXX)

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}
restore_timers() {
  [[ "$units_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
}
record_exit() {
  exit_code=$?
  restore_timers || exit_code=1
  rm -f "$unit_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
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
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_scalar "SELECT count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
        ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}
function_sha() {
  psql_scalar "SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')"
}

for required in "$migration" "$test_sql" "$manifest"; do
  [[ -f "$repo_root/$required" ]]
done
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$expected_test_sha" ]]
[[ "$(sha256sum "$repo_root/$manifest" | cut -d' ' -f1)" == "$expected_manifest_sha" ]]
[[ "$(jq -r '.owner_user_id' "$repo_root/$manifest")" == "$owner" ]]
[[ "$(jq -r '.job_id' "$repo_root/$manifest")" == "$job" ]]
[[ "$(jq -r '.blocked_run_id' "$repo_root/$manifest")" == "$blocked_run" ]]
[[ "$(jq -r '.blocked_event_id' "$repo_root/$manifest")" == "$blocked_event" ]]
[[ "$(jq -r '.prior_function_sha256' "$repo_root/$manifest")" == "$old_function_sha" ]]
[[ "$(jq -r '.installed_function_sha256' "$repo_root/$manifest")" == "$new_function_sha" ]]

exec 9>"$lock_file"
flock -n 9
umask 077
set -a
source "$repo_root/.env"
set +a
[[ -n "${VS_SERVICE_TOKEN:-}" ]]
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_local_circuit_${run_tag}.status"
before="$snapshot_dir/memory_v1_v5_local_circuit_before_${run_tag}.tsv"
after="$snapshot_dir/memory_v1_v5_local_circuit_after_${run_tag}.tsv"

phase=preflight
[[ "$(function_sha)" == "$old_function_sha" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid
    AND evidence_content_sha256='$content_sha' AND status='pending'
    AND attempts=2 AND lease_token IS NULL AND lease_expires_at IS NULL")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.v5_local_inference_event
  WHERE owner_user_id='$owner'::uuid AND event_id='$blocked_event'::uuid
    AND run_id='$blocked_run'::uuid AND action='blocked'
    AND outcome='circuit_open' AND local_model_calls=0
    AND external_model_calls=0")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid")" == 0 ]]

: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$unit_state" ]]
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$unit_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$unit_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_local_circuit_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_local_circuit_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$backup.catalog"
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
psql_scalar "SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)

phase=install_and_rollback_tests
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d memory <"$repo_root/$migration" >/dev/null
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d memory <"$repo_root/$test_sql" >/dev/null

phase=postflight
[[ "$(function_sha)" == "$new_function_sha" ]]
capture_state "$after"
cmp -s "$before" "$after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid")" == 0 ]]
[[ "$(psql_scalar "SELECT (p.prosecdef
  AND p.proowner='memory_v5_local_inference_maintainer'::regrole
  AND p.proconfig=ARRAY['search_path=pg_catalog']::text[]
  AND has_function_privilege('brains_app',p.oid,'EXECUTE')
  AND NOT EXISTS (SELECT 1 FROM aclexplode(p.proacl) AS acl
    WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE'))::integer
  FROM pg_proc AS p WHERE p.oid=
  'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure")" == 1 ]]
restore_timers
curl --fail --silent --show-error --max-time 10 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null

phase=report
report="$snapshot_dir/memory_v1_v5_local_circuit_${run_tag}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg manifest_sha "$expected_manifest_sha" \
  --arg backup "$backup" --arg qdrant_sha "$qdrant_before" \
  '{contract_version:"memory_v1_v5_local_circuit_fingerprint_apply_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    manifest_sha256:$manifest_sha,backup:$backup,qdrant_sha256:$qdrant_sha,
    checks:{fresh_backup:true,rollback_only_security_tests:true,
      compiler_fingerprinted_circuit:true,memory_rows_unchanged:true,
      account_isolation:true,qdrant_unchanged:true,model_calls:0,
      packet_writes:0,claim_writes:0,retrieval_changes:0,
      prompt_influence_changes:0,timers_restored:true},
    hard_stop:"before_private_compact_prompt_canary"}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'memory_v1_v5_local_circuit_fingerprint_production_apply: PASS\n'
printf 'report=%s\n' "$report"

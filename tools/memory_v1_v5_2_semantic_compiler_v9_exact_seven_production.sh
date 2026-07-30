#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Deploys compiler v9 and its single-hash persistence
# compatibility, enqueues exactly seven reviewed records, runs private local
# extraction, and stops before routing, staging, claims, Qdrant, or prompts.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for the private endpoint key' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_SEMANTIC_COMPILER_V9_EXACT_SEVEN:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_SEMANTIC_COMPILER_V9_EXACT_SEVEN=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-/opt/chat-memory/.env}"
set +a

live=/opt/chat-memory
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
expected_live_head=e6d8bbc46138eb95b53188ac66b82c9e00bbf0df
required_source_commit=ca8dcbd7db6d9b8eb572d87cb28f93b4da1321dd
selector=20260730_v5_2_semantic_compiler_v9_reextract_v1
compiler_version=memory_v1_semantic_policy_compiler_v9
compiler_sha=738cc80f374e3c7e441fd03f964b77d01401422d286a9bc52205c6e060767ae6
manifest_sha=dab07b531eb987fba80b67cbc4c106a4d1fe3375add7cb9bb0de7842eaa6b91e
old_writer_sha=4dcbd998364abc91d5f6abd0844546c74387f4c057fc876efbee7c222eb0c8ab
new_writer_sha=2b3182f59091a7d93697ae79ee6a8e1cc7541829f957b5a859c47cf28707d190
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_semantic_compiler_v9_exact_seven.lock

manifest=manifests/memory_v1_v5_2_semantic_compiler_v9_reextract_20260730.json
compat_migration=ops/sql/20260730_memory_v1_v5_2_compiler_v9_persistence_compat.sql
compat_rollback=ops/sql/20260730_memory_v1_v5_2_compiler_v9_persistence_compat_rollback.sql
compat_test=tests/memory_v1_v5_2_compiler_v9_persistence_compat.sql
compat_clone=tools/memory_v1_v5_2_compiler_v9_persistence_compat_clone.sh
selector_migration=ops/sql/20260730_memory_v1_v5_2_semantic_compiler_v9_reextract.sql
selector_rollback=ops/sql/20260730_memory_v1_v5_2_semantic_compiler_v9_reextract_rollback.sql
selector_test=tests/memory_v1_v5_2_semantic_compiler_v9_reextract.sql
selector_clone=tools/memory_v1_v5_2_semantic_compiler_v9_reextract_clone.sh
provider=scripts/memory_v1_relational_extraction_v5_local_provider.py
canary=tools/memory_v1_v5_local_inference_canary_apply.sh

declare -A expected_sha256=(
  ["$manifest"]=c09aed8251a981baaecbf4ecedd3d243ac24a40e7f68697643e3b2409138ac57
  ["$compat_migration"]=b219f9e1c5682cbb3fbc70b0793bfb06d9d167d13d75246e283d8eea28b930fa
  ["$compat_rollback"]=4723e18d1d826a6a3fc7597feac9cfcf7486b11eac7223663f69e3e8db52ee30
  ["$compat_test"]=518ea14e6d4bac7cf83321934355dd9ccc4f3407e7fb05e6b7781b36f46cc245
  ["$compat_clone"]=b765bdbfc37d907aecf14ab0918f70b06844bb1dadc8b2aa9771168d0c5ad984
  ["$selector_migration"]=20eeba37b35d462e482f2b2d4314e5976a422decc917b172afa2d48d8600cdaf
  ["$selector_rollback"]=e4028100d2860c9d3afef7702812bd526a9f9907561f94d8a93e0c06897c2bc9
  ["$selector_test"]=07b776d95f378a01f05cce06bb950b48b3c814648a93b92bb6ecdf05acb843ee
  ["$selector_clone"]=951fbdc569edf75ffd80d8a816b7e55fb07a9564ee932d32623598859514a715
  ["$provider"]=d30a59c35d671610dc06af12f0fa55b8b1520bb0c3ffdaef9041afe3949af134
  ["$canary"]=b4b8e9c30d46599334e194544d31f33ce8030f9c077f6d6097fc3cdf8334d86f
)

timer_state=$(mktemp /tmp/memory-v9-exact-seven-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v9-exact-seven-tables.XXXXXX)
before_static=$(mktemp /tmp/memory-v9-exact-seven-static-before.XXXXXX)
after_static=$(mktemp /tmp/memory-v9-exact-seven-static-after.XXXXXX)
before_isolation=$(mktemp /tmp/memory-v9-exact-seven-isolation-before.XXXXXX)
after_isolation=$(mktemp /tmp/memory-v9-exact-seven-isolation-after.XXXXXX)
apply_output=$(mktemp /tmp/memory-v9-exact-seven-apply.XXXXXX)
replay_output=$(mktemp /tmp/memory-v9-exact-seven-replay.XXXXXX)
canary_dir=$(mktemp -d /tmp/memory-v9-exact-seven-canaries.XXXXXX)
chmod 0600 "$timer_state" "$table_list" "$before_static" "$after_static" \
  "$before_isolation" "$after_isolation" "$apply_output" "$replay_output"
timers_quiesced=0
brains_was_active=0
phase=initialization
status_file=

scalar() {
  docker exec "$container" psql -U sage -d "$database" -X -At \
    -v ON_ERROR_STOP=1 -c "$1" | tr -d '[:space:]'
}

run_sql() {
  docker exec -i "$container" psql -U sage -d "$database" -X \
    -v ON_ERROR_STOP=1 "$@"
}

writer_sha() {
  scalar "SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null
    else
      systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  rc=$?
  trap - EXIT
  if [[ "$brains_was_active" -eq 1 ]] \
     && [[ "$(systemctl is-active brains.service)" != active ]]; then
    systemctl start brains.service || rc=1
  fi
  if [[ "$timers_quiesced" -eq 1 ]]; then
    restore_timers || rc=1
  fi
  rm -f "$timer_state" "$table_list" "$before_static" "$after_static" \
    "$before_isolation" "$after_isolation" "$apply_output" "$replay_output"
  rm -rf "$canary_dir"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$phase" "$rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$rc"
}
trap record_exit EXIT

capture_static_state() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "SELECT count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
        ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM \"$schema\".\"$table\" AS value) rows")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$table_list"
}

capture_isolation_state() {
  local output=$1
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    WITH target_jobs AS (
      SELECT (value->>'job_id')::uuid AS job_id,
             (value->>'terminal_id')::uuid AS terminal_id
      FROM jsonb_array_elements(
        \$manifest\$$(
          jq -cS '.items' "$manifest"
        )\$manifest\$::jsonb
      )
    ), state(label,row_json) AS (
      SELECT 'other_jobs',to_jsonb(value)::text
      FROM memory.evidence_extraction_job AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'other_terminals',to_jsonb(value)::text
      FROM memory.evidence_intake_terminal AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'other_events',to_jsonb(value)::text
      FROM memory.evidence_extraction_event AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'other_ledger',to_jsonb(value)::text
      FROM memory.v5_local_inference_event AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'other_packets',to_jsonb(value)::text
      FROM memory.evidence_extraction_packet_v5_local AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'same_owner_other_jobs',to_jsonb(value)::text
      FROM memory.evidence_extraction_job AS value
      WHERE owner_user_id='$owner'::uuid
        AND job_id NOT IN (SELECT job_id FROM target_jobs)
      UNION ALL
      SELECT 'same_owner_other_terminals',to_jsonb(value)::text
      FROM memory.evidence_intake_terminal AS value
      WHERE owner_user_id='$owner'::uuid
        AND terminal_id NOT IN (SELECT terminal_id FROM target_jobs)
      UNION ALL
      SELECT 'same_owner_other_events',to_jsonb(value)::text
      FROM memory.evidence_extraction_event AS value
      WHERE owner_user_id='$owner'::uuid
        AND job_id NOT IN (SELECT job_id FROM target_jobs)
      UNION ALL
      SELECT 'same_owner_other_ledger',to_jsonb(value)::text
      FROM memory.v5_local_inference_event AS value
      WHERE owner_user_id='$owner'::uuid
        AND (
          job_id IS NULL
          OR job_id NOT IN (SELECT job_id FROM target_jobs)
        )
      UNION ALL
      SELECT 'same_owner_other_packets',to_jsonb(value)::text
      FROM memory.evidence_extraction_packet_v5_local AS value
      WHERE owner_user_id='$owner'::uuid
        AND job_id NOT IN (SELECT job_id FROM target_jobs)
    ), labels(label) AS (VALUES
      ('other_jobs'),('other_terminals'),('other_events'),
      ('other_ledger'),('other_packets'),
      ('same_owner_other_jobs'),('same_owner_other_terminals'),
      ('same_owner_other_events'),('same_owner_other_ledger'),
      ('same_owner_other_packets')
    )
    SELECT label || E'\\t' || count(row_json)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(
        row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
    FROM labels LEFT JOIN state USING(label)
    GROUP BY label ORDER BY label
  " >"$output"
}

enqueue_items() {
  local expected_outcome=$1 output=$2 item
  : >"$output"
  while IFS= read -r item; do
    operation=$(jq -r '.operation_id' <<<"$item")
    job=$(jq -r '.job_id' <<<"$item")
    terminal=$(jq -r '.terminal_id' <<<"$item")
    evidence=$(jq -r '.evidence_id' <<<"$item")
    content=$(jq -r '.content_sha256' <<<"$item")
    prior_packet=$(jq -r '.prior_packet_id' <<<"$item")
    prior_storage=$(jq -r '.prior_packet_storage_sha256' <<<"$item")
    psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 -At -F $'\t' \
      -v owner="$owner" >>"$output" <<SQL
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT * FROM memory.enqueue_owner_v5_2_semantic_compiler_v9_reextract_v1(
  '$operation','$job','$terminal','$evidence','$content',
  '$prior_packet','$prior_storage','$manifest_sha','$compiler_sha'
);
COMMIT;
SQL
  done < <(jq -cS '.items[]' "$manifest")
  [[ "$(grep -c $'\tpending\t'"$expected_outcome"'$' "$output")" == 7 ]]
}

for artifact in "${!expected_sha256[@]}"; do
  [[ -f "$artifact" ]]
  [[ "$(sha256sum "$artifact" | awk '{print $1}')" == \
    "${expected_sha256[$artifact]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_source_commit" HEAD
target_commit=$(git rev-parse HEAD)
[[ "$(git -C "$live" rev-parse HEAD)" == "$expected_live_head" ]]
[[ -z "$(git -C "$live" status --porcelain)" ]]
git -C "$live" merge-base --is-ancestor "$expected_live_head" "$target_commit"
[[ "$(jq -cS . "$manifest" | tr -d '\n' | sha256sum | awk '{print $1}')" \
  == "$manifest_sha" ]]
[[ "$(jq -r '.items|length' "$manifest")" == 7 ]]
[[ "$(jq -r '.owner_user_id' "$manifest")" == "$owner" ]]
[[ "$(jq -r '.policy_compiler_sha256' "$manifest")" == "$compiler_sha" ]]
[[ "$(writer_sha)" == "$old_writer_sha" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" == 0 ]]

phase=clone_tests
[[ "$(bash "$compat_clone")" == \
  'memory_v1_v5_2_compiler_v9_persistence_compat_clone: PASS' ]]
[[ "$(bash "$selector_clone")" == \
  'memory_v1_v5_2_semantic_compiler_v9_reextract_clone: PASS' ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_semantic_compiler_v9_exact_seven_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_semantic_compiler_v9_exact_seven_${run_tag}.json"

phase=inventory_timers
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
timer_count=$(wc -l <"$timer_state")
(( timer_count >= 15 && timer_count <= 40 ))

phase=quiesce_timers
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"

phase=fresh_backup
partial="$snapshot_dir/.memory_pre_v9_exact_seven_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v9_exact_seven_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
docker exec -i "$container" pg_restore -l <"$partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -U sage -d "$database" -X -At -F $'\t' \
  -c "SELECT table_schema,table_name
      FROM information_schema.tables
      WHERE table_type='BASE TABLE' AND table_schema='memory'
        AND table_name NOT IN (
          'evidence_extraction_job','evidence_intake_terminal',
          'evidence_extraction_event','v5_local_inference_event',
          'evidence_extraction_packet_v5_local'
        )
      ORDER BY table_schema,table_name" >"$table_list"
capture_static_state "$before_static"
capture_isolation_state "$before_isolation"
qdrant_before=$(qdrant_signature)
jobs_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')
terminals_before=$(scalar 'SELECT count(*) FROM memory.evidence_intake_terminal')
events_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')
ledger_before=$(scalar 'SELECT count(*) FROM memory.v5_local_inference_event')
packets_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_packet_v5_local')

phase=stop_brains
brains_was_active=1
systemctl stop brains.service
[[ "$(systemctl is-active brains.service)" == inactive ]]

phase=install_schema
run_sql <"$compat_migration" >/dev/null
run_sql <"$selector_migration" >/dev/null
run_sql <"$compat_test" >/dev/null
first=$(jq -cS '.items[0]' "$manifest")
run_sql \
  -v target_owner="$owner" \
  -v other_owner="$other" \
  -v evidence_id="$(jq -r '.evidence_id' <<<"$first")" \
  -v content_sha256="$(jq -r '.content_sha256' <<<"$first")" \
  -v prior_packet_id="$(jq -r '.prior_packet_id' <<<"$first")" \
  -v prior_packet_storage_sha256="$(jq -r '.prior_packet_storage_sha256' <<<"$first")" \
  -v operation_id="$(jq -r '.operation_id' <<<"$first")" \
  -v job_id="$(jq -r '.job_id' <<<"$first")" \
  -v terminal_id="$(jq -r '.terminal_id' <<<"$first")" \
  -v manifest_sha256="$manifest_sha" \
  -v compiler_sha256="$compiler_sha" \
  <"$selector_test" >/dev/null
[[ "$(writer_sha)" == "$new_writer_sha" ]]

phase=deploy_code
git -C "$live" merge --ff-only "$target_commit"
[[ "$(git -C "$live" rev-parse HEAD)" == "$target_commit" ]]
[[ -z "$(git -C "$live" status --porcelain)" ]]
systemctl start brains.service
[[ "$(systemctl is-active brains.service)" == active ]]
for _attempt in $(seq 1 30); do
  http_code=$(curl -sS -o /dev/null -w '%{http_code}' \
    http://127.0.0.1:8088/docs || true)
  [[ "$http_code" == 200 ]] && break
  sleep 1
done
[[ "$http_code" == 200 ]]

phase=transactional_enqueue
enqueue_items applied "$apply_output"

phase=selector_replay
enqueue_items replayed "$replay_output"

phase=private_extraction
index=0
while IFS= read -r item; do
  index=$((index+1))
  job=$(jq -r '.job_id' <<<"$item")
  evidence=$(jq -r '.evidence_id' <<<"$item")
  content=$(jq -r '.content_sha256' <<<"$item")
  run_id=$(jq -r '.run_id' <<<"$item")
  output="$canary_dir/$index.out"
  MEMORY_V1_PREDICATE_CONTRACT_PROFILE=v5_2 \
  MEMORY_V1_V5_LOCAL_INFERENCE_CANARY=authorized \
  MEMORY_V1_V5_2_LOCAL_INFERENCE_CANARY=authorized \
    bash "$canary" "$job" "$evidence" "$content" "$run_id" 0 4096 \
      >"$output"
  grep -qx 'outcome=accepted' "$output"
done < <(jq -cS '.items[]' "$manifest")
[[ "$index" == 7 ]]

phase=postflight
capture_static_state "$after_static"
capture_isolation_state "$after_isolation"
cmp -s "$before_static" "$after_static"
cmp -s "$before_isolation" "$after_isolation"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')" \
  == "$((jobs_before+7))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_intake_terminal')" \
  == "$((terminals_before+7))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" \
  == "$((events_before+21))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_inference_event')" \
  == "$((ledger_before+14))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_packet_v5_local')" \
  == "$((packets_before+7))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND selector_version='$selector'
    AND status='review_required' AND attempts=1")" == 7 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$other'::uuid AND selector_version='$selector'")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND policy_compiler_sha256='$compiler_sha'
    AND job_id IN (
      SELECT (value->>'job_id')::uuid
      FROM jsonb_array_elements(
        \$manifest\$$(
          jq -cS '.items' "$manifest"
        )\$manifest\$::jsonb
      )
    ) AND external_model_calls=0")" == 7 ]]
local_calls=$(scalar "SELECT coalesce(sum(local_model_calls),0)
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND policy_compiler_sha256='$compiler_sha'
    AND job_id IN (
      SELECT (value->>'job_id')::uuid
      FROM jsonb_array_elements(
        \$manifest\$$(
          jq -cS '.items' "$manifest"
        )\$manifest\$::jsonb
      )
    )")
(( local_calls >= 0 && local_calls <= 1 ))
[[ "$(scalar "SELECT coalesce(sum(local_model_calls),0)
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND job_id<>'ad81be18-93b0-5907-aefa-9a03e00e79cf'::uuid
    AND job_id IN (
      SELECT (value->>'job_id')::uuid
      FROM jsonb_array_elements(
        \$manifest\$$(
          jq -cS '.items' "$manifest"
        )\$manifest\$::jsonb
      )
    )")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid
    AND packet_id IN (
      SELECT packet_id FROM memory.evidence_extraction_packet_v5_local
      WHERE policy_compiler_sha256='$compiler_sha'
    )")" == 0 ]]

phase=restore_timers
restore_timers
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  [[ "$(systemctl is-active "$unit")" == "$active" ]]
done <"$timer_state"

phase=report
packet_summary=$(docker exec "$container" psql -U sage -d "$database" \
  -X -Atqc "
  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'job_id',job_id,
    'local_model_calls',local_model_calls,
    'manual_review_required',manual_review_required,
    'entities',jsonb_array_length(normalized_packet->'entity_mentions'),
    'observations',jsonb_array_length(normalized_packet->'observations'),
    'deferrals',jsonb_array_length(normalized_packet->'deferrals')
  ) ORDER BY job_id),'[]'::jsonb)
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND policy_compiler_sha256='$compiler_sha'
    AND job_id IN (
      SELECT (value->>'job_id')::uuid
      FROM jsonb_array_elements(
        \$manifest\$$(
          jq -cS '.items' "$manifest"
        )\$manifest\$::jsonb
      )
    )")
jq -n \
  --arg contract_version memory_v1_v5_2_semantic_compiler_v9_exact_seven_v1 \
  --arg commit "$target_commit" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  --arg writer_sha256 "$(writer_sha)" \
  --argjson timer_count "$timer_count" \
  --argjson local_model_calls "$local_calls" \
  --argjson packet_summary "$packet_summary" \
  '{
    contract_version:$contract_version,
    outcome:"pass",
    commit:$commit,
    manifest_sha256:$manifest_sha256,
    compiler_version:"memory_v1_semantic_policy_compiler_v9",
    packet_writer_sha256:$writer_sha256,
    bounded_writes:{
      jobs:7,terminals:7,extraction_events:21,
      local_inference_events:14,immutable_packets:7
    },
    local_model_calls:$local_model_calls,
    external_model_calls:0,
    packets:$packet_summary,
    zero_write_replay_proved:true,
    account_isolation_proved:true,
    non_target_memory_rows_unchanged:true,
    qdrant_unchanged:true,
    routing:0,staging:0,claims:0,projections:0,
    retrieval:0,prompt_influence:0,
    timers:{count:$timer_count,restored:true},
    backup:{path:$backup,sha256:$backup_sha256},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_durable_stance_promotion"
  }' >"$report"
chmod 0600 "$report"
report_sha=$(sha256sum "$report" | awk '{print $1}')
printf '%s  %s\n' "$report_sha" "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_2_semantic_compiler_v9_exact_seven: PASS\n'
printf 'report=%s\nreport_sha256=%s\nbackup=%s\nbackup_sha256=%s\n' \
  "$report" "$report_sha" "$backup" "$backup_sha"

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the zero-call review compatibility and
# transactionally records review routes for exactly two compiler-v8 packets.
# It stops before relational staging, claims, Qdrant, retrieval, or prompts.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for backup and timer control' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_COMPILER_V8_ROUTE:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_COMPILER_V8_ROUTE=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-$repo_root/.env}"
set +a

container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
care_packet=b76915b8-0603-50e8-b263-761da39f5651
profession_packet=6ae4a8e6-b207-5201-a997-53fb2363fc9d
care_evidence=fea59e7e-30f5-4139-b634-97b291c88e14
profession_evidence=dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5
required_base=6bb6e59d8953adf8b9e7e8810263806d5500e451
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_compiler_v8_route.lock
migration=ops/sql/20260725_memory_v1_v5_2_router_zero_call_review_compat.sql
sql_test=tests/memory_v1_v5_2_router_zero_call_review_compat.sql
worker=scripts/memory_v1_v5_2_exact_review_route.py
manifest=manifests/memory_v1_v5_2_compiler_v8_two_packet_route_20260725.json
manifest_sha=28ac2bb11bbb5b1b1213cc93832f16a50b812d79dfc4168815369a8306b4fc97
python_bin=/opt/chat-memory/venv/bin/python

declare -A expected_sha=(
  [ops/sql/20260725_memory_v1_v5_2_router_zero_call_review_compat.sql]=2e468c919cbc1a0f6f910f4b800c77786f54df555272e5f4900caacd69b70812
  [ops/sql/20260725_memory_v1_v5_2_router_zero_call_review_compat_rollback.sql]=6dde829b4e0643b57849f58f752a8e99b52b79a103a0cd7869e3a7eef8d039e6
  [scripts/memory_v1_v5_1_review_local_packet.py]=dacd61733dfdb043df99f6be612e6bf79a567e938b1a3ff4a21fadf4e08f6a55
  [scripts/memory_v1_v5_2_exact_review_route.py]=9921cea123ffa879690b5afdab95f8bf508c10b3a7d7fbdd5aa07abeace8f3bd
  [scripts/memory_v1_v5_2_local_packet_router.py]=ca8b6f75743686a649aa8d9bb888ec6b315c1066724c72f1270df7be919d8056
  [tests/memory_v1_v5_2_router_zero_call_review_compat.sql]=ccc658dd7739e4aa17857979d7f67e823285d7d79f0a5985c87ca73ca64e6900
  [tests/test_memory_v1_v5_2_exact_review_route.py]=a7ca8039a165e0df013c03cc048b33ce608a1e259e8ac3e0dd221173b9399480
  [tests/test_memory_v1_v5_2_local_packet_router.py]=a5e860fc1f9b1c5c088f6b1a2708d552f1487a020a11f5b60019f86f921058db
  [tests/test_memory_v1_v5_2_review_profile.py]=5533b3682b1ccd74e5fbb0d37385b33941cd68cb0ff8bcbf3770c48af7dc435b
  [tools/memory_v1_v5_2_compiler_v8_two_packet_route_clone.sh]=0f13489f744950f28ccca97e04f40f107f4840a39e56829fd98e451ef862ee8d
)

timer_state=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.timers)
table_list=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.tables)
protected_before=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.before)
protected_after=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.after)
routes_before=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.routes-before)
routes_after=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.routes-after)
dry_output=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.dry)
apply_output=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.apply)
replay_output=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.replay)
isolation_output=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.isolation)
chmod 0600 "$timer_state" "$table_list" "$protected_before" "$protected_after" \
  "$routes_before" "$routes_after" "$dry_output" "$apply_output" \
  "$replay_output" "$isolation_output"

phase=initialization
timers_quiesced=0
status_file=

scalar() {
  docker exec "$container" psql -U sage -d "$database" -X -At \
    -v ON_ERROR_STOP=1 -c "$1" | tr -d '[:space:]'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_tables() {
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

capture_non_target_routes() {
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    SELECT count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(
        to_jsonb(value)::text,E'\\n' ORDER BY to_jsonb(value)::text
      ),''),'UTF8'),'sha256'),'hex')
    FROM memory.v5_2_local_packet_route_event AS value
    WHERE packet_id NOT IN ('$care_packet'::uuid,'$profession_packet'::uuid)
  " >"$1"
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
  restore_timers || rc=1
  rm -f "$timer_state" "$table_list" "$protected_before" "$protected_after" \
    "$routes_before" "$routes_after" "$dry_output" "$apply_output" \
    "$replay_output" "$isolation_output"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$phase" "$rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$rc"
}
trap record_exit EXIT

[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_base" HEAD
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(curl -sS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:8088/docs)" == 200 ]]
[[ -x "$python_bin" && -x "$worker" ]]
[[ "$(sha256sum "$manifest" | awk '{print $1}')" == "$manifest_sha" ]]
for file in "${!expected_sha[@]}"; do
  [[ "$(sha256sum "$file" | awk '{print $1}')" == "${expected_sha[$file]}" ]]
done
"$python_bin" -m py_compile \
  scripts/memory_v1_v5_1_review_local_packet.py \
  scripts/memory_v1_v5_2_local_packet_router.py "$worker"
PYTHONPATH="$repo_root" "$python_bin" -m unittest \
  tests/test_memory_v1_v5_2_review_profile.py \
  tests/test_memory_v1_v5_2_local_packet_router.py \
  tests/test_memory_v1_v5_2_exact_review_route.py
[[ "$(stat -c '%a:%U:%G' "$review_root")" == 700:ubuntu:ubuntu ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND packet_id IN ('$care_packet'::uuid,'$profession_packet'::uuid)")" == 2 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id IN ('$care_packet'::uuid,'$profession_packet'::uuid)")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id IN ('$care_evidence'::uuid,'$profession_evidence'::uuid)")" == 0 ]]

for packet in "$care_packet" "$profession_packet"; do
  packet_hash=$(printf %s "$packet" | sha256sum | awk '{print $1}')
  [[ ! -e "$review_root/v5-2-router-$packet_hash-review.json" ]]
  [[ ! -e "$review_root/v5-2-router-$packet_hash-stage.json" ]]
done

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_compiler_v8_route_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_compiler_v8_route_${run_tag}.json"

phase=quiesce_timers
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$timer_state" ]]
timer_count=$(wc -l <"$timer_state" | tr -d '[:space:]')
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
partial="$snapshot_dir/.memory_pre_v5_2_compiler_v8_route_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_compiler_v8_route_${run_tag}.dump"
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
      WHERE table_type='BASE TABLE'
        AND table_schema='memory'
        AND table_name<>'v5_2_local_packet_route_event'
      ORDER BY table_schema,table_name" >"$table_list"
capture_tables "$protected_before"
capture_non_target_routes "$routes_before"
qdrant_before=$(qdrant_signature)
routes_before_count=$(scalar \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')

phase=install_compatibility
docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 \
  -v owner_user_id="$owner" -v other_owner_user_id="$other" \
  -v zero_call_packet_id="$care_packet" \
  -v one_call_packet_id="$profession_packet" \
  <"$sql_test" >/dev/null

run_worker() {
  local actor=$1 output=$2 apply=${3:-false}
  local -a command=(
    "$python_bin" "$worker"
    --owner-user-id "$actor"
    --packet-id "$care_packet"
    --packet-id "$profession_packet"
    --review-root "$review_root"
  )
  if [[ "$apply" == true ]]; then
    command+=(--apply)
  fi
  runuser -u ubuntu -- env \
    POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
    MEMORY_V1_V5_2_EXACT_REVIEW_ROUTE_APPLY=memory_v1_v5_2_exact_review_route_apply_v1 \
    "${command[@]}" >"$output"
}

phase=exact_dry_run
run_worker "$owner" "$dry_output"
jq -e '
  .apply==false and (.plans|length)==2 and
  ([.plans[].route]|all(.=="manual_review_artifact_ready")) and
  .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$dry_output" >/dev/null

phase=transactional_review_route
run_worker "$owner" "$apply_output" true
jq -e '
  .apply==true and .outcome=="manual_review_artifacts_ready" and
  .write_counts.route_events==2 and
  .write_counts.restricted_review_artifacts==4 and
  .write_counts.stage==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .transactional_apply_proved==true and
  .zero_write_replay_proved==true and .external_model_calls==0
' "$apply_output" >/dev/null

phase=zero_write_replay
run_worker "$owner" "$replay_output"
jq -e '
  .apply==false and (.plans|length)==2 and
  ([.plans[].route]|all(.=="no_work")) and
  .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$replay_output" >/dev/null

phase=account_isolation
run_worker "$other" "$isolation_output"
jq -e '
  .apply==false and (.plans|length)==2 and
  ([.plans[].route]|all(.=="no_work")) and
  .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$isolation_output" >/dev/null

phase=postflight
capture_tables "$protected_after"
capture_non_target_routes "$routes_after"
cmp -s "$protected_before" "$protected_after"
cmp -s "$routes_before" "$routes_after"
[[ "$(scalar 'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" \
  == "$((routes_before_count+2))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id IN ('$care_packet'::uuid,'$profession_packet'::uuid)
    AND route='manual_review_artifact_ready'")" == 2 ]]
[[ "$(scalar "SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id IN ('$care_evidence'::uuid,'$profession_evidence'::uuid)")" == 0 ]]
artifact_sha=()
for packet in "$care_packet" "$profession_packet"; do
  packet_hash=$(printf %s "$packet" | sha256sum | awk '{print $1}')
  for suffix in review stage; do
    path="$review_root/v5-2-router-$packet_hash-$suffix.json"
    [[ "$(stat -c '%a:%U:%G' "$path")" == 600:ubuntu:ubuntu ]]
    artifact_sha+=("$(sha256sum "$path" | awk '{print $1}')")
  done
done
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

phase=restore_timers
restore_timers
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  [[ "$(systemctl is-active "$unit")" == "$active" ]]
done <"$timer_state"

phase=report
jq -n \
  --arg contract_version memory_v1_v5_2_compiler_v8_two_packet_route_apply_v1 \
  --arg commit "$(git rev-parse HEAD)" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  --argjson timer_count "$timer_count" \
  --argjson artifact_sha256 "$(printf '%s\n' "${artifact_sha[@]}" \
    | jq -R . | jq -s 'sort')" \
  '{
    contract_version:$contract_version,
    outcome:"pass",
    commit:$commit,
    manifest_sha256:$manifest_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    bounded_writes:{review_route_events:2,restricted_review_artifacts:4},
    transactional_apply_proved:true,
    zero_write_replay_proved:true,
    account_isolation_proved:true,
    non_target_rows_unchanged:true,
    artifact_sha256:$artifact_sha256,
    qdrant:{unchanged:true,sha256:$qdrant_sha256},
    external_model_calls:0,
    relational_stage_batches:0,
    claims:0,
    retrieval_activation:0,
    prompt_influence:0,
    timers:{count:$timer_count,restored:true},
    hard_stop:"before_relational_staging_claims_qdrant_retrieval_or_prompt_influence"
  }' >"$report"
chmod 0600 "$report"
report_sha=$(sha256sum "$report" | awk '{print $1}')
printf '%s  %s\n' "$report_sha" "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_2_compiler_v8_two_packet_route_apply: PASS\n'
printf 'report=%s\nreport_sha256=%s\nbackup=%s\nbackup_sha256=%s\n' \
  "$report" "$report_sha" "$backup" "$backup_sha"

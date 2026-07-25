#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Transactionally records exact review routes for four
# immutable V5.2 packets. No staging, claims, Qdrant, retrieval, or prompt
# influence is permitted.

[[ "$EUID" -eq 0 ]]
if [[ "${MEMORY_V1_V5_2_MIXED_REVIEW_ROUTE_BATCH:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_MIXED_REVIEW_ROUTE_BATCH=authorized is required' >&2
  exit 1
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
manifest=manifests/memory_v1_v5_2_mixed_review_route_batch_20260725.json
manifest_sha=fa369796412929e6d50bff5ae96249f15794de8a9986228709474742e6aa3d4e
worker=scripts/memory_v1_v5_2_exact_review_route.py
worker_sha=bc6d0f915b2e8bc9c42489abc0bbf7f9c8ae593d575b18291d004c58fee5e6ab
unit_test=tests/test_memory_v1_v5_2_exact_review_route.py
unit_test_sha=1c89259f58c2eea89c86e5263cf58f7c51104f555fe8f9f6082279ac444afd90
python_bin=/opt/chat-memory/venv/bin/python
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_mixed_review_route_batch.lock
run_tag=
artifact_dir=
reviews=
status_file=
phase=initialization
timers_quiesced=0
brains_quiesced=0
brains_state_before=
timer_state=$(mktemp /tmp/memory-v5-2-mixed-route.XXXXXX.timers)
table_list=$(mktemp /tmp/memory-v5-2-mixed-route.XXXXXX.tables)
protected_before=$(mktemp /tmp/memory-v5-2-mixed-route.XXXXXX.before)
protected_after=$(mktemp /tmp/memory-v5-2-mixed-route.XXXXXX.after)
routes_before=$(mktemp /tmp/memory-v5-2-mixed-route.XXXXXX.routes-before)
routes_after=$(mktemp /tmp/memory-v5-2-mixed-route.XXXXXX.routes-after)

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
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
    state=$(scalar "
      SELECT count(*)::text || E'\\t' ||
        encode(public.digest(convert_to(coalesce(string_agg(
          to_jsonb(value)::text,E'\\n' ORDER BY to_jsonb(value)::text
        ),''),'UTF8'),'sha256'),'hex')
      FROM \"$schema\".\"$table\" AS value
    ")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$table_list"
}

capture_non_target_routes() {
  local output=$1
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "
      SELECT count(*)::text || E'\\t' ||
        encode(public.digest(convert_to(coalesce(string_agg(
          to_jsonb(value)::text,E'\\n' ORDER BY to_jsonb(value)::text
        ),''),'UTF8'),'sha256'),'hex')
      FROM memory.v5_2_local_packet_route_event AS value
      WHERE packet_id<>ALL(string_to_array('$packet_csv',',')::uuid[])
    " >"$output"
}

authenticated_health() {
  [[ -n "${VS_SERVICE_TOKEN:-}" ]]
  for _attempt in $(seq 1 30); do
    if [[ "$(systemctl is-active brains.service)" == active ]] \
       && curl --fail --silent --max-time 5 \
          -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
          http://127.0.0.1:8088/healthz \
          | jq -e '.status=="ok"' >/dev/null \
       && curl --fail --silent --max-time 5 \
          -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
          http://127.0.0.1:8088/readyz \
          | jq -e '.ok==true and .postgres==true' >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

restore_runtime() {
  if [[ "$brains_quiesced" -eq 1 ]]; then
    if [[ "$brains_state_before" == active ]]; then
      systemctl start brains.service
    else
      systemctl stop brains.service
    fi
    [[ "$(systemctl is-active brains.service)" == "$brains_state_before" ]]
    brains_quiesced=0
  fi
  if [[ "$timers_quiesced" -eq 1 ]]; then
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
  fi
}

record_exit() {
  rc=$?
  trap - EXIT
  restore_runtime || rc=1
  rm -f "$timer_state" "$table_list" "$protected_before" "$protected_after" \
    "$routes_before" "$routes_after"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$phase" "$rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$rc"
}
trap record_exit EXIT

[[ -z "$(git status --porcelain)" ]]
[[ "$(sha256sum "$manifest" | awk '{print $1}')" == "$manifest_sha" ]]
[[ "$(sha256sum "$worker" | awk '{print $1}')" == "$worker_sha" ]]
[[ "$(sha256sum "$unit_test" | awk '{print $1}')" == "$unit_test_sha" ]]
git merge-base --is-ancestor \
  "$(jq -er '.required_ancestor_commit' "$manifest")" HEAD
[[ "$(jq -er '.owner_user_id' "$manifest")" == "$owner" ]]
[[ "$(jq -er '.items|length' "$manifest")" == 4 ]]
[[ "$(jq -er '.maximum_packets' "$manifest")" == 16 ]]
[[ "$(jq -er '.external_model_calls' "$manifest")" == 0 ]]
[[ -x "$python_bin" && -x "$worker" ]]
[[ "$(stat -c '%a:%U:%G' "$review_root")" == 700:ubuntu:ubuntu ]]
bash -n "$0"

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${VS_SERVICE_TOKEN:-}" ]]
authenticated_health

mapfile -t packet_ids < <(jq -er '.items[].packet_id' "$manifest")
mapfile -t evidence_ids < <(jq -er '.items[].evidence_id' "$manifest")
[[ "${#packet_ids[@]}" -eq 4 && "${#evidence_ids[@]}" -eq 4 ]]
packet_csv=$(IFS=,; printf '%s' "${packet_ids[*]}")
evidence_csv=$(IFS=,; printf '%s' "${evidence_ids[*]}")

[[ "$(scalar "
  SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
")" == 4 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
")" == 0 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id=ANY(string_to_array('$evidence_csv',',')::uuid[])
")" == 0 ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
artifact_dir="$review_root/mixed-review-route-production-$run_tag"
reviews="$artifact_dir/reviews"
status_file="$snapshot_dir/memory_v1_v5_2_mixed_review_route_${run_tag}.status"
install -d -o root -g root -m 0700 "$artifact_dir"
install -d -o ubuntu -g ubuntu -m 0700 "$reviews"

phase=tests
PYTHONPATH="$repo_root" "$python_bin" -m unittest \
  tests.test_memory_v1_v5_2_exact_review_route

phase=quiesce
: >"$timer_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$timer_state" ]]
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
brains_state_before=$(systemctl is-active brains.service)
if [[ "$brains_state_before" == active ]]; then systemctl stop brains.service; fi
brains_quiesced=1
for _attempt in $(seq 1 30); do
  systemctl is-active --quiet brains.service || break
  sleep 1
done
! systemctl is-active --quiet brains.service

phase=backup
partial="$snapshot_dir/.memory_pre_v5_2_mixed_route_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_mixed_route_${run_tag}.dump"
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
docker exec "$container" psql -X -A -F $'\t' -t -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    SELECT table_schema,table_name
    FROM information_schema.tables
    WHERE table_type='BASE TABLE' AND table_schema='memory'
      AND table_name<>'v5_2_local_packet_route_event'
    ORDER BY table_schema,table_name
  " >"$table_list"
capture_tables "$protected_before"
capture_non_target_routes "$routes_before"
routes_before_count=$(scalar \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')
qdrant_before=$(qdrant_signature)

run_worker() {
  local actor=$1 output=$2 apply=${3:-false}
  local -a command=(
    "$python_bin" "$worker"
    --owner-user-id "$actor"
    --review-root "$reviews"
  )
  local packet
  for packet in "${packet_ids[@]}"; do
    command+=(--packet-id "$packet")
  done
  if [[ "$apply" == true ]]; then command+=(--apply); fi
  runuser -u ubuntu -- env \
    POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
    MEMORY_V1_V5_2_EXACT_REVIEW_ROUTE_APPLY=memory_v1_v5_2_exact_review_route_apply_v1 \
    "${command[@]}" >"$output"
  chmod 0600 "$output"
}

dry="$artifact_dir/dry.json"
apply="$artifact_dir/apply.json"
replay="$artifact_dir/replay.json"
isolation="$artifact_dir/isolation.json"

phase=dry_run
run_worker "$owner" "$dry"
jq -e '
  .apply==false and (.plans|length)==4 and
  ([.plans[].route]|all(.=="manual_review_artifact_ready")) and
  .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$dry" >/dev/null

phase=transactional_apply
run_worker "$owner" "$apply" true
jq -e '
  .apply==true and .outcome=="manual_review_artifacts_ready" and
  .write_counts.route_events==4 and
  .write_counts.restricted_review_artifacts==8 and
  .write_counts.stage==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .transactional_apply_proved==true and
  .zero_write_replay_proved==true and .external_model_calls==0
' "$apply" >/dev/null

phase=zero_write_replay
run_worker "$owner" "$replay"
jq -e '
  .apply==false and (.plans|length)==4 and
  ([.plans[].route]|all(.=="no_work")) and
  .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$replay" >/dev/null

phase=account_isolation
run_worker "$other" "$isolation"
jq -e '
  .apply==false and (.plans|length)==4 and
  ([.plans[].route]|all(.=="no_work")) and
  .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$isolation" >/dev/null

phase=verification
capture_tables "$protected_after"
capture_non_target_routes "$routes_after"
cmp -s "$protected_before" "$protected_after"
cmp -s "$routes_before" "$routes_after"
[[ "$(scalar 'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" \
  == "$((routes_before_count+4))" ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
    AND route='manual_review_artifact_ready'
")" == 4 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id=ANY(string_to_array('$evidence_csv',',')::uuid[])
")" == 0 ]]
artifact_count=$(find "$reviews" -maxdepth 1 -type f -name '*.json' | wc -l)
[[ "$artifact_count" -eq 8 ]]
while IFS= read -r path; do
  [[ "$(stat -c '%a:%U:%G' "$path")" == 600:ubuntu:ubuntu ]]
done < <(find "$reviews" -maxdepth 1 -type f -name '*.json' | sort)
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

phase=restore_runtime
restore_runtime
authenticated_health

phase=report
report="$artifact_dir/report.json"
jq -n \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:"memory_v1_v5_2_mixed_review_route_production_report_v1",
    head_commit:$head_commit,
    manifest_sha256:$manifest_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    packet_count:4,
    review_route_events:4,
    restricted_review_artifacts:8,
    transactional_apply_proved:true,
    zero_write_replay_proved:true,
    account_isolation_proved:true,
    non_target_records_unchanged:true,
    qdrant_unchanged:true,
    qdrant_sha256:$qdrant_sha256,
    timers_restored_exactly:true,
    brains_service_restored:true,
    external_model_calls:0,
    stage_writes:0,
    claim_writes:0,
    prompt_influence:0,
    hard_stop:"before_relational_staging_claims_qdrant_retrieval_or_prompt_influence"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_2_mixed_review_route_batch_production: PASS\n'
printf 'report=%s\nreport_sha256=%s\nbackup=%s\nbackup_sha256=%s\n' \
  "$report" "$(awk '{print $1}' "$report.sha256")" "$backup" "$backup_sha"

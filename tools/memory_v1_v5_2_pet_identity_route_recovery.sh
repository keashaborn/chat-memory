#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Resumes the pet-identity route milestone after the two
# exact supersessions were committed and before any review route was written.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for backup and timer control' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_PET_IDENTITY_ROUTE_RECOVERY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_PET_IDENTITY_ROUTE_RECOVERY=authorized is required' >&2
  exit 1
fi

repo=/opt/chat-memory
target=${MEMORY_V1_V5_2_PET_IDENTITY_ROUTE_TARGET_COMMIT:?target commit is required}
[[ "$(runuser -u ubuntu -- git -C "$repo" rev-parse HEAD)" == "$target" ]]
[[ -z "$(runuser -u ubuntu -- git -C "$repo" status --porcelain)" ]]

container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=673d64a3-c4ba-4d1c-89e3-e0c579022fad
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_pet_identity_route.lock
python_bin=/opt/chat-memory/venv/bin/python
manifest=$repo/manifests/memory_v1_v5_2_pet_identity_exact_route_20260729.json
worker=$repo/scripts/memory_v1_v5_2_exact_route_batch.py
route_packets=(
  f7bd7d42-9241-562c-9150-014d5c5f762f
  4f091b92-3db2-5c44-8676-7a2cdc989d01
  afe46ab5-6243-5568-bc71-b23772008c63
)
prior_packets=(
  adc8ecf7-63bd-5dc0-8283-0c2f765893c5
  3b44e557-2910-510c-ac8a-707ef92a398d
)

[[ "$(sha256sum "$manifest" | cut -d' ' -f1)" \
  == c869c60046cf13a2464702a9e29eb959c572eece84f751ef95597de3e6748453 ]]
[[ "$(sha256sum "$worker" | cut -d' ' -f1)" \
  == d8cb2d071a967f2acf9aaa3c94ad706aef30e0c9b6463f5736bbd1c8d1891ced ]]
[[ "$(sha256sum "$repo/scripts/memory_v1_v5_1_review_local_packet.py" | cut -d' ' -f1)" \
  == ea980e41752a0cc3d35e8fe90024b148270747c18d8545a4d3b19a65c16bcfc2 ]]
[[ "$(stat -c '%a:%U:%G' "$review_root")" == 700:ubuntu:ubuntu ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8088/docs)" == 200 ]]

set -a
source "$repo/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]

timer_state=$(mktemp /tmp/memory-pet-route-recovery.XXXXXX.timers)
table_list=$(mktemp /tmp/memory-pet-route-recovery.XXXXXX.tables)
protected_before=$(mktemp /tmp/memory-pet-route-recovery.XXXXXX.before)
protected_after=$(mktemp /tmp/memory-pet-route-recovery.XXXXXX.after)
non_target_before=$(mktemp /tmp/memory-pet-route-recovery.XXXXXX.route-before)
non_target_after=$(mktemp /tmp/memory-pet-route-recovery.XXXXXX.route-after)
dry_output=$(mktemp /tmp/memory-pet-route-recovery.XXXXXX.dry)
apply_output=$(mktemp /tmp/memory-pet-route-recovery.XXXXXX.apply)
chmod 0600 "$timer_state" "$table_list" "$protected_before" "$protected_after" \
  "$non_target_before" "$non_target_after" "$dry_output" "$apply_output"

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
    | sha256sum | cut -d' ' -f1
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
    WHERE owner_user_id<>'$owner'::uuid
       OR packet_id NOT IN (
         '${route_packets[0]}'::uuid,
         '${route_packets[1]}'::uuid,
         '${route_packets[2]}'::uuid
       )
  " >"$1"
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null
    else
      systemctl disable "$unit" >/dev/null 2>&1 || true
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
    "$non_target_before" "$non_target_after" "$dry_output" "$apply_output"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$phase" "$rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$rc"
}
trap record_exit EXIT

[[ "$(scalar "SELECT count(*) FROM memory.v5_local_packet_supersession
  WHERE owner_user_id='$owner'::uuid
    AND prior_packet_id IN (
      '${prior_packets[0]}'::uuid,'${prior_packets[1]}'::uuid
    )
    AND reason_code='pet_identity_semantics_reextracted'")" == 2 ]]
[[ "$(scalar "SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id IN (
      '${route_packets[0]}'::uuid,
      '${route_packets[1]}'::uuid,
      '${route_packets[2]}'::uuid
    )")" == 0 ]]
for packet in "${route_packets[@]}"; do
  packet_hash=$(printf %s "$packet" | sha256sum | cut -d' ' -f1)
  [[ ! -e "$review_root/v5-2-router-$packet_hash-review.json" ]]
  [[ ! -e "$review_root/v5-2-router-$packet_hash-stage.json" ]]
done

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(printf %s "$target" | cut -c1-12)"
status_file="$snapshot_dir/memory_v1_v5_2_pet_identity_route_recovery_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_pet_identity_route_recovery_${run_tag}.json"

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
partial="$snapshot_dir/.memory_pre_v5_2_pet_identity_route_recovery_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_pet_identity_route_recovery_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
docker exec -i "$container" pg_restore -l <"$partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
backup_sha=$(sha256sum "$backup" | cut -d' ' -f1)
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
capture_non_target_routes "$non_target_before"
qdrant_before=$(qdrant_signature)
route_before=$(scalar 'SELECT count(*) FROM memory.v5_2_local_packet_route_event')
legacy_review_before=$(scalar 'SELECT count(*) FROM memory.v5_local_packet_review_artifact')

run_worker() {
  local output=$1 apply=${2:-false}
  local -a command=(
    "$python_bin" "$worker"
    --manifest "$manifest"
    --review-root "$review_root"
    --other-owner-user-id "$other_owner"
  )
  if [[ "$apply" == true ]]; then
    command+=(--apply)
  fi
  runuser -u ubuntu -- env \
    POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo" \
    MEMORY_V1_V5_2_EXACT_ROUTE_BATCH_APPLY=memory_v1_v5_2_exact_route_batch_apply_v1 \
    "${command[@]}" >"$output"
}

phase=route_dry_run
run_worker "$dry_output"
jq -e '
  .apply==false and .packet_count==3 and
  .route_counts.manual_review_routes==3 and
  .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$dry_output" >/dev/null

phase=route_apply
run_worker "$apply_output" true
jq -e '
  .apply==true and .outcome=="exact_routes_applied" and
  .packet_count==3 and .route_counts.manual_review_routes==3 and
  .write_counts.route_events==3 and
  .write_counts.restricted_review_artifacts==6 and
  .transactional_apply_proved==true and
  .zero_write_replay_proved==true and .cross_owner_visible==0 and
  .write_counts.stage==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .external_model_calls==0
' "$apply_output" >/dev/null

phase=postflight
[[ "$(scalar "SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id IN (
      '${route_packets[0]}'::uuid,
      '${route_packets[1]}'::uuid,
      '${route_packets[2]}'::uuid
    )
    AND route='manual_review_artifact_ready'")" == 3 ]]
[[ "$(scalar 'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" \
  == "$((route_before + 3))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_review_artifact')" \
  == "$legacy_review_before" ]]
for prior in "${prior_packets[@]}"; do
  [[ "$(psql "$POSTGRES_DSN" -X -Atq -v ON_ERROR_STOP=1 <<SQL | tail -1
BEGIN READ ONLY;
SELECT set_config('app.user_id','$owner',true);
SELECT count(*) FROM memory.plan_owner_v5_2_exact_packet_route_v1('$prior'::uuid);
ROLLBACK;
SQL
  )" == 0 ]]
done
capture_tables "$protected_after"
capture_non_target_routes "$non_target_after"
cmp -s "$protected_before" "$protected_after"
cmp -s "$non_target_before" "$non_target_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

artifact_sha=()
for packet in "${route_packets[@]}"; do
  packet_hash=$(printf %s "$packet" | sha256sum | cut -d' ' -f1)
  for suffix in review stage; do
    path="$review_root/v5-2-router-$packet_hash-$suffix.json"
    [[ "$(stat -c '%a:%U:%G' "$path")" == 600:ubuntu:ubuntu ]]
    artifact_sha+=("$(sha256sum "$path" | cut -d' ' -f1)")
  done
done

phase=restore_timers
restore_timers
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  [[ "$(systemctl is-active "$unit")" == "$active" ]]
done <"$timer_state"
[[ "$(systemctl is-active brains.service)" == active ]]

phase=report
jq -n \
  --arg contract_version memory_v1_v5_2_pet_identity_route_recovery_v1 \
  --arg commit "$target" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  --argjson timer_count "$timer_count" \
  --argjson artifact_sha256 "$(printf '%s\n' "${artifact_sha[@]}" \
    | jq -R . | jq -s 'sort')" \
  '{
    contract_version:$contract_version,
    outcome:"pass",
    commit:$commit,
    backup:{path:$backup,sha256:$backup_sha256},
    recovered_from:"supersessions_applied_routes_absent",
    bounded_writes:{review_route_events:3,restricted_review_files:6},
    zero_write_replay_proved:true,
    account_isolation_proved:true,
    non_target_rows_unchanged:true,
    legacy_review_artifacts_unchanged:true,
    artifact_sha256:$artifact_sha256,
    qdrant:{unchanged:true,sha256:$qdrant_sha256},
    model_calls:0,
    relational_stage_batches:0,
    claims:0,
    retrieval_activation:0,
    prompt_influence:0,
    timers:{count:$timer_count,restored:true},
    hard_stop:"before_relational_staging_claims_qdrant_retrieval_or_prompt_influence"
  }' >"$report"
chmod 0600 "$report"
report_sha=$(sha256sum "$report" | cut -d' ' -f1)
printf '%s  %s\n' "$report_sha" "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf '%s\n' \
  'memory_v1_v5_2_pet_identity_route_recovery: PASS' \
  "commit=$target" \
  "report=$report" \
  "report_sha256=$report_sha" \
  "backup=$backup" \
  "backup_sha256=$backup_sha" \
  'routes=3 artifacts=6 model_calls=0' \
  'claims=0 staging=0 qdrant=0 retrieval=0 prompt_influence=0'

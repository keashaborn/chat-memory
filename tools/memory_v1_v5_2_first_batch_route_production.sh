#!/usr/bin/env bash
set -euo pipefail

# seebx only. Installs the additive V5.2 zero-atom route contract and
# reconciles exactly the manifest-locked first 66 packets.

[[ "$EUID" -eq 0 ]]
[[ "${MEMORY_V1_V5_2_FIRST_BATCH_ROUTE_APPLY:-}" == authorized ]]

repo_root=/opt/chat-memory
cd "$repo_root"

container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
manifest=manifests/memory_v1_v5_2_first_batch_route_20260728.json
manifest_sha=508080c08f199326577a4992a9b4f654b1d0f6caead5b66bdc77eaf456404a9d
migration=ops/sql/20260728_memory_v1_v5_2_zero_atom_deferral_route.sql
migration_sha=fac61e59b610a337c6a9662c2304d3a89316901f2d64d124554287e38d399c7c
rollback=ops/sql/20260728_memory_v1_v5_2_zero_atom_deferral_route_rollback.sql
rollback_sha=c22ea48034bba15913e85b56a25f0c5224eff27fb381bda1803455620bfb0564
sql_test=tests/memory_v1_v5_2_zero_atom_deferral_route.sql
sql_test_sha=ef3d24b2437cf04eb54f41ce3f4af2403a0d9e3614b72cc52760907213fef015
terminal_worker=scripts/memory_v1_v5_2_exact_terminal_batch.py
terminal_worker_sha=d794af40576670b2112395d35214cb5ea065d98193826e1052f1d80b27679a6c
terminal_test=tests/test_memory_v1_v5_2_exact_terminal_batch.py
terminal_test_sha=06fab06ced363babca09148a76b3fc515076de70a400495818588adbb4a1c5b2
review_worker=scripts/memory_v1_v5_2_exact_review_route.py
review_worker_sha=bc6d0f915b2e8bc9c42489abc0bbf7f9c8ae593d575b18291d004c58fee5e6ab
review_test=tests/test_memory_v1_v5_2_exact_review_route.py
review_test_sha=1c89259f58c2eea89c86e5263cf58f7c51104f555fe8f9f6082279ac444afd90
review_builder=scripts/memory_v1_v5_1_review_local_packet.py
review_builder_sha=6c98361ddc6f375ebde215631b2a61f9b0292ec5f89137da723fe794642c2c29
review_builder_test=tests/test_memory_v1_v5_1_review_local_packet.py
review_builder_test_sha=8acc983257865ad7049368dfbc9d234c70e1aa262d124969afb6695e361855cb
clone_wrapper=tools/memory_v1_v5_2_first_batch_route_clone.sh
python_bin=/opt/chat-memory/venv/bin/python
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_first_batch_route.lock
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
artifact_dir="$review_root/first-batch-route-production-$run_tag"
reviews="$artifact_dir/reviews"
backup="$snapshot_dir/memory-pre-first-batch-route-$run_tag.dump"
report="$artifact_dir/report.json"

timer_state=$(mktemp /tmp/memory-first-batch-timers.XXXXXX)
timer_restored=$(mktemp /tmp/memory-first-batch-restored.XXXXXX)
table_list=$(mktemp /tmp/memory-first-batch-tables.XXXXXX)
before=$(mktemp /tmp/memory-first-batch-before.XXXXXX)
after=$(mktemp /tmp/memory-first-batch-after.XXXXXX)
terminal_dry=$(mktemp /tmp/memory-first-batch-terminal-dry.XXXXXX)
terminal_apply=$(mktemp /tmp/memory-first-batch-terminal-apply.XXXXXX)
terminal_other=$(mktemp /tmp/memory-first-batch-terminal-other.XXXXXX)
review_dry=$(mktemp /tmp/memory-first-batch-review-dry.XXXXXX)
review_apply=$(mktemp /tmp/memory-first-batch-review-apply.XXXXXX)
review_replay=$(mktemp /tmp/memory-first-batch-review-replay.XXXXXX)
review_other=$(mktemp /tmp/memory-first-batch-review-other.XXXXXX)
chmod 0600 "$timer_state" "$timer_restored" "$table_list" "$before" "$after" \
  "$terminal_dry" "$terminal_apply" "$terminal_other" "$review_dry" \
  "$review_apply" "$review_replay" "$review_other"

timers_quiesced=0
brains_stopped=0
schema_installed=0
data_committed=0
phase=initialization

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

run_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$database" "$@"
}

query_rows() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_timer_state() {
  local output=$1 unit
  : >"$output"
  while IFS= read -r unit; do
    printf '%s\t%s\t%s\n' "$unit" \
      "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
      >>"$output"
  done < <(
    systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
      | awk '{print $1}' | sort -u
  )
}

restore_runtime() {
  local unit enabled active
  if [[ "$brains_stopped" -eq 1 ]]; then
    systemctl start brains.service
    brains_stopped=0
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
    done <"$timer_state"
    capture_timer_state "$timer_restored"
    cmp -s "$timer_state" "$timer_restored"
    timers_quiesced=0
  fi
}

cleanup() {
  local status=$?
  if [[ "$schema_installed" -eq 1 && "$data_committed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || status=1
  fi
  restore_runtime || status=1
  if [[ "$status" -ne 0 && -d "$artifact_dir" ]]; then
    for diagnostic in "$terminal_dry" "$terminal_apply" "$terminal_other" \
      "$review_dry" "$review_apply" "$review_replay" "$review_other"; do
      if [[ -s "$diagnostic" ]]; then
        install -o ubuntu -g ubuntu -m 0600 "$diagnostic" \
          "$artifact_dir/diagnostic-$(basename "$diagnostic").json"
      fi
    done
    {
      printf 'phase=%s\nexit_code=%s\n' "$phase" "$status"
    } >"$artifact_dir/failure-status.txt"
    chown ubuntu:ubuntu "$artifact_dir/failure-status.txt"
    chmod 0600 "$artifact_dir/failure-status.txt"
  fi
  rm -f "$timer_state" "$timer_restored" "$table_list" "$before" "$after" \
    "$terminal_dry" "$terminal_apply" "$terminal_other" "$review_dry" \
    "$review_apply" "$review_replay" "$review_other"
  exit "$status"
}
trap cleanup EXIT

capture_protected_tables() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM \"$schema\".\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$table_list"
}

target_route_count() {
  scalar "
    SELECT count(*)
    FROM memory.v5_2_local_packet_route_event
    WHERE owner_user_id='$owner'::uuid
      AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
  "
}

exec 9>"$lock_file"
flock -n 9 || {
  echo 'first-batch route lock is busy' >&2
  exit 1
}

[[ -z "$(git status --porcelain)" ]]
[[ -x "$python_bin" && -x "$terminal_worker" && -x "$review_worker" ]]
while IFS=$'\t' read -r file expected; do
  [[ "$(sha256sum "$file" | awk '{print $1}')" == "$expected" ]]
done <<EOF
$manifest	$manifest_sha
$migration	$migration_sha
$rollback	$rollback_sha
$sql_test	$sql_test_sha
$terminal_worker	$terminal_worker_sha
$terminal_test	$terminal_test_sha
$review_worker	$review_worker_sha
$review_test	$review_test_sha
$review_builder	$review_builder_sha
$review_builder_test	$review_builder_test_sha
EOF
bash -n "$clone_wrapper"
git merge-base --is-ancestor \
  "$(jq -er '.required_ancestor_commit' "$manifest")" HEAD

mapfile -t terminal_items < <(
  jq -er '.terminal_items[] |
    "\(.packet_id):\(.packet_storage_sha256)"' "$manifest"
)
mapfile -t disposition_items < <(
  jq -er '.disposition_items[] |
    "\(.packet_id):\(.packet_storage_sha256):\(.reason_code)"' "$manifest"
)
mapfile -t review_ids < <(jq -er '.review_items[].packet_id' "$manifest")
mapfile -t all_packet_ids < <(
  jq -er '[.terminal_items[],.disposition_items[],.review_items[]] |
    .[].packet_id' "$manifest"
)
mapfile -t all_evidence_ids < <(
  jq -er '[.terminal_items[],.disposition_items[],.review_items[]] |
    .[].evidence_id' "$manifest"
)
[[ "${#terminal_items[@]}" -eq 48 ]]
[[ "${#disposition_items[@]}" -eq 7 ]]
[[ "${#review_ids[@]}" -eq 11 ]]
[[ "${#all_packet_ids[@]}" -eq 66 ]]
packet_csv=$(IFS=,; printf '%s' "${all_packet_ids[*]}")
evidence_csv=$(IFS=,; printf '%s' "${all_evidence_ids[*]}")

install -d -o ubuntu -g ubuntu -m 0700 \
  "$snapshot_dir" "$artifact_dir" "$reviews"
touch "$backup"
chown ubuntu:ubuntu "$backup"
chmod 0600 "$backup"

capture_timer_state "$timer_state"
phase=quiescing_runtime
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$timer_state"
timers_quiesced=1
systemctl stop brains.service
brains_stopped=1

docker exec "$container" pg_isready -U sage -d "$database" >/dev/null

phase=creating_backup
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner >"$backup"
[[ -s "$backup" ]]
backup_sha=$(sha256sum "$backup" | awk '{print $1}')

docker exec "$container" psql -X -A -F $'\t' -t -U sage -d "$database" \
  -c "SELECT table_schema,table_name
      FROM information_schema.tables
      WHERE table_schema='memory' AND table_type='BASE TABLE'
        AND table_name<>'v5_2_local_packet_route_event'
      ORDER BY 1,2" >"$table_list"
phase=capturing_protected_baseline
capture_protected_tables "$before"
qdrant_before=$(qdrant_signature)
non_target_route_before=$(scalar "
  SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
    coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
    'UTF8'),'sha256'),'hex')
  FROM (
    SELECT to_jsonb(value)::text AS row_json
    FROM memory.v5_2_local_packet_route_event AS value
    WHERE NOT (
      owner_user_id='$owner'::uuid
      AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
    )
  ) AS rows
")
[[ "$(target_route_count)" == 0 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id=ANY(string_to_array('$evidence_csv',',')::uuid[])
")" == 0 ]]

phase=installing_schema
run_sql <"$migration" >/dev/null
schema_installed=1
run_sql <"$sql_test" >/dev/null

phase=running_preflight
set -a
source /opt/chat-memory/.env
set +a
terminal_args=()
for item in "${terminal_items[@]}"; do
  terminal_args+=(--terminal-item "$item")
done
for item in "${disposition_items[@]}"; do
  terminal_args+=(--disposition-item "$item")
done
review_args=()
review_csv=$(IFS=,; printf '%s' "${review_ids[*]}")
mapfile -t superseded_review_ids < <(query_rows "
  SELECT packet.packet_id
  FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id='$owner'::uuid
    AND packet.packet_id=ANY(string_to_array('$review_csv',',')::uuid[])
    AND EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_event AS lineage
      JOIN memory.evidence_extraction_job AS successor
        ON successor.owner_user_id=lineage.owner_user_id
       AND successor.job_id=lineage.job_id
      WHERE lineage.owner_user_id=packet.owner_user_id
        AND lineage.event_type='queued'
        AND lineage.details->>'prior_packet_id'=packet.packet_id::text
        AND successor.evidence_id=packet.evidence_id
        AND successor.evidence_content_sha256=packet.evidence_content_sha256
        AND successor.route='relational_extraction'
    )
  ORDER BY packet.packet_id
")
mapfile -t pending_review_ids < <(query_rows "
  SELECT packet.packet_id
  FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id='$owner'::uuid
    AND packet.packet_id=ANY(string_to_array('$review_csv',',')::uuid[])
    AND NOT EXISTS (
      SELECT 1
      FROM memory.evidence_extraction_event AS lineage
      JOIN memory.evidence_extraction_job AS successor
        ON successor.owner_user_id=lineage.owner_user_id
       AND successor.job_id=lineage.job_id
      WHERE lineage.owner_user_id=packet.owner_user_id
        AND lineage.event_type='queued'
        AND lineage.details->>'prior_packet_id'=packet.packet_id::text
        AND successor.evidence_id=packet.evidence_id
        AND successor.evidence_content_sha256=packet.evidence_content_sha256
        AND successor.route='relational_extraction'
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_2_local_packet_route_event AS routed
      WHERE routed.owner_user_id=packet.owner_user_id
        AND routed.packet_id=packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.v5_local_packet_disposition AS disposed
      WHERE disposed.owner_user_id=packet.owner_user_id
        AND disposed.packet_id=packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.relational_stage_batch AS staged
      WHERE staged.owner_user_id=packet.owner_user_id
        AND staged.evidence_id=packet.evidence_id
    )
  ORDER BY packet.packet_id
")
[[ "${#pending_review_ids[@]}" -ge 1 ]]
[[ "$(( ${#pending_review_ids[@]} + ${#superseded_review_ids[@]} ))" -eq 11 ]]
for packet_id in "${pending_review_ids[@]}"; do
  review_args+=(--packet-id "$packet_id")
done
pending_review_count=${#pending_review_ids[@]}
superseded_review_count=${#superseded_review_ids[@]}

run_terminal() {
  local actor=$1 output=$2 apply=${3:-false}
  local -a command=(
    "$python_bin" "$terminal_worker"
    --owner-user-id "$actor"
    "${terminal_args[@]}"
  )
  [[ "$apply" == false ]] || command+=(--apply)
  runuser -u ubuntu -- env \
    POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
    MEMORY_V1_V5_2_EXACT_TERMINAL_BATCH_APPLY=memory_v1_v5_2_exact_terminal_batch_apply_v1 \
    "${command[@]}" >"$output"
}

run_review() {
  local actor=$1 output=$2 apply=${3:-false}
  local -a command=(
    "$python_bin" "$review_worker"
    --owner-user-id "$actor"
    --review-root "$reviews"
    "${review_args[@]}"
  )
  [[ "$apply" == false ]] || command+=(--apply)
  runuser -u ubuntu -- env \
    POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
    MEMORY_V1_V5_2_EXACT_REVIEW_ROUTE_APPLY=memory_v1_v5_2_exact_review_route_apply_v1 \
    "${command[@]}" >"$output"
}

run_terminal "$owner" "$terminal_dry"
jq -e '.apply==false and .packet_count==55
  and .terminal_route_count==48 and .disposition_count==7
  and .database_writes==0 and .external_model_calls==0
  and .stage_writes==0 and .claim_writes==0
  and .qdrant_writes==0 and .prompt_influence==0' \
  "$terminal_dry" >/dev/null
if run_terminal "$other" "$terminal_other"; then
  echo 'cross-owner terminal routing unexpectedly succeeded' >&2
  exit 1
fi
run_review "$owner" "$review_dry"
jq -e --argjson expected "$pending_review_count" '.apply==false
  and (.plans|length)==$expected
  and ([.plans[].route]|all(.=="manual_review_artifact_ready"))
  and .database_writes==0 and .filesystem_writes==0' \
  "$review_dry" >/dev/null
run_review "$other" "$review_other"
jq -e --argjson expected "$pending_review_count" '.apply==false
  and (.plans|length)==$expected
  and ([.plans[].route]|all(.=="no_work"))
  and .database_writes==0 and .filesystem_writes==0' \
  "$review_other" >/dev/null

phase=applying_exact_routes
run_terminal "$owner" "$terminal_apply" true
jq -e '.apply==true and .database_writes==55
  and .transactional_apply_proved==true
  and .zero_write_replay_proved==true
  and .stage_writes==0 and .claim_writes==0
  and .qdrant_writes==0 and .prompt_influence==0' \
  "$terminal_apply" >/dev/null
run_review "$owner" "$review_apply" true
jq -e --argjson expected "$pending_review_count" '.apply==true
  and .outcome=="manual_review_artifacts_ready"
  and .write_counts.route_events==$expected
  and .write_counts.restricted_review_artifacts==($expected*2)
  and .transactional_apply_proved==true
  and .zero_write_replay_proved==true
  and .write_counts.stage==0 and .write_counts.claims==0
  and .write_counts.qdrant==0 and .write_counts.prompt_influence==0' \
  "$review_apply" >/dev/null
data_committed=1

phase=verifying_replay
run_review "$owner" "$review_replay"
jq -e --argjson expected "$pending_review_count" '.apply==false
  and (.plans|length)==$expected
  and ([.plans[].route]|all(.=="no_work"))
  and .database_writes==0 and .filesystem_writes==0' \
  "$review_replay" >/dev/null

[[ "$(target_route_count)" == "$((55+pending_review_count))" ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
    AND reason_code='deferral_only_no_stage_v5_2'
")" == 50 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
    AND reason_code='deferral_only_review_unresolved_v5_2'
")" == 5 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
    AND reason_code='reviewable_relational_packet_v5_2'
")" == "$pending_review_count" ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid
    AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
")" == 0 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id=ANY(string_to_array('$evidence_csv',',')::uuid[])
")" == 0 ]]
[[ "$(find "$reviews" -maxdepth 1 -type f -name '*.json' | wc -l)" \
      == "$((pending_review_count*2))" ]]

phase=verifying_protected_state
capture_protected_tables "$after"
cmp -s "$before" "$after"
non_target_route_after=$(scalar "
  SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
    coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
    'UTF8'),'sha256'),'hex')
  FROM (
    SELECT to_jsonb(value)::text AS row_json
    FROM memory.v5_2_local_packet_route_event AS value
    WHERE NOT (
      owner_user_id='$owner'::uuid
      AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
    )
  ) AS rows
")
[[ "$non_target_route_before" == "$non_target_route_after" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

phase=restoring_runtime
restore_runtime
[[ "$(systemctl is-active brains.service)" == active ]]
curl --fail --silent --show-error --max-time 30 \
  http://127.0.0.1:8000/health >/dev/null

phase=writing_report
jq -n \
  --arg production_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg migration_sha256 "$migration_sha" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  --argjson pending_review_count "$pending_review_count" \
  --argjson superseded_review_count "$superseded_review_count" \
  '{
    contract_version:"memory_v1_v5_2_first_batch_route_production_report_v1",
    production_commit:$production_commit,
    backup:$backup,
    backup_sha256:$backup_sha256,
    migration_sha256:$migration_sha256,
    manifest_sha256:$manifest_sha256,
    manifest_packet_count:66,
    terminal_no_stage:50,
    terminal_review_unresolved:5,
    manual_review_artifact_ready:$pending_review_count,
    superseded_review_packets:$superseded_review_count,
    restricted_review_artifacts:($pending_review_count*2),
    database_route_writes:(55+$pending_review_count),
    stage_writes:0,
    claim_writes:0,
    qdrant_writes:0,
    external_model_calls:0,
    prompt_influence:0,
    transactional_apply_proved:true,
    zero_write_replay_proved:true,
    account_isolation_proved:true,
    protected_tables_unchanged:true,
    non_target_routes_unchanged:true,
    qdrant_unchanged:true,
    qdrant_sha256:$qdrant_sha256,
    timers_restored:true,
    brains_healthy:true,
    hard_stop:"before_review_decisions_staging_claims_qdrant_or_prompt_influence"
  }' >"$report"
chown ubuntu:ubuntu "$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chown ubuntu:ubuntu "$report.sha256"
chmod 0600 "$report.sha256"

printf 'memory_v1_v5_2_first_batch_route_production: PASS\n'
printf 'report=%s\nreport_sha256=%s\nbackup=%s\nbackup_sha256=%s\n' \
  "$report" "$(awk '{print $1}' "$report.sha256")" \
  "$backup" "$backup_sha"

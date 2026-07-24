#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the restricted review re-extraction function
# and appends exactly three deterministic pending jobs. No model, packet,
# claim, Qdrant, retrieval, or prompt writes occur in this phase.

if [[ "${MEMORY_V1_V5_2_COMPILER_V7_REEXTRACT_RUN:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_COMPILER_V7_REEXTRACT_RUN=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-$repo_root/.env}"
set +a

container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_compiler_v7_reextract.lock
required_ancestor=010fb6575c5979bdda9ae8d73f76b0dca7aa891d
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
selector=20260724_v5_2_compiler_v7_review_v1
manifest_sha=f1d2e4ec4cf51740ea3e53aa639aa5c2a304f9b5a9a56f0ad84c1c9809955056
compiler_sha=a4387aad59445f65fdfc8413f48567b8f560a352cb6fcf8de415769b8752bfbc
migration=ops/sql/20260724_memory_v1_v5_2_compiler_v7_review_reextract.sql
rollback=ops/sql/20260724_memory_v1_v5_2_compiler_v7_review_reextract_rollback.sql
test_sql=tests/memory_v1_v5_2_compiler_v7_review_reextract.sql
manifest=manifests/memory_v1_v5_2_compiler_v7_review_reextract_20260724.json
clone_test=tools/memory_v1_v5_2_compiler_v7_review_reextract_clone.sh

declare -A expected_sha256=(
  ["$migration"]="f77c65189075668712d661ecebd01fd2d84fba0d2cb0b11942927542b3573efb"
  ["$rollback"]="4e36c3c206a0051ac601b1bf9ce082f0046cbf7e9be34719e53b29504788e57e"
  ["$test_sql"]="1d2f1aff7f4d053f840722083e394b612fbe1b23d45c4f217b7ac18203344437"
  ["$manifest"]="b7d28be05acbaec5294d4c503ce750c2873b91f2a0dc4aa84e137bb179ca2acb"
  ["$clone_test"]="6a87099656d6ff8ce2cc2bbe3b182fe1a706ba8d010ce637eb96538b68347f87"
)

timer_state=$(mktemp /tmp/memory-v5-2-compiler-v7-reextract-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v5-2-compiler-v7-reextract-tables.XXXXXX)
before=$(mktemp /tmp/memory-v5-2-compiler-v7-reextract-before.XXXXXX)
after=$(mktemp /tmp/memory-v5-2-compiler-v7-reextract-after.XXXXXX)
clone_output=$(mktemp /tmp/memory-v5-2-compiler-v7-reextract-clone.XXXXXX)
apply_output=$(mktemp /tmp/memory-v5-2-compiler-v7-reextract-apply.XXXXXX)
replay_output=$(mktemp /tmp/memory-v5-2-compiler-v7-reextract-replay.XXXXXX)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" \
  "$clone_output" "$apply_output" "$replay_output"
timers_quiesced=0
migration_installed=0
writes_committed=0
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

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      sudo -n systemctl enable "$unit" >/dev/null
    else
      sudo -n systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  rc=$?
  if [[ "$migration_installed" -eq 1 && "$writes_committed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || rc=1
  fi
  restore_timers || rc=1
  rm -f "$timer_state" "$table_list" "$before" "$after" \
    "$clone_output" "$apply_output" "$replay_output"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\nwrites_committed=%s\ncompleted_at=%s\n' \
      "$phase" "$rc" "$writes_committed" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$rc"
}
trap record_exit EXIT

capture_state() {
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

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for artifact in "${!expected_sha256[@]}"; do
  [[ -f "$artifact" ]]
  [[ "$(sha256sum "$artifact" | awk '{print $1}')" == \
    "${expected_sha256[$artifact]}" ]]
done
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(jq -cS . "$manifest" | tr -d '\n' | sha256sum | awk '{print $1}')" == \
  "$manifest_sha" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
failed_units_before=$(
  systemctl list-units --state=failed --no-legend --no-pager --plain \
    | awk '{print $1}' | sort | sha256sum | awk '{print $1}'
)

phase=production_clone
bash "$clone_test" >"$clone_output"
[[ "$(tr -d '\r\n' <"$clone_output")" == \
  memory_v1_v5_2_compiler_v7_review_reextract_clone:\ PASS ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_compiler_v7_reextract_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_compiler_v7_reextract_${run_tag}.json"

phase=inventory_timers
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
timer_count=$(wc -l <"$timer_state")
(( timer_count >= 16 && timer_count <= 32 ))

phase=quiesce_timers
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
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
partial="$snapshot_dir/.memory_pre_v5_2_compiler_v7_reextract_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_compiler_v7_reextract_${run_tag}.dump"
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
  -c "SELECT table_schema,table_name FROM information_schema.tables
      WHERE table_type='BASE TABLE' AND table_schema IN ('memory','public')
        AND NOT (table_schema='memory' AND table_name IN
          ('evidence_extraction_job','evidence_intake_terminal',
           'evidence_extraction_event'))
      ORDER BY table_schema,table_name" >"$table_list"
capture_state "$before"
jobs_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')
terminals_before=$(scalar 'SELECT count(*) FROM memory.evidence_intake_terminal')
events_before=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')
qdrant_before=$(qdrant_signature)
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" == 0 ]]

phase=install_restricted_function
run_sql <"$migration" >/dev/null
migration_installed=1

phase=rollback_only_security
run_sql -v target_owner="$owner" -v other_owner="$other" \
  -v evidence_id=33126656-fc5a-5fc1-a035-246b14576ee5 \
  -v content_sha256=be7f19c9986565d34b72d5928301443e61122a5208e7ca8135e348ec2e6711c4 \
  -v operation_id=9dc7a342-524c-549f-92c4-f03daf845a6f \
  -v job_id=db94d835-9598-5805-9101-f320e3c87f0f \
  -v terminal_id=dea158c8-82e8-5b4c-83fe-7bbd13a633b5 \
  -v manifest_sha256="$manifest_sha" \
  -v compiler_sha256="$compiler_sha" \
  <"$test_sql" >/dev/null

phase=transactional_enqueue
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 -At -F $'\t' \
  -v owner="$owner" \
  -v manifest="$manifest_sha" \
  -v compiler="$compiler_sha" >"$apply_output" <<'SQL'
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT * FROM memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
  '9dc7a342-524c-549f-92c4-f03daf845a6f',
  'db94d835-9598-5805-9101-f320e3c87f0f',
  'dea158c8-82e8-5b4c-83fe-7bbd13a633b5',
  '33126656-fc5a-5fc1-a035-246b14576ee5',
  'be7f19c9986565d34b72d5928301443e61122a5208e7ca8135e348ec2e6711c4',
  :'manifest', :'compiler'
);
SELECT * FROM memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
  '6479fbda-6b53-55fe-8b72-829eed81efda',
  '97211776-4362-5ede-9b22-777c5617e205',
  '5c6d918e-15d1-521c-88c3-ebc2981b9967',
  'fea59e7e-30f5-4139-b634-97b291c88e14',
  '895146b94431f7e0ec3292e757e30fc4c782af222bd39598e610654adf08ccec',
  :'manifest', :'compiler'
);
SELECT * FROM memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
  '89fa062d-5fdc-5aa1-b1b8-7d8c833912cf',
  'aecb3efc-48f6-5587-99cc-0aaa60af9662',
  '08e35644-957b-5fc8-a259-3dc0cb169ea8',
  'dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5',
  'fb65fe592059bace88a4daea571ac1ba7df2b5aeb5136233dd01071cb6085182',
  :'manifest', :'compiler'
);
COMMIT;
SQL
writes_committed=1
[[ "$(grep -c $'\tpending\tapplied$' "$apply_output")" -eq 3 ]]

phase=zero_write_replay
jobs_after_apply=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')
terminals_after_apply=$(scalar 'SELECT count(*) FROM memory.evidence_intake_terminal')
events_after_apply=$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 -At -F $'\t' \
  -v owner="$owner" \
  -v manifest="$manifest_sha" \
  -v compiler="$compiler_sha" >"$replay_output" <<'SQL'
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT * FROM memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
  '9dc7a342-524c-549f-92c4-f03daf845a6f',
  'db94d835-9598-5805-9101-f320e3c87f0f',
  'dea158c8-82e8-5b4c-83fe-7bbd13a633b5',
  '33126656-fc5a-5fc1-a035-246b14576ee5',
  'be7f19c9986565d34b72d5928301443e61122a5208e7ca8135e348ec2e6711c4',
  :'manifest', :'compiler'
);
SELECT * FROM memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
  '6479fbda-6b53-55fe-8b72-829eed81efda',
  '97211776-4362-5ede-9b22-777c5617e205',
  '5c6d918e-15d1-521c-88c3-ebc2981b9967',
  'fea59e7e-30f5-4139-b634-97b291c88e14',
  '895146b94431f7e0ec3292e757e30fc4c782af222bd39598e610654adf08ccec',
  :'manifest', :'compiler'
);
SELECT * FROM memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
  '89fa062d-5fdc-5aa1-b1b8-7d8c833912cf',
  'aecb3efc-48f6-5587-99cc-0aaa60af9662',
  '08e35644-957b-5fc8-a259-3dc0cb169ea8',
  'dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5',
  'fb65fe592059bace88a4daea571ac1ba7df2b5aeb5136233dd01071cb6085182',
  :'manifest', :'compiler'
);
COMMIT;
SQL
[[ "$(grep -c $'\tpending\treplayed$' "$replay_output")" -eq 3 ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_job')" == \
  "$jobs_after_apply" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_intake_terminal')" == \
  "$terminals_after_apply" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.evidence_extraction_event')" == \
  "$events_after_apply" ]]

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
[[ "$jobs_after_apply" -eq "$((jobs_before+3))" ]]
[[ "$terminals_after_apply" -eq "$((terminals_before+3))" ]]
[[ "$events_after_apply" -eq "$((events_before+3))" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND selector_version='$selector'
    AND status='pending' AND attempts=0")" == 3 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$other'::uuid AND selector_version='$selector'")" == 0 ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(
  systemctl list-units --state=failed --no-legend --no-pager --plain \
    | awk '{print $1}' | sort | sha256sum | awk '{print $1}'
)" == "$failed_units_before" ]]

phase=restore_timers
restore_timers
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  [[ "$(systemctl is-active "$unit")" == "$active" ]]
done <"$timer_state"

phase=report
jq -n \
  --arg contract_version memory_v1_v5_2_compiler_v7_review_reextract_apply_v1 \
  --arg commit "$(git rev-parse HEAD)" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_before" \
  --argjson timer_count "$timer_count" \
  '{
    contract_version:$contract_version,
    outcome:"pass",
    commit:$commit,
    manifest_sha256:$manifest_sha256,
    bounded_writes:{jobs:3,terminals:3,events:3},
    zero_write_replay_proved:true,
    account_isolation_proved:true,
    non_target_rows_unchanged:true,
    qdrant_unchanged:true,
    model_calls:{local:0,external:0},
    claims:0,
    retrieval:0,
    prompt_influence:0,
    timer_count:$timer_count,
    timers_restored:true,
    backup:{path:$backup,sha256:$backup_sha256},
    qdrant_sha256:$qdrant_sha256
  }' >"$report"
chmod 0600 "$report"
report_sha=$(sha256sum "$report" | awk '{print $1}')
printf '%s  %s\n' "$report_sha" "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_2_compiler_v7_review_reextract: PASS\n'
printf 'report=%s\nreport_sha256=%s\nbackup=%s\nbackup_sha256=%s\n' \
  "$report" "$report_sha" "$backup" "$backup_sha"

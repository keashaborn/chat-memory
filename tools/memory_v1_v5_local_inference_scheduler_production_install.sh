#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the owner-allowlisted local V5 extraction
# scheduler, runs exactly one private-infrastructure canary, proves bounded
# state changes, and enables the two-hour timer only after all checks pass.

if [[ "${MEMORY_V1_V5_LOCAL_SCHEDULER_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_LOCAL_SCHEDULER_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
service=memory-v1-v5-local-inference-scheduler.service
timer=memory-v1-v5-local-inference-scheduler.timer
service_source=ops/systemd/$service
timer_source=ops/systemd/$timer
scheduler=scripts/memory_v1_v5_local_inference_scheduler.py
canary=scripts/memory_v1_v5_local_inference_canary.py
expected_service_sha=7e2f3ef9749d2704a036ef4ab3f75dd449d5c74f78c8180b60ca4646094512ad
expected_timer_sha=0614c7c7b321e1e314e0db718bafbbec05bbc6468ce38f30e83b5a57b35009f8
expected_scheduler_sha=b4cfe967065c1fbeb1cf9f2ccd12dae2f0c67640e3bcc29585b24e047cf01a89
expected_canary_sha=f914fad97468f46674f018f44e2b3f08205cd16823a568b17eb93c30be1a4db7
lock_file=/home/ubuntu/brains/.memory_v1_v5_local_scheduler_install.lock

phase=initialization
run_tag=
status_file=
units_quiesced=0
unit_installed=0
installation_committed=0
unit_state=$(mktemp /tmp/memory-v1-v5-local-scheduler-units.XXXXXX)
protected_tables=$(mktemp /tmp/memory-v1-v5-local-scheduler-tables.XXXXXX)
protected_before=$(mktemp /tmp/memory-v1-v5-local-scheduler-protected-before.XXXXXX)
protected_after=$(mktemp /tmp/memory-v1-v5-local-scheduler-protected-after.XXXXXX)
other_before=$(mktemp /tmp/memory-v1-v5-local-scheduler-other-before.XXXXXX)
other_after=$(mktemp /tmp/memory-v1-v5-local-scheduler-other-after.XXXXXX)
allowed_before=$(mktemp -d /tmp/memory-v1-v5-local-scheduler-allowed-before.XXXXXX)
allowed_after=$(mktemp -d /tmp/memory-v1-v5-local-scheduler-allowed-after.XXXXXX)

psql_scalar() {
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

restore_units() {
  [[ "$units_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    if [[ "$enabled" == enabled ]]; then
      sudo -n systemctl enable "$unit" >/dev/null
    elif [[ "$enabled" == disabled ]]; then
      sudo -n systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
}

record_exit() {
  exit_code=$?
  restore_units || exit_code=1
  if [[ "$unit_installed" -eq 1 && "$installation_committed" -eq 0 ]]; then
    sudo -n systemctl disable --now "$timer" >/dev/null 2>&1 || true
    sudo -n rm -f "/etc/systemd/system/$service" "/etc/systemd/system/$timer"
    sudo -n systemctl daemon-reload
  fi
  rm -f "$unit_state" "$protected_tables" "$protected_before" \
    "$protected_after" "$other_before" "$other_after"
  rm -rf "$allowed_before" "$allowed_after"
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

capture_protected() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$protected_tables"
  chmod 0600 "$output"
}

capture_other_owners() {
  local output=$1
  psql_scalar "
    WITH rows AS (
      SELECT 'evidence_extraction_job' AS source,to_jsonb(t)::text AS row_json
      FROM memory.evidence_extraction_job AS t WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'evidence_extraction_event',to_jsonb(t)::text
      FROM memory.evidence_extraction_event AS t WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'evidence_extraction_packet_v5_local',to_jsonb(t)::text
      FROM memory.evidence_extraction_packet_v5_local AS t WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'v5_local_inference_event',to_jsonb(t)::text
      FROM memory.v5_local_inference_event AS t WHERE owner_user_id<>'$owner'::uuid
    )
    SELECT source || E'\\t' || count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
    FROM rows GROUP BY source ORDER BY source
  " >"$output"
  chmod 0600 "$output"
}

capture_allowed_owner() {
  local directory=$1
  psql_scalar "
    SELECT job_id::text || E'\\t' || encode(public.digest(
      convert_to(to_jsonb(t)::text,'UTF8'),'sha256'),'hex')
    FROM memory.evidence_extraction_job AS t
    WHERE owner_user_id='$owner'::uuid ORDER BY job_id
  " >"$directory/jobs.tsv"
  psql_scalar "
    SELECT event_id::text || E'\\t' || encode(public.digest(
      convert_to(to_jsonb(t)::text,'UTF8'),'sha256'),'hex')
    FROM memory.evidence_extraction_event AS t
    WHERE owner_user_id='$owner'::uuid ORDER BY event_id
  " >"$directory/extraction_events.tsv"
  psql_scalar "
    SELECT event_id::text || E'\\t' || encode(public.digest(
      convert_to(to_jsonb(t)::text,'UTF8'),'sha256'),'hex')
    FROM memory.v5_local_inference_event AS t
    WHERE owner_user_id='$owner'::uuid ORDER BY event_id
  " >"$directory/local_events.tsv"
  psql_scalar "
    SELECT packet_id::text || E'\\t' || encode(public.digest(
      convert_to(to_jsonb(t)::text,'UTF8'),'sha256'),'hex')
    FROM memory.evidence_extraction_packet_v5_local AS t
    WHERE owner_user_id='$owner'::uuid ORDER BY packet_id
  " >"$directory/packets.tsv"
  chmod 0600 "$directory"/*.tsv
}

for pair in \
  "$service_source:$expected_service_sha" \
  "$timer_source:$expected_timer_sha" \
  "$scheduler:$expected_scheduler_sha" \
  "$canary:$expected_canary_sha"; do
  file=${pair%%:*}
  expected=${pair##*:}
  [[ -f "$file" ]]
  [[ "$(sha256sum "$file" | awk '{print $1}')" == "$expected" ]]
done
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor 0fdf0d1343c222d8e03b8227daf398669983c073 HEAD
[[ ! -e "/etc/systemd/system/$service" ]]
[[ ! -e "/etc/systemd/system/$timer" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
sudo -n test -r /etc/memory-v1-local-inference/api-key
[[ "$(sudo -n stat -c '%a:%U:%G' /etc/memory-v1-local-inference/api-key)" == 600:root:root ]]
python3 scripts/memory_v1_v5_local_inference_scheduler_test.py >/dev/null
systemd-analyze verify "$service_source" "$timer_source"

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_local_scheduler_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_local_scheduler_${run_tag}.json"
service_output="$snapshot_dir/memory_v1_v5_local_scheduler_${run_tag}.service.json"

phase=quiesce_existing_timers
: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  [[ "$unit" != "$timer" ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ "$(wc -l <"$unit_state")" -eq 6 ]]
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$unit_state"
while IFS=$'\t' read -r unit _enabled _active; do
  existing_service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$existing_service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$existing_service"
done <"$unit_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_local_scheduler_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_local_scheduler_${run_tag}.dump"
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

phase=baseline
psql_scalar "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name NOT IN (
      'evidence_extraction_job','evidence_extraction_event',
      'evidence_extraction_packet_v5_local','v5_local_inference_event'
    )
  ORDER BY table_name
" >"$protected_tables"
[[ -s "$protected_tables" ]]
capture_protected "$protected_before"
capture_other_owners "$other_before"
capture_allowed_owner "$allowed_before"
qdrant_before=$(qdrant_signature)

phase=install_disabled_units
sudo -n install -o root -g root -m 0644 "$service_source" \
  "/etc/systemd/system/$service"
sudo -n install -o root -g root -m 0644 "$timer_source" \
  "/etc/systemd/system/$timer"
sudo -n systemctl daemon-reload
unit_installed=1
[[ "$(systemctl is-enabled "$timer")" == disabled ]]
[[ "$(systemctl is-active "$timer")" == inactive ]]

phase=one_record_private_canary
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
sudo -n systemctl start "$service"
[[ "$(systemctl show "$service" -p Result --value)" == success ]]
sudo -n journalctl -u "$service" --since "$started_at" --no-pager -o cat \
  | grep '^{' | tail -n 1 >"$service_output"
chmod 0600 "$service_output"
jq -e '
  .worker_version=="memory_v1_v5_local_inference_scheduler_v1" and
  .apply==true and .processed==1 and
  (.result.outcome=="accepted" or .result.outcome=="rejected") and
  .result.local_model_calls==1 and .result.external_model_calls==0 and
  .result.zero_write_replay_proved==true and
  .result.write_counts.claims==0 and .result.write_counts.qdrant==0 and
  .result.write_counts.prompt_influence==0
' "$service_output" >/dev/null
outcome=$(jq -r '.result.outcome' "$service_output")

phase=postflight
capture_protected "$protected_after"
cmp -s "$protected_before" "$protected_after"
capture_other_owners "$other_after"
cmp -s "$other_before" "$other_after"
capture_allowed_owner "$allowed_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

BEFORE="$allowed_before" AFTER="$allowed_after" OUTCOME="$outcome" python3 - <<'PY'
import os
from pathlib import Path

before = Path(os.environ["BEFORE"])
after = Path(os.environ["AFTER"])
outcome = os.environ["OUTCOME"]

def rows(directory: Path, name: str) -> dict[str, str]:
    result = {}
    for line in (directory / name).read_text().splitlines():
        key, digest = line.split("\t", 1)
        result[key] = digest
    return result

jobs_before = rows(before, "jobs.tsv")
jobs_after = rows(after, "jobs.tsv")
assert jobs_before.keys() == jobs_after.keys()
assert sum(jobs_before[key] != jobs_after[key] for key in jobs_before) == 1

for name, expected_delta in (
    ("extraction_events.tsv", 2),
    ("local_events.tsv", 2),
    ("packets.tsv", 1 if outcome == "accepted" else 0),
):
    old = rows(before, name)
    new = rows(after, name)
    assert all(new.get(key) == digest for key, digest in old.items())
    assert len(new) - len(old) == expected_delta
PY

phase=restore_existing_timers
restore_units

phase=enable_scheduler_timer
sudo -n systemctl enable --now "$timer" >/dev/null
[[ "$(systemctl is-enabled "$timer")" == enabled ]]
[[ "$(systemctl is-active "$timer")" == active ]]
installation_committed=1

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg owner_sha256 "$(printf %s "$owner" | sha256sum | awk '{print $1}')" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg service_output "$service_output" --arg outcome "$outcome" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_v5_local_scheduler_install_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    owner_user_id_sha256:$owner_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    canary:{sanitized_output:$service_output,outcome:$outcome,
      local_model_calls:1,external_model_calls:0},
    checks:{fresh_backup:true,hash_locked_runtime:true,
      one_owner_scoped_job:true,zero_write_replay:true,
      protected_memory_tables_unchanged:true,
      other_owner_rows_unchanged:true,qdrant_unchanged:true,
      claims_written:0,prompt_influence:0,existing_timers_restored:true,
      scheduler_timer_enabled:true},
    qdrant_sha256:$qdrant_sha256,
    schedule:{max_jobs_per_cycle:1,interval:"2h",rolling_24h_limit:12,
      consecutive_rejection_stop:3},
    hard_stop:"before_packet_review_staging_claim_promotion_or_prompt_influence"}' \
  >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_local_inference_scheduler_production_install: PASS\n'
printf 'report=%s\nbackup=%s\noutcome=%s\n' "$report" "$backup" "$outcome"

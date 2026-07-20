#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the V5/V5.1 downstream firewall, replaces the
# local inference scheduler with the admin-only V5.1 shadow unit, executes one
# private canary, and proves that no downstream lane or protected store changed.

if [[ "${MEMORY_V1_PREDICATE_RUNTIME_V5_1_ACTIVATE:-}" != authorized ]]; then
  echo 'MEMORY_V1_PREDICATE_RUNTIME_V5_1_ACTIVATE=authorized is required' >&2
  exit 1
fi

repo=/opt/chat-memory
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
authorized_base=76cc4736dad0bc2d8ebca3534b9d4d8a7cf299a7
implementation_ancestor=c4aab3da82a3f6eb6af2d2f38fa1470772e083c5
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_predicate_runtime_v5_1_activate.lock
env_file=/opt/chat-memory/.env
service=memory-v1-v5-local-inference-scheduler.service
timer=memory-v1-v5-local-inference-scheduler.timer
service_source=ops/systemd/$service
timer_source=ops/systemd/$timer
migration=ops/sql/20260720_memory_v1_predicate_runtime_v5_1_downstream_isolation.sql
test_sql=tests/memory_v1_predicate_runtime_v5_1_downstream_isolation.sql
contract_test=scripts/memory_v1_v5_multi_owner_automation_contract_test.py
scheduler=scripts/memory_v1_v5_local_inference_scheduler.py
compiler_sha=af0e7b679480db10855cfb0ab2b705acd26a97238869e12b8b9a3f17bbc0024d

phase=initialization
run_id=
status_file=
timers_quiesced=0
units_installed=0
activation_committed=0
timer_state=$(mktemp /tmp/memory-v1-v5-1-activate-timers.XXXXXX)
unit_backup=$(mktemp -d /tmp/memory-v1-v5-1-activate-units.XXXXXX)
all_tables=$(mktemp /tmp/memory-v1-v5-1-activate-all-tables.XXXXXX)
protected_tables=$(mktemp /tmp/memory-v1-v5-1-activate-protected-tables.XXXXXX)
all_before=$(mktemp /tmp/memory-v1-v5-1-activate-all-before.XXXXXX)
all_after_migration=$(mktemp /tmp/memory-v1-v5-1-activate-all-after-migration.XXXXXX)
protected_before=$(mktemp /tmp/memory-v1-v5-1-activate-protected-before.XXXXXX)
protected_after=$(mktemp /tmp/memory-v1-v5-1-activate-protected-after.XXXXXX)
other_before=$(mktemp /tmp/memory-v1-v5-1-activate-other-before.XXXXXX)
other_after=$(mktemp /tmp/memory-v1-v5-1-activate-other-after.XXXXXX)
allowed_before=$(mktemp -d /tmp/memory-v1-v5-1-activate-allowed-before.XXXXXX)
allowed_after=$(mktemp -d /tmp/memory-v1-v5-1-activate-allowed-after.XXXXXX)
chmod 0600 "$timer_state" "$all_tables" "$protected_tables" \
  "$all_before" "$all_after_migration" "$protected_before" \
  "$protected_after" "$other_before" "$other_after"
chmod 0700 "$unit_backup" "$allowed_before" "$allowed_after"

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

run_sql_file() {
  local file=$1
  docker exec \
    -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=180s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$database" <"$file"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

authenticated_health() {
  set -a
  source "$env_file"
  set +a
  test -n "${VS_SERVICE_TOKEN:-}"
  [[ "$(systemctl is-active brains.service)" == active ]]
  [[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/readyz \
    | jq -e '.ok==true and .postgres==true' >/dev/null
}

capture_tables() {
  local table_file=$1 output=$2 table state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
        encode(public.digest(convert_to(coalesce(string_agg(
          row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_file"
  chmod 0600 "$output"
}

capture_other_owners() {
  local output=$1
  psql_scalar "
    WITH rows AS (
      SELECT 'evidence_extraction_job' AS source,to_jsonb(t)::text AS row_json
      FROM memory.evidence_extraction_job AS t
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'evidence_extraction_event',to_jsonb(t)::text
      FROM memory.evidence_extraction_event AS t
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'evidence_extraction_packet_v5_local',to_jsonb(t)::text
      FROM memory.evidence_extraction_packet_v5_local AS t
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT 'v5_local_inference_event',to_jsonb(t)::text
      FROM memory.v5_local_inference_event AS t
      WHERE owner_user_id<>'$owner'::uuid
    )
    SELECT source || E'\\t' || count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(
        row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
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

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    if [[ "$enabled" == enabled ]]; then
      sudo -n systemctl enable "$unit" >/dev/null
    else
      [[ "$enabled" == disabled ]]
      sudo -n systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      [[ "$active" == inactive ]]
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

restore_units() {
  [[ "$units_installed" -eq 1 ]] || return 0
  sudo -n install -o root -g root -m 0644 "$unit_backup/$service" \
    "/etc/systemd/system/$service"
  sudo -n install -o root -g root -m 0644 "$unit_backup/$timer" \
    "/etc/systemd/system/$timer"
  sudo -n systemctl daemon-reload
  units_installed=0
}

record_exit() {
  exit_code=$?
  if [[ "$activation_committed" -eq 0 ]]; then
    restore_units || exit_code=1
  fi
  restore_timers || exit_code=1
  rm -rf "$unit_backup" "$allowed_before" "$allowed_after"
  rm -f "$timer_state" "$all_tables" "$protected_tables" \
    "$all_before" "$all_after_migration" "$protected_before" \
    "$protected_after" "$other_before" "$other_after"
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

phase=source_preflight
cd "$repo"
git merge-base --is-ancestor "$authorized_base" HEAD
git merge-base --is-ancestor "$implementation_ancestor" HEAD
[[ -z "$(git status --porcelain)" ]]
sha256sum -c <<'HASHES'
92491881458d0ab9d94583306dbba3fc463c187e703bc6ea910c8244204f546e  ops/systemd/memory-v1-v5-local-inference-scheduler.service
456a780bf30533ccf10955982be68275c01b22cdf157c74adba05e0b6fb0b2b0  ops/systemd/memory-v1-v5-local-inference-scheduler.timer
17e9f5889363a57bcdadaa85e5fcb06f8e9eb063b31bb866e6b8a49cd1d099c7  scripts/memory_v1_v5_multi_owner_automation_contract_test.py
d20b8506f544701d58cc05a237b3131f748b6a1dd1c6c6ca56982c04c7fef2f7  ops/sql/20260720_memory_v1_predicate_runtime_v5_1_downstream_isolation.sql
541221a287fb5724f55fb87cbbb0ce8f570fb23e34982f0a1c162afa09095d42  tests/memory_v1_predicate_runtime_v5_1_downstream_isolation.sql
35f163241a5d770dd953d999b284e13d63735fbdbfdeeeabb98fb10ec0550af3  scripts/memory_v1_v5_local_inference_scheduler.py
cd1524ec1ada583ee4b1fde7aa70cf44cf7bf44bb55773b14c25e6c4c93b8d57  scripts/memory_v1_predicate_runtime_profile.py
84606d74ba719c68baaca77d9e73b4787b96ec8f3398aca60a24ee2bbfa8076e  scripts/memory_v1_relational_extraction_v5_local_provider.py
f436a551785c0f1413f70e9ba5ab04e8b1735496ebd835fd8ff9f9068972f219  scripts/memory_v1_relationship_policy_v5_1.py
e0f4e2c2e2afddbe61655ee6cbdb4734252213354ddff151270483ecfd8265d1  specs/memory_v1_predicate_runtime_profiles_v1.json
b56b20db3ce7f2e9c9ded4e00b9419f391e09c8f4f3d23d95ac2adac6028197b  specs/memory_v1_relational_extraction_v5_1.schema.json
HASHES
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python "$contract_test" >/dev/null
systemd-analyze verify "$service_source" "$timer_source"
authenticated_health
sudo -n test -r /etc/memory-v1-local-inference/api-key
[[ "$(sudo -n stat -c '%a:%U:%G' /etc/memory-v1-local-inference/api-key)" == 600:root:root ]]

phase=database_preflight
[[ "$(psql_scalar "SELECT count(*) FROM memory.predicate_contract WHERE registry_version='memory_predicate_registry_v5_1'")" == 82 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.predicate_registry_seed WHERE registry_version='memory_predicate_registry_v5_1'")" == 82 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.relationship_predicate_contract_v5_1")" == 41 ]]
[[ "$(psql_scalar "SELECT runtime_active::int FROM memory.predicate_registry_version WHERE registry_version='memory_predicate_registry_v5_1'")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.v5_local_packet_disposition AS lane JOIN memory.evidence_extraction_packet_v5_local AS packet USING(owner_user_id,packet_id) WHERE packet.normalized_packet->>'contract_version'<>'memory_v1_relational_extraction_v5' OR packet.normalized_packet->>'predicate_registry_version'<>'memory_predicate_registry_v5'")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.v5_local_packet_review_artifact AS lane JOIN memory.evidence_extraction_packet_v5_local AS packet USING(owner_user_id,packet_id) WHERE packet.normalized_packet->>'contract_version'<>'memory_v1_relational_extraction_v5' OR packet.normalized_packet->>'predicate_registry_version'<>'memory_predicate_registry_v5'")" == 0 ]]
[[ -f "/etc/systemd/system/$service" && -f "/etc/systemd/system/$timer" ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_predicate_runtime_v5_1_activate_${run_id}.status"
report="$snapshot_dir/memory_v1_predicate_runtime_v5_1_activate_${run_id}.json"
plan="$snapshot_dir/memory_v1_predicate_runtime_v5_1_plan_${run_id}.json"
service_output="$snapshot_dir/memory_v1_predicate_runtime_v5_1_canary_${run_id}.json"

phase=capture_and_quiesce_timers
: >"$timer_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ "$(wc -l <"$timer_state")" -eq 16 ]]
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  related_service=${unit%.timer}.service
  for _attempt in $(seq 1 60); do
    systemctl is-active --quiet "$related_service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$related_service"
done <"$timer_state"

phase=fresh_backup
backup_partial="$snapshot_dir/.memory_pre_predicate_runtime_v5_1_activate_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_predicate_runtime_v5_1_activate_${run_id}.dump"
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
psql_scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" >"$all_tables"
grep -Ev '^(evidence_extraction_job|evidence_extraction_event|evidence_extraction_packet_v5_local|v5_local_inference_event)$' "$all_tables" >"$protected_tables"
[[ -s "$all_tables" && -s "$protected_tables" ]]
capture_tables "$all_tables" "$all_before"
qdrant_before=$(qdrant_signature)

phase=install_downstream_firewall
run_sql_file "$migration" >/dev/null
run_sql_file "$migration" >/dev/null
run_sql_file "$test_sql" >/dev/null
capture_tables "$all_tables" "$all_after_migration"
cmp -s "$all_before" "$all_after_migration"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=quota_and_plan_preflight
reserved=$(psql_scalar "SELECT count(*) FROM memory.v5_local_inference_event WHERE owner_user_id='$owner'::uuid AND action='reserved' AND created_at>=clock_timestamp()-interval '24 hours'")
[[ "$reserved" -lt 12 ]]
set -a
source "$env_file"
set +a
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python "$scheduler" \
  --owner-user-id "$owner" --contract-profile v5_1 --max-jobs 1 \
  --max-attempts 1 --lease-seconds 900 --timeout-seconds 600 \
  --max-output-tokens 4096 --rolling-window-seconds 86400 \
  --max-reserved-jobs 12 --failure-threshold 3 >"$plan"
chmod 0600 "$plan"
jq -e '
  .apply==false and .owner_count==1 and .max_jobs==1 and
  .predicate_contract_profile=="v5_1" and
  .extraction_contract_version=="memory_v1_relational_extraction_v5_1" and
  .predicate_registry_version=="memory_predicate_registry_v5_1" and
  .external_model_calls==0 and .local_model_calls==0 and
  (.write_counts|to_entries|map(.value==0)|all) and
  (.plans|length)==1 and .plans[0].next_job_id_sha256!=null
' "$plan" >/dev/null
selected_job=$(psql_scalar "SELECT job_id::text FROM memory.evidence_extraction_job WHERE owner_user_id='$owner'::uuid AND route='relational_extraction' AND status IN ('pending','error') AND attempts<1 AND available_at<=clock_timestamp() ORDER BY priority,available_at,created_at,job_id LIMIT 1")
[[ -n "$selected_job" ]]
selected_job_sha=$(printf %s "$selected_job" | sha256sum | awk '{print $1}')
[[ "$(jq -r '.plans[0].next_job_id_sha256' "$plan")" == "$selected_job_sha" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local WHERE owner_user_id='$owner'::uuid AND job_id='$selected_job'::uuid")" == 0 ]]

phase=capture_canary_baseline
capture_tables "$protected_tables" "$protected_before"
capture_other_owners "$other_before"
capture_allowed_owner "$allowed_before"
cp "/etc/systemd/system/$service" "$unit_backup/$service"
cp "/etc/systemd/system/$timer" "$unit_backup/$timer"
chmod 0600 "$unit_backup/$service" "$unit_backup/$timer"

phase=install_v5_1_shadow_units
sudo -n install -o root -g root -m 0644 "$service_source" \
  "/etc/systemd/system/$service"
sudo -n install -o root -g root -m 0644 "$timer_source" \
  "/etc/systemd/system/$timer"
sudo -n systemctl daemon-reload
units_installed=1
cmp -s "$service_source" "/etc/systemd/system/$service"
cmp -s "$timer_source" "/etc/systemd/system/$timer"
[[ "$(systemctl is-active "$timer")" == inactive ]]

phase=one_record_private_v5_1_canary
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
sudo -n systemctl start "$service"
[[ "$(systemctl show "$service" -p Result --value)" == success ]]
sudo -n journalctl -u "$service" --since "$started_at" --no-pager -o cat \
  | grep '^{' | tail -n 1 >"$service_output"
chmod 0600 "$service_output"
jq -e --arg job_sha "$selected_job_sha" '
  .worker_version=="memory_v1_v5_local_inference_scheduler_v1" and
  .apply==true and .owner_count==1 and .processed==1 and
  .predicate_contract_profile=="v5_1" and
  .extraction_contract_version=="memory_v1_relational_extraction_v5_1" and
  .predicate_registry_version=="memory_predicate_registry_v5_1" and
  .result.outcome=="accepted" and .result.job_id_sha256==$job_sha and
  .result.external_model_calls==0 and .result.local_model_calls==1 and
  .result.zero_write_replay_proved==true and
  .result.write_counts.claims==0 and .result.write_counts.qdrant==0 and
  .result.write_counts.prompt_influence==0 and
  .result.write_counts.packets==1
' "$service_output" >/dev/null

phase=canary_database_verification
packet_id=$(psql_scalar "SELECT packet_id::text FROM memory.evidence_extraction_packet_v5_local WHERE owner_user_id='$owner'::uuid AND job_id='$selected_job'::uuid")
[[ -n "$packet_id" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local WHERE owner_user_id='$owner'::uuid AND packet_id='$packet_id'::uuid AND normalized_packet->>'contract_version'='memory_v1_relational_extraction_v5_1' AND normalized_packet->>'predicate_registry_version'='memory_predicate_registry_v5_1' AND policy_compiler_sha256='$compiler_sha'")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.v5_local_packet_disposition WHERE owner_user_id='$owner'::uuid AND packet_id='$packet_id'::uuid")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.v5_local_packet_review_artifact WHERE owner_user_id='$owner'::uuid AND packet_id='$packet_id'::uuid")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_job WHERE owner_user_id='$owner'::uuid AND job_id='$selected_job'::uuid AND status='review_required' AND lease_token IS NULL AND lease_expires_at IS NULL AND last_error IS NULL")" == 1 ]]

phase=live_rollback_only_firewall_probe
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
  -v owner="$owner" -v packet_id="$packet_id" >/dev/null <<'SQL'
BEGIN;
SELECT set_config('app.v5_1_probe_owner', :'owner', true);
SELECT set_config('app.v5_1_probe_packet', :'packet_id', true);
DO $probe$
DECLARE
  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
BEGIN
  SELECT * INTO STRICT packet
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id=current_setting('app.v5_1_probe_owner')::uuid
    AND packet_id=current_setting('app.v5_1_probe_packet')::uuid;
  BEGIN
    INSERT INTO memory.v5_local_packet_disposition(
      disposition_id,owner_user_id,operation_id,packet_id,job_id,evidence_id,
      disposition,reason_code,evidence_content_sha256,
      validator_packet_sha256,packet_storage_sha256,entity_mention_count,
      observation_count,comparison_hint_count,deferral_count,
      local_model_calls,external_model_calls
    ) VALUES (
      public.gen_random_uuid(),packet.owner_user_id,public.gen_random_uuid(),
      packet.packet_id,packet.job_id,packet.evidence_id,'terminal_no_stage',
      'deferral_only_no_stage',packet.evidence_content_sha256,
      packet.validator_packet_sha256,packet.packet_storage_sha256,
      0,0,0,1,packet.local_model_calls,0
    );
    RAISE EXCEPTION 'cross-profile insert unexpectedly succeeded';
  EXCEPTION WHEN check_violation THEN
    IF SQLERRM<>'non-V5 packet cannot enter the legacy packet lane' THEN
      RAISE;
    END IF;
  END;
END
$probe$;
ROLLBACK;
SQL

phase=postflight
capture_tables "$protected_tables" "$protected_after"
cmp -s "$protected_before" "$protected_after"
capture_other_owners "$other_after"
cmp -s "$other_before" "$other_after"
capture_allowed_owner "$allowed_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

BEFORE="$allowed_before" AFTER="$allowed_after" SELECTED_JOB="$selected_job" python3 - <<'PY'
import os
from pathlib import Path

before = Path(os.environ["BEFORE"])
after = Path(os.environ["AFTER"])
selected = os.environ["SELECTED_JOB"]

def rows(directory: Path, name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in (directory / name).read_text().splitlines():
        key, digest = line.split("\t", 1)
        result[key] = digest
    return result

old_jobs = rows(before, "jobs.tsv")
new_jobs = rows(after, "jobs.tsv")
assert old_jobs.keys() == new_jobs.keys()
assert [key for key in old_jobs if old_jobs[key] != new_jobs[key]] == [selected]

for name, delta in (
    ("extraction_events.tsv", 2),
    ("local_events.tsv", 2),
    ("packets.tsv", 1),
):
    old = rows(before, name)
    new = rows(after, name)
    assert all(new.get(key) == digest for key, digest in old.items())
    assert len(new) - len(old) == delta
PY

phase=restore_timers_and_health
restore_timers
authenticated_health
[[ "$(systemctl is-enabled "$timer")" == enabled ]]
[[ "$(systemctl is-active "$timer")" == active ]]

phase=report
packet_sha=$(printf %s "$packet_id" | sha256sum | awk '{print $1}')
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha" \
  --arg plan "$plan" --arg canary "$service_output" \
  --arg selected_job_sha256 "$selected_job_sha" \
  --arg packet_id_sha256 "$packet_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{contract_version:"memory_v1_predicate_runtime_v5_1_activation_report_v1",
    completed_at:$completed_at,head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    canary:{owner_count:1,profile:"v5_1",outcome:"accepted",
      local_model_calls:1,external_model_calls:0,
      selected_job_sha256:$selected_job_sha256,
      packet_id_sha256:$packet_id_sha256,
      sanitized_plan:$plan,sanitized_output:$canary},
    checks:{fresh_backup:true,hash_locked_runtime:true,
      downstream_contract_firewall:true,rollback_probe_passed:true,
      migration_row_changes:0,one_owner_scoped_job:true,
      zero_write_replay:true,other_owner_rows_unchanged:true,
      protected_memory_tables_unchanged:true,qdrant_unchanged:true,
      claims_written:0,prompt_influence:0,external_model_calls:0,
      legacy_packet_lane_rows:0,original_timer_states_restored:true,
      service_healthy:true},
    qdrant_sha256:$qdrant_sha256,
    hard_stop:"before_v5_1_review_stage_claim_projection_retrieval_or_prompt_influence"}' \
  >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|map(.value==true or .value==0)|all' "$report" >/dev/null
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

activation_committed=1
phase=complete
printf '%s\n' 'memory_v1_predicate_runtime_v5_1_activate: PASS'
printf 'report=%s\nbackup=%s\ncanary=%s\n' "$report" "$backup" "$service_output"

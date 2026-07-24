#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the narrow V5.2 content-free
# structured-domain terminal-disposition compatibility path and writes exactly
# one append-only owner-scoped disposition. It does not create review
# artifacts, staging rows, claims, vectors, retrieval, or prompt influence.

if [[ "${MEMORY_V1_V5_2_STRUCTURED_TERMINAL_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_STRUCTURED_TERMINAL_APPLY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=a24c2166-ec0d-558c-ab96-9e1ca74c97a2
job=f4409de8-3d64-4c7f-9991-497021d0bd40
evidence=333def81-9c23-5c80-96ce-93f467323417
packet_storage_sha256=b9016b1afbdbbf459f8871495ffe046a5288c306c44426a89d5f3c42596ac012
plan=ops/manifests/memory_v1_v5_2_structured_domain_terminal_production_plan.json
expected_plan_sha256=0463c19055b8ac4cdb14f1229819971ab5947e5ec0ba02ac98cb1c2323c66240
migration=ops/sql/20260723_memory_v1_v5_2_structured_domain_terminal.sql
rollback=ops/sql/20260723_memory_v1_v5_2_structured_domain_terminal_rollback.sql
security_test=tests/memory_v1_v5_2_structured_domain_terminal.sql
downstream_isolation_test=tests/memory_v1_predicate_runtime_v5_1_downstream_isolation.sql
clone_test=tools/memory_v1_v5_2_structured_domain_terminal_clone.sh
worker=scripts/memory_v1_v5_local_packet_disposition.py
worker_test=scripts/memory_v1_v5_local_packet_disposition_test.py
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_structured_terminal_apply.lock

phase=initialization
run_tag=
status_file=
timers_quiesced=0
schema_installed=0
data_written=0
installation_committed=0
timer_state=$(mktemp /tmp/memory-v1-v5-2-structured-terminal-timers.XXXXXX)
timer_restored=$(mktemp /tmp/memory-v1-v5-2-structured-terminal-restored.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-2-structured-terminal-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-2-structured-terminal-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-v5-2-structured-terminal-after.XXXXXX.tsv)
dry_output=$(mktemp /tmp/memory-v1-v5-2-structured-terminal-dry.XXXXXX.json)
other_output=$(mktemp /tmp/memory-v1-v5-2-structured-terminal-other.XXXXXX.json)
apply_output=$(mktemp /tmp/memory-v1-v5-2-structured-terminal-apply.XXXXXX.json)
replay_output=$(mktemp /tmp/memory-v1-v5-2-structured-terminal-replay.XXXXXX.json)
chmod 0600 "$timer_state" "$timer_restored" "$table_list" "$before" "$after" \
  "$dry_output" "$other_output" "$apply_output" "$replay_output"

run_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d "$database" "$@"
}

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
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

capture_memory_tables() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    [[ "$schema" =~ ^[a-z][a-z0-9_]*$ ]]
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
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
  chmod 0600 "$output"
}

other_disposition_signature() {
  scalar "
    SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
      coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
      'UTF8'),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(value)::text AS row_json
      FROM memory.v5_local_packet_disposition AS value
      WHERE NOT (
        owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
      )
    ) AS rows
  "
}

capture_timer_state() {
  local output=$1 unit
  : >"$output"
  while IFS= read -r unit; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    printf '%s\t%s\t%s\n' "$unit" \
      "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
      >>"$output"
  done < <(
    systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
      | awk '{print $1}' | sort -u
  )
  [[ "$(wc -l <"$output")" -ge 1 ]]
  chmod 0600 "$output"
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
  capture_timer_state "$timer_restored"
  cmp -s "$timer_state" "$timer_restored"
  timers_quiesced=0
}

record_exit() {
  exit_code=$?
  if [[ "$schema_installed" -eq 1 && "$data_written" -eq 0 \
        && "$installation_committed" -eq 0 ]]; then
    run_sql <"$rollback" >/dev/null 2>&1 || exit_code=1
  fi
  restore_timers || exit_code=1
  rm -f "$timer_state" "$timer_restored" "$table_list" "$before" "$after" \
    "$dry_output" "$other_output" "$apply_output" "$replay_output"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'schema_installed=%s\n' "$schema_installed"
      printf 'data_written=%s\n' "$data_written"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

[[ -f "$plan" ]]
[[ "$(sha256sum "$plan" | awk '{print $1}')" == "$expected_plan_sha256" ]]
jq -e \
  --arg owner "$owner" --arg packet "$packet" --arg job "$job" \
  --arg evidence "$evidence" --arg packet_sha "$packet_storage_sha256" '
    .contract_version
      =="memory_v1_v5_2_structured_domain_terminal_production_plan_v1" and
    .target.owner_user_id==$owner and .target.packet_id==$packet and
    .target.job_id==$job and .target.evidence_id==$evidence and
    .target.packet_storage_sha256==$packet_sha and
    .allowed_persistent_changes.rows
      =={"memory.v5_local_packet_disposition":1}
  ' "$plan" >/dev/null

while IFS=$'\t' read -r file expected; do
  [[ "$file" =~ ^[a-zA-Z0-9_./-]+$ ]]
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]]
  [[ -f "$file" ]]
  [[ "$(sha256sum "$file" | awk '{print $1}')" == "$expected" ]]
done < <(jq -r '.required_inputs|to_entries[]|[.key,.value]|@tsv' "$plan")

expected_base=$(jq -r '.expected_base_commit' "$plan")
[[ "$expected_base" =~ ^[0-9a-f]{40}$ ]]
git merge-base --is-ancestor "$expected_base" HEAD
[[ -z "$(git status --porcelain)" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
[[ "$(docker inspect -f '{{.State.Running}}' "$container")" == true ]]
[[ "$(docker inspect -f '{{.State.Running}}' brains-qdrant-1)" == true ]]
bash -n "$clone_test"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$worker_test" >/dev/null

[[ "$(scalar "
  SELECT count(*)
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND packet_id='$packet'::uuid
    AND job_id='$job'::uuid
    AND evidence_id='$evidence'::uuid
    AND packet_storage_sha256='$packet_storage_sha256'
    AND normalized_packet->>'contract_version'
          ='memory_v1_relational_extraction_v5_2'
    AND normalized_packet->>'predicate_registry_version'
          ='memory_predicate_registry_v5_2'
    AND entity_mention_count=0
    AND observation_count=0
    AND comparison_hint_count=0
    AND deferral_count=1
    AND NOT manual_review_required
    AND normalized_packet @?
      '$.deferrals[*] ? (@.reason_code == \"structured_domain\" && @.memory_shape == \"none\" && @.review_required == false)'
")" == 1 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
")" == 0 ]]

phase=clone_verification
"$clone_test" >/dev/null

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_structured_terminal_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_structured_terminal_${run_tag}.json"

phase=quiesce_timers
capture_timer_state "$timer_state"
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

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_2_structured_terminal_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_structured_terminal_${run_tag}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
backup_sha256=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha256" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
query_rows "
  SELECT table_schema || E'\\t' || table_name
  FROM information_schema.tables
  WHERE table_type='BASE TABLE' AND table_schema='memory'
    AND table_name<>'v5_local_packet_disposition'
  ORDER BY table_schema,table_name
" >"$table_list"
[[ -s "$table_list" ]]
capture_memory_tables "$before"
other_dispositions_before=$(other_disposition_signature)
qdrant_before=$(qdrant_signature)

phase=install_schema
run_sql <"$migration" >/dev/null
schema_installed=1
run_sql <"$migration" >/dev/null

phase=rollback_only_security_tests
set -a
source .env
set +a
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" -v other_owner="$other" \
  -v packet_id="$packet" \
  -v packet_storage_sha256="$packet_storage_sha256" \
  -v observation_packet_id=e21e39bb-20fe-5b95-bc72-77fd774f0faf \
  -v observation_packet_storage_sha256="$(scalar "
    SELECT packet_storage_sha256
    FROM memory.evidence_extraction_packet_v5_local
    WHERE owner_user_id='$owner'::uuid
      AND packet_id='e21e39bb-20fe-5b95-bc72-77fd774f0faf'::uuid
  ")" <"$security_test" >/dev/null
run_sql -v target_owner="$owner" -v other_owner="$other" \
  <"$downstream_isolation_test" >/dev/null
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
")" == 0 ]]

phase=dry_run
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" >"$dry_output"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$other" >"$other_output"
DRY="$dry_output" OTHER="$other_output" PACKET="$packet" python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

dry = json.loads(Path(os.environ["DRY"]).read_text())
other = json.loads(Path(os.environ["OTHER"]).read_text())
packet_sha256 = hashlib.sha256(os.environ["PACKET"].encode()).hexdigest()
assert dry["apply"] is False and dry["database_writes"] == 0
assert dry["external_model_calls"] == 0 and dry["prompt_influence"] == 0
assert len(dry["plans"]) == 1
assert dry["plans"][0]["packet_id_sha256"] == packet_sha256
assert dry["plans"][0]["route"] == "terminal_deferral"
assert dry["plans"][0]["reason_code"] == "deferral_only_no_stage"
assert dry["plans"][0]["counts"] == {
    "entity_mentions": 0,
    "observations": 0,
    "comparison_hints": 0,
    "deferrals": 1,
}
assert all(
    row.get("packet_id_sha256") != packet_sha256 for row in other["plans"]
)
assert "source_text" not in json.dumps([dry, other])
PY

phase=apply_exact_disposition
POSTGRES_DSN="$POSTGRES_DSN" \
MEMORY_V1_V5_LOCAL_PACKET_DISPOSITION_APPLY=memory_v1_v5_local_packet_disposition_apply_v1 \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" --apply >"$apply_output"
if [[ "$(scalar "
  SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND disposition='terminal_no_stage'
    AND reason_code='deferral_only_no_stage'
    AND review_decision IS NULL
    AND NOT promotion_eligible
    AND review_basis_sha256 IS NULL
")" == 1 ]]; then
  data_written=1
fi
[[ "$data_written" -eq 1 ]]

POSTGRES_DSN="$POSTGRES_DSN" \
MEMORY_V1_V5_LOCAL_PACKET_DISPOSITION_APPLY=memory_v1_v5_local_packet_disposition_apply_v1 \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" --apply >"$replay_output"
APPLIED="$apply_output" REPLAYED="$replay_output" python3 - <<'PY'
import json
import os
from pathlib import Path

applied = json.loads(Path(os.environ["APPLIED"]).read_text())
replayed = json.loads(Path(os.environ["REPLAYED"]).read_text())
assert applied["apply"] is True and applied["outcome"] == "terminal_no_stage"
assert applied["write_counts"]["dispositions"] == 1
assert applied["write_counts"]["stage"] == 0
assert applied["write_counts"]["claims"] == 0
assert applied["write_counts"]["qdrant"] == 0
assert applied["write_counts"]["prompt_influence"] == 0
assert applied["zero_write_replay_proved"] is True
assert applied["external_model_calls"] == 0
assert replayed["outcome"] in {"no_work", "manual_review_pending"}
assert replayed["write_counts"]["dispositions"] == 0
assert replayed["external_model_calls"] == 0
assert "source_text" not in json.dumps([applied, replayed])
PY

phase=postflight
[[ "$(other_disposition_signature)" == "$other_dispositions_before" ]]
capture_memory_tables "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

phase=restore_timers
restore_timers
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]
[[ "$(docker inspect -f '{{.State.Running}}' "$container")" == true ]]
[[ "$(docker inspect -f '{{.State.Running}}' brains-qdrant-1)" == true ]]
installation_committed=1

phase=report
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg plan_sha256 "$expected_plan_sha256" \
  --arg owner_sha256 "$(printf %s "$owner" | sha256sum | awk '{print $1}')" \
  --arg packet_sha256 "$(printf %s "$packet" | sha256sum | awk '{print $1}')" \
  --arg backup "$backup" --arg backup_sha256 "$backup_sha256" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:
      "memory_v1_v5_2_structured_domain_terminal_production_report_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    plan_sha256:$plan_sha256,
    owner_user_id_sha256:$owner_sha256,
    packet_id_sha256:$packet_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    result:{
      terminal_dispositions_written:1,
      review_artifacts_written:0,
      stage_rows_written:0,
      claims_written:0,
      qdrant_writes:0,
      prompt_influence:0,
      external_model_calls:0
    },
    checks:{
      hash_locked_inputs:true,
      production_clone_passed:true,
      fresh_backup:true,
      rollback_only_security_tests:true,
      owner_isolation:true,
      zero_write_replay:true,
      non_target_memory_tables_unchanged:true,
      other_dispositions_unchanged:true,
      qdrant_unchanged:true,
      timers_restored_exactly:true,
      services_healthy:true
    },
    qdrant_sha256:$qdrant_sha256
  }' >"$report"
chmod 0600 "$report"
jq -e '.checks|to_entries|all(.value==true)' "$report" >/dev/null

phase=complete
printf '%s\n' "memory_v1_v5_2_structured_domain_terminal_production: PASS"
printf '%s\n' "report=$report"

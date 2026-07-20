#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the restricted V5.1 shadow-input API and emits
# one owner-only, deterministic, zero-write report. It does not persist
# snapshots or reviews, apply patterns, activate jobs, or affect retrieval.

repo=/opt/chat-memory
container=brains-postgres-1
database=memory
database_role=sage
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
authorized_base=76cc4736dad0bc2d8ebca3534b9d4d8a7cf299a7
implementation_commit=de9e81d
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_pattern_salience_shadow_v5_1.lock
env_file=/opt/chat-memory/.env
migration=ops/sql/20260720_memory_v1_pattern_salience_shadow_input_v5_1.sql
test_sql=tests/memory_v1_pattern_salience_shadow_input_v5_1.sql
worker=scripts/memory_v1_pattern_salience_shadow_v5_1.py
phase=initialization
timers_quiesced=0
timer_state=
status_file=
timers=()

restore_timers() {
  [[ "$timers_quiesced" -eq 1 && -s "$timer_state" ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]] || return 1
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]] || return 1
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      [[ "$active" == inactive ]] || return 1
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  exit_code=$?
  if [[ "$timers_quiesced" -eq 1 ]]; then
    failed_phase=$phase
    phase=restore_timers_after_failure
    restore_timers || exit_code=1
    phase=$failed_phase
  fi
  if [[ -n "$status_file" ]]; then
    {
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U "$database_role" -d "$database" -c "$1"
}

run_sql_file() {
  local file=$1
  docker exec \
    -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=180s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U "$database_role" -d "$database" <"$repo/$file"
}

authenticated_health() {
  set -a
  source "$env_file"
  set +a
  test -n "${VS_SERVICE_TOKEN:-}"
  [[ "$(systemctl is-active brains.service)" == active ]]
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/readyz \
    | jq -e '.ok==true and .postgres==true' >/dev/null
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_memory_state() {
  local table_list=$1 output=$2 state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || return 1
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

phase=source_preflight
cd "$repo"
git merge-base --is-ancestor "$authorized_base" HEAD
git merge-base --is-ancestor "$implementation_commit" HEAD
[[ -z "$(git status --porcelain)" ]]
sha256sum -c <<'HASHES'
eec8b125020507973380471274526e4c629085e27e65e224f3b02a31a38ab6e6  ops/sql/20260720_memory_v1_pattern_salience_shadow_input_v5_1.sql
44787c4cb821400434191cdfacf0179fedd0a9071c96685f63a03efd1f3443d7  tests/memory_v1_pattern_salience_shadow_input_v5_1.sql
57981b31194ae2ff40747277b339e1a52e3fec13e32a52d0c9d1cb41cb59604a  scripts/memory_v1_pattern_salience_shadow_v5_1.py
394d35fdc75b44ab32379d2b66d5eb58466cc315f86ceb7ea6620148d2173287  tests/test_memory_v1_pattern_salience_shadow_v5_1.py
HASHES
PYTHONPATH=. venv/bin/python -m unittest \
  tests/test_memory_v1_pattern_salience_shadow_v5_1.py \
  tests/test_memory_v1_pattern_salience_policy_v5_1.py \
  tests/test_memory_v1_pattern_review_contract_v5_1.py >/dev/null

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
execution_head=$(git rev-parse HEAD)
status_file="$snapshot_dir/memory_v1_pattern_salience_shadow_${run_id}.status"
timer_state="$snapshot_dir/memory_v1_pattern_salience_shadow_timers_before_${run_id}.tsv"

phase=database_preflight
[[ "$(psql_scalar "SELECT current_setting('server_version_num')::integer/10000")" == 16 ]]
[[ "$(psql_scalar "SELECT to_regprocedure('memory.load_pattern_salience_shadow_inputs_v5_1(integer,integer)') IS NULL")" == t ]]
authenticated_health

phase=capture_timer_state
mapfile -t timers < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
[[ ${#timers[@]} -gt 0 ]]
: >"$timer_state"
for unit in "${timers[@]}"; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done
chmod 0600 "$timer_state"

phase=quiesce_memory_v1_timers
for unit in "${timers[@]}"; do sudo -n systemctl stop "$unit"; done
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done <"$timer_state"

phase=capture_baseline
table_list="$snapshot_dir/memory_v1_pattern_salience_shadow_tables_${run_id}.txt"
before_state="$snapshot_dir/memory_v1_pattern_salience_shadow_before_${run_id}.tsv"
after_state="$snapshot_dir/memory_v1_pattern_salience_shadow_after_${run_id}.tsv"
psql_scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" >"$table_list"
chmod 0600 "$table_list"
capture_memory_state "$table_list" "$before_state"
qdrant_before=$(qdrant_signature)

phase=fresh_backup
backup_partial="$snapshot_dir/.memory_pre_pattern_salience_shadow_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_pattern_salience_shadow_${run_id}.dump"
catalog="$backup.catalog"
checksum="$backup.sha256"
docker exec "$container" pg_dump -U "$database_role" -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$checksum"
chmod 0600 "$checksum"

phase=revalidate_before_write
git merge-base --is-ancestor "$implementation_commit" HEAD
[[ -z "$(git status --porcelain)" ]]
sha256sum -c <<'HASHES'
eec8b125020507973380471274526e4c629085e27e65e224f3b02a31a38ab6e6  ops/sql/20260720_memory_v1_pattern_salience_shadow_input_v5_1.sql
44787c4cb821400434191cdfacf0179fedd0a9071c96685f63a03efd1f3443d7  tests/memory_v1_pattern_salience_shadow_input_v5_1.sql
57981b31194ae2ff40747277b339e1a52e3fec13e32a52d0c9d1cb41cb59604a  scripts/memory_v1_pattern_salience_shadow_v5_1.py
HASHES

phase=install_restricted_loader
install_log="$snapshot_dir/memory_v1_pattern_salience_shadow_${run_id}.log"
: >"$install_log"
run_sql_file "$migration" >>"$install_log" 2>&1
run_sql_file "$migration" >>"$install_log" 2>&1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$install_log" 2>&1
chmod 0600 "$install_log"

phase=owner_only_zero_write_shadow
shadow_report="$snapshot_dir/memory_v1_pattern_salience_shadow_zero_write_${run_id}.json"
set -a
source "$env_file"
set +a
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo" venv/bin/python "$worker" \
  --owner-user-id "$owner" --as-of-date 2026-07-20 \
  --output "$shadow_report" >"$shadow_report.summary"
chmod 0600 "$shadow_report.summary"
jq -e '
  .contract_version=="memory_v1_pattern_salience_shadow_report_v5_1" and
  .owner_user_id_sha256=="9f5d6523a63c8ff7391ecf514fab572af530874832cddc9f56eb3db93cf45b15" and
  (.pattern_proposals|length)==0 and
  (.target_snapshot_candidates|length)>0 and
  .write_budget.pattern_heads==0 and
  .write_budget.pattern_revisions==0 and
  .write_budget.qdrant==0 and
  .write_budget.prompt_influence==0 and
  .proof.database_writes==0 and
  .proof.external_model_calls==0 and
  .proof.qdrant_writes==0 and
  .proof.prompt_influence==0 and
  .proof.owner_scope_bound==true and
  .proof.input_complete==true and
  .proof.literal_pattern_identity_fail_closed==true
' "$shadow_report" >/dev/null
sha256sum "$shadow_report" >"$shadow_report.sha256"
chmod 0600 "$shadow_report" "$shadow_report.sha256"

phase=postflight
capture_memory_state "$table_list" "$after_state"
cmp -s "$before_state" "$after_state"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(psql_scalar "SELECT (NOT rolcanlogin AND NOT rolinherit AND NOT rolbypassrls AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole)::text FROM pg_roles WHERE rolname='memory_v5_epistemic_writer'")" == t ]]
[[ "$(psql_scalar "SELECT NOT pg_has_role('brains_app','memory_v5_epistemic_writer','MEMBER')")" == t ]]
[[ "$(psql_scalar "SELECT has_function_privilege('brains_app','memory.load_pattern_salience_shadow_inputs_v5_1(integer,integer)','EXECUTE')")" == t ]]
[[ "$(psql_scalar "SELECT has_table_privilege('brains_app','memory.claim_observation','SELECT')")" == f ]]

phase=restore_timer_state
restore_timers
timer_after="$snapshot_dir/memory_v1_pattern_salience_shadow_timers_after_${run_id}.tsv"
: >"$timer_after"
for unit in "${timers[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_after"
done
chmod 0600 "$timer_after"
cmp -s "$timer_state" "$timer_after"

phase=service_health
authenticated_health

phase=report
report="$snapshot_dir/memory_v1_pattern_salience_shadow_install_${run_id}.json"
python3 - "$report" "$backup" "$checksum" "$shadow_report" "$execution_head" <<'PY'
import hashlib,json,sys
from datetime import datetime,timezone
from pathlib import Path
report,backup,checksum,shadow_path=map(Path,sys.argv[1:5])
execution_head=sys.argv[5]
shadow=json.loads(shadow_path.read_text())
value={
  "contract_version":"memory_v1_pattern_salience_shadow_install_report_v1",
  "completed_at":datetime.now(timezone.utc).isoformat(),
  "source_commit":execution_head,
  "backup":str(backup),
  "backup_sha256":checksum.read_text().split()[0],
  "shadow_report":str(shadow_path),
  "shadow_report_file_sha256":hashlib.sha256(shadow_path.read_bytes()).hexdigest(),
  "shadow_summary":{
    "observations":shadow["input_counts"]["observations"],
    "targets":shadow["input_counts"]["targets"],
    "pattern_proposals":len(shadow["pattern_proposals"]),
    "target_snapshot_candidates":len(shadow["target_snapshot_candidates"]),
    "pattern_routes":shadow["pattern_routes"],
  },
  "checks":{
    "fresh_backup_verified":True,
    "all_discovered_memory_v1_timer_states_restored_exactly":True,
    "restricted_loader_installed_and_replay_safe":True,
    "owner_isolation_verified_on_two_real_owners":True,
    "zero_write_shadow_report":True,
    "preexisting_memory_rows_unchanged":True,
    "qdrant_unchanged":True,
    "brains_health_and_readiness_passed":True,
    "external_model_calls":0,
    "local_model_calls":0,
    "pattern_heads_or_revisions_written":0,
    "retrieval_or_prompt_influence":False,
  },
  "hard_stop":"before_snapshot_or_pattern_review_persistence_pattern_apply_runtime_activation",
}
payload=json.dumps(value,indent=2,sort_keys=True)+"\n"
report.write_text(payload)
report.chmod(0o600)
Path(str(report)+".sha256").write_text(hashlib.sha256(payload.encode()).hexdigest()+"  "+str(report)+"\n")
Path(str(report)+".sha256").chmod(0o600)
PY

phase=complete
printf '%s\n' 'memory_v1_pattern_salience_shadow_v5_1_production_install: PASS'
printf 'report=%s\nshadow_report=%s\n' "$report" "$shadow_report"

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Materializes exactly four reviewed compiler-v8 claims
# and applies audited supported assessments. It never calls an external model,
# writes Qdrant, activates retrieval, or influences prompts.

if [[ "${MEMORY_V1_V5_2_COMPILER_V8_CLAIM_MATERIALIZATION_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_COMPILER_V8_CLAIM_MATERIALIZATION_APPLY=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 4 ]]; then
  echo 'usage: materialization_production_apply.sh MANIFEST PREFLIGHT APPLY REPLAY' >&2
  exit 2
fi
repo_root=$(git rev-parse --show-toplevel)
manifest=$(realpath "$1")
preflight_result=$(realpath -m "$2")
apply_result=$(realpath -m "$3")
replay_result=$(realpath -m "$4")
review_root=/home/ubuntu/memory-v1-reviews
target_owner=$(jq -er '.owner_user_id' "$manifest")
item_count=$(jq -er '.items|length' "$manifest")
expected_insert=$(jq -er '.expected_insert_rows' "$manifest")
expected_mutated=$(jq -er '.expected_mutated_rows' "$manifest")
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
apply_runner=scripts/memory_v1_v5_claim_projection_apply_batch.py
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_compiler_v8_claim_materialization.lock
expected_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
expected_observations="'a0ea633d-96df-4ad8-a0c1-b3f4f84e30cc','c8ce8cd0-e058-4181-ae94-fd6fb1e7c6eb','70d55f38-1e33-418f-8ec6-6bfd2051f4e6','bbd94cc7-e9d5-429f-8af1-1a029b119db0'"
phase=initialization
status_file=
run_tag=
units_quiesced=0
brains_quiesced=0
brains_state_before=
unit_state=$(mktemp /tmp/memory-v1-claim-apply-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-claim-apply-tables.XXXXXX)
psql_row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 -U sage -d "$database" -c "$1" | sed -n '1p'
}

restore_timers() {
  [[ "$units_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    if [[ "$active" == active ]]; then sudo -n systemctl start "$unit"; else sudo -n systemctl stop "$unit"; fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
}

restore_runtime() {
  if [[ "$brains_quiesced" -eq 1 ]]; then
    if [[ "$brains_state_before" == active ]]; then sudo -n systemctl start brains.service; else sudo -n systemctl stop brains.service; fi
    [[ "$(systemctl is-active brains.service)" == "$brains_state_before" ]]
    brains_quiesced=0
  fi
  restore_timers
}

record_exit() {
  code=$?
  if [[ "$brains_quiesced" -eq 1 || "$units_quiesced" -eq 1 ]]; then restore_runtime || code=1; fi
  rm -f "$unit_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    printf 'run_tag=%s\nphase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$run_tag" "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$code"
}
trap record_exit EXIT

capture_partition() {
  local partition=$1 output=$2 table has_owner predicate state
  : >"$output"
  while IFS=$'\t' read -r table has_owner; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    if [[ "$has_owner" == t ]]; then
      if [[ "$partition" == target ]]; then predicate="owner_user_id='$target_owner'::uuid"; else predicate="owner_user_id IS DISTINCT FROM '$target_owner'::uuid"; fi
    elif [[ "$partition" == target ]]; then continue; else predicate=true; fi
    state=$(psql_row "SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex') FROM (SELECT to_jsonb(value)::text AS row_json FROM memory.\"$table\" AS value WHERE $predicate) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

verify_target_delta() {
  BEFORE="$1" AFTER="$2" MANIFEST="$manifest" python3 - <<'PY'
import json,os
from pathlib import Path
def load(path):
    out={}
    for line in Path(path).read_text().splitlines():
        table,count,digest=line.split('\t'); out[table]=(int(count),digest)
    return out
before,after=load(os.environ['BEFORE']),load(os.environ['AFTER'])
expected=json.loads(Path(os.environ['MANIFEST']).read_text())['expected_table_rows']
if before.keys()!=after.keys(): raise SystemExit('target table set changed')
for table in before:
    wanted=expected.get(table,0); delta=after[table][0]-before[table][0]
    if delta!=wanted: raise SystemExit(f'unexpected target delta {table}: {delta} != {wanted}')
    if wanted==0 and before[table][1]!=after[table][1]: raise SystemExit(f'unexpected target mutation {table}')
PY
}

[[ "$manifest" == "$review_root"/* && -f "$manifest" && "$(stat -c '%a' "$manifest")" == 600 ]]
for output in "$preflight_result" "$apply_result" "$replay_result"; do [[ "$output" == "$review_root"/* && ! -e "$output" ]]; done
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
[[ "$(jq -er '.required_head_commit' "$manifest")" == "$head" ]]
[[ "$item_count" == 4 ]]
[[ "$target_owner" == "$expected_owner" ]]
[[ "$(jq -er '.defer_projection_outbox' "$manifest")" == true ]]
[[ "$(jq -r '[.items[].predicate]|sort|join(",")' "$manifest")" == "occupation.works_as,occupation.works_as,relationship.caregiver_for,relationship.spouse_of" ]]
[[ "$expected_insert" == 44 ]]
[[ "$expected_mutated" == 48 ]]
set -a; source /opt/chat-memory/.env; set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${QDRANT_URL:-}" ]]
exec 9>"$lock_file"; flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_compiler_v8_claim_materialization_${run_tag}.status"
target_before="$snapshot_dir/memory_v1_v5_2_claim_target_before_${run_tag}.tsv"
target_apply="$snapshot_dir/memory_v1_v5_2_claim_target_after_${run_tag}.tsv"
target_replay="$snapshot_dir/memory_v1_v5_2_claim_target_replay_${run_tag}.tsv"
non_target_before="$snapshot_dir/memory_v1_v5_2_claim_non_target_before_${run_tag}.tsv"
non_target_apply="$snapshot_dir/memory_v1_v5_2_claim_non_target_after_${run_tag}.tsv"
non_target_replay="$snapshot_dir/memory_v1_v5_2_claim_non_target_replay_${run_tag}.tsv"

phase=capture_timer_state
: >"$unit_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$unit_state" ]]
chmod 0600 "$unit_state"
phase=quiesce_timers
while IFS=$'\t' read -r unit _enabled active; do [[ "$active" != active ]] || sudo -n systemctl stop "$unit"; done <"$unit_state"
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do systemctl is-active --quiet "$service" || break; sleep 1; done
  ! systemctl is-active --quiet "$service"
done <"$unit_state"
phase=quiesce_brains
brains_state_before=$(systemctl is-active brains.service)
[[ "$brains_state_before" != active ]] || sudo -n systemctl stop brains.service
brains_quiesced=1
for _attempt in $(seq 1 30); do systemctl is-active --quiet brains.service || break; sleep 1; done
! systemctl is-active --quiet brains.service

phase=capture_baseline
docker exec "$container" psql -X -A -F $'\t' -t -v ON_ERROR_STOP=1 -U sage -d "$database" -c "SELECT table_name,EXISTS(SELECT 1 FROM information_schema.columns AS c WHERE c.table_schema='memory' AND c.table_name=t.table_name AND c.column_name='owner_user_id') FROM information_schema.tables AS t WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" >"$table_list"
capture_partition target "$target_before"; capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)
[[ "$(psql_row "SELECT count(*) FROM memory.claim_observation WHERE owner_user_id='$target_owner' AND observation_id IN ($expected_observations)")" == 0 ]]

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_2_compiler_v8_claim_materialization_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_compiler_v8_claim_materialization_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$backup_partial" "$backup"; chmod 0600 "$backup" "$backup.catalog"
sha256sum "$backup" >"$backup.sha256"; chmod 0600 "$backup.sha256"

phase=zero_write_preflight
MEMORY_V1_REQUIRED_HEAD="$head" PYTHONPATH="$repo_root/scripts:$repo_root" /opt/chat-memory/venv/bin/python "$repo_root/$apply_runner" --mode preflight --manifest "$manifest" --output "$preflight_result"
[[ "$(jq -er '.insert_rows' "$preflight_result")" == 0 ]]

phase=transactional_apply
MEMORY_V1_REQUIRED_HEAD="$head" MEMORY_V1_CLAIM_PROJECTION_APPLY_BATCH=authorized PYTHONPATH="$repo_root/scripts:$repo_root" /opt/chat-memory/venv/bin/python "$repo_root/$apply_runner" --mode apply --manifest "$manifest" --output "$apply_result"
[[ "$(jq -er '.insert_rows' "$apply_result")" == "$expected_insert" && "$(jq -er '.mutated_rows' "$apply_result")" == "$expected_mutated" ]]
claim_ids=$(jq -r '[.outcomes[].claim_id]|join(",")' "$apply_result")
[[ "$(psql_row "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner' AND status='supported' AND claim_id=ANY(string_to_array('$claim_ids',',')::uuid[])")" == 4 ]]
[[ "$(psql_row "SELECT count(*) FROM memory.claim_observation WHERE owner_user_id='$target_owner' AND observation_id IN ($expected_observations) AND claim_id=ANY(string_to_array('$claim_ids',',')::uuid[])")" == 4 ]]
[[ "$(psql_row "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner' AND claim_id=ANY(string_to_array('$claim_ids',',')::uuid[]) AND canonical_text IN ('The user is a caregiver for Monika.','The user is a spouse of Monika.','The user formerly worked as BCBA.','The user formerly worked as clinical psychologist.')")" == 4 ]]
[[ "$(psql_row "SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id='$target_owner' AND aggregate_id=ANY(string_to_array('$claim_ids',',')::uuid[])")" == 0 ]]
capture_partition target "$target_apply"; capture_partition non_target "$non_target_apply"
verify_target_delta "$target_before" "$target_apply"; cmp -s "$non_target_before" "$non_target_apply"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=zero_write_replay
MEMORY_V1_REQUIRED_HEAD="$head" PYTHONPATH="$repo_root/scripts:$repo_root" /opt/chat-memory/venv/bin/python "$repo_root/$apply_runner" --mode replay --manifest "$manifest" --apply-result "$apply_result" --output "$replay_result"
[[ "$(jq -er '.insert_rows' "$replay_result")" == 0 && "$(jq -er '.mutated_rows' "$replay_result")" == 0 ]]
capture_partition target "$target_replay"; capture_partition non_target "$non_target_replay"
cmp -s "$target_apply" "$target_replay"; cmp -s "$non_target_apply" "$non_target_replay"

[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=restore_runtime
restore_runtime
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
docker exec "$container" pg_isready -U sage -d "$database" >/dev/null
curl --fail --silent --show-error --max-time 30 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz >/dev/null
phase=report
report="$snapshot_dir/memory_v1_v5_2_compiler_v8_claim_materialization_${run_tag}.json"
REPORT="$report" BACKUP="$backup" MANIFEST="$manifest" PREFLIGHT="$preflight_result" APPLY="$apply_result" REPLAY="$replay_result" HEAD="$head" ITEM_COUNT="$item_count" EXPECTED_INSERT="$expected_insert" EXPECTED_MUTATED="$expected_mutated" QDRANT="$qdrant_before" python3 - <<'PY'
import datetime as dt,json,os
from pathlib import Path
m=json.loads(Path(os.environ['MANIFEST']).read_text())
report={
 'contract_version':'memory_v1_v5_2_compiler_v8_claim_materialization_report_v1',
 'completed_at':dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00','Z'),
 'head_commit':os.environ['HEAD'],'owner_user_id':m['owner_user_id'],'manifest_sha256':m['manifest_sha256'],
 'backup':os.environ['BACKUP'],
 'evidence':{'preflight':os.environ['PREFLIGHT'],'apply':os.environ['APPLY'],'replay':os.environ['REPLAY']},
 'verification':{'insert_rows':int(os.environ['EXPECTED_INSERT']),'mutated_rows':int(os.environ['EXPECTED_MUTATED']),
   'claims_supported':int(os.environ['ITEM_COUNT']),'zero_write_replay':True,
   'embedding_requests':0,'qdrant_points_created':0,'qdrant_sha256':os.environ['QDRANT'],
   'projection_outbox_rows_created':0,'retrieval_activated':False,
   'non_target_database_unchanged':True,'account_isolation_verified':True,
   'qdrant_unchanged':True,'prompt_influence':False,
   'general_account_activation':False,'all_memory_timers_restored_exactly':True,
   'brains_service_restored_exactly':True,'brains_health_authenticated':True},
 'hard_stop':'before_qdrant_projection_retrieval_prompt_influence_or_general_account_activation'}
path=Path(os.environ['REPORT']); path.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n'); path.chmod(0o600)
PY
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'backup=%s\nreport=%s\n' "$backup" "$report"
printf 'memory_v1_v5_2_compiler_v8_claim_materialization_production: PASS\n'

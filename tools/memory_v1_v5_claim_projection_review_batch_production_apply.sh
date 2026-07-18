#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Records four hash-locked, owner-scoped projection review
# decisions. It cannot call apply_projection_v5 or materialize claims.

if [[ "${MEMORY_V1_CLAIM_PROJECTION_REVIEW_BATCH_PRODUCTION_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_CLAIM_PROJECTION_REVIEW_BATCH_PRODUCTION_APPLY=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 4 ]]; then
  echo 'usage: review_batch_production_apply.sh MANIFEST PREFLIGHT APPLY REPLAY' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
manifest=$(realpath "$1")
preflight_result=$(realpath -m "$2")
apply_result=$(realpath -m "$3")
replay_result=$(realpath -m "$4")
review_root=/home/ubuntu/memory-v1-reviews
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
runner=scripts/memory_v1_v5_claim_projection_review_batch.py
lock_file=/home/ubuntu/brains/.memory_v1_claim_projection_review_batch_apply.lock
phase=initialization
status_file=
run_tag=
units_quiesced=0
brains_quiesced=0
brains_state_before=
unit_state=$(mktemp /tmp/memory-v1-claim-projection-review-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-claim-projection-review-tables.XXXXXX)
timers=(
  memory-v1-consolidation.timer
  memory-v1-deferred-reconciliation-scan.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-governance.timer
  memory-v1-projection.timer
  memory-v1-v5-chat-capture.timer
)

psql_row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
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

verify_target_delta() {
  BEFORE="$1" AFTER="$2" python3 - <<'PY'
import os
from pathlib import Path
def load(path):
    result={}
    for line in Path(path).read_text().splitlines():
        table,count,digest=line.split("\t")
        result[table]=(int(count),digest)
    return result
before,after=load(os.environ['BEFORE']),load(os.environ['AFTER'])
if before.keys()!=after.keys(): raise SystemExit('target-owner table set changed')
for table in before:
    wanted=4 if table=='projection_review' else 0
    delta=after[table][0]-before[table][0]
    if delta!=wanted: raise SystemExit(f'unexpected target delta {table}: {delta} != {wanted}')
    if wanted==0 and before[table][1]!=after[table][1]: raise SystemExit(f'unexpected target mutation {table}')
PY
}

[[ "$manifest" == "$review_root"/* && -f "$manifest" && "$(stat -c '%a' "$manifest")" == 600 ]]
for output in "$preflight_result" "$apply_result" "$replay_result"; do [[ "$output" == "$review_root"/* && ! -e "$output" ]]; done
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
[[ "$(jq -er '.contract_version' "$manifest")" == memory_v1_claim_projection_review_batch_manifest_v1 ]]
[[ "$(jq -er '.owner_user_id' "$manifest")" == "$target_owner" ]]
[[ "$(jq -er '.required_head_commit' "$manifest")" == "$head" ]]
[[ "$(jq -er '.expected_new_rows' "$manifest")" == 4 ]]
[[ "$(jq -er '.items|length' "$manifest")" == 4 ]]

set -a; source "$repo_root/.env"; set +a
[[ -n "${POSTGRES_DSN:-}" ]]
exec 9>"$lock_file"; flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_claim_projection_review_batch_${run_tag}.status"
target_before="$snapshot_dir/memory_v1_claim_projection_review_target_before_${run_tag}.tsv"
target_preflight="$snapshot_dir/memory_v1_claim_projection_review_target_preflight_${run_tag}.tsv"
target_after="$snapshot_dir/memory_v1_claim_projection_review_target_after_${run_tag}.tsv"
target_replay="$snapshot_dir/memory_v1_claim_projection_review_target_replay_${run_tag}.tsv"
non_target_before="$snapshot_dir/memory_v1_claim_projection_review_non_target_before_${run_tag}.tsv"
non_target_preflight="$snapshot_dir/memory_v1_claim_projection_review_non_target_preflight_${run_tag}.tsv"
non_target_after="$snapshot_dir/memory_v1_claim_projection_review_non_target_after_${run_tag}.tsv"
non_target_replay="$snapshot_dir/memory_v1_claim_projection_review_non_target_replay_${run_tag}.tsv"

phase=capture_timer_state
: >"$unit_state"
for unit in "${timers[@]}"; do printf '%s\t%s\t%s\n' "$unit" "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" >>"$unit_state"; done
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

phase=backup
backup_partial="$snapshot_dir/.memory_pre_claim_projection_review_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_claim_projection_review_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$backup_partial" "$backup"; chmod 0600 "$backup" "$backup.catalog"
sha256sum "$backup" >"$backup.sha256"; chmod 0600 "$backup.sha256"

phase=zero_write_preflight
MEMORY_V1_REQUIRED_HEAD="$head" PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python "$repo_root/$runner" --mode preflight --manifest "$manifest" --output "$preflight_result"
[[ "$(jq -er '.rows_written' "$preflight_result")" == 0 ]]
capture_partition target "$target_preflight"; capture_partition non_target "$non_target_preflight"
cmp -s "$target_before" "$target_preflight"; cmp -s "$non_target_before" "$non_target_preflight"

phase=transactional_review
MEMORY_V1_REQUIRED_HEAD="$head" MEMORY_V1_CLAIM_PROJECTION_REVIEW_BATCH_APPLY=authorized PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python "$repo_root/$runner" --mode apply --manifest "$manifest" --output "$apply_result"
[[ "$(jq -er '.rows_written' "$apply_result")" == 4 ]]
capture_partition target "$target_after"; capture_partition non_target "$non_target_after"
verify_target_delta "$target_before" "$target_after"; cmp -s "$non_target_before" "$non_target_after"
qdrant_after=$(qdrant_signature); [[ "$qdrant_after" == "$qdrant_before" ]]

phase=zero_write_replay
MEMORY_V1_REQUIRED_HEAD="$head" PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python "$repo_root/$runner" --mode replay --manifest "$manifest" --output "$replay_result"
[[ "$(jq -er '.rows_written' "$replay_result")" == 0 ]]
capture_partition target "$target_replay"; capture_partition non_target "$non_target_replay"
cmp -s "$target_after" "$target_replay"; cmp -s "$non_target_after" "$non_target_replay"
qdrant_replay=$(qdrant_signature); [[ "$qdrant_replay" == "$qdrant_before" ]]

phase=restore_runtime
restore_runtime
phase=report
report="$snapshot_dir/memory_v1_claim_projection_review_batch_${run_tag}.json"
REPORT="$report" BACKUP="$backup" MANIFEST="$manifest" PREFLIGHT="$preflight_result" APPLY="$apply_result" REPLAY="$replay_result" QDRANT="$qdrant_before" HEAD="$head" python3 - <<'PY'
import datetime as dt,json,os
from pathlib import Path
manifest=json.loads(Path(os.environ['MANIFEST']).read_text())
report={
 'contract_version':'memory_v1_claim_projection_review_batch_report_v1',
 'completed_at':dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00','Z'),
 'head_commit':os.environ['HEAD'],'owner_user_id':manifest['owner_user_id'],
 'manifest_sha256':manifest['manifest_sha256'],'backup':os.environ['BACKUP'],
 'evidence':{'preflight_result':os.environ['PREFLIGHT'],'apply_result':os.environ['APPLY'],'replay_result':os.environ['REPLAY']},
 'verification':{'review_rows_written':4,'zero_write_replay':True,'non_target_memory_unchanged':True,
   'claims_written':0,'projection_apply_events_written':0,'qdrant_sha256':os.environ['QDRANT'],'qdrant_unchanged':True,
   'retrieval_activated':False,'prompt_influence_activated':False,'timers_restored_exactly':True,'brains_service_restored_exactly':True},
 'hard_stop':'before_projection_apply_claim_materialization_or_retrieval_activation'}
path=Path(os.environ['REPORT']); path.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n'); path.chmod(0o600)
PY
phase=complete
printf 'backup=%s\nreport=%s\n' "$backup" "$report"
printf 'memory_v1_v5_claim_projection_review_batch_production_apply: PASS\n'

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Materializes one through four reviewed V5/V5.1 claims,
# applies audited supported assessments, projects exactly one Qdrant point per
# claim with zero HTTP retries, and performs owner-only read-only shadow tests.
# Prompt influence is never enabled.

if [[ "${MEMORY_V1_CLAIM_PROJECTION_PRODUCTION_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_CLAIM_PROJECTION_PRODUCTION_APPLY=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 5 ]]; then
  echo 'usage: production_apply.sh MANIFEST PREFLIGHT APPLY REPLAY PROJECT' >&2
  exit 2
fi
repo_root=$(git rev-parse --show-toplevel)
manifest=$(realpath "$1")
preflight_result=$(realpath -m "$2")
apply_result=$(realpath -m "$3")
replay_result=$(realpath -m "$4")
project_result=$(realpath -m "$5")
review_root=/home/ubuntu/memory-v1-reviews
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
apply_runner=scripts/memory_v1_v5_claim_projection_apply_batch.py
project_runner=scripts/memory_v1_v5_claim_projection_controlled_project.py
lock_file=/home/ubuntu/brains/.memory_v1_claim_projection_apply_batch.lock
phase=initialization
status_file=
run_tag=
units_quiesced=0
brains_quiesced=0
brains_state_before=
unit_state=$(mktemp /tmp/memory-v1-claim-apply-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-claim-apply-tables.XXXXXX)
item_count=$(jq -er '.items|length' "$manifest")

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

authenticated_health() {
  set -a
  source "$repo_root/.env"
  set +a
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

capture_qdrant() {
  local target=$1 non_target=$2 response
  response=$(mktemp /tmp/memory-v1-qdrant-scroll.XXXXXX)
  curl --fail --silent --show-error --max-time 30 -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll >"$response"
  jq -cS --arg owner "$target_owner" '.result.points|sort_by(.id|tostring)|.[]|select(.payload.owner_user_id==$owner)' "$response" >"$target"
  jq -cS --arg owner "$target_owner" '.result.points|sort_by(.id|tostring)|.[]|select(.payload.owner_user_id!=$owner)' "$response" >"$non_target"
  chmod 0600 "$target" "$non_target"
  rm -f "$response"
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

verify_projection_only_delta() {
  BEFORE="$1" AFTER="$2" python3 - <<'PY'
import os
from pathlib import Path
def load(path):
    out={}
    for line in Path(path).read_text().splitlines():
        table,count,digest=line.split('\t'); out[table]=(int(count),digest)
    return out
before,after=load(os.environ['BEFORE']),load(os.environ['AFTER'])
if before.keys()!=after.keys(): raise SystemExit('target table set changed after projection')
for table in before:
    if before[table][0]!=after[table][0]: raise SystemExit(f'row count changed during projection: {table}')
    if table!='projection_outbox' and before[table][1]!=after[table][1]:
        raise SystemExit(f'unexpected database mutation during projection: {table}')
if before['projection_outbox'][1]==after['projection_outbox'][1]:
    raise SystemExit('projection outbox did not advance to done')
PY
}

verify_qdrant_delta() {
  BEFORE="$1" AFTER="$2" PROJECT="$project_result" \
    ITEM_COUNT="$item_count" TARGET_OWNER="$target_owner" python3 - <<'PY'
import json,os
from pathlib import Path
def load(path):
    out={}
    for line in Path(path).read_text().splitlines():
        value=json.loads(line); out[str(value['id'])]=value
    return out
before,after=load(os.environ['BEFORE']),load(os.environ['AFTER'])
project=json.loads(Path(os.environ['PROJECT']).read_text())
ids={row['claim_id'] for row in project['completed']}
if len(ids)!=int(os.environ['ITEM_COUNT']) or ids & before.keys():
    raise SystemExit('target Qdrant point count or identity mismatch')
if after.keys()!=before.keys()|ids: raise SystemExit('unexpected target Qdrant point set delta')
for point_id,value in before.items():
    if after[point_id]!=value: raise SystemExit('existing target Qdrant point changed')
for point_id in ids:
    point=after[point_id]
    payload=point.get('payload',{})
    if payload.get('owner_user_id')!=os.environ['TARGET_OWNER']: raise SystemExit('Qdrant owner mismatch')
    if payload.get('claim_id')!=point_id or payload.get('status')!='supported': raise SystemExit('Qdrant claim payload mismatch')
    if payload.get('revision_number')!=2 or payload.get('schema_version')!='memory_claim_projection_v1': raise SystemExit('Qdrant projection contract mismatch')
    if not isinstance(point.get('vector'),list) or len(point['vector'])!=3072: raise SystemExit('Qdrant vector dimension mismatch')
PY
}

[[ "$manifest" == "$review_root"/* && -f "$manifest" && "$(stat -c '%a' "$manifest")" == 600 ]]
for output in "$preflight_result" "$apply_result" "$replay_result" "$project_result"; do [[ "$output" == "$review_root"/* && ! -e "$output" ]]; done
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
[[ "$(jq -er '.required_head_commit' "$manifest")" == "$head" ]]
[[ "$(jq -er '.owner_user_id' "$manifest")" == "$target_owner" ]]
[[ "$item_count" -ge 1 && "$item_count" -le 4 ]]
[[ "$(jq -er '.expected_insert_rows' "$manifest")" == "$((12*item_count))" ]]
[[ "$(jq -er '.expected_mutated_rows' "$manifest")" == "$((13*item_count))" ]]
set -a; source "$repo_root/.env"; set +a
[[ -n "${POSTGRES_DSN:-}" && -n "${QDRANT_URL:-}" && -n "${OPENAI_API_KEY:-}" ]]
[[ "${EMBED_MODEL:-text-embedding-3-large}" == text-embedding-3-large ]]
authenticated_health
exec 9>"$lock_file"; flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_claim_projection_apply_batch_${run_tag}.status"
target_before="$snapshot_dir/memory_v1_claim_apply_target_before_${run_tag}.tsv"
target_apply="$snapshot_dir/memory_v1_claim_apply_target_after_${run_tag}.tsv"
target_replay="$snapshot_dir/memory_v1_claim_apply_target_replay_${run_tag}.tsv"
target_final="$snapshot_dir/memory_v1_claim_apply_target_final_${run_tag}.tsv"
non_target_before="$snapshot_dir/memory_v1_claim_apply_non_target_before_${run_tag}.tsv"
non_target_apply="$snapshot_dir/memory_v1_claim_apply_non_target_after_${run_tag}.tsv"
non_target_replay="$snapshot_dir/memory_v1_claim_apply_non_target_replay_${run_tag}.tsv"
non_target_final="$snapshot_dir/memory_v1_claim_apply_non_target_final_${run_tag}.tsv"
q_target_before="$snapshot_dir/memory_v1_claim_qdrant_target_before_${run_tag}.jsonl"
q_other_before="$snapshot_dir/memory_v1_claim_qdrant_other_before_${run_tag}.jsonl"
q_target_after="$snapshot_dir/memory_v1_claim_qdrant_target_after_${run_tag}.jsonl"
q_other_after="$snapshot_dir/memory_v1_claim_qdrant_other_after_${run_tag}.jsonl"

phase=capture_timer_state
: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
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
capture_qdrant "$q_target_before" "$q_other_before"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_claim_projection_apply_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_claim_projection_apply_${run_tag}.dump"
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
[[ "$(jq -er '.insert_rows' "$apply_result")" == "$((12*item_count))" \
   && "$(jq -er '.mutated_rows' "$apply_result")" == "$((13*item_count))" ]]
capture_partition target "$target_apply"; capture_partition non_target "$non_target_apply"
verify_target_delta "$target_before" "$target_apply"; cmp -s "$non_target_before" "$non_target_apply"
q_apply_target=$(mktemp /tmp/memory-v1-q-target-apply.XXXXXX); q_apply_other=$(mktemp /tmp/memory-v1-q-other-apply.XXXXXX)
capture_qdrant "$q_apply_target" "$q_apply_other"
cmp -s "$q_target_before" "$q_apply_target"; cmp -s "$q_other_before" "$q_apply_other"
rm -f "$q_apply_target" "$q_apply_other"

phase=zero_write_replay
MEMORY_V1_REQUIRED_HEAD="$head" PYTHONPATH="$repo_root/scripts:$repo_root" /opt/chat-memory/venv/bin/python "$repo_root/$apply_runner" --mode replay --manifest "$manifest" --apply-result "$apply_result" --output "$replay_result"
[[ "$(jq -er '.insert_rows' "$replay_result")" == 0 && "$(jq -er '.mutated_rows' "$replay_result")" == 0 ]]
capture_partition target "$target_replay"; capture_partition non_target "$non_target_replay"
cmp -s "$target_apply" "$target_replay"; cmp -s "$non_target_apply" "$non_target_replay"

phase=controlled_projection_and_shadow
MEMORY_V1_CONTROLLED_PROJECTION=authorized PYTHONPATH="$repo_root/scripts:$repo_root" /opt/chat-memory/venv/bin/python "$repo_root/$project_runner" --apply-result "$apply_result" --output "$project_result"
[[ "$(jq -er '.embedding_requests' "$project_result")" == "$item_count" ]]
[[ "$(jq -er '.automatic_http_retries' "$project_result")" == 0 ]]
[[ "$(jq -er '.qdrant_writes' "$project_result")" == "$item_count" ]]
[[ "$(jq -er '.shadow_tests|length' "$project_result")" == "$item_count" ]]
capture_partition target "$target_final"; capture_partition non_target "$non_target_final"
verify_projection_only_delta "$target_apply" "$target_final"
cmp -s "$non_target_apply" "$non_target_final"
capture_qdrant "$q_target_after" "$q_other_after"
cmp -s "$q_other_before" "$q_other_after"
verify_qdrant_delta "$q_target_before" "$q_target_after"
claim_ids=$(jq -r '[.outcomes[].claim_id]|join(",")' "$apply_result")
[[ "$(psql_row "SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id='$target_owner'::uuid AND status='done' AND attempts=1 AND aggregate_id=ANY(string_to_array('$claim_ids',',')::uuid[])")" == "$item_count" ]]

phase=restore_runtime
restore_runtime
authenticated_health
phase=report
report="$snapshot_dir/memory_v1_claim_projection_apply_batch_${run_tag}.json"
REPORT="$report" BACKUP="$backup" MANIFEST="$manifest" PREFLIGHT="$preflight_result" APPLY="$apply_result" REPLAY="$replay_result" PROJECT="$project_result" HEAD="$head" ITEM_COUNT="$item_count" python3 - <<'PY'
import datetime as dt,json,os
from pathlib import Path
m=json.loads(Path(os.environ['MANIFEST']).read_text())
report={
 'contract_version':'memory_v1_claim_projection_apply_batch_report_v1',
 'completed_at':dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00','Z'),
 'head_commit':os.environ['HEAD'],'owner_user_id':m['owner_user_id'],'manifest_sha256':m['manifest_sha256'],
 'backup':os.environ['BACKUP'],
 'evidence':{'preflight':os.environ['PREFLIGHT'],'apply':os.environ['APPLY'],'replay':os.environ['REPLAY'],'projection':os.environ['PROJECT']},
 'verification':{'insert_rows':12*int(os.environ['ITEM_COUNT']),'mutated_rows':13*int(os.environ['ITEM_COUNT']),
   'claims_supported':int(os.environ['ITEM_COUNT']),'zero_write_replay':True,
   'embedding_requests':int(os.environ['ITEM_COUNT']),'automatic_http_retries':0,
   'qdrant_points_created':int(os.environ['ITEM_COUNT']),'shadow_tests_passed':int(os.environ['ITEM_COUNT']),
   'non_target_database_unchanged':True,'non_target_qdrant_unchanged':True,'prompt_influence':False,
   'general_account_activation':False,'timers_restored_exactly':True,'brains_service_restored_exactly':True},
 'hard_stop':'before_prompt_influence_or_general_account_activation'}
path=Path(os.environ['REPORT']); path.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n'); path.chmod(0o600)
PY
phase=complete
printf 'backup=%s\nreport=%s\n' "$backup" "$report"
printf 'memory_v1_v5_claim_projection_apply_batch_production_apply: PASS\n'

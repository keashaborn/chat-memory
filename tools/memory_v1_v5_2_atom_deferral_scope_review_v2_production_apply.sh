#!/usr/bin/env bash
set -euo pipefail

if [[ "${MEMORY_V1_V5_2_ATOM_SCOPE_V2_PRODUCTION_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_ATOM_SCOPE_V2_PRODUCTION_APPLY=authorized is required' >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo 'production atom-scope apply requires root' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
export GIT_OPTIONAL_LOCKS=0
set -a
source "${MEMORY_V1_ENV_FILE:-/opt/chat-memory/.env}"
set +a

container=brains-postgres-1
database=memory
python_bin=/opt/chat-memory/venv/bin/python
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
profession_packet=6ae4a8e6-b207-5201-a997-53fb2363fc9d
caregiving_packet=b76915b8-0603-50e8-b263-761da39f5651
migration=ops/sql/20260725_memory_v1_v5_2_atom_deferral_scope_review_v2.sql
test_sql=tests/memory_v1_v5_2_atom_deferral_scope_review_v2.sql
review_root=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_atom_scope_v2.lock
unit_state=$(mktemp /tmp/memory-v5-2-atom-scope-v2-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v5-2-atom-scope-v2-tables.XXXXXX)
phase=initialization
units_quiesced=0
run_tag=
status_file=

psql_scalar() {
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "$1"
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
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
}

record_exit() {
  exit_code=$?
  if [[ "$exit_code" -eq 0 && "$phase" != complete ]]; then
    exit_code=1
  fi
  restore_timers || exit_code=1
  rm -f "$unit_state" "$table_list"
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

capture_partition() {
  local partition=$1 output=$2 table has_owner predicate state
  : >"$output"
  while IFS=$'\t' read -r table has_owner; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    if [[ "$has_owner" == t ]]; then
      if [[ "$partition" == target ]]; then
        predicate="owner_user_id='$owner'::uuid"
      else
        predicate="owner_user_id<>'$owner'::uuid"
      fi
    elif [[ "$partition" == target ]]; then
      continue
    else
      predicate=true
    fi
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
        WHERE $predicate
      ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

verify_target_delta() {
  BEFORE="$target_before" AFTER="$target_after" "$python_bin" - <<'PY'
import os
from pathlib import Path

def load(path):
    rows={}
    for line in Path(path).read_text().splitlines():
        table,count,digest=line.split("\t")
        rows[table]=(int(count),digest)
    return rows

before=load(os.environ["BEFORE"])
after=load(os.environ["AFTER"])
assert before.keys() == after.keys()
expected={
    "v5_2_atom_admission_proposal":2,
    "v5_2_atom_admission_review":2,
    "v5_2_atom_admission_apply":2,
    "v5_2_atom_admission_operation":6,
}
for table in before:
    delta=after[table][0]-before[table][0]
    wanted=expected.get(table,0)
    if delta != wanted:
        raise SystemExit(f"unexpected target-owner delta {table}: {delta} != {wanted}")
    if wanted == 0 and before[table][1] != after[table][1]:
        raise SystemExit(f"unexpected target-owner mutation {table}")
PY
}

[[ -z "$(git status --porcelain)" ]]
head=$(git rev-parse HEAD)
[[ -n "${MEMORY_V1_REQUIRED_HEAD:-}" ]]
[[ "$head" == "$MEMORY_V1_REQUIRED_HEAD" ]]
[[ -f "$migration" && -f "$test_sql" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
mkdir -p "$review_root" "$snapshot_root"
chmod 0700 "$review_root" "$snapshot_root"
exec 9>"$lock_file"
flock -n 9
umask 077

run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_root/memory_v1_v5_2_atom_scope_v2_${run_tag}.status"
manifest="$review_root/v5-2-atom-scope-v2-${run_tag}-manifest.json"
preflight="$review_root/v5-2-atom-scope-v2-${run_tag}-preflight.json"
applied="$review_root/v5-2-atom-scope-v2-${run_tag}-applied.json"
replayed="$review_root/v5-2-atom-scope-v2-${run_tag}-replayed.json"
target_before="$snapshot_root/memory_v1_v5_2_atom_scope_target_before_${run_tag}.tsv"
target_after="$snapshot_root/memory_v1_v5_2_atom_scope_target_after_${run_tag}.tsv"
non_target_before="$snapshot_root/memory_v1_v5_2_atom_scope_non_target_before_${run_tag}.tsv"
non_target_after="$snapshot_root/memory_v1_v5_2_atom_scope_non_target_after_${run_tag}.tsv"

phase=quiesce
: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$unit_state" ]]
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$unit_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$unit_state"

phase=backup
backup_partial="$snapshot_root/.memory_pre_atom_scope_v2_${run_tag}.dump.partial"
backup="$snapshot_root/memory_pre_atom_scope_v2_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
[[ "$(psql_scalar "
  SELECT count(*) FROM memory.v5_2_atom_admission_proposal
  WHERE owner_user_id='$owner'::uuid
    AND packet_id IN (
      '$profession_packet'::uuid,'$caregiving_packet'::uuid
    )
    AND policy_version='memory_v1_v5_2_atom_admission_policy_v2'")" == 0 ]]
docker exec "$container" psql -U sage -d "$database" -X -AtF $'\t' -c "
  SELECT table_name,EXISTS (
    SELECT 1 FROM information_schema.columns AS column_info
    WHERE column_info.table_schema='memory'
      AND column_info.table_name=tables.table_name
      AND column_info.column_name='owner_user_id'
  )
  FROM information_schema.tables AS tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name" >"$table_list"
[[ -s "$table_list" ]]
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)
v1_planner_before=$(psql_scalar "
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.plan_owner_v5_2_atom_admission_v1(uuid)'::regprocedure
  ),'UTF8'),'sha256'),'hex')")

phase=install
docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
[[ "$(psql_scalar "
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.plan_owner_v5_2_atom_admission_v1(uuid)'::regprocedure
  ),'UTF8'),'sha256'),'hex')")" == "$v1_planner_before" ]]
docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v other_owner="$other" \
  -v profession_packet="$profession_packet" \
  -v caregiving_packet="$caregiving_packet" \
  <"$test_sql" >/dev/null

phase=apply
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  "$python_bin" scripts/memory_v1_v5_2_atom_admission_manifest_v2.py \
  --output "$manifest" >/dev/null
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  "$python_bin" scripts/memory_v1_v5_2_atom_admission_apply_v2.py \
  --mode preflight --manifest "$manifest" --output "$preflight" >/dev/null
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY_V2=authorized \
  "$python_bin" scripts/memory_v1_v5_2_atom_admission_apply_v2.py \
  --mode apply --manifest "$manifest" --output "$applied" >/dev/null
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  "$python_bin" scripts/memory_v1_v5_2_atom_admission_apply_v2.py \
  --mode replay --manifest "$manifest" --output "$replayed" >/dev/null

MANIFEST="$manifest" PREFLIGHT="$preflight" APPLIED="$applied" \
REPLAYED="$replayed" "$python_bin" - <<'PY'
import json
import os
from pathlib import Path

manifest=json.loads(Path(os.environ["MANIFEST"]).read_text())
preflight=json.loads(Path(os.environ["PREFLIGHT"]).read_text())
applied=json.loads(Path(os.environ["APPLIED"]).read_text())
replayed=json.loads(Path(os.environ["REPLAYED"]).read_text())
assert len(manifest["items"]) == 2
assert preflight["persistent_writes"] == 0
assert applied["persistent_writes"] == 12
assert replayed["persistent_writes"] == 0
assert all(item["proposal_outcome"] == "applied" for item in applied["results"])
assert all(item["review_outcome"] == "applied" for item in applied["results"])
assert all(item["apply_outcome"] == "applied" for item in applied["results"])
assert all(item["proposal_outcome"] == "replayed" for item in replayed["results"])
assert all(item["review_outcome"] == "replayed" for item in replayed["results"])
assert all(item["apply_outcome"] == "replayed" for item in replayed["results"])
PY

phase=postflight
capture_partition target "$target_after"
capture_partition non_target "$non_target_after"
cmp -s "$non_target_before" "$non_target_after"
verify_target_delta
[[ "$(psql_scalar "
  SELECT count(*) FROM memory.v5_2_atom_admission_proposal
  WHERE owner_user_id='$owner'::uuid
    AND policy_version='memory_v1_v5_2_atom_admission_policy_v2'
    AND packet_id IN (
      '$profession_packet'::uuid,'$caregiving_packet'::uuid
    )
    AND admitted_observation_count=2
    AND jsonb_array_length(
      proposal#>'{stage_projection,observations}'
    )=2
    AND jsonb_array_length(
      proposal#>'{stage_projection,deferrals}'
    )=0")" == 2 ]]
[[ "$(psql_scalar "
  SELECT count(*) FROM memory.v5_2_atom_admission_review AS review
  JOIN memory.v5_2_atom_admission_proposal AS proposal
    USING(owner_user_id,proposal_id)
  WHERE review.owner_user_id='$owner'::uuid
    AND proposal.policy_version='memory_v1_v5_2_atom_admission_policy_v2'
    AND review.decision='authorized'
    AND review.reviewer_type='user'
    AND review.reviewer_ref='$owner'")" == 2 ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

restore_timers

phase=report
report="$snapshot_root/memory_v1_v5_2_atom_scope_v2_${run_tag}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$head" \
  --arg owner_user_id_sha256 "$(printf %s "$owner" | sha256sum | awk '{print $1}')" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg manifest "$manifest" \
  --arg manifest_sha256 "$(jq -er '.manifest_sha256' "$manifest")" \
  --arg preflight_sha256 "$(sha256sum "$preflight" | awk '{print $1}')" \
  --arg applied_sha256 "$(sha256sum "$applied" | awk '{print $1}')" \
  --arg replayed_sha256 "$(sha256sum "$replayed" | awk '{print $1}')" \
  --arg qdrant_sha256 "$qdrant_before" \
  '{
    contract_version:"memory_v1_v5_2_atom_scope_v2_production_report_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    owner_user_id_sha256:$owner_user_id_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    reviewed_manifest:{path:$manifest,sha256:$manifest_sha256},
    result_hashes:{
      preflight:$preflight_sha256,
      applied:$applied_sha256,
      replayed:$replayed_sha256
    },
    exact_new_rows:{
      proposals:2,reviews:2,applies:2,operations:6,
      relational_stage:0,entity_mentions:0,observations:0,
      claims:0,qdrant:0,prompt_influence:0
    },
    account_isolation:true,
    qdrant_sha256:$qdrant_sha256,
    timers_restored:true,
    service_health:"active",
    stopped_before:[
      "relational_staging","entity_resolution","claims",
      "qdrant_projection","retrieval","prompt_influence"
    ]
  }' >"$report"
chmod 0600 "$report"
chown ubuntu:ubuntu "$manifest" "$preflight" "$applied" "$replayed" "$report"

phase=complete
printf 'REPORT=%s\n' "$report"
printf 'MANIFEST_SHA256=%s\n' "$(jq -er '.manifest_sha256' "$manifest")"
printf '%s\n' \
  'memory_v1_v5_2_atom_deferral_scope_review_v2_production_apply: PASS'

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies one exact reviewed Neko correction entity link.
# It creates no entity, claim, projection, Qdrant vector, retrieval behavior,
# or prompt influence.

if [[ "${MEMORY_V1_V5_2_NEKO_CORRECTION_ENTITY_PRODUCTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_NEKO_CORRECTION_ENTITY_PRODUCTION=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=599c55b87c446344a6cd6317b2be75a4637f271f
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
resolution=e4c13bee-895d-47df-aeec-3c540e6b889d
observation=5261da41-f863-42cd-8e3f-6e947f9743f2
entity=09308a2b-3019-4f59-8fc3-bb1fe1408a0d
runner=scripts/memory_v1_v5_2_entity_resolution_batch.py
fixture=tests/memory_v1_v5_2_entity_resolution_batch_fixture.py
unit_test=tests/test_memory_v1_v5_2_entity_resolution_batch.py
manifest_source=manifests/memory_v1_v5_2_neko_correction_entity_apply_20260725.json
python_bin=/opt/chat-memory/venv/bin/python
review_root=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_neko_correction_entity.lock
phase=initialization
timers_quiesced=0
brains_quiesced=0
brains_state_before=
artifact_dir=
status_file=
run_id=
timer_state=$(mktemp /tmp/memory-v1-v5-2-neko-entity-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-2-neko-entity-tables.XXXXXX)

declare -A expected_delta=(
  [relational_operation_request]=2
  [entity_alias_observation]=1
  [entity_resolution_review]=1
  [entity_resolution_apply]=1
  [observation_entity_binding]=1
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

authenticated_health() {
  set -a
  source /opt/chat-memory/.env
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

restore_runtime() {
  if [[ "$brains_quiesced" -eq 1 ]]; then
    if [[ "$brains_state_before" == active ]]; then
      sudo -n systemctl start brains.service
    else
      sudo -n systemctl stop brains.service
    fi
    [[ "$(systemctl is-active brains.service)" == "$brains_state_before" ]]
    brains_quiesced=0
  fi
  if [[ "$timers_quiesced" -eq 1 ]]; then
    while IFS=$'\t' read -r unit enabled active; do
      [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
      if [[ "$active" == active ]]; then
        sudo -n systemctl start "$unit"
      else
        sudo -n systemctl stop "$unit"
      fi
      [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
      [[ "$(systemctl is-active "$unit")" == "$active" ]]
    done <"$timer_state"
    timers_quiesced=0
  fi
}

record_exit() {
  code=$?
  if [[ "$code" -eq 0 && "$phase" != complete ]]; then
    code=1
  fi
  restore_runtime || code=1
  rm -f "$timer_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_id=%s\n' "$run_id"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
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
      if [[ "$partition" == target ]]; then
        predicate="owner_user_id='$owner'::uuid"
      else
        predicate="owner_user_id IS DISTINCT FROM '$owner'::uuid"
      fi
    elif [[ "$partition" == target ]]; then
      continue
    else
      predicate=true
    fi
    state=$(psql_row "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
        WHERE $predicate
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

verify_target_delta() {
  local before=$1 after=$2 expected_file="$artifact_dir/expected-deltas.tsv"
  : >"$expected_file"
  for table in "${!expected_delta[@]}"; do
    printf '%s\t%s\n' "$table" "${expected_delta[$table]}" >>"$expected_file"
  done
  sort -o "$expected_file" "$expected_file"
  BEFORE="$before" AFTER="$after" EXPECTED="$expected_file" python3 - <<'PY'
import os
from pathlib import Path


def states(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        table, count, digest = line.split("\t")
        result[table] = (int(count), digest)
    return result


before = states(os.environ["BEFORE"])
after = states(os.environ["AFTER"])
expected = {}
for line in Path(os.environ["EXPECTED"]).read_text().splitlines():
    table, count = line.split("\t")
    expected[table] = int(count)
if before.keys() != after.keys():
    raise SystemExit("target-owner table set changed")
for table, old_state in before.items():
    wanted = expected.get(table, 0)
    actual = after[table][0] - old_state[0]
    if actual != wanted:
        raise SystemExit(f"unexpected target delta {table}: {actual} != {wanted}")
    if wanted == 0 and after[table][1] != old_state[1]:
        raise SystemExit(f"unexpected target mutation {table}")
PY
  chmod 0600 "$expected_file"
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
head=$(git -C "$repo_root" rev-parse HEAD)
[[ -x "$python_bin" ]]
for path in "$runner" "$fixture" "$unit_test" "$manifest_source"; do
  [[ -f "$repo_root/$path" ]]
done

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
authenticated_health

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_${head:0:12}"
artifact_dir="$review_root/neko-correction-entity-production-$run_id"
status_file="$snapshot_root/memory_v1_v5_2_neko_correction_entity_${run_id}.status"
mkdir -p "$artifact_dir"
chmod 0700 "$artifact_dir"

phase=capture_timer_state
: >"$timer_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$timer_state" ]]
chmod 0600 "$timer_state"

phase=quiesce_timers
while IFS=$'\t' read -r unit _enabled active; do
  [[ "$active" != active ]] || sudo -n systemctl stop "$unit"
done <"$timer_state"
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

phase=quiesce_brains
brains_state_before=$(systemctl is-active brains.service)
if [[ "$brains_state_before" == active ]]; then
  sudo -n systemctl stop brains.service
fi
brains_quiesced=1
for _attempt in $(seq 1 30); do
  systemctl is-active --quiet brains.service || break
  sleep 1
done
! systemctl is-active --quiet brains.service

phase=capture_baseline
docker exec "$container" psql -X -A -F $'\t' -t -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    SELECT table_name, EXISTS (
      SELECT 1 FROM information_schema.columns AS column_row
      WHERE column_row.table_schema='memory'
        AND column_row.table_name=table_row.table_name
        AND column_row.column_name='owner_user_id'
    )
    FROM information_schema.tables AS table_row
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  " >"$table_list"
target_before="$artifact_dir/target-before.tsv"
target_after="$artifact_dir/target-after.tsv"
target_replay="$artifact_dir/target-replay.tsv"
non_target_before="$artifact_dir/non-target-before.tsv"
non_target_after="$artifact_dir/non-target-after.tsv"
non_target_replay="$artifact_dir/non-target-replay.tsv"
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)

phase=backup
backup_partial="$snapshot_root/.memory_pre_v5_2_neko_correction_entity_${run_id}.dump.partial"
backup="$snapshot_root/memory_pre_v5_2_neko_correction_entity_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

manifest="$artifact_dir/manifest.json"
plan="$artifact_dir/plan.json"
authorization="$artifact_dir/authorization.json"
cross_manifest="$artifact_dir/cross-owner-manifest.json"
apply_result="$artifact_dir/apply-report.json"
report="$artifact_dir/report.json"
cp "$repo_root/$manifest_source" "$manifest"
chmod 0600 "$manifest"

phase=preflight
PYTHONPATH="$repo_root" "$python_bin" "$repo_root/$unit_test"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  "$python_bin" "$repo_root/$runner" plan \
  --manifest "$manifest" --review-root "$review_root" --output "$plan"
jq --arg owner "$other_owner" '.owner_user_id=$owner' \
  "$manifest" >"$cross_manifest"
chmod 0600 "$cross_manifest"
if POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  "$python_bin" "$repo_root/$runner" plan \
  --manifest "$cross_manifest" --review-root "$review_root" \
  --output "$artifact_dir/cross-owner-plan.json" >/dev/null 2>&1; then
  echo 'cross-owner Neko entity plan unexpectedly passed' >&2
  exit 1
fi
PYTHONPATH="$repo_root" "$python_bin" "$repo_root/$fixture" \
  --plan "$plan" --output "$authorization" --head "$head"

phase=apply
MEMORY_V1_V5_2_ENTITY_RESOLUTION_BATCH_APPLY=authorized \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  "$python_bin" "$repo_root/$runner" apply \
  --plan "$plan" --authorization "$authorization" \
  --review-root "$review_root" \
  --confirm RECONCILE_REVIEW_AND_APPLY_OWNER_V5_2_ENTITY_RESOLUTIONS_ONLY \
  --output "$apply_result"

[[ "$(jq -er '.database_rows_created' "$apply_result")" == 6 ]]
[[ "$(jq -er '.bindings_created' "$apply_result")" == 1 ]]
[[ "$(jq -er '.item_count' "$apply_result")" == 1 ]]
[[ "$(jq -er '.applied[0].operation' "$apply_result")" \
  == manual_link_existing_and_apply ]]
[[ "$(jq -er '.applied[0].applied_entity_id' "$apply_result")" == "$entity" ]]
[[ "$(jq -er '.replayed[0].review_outcome' "$apply_result")" == replayed ]]
[[ "$(jq -er '.replayed[0].apply_outcome' "$apply_result")" == replayed ]]
[[ "$(jq -er '.replayed[0].bindings_created' "$apply_result")" == 0 ]]

capture_partition target "$target_after"
capture_partition non_target "$non_target_after"
verify_target_delta "$target_before" "$target_after"
cmp -s "$non_target_before" "$non_target_after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=replay_proof
capture_partition target "$target_replay"
capture_partition non_target "$non_target_replay"
cmp -s "$target_after" "$target_replay"
cmp -s "$non_target_after" "$non_target_replay"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=postflight
[[ "$(psql_row "SELECT count(*) FROM memory.observation_entity_binding
  WHERE owner_user_id='$owner'::uuid
    AND observation_id='$observation'::uuid
    AND subject_entity_id='$entity'::uuid
    AND subject_resolution_id='$resolution'::uuid
    AND object_entity_id IS NULL
    AND object_resolution_id IS NULL")" == 1 ]]
[[ "$(psql_row "SELECT count(*) FROM memory.entity_alias_observation
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id='33126656-fc5a-5fc1-a035-246b14576ee5'::uuid
    AND mention_id='d1d58fb0-0422-4bf4-86bb-e7c98222a00c'::uuid
    AND resolution_id='$resolution'::uuid
    AND entity_id='$entity'::uuid
    AND alias_text='Neko'
    AND normalized_alias='neko'
    AND alias_type='observed_name'")" == 1 ]]
[[ "$(psql_row "SELECT count(*) FROM memory.claim_observation
  WHERE owner_user_id='$owner'::uuid
    AND observation_id='$observation'::uuid")" == 0 ]]

phase=restore
restore_runtime
authenticated_health
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]

REPORT="$report" APPLY="$apply_result" HEAD="$head" \
BACKUP="$backup" QDRANT="$qdrant_before" python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

apply = json.loads(Path(os.environ["APPLY"]).read_text())
backup_path = Path(os.environ["BACKUP"])
value = {
    "contract_version": "memory_v1_v5_2_neko_correction_entity_production_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": apply["owner_user_id"],
    "resolution_id": apply["applied"][0]["resolution_id"],
    "applied_entity_id": apply["applied"][0]["applied_entity_id"],
    "backup": str(backup_path),
    "backup_sha256": hashlib.sha256(backup_path.read_bytes()).hexdigest(),
    "verification": {
        "database_rows_created": 6,
        "bindings_created": 1,
        "alias_observations_created": 1,
        "zero_write_replay": True,
        "cross_owner_rejected": True,
        "non_target_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "entities_written": 0,
        "claims_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
        "timers_restored": True,
        "service_healthy": True,
    },
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY

phase=complete
printf 'report=%s\n' "$report"
printf 'backup=%s\n' "$backup"
printf 'memory_v1_v5_2_neko_correction_entity_apply_production: PASS\n'

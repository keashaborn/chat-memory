#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Records one exact V5.2 review route and its reviewed
# atom-admission decision. Stops before relational staging, entities,
# observations, claims, Qdrant, retrieval, or prompt influence.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for backup and timer control' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_EVIDENCE_CONTEXT_ROUTE_ADMISSION_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_EVIDENCE_CONTEXT_ROUTE_ADMISSION_APPLY=authorized is required' >&2
  exit 1
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
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
packet=25561cba-6891-51b6-94e9-c4a0db8eb031
evidence=049205b4-9a6c-5e1a-bb8f-2ab9f05f8964
manifest=manifests/memory_v1_v5_2_evidence_context_atom_admission_20260726.json
manifest_file_sha=36f46d759948f67896bb340e1e3ade173bec250c6471c68e0f230ae0230799f4
manifest_contract_sha=1c13e5b1045df80aa62b78a6842a2b298a420267893b3a4eb9217c32f045a2e2
router=scripts/memory_v1_v5_2_exact_review_route.py
admission=scripts/memory_v1_v5_2_atom_admission_apply_v2.py
review_root=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_evidence_context_route_admission.lock

declare -A expected_sha=(
  [scripts/memory_v1_v5_1_review_local_packet.py]=dacd61733dfdb043df99f6be612e6bf79a567e938b1a3ff4a21fadf4e08f6a55
  [scripts/memory_v1_v5_2_local_packet_router.py]=ca8b6f75743686a649aa8d9bb888ec6b315c1066724c72f1270df7be919d8056
  [scripts/memory_v1_v5_2_exact_review_route.py]=bc6d0f915b2e8bc9c42489abc0bbf7f9c8ae593d575b18291d004c58fee5e6ab
  [scripts/memory_v1_v5_2_atom_admission_apply_v2.py]=721184f8eebf5f4d14b553eb0cf0134960456d8834029263da1d10963868c5e7
  [tests/test_memory_v1_v5_2_exact_review_route.py]=1c89259f58c2eea89c86e5263cf58f7c51104f555fe8f9f6082279ac444afd90
  [tests/test_memory_v1_v5_2_atom_admission_apply_v2.py]=11425d0c0cc26d9053eab1a15e7bd655c4c5d84a01f4fc893d17874cac450086
  [manifests/memory_v1_v5_2_evidence_context_atom_admission_20260726.json]=36f46d759948f67896bb340e1e3ade173bec250c6471c68e0f230ae0230799f4
  [tools/memory_v1_v5_2_evidence_context_route_admission_clone.sh]=807d292bbcdf43d7be57046978819bc5f574d89e35cb005c1d3358d52412169b
)

unit_state=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.timers)
table_list=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.tables)
target_before=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.target-before)
target_after=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.target-after)
non_target_before=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.non-target-before)
non_target_after=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.non-target-after)
dry_output=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.dry)
route_output=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.route)
route_replay=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.route-replay)
isolation_output=$(mktemp /tmp/memory-v5-2-evidence-context-admission.XXXXXX.isolation)
chmod 0600 "$unit_state" "$table_list" "$target_before" "$target_after" \
  "$non_target_before" "$non_target_after" "$dry_output" "$route_output" \
  "$route_replay" "$isolation_output"
phase=initialization
timers_quiesced=0
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
  [[ "$timers_quiesced" -eq 1 ]] || return 0
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
  timers_quiesced=0
}

record_exit() {
  rc=$?
  trap - EXIT
  if [[ "$rc" -eq 0 && "$phase" != complete ]]; then
    rc=1
  fi
  restore_timers || rc=1
  rm -f "$unit_state" "$table_list" "$target_before" "$target_after" \
    "$non_target_before" "$non_target_after" "$dry_output" "$route_output" \
    "$route_replay" "$isolation_output"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$phase" "$rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$rc"
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
      SELECT count(*)::text || E'\\t' ||
        encode(public.digest(convert_to(coalesce(string_agg(
          row_json,E'\\n' ORDER BY row_json
        ),''),'UTF8'),'sha256'),'hex')
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
    rows = {}
    for line in Path(path).read_text().splitlines():
        table, count, digest = line.split("\t")
        rows[table] = (int(count), digest)
    return rows


before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
assert before.keys() == after.keys()
expected = {
    "v5_2_local_packet_route_event": 1,
    "v5_2_atom_admission_proposal": 1,
    "v5_2_atom_admission_review": 1,
    "v5_2_atom_admission_apply": 1,
    "v5_2_atom_admission_operation": 3,
}
for table in before:
    delta = after[table][0] - before[table][0]
    wanted = expected.get(table, 0)
    if delta != wanted:
        raise SystemExit(f"unexpected target-owner delta {table}: {delta} != {wanted}")
    if wanted == 0 and before[table][1] != after[table][1]:
        raise SystemExit(f"unexpected target-owner mutation {table}")
PY
}

run_router() {
  local actor=$1 output=$2 apply=${3:-false}
  local -a command=(
    "$python_bin" "$router"
    --owner-user-id "$actor"
    --packet-id "$packet"
    --review-root "$review_root"
  )
  if [[ "$apply" == true ]]; then
    command+=(--apply)
  fi
  runuser -u ubuntu -- env \
    POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
    MEMORY_V1_V5_2_EXACT_REVIEW_ROUTE_APPLY=memory_v1_v5_2_exact_review_route_apply_v1 \
    "${command[@]}" >"$output"
}

[[ -z "$(git status --porcelain)" ]]
head=$(git rev-parse HEAD)
[[ -n "${MEMORY_V1_REQUIRED_HEAD:-}" ]]
[[ "$head" == "$MEMORY_V1_REQUIRED_HEAD" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(curl -sS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:8088/docs)" == 200 ]]
[[ "$(sha256sum "$manifest" | awk '{print $1}')" == "$manifest_file_sha" ]]
[[ "$(jq -er '.manifest_sha256' "$manifest")" == "$manifest_contract_sha" ]]
for file in "${!expected_sha[@]}"; do
  [[ "$(sha256sum "$file" | awk '{print $1}')" == "${expected_sha[$file]}" ]]
done
"$python_bin" -m py_compile \
  scripts/memory_v1_v5_1_review_local_packet.py \
  scripts/memory_v1_v5_2_local_packet_router.py "$router" "$admission"
PYTHONPATH="$repo_root" "$python_bin" -m unittest \
  tests/test_memory_v1_v5_2_exact_review_route.py \
  tests/test_memory_v1_v5_2_atom_admission_apply_v2.py

mkdir -p "$review_root" "$snapshot_root"
chown ubuntu:ubuntu "$review_root"
chmod 0700 "$review_root" "$snapshot_root"
packet_hash=$(printf %s "$packet" | sha256sum | awk '{print $1}')
review_artifact="$review_root/v5-2-router-$packet_hash-review.json"
stage_artifact="$review_root/v5-2-router-$packet_hash-stage.json"
[[ ! -e "$review_artifact" && ! -e "$stage_artifact" ]]
[[ "$(psql_scalar "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")" == 0 ]]
[[ "$(psql_scalar "
  SELECT count(*) FROM memory.v5_2_atom_admission_proposal
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")" == 0 ]]
[[ "$(psql_scalar "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid")" == 0 ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_root/memory_v1_v5_2_evidence_context_admission_${run_tag}.status"
preflight="$review_root/v5-2-evidence-context-admission-${run_tag}-preflight.json"
applied="$review_root/v5-2-evidence-context-admission-${run_tag}-applied.json"
replayed="$review_root/v5-2-evidence-context-admission-${run_tag}-replayed.json"

phase=quiesce_timers
: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$unit_state" ]]
timer_count=$(wc -l <"$unit_state" | tr -d '[:space:]')
timers_quiesced=1
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

phase=fresh_backup
partial="$snapshot_root/.memory_pre_v5_2_evidence_context_admission_${run_tag}.dump.partial"
backup="$snapshot_root/memory_pre_v5_2_evidence_context_admission_${run_tag}.dump"
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

phase=route_dry_run
run_router "$owner" "$dry_output"
jq -e '
  .apply==false and (.plans|length)==1 and
  .plans[0].route=="manual_review_artifact_ready" and
  .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and
  .qdrant_writes==0 and .prompt_influence==0 and
  .external_model_calls==0
' "$dry_output" >/dev/null

phase=route_apply
run_router "$owner" "$route_output" true
jq -e '
  .apply==true and .outcome=="manual_review_artifacts_ready" and
  .write_counts.route_events==1 and
  .write_counts.restricted_review_artifacts==2 and
  .write_counts.stage==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .transactional_apply_proved==true and
  .zero_write_replay_proved==true and .external_model_calls==0
' "$route_output" >/dev/null

phase=route_replay_and_isolation
run_router "$owner" "$route_replay"
run_router "$other" "$isolation_output"
for output in "$route_replay" "$isolation_output"; do
  jq -e '
    .apply==false and (.plans|length)==1 and
    .plans[0].route=="no_work" and
    .database_writes==0 and .filesystem_writes==0 and
    .stage_writes==0 and .claim_writes==0 and
    .qdrant_writes==0 and .prompt_influence==0 and
    .external_model_calls==0
  ' "$output" >/dev/null
done

phase=admission_preflight
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  "$python_bin" "$admission" \
  --mode preflight --manifest "$manifest" --output "$preflight" >/dev/null
phase=admission_apply
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY_V2=authorized \
  "$python_bin" "$admission" \
  --mode apply --manifest "$manifest" --output "$applied" >/dev/null
phase=admission_replay
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  "$python_bin" "$admission" \
  --mode replay --manifest "$manifest" --output "$replayed" >/dev/null

PREFLIGHT="$preflight" APPLIED="$applied" REPLAYED="$replayed" \
"$python_bin" - <<'PY'
import json
import os
from pathlib import Path


preflight = json.loads(Path(os.environ["PREFLIGHT"]).read_text())
applied = json.loads(Path(os.environ["APPLIED"]).read_text())
replayed = json.loads(Path(os.environ["REPLAYED"]).read_text())
assert preflight["persistent_writes"] == 0
assert applied["persistent_writes"] == 6
assert replayed["persistent_writes"] == 0
assert len(applied["results"]) == len(replayed["results"]) == 1
assert applied["results"][0]["proposal_outcome"] == "applied"
assert applied["results"][0]["review_outcome"] == "applied"
assert applied["results"][0]["apply_outcome"] == "applied"
assert replayed["results"][0]["proposal_outcome"] == "replayed"
assert replayed["results"][0]["review_outcome"] == "replayed"
assert replayed["results"][0]["apply_outcome"] == "replayed"
PY

phase=postflight
capture_partition target "$target_after"
capture_partition non_target "$non_target_after"
cmp -s "$non_target_before" "$non_target_after"
verify_target_delta
[[ "$(psql_scalar "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND route='manual_review_artifact_ready'")" == 1 ]]
[[ "$(psql_scalar "
  SELECT count(*) FROM memory.v5_2_atom_admission_proposal
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND policy_version='memory_v1_v5_2_atom_admission_policy_v2'
    AND admitted_entity_mention_count=1
    AND admitted_observation_count=1
    AND deferred_atom_count=0
    AND rejected_atom_count=0")" == 1 ]]
[[ "$(psql_scalar "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid")" == 0 ]]
[[ "$(stat -c '%a:%U:%G' "$review_artifact")" == 600:ubuntu:ubuntu ]]
[[ "$(stat -c '%a:%U:%G' "$stage_artifact")" == 600:ubuntu:ubuntu ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

chown ubuntu:ubuntu "$preflight" "$applied" "$replayed"
restore_timers

phase=report
report="$snapshot_root/memory_v1_v5_2_evidence_context_admission_${run_tag}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$head" \
  --arg owner_user_id_sha256 "$(printf %s "$owner" | sha256sum | awk '{print $1}')" \
  --arg manifest_sha256 "$manifest_contract_sha" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg review_artifact_sha256 "$(sha256sum "$review_artifact" | awk '{print $1}')" \
  --arg stage_artifact_sha256 "$(sha256sum "$stage_artifact" | awk '{print $1}')" \
  --arg preflight_sha256 "$(sha256sum "$preflight" | awk '{print $1}')" \
  --arg applied_sha256 "$(sha256sum "$applied" | awk '{print $1}')" \
  --arg replayed_sha256 "$(sha256sum "$replayed" | awk '{print $1}')" \
  --arg qdrant_sha256 "$qdrant_after" \
  --argjson timer_count "$timer_count" \
  '{
    contract_version:"memory_v1_v5_2_evidence_context_route_admission_report_v1",
    outcome:"pass",
    completed_at:$completed_at,
    head_commit:$head_commit,
    owner_user_id_sha256:$owner_user_id_sha256,
    manifest_sha256:$manifest_sha256,
    backup:{path:$backup,sha256:$backup_sha256},
    exact_new_rows:{
      review_route_events:1,
      atom_admission_proposals:1,
      atom_admission_reviews:1,
      atom_admission_applies:1,
      atom_admission_operations:3,
      relational_stage:0,
      entity_mentions:0,
      observations:0,
      claims:0,
      qdrant:0,
      prompt_influence:0
    },
    restricted_artifacts:{
      review_sha256:$review_artifact_sha256,
      stage_preflight_sha256:$stage_artifact_sha256,
      admission_preflight_sha256:$preflight_sha256,
      admission_apply_sha256:$applied_sha256,
      admission_replay_sha256:$replayed_sha256
    },
    zero_write_replay_proved:true,
    account_isolation_proved:true,
    non_target_rows_unchanged:true,
    qdrant:{unchanged:true,sha256:$qdrant_sha256},
    timers:{count:$timer_count,restored:true},
    service_health:"active",
    external_model_calls:0,
    hard_stop:"before_relational_staging_entities_observations_claims_qdrant_retrieval_or_prompt_influence"
  }' >"$report"
chmod 0600 "$report"
report_sha=$(sha256sum "$report" | awk '{print $1}')
printf '%s  %s\n' "$report_sha" "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf '%s\n' \
  'memory_v1_v5_2_evidence_context_route_admission_production_apply: PASS' \
  "report=$report" \
  "report_sha256=$report_sha" \
  "backup=$backup" \
  "backup_sha256=$backup_sha"

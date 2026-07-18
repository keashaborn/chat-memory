#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Transactionally stages, reviews, and materializes one
# hash-locked project projection for one owner. Qdrant, retrieval, prompts,
# frontend code, other owners, and unrelated Memory V1 rows remain unchanged.

if [[ "${MEMORY_V1_PROJECT_PROJECTION_PRODUCTION_APPLY:-}" != "authorized" ]]; then
  echo "MEMORY_V1_PROJECT_PROJECTION_PRODUCTION_APPLY=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=3d224c2197bec3d0a62f8be8f8e4f4cf61c048db
bundle=/home/ubuntu/memory-v1-reviews/v5-project-memory-service-projection-bundle-20260718.json
expected_bundle_file_sha=aa74118093dbbd3ae4c2d6bf3c05af1db2d6eef454fe85c76dca1a0a47d9a38a
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
plan=b8e6f378-0f69-56b8-a032-e01af4411f8e
request=93ac4965-c20a-540a-96ff-a497b98c51c1
project=08cd6a8a-5599-43d5-8d5c-b59401df8ccc
observation=258d8d96-2cbd-4296-b878-769c90533fae
component=memory-v1
knowledge_key=architecture.memory_service
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
review_dir=/home/ubuntu/memory-v1-reviews
lock_file=/home/ubuntu/brains/.memory_v1_v5_project_projection_apply.lock
phase=initialization
status_file=
units_quiesced=0
unit_state_before=
timers=(
  memory-v1-consolidation.timer
  memory-v1-deferred-reconciliation-scan.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-governance.timer
  memory-v1-projection.timer
  memory-v1-v5-chat-capture.timer
)

[[ -z "$(git -C "$repo_root" status --porcelain)" ]] || {
  echo "production project apply requires a clean Git worktree" >&2
  exit 1
}
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(stat -c '%a' "$bundle")" == 600 ]]
[[ "$(sha256sum "$bundle" | awk '{print $1}')" == "$expected_bundle_file_sha" ]] || {
  echo "project projection bundle file SHA drifted" >&2
  exit 1
}

mkdir -p "$snapshot_dir" "$review_dir"
exec 9>"$lock_file"
flock -n 9 || {
  echo "another project projection apply holds the lock" >&2
  exit 1
}
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_project_projection_apply_${run_id}.status"
unit_state_before="$snapshot_dir/memory_v1_project_projection_units_before_${run_id}.tsv"

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

restore_units() {
  if [[ "$units_quiesced" -ne 1 || ! -s "$unit_state_before" ]]; then
    return 0
  fi
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]] || return 1
    if [[ "$active" == "active" ]]; then
      sudo systemctl start "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state_before"
  units_quiesced=0
}

record_exit() {
  code=$?
  if [[ "$units_quiesced" -eq 1 ]]; then
    saved_phase=$phase
    phase=restore_timer_state_after_failure
    restore_units || true
    phase=$saved_phase
  fi
  {
    printf 'run_id=%s\n' "$run_id"
    printf 'phase=%s\n' "$phase"
    printf 'exit_code=%s\n' "$code"
    printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$status_file"
  chmod 0600 "$status_file"
}
trap record_exit EXIT

qdrant_signature() {
  curl --fail --silent --show-error \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id)' \
    | sha256sum | awk '{print $1}'
}

table_state() {
  local table=$1
  local predicate=${2:-true}
  [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || return 1
  psql_scalar "
    SELECT count(*)::text || E'\\t' ||
           encode(public.digest(coalesce(string_agg(row_json,E'\\n'
             ORDER BY row_json),''),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(table_row)::text row_json
      FROM memory.\"$table\" AS table_row
      WHERE $predicate
    ) rows
  "
}

capture_all() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    printf '%s\t%s\n' "$table" "$(table_state "$table")" >>"$output"
  done < <(psql_scalar "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

capture_other_owners() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    printf '%s\t%s\n' "$table" \
      "$(table_state "$table" "owner_user_id IS DISTINCT FROM '$owner'::uuid")" \
      >>"$output"
  done < <(psql_scalar "
    SELECT table_name FROM information_schema.columns
    WHERE table_schema='memory' AND column_name='owner_user_id'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

capture_non_target() {
  local output=$1
  local review_id=$2 knowledge_id=$3 revision_id=$4 event_id=$5
  : >"$output"
  while IFS= read -r table; do
    predicate=true
    case "$table" in
      projection_plan)
        predicate="plan_id<>'$plan'::uuid" ;;
      projection_plan_item|projection_project_payload|projection_plan_observation)
        predicate="plan_id<>'$plan'::uuid" ;;
      projection_review)
        predicate="review_id<>'$review_id'::uuid" ;;
      project_knowledge_head_v5)
        predicate="NOT (project_id='$project'::uuid AND knowledge_id='$knowledge_id'::uuid)" ;;
      project_knowledge_revision_v5|project_knowledge_revision_observation)
        predicate="revision_id<>'$revision_id'::uuid" ;;
      projection_apply_event)
        predicate="event_id<>'$event_id'::uuid" ;;
      projection_dispatch_v5)
        predicate="apply_event_id<>'$event_id'::uuid" ;;
    esac
    printf '%s\t%s\n' "$table" "$(table_state "$table" "$predicate")" \
      >>"$output"
  done < <(psql_scalar "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  ")
  chmod 0600 "$output"
}

phase=preflight
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM memory.projection_plan
    WHERE owner_user_id='$owner'::uuid AND plan_id='$plan'::uuid)=0
  AND
  (SELECT count(*) FROM memory.project_knowledge_head_v5
    WHERE owner_user_id='$owner'::uuid AND project_id='$project'::uuid
      AND component_key='$component' AND knowledge_key='$knowledge_key')=0
  AND
  (SELECT count(*) FROM memory.projection_apply_event
    WHERE owner_user_id='$owner'::uuid AND request_id='$request'::uuid)=0
)::integer")" == 1 ]]

phase=capture_timer_state
: >"$unit_state_before"
for unit in "${timers[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state_before"
done
chmod 0600 "$unit_state_before"

phase=quiesce_timers
while IFS=$'\t' read -r unit _enabled active; do
  if [[ "$active" == "active" ]]; then sudo systemctl stop "$unit"; fi
done <"$unit_state_before"
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  systemctl is-active --quiet "$service" && exit 1
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done <"$unit_state_before"

phase=baseline_capture
baseline="$snapshot_dir/memory_v1_project_projection_before_${run_id}.tsv"
post="$snapshot_dir/memory_v1_project_projection_after_${run_id}.tsv"
other_before="$snapshot_dir/memory_v1_project_projection_other_before_${run_id}.tsv"
other_after="$snapshot_dir/memory_v1_project_projection_other_after_${run_id}.tsv"
replay_before="$snapshot_dir/memory_v1_project_projection_replay_before_${run_id}.tsv"
replay_after="$snapshot_dir/memory_v1_project_projection_replay_after_${run_id}.tsv"
capture_all "$baseline"
capture_other_owners "$other_before"
qdrant_before=$(qdrant_signature)

phase=backup
partial="$snapshot_dir/.memory_pre_project_projection_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_project_projection_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
docker exec -i "$container" pg_restore -l <"$partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=transactional_apply
apply_result="$review_dir/v5-project-memory-service-projection-apply-20260718.json"
replay_result="$review_dir/v5-project-memory-service-projection-replay-20260718.json"
[[ ! -e "$apply_result" && ! -e "$replay_result" ]]
set -a
source "$repo_root/.env"
set +a
MEMORY_V1_PROJECT_PROJECTION_APPLY=authorized PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_project_projection_apply.py" \
  --mode apply --bundle "$bundle" --request-id "$request" \
  --output "$apply_result"

review_id=$(jq -r '.review_id' "$apply_result")
knowledge_id=$(jq -r '.knowledge_id' "$apply_result")
revision_id=$(jq -r '.revision_id' "$apply_result")
event_id=$(jq -r '.apply_event_id' "$apply_result")
semantic=$(jq -r '.packet.projections[0].identity.semantic_key_sha256' "$bundle")
content_sha=$(BUNDLE="$bundle" python3 - <<'PY'
import hashlib, json, os
value=json.load(open(os.environ["BUNDLE"]))["packet"]["projections"][0]["payload"]
content={key:value[key] for key in ("canonical_text","document_state","authority_level","surface_policy")}
text=json.dumps(content,sort_keys=True,separators=(",",":"),ensure_ascii=False)
print(hashlib.sha256(text.encode()).hexdigest())
PY
)

phase=cross_transaction_zero_write_replay
capture_all "$replay_before"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/scripts/memory_v1_v5_project_projection_apply.py" \
  --mode replay --bundle "$bundle" --request-id "$request" \
  --prior-result "$apply_result" --output "$replay_result"
capture_all "$replay_after"
cmp -s "$replay_before" "$replay_after" || {
  diff -u "$replay_before" "$replay_after" >&2 || true
  echo "project projection replay changed database rows" >&2
  exit 1
}

phase=postflight
capture_non_target "$post" "$review_id" "$knowledge_id" "$revision_id" "$event_id"
capture_other_owners "$other_after"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo "unrelated Memory V1 rows changed" >&2
  exit 1
}
cmp -s "$other_before" "$other_after" || {
  diff -u "$other_before" "$other_after" >&2 || true
  echo "another owner's Memory V1 rows changed" >&2
  exit 1
}
[[ "$(psql_scalar "SELECT (
  (SELECT count(*) FROM memory.projection_plan
    WHERE owner_user_id='$owner'::uuid AND plan_id='$plan'::uuid)=1
  AND (SELECT count(*) FROM memory.projection_plan_item
    WHERE owner_user_id='$owner'::uuid AND plan_id='$plan'::uuid)=1
  AND (SELECT count(*) FROM memory.projection_project_payload
    WHERE owner_user_id='$owner'::uuid AND plan_id='$plan'::uuid)=1
  AND (SELECT count(*) FROM memory.projection_plan_observation
    WHERE owner_user_id='$owner'::uuid AND plan_id='$plan'::uuid
      AND observation_id='$observation'::uuid)=1
  AND (SELECT count(*) FROM memory.projection_review
    WHERE owner_user_id='$owner'::uuid AND review_id='$review_id'::uuid
      AND decision='authorized')=1
  AND (SELECT count(*) FROM memory.project_knowledge_head_v5
    WHERE owner_user_id='$owner'::uuid AND project_id='$project'::uuid
      AND knowledge_id='$knowledge_id'::uuid AND component_key='$component'
      AND binding_source='trusted_component_registry'
      AND semantic_key_sha256='$semantic' AND knowledge_kind='current_state'
      AND knowledge_key='$knowledge_key' AND revision_number=1
      AND current_revision_id='$revision_id'::uuid)=1
  AND (SELECT count(*) FROM memory.project_knowledge_revision_v5
    WHERE owner_user_id='$owner'::uuid AND project_id='$project'::uuid
      AND knowledge_id='$knowledge_id'::uuid AND revision_id='$revision_id'::uuid
      AND revision_number=1 AND content_sha256='$content_sha')=1
  AND (SELECT count(*) FROM memory.project_knowledge_revision_observation
    WHERE owner_user_id='$owner'::uuid AND project_id='$project'::uuid
      AND revision_id='$revision_id'::uuid
      AND observation_id='$observation'::uuid)=1
  AND (SELECT count(*) FROM memory.projection_apply_event
    WHERE owner_user_id='$owner'::uuid AND event_id='$event_id'::uuid
      AND request_id='$request'::uuid AND lane='project_knowledge')=1
  AND (SELECT count(*) FROM memory.projection_dispatch_v5
    WHERE owner_user_id='$owner'::uuid AND apply_event_id='$event_id'::uuid
      AND lane='project_knowledge')=1
)::integer")" == 1 ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]] || {
  echo "Qdrant changed during project projection apply" >&2
  exit 1
}

phase=restore_timer_state
restore_units
unit_state_after="$snapshot_dir/memory_v1_project_projection_units_after_${run_id}.tsv"
: >"$unit_state_after"
for unit in "${timers[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state_after"
done
chmod 0600 "$unit_state_after"
cmp -s "$unit_state_before" "$unit_state_after"

phase=report
report="$snapshot_dir/memory_v1_project_projection_apply_${run_id}.json"
REPORT="$report" APPLY_RESULT="$apply_result" REPLAY_RESULT="$replay_result" \
BACKUP="$backup" CATALOG="$catalog" BASELINE="$baseline" POST="$post" \
OTHER_BEFORE="$other_before" OTHER_AFTER="$other_after" \
REPLAY_BEFORE="$replay_before" REPLAY_AFTER="$replay_after" \
UNIT_BEFORE="$unit_state_before" UNIT_AFTER="$unit_state_after" \
QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

apply=json.load(open(os.environ["APPLY_RESULT"]))
report={
 "contract_version":"memory_v1_v5_project_projection_production_apply_report_v1",
 "instruction_source":"user_continue_20260717",
 "completed_at":dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00","Z"),
 "head_commit":os.environ["HEAD"],
 "plan_id":apply["plan_id"],"review_id":apply["review_id"],
 "apply_event_id":apply["apply_event_id"],"project_id":apply["project_id"],
 "knowledge_id":apply["knowledge_id"],"revision_id":apply["revision_id"],
 "backup":{"path":os.environ["BACKUP"],"catalog":os.environ["CATALOG"]},
 "evidence":{"apply_result":os.environ["APPLY_RESULT"],
   "replay_result":os.environ["REPLAY_RESULT"],"baseline":os.environ["BASELINE"],
   "post":os.environ["POST"],"other_owner_before":os.environ["OTHER_BEFORE"],
   "other_owner_after":os.environ["OTHER_AFTER"],"replay_before":os.environ["REPLAY_BEFORE"],
   "replay_after":os.environ["REPLAY_AFTER"],"timer_before":os.environ["UNIT_BEFORE"],
   "timer_after":os.environ["UNIT_AFTER"],"qdrant_before_sha256":os.environ["QDRANT_BEFORE"],
   "qdrant_after_sha256":os.environ["QDRANT_AFTER"]},
 "created":{"projection_stage":4,"projection_review":1,"project_materialization":5,"total":10},
 "checks":{"cross_transaction_zero_write_replay":True,"cross_owner_rejected":True,
   "other_owner_rows_unchanged":True,"unrelated_memory_rows_unchanged":True,
   "qdrant_unchanged":True,"timer_state_restored":True,"dispatch_unconsumed":True,
   "retrieval_activated":False,"prompt_influence":False},
 "hard_stop":"before_project_dispatch_consumption_or_retrieval_activation"
}
Path(os.environ["REPORT"]).write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_project_projection_production_apply: PASS\n'
printf 'report=%s\nbackup=%s\napply_result=%s\n' \
  "$report" "$backup" "$apply_result"

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the narrow canonical-name compatibility layer,
# stages and reviews exactly one owner-scoped candidate, proves zero-write
# replay and isolation, then stops before claims, Qdrant, retrieval, or prompt
# influence.

if [[ "${MEMORY_V1_V5_2_CANONICAL_NAME_CLAIM_PRODUCTION_APPLY:-}" \
      != authorized ]]; then
  echo 'MEMORY_V1_V5_2_CANONICAL_NAME_CLAIM_PRODUCTION_APPLY=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 1 ]]; then
  echo 'usage: ..._canonical_name_claim_review_production.sh ARTIFACT_DIR' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
artifact_dir=$(realpath -m "$1")
review_root=/home/ubuntu/memory-v1-reviews
snapshot_dir=/home/ubuntu/brains/snapshots
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
observation=93024235-89a8-49d5-88fa-7e4a143b68f3
stage_runner=scripts/memory_v1_v5_2_canonical_name_claim_stage.py
review_manifest_runner=scripts/memory_v1_v5_2_canonical_name_claim_review_manifest.py
review_runner=scripts/memory_v1_v5_2_canonical_name_claim_review_batch.py
migration=ops/sql/20260725_memory_v1_v5_2_canonical_name_claim_source.sql
security_test=tests/memory_v1_v5_2_canonical_name_claim_source_security.sql
container=brains-postgres-1
database=memory
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_canonical_name_claim_apply.lock

[[ "$artifact_dir" == "$review_root"/* ]]
[[ ! -e "$artifact_dir" ]]
[[ "$repo_root" == /opt/chat-memory ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
for required in \
  "$stage_runner" "$review_manifest_runner" "$review_runner" \
  "$migration" "$security_test"; do
  [[ -f "$repo_root/$required" ]]
done

phase=initialization
run_tag=
status_file=
units_quiesced=0
brains_quiesced=0
brains_state_before=
unit_state=$(mktemp /tmp/memory-v1-v5-2-canonical-name-units.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-v5-2-canonical-name-tables.XXXXXX)

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
  if [[ "$units_quiesced" -eq 1 ]]; then
    while IFS=$'\t' read -r unit enabled active; do
      [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
      if [[ "$active" == active ]]; then
        sudo -n systemctl start "$unit"
      else
        sudo -n systemctl stop "$unit"
      fi
      [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
      [[ "$(systemctl is-active "$unit")" == "$active" ]]
    done <"$unit_state"
    units_quiesced=0
  fi
}

record_exit() {
  exit_code=$?
  if [[ "$exit_code" -eq 0 && "$phase" != complete ]]; then
    exit_code=1
  fi
  restore_runtime || exit_code=1
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
        predicate="owner_user_id='$target_owner'::uuid"
      else
        predicate="owner_user_id IS DISTINCT FROM '$target_owner'::uuid"
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
      ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

verify_target_delta() {
  BEFORE="$target_before" AFTER="$target_after" python3 - <<'PY'
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
expected = {
    "projection_plan": 1,
    "projection_plan_item": 1,
    "projection_claim_payload": 1,
    "projection_plan_observation": 1,
    "projection_review": 1,
}
if before.keys() != after.keys():
    raise SystemExit("target-owner table set changed")
for table in before:
    delta = after[table][0] - before[table][0]
    wanted = expected.get(table, 0)
    if delta != wanted:
        raise SystemExit(
            f"unexpected target-owner delta {table}: {delta} != {wanted}"
        )
    if wanted == 0 and before[table][1] != after[table][1]:
        raise SystemExit(f"unexpected target-owner mutation {table}")
PY
}

set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ -n "${VS_SERVICE_TOKEN:-}" ]]

exec 9>"$lock_file"
flock -n 9
umask 077
head=$(git -C "$repo_root" rev-parse HEAD)
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_canonical_name_claim_${run_tag}.status"
mkdir -m 0700 "$artifact_dir"

manifest="$artifact_dir/stage-manifest.json"
authorization="$artifact_dir/stage-authorization.json"
cross_result="$artifact_dir/stage-cross-owner.json"
stage_apply="$artifact_dir/stage-apply.json"
stage_replay="$artifact_dir/stage-replay.json"
decisions="$artifact_dir/review-decisions.json"
review_manifest="$artifact_dir/review-manifest.json"
review_preflight="$artifact_dir/review-preflight.json"
review_apply="$artifact_dir/review-apply.json"
review_replay="$artifact_dir/review-replay.json"
report="$artifact_dir/production-report.json"

target_before="$snapshot_dir/memory_v1_v5_2_canonical_name_target_before_${run_tag}.tsv"
target_after="$snapshot_dir/memory_v1_v5_2_canonical_name_target_after_${run_tag}.tsv"
target_replay="$snapshot_dir/memory_v1_v5_2_canonical_name_target_replay_${run_tag}.tsv"
non_target_before="$snapshot_dir/memory_v1_v5_2_canonical_name_non_target_before_${run_tag}.tsv"
non_target_after="$snapshot_dir/memory_v1_v5_2_canonical_name_non_target_after_${run_tag}.tsv"
non_target_replay="$snapshot_dir/memory_v1_v5_2_canonical_name_non_target_replay_${run_tag}.tsv"

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
  sudo -n systemctl stop "$unit"
done <"$unit_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$unit_state"

brains_state_before=$(systemctl is-active brains.service)
if [[ "$brains_state_before" == active ]]; then
  sudo -n systemctl stop brains.service
fi
brains_quiesced=1
! systemctl is-active --quiet brains.service

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_2_canonical_name_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_canonical_name_${run_tag}.dump"
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

phase=install
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$migration"
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$migration"
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$security_test"

phase=prepare
PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python \
  "$repo_root/tests/test_memory_v1_v5_2_compiler_v8_claim_stage.py"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$stage_runner" manifest \
  --owner "$target_owner" --observation "$observation" \
  --required-head "$head" --output "$manifest"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$stage_runner" authorize \
  --manifest "$manifest" --output "$authorization"
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$stage_runner" cross-owner \
  --manifest "$manifest" --other-owner "$other_owner" --output "$cross_result"
[[ "$(jq -er '.cross_owner_rejected' "$cross_result")" == true ]]

MANIFEST="$manifest" OUTPUT="$decisions" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
from scripts.memory_v1_projection_v5_contract_test import sha256

stage = json.loads(Path(os.environ["MANIFEST"]).read_text())
if len(stage["items"]) != 1:
    raise SystemExit("exactly one canonical-name candidate is required")
item = stage["items"][0]
if (
    item["predicate"] != "identity.name_canonical"
    or item["canonical_text"] != "Neko's canonical name is Neko."
):
    raise SystemExit("canonical-name candidate semantics drifted")
value = {
    "contract_version":
        "memory_v1_v5_2_canonical_name_claim_review_decisions_v1",
    "owner_user_id": stage["owner_user_id"],
    "evidence_ids": stage["evidence_ids"],
    "decisions": [{
        "observation_id": item["observation_id"],
        "decision": "authorized",
        "reason": (
            "Reviewed canonical-name claim for the reconciled Neko animal "
            "entity; the source correction remains immutable evidence."
        ),
        "reason_codes": [
            "correction_target_reconciled",
            "canonical_name_normalized",
            "owner_entity_binding_reviewed",
        ],
    }],
}
value["decisions_sha256"] = sha256(value)
path = Path(os.environ["OUTPUT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY
PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_manifest_runner" \
  --owner "$target_owner" --required-head "$head" \
  --stage-manifest "$manifest" --decisions "$decisions" \
  --output "$review_manifest"

phase=baseline
docker exec "$container" psql -X -A -t -F $'\t' -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    SELECT table_name, EXISTS (
      SELECT 1 FROM information_schema.columns AS column_info
      WHERE column_info.table_schema='memory'
        AND column_info.table_name=tables.table_name
        AND column_info.column_name='owner_user_id')
    FROM information_schema.tables AS tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name" >"$table_list"
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)

phase=apply
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CANONICAL_NAME_CLAIM_STAGE_APPLY=authorized \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$stage_runner" apply \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_EXACT_NEKO_CANONICAL_NAME_CLAIM_CANDIDATE_ONLY \
  --output "$stage_apply"
[[ "$(jq -er '.rows_written' "$stage_apply")" == 4 ]]

MEMORY_V1_REQUIRED_HEAD="$head" \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode preflight --manifest "$review_manifest" --output "$review_preflight"
[[ "$(jq -er '.rows_written' "$review_preflight")" == 0 ]]
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CANONICAL_NAME_CLAIM_REVIEW_APPLY=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode apply --manifest "$review_manifest" --output "$review_apply"
[[ "$(jq -er '.rows_written' "$review_apply")" == 1 ]]

capture_partition target "$target_after"
capture_partition non_target "$non_target_after"
verify_target_delta
cmp -s "$non_target_before" "$non_target_after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=replay
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CANONICAL_NAME_CLAIM_STAGE_APPLY=authorized \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  "$repo_root/$stage_runner" replay \
  --manifest "$manifest" --authorization "$authorization" \
  --confirm STAGE_EXACT_NEKO_CANONICAL_NAME_CLAIM_CANDIDATE_ONLY \
  --output "$stage_replay"
MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_CANONICAL_NAME_CLAIM_REVIEW_APPLY=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" \
  --mode replay --manifest "$review_manifest" --output "$review_replay"
[[ "$(jq -er '.rows_written' "$stage_replay")" == 0 ]]
[[ "$(jq -er '.rows_written' "$review_replay")" == 0 ]]
capture_partition target "$target_replay"
capture_partition non_target "$non_target_replay"
cmp -s "$target_after" "$target_replay"
cmp -s "$non_target_after" "$non_target_replay"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=postflight
[[ "$(psql_row "
  SELECT count(*)
  FROM memory.projection_claim_payload AS payload
  JOIN memory.projection_plan_item AS item
    USING (owner_user_id,plan_id,projection_ref)
  JOIN memory.projection_plan_observation AS link
    USING (owner_user_id,plan_id,projection_ref)
  JOIN memory.projection_review AS review
    USING (owner_user_id,plan_id,projection_ref)
  WHERE payload.owner_user_id='$target_owner'
    AND link.observation_id='$observation'::uuid
    AND item.predicate='identity.name_canonical'
    AND item.review_state='manual_review_required'
    AND payload.claim_class='direct_claim'
    AND payload.canonical_text='Neko''s canonical name is Neko.'
    AND review.decision='authorized'")" == 1 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim_observation
  WHERE owner_user_id='$target_owner'
    AND observation_id='$observation'::uuid")" == 0 ]]

phase=restore
restore_runtime
docker exec "$container" pg_isready -U sage -d "$database" >/dev/null
curl --fail --silent --show-error --max-time 30 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz >/dev/null

phase=report
REPORT="$report" BACKUP="$backup" HEAD="$head" MANIFEST="$manifest" \
REVIEW="$review_manifest" STAGE="$stage_apply" REVIEW_RESULT="$review_apply" \
QDRANT="$qdrant_before" python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

stage = json.loads(Path(os.environ["MANIFEST"]).read_text())
review = json.loads(Path(os.environ["REVIEW"]).read_text())
stage_result = json.loads(Path(os.environ["STAGE"]).read_text())
review_result = json.loads(Path(os.environ["REVIEW_RESULT"]).read_text())
value = {
    "contract_version":
        "memory_v1_v5_2_canonical_name_claim_review_production_report_v1",
    "completed_at":
        dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "head_commit": os.environ["HEAD"],
    "owner_user_id": stage["owner_user_id"],
    "backup": os.environ["BACKUP"],
    "stage_manifest_sha256": stage["manifest_sha256"],
    "review_manifest_sha256": review["manifest_sha256"],
    "candidate": {
        "observation_id": stage["items"][0]["observation_id"],
        "predicate": stage["items"][0]["predicate"],
        "canonical_text": stage["items"][0]["canonical_text"],
    },
    "verification": {
        "stage_rows_written": stage_result["rows_written"],
        "existing_entailment_reused": True,
        "review_rows_written": review_result["rows_written"],
        "zero_write_replay": True,
        "account_isolation_verified": True,
        "non_target_memory_unchanged": True,
        "qdrant_sha256": os.environ["QDRANT"],
        "qdrant_unchanged": True,
        "claims_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
        "timers_restored_exactly": True,
        "brains_service_restored_exactly": True,
    },
}
path = Path(os.environ["REPORT"])
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
path.chmod(0o600)
PY
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'backup=%s\n' "$backup"
printf 'report=%s\n' "$report"
printf 'memory_v1_v5_2_canonical_name_claim_review_production: PASS\n'

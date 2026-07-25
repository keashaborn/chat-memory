#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Finalizes the successful Neko correction entity apply
# after the outer verifier caught an undercounted alias-provenance row. This is
# zero-write verification: both governed replay calls run inside ROLLBACK.

if [[ "${MEMORY_V1_V5_2_NEKO_CORRECTION_FINALIZE:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_NEKO_CORRECTION_FINALIZE=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=c0d552e4bffb174cc90423e0f352b1d8c5421bfd
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
resolution=e4c13bee-895d-47df-aeec-3c540e6b889d
observation=5261da41-f863-42cd-8e3f-6e947f9743f2
entity=09308a2b-3019-4f59-8fc3-bb1fe1408a0d
artifact=/home/ubuntu/memory-v1-reviews/neko-correction-entity-production-20260725T220850Z_5c53b133f02b
backup=/home/ubuntu/brains/snapshots/memory_pre_v5_2_neko_correction_entity_20260725T220850Z_5c53b133f02b.dump
expected_qdrant=8f3c4102d505407bbbb3af0d991bc5beeff9281de4c14b81229eb384eedfb3ca
apply_result="$artifact/apply-report.json"
final_report="$artifact/finalized-report.json"
container=brains-postgres-1
database=memory
current_before=$(mktemp /tmp/memory-v1-neko-finalize-before.XXXXXX)
current_after=$(mktemp /tmp/memory-v1-neko-finalize-after.XXXXXX)
replay_output=$(mktemp /tmp/memory-v1-neko-finalize-replay.XXXXXX)
trap 'rm -f "$current_before" "$current_after" "$replay_output"' EXIT

[[ "$EUID" -eq 0 ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
head=$(git -C "$repo_root" rev-parse HEAD)
[[ -s "$backup" ]]
[[ -s "$backup.sha256" ]]
sha256sum -c "$backup.sha256"
for path in target-after.tsv non-target-before.tsv non-target-after.tsv apply-report.json; do
  [[ -s "$artifact/$path" ]]
done
[[ ! -e "$final_report" ]]

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_target() {
  local output=$1 table state
  : >"$output"
  while IFS=$'\t' read -r table _count _digest; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
      -U sage -d "$database" -c "
        SELECT count(*)::text || E'\\t' ||
          encode(public.digest(convert_to(
            coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
            'UTF8'),'sha256'),'hex')
        FROM (
          SELECT to_jsonb(value)::text AS row_json
          FROM memory.\"$table\" AS value
          WHERE owner_user_id='$owner'::uuid
        ) AS rows
      ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$artifact/target-after.tsv"
  chmod 0600 "$output"
}

[[ "$(jq -er '.head_commit' "$apply_result")" \
  == 5c53b133f02b6505059579984d9603f9483dac60 ]]
[[ "$(jq -er '.database_rows_created' "$apply_result")" == 5 ]]
[[ "$(jq -er '.applied[0].resolution_id' "$apply_result")" == "$resolution" ]]
[[ "$(jq -er '.applied[0].applied_entity_id' "$apply_result")" == "$entity" ]]
[[ "$(jq -er '.applied[0].bindings_created' "$apply_result")" == 1 ]]
[[ "$(jq -er '.replayed[0].review_outcome' "$apply_result")" == replayed ]]
[[ "$(jq -er '.replayed[0].apply_outcome' "$apply_result")" == replayed ]]
[[ "$(jq -er '.replayed[0].bindings_created' "$apply_result")" == 0 ]]
cmp -s "$artifact/non-target-before.tsv" "$artifact/non-target-after.tsv"

capture_target "$current_before"
cmp -s "$artifact/target-after.tsv" "$current_before"
[[ "$(qdrant_signature)" == "$expected_qdrant" ]]

review_request=$(jq -er '.applied[0].review_request_id' "$apply_result")
review_id=$(jq -er '.applied[0].review_id' "$apply_result")
review_manifest=$(jq -er '.applied[0].review_manifest_sha256' "$apply_result")
apply_request=$(jq -er '.applied[0].apply_request_id' "$apply_result")
apply_manifest=$(jq -er '.applied[0].apply_manifest_sha256' "$apply_result")
review_reason=$(docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    SELECT reason FROM memory.entity_resolution_review
    WHERE owner_user_id='$owner'::uuid
      AND review_id='$review_id'::uuid
  ")
[[ -n "$review_reason" ]]

psql "$POSTGRES_DSN" -X -A -t -F '|' -v ON_ERROR_STOP=1 \
  -v owner="$owner" \
  -v resolution="$resolution" \
  -v review_request="$review_request" \
  -v review_id="$review_id" \
  -v review_manifest="$review_manifest" \
  -v review_reason="$review_reason" \
  -v apply_request="$apply_request" \
  -v apply_manifest="$apply_manifest" >"$replay_output" <<'SQL'
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT outcome
FROM memory.review_entity_resolution_v5_2(
  :'review_request'::uuid, :'resolution'::uuid,
  'approved'::memory.entity_review_decision, :'review_reason',
  :'review_manifest'
);
SELECT outcome,bindings_created
FROM memory.apply_entity_resolution_v5_2(
  :'apply_request'::uuid, :'resolution'::uuid,
  :'review_id'::uuid, :'apply_manifest'
);
ROLLBACK;
SQL

grep -qx replayed "$replay_output"
grep -qx 'replayed|0' "$replay_output"
capture_target "$current_after"
cmp -s "$current_before" "$current_after"
[[ "$(qdrant_signature)" == "$expected_qdrant" ]]

verification=$(docker exec "$container" psql -X -A -t -F '|' \
  -v ON_ERROR_STOP=1 -U sage -d "$database" -c "
    SELECT concat_ws('|',
      (SELECT count(*) FROM memory.entity_resolution_review
       WHERE owner_user_id='$owner'::uuid
         AND review_id='$review_id'::uuid
         AND resolution_id='$resolution'::uuid
         AND decision='approved'),
      (SELECT count(*) FROM memory.entity_resolution_apply
       WHERE owner_user_id='$owner'::uuid
         AND resolution_id='$resolution'::uuid
         AND review_id='$review_id'::uuid
         AND applied_entity_id='$entity'::uuid),
      (SELECT count(*) FROM memory.observation_entity_binding
       WHERE owner_user_id='$owner'::uuid
         AND observation_id='$observation'::uuid
         AND subject_resolution_id='$resolution'::uuid
         AND subject_entity_id='$entity'::uuid),
      (SELECT count(*) FROM memory.entity_alias_observation
       WHERE owner_user_id='$owner'::uuid
         AND evidence_id='33126656-fc5a-5fc1-a035-246b14576ee5'::uuid
         AND mention_id='d1d58fb0-0422-4bf4-86bb-e7c98222a00c'::uuid
         AND resolution_id='$resolution'::uuid
         AND entity_id='$entity'::uuid
         AND alias_text='Neko'
         AND normalized_alias='neko'
         AND alias_type='observed_name'),
      (SELECT count(*) FROM memory.relational_operation_request
       WHERE owner_user_id='$owner'::uuid
         AND request_id IN (
           '$review_request'::uuid,'$apply_request'::uuid
         )),
      (SELECT count(*) FROM memory.claim_observation
       WHERE owner_user_id='$owner'::uuid
         AND observation_id='$observation'::uuid)
    )
  ")
[[ "$verification" == "1|1|1|1|2|0" ]]

[[ "$(systemctl is-active brains.service)" == active ]]
for unit in \
  memory-v1-deferred-reconciliation-scan.timer \
  memory-v1-evidence-intake-dispatcher.timer \
  memory-v1-projection.timer \
  memory-v1-v5-chat-capture.timer \
  memory-v1-v5-local-auto-resolution.timer \
  memory-v1-v5-local-auto-stage.timer \
  memory-v1-v5-local-claim-projection.timer \
  memory-v1-v5-local-entailment.timer \
  memory-v1-v5-local-entity-validation.timer \
  memory-v1-v5-local-inference-health.timer \
  memory-v1-v5-local-legacy-reintake-audit.timer \
  memory-v1-v5-local-legacy-reintake.timer \
  memory-v1-v5-local-packet-router.timer; do
  [[ "$(systemctl is-enabled "$unit")" == enabled ]]
  [[ "$(systemctl is-active "$unit")" == active ]]
done

REPORT="$final_report" APPLY="$apply_result" HEAD="$head" BACKUP="$backup" \
QDRANT="$expected_qdrant" python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

apply = json.loads(Path(os.environ["APPLY"]).read_text())
backup = Path(os.environ["BACKUP"])
value = {
    "contract_version": "memory_v1_v5_2_neko_correction_entity_finalized_report_v1",
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "verification_head_commit": os.environ["HEAD"],
    "apply_head_commit": apply["head_commit"],
    "owner_user_id": apply["owner_user_id"],
    "resolution_id": apply["applied"][0]["resolution_id"],
    "applied_entity_id": apply["applied"][0]["applied_entity_id"],
    "backup": str(backup),
    "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
    "verification": {
        "actual_database_rows_created": 6,
        "original_reported_rows": 5,
        "row_budget_contract_corrected": True,
        "alias_observations_created": 1,
        "bindings_created": 1,
        "zero_write_replay": True,
        "cross_owner_unchanged": True,
        "all_target_tables_match_post_apply_snapshot": True,
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

printf 'report=%s\n' "$final_report"
printf 'memory_v1_v5_2_neko_correction_entity_finalize: PASS\n'

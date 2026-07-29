#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Deploys the tested compiler and append-only schema,
# re-extracts exactly two owner-scoped packets, and stops before routing,
# staging, claims, Qdrant, retrieval, or prompt influence.

if [[ "${MEMORY_V1_V5_2_PET_IDENTITY_REEXTRACT_RUN:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_PET_IDENTITY_REEXTRACT_RUN=authorized is required' >&2
  exit 1
fi
test "$(id -u)" -eq 0

live=/opt/chat-memory
target=/tmp/chat-memory-pet-temporal-integration-v2
expected_live_head=3e8d56b06b37894d77dcbccd03d5a5f0e41bf3c6
required_target_ancestor=e3318a2885b54a29f5892f81993103510b8d7309
target_head=$(git -C "$target" rev-parse HEAD)
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=673d64a3-c4ba-4d1c-89e3-e0c579022fad
selector=20260729_v5_2_pet_identity_compiler_reextract_v1
compiler_version=memory_v1_semantic_policy_compiler_v8
migration=ops/sql/20260729_memory_v1_v5_2_pet_identity_reextract.sql
rollback=ops/sql/20260729_memory_v1_v5_2_pet_identity_reextract_rollback.sql
clone_tool=tools/memory_v1_v5_2_pet_identity_reextract_clone.sh
canary=scripts/memory_v1_v5_local_inference_canary.py
api_key_file=/etc/memory-v1-local-inference/api-key
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_pet_identity_reextract.lock
artifact_dir=$(mktemp -d /tmp/memory-pet-identity-production.XXXXXX)
timer_state=$artifact_dir/timers.tsv
protected_before=$artifact_dir/protected-before.tsv
protected_after=$artifact_dir/protected-after.tsv
isolation_before=$artifact_dir/isolation-before.tsv
isolation_after=$artifact_dir/isolation-after.tsv
timers_restored=0
stage=preflight

packets=(
  adc8ecf7-63bd-5dc0-8283-0c2f765893c5
  3b44e557-2910-510c-ac8a-707ef92a398d
)
evidence=(
  c5d6f5cf-c6d6-554c-85d9-02150e8b8ae7
  2be95051-30c8-5f87-8ff9-e0999600b447
)
content_sha=(
  363ec5a70bc7e215ea9e2c95cd1e5595e430a491e90ecda01bdce241ae9c5530
  429f42d7b33edc49fbffde26b10531644e8ac7e04ca9d6eca7742b0a63d2b85e
)
storage_sha=(
  5454b320db178148ca5832108cd84632180d7ca3329b88dd7a03bb9009689d11
  df60115074ecfa66de0e3efa767f7a53448db4bc932cd49b9c65afca36d1f039
)
reasons=(
  unsupported_pet_deceased_role
  ambiguous_pet_loss_identity_missing
)
operations=(
  61141ef5-3adc-5be1-98a1-59244f32b699
  aa338f59-2eb5-5585-81d1-b49df83fc012
)
jobs=(
  05d1d64e-e403-51d6-9dcc-1894a7b62941
  e7d7a8af-fc33-55a6-ac70-0e97bfc5b878
)
terminals=(
  86154895-812a-5485-a8bf-6ee71772cce8
  5ba2fa6a-02a5-5a8f-8a9a-67457c8f9b50
)
runs=(
  5ba78195-2d68-50fc-bb55-c146406d4ea6
  99a585d4-3571-52c7-bb8b-5db652898c1e
)

declare -A expected_sha=(
  ["$migration"]=865573081c158f3a72084b2ca0433251ce94cce198597751415a9b6524848ce4
  ["$rollback"]=90402debe3cebc8c70fc804b97f18ea6e14f925286947e6b845d8af7d5577036
  ["scripts/memory_v1_relational_extraction_v5_local_provider.py"]=138fd7905e612404837922cc52c838466f812a39e55033288affb9ad9a9c795c
  ["tests/test_memory_v1_local_provider_v5_2.py"]=87c4bf49f3ff7f410ffd313b678a32987dcbd1ef052f343b2c60972123f723e2
  ["$clone_tool"]=2b3ec2c7ecc22b4f5cfef83a0fbac7dc3b47ec73a0585b2fd1353162d004642f
)

restore_timers() {
  if [[ "$timers_restored" == 1 || ! -s "$timer_state" ]]; then
    return
  fi
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null
    else
      systemctl disable "$unit" >/dev/null 2>&1 || true
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
    test "$(systemctl is-enabled "$unit")" = "$enabled"
    test "$(systemctl is-active "$unit")" = "$active"
  done <"$timer_state"
  timers_restored=1
}

cleanup() {
  rc=$?
  trap - EXIT
  restore_timers || rc=1
  if [[ "$rc" != 0 ]]; then
    printf 'memory_v1_v5_2_pet_identity_reextract_production: FAIL stage=%s rc=%s\n' \
      "$stage" "$rc" >&2
    printf 'artifacts_retained=%s\n' "$artifact_dir" >&2
  else
    rm -rf "$artifact_dir"
  fi
  exit "$rc"
}
trap cleanup EXIT

test "$(git -C "$live" rev-parse HEAD)" = "$expected_live_head"
test -z "$(git -C "$live" status --porcelain)"
test -z "$(git -C "$target" status --porcelain)"
git -C "$live" merge-base --is-ancestor "$expected_live_head" "$target_head"
git -C "$target" merge-base --is-ancestor \
  "$required_target_ancestor" "$target_head"
test -r "$api_key_file"
test "$(stat -c %a "$api_key_file")" = 600
test "$(systemctl is-active brains.service)" = active
for path in "${!expected_sha[@]}"; do
  test "$(sha256sum "$target/$path" | awk '{print $1}')" = \
    "${expected_sha[$path]}"
done

stage=clone_verification
REPO_ROOT="$target" bash "$target/$clone_tool" \
  >"$artifact_dir/clone.out"
grep -qx 'memory_v1_v5_2_pet_identity_reextract_clone: PASS' \
  "$artifact_dir/clone.out"

exec 9>"$lock_file"
flock -n 9

systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u \
  | while read -r unit; do
      printf '%s\t%s\t%s\n' "$unit" \
        "$(systemctl is-enabled "$unit")" \
        "$(systemctl is-active "$unit")"
    done >"$timer_state"
test -s "$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$timer_state"
test "$(
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE lease_token IS NOT NULL
      AND lease_expires_at > clock_timestamp();
  "
)" = 0

set -a
source "$live/.env"
set +a
test -n "${POSTGRES_DSN:-}"

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

protected_snapshot() {
  local output=$1
  docker exec "$container" psql -U sage -d "$database" -X -Atq \
    -v ON_ERROR_STOP=1 >"$output" <<'SQL'
SELECT 'claim', count(*), coalesce(max(created_at)::text, '')
FROM memory.claim
UNION ALL
SELECT 'entity', count(*), coalesce(max(created_at)::text, '')
FROM memory.entity
UNION ALL
SELECT 'observation', count(*), coalesce(max(created_at)::text, '')
FROM memory.observation
UNION ALL
SELECT 'relational_stage', count(*), coalesce(max(created_at)::text, '')
FROM memory.relational_stage_batch
UNION ALL
SELECT 'projection_outbox', count(*), coalesce(max(created_at)::text, '')
FROM memory.projection_outbox
UNION ALL
SELECT 'answer_binding', count(*), coalesce(max(created_at)::text, '')
FROM memory.final_answer_memory_binding_v1;
SQL
}

isolation_snapshot() {
  local output=$1
  docker exec "$container" psql -U sage -d "$database" -X -Atq \
    -v ON_ERROR_STOP=1 >"$output" <<SQL
WITH rows(label, value) AS (
  SELECT 'other_jobs', to_jsonb(value)::text
  FROM memory.evidence_extraction_job AS value
  WHERE owner_user_id <> '$owner'::uuid
  UNION ALL
  SELECT 'other_terminals', to_jsonb(value)::text
  FROM memory.evidence_intake_terminal AS value
  WHERE owner_user_id <> '$owner'::uuid
  UNION ALL
  SELECT 'other_events', to_jsonb(value)::text
  FROM memory.evidence_extraction_event AS value
  WHERE owner_user_id <> '$owner'::uuid
  UNION ALL
  SELECT 'other_ledger', to_jsonb(value)::text
  FROM memory.v5_local_inference_event AS value
  WHERE owner_user_id <> '$owner'::uuid
  UNION ALL
  SELECT 'other_packets', to_jsonb(value)::text
  FROM memory.evidence_extraction_packet_v5_local AS value
  WHERE owner_user_id <> '$owner'::uuid
  UNION ALL
  SELECT 'same_owner_other_jobs', to_jsonb(value)::text
  FROM memory.evidence_extraction_job AS value
  WHERE owner_user_id = '$owner'::uuid
    AND job_id NOT IN ('${jobs[0]}'::uuid, '${jobs[1]}'::uuid)
  UNION ALL
  SELECT 'same_owner_other_events', to_jsonb(value)::text
  FROM memory.evidence_extraction_event AS value
  WHERE owner_user_id = '$owner'::uuid
    AND job_id NOT IN ('${jobs[0]}'::uuid, '${jobs[1]}'::uuid)
  UNION ALL
  SELECT 'same_owner_other_ledger', to_jsonb(value)::text
  FROM memory.v5_local_inference_event AS value
  WHERE owner_user_id = '$owner'::uuid
    AND (
      job_id IS NULL
      OR NOT (
        job_id = ANY(
          ARRAY['${jobs[0]}'::uuid, '${jobs[1]}'::uuid]
        )
      )
    )
  UNION ALL
  SELECT 'same_owner_other_packets', to_jsonb(value)::text
  FROM memory.evidence_extraction_packet_v5_local AS value
  WHERE owner_user_id = '$owner'::uuid
    AND job_id NOT IN ('${jobs[0]}'::uuid, '${jobs[1]}'::uuid)
), labels(label) AS (VALUES
  ('other_jobs'),
  ('other_terminals'),
  ('other_events'),
  ('other_ledger'),
  ('other_packets'),
  ('same_owner_other_jobs'),
  ('same_owner_other_events'),
  ('same_owner_other_ledger'),
  ('same_owner_other_packets')
)
SELECT labels.label, count(rows.value),
       encode(public.digest(convert_to(coalesce(string_agg(
         rows.value, E'\n' ORDER BY rows.value), ''), 'UTF8'), 'sha256'), 'hex')
FROM labels LEFT JOIN rows USING(label)
GROUP BY labels.label
ORDER BY labels.label;
SQL
}

run_tag="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir=/var/backups/chat-memory/pet-identity-reextract-$run_tag
mkdir -p "$backup_dir"
stage=fresh_backup
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_dir/memory.dump"
test -s "$backup_dir/memory.dump"
sha256sum "$backup_dir/memory.dump" >"$backup_dir/memory.dump.sha256"
git -C "$live" tag "rollback/pet-identity-reextract-$run_tag" \
  "$expected_live_head"

protected_snapshot "$protected_before"
isolation_snapshot "$isolation_before"
qdrant_before=$(qdrant_signature)

stage=code_deploy
git -C "$live" merge --ff-only "$target_head"
test "$(git -C "$live" rev-parse HEAD)" = "$target_head"
for path in "${!expected_sha[@]}"; do
  test "$(sha256sum "$live/$path" | awk '{print $1}')" = \
    "${expected_sha[$path]}"
done

stage=schema_install
docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 <"$live/$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 <"$live/$migration" >/dev/null
test "$(
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    SELECT pg_get_userbyid(proowner)
    FROM pg_proc
    WHERE oid='memory.plan_owner_v5_2_pet_identity_reextract_v1(uuid)'::regprocedure;
  "
)" = memory_v5_local_reextract_maintainer

stage=rollback_only_security
for index in 0 1; do
  count=$(psql "$POSTGRES_DSN" -X -Atq -v ON_ERROR_STOP=1 <<SQL | tail -1
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT count(*)
FROM memory.plan_owner_v5_2_pet_identity_reextract_v1(
  '${packets[$index]}'::uuid
);
ROLLBACK;
SQL
)
  test "$count" = 1
  count=$(psql "$POSTGRES_DSN" -X -Atq -v ON_ERROR_STOP=1 <<SQL | tail -1
BEGIN;
SELECT set_config('app.user_id','$other_owner',true);
SELECT count(*)
FROM memory.plan_owner_v5_2_pet_identity_reextract_v1(
  '${packets[$index]}'::uuid
);
ROLLBACK;
SQL
)
  test "$count" = 0
done

stage=restart_brains
systemctl restart brains.service
for _attempt in $(seq 1 60); do
  if [[ "$(systemctl is-active brains.service)" == active ]] \
     && ss -ltn | grep -q '127.0.0.1:8088'; then
    break
  fi
  sleep 1
done
test "$(systemctl is-active brains.service)" = active
ss -ltn | grep -q '127.0.0.1:8088'

stage=enqueue_and_replay
for index in 0 1; do
  apply_sql="
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT job_id,intake_terminal_id,status,apply_outcome
FROM memory.enqueue_owner_v5_2_pet_identity_reextract_v1(
  '${operations[$index]}'::uuid,
  '${jobs[$index]}'::uuid,
  '${terminals[$index]}'::uuid,
  '${packets[$index]}'::uuid,
  '${content_sha[$index]}',
  '${storage_sha[$index]}',
  '${reasons[$index]}',
  '$selector',
  '$compiler_version'
);
COMMIT;"
  applied=$(psql "$POSTGRES_DSN" -X -Atq -F '|' -v ON_ERROR_STOP=1 \
    -c "$apply_sql" | grep -E 'applied|replayed')
  test "$applied" = "${jobs[$index]}|${terminals[$index]}|pending|applied"
  replayed=$(psql "$POSTGRES_DSN" -X -Atq -F '|' -v ON_ERROR_STOP=1 \
    -c "$apply_sql" | grep -E 'applied|replayed')
  test "$replayed" = "${jobs[$index]}|${terminals[$index]}|pending|replayed"
done

stage=private_canaries
for index in 0 1; do
  MEMORY_V1_V5_LOCAL_INFERENCE_APPLY=memory_v1_v5_local_inference_canary_apply_v1 \
  MEMORY_V1_LOCAL_INFERENCE_API_KEY="$(<"$api_key_file")" \
  POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$live" \
  "$live/venv/bin/python" "$live/$canary" \
    --owner-user-id "$owner" \
    --evidence-id "${evidence[$index]}" \
    --expected-job-id "${jobs[$index]}" \
    --expected-content-sha256 "${content_sha[$index]}" \
    --selector-version "$selector" \
    --run-id "${runs[$index]}" \
    --contract-profile v5_2 \
    --max-attempts 1 \
    --max-reserved-jobs 100 \
    --failure-threshold 3 \
    --apply \
    >"$artifact_dir/canary-$index.json"
  jq -e '
    .outcome=="accepted"
    and .local_model_calls==1
    and .external_model_calls==0
    and .write_counts.claims==0
    and .write_counts.qdrant==0
    and .write_counts.prompt_influence==0
  ' "$artifact_dir/canary-$index.json" >/dev/null
done

stage=semantic_review
docker exec "$container" psql -U sage -d "$database" -X -Atq \
  -v ON_ERROR_STOP=1 -c "
    SELECT jsonb_build_object(
      'ordinal', evidence.metadata->>'contextual_ordinal',
      'packet_id', packet.packet_id,
      'storage_sha256', packet.packet_storage_sha256,
      'packet', packet.normalized_packet
    )
    FROM memory.evidence_extraction_packet_v5_local AS packet
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=packet.owner_user_id
     AND evidence.evidence_id=packet.evidence_id
    WHERE packet.owner_user_id='$owner'::uuid
      AND packet.job_id IN (
        '${jobs[0]}'::uuid,
        '${jobs[1]}'::uuid
      )
    ORDER BY (evidence.metadata->>'contextual_ordinal')::integer;
  " >"$artifact_dir/packets.jsonl"

"$live/venv/bin/python" - "$artifact_dir/packets.jsonl" <<'PY'
from __future__ import annotations

import json
from pathlib import Path
import sys

rows = [
    json.loads(line)
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    if line.strip()
]
assert [row["ordinal"] for row in rows] == ["0", "3"]
packets = {int(row["ordinal"]): row["packet"] for row in rows}

def one_named(packet: dict, name: str) -> dict:
    values = [
        item
        for item in packet["entity_mentions"]
        if item.get("name_text") == name
    ]
    assert len(values) == 1
    return values[0]

keasha = one_named(packets[0], "Keasha von Steffen Haus")
assert keasha["relationship_role"] == "pet:reported"
assert [item["predicate"] for item in packets[0]["observations"]] == [
    "identity.name"
]
assert not packets[0]["deferrals"]

dahlia = one_named(packets[3], "Dahlia")
assert dahlia["relationship_role"] == "pet:reported"
assert [item["predicate"] for item in packets[3]["observations"]] == [
    "identity.name"
]
assert {item["reason_code"] for item in packets[3]["deferrals"]} == {
    "sensitive_manual_review"
}
assert not any(
    item["predicate"] == "life_event.died"
    for packet in packets.values()
    for item in packet["observations"]
)
print(
    "pet_identity_production_semantics=PASS "
    "keasha_identity=1 dahlia_identity=1 false_deaths=0"
)
PY

test "$(
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND job_id IN ('${jobs[0]}'::uuid, '${jobs[1]}'::uuid)
      AND selector_version='$selector'
      AND status='review_required'
      AND attempts=1;
  "
)" = 2
test "$(
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_packet_v5_local
    WHERE owner_user_id='$owner'::uuid
      AND job_id IN ('${jobs[0]}'::uuid, '${jobs[1]}'::uuid)
      AND local_model_calls=1
      AND external_model_calls=0;
  "
)" = 2

protected_snapshot "$protected_after"
isolation_snapshot "$isolation_after"
cmp "$protected_before" "$protected_after"
cmp "$isolation_before" "$isolation_after"
qdrant_after=$(qdrant_signature)
test "$qdrant_before" = "$qdrant_after"

stage=restore_timers
restore_timers
test "$(systemctl is-active brains.service)" = active

report=$snapshot_dir/memory_v1_v5_2_pet_identity_reextract_$run_tag.json
backup_sha=$(awk '{print $1}' "$backup_dir/memory.dump.sha256")
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head "$target_head" \
  --arg backup "$backup_dir/memory.dump" \
  --arg backup_sha "$backup_sha" \
  --arg qdrant "$qdrant_after" \
  '{
    contract_version:
      "memory_v1_v5_2_pet_identity_reextract_production_v1",
    completed_at:$completed_at,
    head_commit:$head,
    backup:{path:$backup,sha256:$backup_sha},
    scope:{
      owners:1,
      jobs:2,
      packets:2,
      local_model_calls:2,
      external_model_calls:0,
      claims:0,
      staging:0,
      qdrant_writes:0,
      prompt_influence:0
    },
    checks:{
      clone_passed:true,
      fresh_backup:true,
      hash_locked:true,
      replay_zero_write:true,
      owner_isolation:true,
      semantics_passed:true,
      protected_stores_unchanged:true,
      qdrant_unchanged:true,
      timers_restored:true,
      service_healthy:true
    },
    qdrant_sha256:$qdrant,
    hard_stop:"before_packet_supersession_or_routing"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
stage=complete
printf '%s\nreport=%s\nbackup=%s\n' \
  'memory_v1_v5_2_pet_identity_reextract_production: PASS' \
  "$report" "$backup_dir/memory.dump"

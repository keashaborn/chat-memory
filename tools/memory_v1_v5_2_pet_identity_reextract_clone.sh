#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Tests two exact append-only pet identity re-extractions
# in a disposable production clone. Production is never written.

repo=${REPO_ROOT:-/tmp/chat-memory-pet-temporal-integration-v2}
production_repo=/opt/chat-memory
container=brains-postgres-1
source_db=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=673d64a3-c4ba-4d1c-89e3-e0c579022fad
selector=20260729_v5_2_pet_identity_compiler_reextract_v1
compiler_version=memory_v1_semantic_policy_compiler_v8
migration=$repo/ops/sql/20260729_memory_v1_v5_2_pet_identity_reextract.sql
canary=$repo/scripts/memory_v1_v5_local_inference_canary.py
python_bin=/opt/chat-memory/venv/bin/python
api_key_file=/etc/memory-v1-local-inference/api-key
clone_db="memory_pet_identity_reextract_$(date -u +%Y%m%dT%H%M%SZ)_$$"
artifact_dir=$(mktemp -d /tmp/memory-pet-identity-reextract.XXXXXX)
timer_state=$artifact_dir/timers.tsv
protected_before=$artifact_dir/protected-before.tsv
protected_after=$artifact_dir/protected-after.tsv
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
  done <"$timer_state"
  timers_restored=1
}

cleanup() {
  rc=$?
  trap - EXIT
  restore_timers
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
  rm -rf "$artifact_dir"
  if [[ "$rc" != 0 ]]; then
    printf 'memory_v1_v5_2_pet_identity_reextract_clone: FAIL stage=%s rc=%s\n' \
      "$stage" "$rc" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT

test "$(id -u)" -eq 0
test -z "$(git -C "$production_repo" status --porcelain)"
test -z "$(git -C "$repo" status --porcelain)"
test -r "$migration"
test -r "$canary"
test -r "$api_key_file"
test "$(stat -c %a "$api_key_file")" = 600
test "$(systemctl is-active brains.service)" = active

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
  docker exec "$container" psql -U sage -d "$source_db" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE lease_token IS NOT NULL
      AND lease_expires_at > clock_timestamp();
  "
)" = 0

set -a
source "$production_repo/.env"
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
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atq \
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

qdrant_before=$(qdrant_signature)
stage=clone_create
docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"
docker exec -i "$container" psql -U sage -d "$clone_db" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone_db" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" \
  "$python_bin" - <<'PY'
from urllib.parse import urlsplit, urlunsplit
import os

source = urlsplit(os.environ["SOURCE_DSN"])
if source.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("source DSN scheme is invalid")
if source.hostname not in {"127.0.0.1", "::1", "localhost"}:
    raise SystemExit("source DSN is not loopback")
print(urlunsplit((
    source.scheme,
    source.netloc,
    "/" + os.environ["CLONE_DB"],
    source.query,
    source.fragment,
)))
PY
)

protected_snapshot "$protected_before"

stage=plan_and_isolation
for index in 0 1; do
  plan=$(psql "$clone_dsn" -X -Atq -F $'\t' -v ON_ERROR_STOP=1 <<SQL | tail -1
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT prior_packet_id,evidence_id,evidence_content_sha256,
       prior_packet_storage_sha256,reason_code,contextual_ordinal
FROM memory.plan_owner_v5_2_pet_identity_reextract_v1(
  '${packets[$index]}'::uuid
);
ROLLBACK;
SQL
)
  test "$plan" = ${packets[$index]}$'\t'${evidence[$index]}$'\t'${content_sha[$index]}$'\t'${storage_sha[$index]}$'\t'${reasons[$index]}$'\t'$((index * 3))
  other_count=$(psql "$clone_dsn" -X -Atq -v ON_ERROR_STOP=1 <<SQL | tail -1
BEGIN;
SELECT set_config('app.user_id','$other_owner',true);
SELECT count(*)
FROM memory.plan_owner_v5_2_pet_identity_reextract_v1(
  '${packets[$index]}'::uuid
);
ROLLBACK;
SQL
)
  test "$other_count" = 0
done

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
  applied=$(psql "$clone_dsn" -X -Atq -F '|' -v ON_ERROR_STOP=1 \
    -c "$apply_sql" | grep -E 'applied|replayed')
  test "$applied" = "${jobs[$index]}|${terminals[$index]}|pending|applied"
  replayed=$(psql "$clone_dsn" -X -Atq -F '|' -v ON_ERROR_STOP=1 \
    -c "$apply_sql" | grep -E 'applied|replayed')
  test "$replayed" = "${jobs[$index]}|${terminals[$index]}|pending|replayed"
done

test "$(
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND selector_version='$selector'
      AND status='pending'
      AND attempts=0;
  "
)" = 2

stage=private_persisted_canaries
for index in 0 1; do
  MEMORY_V1_V5_LOCAL_INFERENCE_APPLY=memory_v1_v5_local_inference_canary_apply_v1 \
  MEMORY_V1_LOCAL_INFERENCE_API_KEY="$(<"$api_key_file")" \
  POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" \
  "$python_bin" "$canary" \
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

stage=semantic_assertions
docker exec "$container" psql -U sage -d "$clone_db" -X -Atq \
  -v ON_ERROR_STOP=1 -c "
    SELECT jsonb_build_object(
      'ordinal',
        evidence.metadata->>'contextual_ordinal',
      'packet',
        packet.normalized_packet
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

"$python_bin" - "$artifact_dir/packets.jsonl" <<'PY'
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
    matches = [
        entity
        for entity in packet["entity_mentions"]
        if entity.get("name_text") == name
    ]
    assert len(matches) == 1
    return matches[0]

def predicates(packet: dict) -> list[str]:
    return [item["predicate"] for item in packet["observations"]]

keasha = one_named(packets[0], "Keasha von Steffen Haus")
assert keasha["relationship_role"] == "pet:reported"
assert predicates(packets[0]) == ["identity.name"]
assert not packets[0]["deferrals"]

dahlia = one_named(packets[3], "Dahlia")
assert dahlia["relationship_role"] == "pet:reported"
assert predicates(packets[3]) == ["identity.name"]
assert {item["reason_code"] for item in packets[3]["deferrals"]} == {
    "sensitive_manual_review"
}
print(
    "pet_identity_semantics=PASS "
    "keasha_identity=1 dahlia_identity=1 false_deaths=0"
)
PY

test "$(
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND job_id IN ('${jobs[0]}'::uuid,'${jobs[1]}'::uuid)
      AND status='review_required'
      AND attempts=1;
  "
)" = 2
test "$(
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_packet_v5_local
    WHERE owner_user_id='$owner'::uuid
      AND job_id IN ('${jobs[0]}'::uuid,'${jobs[1]}'::uuid)
      AND local_model_calls=1
      AND external_model_calls=0;
  "
)" = 2

protected_snapshot "$protected_after"
cmp "$protected_before" "$protected_after"
qdrant_after=$(qdrant_signature)
test "$qdrant_before" = "$qdrant_after"

restore_timers
stage=final_health
test "$(systemctl is-active brains.service)" = active
printf '%s\n' \
  'memory_v1_v5_2_pet_identity_reextract_clone: PASS' \
  'new_jobs=2 replay_writes=0 local_model_calls=2 external_model_calls=0' \
  'claims=0 staging=0 qdrant=0 prompt_influence=0 cross_owner_visible=0'

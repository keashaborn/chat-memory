#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the clone-tested review-disposition contract and
# applies exactly six hash-locked, owner-scoped, non-promotable decisions.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for backup and timer control' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_REVIEW_DISPOSITION_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_REVIEW_DISPOSITION_APPLY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
production_repo=/opt/chat-memory
required_production=d4f30cbbf0b74bf70437d216dce9799774b29e95
target_commit=${MEMORY_V1_V5_2_REVIEW_DISPOSITION_TARGET_COMMIT:?target commit is required}
container=brains-postgres-1
database=memory
backup_root=/var/backups/chat-memory
migration=ops/sql/20260730_memory_v1_v5_2_review_disposition.sql
rollback=ops/sql/20260730_memory_v1_v5_2_review_disposition_rollback.sql
manifest=evals/memory_v1_v5_2_review_disposition_exact_six_20260730.json
clone_harness=tools/memory_v1_v5_2_review_disposition_clone.sh
expected_migration_sha=9b7291ccb1fd09d5ac86d3bee853553fd562ab0b0c282ef0bfaa63b77990b596
expected_rollback_sha=d32ed923b32ecc64fbaf29ad7e5de823386cca61030101fd6b700fe939d823e0
expected_manifest_sha=bca0caa34ef69bce0c4926366a3eba8cc99b8a877f4fecebcad88ac26af7c3fc
expected_clone_sha=d718fca4c5247a8ffe0890206c639e9b0010a967f1723e9737dbe1c409b79b08

timer_state=$(mktemp /tmp/memory-v5-2-review-disposition.XXXXXX.timers)
work=$(mktemp -d /tmp/memory-v5-2-review-disposition.XXXXXX)
chmod 0600 "$timer_state"
chmod 0700 "$work"
timers_quiesced=0

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
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
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null
    elif [[ "$enabled" == disabled ]]; then
      systemctl disable "$unit" >/dev/null 2>&1 || true
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit" 2>/dev/null || true)" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

cleanup() {
  rc=$?
  trap - EXIT
  restore_timers || rc=1
  rm -rf "$work"
  rm -f "$timer_state"
  exit "$rc"
}
trap cleanup EXIT

cd "$repo_root"
[[ "$(git rev-parse HEAD)" == "$target_commit" ]]
[[ -z "$(git status --porcelain)" ]]
[[ "$(git -C "$production_repo" rev-parse HEAD)" == "$required_production" ]]
[[ -z "$(git -C "$production_repo" status --porcelain)" ]]
git merge-base --is-ancestor "$required_production" "$target_commit"
[[ "$(sha256sum "$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$manifest" | awk '{print $1}')" == "$expected_manifest_sha" ]]
[[ "$(sha256sum "$clone_harness" | awk '{print $1}')" == "$expected_clone_sha" ]]
bash -n "$0"
"$clone_harness"

jq -e '
  .contract_version=="memory_v1_v5_2_review_disposition_manifest_v1" and
  (.items | length)==6 and
  ([.items[].packet_id] | unique | length)==6
' "$manifest" >/dev/null

dispositions_before=$(scalar \
  'SELECT count(*) FROM memory.v5_local_packet_disposition')
stage_before=$(scalar 'SELECT count(*) FROM memory.relational_stage_batch')
claims_before=$(scalar 'SELECT count(*) FROM memory.claim')
qdrant_before=$(qdrant_signature)

while IFS= read -r item; do
  owner=$(jq -r '.owner_user_id' <<<"$item")
  packet=$(jq -r '.packet_id' <<<"$item")
  packet_sha=$(jq -r '.packet_storage_sha256' <<<"$item")
  [[ "$(scalar "
    SELECT count(*)
    FROM memory.evidence_extraction_packet_v5_local AS packet
    JOIN memory.v5_2_local_packet_route_event AS route
      ON route.owner_user_id=packet.owner_user_id
     AND route.packet_id=packet.packet_id
     AND route.route='manual_review_artifact_ready'
    WHERE packet.owner_user_id='$owner'::uuid
      AND packet.packet_id='$packet'::uuid
      AND packet.packet_storage_sha256='$packet_sha'
  ")" == 1 ]]
  [[ "$(scalar "
    SELECT count(*)
    FROM memory.v5_local_packet_disposition
    WHERE owner_user_id='$owner'::uuid
      AND packet_id='$packet'::uuid
  ")" == 0 ]]
done < <(jq -c '.items[]' "$manifest")

[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE packet_id='1f7fe393-afc3-5982-b69c-c90877659ef6'::uuid
")" == 0 ]]

while IFS= read -r unit; do
  enabled=$(systemctl is-enabled "$unit" 2>/dev/null || true)
  active=$(systemctl is-active "$unit" 2>/dev/null || true)
  printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active" >>"$timer_state"
done < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u
)
[[ -s "$timer_state" ]]
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$timer_state"

mkdir -p "$backup_root"
run_tag=$(date -u +%Y%m%dT%H%M%SZ)
backup="$backup_root/memory_pre_v5_2_review_disposition_${run_tag}.dump"
backup_sha="$backup.sha256"
umask 077
docker exec "$container" pg_dump -U sage -d "$database" -Fc >"$backup"
[[ -s "$backup" ]]
sha256sum "$backup" >"$backup_sha"
chmod 0600 "$backup" "$backup_sha"
umask 022

sudo -u ubuntu git -C "$production_repo" merge --ff-only "$target_commit"
[[ "$(git -C "$production_repo" rev-parse HEAD)" == "$target_commit" ]]
[[ -z "$(git -C "$production_repo" status --porcelain)" ]]
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$production_repo/$migration" >/dev/null

set -a
source "$production_repo/.env"
set +a

while IFS= read -r item; do
  owner=$(jq -r '.owner_user_id' <<<"$item")
  packet=$(jq -r '.packet_id' <<<"$item")
  packet_sha=$(jq -r '.packet_storage_sha256' <<<"$item")
  operation=$(jq -r '.operation_id' <<<"$item")
  disposition=$(jq -r '.disposition_id' <<<"$item")
  decision=$(jq -r '.review_decision' <<<"$item")
  reason=$(jq -r '.reason_code' <<<"$item")
  basis=$(jq -r '.review_basis_sha256' <<<"$item")

  psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
    -v owner="$owner" -v packet="$packet" -v packet_sha="$packet_sha" \
    -v operation="$operation" -v disposition="$disposition" \
    -v decision="$decision" -v reason="$reason" -v basis="$basis" \
    <<'SQL' >"$work/$packet.apply"
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT *
FROM memory.finalize_owner_v5_2_review_disposition_v1(
  :'operation'::uuid,
  :'disposition'::uuid,
  :'packet'::uuid,
  :'packet_sha',
  :'decision',
  :'reason',
  :'basis'
);
COMMIT;
SQL
  grep -q 'applied' "$work/$packet.apply"

  psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
    -v owner="$owner" -v packet="$packet" -v packet_sha="$packet_sha" \
    -v operation="$operation" -v disposition="$disposition" \
    -v decision="$decision" -v reason="$reason" -v basis="$basis" \
    <<'SQL' >"$work/$packet.replay"
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT *
FROM memory.finalize_owner_v5_2_review_disposition_v1(
  :'operation'::uuid,
  :'disposition'::uuid,
  :'packet'::uuid,
  :'packet_sha',
  :'decision',
  :'reason',
  :'basis'
);
COMMIT;
SQL
  grep -q 'replayed' "$work/$packet.replay"
done < <(jq -c '.items[]' "$manifest")

[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" \
  == "$((dispositions_before + 6))" ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE reason_code='semantic_predicate_misclassification'
    AND review_decision='rejected' AND NOT promotion_eligible
")" == 2 ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE reason_code='turn_local_instruction_not_durable'
    AND review_decision='rejected' AND NOT promotion_eligible
")" == 2 ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE reason_code='hypothetical_or_scenario_not_durable'
    AND review_decision='rejected' AND NOT promotion_eligible
")" == 1 ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE reason_code='contextual_state_not_durable'
    AND review_decision='deferred' AND NOT promotion_eligible
")" == 1 ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE packet_id='1f7fe393-afc3-5982-b69c-c90877659ef6'::uuid
")" == 0 ]]
[[ "$(scalar 'SELECT count(*) FROM memory.relational_stage_batch')" \
  == "$stage_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

first=$(jq -c '.items[0]' "$manifest")
packet=$(jq -r '.packet_id' <<<"$first")
packet_sha=$(jq -r '.packet_storage_sha256' <<<"$first")
operation=$(jq -r '.operation_id' <<<"$first")
disposition=$(jq -r '.disposition_id' <<<"$first")
decision=$(jq -r '.review_decision' <<<"$first")
reason=$(jq -r '.reason_code' <<<"$first")
basis=$(jq -r '.review_basis_sha256' <<<"$first")
if psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  -v owner=9dd7426d-77eb-4765-9db2-13e33ad7444d \
  -v packet="$packet" -v packet_sha="$packet_sha" \
  -v operation="$operation" -v disposition="$disposition" \
  -v decision="$decision" -v reason="$reason" -v basis="$basis" \
  <<'SQL' >/dev/null 2>&1
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT *
FROM memory.finalize_owner_v5_2_review_disposition_v1(
  :'operation'::uuid, :'disposition'::uuid, :'packet'::uuid, :'packet_sha',
  :'decision', :'reason', :'basis'
);
ROLLBACK;
SQL
then
  echo 'cross-owner reviewed disposition succeeded' >&2
  exit 1
fi

if docker exec "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    UPDATE memory.v5_local_packet_disposition
    SET reason_code=reason_code
    WHERE disposition_id='d72c9b62-3f00-4dd0-8c71-05db3fde3b65'::uuid
  " >/dev/null 2>&1; then
  echo 'append-only disposition accepted an update' >&2
  exit 1
fi

[[ "$(systemctl is-active brains.service)" == active ]]
curl --fail --silent --show-error --max-time 30 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null

restore_timers

printf 'PRODUCTION_HEAD=%s\n' "$target_commit"
printf 'BACKUP=%s\n' "$backup"
printf 'BACKUP_SHA256=%s\n' "$(awk '{print $1}' "$backup_sha")"
printf 'SCHEMA_ROLLBACK=%s\n' "$production_repo/$rollback"
printf 'SCHEMA_ROLLBACK_SHA256=%s\n' "$expected_rollback_sha"
printf 'DISPOSITIONS=6\n'
printf 'REJECTED=5\n'
printf 'DEFERRED=1\n'
printf 'VALID_NAME_PACKET_UNCHANGED=1\n'
printf 'STAGE_WRITES=0\n'
printf 'CLAIM_WRITES=0\n'
printf 'QDRANT_WRITES=0\n'
printf 'CROSS_OWNER_VISIBLE=0\n'
printf 'ZERO_WRITE_REPLAY=proved\n'
printf 'SERVICE_HEALTH=ok\n'
printf 'TIMERS_RESTORED=exact\n'
printf 'memory_v1_v5_2_review_disposition_production_apply: PASS\n'

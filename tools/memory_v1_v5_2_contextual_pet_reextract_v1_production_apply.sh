#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the exact contextual-pet re-extraction function,
# runs rollback-only security tests, and appends exactly five queue triplets.

if [[ ${EUID} -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 2
fi
if [[ "${MEMORY_V1_CONTEXTUAL_PET_REEXTRACT_AUTHORIZED:-}" != authorized ]]; then
  echo 'contextual pet re-extraction authorization is required' >&2
  exit 2
fi

repo=/opt/chat-memory
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
selector=20260727_v5_2_contextual_pet_reextract_v1
manifest=7fdfbf9e8b07e482f5b25a3f8b49353fdd393727d78e93118c27415e1ef37566
provider_sha=6c04a8243f1c0281776f05e90443f3dec1cd2451d5d2238e4c39391baf20fe21
relationship_sha=767a4736fd4c4c97b33f01c38652c15c9cc9f574c1e7c1bc8d7a42d5946cc0d6
migration="$repo/ops/sql/20260727_memory_v1_v5_2_contextual_pet_reextract_v1.sql"
rollback="$repo/ops/sql/20260727_memory_v1_v5_2_contextual_pet_reextract_v1_rollback.sql"
test_sql="$repo/tests/memory_v1_v5_2_contextual_pet_reextract_v1.sql"
manifest_file="$repo/evals/memory_v1_v5_2_contextual_pet_reextract_manifest_20260727.json"
clone_tool="$repo/tools/memory_v1_v5_2_contextual_pet_reextract_v1_clone.sh"
expected_migration_sha=4bd5319410f09f3dd20370e7cee535f7c74e2f7f2f693ebfbcf149d06dcca5e5
expected_rollback_sha=39a10cec6e8ee7f99d04a6371e1a29b61d70f1a352ff8b47347666166c3d6450
expected_test_sha=fffa55df9150e90324c959d4a2ebb4a1fd13fa15e120019f7cacdd820466c11a
expected_clone_sha=931499af8ade7342eca0bad0081069dac3a17dd103aac49bca1864919c36f5c5
snapshot_dir=/home/ubuntu/brains/snapshots
run_tag=$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo" rev-parse --short=12 HEAD)
backup="$snapshot_dir/memory_pre_contextual_pet_reextract_${run_tag}.dump"
timer_state=$(mktemp /tmp/memory-contextual-pet-timers.XXXXXX.tsv)
chmod 0600 "$timer_state"
timers_restored=0

scalar() {
  local query=$1
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "$query"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

old_target_signature() {
  scalar "
    SELECT encode(public.digest(convert_to(coalesce(string_agg(
      value,E'\\n' ORDER BY value
    ),''),'UTF8'),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(job)::text AS value
      FROM memory.evidence_extraction_job AS job
      WHERE job.owner_user_id='$owner'::uuid
        AND job.job_id IN (
          '8b1b36c4-2a7b-43a3-bdbb-97d17749e1f6'::uuid,
          '1349d208-d1b4-476d-8379-f49c84870543'::uuid,
          'a4d6a327-372c-4776-9411-bcb1b58ac121'::uuid,
          '079e5f54-e0c3-4051-976c-a47593a3ab6b'::uuid,
          '13ac1f7e-a09e-426e-98e8-d963d81f02a8'::uuid
        )
      UNION ALL
      SELECT to_jsonb(packet)::text
      FROM memory.evidence_extraction_packet_v5_local AS packet
      WHERE packet.owner_user_id='$owner'::uuid
        AND packet.evidence_id IN (
          '03323af1-c5b1-509a-81be-31f996636cc5'::uuid,
          '0522d532-6b88-5d00-9980-6ea24d0b1af4'::uuid,
          '2919855e-cf22-5095-93b4-a881e93b54c0'::uuid,
          '3e59b50f-8e5f-5e65-a487-72f5d482a19c'::uuid,
          '5480aacd-e5f3-5060-8c6a-95ef0e46c9fe'::uuid
        )
    ) AS rows"
}

other_owner_signature() {
  scalar "
    SELECT encode(public.digest(convert_to(coalesce(string_agg(
      value,E'\\n' ORDER BY value
    ),''),'UTF8'),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(job)::text AS value
      FROM memory.evidence_extraction_job AS job
      WHERE job.owner_user_id='$other'::uuid
      UNION ALL
      SELECT to_jsonb(terminal)::text
      FROM memory.evidence_intake_terminal AS terminal
      WHERE terminal.owner_user_id='$other'::uuid
      UNION ALL
      SELECT to_jsonb(event)::text
      FROM memory.evidence_extraction_event AS event
      WHERE event.owner_user_id='$other'::uuid
      UNION ALL
      SELECT to_jsonb(packet)::text
      FROM memory.evidence_extraction_packet_v5_local AS packet
      WHERE packet.owner_user_id='$other'::uuid
    ) AS rows"
}

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
  rm -f "$timer_state"
  exit "$rc"
}
trap cleanup EXIT

test -z "$(git -C "$repo" status --short)"
test "$(sha256sum "$migration" | awk '{print $1}')" = "$expected_migration_sha"
test "$(sha256sum "$rollback" | awk '{print $1}')" = "$expected_rollback_sha"
test "$(sha256sum "$test_sql" | awk '{print $1}')" = "$expected_test_sha"
test "$(sha256sum "$clone_tool" | awk '{print $1}')" = "$expected_clone_sha"
test "$(sha256sum "$manifest_file" | awk '{print $1}')" = "$manifest"
test "$(sha256sum "$repo/scripts/memory_v1_relational_extraction_v5_local_provider.py" |
  awk '{print $1}')" = "$provider_sha"
test "$(sha256sum "$repo/scripts/memory_v1_relationship_observation_v5_1.py" |
  awk '{print $1}')" = "$relationship_sha"
test "$(scalar "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" = 0

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

mkdir -p "$snapshot_dir"
docker exec "$container" pg_dump -U sage -d "$database" -Fc \
  --no-owner --no-privileges >"$backup"
test -s "$backup"
chmod 0600 "$backup"

job_before=$(scalar "SELECT count(*) FROM memory.evidence_extraction_job")
terminal_before=$(scalar "SELECT count(*) FROM memory.evidence_intake_terminal")
event_before=$(scalar "SELECT count(*) FROM memory.evidence_extraction_event")
packet_before=$(scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local")
claim_before=$(scalar "SELECT count(*) FROM memory.claim")
observation_before=$(scalar "SELECT count(*) FROM memory.observation")
old_target_before=$(old_target_signature)
other_before=$(other_owner_signature)
qdrant_before=$(qdrant_signature)

docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v other_owner="$other" \
  -v manifest_sha256="$manifest" \
  <"$test_sql" >/dev/null
test "$(scalar "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" = 0

apply_result=$(
  docker exec -i "$container" psql -U sage -d "$database" -X -At \
    -v ON_ERROR_STOP=1 <<SQL
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT concat_ws('|',apply_outcome,job_count,terminal_count,event_count)
FROM memory.enqueue_owner_v5_2_contextual_pet_reextract_v1('$manifest');
COMMIT;
SQL
)
test "$(printf '%s\n' "$apply_result" | grep '^applied|')" = 'applied|5|5|5'

replay_result=$(
  docker exec -i "$container" psql -U sage -d "$database" -X -At \
    -v ON_ERROR_STOP=1 <<SQL
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT concat_ws('|',apply_outcome,job_count,terminal_count,event_count)
FROM memory.enqueue_owner_v5_2_contextual_pet_reextract_v1('$manifest');
COMMIT;
SQL
)
test "$(printf '%s\n' "$replay_result" | grep '^replayed|')" = \
  'replayed|5|5|5'

test "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job")" = \
  "$((job_before+5))"
test "$(scalar "SELECT count(*) FROM memory.evidence_intake_terminal")" = \
  "$((terminal_before+5))"
test "$(scalar "SELECT count(*) FROM memory.evidence_extraction_event")" = \
  "$((event_before+5))"
test "$(scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local")" = \
  "$packet_before"
test "$(scalar "SELECT count(*) FROM memory.claim")" = "$claim_before"
test "$(scalar "SELECT count(*) FROM memory.observation")" = "$observation_before"
test "$(scalar "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND selector_version='$selector'
    AND status='pending' AND attempts=0")" = 5
test "$(scalar "
  SELECT count(*) FROM memory.evidence_intake_terminal
  WHERE owner_user_id='$owner'::uuid
    AND selector_version='$selector'
    AND details->>'manifest_sha256'='$manifest'")" = 5
test "$(scalar "
  SELECT count(*) FROM memory.evidence_extraction_event AS event
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=event.owner_user_id
   AND job.job_id=event.job_id
  WHERE event.owner_user_id='$owner'::uuid
    AND job.selector_version='$selector'
    AND event.event_type='queued'")" = 5
test "$(old_target_signature)" = "$old_target_before"
test "$(other_owner_signature)" = "$other_before"
test "$(qdrant_signature)" = "$qdrant_before"
test "$(systemctl is-active brains.service)" = active

restore_timers

printf '%s\n' \
  'memory_v1_v5_2_contextual_pet_reextract_v1_production: PASS' \
  "head=$(git -C "$repo" rev-parse HEAD)" \
  "backup=$backup" \
  "manifest_sha256=$manifest" \
  'new_jobs=5' \
  'new_terminals=5' \
  'new_events=5' \
  'replay_writes=0' \
  'old_jobs_unchanged=true' \
  'other_owner_unchanged=true' \
  'packets_unchanged=true' \
  'claims_unchanged=true' \
  'observations_unchanged=true' \
  'qdrant_unchanged=true' \
  'timers_restored=true' \
  'brains_active=true'

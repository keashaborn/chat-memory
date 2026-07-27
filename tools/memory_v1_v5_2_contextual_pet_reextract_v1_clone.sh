#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs and applies the exact five-record append-only
# re-extraction batch on a disposable production clone.

repo=/opt/chat-memory
container=brains-postgres-1
production=memory
clone="memory_contextual_pet_reextract_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
selector=20260727_v5_2_contextual_pet_reextract_v1
manifest=7fdfbf9e8b07e482f5b25a3f8b49353fdd393727d78e93118c27415e1ef37566
provider_sha=6c04a8243f1c0281776f05e90443f3dec1cd2451d5d2238e4c39391baf20fe21
relationship_sha=767a4736fd4c4c97b33f01c38652c15c9cc9f574c1e7c1bc8d7a42d5946cc0d6
migration="$repo/ops/sql/20260727_memory_v1_v5_2_contextual_pet_reextract_v1.sql"
test_sql="$repo/tests/memory_v1_v5_2_contextual_pet_reextract_v1.sql"
manifest_file="$repo/evals/memory_v1_v5_2_contextual_pet_reextract_manifest_20260727.json"
backup=$(mktemp /tmp/memory-contextual-pet-reextract.XXXXXX.dump)
chmod 0600 "$backup"

cleanup() {
  rc=$?
  trap - EXIT
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  exit "$rc"
}
trap cleanup EXIT

scalar() {
  local database=$1
  local query=$2
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

target_signature() {
  local database=$1
  scalar "$database" "
    SELECT encode(public.digest(convert_to(coalesce(string_agg(
      value,E'\\n' ORDER BY value
    ),''),'UTF8'),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(evidence)::text AS value
      FROM memory.evidence AS evidence
      WHERE evidence.owner_user_id='$owner'::uuid
        AND evidence.evidence_id IN (
          '03323af1-c5b1-509a-81be-31f996636cc5'::uuid,
          '0522d532-6b88-5d00-9980-6ea24d0b1af4'::uuid,
          '2919855e-cf22-5095-93b4-a881e93b54c0'::uuid,
          '3e59b50f-8e5f-5e65-a487-72f5d482a19c'::uuid,
          '5480aacd-e5f3-5060-8c6a-95ef0e46c9fe'::uuid
        )
      UNION ALL
      SELECT to_jsonb(job)::text
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

test -z "$(git -C "$repo" status --short)"
test "$(sha256sum "$manifest_file" | awk '{print $1}')" = "$manifest"
test "$(sha256sum "$repo/scripts/memory_v1_relational_extraction_v5_local_provider.py" |
  awk '{print $1}')" = "$provider_sha"
test "$(sha256sum "$repo/scripts/memory_v1_relationship_observation_v5_1.py" |
  awk '{print $1}')" = "$relationship_sha"

production_before=$(target_signature "$production")
qdrant_before=$(qdrant_signature)
selector_before=$(scalar "$production" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")
test "$selector_before" = 0

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc --no-owner --no-privileges >"$backup"
test -s "$backup"
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"
docker exec "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 -c \
  'GRANT USAGE ON SCHEMA memory TO
     brains_app,memory_v5_local_reextract_maintainer;
   GRANT SELECT ON
     memory.evidence,
     memory.evidence_extraction_job,
     memory.evidence_intake_terminal,
     memory.evidence_extraction_event,
     memory.relational_stage_batch
     TO memory_v5_local_reextract_maintainer;
   GRANT INSERT ON
     memory.evidence_extraction_job,
     memory.evidence_intake_terminal,
     memory.evidence_extraction_event
     TO memory_v5_local_reextract_maintainer' >/dev/null

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v other_owner="$other" \
  -v manifest_sha256="$manifest" \
  <"$test_sql" >/dev/null

first_result=$(
  docker exec -i "$container" psql -U sage -d "$clone" -X -At \
    -v ON_ERROR_STOP=1 <<SQL
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT concat_ws('|',apply_outcome,job_count,terminal_count,event_count)
FROM memory.enqueue_owner_v5_2_contextual_pet_reextract_v1('$manifest');
COMMIT;
SQL
)
test "$(printf '%s\n' "$first_result" | grep '^applied|')" = 'applied|5|5|5'

replay_result=$(
  docker exec -i "$container" psql -U sage -d "$clone" -X -At \
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

test "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND selector_version='$selector'
    AND status='pending' AND attempts=0")" = 5
test "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_intake_terminal
  WHERE owner_user_id='$owner'::uuid
    AND selector_version='$selector'
    AND details->>'manifest_sha256'='$manifest'")" = 5
test "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id<>'$owner'::uuid
    AND selector_version='$selector'")" = 0
test "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND job_id IN (
      '8b1b36c4-2a7b-43a3-bdbb-97d17749e1f6'::uuid,
      '1349d208-d1b4-476d-8379-f49c84870543'::uuid,
      'a4d6a327-372c-4776-9411-bcb1b58ac121'::uuid,
      '079e5f54-e0c3-4051-976c-a47593a3ab6b'::uuid,
      '13ac1f7e-a09e-426e-98e8-d963d81f02a8'::uuid
    )
    AND (
      (job_id='8b1b36c4-2a7b-43a3-bdbb-97d17749e1f6'::uuid
        AND status='review_required' AND attempts=2)
      OR
      (job_id<>'8b1b36c4-2a7b-43a3-bdbb-97d17749e1f6'::uuid
        AND status='pending' AND attempts=1)
    )")" = 5

test "$(target_signature "$production")" = "$production_before"
test "$(scalar "$production" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" = "$selector_before"
test "$(qdrant_signature)" = "$qdrant_before"
test "$(systemctl is-active brains.service)" = active

printf '%s\n' \
  'memory_v1_v5_2_contextual_pet_reextract_v1_clone: PASS' \
  'new_jobs=5' \
  'new_terminals=5' \
  'new_events=5' \
  'replay_writes=0' \
  'production_unchanged=true' \
  'other_owners_unchanged=true' \
  'qdrant_unchanged=true' \
  'clone_deleted=true'

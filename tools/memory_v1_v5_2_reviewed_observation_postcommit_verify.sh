#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Verify the committed three-observation production
# milestone against its pre-write backup without repeating any write.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 1
fi
if [[ $# -ne 2 ]]; then
  echo 'usage: memory_v1_v5_2_reviewed_observation_postcommit_verify.sh BACKUP EXPECTED_QDRANT_SHA256' >&2
  exit 1
fi

backup=$1
expected_qdrant_sha256=$2
container=brains-postgres-1
production=memory
clone="memory_reviewed_observation_verify_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
work=$(mktemp -d /tmp/memory-reviewed-observation-verify.XXXXXX)
report=/home/ubuntu/brains/snapshots/memory_v1_v5_2_reviewed_observation_postcommit_$(date -u +%Y%m%dT%H%M%SZ).json

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists "$clone" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

[[ -s "$backup" ]]
[[ -f "$backup.sha256" ]]
sha256sum -c "$backup.sha256" >/dev/null
[[ "$expected_qdrant_sha256" =~ ^[0-9a-f]{64}$ ]]

docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --no-owner --no-privileges <"$backup"

scalar() {
  local database=$1 sql=$2
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$sql" | tr -d '[:space:]'
}

table_hash() {
  local database=$1 table=$2
  scalar "$database" "
    SELECT encode(public.digest(convert_to(coalesce(string_agg(
      row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(value)::text AS row_json
      FROM memory.\"$table\" AS value
    ) AS rows
  "
}

protected_tables=(
  entity
  entity_alias
  entity_alias_observation
  entity_mention
  observation
  observation_entity_binding
  observation_temporal
  claim
  claim_assessment
  claim_evidence
  claim_observation
  claim_relation
  claim_relation_v5
  claim_revision
  projection_apply_event
  projection_claim_payload
  projection_dispatch_v5
  projection_outbox
  projection_plan
  projection_plan_item
  projection_plan_observation
  projection_plan_relation
  projection_preference_payload
  projection_project_payload
  projection_review
)

for table in "${protected_tables[@]}"; do
  [[ "$(table_hash "$clone" "$table")" == \
    "$(table_hash "$production" "$table")" ]]
done

delta() {
  local table=$1 predicate=${2:-TRUE}
  local before after
  before=$(scalar "$clone" "SELECT count(*) FROM memory.\"$table\" WHERE $predicate")
  after=$(scalar "$production" "SELECT count(*) FROM memory.\"$table\" WHERE $predicate")
  printf '%s' "$((after - before))"
}

for table in v5_local_packet_stage_admission \
  v5_local_entailment_assessment observation_entailment_v5 \
  relational_operation_request; do
  [[ "$(delta "$table")" == 3 ]]
  [[ "$(delta "$table" "owner_user_id='$owner'::uuid")" == 3 ]]
  [[ "$(delta "$table" "owner_user_id<>'$owner'::uuid")" == 0 ]]
done

[[ "$(scalar "$production" "
  SELECT count(*)
  FROM memory.v5_2_reviewed_observation_stage_admission
  WHERE admission_id=ANY(ARRAY[
    '35febb7e-0993-5ddf-b9b0-71a22d1b8501'::uuid,
    '28f7e091-252e-5357-b64f-144f6445e2e8'::uuid,
    'daa5a659-ba7c-5841-adb2-6aaa7c0481ae'::uuid
  ])
    AND owner_user_id='$owner'::uuid
    AND (
      admission_id<>'daa5a659-ba7c-5841-adb2-6aaa7c0481ae'::uuid
        AND decision='v5_2_atom_reviewed_stage'
      OR admission_id='daa5a659-ba7c-5841-adb2-6aaa7c0481ae'::uuid
        AND decision='v5_2_reviewed_route_stage'
    )
")" == 3 ]]

[[ "$(scalar "$production" "
  SELECT count(*)
  FROM memory.v5_local_entailment_assessment
  WHERE owner_user_id='$owner'::uuid
    AND governed_decision='accepted'
    AND observation_id=ANY(ARRAY[
      '917ab793-6f03-4af4-847b-c87f5632fa91'::uuid,
      'c0194481-bed5-438f-9407-07e398f14e50'::uuid,
      '14e21c6b-1728-439b-9613-7d9b933d33b8'::uuid
    ])
")" == 3 ]]

qdrant_sha256=$(
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
)
[[ "$qdrant_sha256" == "$expected_qdrant_sha256" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git -C /opt/chat-memory rev-parse HEAD)" \
  --arg backup "$backup" \
  --arg qdrant_sha256 "$qdrant_sha256" \
  '{
    contract_version:"memory_v1_v5_2_reviewed_observation_postcommit_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    backup:$backup,
    results:{
      admissions:3,
      accepted_entailment_assessments:3,
      protected_store_changes:0,
      cross_owner_changes:0,
      claim_changes:0,
      projection_changes:0,
      qdrant_changes:0,
      repeated_writes:0
    },
    qdrant_sha256:$qdrant_sha256
  }' >"$report"
chmod 0600 "$report"

printf '%s\n' \
  'REVIEWED_OBSERVATION_POSTCOMMIT=PASS' \
  "REPORT=$report" \
  "QDRANT_SHA256=$qdrant_sha256"

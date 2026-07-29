#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable database, installs
# the generic claim temporal-reconciliation path, revises exactly the existing
# Neko pet relationship in the clone, proves replay and isolation, and drops
# the clone. Production and Qdrant remain read-only.

if [[ "$#" -ne 1 ]]; then
  echo 'usage: memory_v1_v5_2_claim_temporal_reconciliation_clone.sh ARTIFACT_DIR' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
artifact_dir=$(realpath -m "$1")
review_root=/home/ubuntu/memory-v1-reviews
container=brains-postgres-1
source_db=memory
clone_db="memory_claim_temporal_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
claim=bd20dd0a-9fa0-4a21-8a93-e828c8044150
observation=bc8866ad-95e8-4413-832e-813f601eece6
death_claim=186335e4-ef19-43fb-ad05-f350c18461c1
review_request=aeb81d43-ec6f-4c4f-9a55-a5300a018394
apply_request=10f1d718-a8c6-447a-a15b-c8ec026e847f
migration=ops/sql/20260729_memory_v1_v5_2_claim_temporal_reconciliation.sql
rollback=ops/sql/20260729_memory_v1_v5_2_claim_temporal_reconciliation_rollback.sql
security_test=tests/memory_v1_v5_2_claim_temporal_reconciliation_security.sql
reason_codes='["historical_relationship_reviewed","supported_terminal_life_event","bounded_temporal_upper_bound"]'
rationale='The governed historical pet observation and supported death claim supersede the current-tense rendering while preserving the relationship claim identity.'
reviewer_ref=memory_v1_v5_2_claim_temporal_reconciliation_20260729

[[ "$artifact_dir" == "$review_root"/* ]]
[[ ! -e "$artifact_dir" ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
for path in "$migration" "$rollback" "$security_test"; do
  [[ -f "$repo_root/$path" ]]
done
mkdir -m 0700 "$artifact_dir"

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

production_signature() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$source_db" -c "
      SELECT encode(public.digest(convert_to(
        jsonb_build_object(
          'claim',(SELECT to_jsonb(c) FROM memory.claim c
            WHERE owner_user_id='$owner'::uuid
              AND claim_id='$claim'::uuid),
          'revision_count',(SELECT count(*) FROM memory.claim_revision
            WHERE owner_user_id='$owner'::uuid
              AND claim_id='$claim'::uuid),
          'link_count',(SELECT count(*) FROM memory.claim_observation
            WHERE owner_user_id='$owner'::uuid
              AND claim_id='$claim'::uuid),
          'request_count',(SELECT count(*)
            FROM memory.relational_operation_request),
          'review_table',to_regclass(
            'memory.claim_temporal_reconciliation_review_v5_2'),
          'apply_table',to_regclass(
            'memory.claim_temporal_reconciliation_apply_v5_2')
        )::text,'UTF8'),'sha256'),'hex')
    "
}

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

qdrant_before=$(qdrant_signature)
production_before=$(production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)

docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"
docker exec -i "$container" psql -X -q -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" <"$repo_root/$migration"
docker exec -i "$container" psql -X -q -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" <"$repo_root/$migration"
docker exec -i "$container" psql -X -q -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" <"$repo_root/$security_test"

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone_db" -c "$1" | sed -n '1p'
}

claim_before=$(scalar "
  SELECT encode(public.digest(convert_to(to_jsonb(c)::text,'UTF8'),
    'sha256'),'hex')
  FROM memory.claim c
  WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid")
old_revisions_before=$(scalar "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(
    to_jsonb(r)::text,E'\\n' ORDER BY revision_number),''),
    'UTF8'),'sha256'),'hex')
  FROM memory.claim_revision r
  WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid")
other_owner_before=$(scalar "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(
    row_json,E'\\n' ORDER BY row_json),''),
    'UTF8'),'sha256'),'hex')
  FROM (
    SELECT to_jsonb(c)::text AS row_json FROM memory.claim c
    WHERE owner_user_id='$other_owner'::uuid
    UNION ALL
    SELECT to_jsonb(r)::text FROM memory.claim_revision r
    WHERE owner_user_id='$other_owner'::uuid
    UNION ALL
    SELECT to_jsonb(l)::text FROM memory.claim_observation l
    WHERE owner_user_id='$other_owner'::uuid
  ) rows")
protected_before=$(scalar "
  SELECT jsonb_build_object(
    'claim_count',(SELECT count(*) FROM memory.claim),
    'assessment_count',(SELECT count(*) FROM memory.claim_assessment),
    'outbox_count',(SELECT count(*) FROM memory.projection_outbox),
    'entity_count',(SELECT count(*) FROM memory.entity),
    'observation_count',(SELECT count(*) FROM memory.observation)
  )::text")

apply_log="$artifact_dir/clone-apply.log"
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" \
  -v owner="$owner" -v other_owner="$other_owner" \
  -v claim="$claim" -v observation="$observation" \
  -v death_claim="$death_claim" \
  -v review_request="$review_request" -v apply_request="$apply_request" \
  -v reason_codes="$reason_codes" -v rationale="$rationale" \
  -v reviewer_ref="$reviewer_ref" >"$apply_log" 2>&1 <<'SQL'
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', true);

SELECT * FROM memory.preflight_claim_temporal_reconciliation_v5_2(
  :'claim'::uuid,:'observation'::uuid
) \gset pre_
SELECT 1 / ((:'pre_current_status'='supported')::integer);
SELECT 1 / ((:'pre_from_temporal_state'='current')::integer);
SELECT 1 / ((:'pre_target_temporal_state'='historical')::integer);
SELECT 1 / ((:'pre_current_revision_number'::integer=2)::integer);
SELECT 1 / ((:'pre_corroborating_claim_id'=:'death_claim')::integer);
SELECT 1 / ((:'pre_desired_canonical_text'=
  'The user formerly had a pet named Neko.')::integer);

SELECT * FROM memory.review_claim_temporal_reconciliation_v5_2(
  :'review_request'::uuid,:'claim'::uuid,:'observation'::uuid,
  :'reason_codes'::jsonb,:'rationale','system',:'reviewer_ref',
  :'pre_authorization_manifest_sha256'
) \gset review_
SELECT 1 / ((:'review_outcome'='applied')::integer);
SELECT 1 / ((:'review_rows_written'::integer=2)::integer);

SELECT * FROM memory.review_claim_temporal_reconciliation_v5_2(
  :'review_request'::uuid,:'claim'::uuid,:'observation'::uuid,
  :'reason_codes'::jsonb,:'rationale','system',:'reviewer_ref',
  :'pre_authorization_manifest_sha256'
) \gset review_replay_
SELECT 1 / ((:'review_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'review_replay_rows_written'::integer=0)::integer);
SELECT 1 / ((:'review_replay_review_id'=:'review_review_id')::integer);

SELECT *
FROM memory.preflight_claim_temporal_reconciliation_apply_v5_2(
  :'claim'::uuid,:'review_review_id'::uuid
) \gset apply_pre_
SELECT 1 / ((:'apply_pre_prior_revision_number'::integer=2)::integer);
SELECT 1 / ((:'apply_pre_resulting_revision_number'::integer=3)::integer);

SELECT * FROM memory.apply_claim_temporal_reconciliation_v5_2(
  :'apply_request'::uuid,:'claim'::uuid,:'review_review_id'::uuid,
  :'apply_pre_apply_manifest_sha256'
) \gset apply_
SELECT 1 / ((:'apply_outcome'='applied')::integer);
SELECT 1 / ((:'apply_rows_written'::integer=5)::integer);
SELECT 1 / ((:'apply_resulting_revision_number'::integer=3)::integer);

SELECT * FROM memory.apply_claim_temporal_reconciliation_v5_2(
  :'apply_request'::uuid,:'claim'::uuid,:'review_review_id'::uuid,
  :'apply_pre_apply_manifest_sha256'
) \gset apply_replay_
SELECT 1 / ((:'apply_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'apply_replay_rows_written'::integer=0)::integer);
SELECT 1 / ((:'apply_replay_event_id'=:'apply_event_id')::integer);

RESET SESSION AUTHORIZATION;
SELECT 1 / (((
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id=:'owner'::uuid
    AND claim_id=:'claim'::uuid
    AND status='supported'
    AND canonical_text='The user formerly had a pet named Neko.'
    AND valid_to='2026-07-28 04:04:34.272603+00'::timestamptz
    AND qualifiers->>'temporal_state'='historical'
    AND qualifiers->>'valid_to_semantics'='exclusive_upper_bound'
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.claim_revision
  WHERE owner_user_id=:'owner'::uuid
    AND claim_id=:'claim'::uuid
    AND revision_number=3
    AND reason='claim_temporal_reconciliation_v5_2:historical'
    AND snapshot->>'canonical_text'
      ='The user formerly had a pet named Neko.'
)=1)::integer);
SELECT 1 / (((
  SELECT count(*) FROM memory.claim_observation
  WHERE owner_user_id=:'owner'::uuid
    AND claim_id=:'claim'::uuid
    AND observation_id=:'observation'::uuid
    AND stance='supports'
)=1)::integer);

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'other_owner', true);
DO $isolation$
BEGIN
  BEGIN
    PERFORM *
    FROM memory.preflight_claim_temporal_reconciliation_v5_2(
      'bd20dd0a-9fa0-4a21-8a93-e828c8044150'::uuid,
      'bc8866ad-95e8-4413-832e-813f601eece6'::uuid
    );
    RAISE EXCEPTION 'cross-owner temporal reconciliation succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$isolation$;

RESET SESSION AUTHORIZATION;
COMMIT;
\echo review_id=:'review_review_id'
\echo review_manifest_sha256=:'pre_authorization_manifest_sha256'
\echo apply_manifest_sha256=:'apply_pre_apply_manifest_sha256'
\echo apply_event_id=:'apply_event_id'
SQL
chmod 0600 "$apply_log"

claim_after=$(scalar "
  SELECT encode(public.digest(convert_to(to_jsonb(c)::text,'UTF8'),
    'sha256'),'hex')
  FROM memory.claim c
  WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid")
[[ "$claim_after" != "$claim_before" ]]
old_revisions_after=$(scalar "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(
    to_jsonb(r)::text,E'\\n' ORDER BY revision_number),''),
    'UTF8'),'sha256'),'hex')
  FROM memory.claim_revision r
  WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid
    AND revision_number<=2")
[[ "$old_revisions_after" == "$old_revisions_before" ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.claim_revision
  WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid")" == 3 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.claim_temporal_reconciliation_review_v5_2
  WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid")" == 1 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.claim_temporal_reconciliation_apply_v5_2
  WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid")" == 1 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id='$owner'::uuid
    AND request_id IN ('$review_request'::uuid,'$apply_request'::uuid)")" == 2 ]]

other_owner_after=$(scalar "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(
    row_json,E'\\n' ORDER BY row_json),''),
    'UTF8'),'sha256'),'hex')
  FROM (
    SELECT to_jsonb(c)::text AS row_json FROM memory.claim c
    WHERE owner_user_id='$other_owner'::uuid
    UNION ALL
    SELECT to_jsonb(r)::text FROM memory.claim_revision r
    WHERE owner_user_id='$other_owner'::uuid
    UNION ALL
    SELECT to_jsonb(l)::text FROM memory.claim_observation l
    WHERE owner_user_id='$other_owner'::uuid
  ) rows")
[[ "$other_owner_after" == "$other_owner_before" ]]
protected_after=$(scalar "
  SELECT jsonb_build_object(
    'claim_count',(SELECT count(*) FROM memory.claim),
    'assessment_count',(SELECT count(*) FROM memory.claim_assessment),
    'outbox_count',(SELECT count(*) FROM memory.projection_outbox),
    'entity_count',(SELECT count(*) FROM memory.entity),
    'observation_count',(SELECT count(*) FROM memory.observation)
  )::text")
[[ "$protected_after" == "$protected_before" ]]

[[ "$(production_signature)" == "$production_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

jq -n \
  --arg contract memory_v1_v5_2_claim_temporal_reconciliation_clone_v1 \
  --arg production_head "$production_head_before" \
  --arg clone_database "$clone_db" \
  --arg owner_user_id "$owner" \
  --arg claim_id "$claim" \
  --arg observation_id "$observation" \
  --arg canonical_text 'The user formerly had a pet named Neko.' \
  --arg qdrant_sha256 "$qdrant_before" \
  '{
    contract_version:$contract,
    production_head:$production_head,
    clone_database:$clone_database,
    owner_user_id:$owner_user_id,
    claim_id:$claim_id,
    observation_id:$observation_id,
    resulting_revision_number:3,
    canonical_text:$canonical_text,
    review_rows:1,
    apply_rows:1,
    operation_request_rows:2,
    claim_observation_rows:1,
    historical_revisions_preserved:true,
    replay_rows_written:0,
    cross_owner_rejected:true,
    protected_stores_unchanged:true,
    production_unchanged:true,
    qdrant_unchanged:true,
    qdrant_sha256:$qdrant_sha256
  }' >"$artifact_dir/clone-report.json"
chmod 0600 "$artifact_dir/clone-report.json"

printf 'ARTIFACT_DIR=%s\n' "$artifact_dir"
printf 'CLONE_REPORT=%s\n' "$artifact_dir/clone-report.json"

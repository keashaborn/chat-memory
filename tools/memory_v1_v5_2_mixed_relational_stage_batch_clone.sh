#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable Postgres clone,
# stages three exact review-clean V5.2 packets, proves replay and owner isolation,
# and leaves production, Qdrant, claims, and prompt behavior unchanged.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
port=${MEMORY_V1_V5_2_MIXED_STAGE_CLONE_PORT:-55498}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52mixedstageclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
manifest_source=manifests/memory_v1_v5_2_mixed_relational_stage_batch_20260725.json
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
authorizer=tests/memory_v1_v5_2_stage_batch_fixture.py
review_root=/home/ubuntu/memory-v1-reviews
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
evidence_ids=(
  33126656-fc5a-5fc1-a035-246b14576ee5
  405fcdb1-a4d2-53ff-91ad-542b258cea03
  4550d3a1-7649-5d1b-aff8-f2504e36f869
)
backup=$(mktemp /tmp/memory-v1-v5-2-mixed-stage.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-mixed-stage-roles.XXXXXX.sql)
work=$(mktemp -d "$review_root/mixed-relational-stage-clone.XXXXXX")
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$role_sql"
  rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup" "$role_sql"
chmod 0700 "$work"

assert_equal() {
  local label=$1 actual=$2 expected=$3
  if [[ "$actual" != "$expected" ]]; then
    printf 'ASSERTION_FAILED=%s\nexpected=%s\nactual=%s\n' \
      "$label" "$expected" "$actual" >&2
    exit 1
  fi
}

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

production_signature() {
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "
      SELECT encode(public.digest(convert_to(
        coalesce(string_agg(value,E'\\n' ORDER BY value),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT table_name || ':' || count(*)::text AS value
        FROM information_schema.tables
        CROSS JOIN LATERAL (SELECT 1) AS marker
        WHERE table_schema='memory' AND table_type='BASE TABLE'
        GROUP BY table_name
      ) AS counts"
}

target_counts() {
  scalar "
    WITH target(evidence_id) AS (
      VALUES
        ('${evidence_ids[0]}'::uuid),
        ('${evidence_ids[1]}'::uuid),
        ('${evidence_ids[2]}'::uuid)
    )
    SELECT concat_ws(',',
      (SELECT count(*) FROM memory.relational_stage_batch
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id IN (SELECT evidence_id FROM target)),
      (SELECT count(*) FROM memory.entity_mention
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id IN (SELECT evidence_id FROM target)),
      (SELECT count(*) FROM memory.entity_resolution_plan
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id IN (SELECT evidence_id FROM target)),
      (SELECT count(*) FROM memory.entity_resolution_candidate AS candidate
       JOIN memory.entity_resolution_plan AS plan
         USING(owner_user_id,resolution_id)
       WHERE plan.owner_user_id='$target_owner'::uuid
         AND plan.evidence_id IN (SELECT evidence_id FROM target)),
      (SELECT count(*) FROM memory.observation
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id IN (SELECT evidence_id FROM target)),
      (SELECT count(*) FROM memory.observation_temporal AS temporal
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$target_owner'::uuid
         AND observation.evidence_id IN (SELECT evidence_id FROM target)),
      (SELECT count(*) FROM memory.relational_operation_request
       WHERE owner_user_id='$target_owner'::uuid
         AND target_key IN (
           SELECT evidence_id::text FROM target
         ))
    )"
}

[[ -z "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain)" ]]
[[ "$(sha256sum "$manifest_source" | awk '{print $1}')" == \
  d67ee1ec7910c64fd0d1aff8bff4bd59bfecdba767437b03345534acaee5a293 ]]
[[ "$(sha256sum "$stage_runner" | awk '{print $1}')" == \
  3dddef3dbf71862fc42252d6263075ad8d407bc292a4f06760866c816c3699b9 ]]
[[ "$(sha256sum "$authorizer" | awk '{print $1}')" == \
  96461968b5afa0ec2a8aa207ec7ec2096278d851cd51d4e0a7c90541ab5a54ac ]]

qdrant_before=$(qdrant_signature)
production_before=$(production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)

docker exec brains-postgres-1 pg_dump -U sage -d memory -Fc >"$backup"
[[ -s "$backup" ]]
docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c "
  SELECT format(
    'CREATE ROLE %I %s %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;',
    rolname,
    CASE WHEN rolcanlogin THEN 'LOGIN' ELSE 'NOLOGIN' END,
    CASE WHEN rolinherit THEN 'INHERIT' ELSE 'NOINHERIT' END
  )
  FROM pg_roles
  WHERE rolname NOT LIKE 'pg\\_%' ESCAPE '\\'
    AND rolname NOT IN ('sage','postgres')
  ORDER BY rolname
" >"$role_sql"
[[ -s "$role_sql" ]]

"${compose[@]}" up -d --wait postgres
run_sql <"$role_sql"
printf '%s\n' \
  "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists <"$backup"

assert_equal initial_target_counts "$(target_counts)" '0,0,0,0,0,0,0'
entities_before=$(scalar "
  SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")
claims_before=$(scalar "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")

cp "$manifest_source" "$work/stage-manifest.json"
chmod 0600 "$work/stage-manifest.json"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$stage_runner" plan \
  --manifest "$work/stage-manifest.json" \
  --review-root "$review_root" \
  --output "$work/stage-plan.json"
head=$(git rev-parse HEAD)
/opt/chat-memory/venv/bin/python "$authorizer" authorize \
  --plan "$work/stage-plan.json" \
  --output "$work/stage-authorization.json" \
  --head "$head"
MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$stage_runner" apply \
  --plan "$work/stage-plan.json" \
  --authorization "$work/stage-authorization.json" \
  --review-root "$review_root" \
  --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
  --output "$work/stage-apply.json"

assert_equal staged_target_counts "$(target_counts)" '3,3,3,3,3,3,3'
assert_equal stage_rows \
  "$(jq -r '.database_rows_created' "$work/stage-apply.json")" 21
assert_equal replay_rows \
  "$(jq -r '.checks.replay_rows_written' "$work/stage-apply.json")" 0
assert_equal entities_unchanged "$(scalar "
  SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")" "$entities_before"
assert_equal claims_unchanged "$(scalar "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")" "$claims_before"

cross_owner_visible=$(PGPASSWORD=clone_only_brains_password psql \
  -h 127.0.0.1 -p "$port" -U brains_app -d memory -X -A -t \
  -v ON_ERROR_STOP=1 -c "
    BEGIN;
    SELECT set_config('app.user_id','$other_owner',true);
    SELECT count(*) FROM memory.observation
    WHERE evidence_id IN (
      '${evidence_ids[0]}'::uuid,'${evidence_ids[1]}'::uuid,
      '${evidence_ids[2]}'::uuid
    );
    ROLLBACK;" | sed -n '3p')
assert_equal cross_owner_visible "$cross_owner_visible" 0

assert_equal qdrant_unchanged "$(qdrant_signature)" "$qdrant_before"
assert_equal production_rows_unchanged \
  "$(production_signature)" "$production_before"
assert_equal production_head_unchanged \
  "$(git -C /opt/chat-memory rev-parse HEAD)" "$production_head_before"
assert_equal brains_service "$(systemctl is-active brains.service)" active

printf '%s\n' \
  'MEMORY_V1_V5_2_MIXED_RELATIONAL_STAGE_BATCH_CLONE=PASS' \
  'bundle_count=3' \
  'stage_rows_created=21' \
  'replay_rows_created=0' \
  'entity_rows_created=0' \
  'claim_rows_created=0' \
  'production_writes=0' \
  'qdrant_writes=0' \
  'cross_owner_visible_rows=0' \
  'hard_stop=before_entity_review_apply_claims_projection_or_retrieval'

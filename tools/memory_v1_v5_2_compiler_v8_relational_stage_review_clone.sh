#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable clone, stages the
# two exact compiler-v8 packets, and records three manual entity reviews.
# It never applies an entity, creates a claim, or touches Qdrant/retrieval.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_2_V8_STAGE_REVIEW_CLONE_PORT:-55497}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52v8stagereviewclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260725_memory_v1_v5_2_atom_stage_planner_v2.sql
rollback=ops/sql/20260725_memory_v1_v5_2_atom_stage_planner_v2_rollback.sql
sql_test=tests/memory_v1_v5_2_atom_stage_planner_v2.sql
manifest_builder=scripts/memory_v1_v5_2_compiler_v8_stage_manifests.py
bundle_builder=scripts/memory_v1_v5_2_atom_stage_bundle_v2.py
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
stage_fixture=tests/memory_v1_v5_2_stage_batch_fixture.py
review_runner=scripts/memory_v1_v5_2_entity_resolution_review_only_batch.py
backup=$(mktemp /tmp/memory-v1-v5-2-v8-stage-review.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-v8-stage-review-roles.XXXXXX.sql)
work=$(mktemp -d /tmp/memory-v1-v5-2-v8-stage-review.XXXXXX)
reviews="$work/reviews"
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
care_evidence=fea59e7e-30f5-4139-b634-97b291c88e14
profession_evidence=dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$role_sql"
  rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup" "$role_sql"
mkdir -m 0700 "$reviews"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

assert_equal() {
  local label=$1 actual=$2 expected=$3
  if [[ "$actual" != "$expected" ]]; then
    printf 'ASSERTION_FAILED=%s\nexpected=%s\nactual=%s\n' \
      "$label" "$expected" "$actual" >&2
    exit 1
  fi
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
        CROSS JOIN LATERAL (
          SELECT 1
        ) AS marker
        WHERE table_schema='memory' AND table_type='BASE TABLE'
        GROUP BY table_name
      ) AS counts"
}

target_counts() {
  scalar "
    SELECT concat_ws(',',
      (SELECT count(*) FROM memory.relational_stage_batch
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id IN ('$care_evidence'::uuid,'$profession_evidence'::uuid)),
      (SELECT count(*) FROM memory.entity_mention
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id IN ('$care_evidence'::uuid,'$profession_evidence'::uuid)),
      (SELECT count(*) FROM memory.entity_resolution_plan
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id IN ('$care_evidence'::uuid,'$profession_evidence'::uuid)),
      (SELECT count(*) FROM memory.entity_resolution_candidate AS candidate
       JOIN memory.entity_resolution_plan AS plan
         ON plan.owner_user_id=candidate.owner_user_id
        AND plan.resolution_id=candidate.resolution_id
       WHERE plan.owner_user_id='$target_owner'::uuid
         AND plan.evidence_id IN ('$care_evidence'::uuid,'$profession_evidence'::uuid)),
      (SELECT count(*) FROM memory.observation
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id IN ('$care_evidence'::uuid,'$profession_evidence'::uuid)),
      (SELECT count(*) FROM memory.observation_temporal AS temporal
       JOIN memory.observation AS observation
         ON observation.owner_user_id=temporal.owner_user_id
        AND observation.observation_id=temporal.observation_id
       WHERE observation.owner_user_id='$target_owner'::uuid
         AND observation.evidence_id IN ('$care_evidence'::uuid,'$profession_evidence'::uuid)),
      (SELECT count(*) FROM memory.entity_resolution_review AS review
       JOIN memory.entity_resolution_plan AS plan
         ON plan.owner_user_id=review.owner_user_id
        AND plan.resolution_id=review.resolution_id
       WHERE plan.owner_user_id='$target_owner'::uuid
         AND plan.evidence_id IN ('$care_evidence'::uuid,'$profession_evidence'::uuid)),
      (SELECT count(*) FROM memory.entity_resolution_apply AS applied
       JOIN memory.entity_resolution_plan AS plan
         ON plan.owner_user_id=applied.owner_user_id
        AND plan.resolution_id=applied.resolution_id
       WHERE plan.owner_user_id='$target_owner'::uuid
         AND plan.evidence_id IN ('$care_evidence'::uuid,'$profession_evidence'::uuid)),
      (SELECT count(*) FROM memory.relational_operation_request
       WHERE owner_user_id='$target_owner'::uuid
         AND (
           target_key IN ('$care_evidence','$profession_evidence')
           OR target_key IN (
             SELECT resolution_id::text FROM memory.entity_resolution_plan
             WHERE owner_user_id='$target_owner'::uuid
               AND evidence_id IN (
                 '$care_evidence'::uuid,'$profession_evidence'::uuid
               )
           )
         ))
    )"
}

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

v1_before=$(scalar "
  SELECT encode(public.digest(convert_to(
    pg_get_functiondef(
      'memory.plan_owner_v5_2_atom_stage_v1(uuid)'::regprocedure
    ),'UTF8'),'sha256'),'hex')")
run_sql <"$migration"
run_sql <"$migration"
PGPASSWORD=clone_only_brains_password psql \
  -h 127.0.0.1 -p "$port" -U brains_app -d memory \
  -X -v ON_ERROR_STOP=1 -f "$sql_test"
v1_after=$(scalar "
  SELECT encode(public.digest(convert_to(
    pg_get_functiondef(
      'memory.plan_owner_v5_2_atom_stage_v1(uuid)'::regprocedure
    ),'UTF8'),'sha256'),'hex')")
assert_equal v1_planner_unchanged "$v1_after" "$v1_before"
assert_equal initial_target_counts "$(target_counts)" '0,0,0,0,0,0,0,0,0'
target_entities_before=$(scalar "
  SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")
target_claims_before=$(scalar "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$manifest_builder" \
  --output-root "$reviews/manifests"

for case_id in compiler_v8_caregiving compiler_v8_former_profession; do
  mkdir -m 0700 "$reviews/$case_id"
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
    /opt/chat-memory/venv/bin/python "$bundle_builder" build \
    --manifest "$reviews/manifests/$case_id.json" \
    --output-root "$reviews/$case_id"
done

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$bundle_builder" probe \
  --bundle "$reviews/compiler_v8_caregiving/bundle.json" \
  --apply-id 190f0b21-6e54-5c1d-8a99-6b08358d4846 \
  --other-owner-user-id "$other_owner"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$bundle_builder" probe \
  --bundle "$reviews/compiler_v8_former_profession/bundle.json" \
  --apply-id f87ae2b4-57ee-5969-8ce1-bc80a6bfec83 \
  --other-owner-user-id "$other_owner"

jq -s '{
  contract_version:"memory_v1_v5_2_stage_batch_manifest_v1",
  target_server:"seebx",
  owner_user_id:"1240822d-ac9a-4096-95aa-e2b24d36ef50",
  bundles:(.[0].bundles + .[1].bundles)
}' \
  "$reviews/compiler_v8_caregiving/stage-manifest.json" \
  "$reviews/compiler_v8_former_profession/stage-manifest.json" \
  >"$reviews/stage-manifest.json"
chmod 0600 "$reviews/stage-manifest.json"

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$stage_runner" plan \
  --manifest "$reviews/stage-manifest.json" \
  --review-root "$reviews" \
  --output "$reviews/stage-plan.json"
head=$(git -C "$repo_root" rev-parse HEAD)
/opt/chat-memory/venv/bin/python "$stage_fixture" authorize \
  --plan "$reviews/stage-plan.json" \
  --output "$reviews/stage-authorization.json" \
  --head "$head"
MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$stage_runner" apply \
  --plan "$reviews/stage-plan.json" \
  --authorization "$reviews/stage-authorization.json" \
  --review-root "$reviews" \
  --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
  --output "$reviews/stage-apply.json"

assert_equal staged_target_counts "$(target_counts)" '2,5,5,2,4,4,0,0,2'
assert_equal stage_rows \
  "$(jq -r '.database_rows_created' "$reviews/stage-apply.json")" 24
assert_equal stage_replay_rows \
  "$(jq -r '.checks.replay_rows_written' "$reviews/stage-apply.json")" 0

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$review_runner" manifest \
  --output "$reviews/entity-review-manifest.json"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$review_runner" plan \
  --manifest "$reviews/entity-review-manifest.json" \
  --review-root "$reviews" \
  --output "$reviews/entity-review-plan.json"

plan_sha=$(sha256sum "$reviews/entity-review-plan.json" | awk '{print $1}')
authorized_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
expires_at=$(date -u -d '+20 minutes' +%Y-%m-%dT%H:%M:%SZ)
jq -n \
  --arg authorization_id "$(cat /proc/sys/kernel/random/uuid)" \
  --arg authorized_at "$authorized_at" --arg expires_at "$expires_at" \
  --arg head "$head" --arg owner "$target_owner" --arg plan_sha "$plan_sha" \
  '{
    contract_version:"memory_v1_v5_2_entity_review_only_authorization_v1",
    authorization_id:$authorization_id,authorized:true,authorized_by:"Eric Lund",
    authorized_at:$authorized_at,expires_at:$expires_at,
    expected_head_commit:$head,target_server:"seebx",
    scope:"review_owner_v5_2_entity_resolutions_without_apply",
    owner_user_id:$owner,plan_sha256:$plan_sha,expected_item_count:3,
    expected_new_rows:6,
    confirmation:"REVIEW_OWNER_V5_2_ENTITY_RESOLUTIONS_WITHOUT_APPLY"
  }' >"$reviews/entity-review-authorization.json"
chmod 0600 "$reviews/entity-review-authorization.json"

MEMORY_V1_V5_2_ENTITY_REVIEW_ONLY_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$review_runner" apply \
  --plan "$reviews/entity-review-plan.json" \
  --authorization "$reviews/entity-review-authorization.json" \
  --review-root "$reviews" \
  --confirm REVIEW_OWNER_V5_2_ENTITY_RESOLUTIONS_WITHOUT_APPLY \
  --output "$reviews/entity-review-apply.json"

assert_equal reviewed_target_counts "$(target_counts)" '2,5,5,2,4,4,3,0,5'
assert_equal review_rows \
  "$(jq -r '.new_rows' "$reviews/entity-review-apply.json")" 6
assert_equal review_replay \
  "$(jq -r '.zero_write_replay' "$reviews/entity-review-apply.json")" true
assert_equal expected_predicates "$(scalar "
  SELECT string_agg(predicate,',' ORDER BY predicate)
  FROM memory.observation
  WHERE owner_user_id='$target_owner'::uuid
    AND evidence_id IN ('$care_evidence'::uuid,'$profession_evidence'::uuid)")" \
  'occupation.works_as,occupation.works_as,relationship.caregiver_for,relationship.spouse_of'
assert_equal historical_temporals "$(scalar "
  SELECT count(*)
  FROM memory.observation AS observation
  JOIN memory.observation_temporal AS temporal
    USING(owner_user_id,observation_id)
  WHERE observation.owner_user_id='$target_owner'::uuid
    AND observation.evidence_id='$profession_evidence'::uuid
    AND observation.predicate='occupation.works_as'
    AND lower(temporal.instant_range) IS NULL
    AND upper(temporal.instant_range) IS NOT NULL")" 2
OTHER_OWNER="$other_owner" CARE_EVIDENCE="$care_evidence" \
  PROFESSION_EVIDENCE="$profession_evidence" POSTGRES_DSN="$dsn" \
  /opt/chat-memory/venv/bin/python - <<'PY'
import asyncio, os, uuid
import asyncpg

async def main():
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        for value in ("CARE_EVIDENCE", "PROFESSION_EVIDENCE"):
            try:
                async with conn.transaction(readonly=True):
                    await conn.execute(
                        "SELECT set_config('app.user_id',$1,true)",
                        os.environ["OTHER_OWNER"],
                    )
                    await conn.fetchval(
                        "SELECT memory.plan_owner_v5_2_entity_resolution_review_v1($1::uuid)",
                        uuid.UUID(os.environ[value]),
                    )
            except asyncpg.PostgresError as exc:
                if exc.sqlstate != "P0002":
                    raise
            else:
                raise RuntimeError("cross-owner entity review plan resolved")
    finally:
        await conn.close()

asyncio.run(main())
PY
assert_equal target_entities_unchanged "$(scalar "
  SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")" "$target_entities_before"
assert_equal target_claims_unchanged "$(scalar "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")" "$target_claims_before"

run_sql <"$rollback"
assert_equal v2_planner_removed "$(scalar "
  SELECT to_regprocedure(
    'memory.plan_owner_v5_2_atom_stage_v2(uuid)'
  ) IS NULL")" t
assert_equal review_planner_removed "$(scalar "
  SELECT to_regprocedure(
    'memory.plan_owner_v5_2_entity_resolution_review_v1(uuid)'
  ) IS NULL")" t
assert_equal rows_survive_planner_rollback "$(target_counts)" '2,5,5,2,4,4,3,0,5'

assert_equal qdrant_unchanged "$(qdrant_signature)" "$qdrant_before"
assert_equal production_rows_unchanged "$(production_signature)" "$production_before"
assert_equal production_head_unchanged \
  "$(git -C /opt/chat-memory rev-parse HEAD)" "$production_head_before"
assert_equal brains_service "$(systemctl is-active brains.service)" active
docker exec brains-postgres-1 pg_isready -U sage -d memory >/dev/null

printf '%s\n' \
  'MEMORY_V1_V5_2_COMPILER_V8_RELATIONAL_STAGE_REVIEW_CLONE=PASS' \
  'clone_stage_rows_created=24' \
  'clone_review_rows_created=6' \
  'manual_reviews=3' \
  'entity_apply_rows=0' \
  'entity_rows_created=0' \
  'claim_rows_created=0' \
  'production_writes=0' \
  'qdrant_writes=0' \
  'retrieval_changes=0' \
  'prompt_changes=0' \
  'hard_stop=before_entity_apply_claims_projection_or_retrieval'

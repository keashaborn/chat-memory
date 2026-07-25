#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable clone, installs
# the read-only exact-target planner, applies five reviewed/automatic entity
# resolutions, and verifies four observation bindings. It never creates a
# claim, projection, Qdrant point, retrieval input, or prompt input.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_2_V8_ENTITY_APPLY_CLONE_PORT:-55499}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52v8entityapplyclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260725_memory_v1_v5_2_entity_apply_planner_v1.sql
rollback=ops/sql/20260725_memory_v1_v5_2_entity_apply_planner_v1_rollback.sql
sql_test=tests/memory_v1_v5_2_entity_apply_planner_v1.sql
runner=scripts/memory_v1_v5_2_compiler_v8_entity_apply_batch.py
backup=$(mktemp /tmp/memory-v1-v5-2-v8-entity-apply.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-v8-entity-apply-roles.XXXXXX.sql)
work=$(mktemp -d /tmp/memory-v1-v5-2-v8-entity-apply.XXXXXX)
reviews="$work/reviews"
manifest="$reviews/manifest.json"
plan="$reviews/plan.json"
authorization="$reviews/authorization.json"
report="$reviews/apply-report.json"
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
self_entity=35029129-27bd-457b-8cb5-82dd37ba32ba
care_evidence=fea59e7e-30f5-4139-b634-97b291c88e14
profession_evidence=dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5
care_self_resolution=f7f8b81b-4fa5-4f36-a0e4-4fb2cfd571ae
monika_resolution=8ed545c8-da8b-4db8-92fa-519ee62a8185
profession_self_resolution=37e5a24b-2862-46df-8ce6-7fc6cfbc61bb
psychologist_resolution=35fe3e20-e02d-4304-ba4b-2e85066034be
bcba_resolution=065f0bde-dd18-4630-8840-4ba49655bc55

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

production_target_signature() {
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "
      SELECT encode(public.digest(convert_to(
        concat_ws('|',
          (SELECT count(*) FROM memory.entity
           WHERE owner_user_id='$owner'::uuid),
          (SELECT count(*) FROM memory.entity_resolution_apply
           WHERE owner_user_id='$owner'::uuid),
          (SELECT count(*) FROM memory.entity_alias_observation
           WHERE owner_user_id='$owner'::uuid),
          (SELECT count(*) FROM memory.observation_entity_binding
           WHERE owner_user_id='$owner'::uuid),
          (SELECT count(*) FROM memory.relational_operation_request
           WHERE owner_user_id='$owner'::uuid),
          (SELECT count(*) FROM memory.claim
           WHERE owner_user_id='$owner'::uuid),
          (SELECT count(*) FROM memory.claim_revision
           WHERE owner_user_id='$owner'::uuid),
          (SELECT count(*) FROM memory.claim_evidence
           WHERE owner_user_id='$owner'::uuid),
          (SELECT count(*) FROM memory.projection_outbox
           WHERE owner_user_id='$owner'::uuid)
        ),'UTF8'),'sha256'),'hex')"
}

target_counts() {
  scalar "
    SELECT concat_ws(',',
      (SELECT count(*) FROM memory.entity
       WHERE owner_user_id='$owner'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_apply AS applied
       JOIN memory.entity_resolution_plan AS resolution
         ON resolution.owner_user_id=applied.owner_user_id
        AND resolution.resolution_id=applied.resolution_id
       WHERE resolution.owner_user_id='$owner'::uuid
         AND resolution.evidence_id IN (
           '$care_evidence'::uuid,'$profession_evidence'::uuid
         )),
      (SELECT count(*) FROM memory.entity_alias_observation AS alias
       WHERE alias.owner_user_id='$owner'::uuid
         AND alias.evidence_id IN (
           '$care_evidence'::uuid,'$profession_evidence'::uuid
         )),
      (SELECT count(*) FROM memory.observation_entity_binding AS binding
       JOIN memory.observation AS observation
         ON observation.owner_user_id=binding.owner_user_id
        AND observation.observation_id=binding.observation_id
       WHERE observation.owner_user_id='$owner'::uuid
         AND observation.evidence_id IN (
           '$care_evidence'::uuid,'$profession_evidence'::uuid
         )),
      (SELECT count(*) FROM memory.relational_operation_request
       WHERE owner_user_id='$owner'::uuid
         AND operation='apply_resolution'
         AND target_key IN (
           '$care_self_resolution','$monika_resolution',
           '$profession_self_resolution','$psychologist_resolution',
           '$bcba_resolution'
         ))
    )"
}

qdrant_before=$(qdrant_signature)
production_before=$(production_target_signature)
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

run_sql <"$migration"
run_sql <"$migration"
PGPASSWORD=clone_only_brains_password psql \
  -h 127.0.0.1 -p "$port" -U brains_app -d memory \
  -X -v ON_ERROR_STOP=1 -f "$sql_test"

initial_counts=$(target_counts)
IFS=',' read -r initial_entity_count initial_apply_count initial_alias_count \
  initial_binding_count initial_operation_count <<<"$initial_counts"
assert_equal initial_target_applies "$initial_apply_count" 0
assert_equal initial_target_aliases "$initial_alias_count" 0
assert_equal initial_target_bindings "$initial_binding_count" 0
assert_equal initial_target_operations "$initial_operation_count" 0

claims_before=$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.claim
     WHERE owner_user_id='$owner'::uuid),
    (SELECT count(*) FROM memory.claim_revision
     WHERE owner_user_id='$owner'::uuid),
    (SELECT count(*) FROM memory.claim_evidence
     WHERE owner_user_id='$owner'::uuid),
    (SELECT count(*) FROM memory.projection_outbox
     WHERE owner_user_id='$owner'::uuid)
  )")
other_before=$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.entity
     WHERE owner_user_id='$other_owner'::uuid),
    (SELECT count(*) FROM memory.entity_resolution_apply
     WHERE owner_user_id='$other_owner'::uuid),
    (SELECT count(*) FROM memory.entity_alias_observation
     WHERE owner_user_id='$other_owner'::uuid),
    (SELECT count(*) FROM memory.observation_entity_binding
     WHERE owner_user_id='$other_owner'::uuid),
    (SELECT count(*) FROM memory.relational_operation_request
     WHERE owner_user_id='$other_owner'::uuid)
  )")

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$runner" manifest --output "$manifest"
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$runner" plan \
  --manifest "$manifest" --review-root "$reviews" --output "$plan"

plan_sha=$(sha256sum "$plan" | awk '{print $1}')
head=$(git -C "$repo_root" rev-parse HEAD)
authorized_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
expires_at=$(date -u -d '+20 minutes' +%Y-%m-%dT%H:%M:%SZ)
jq -n \
  --arg authorization_id "$(cat /proc/sys/kernel/random/uuid)" \
  --arg authorized_at "$authorized_at" --arg expires_at "$expires_at" \
  --arg head "$head" --arg owner "$owner" --arg plan_sha "$plan_sha" \
  '{
    contract_version:"memory_v1_v5_2_compiler_v8_entity_apply_authorization_v1",
    authorization_id:$authorization_id,authorized:true,authorized_by:"Eric Lund",
    authorized_at:$authorized_at,expires_at:$expires_at,
    expected_head_commit:$head,target_server:"seebx",
    scope:"apply_compiler_v8_entities_and_bind_only",
    owner_user_id:$owner,plan_sha256:$plan_sha,expected_item_count:5,
    expected_new_entities:3,expected_total_bindings:4,expected_new_rows:20,
    confirmation:"APPLY_FIVE_COMPILER_V8_ENTITY_RESOLUTIONS_AND_BIND_ONLY"
  }' >"$authorization"
chmod 0600 "$authorization"

if PGPASSWORD=clone_only_brains_password psql "$dsn" \
  -X -v ON_ERROR_STOP=1 -c "
    BEGIN;
    SELECT set_config('app.user_id','$other_owner',true);
    SELECT * FROM memory.preflight_entity_resolution_apply_v5_2(
      '$monika_resolution'::uuid,'200e0507-1f1e-4068-8f32-113b8d1c62fb'::uuid
    );
    ROLLBACK;" >/dev/null 2>&1; then
  echo 'cross-owner V5.2 entity apply preflight unexpectedly resolved' >&2
  exit 1
fi

MEMORY_V1_V5_2_COMPILER_V8_ENTITY_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$runner" apply \
  --plan "$plan" --authorization "$authorization" \
  --review-root "$reviews" \
  --confirm APPLY_FIVE_COMPILER_V8_ENTITY_RESOLUTIONS_AND_BIND_ONLY \
  --output "$report"

assert_equal report_rows "$(jq -r '.new_rows' "$report")" 20
assert_equal report_entities "$(jq -r '.new_entities' "$report")" 3
assert_equal report_applies \
  "$(jq -r '.new_entity_resolution_applies' "$report")" 5
assert_equal report_aliases \
  "$(jq -r '.new_alias_observations' "$report")" 3
assert_equal report_bindings \
  "$(jq -r '.new_observation_bindings' "$report")" 4
assert_equal report_operations \
  "$(jq -r '.new_operation_requests' "$report")" 5
assert_equal report_replay "$(jq -r '.zero_write_replay' "$report")" true
assert_equal applied_outcomes \
  "$(jq -r '[.first_results[].outcome] | unique | join(",")' "$report")" \
  applied
assert_equal replay_outcomes \
  "$(jq -r '[.replay_results[].outcome] | unique | join(",")' "$report")" \
  replayed

final_counts=$(target_counts)
IFS=',' read -r final_entity_count final_apply_count final_alias_count \
  final_binding_count final_operation_count <<<"$final_counts"
assert_equal entity_delta "$((final_entity_count-initial_entity_count))" 3
assert_equal exact_applies "$final_apply_count" 5
assert_equal exact_aliases "$final_alias_count" 3
assert_equal exact_bindings "$final_binding_count" 4
assert_equal exact_operations "$final_operation_count" 5

assert_equal named_entities "$(scalar "
  SELECT string_agg(entity_type || ':' || canonical_name,',' ORDER BY canonical_name)
  FROM memory.entity
  WHERE owner_user_id='$owner'::uuid
    AND metadata->>'resolution_id' IN (
      '$monika_resolution','$psychologist_resolution','$bcba_resolution'
    )")" \
  'concept:BCBA,person:Monika,concept:clinical psychologist'

assert_equal binding_semantics "$(scalar "
  SELECT string_agg(
    observation.predicate || ':' ||
    subject.entity_id::text || '->' || object.canonical_name,
    ',' ORDER BY observation.predicate,object.canonical_name
  )
  FROM memory.observation_entity_binding AS binding
  JOIN memory.observation AS observation
    ON observation.owner_user_id=binding.owner_user_id
   AND observation.observation_id=binding.observation_id
  JOIN memory.entity AS subject
    ON subject.owner_user_id=binding.owner_user_id
   AND subject.entity_id=binding.subject_entity_id
  JOIN memory.entity AS object
    ON object.owner_user_id=binding.owner_user_id
   AND object.entity_id=binding.object_entity_id
  WHERE observation.owner_user_id='$owner'::uuid
    AND observation.evidence_id IN (
      '$care_evidence'::uuid,'$profession_evidence'::uuid
    )")" \
  "occupation.works_as:$self_entity->BCBA,occupation.works_as:$self_entity->clinical psychologist,relationship.caregiver_for:$self_entity->Monika,relationship.spouse_of:$self_entity->Monika"

assert_equal durable_claims_unchanged "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.claim
     WHERE owner_user_id='$owner'::uuid),
    (SELECT count(*) FROM memory.claim_revision
     WHERE owner_user_id='$owner'::uuid),
    (SELECT count(*) FROM memory.claim_evidence
     WHERE owner_user_id='$owner'::uuid),
    (SELECT count(*) FROM memory.projection_outbox
     WHERE owner_user_id='$owner'::uuid)
  )")" "$claims_before"
assert_equal other_owner_unchanged "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.entity
     WHERE owner_user_id='$other_owner'::uuid),
    (SELECT count(*) FROM memory.entity_resolution_apply
     WHERE owner_user_id='$other_owner'::uuid),
    (SELECT count(*) FROM memory.entity_alias_observation
     WHERE owner_user_id='$other_owner'::uuid),
    (SELECT count(*) FROM memory.observation_entity_binding
     WHERE owner_user_id='$other_owner'::uuid),
    (SELECT count(*) FROM memory.relational_operation_request
     WHERE owner_user_id='$other_owner'::uuid)
  )")" "$other_before"

run_sql <"$rollback"
assert_equal planner_removed "$(scalar "
  SELECT to_regprocedure(
    'memory.plan_owner_v5_2_entity_apply_review_v1(uuid)'
  ) IS NULL")" t
assert_equal applied_rows_survive_planner_rollback "$(target_counts)" "$final_counts"

assert_equal qdrant_unchanged "$(qdrant_signature)" "$qdrant_before"
assert_equal production_target_unchanged \
  "$(production_target_signature)" "$production_before"
assert_equal production_head_unchanged \
  "$(git -C /opt/chat-memory rev-parse HEAD)" "$production_head_before"

printf 'V8_ENTITY_APPLY_BINDING_CLONE=PASS\n'
printf 'NEW_ENTITIES=3\nENTITY_APPLIES=5\nALIASES=3\nBINDINGS=4\n'
printf 'CLAIMS=0\nPROJECTIONS=0\nQDRANT_WRITES=0\n'
printf 'ACCOUNT_ISOLATION=PASS\nZERO_WRITE_REPLAY=PASS\n'
printf 'REPORT_SHA256=%s\n' "$(sha256sum "$report" | awk '{print $1}')"

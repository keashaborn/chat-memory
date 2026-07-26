#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable Postgres clone,
# stages one previously admitted V5.2 stance atom, and verifies its trusted-self
# resolution. It never applies an entity, binds an observation, creates a
# claim, writes Qdrant, or changes retrieval/prompt behavior.

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STAGE_CLONE_PORT:-55503}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52evidencecontextstageclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
build_manifest=manifests/memory_v1_v5_2_evidence_context_atom_stage_20260726.json
bundle_builder=scripts/memory_v1_v5_2_atom_stage_bundle_v2.py
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
stage_fixture=tests/memory_v1_v5_2_stage_batch_fixture.py
compat_migration=ops/sql/20260726_memory_v1_v5_2_disposition_atom_projection_compat.sql
compat_rollback=ops/sql/20260726_memory_v1_v5_2_disposition_atom_projection_compat_rollback.sql
compat_test=tests/memory_v1_v5_2_disposition_atom_projection_compat.sql
backup=$(mktemp /tmp/memory-v1-v5-2-evidence-context-stage.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-evidence-context-stage-roles.XXXXXX.sql)
work=$(mktemp -d /tmp/memory-v1-v5-2-evidence-context-stage.XXXXXX)
reviews="$work/reviews"
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

baseline_commit=112e610990b56b5237d1f2d8e66f261fd5e52602
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
apply_id=2fca9e6a-e5db-5568-bcd1-5e43b7e2a5d3
evidence_id=049205b4-9a6c-5e1a-bb8f-2ab9f05f8964
self_entity_id=35029129-27bd-457b-8cb5-82dd37ba32ba

build_manifest_sha=222e0c61bef072cbcc9c9235f9a226386c6cb26a941e28c0cc4a22c833833c6a
bundle_builder_sha=5a611180ecbb3fa3b9220459ef870879e74a8d487847ef76084b379c32ac30bb
stage_runner_sha=3dddef3dbf71862fc42252d6263075ad8d407bc292a4f06760866c816c3699b9
stage_fixture_sha=96461968b5afa0ec2a8aa207ec7ec2096278d851cd51d4e0a7c90541ab5a54ac
compose_ci_sha=c437744f84f82ce14446fa420bb4680312bbc08acdbdebb92e6a48cc1af23f8e
compose_clone_sha=3185bdb665cb37dd2e695e7653ff8b749ba5dd75a2f3bbe06a7c2717a2e5c0e2
compat_migration_sha=758af9d35fc2cd00514525b40b5dc19937a99c17d18a5ec4e3bece151ad59202
compat_rollback_sha=88f2c7ea892799a7c0c7219e0b67bc0a78bb861946007c0f84e68b98c5860f5b
compat_test_sha=4ef8b7a10ac3061a39a2eff6d81ba5bcef3736369d856929aea5a235dcdda0cd

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

production_scalar() {
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
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

assert_sha() {
  local label=$1 path=$2 expected=$3
  assert_equal "$label" "$(sha256sum "$path" | awk '{print $1}')" "$expected"
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
  production_scalar "
    SELECT md5(jsonb_build_object(
      'route_events',(SELECT count(*) FROM memory.v5_2_local_packet_route_event),
      'atom_proposals',(SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
      'atom_reviews',(SELECT count(*) FROM memory.v5_2_atom_admission_review),
      'atom_applies',(SELECT count(*) FROM memory.v5_2_atom_admission_apply),
      'atom_operations',(SELECT count(*) FROM memory.v5_2_atom_admission_operation),
      'stage_batches',(SELECT count(*) FROM memory.relational_stage_batch),
      'operation_requests',(SELECT count(*) FROM memory.relational_operation_request),
      'mentions',(SELECT count(*) FROM memory.entity_mention),
      'resolutions',(SELECT count(*) FROM memory.entity_resolution_plan),
      'candidates',(SELECT count(*) FROM memory.entity_resolution_candidate),
      'observations',(SELECT count(*) FROM memory.observation),
      'temporals',(SELECT count(*) FROM memory.observation_temporal),
      'resolution_reviews',(SELECT count(*) FROM memory.entity_resolution_review),
      'resolution_applies',(SELECT count(*) FROM memory.entity_resolution_apply),
      'bindings',(SELECT count(*) FROM memory.observation_entity_binding),
      'claims',(SELECT count(*) FROM memory.claim),
      'claim_observations',(SELECT count(*) FROM memory.claim_observation)
    )::text)"
}

target_stage_counts() {
  scalar "
    SELECT concat_ws(',',
      (SELECT count(*) FROM memory.relational_stage_batch
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.relational_operation_request
       WHERE owner_user_id='$target_owner'::uuid
         AND target_key='$evidence_id'),
      (SELECT count(*) FROM memory.entity_mention
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_plan
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_candidate AS candidate
       JOIN memory.entity_resolution_plan AS resolution
         USING(owner_user_id,resolution_id)
       WHERE resolution.owner_user_id='$target_owner'::uuid
         AND resolution.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.observation
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.observation_temporal AS temporal
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$target_owner'::uuid
         AND observation.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_review AS review
       JOIN memory.entity_resolution_plan AS resolution
         USING(owner_user_id,resolution_id)
       WHERE resolution.owner_user_id='$target_owner'::uuid
         AND resolution.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_apply AS applied
       JOIN memory.entity_resolution_plan AS resolution
         USING(owner_user_id,resolution_id)
       WHERE resolution.owner_user_id='$target_owner'::uuid
         AND resolution.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.observation_entity_binding AS binding
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$target_owner'::uuid
         AND observation.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.claim_observation AS linked
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$target_owner'::uuid
         AND observation.evidence_id='$evidence_id'::uuid)
    )"
}

git -C "$repo_root" merge-base --is-ancestor "$baseline_commit" HEAD
test -z "$(git -C "$repo_root" status --short)"
assert_sha build_manifest "$build_manifest" "$build_manifest_sha"
assert_sha bundle_builder "$bundle_builder" "$bundle_builder_sha"
assert_sha stage_runner "$stage_runner" "$stage_runner_sha"
assert_sha stage_fixture "$stage_fixture" "$stage_fixture_sha"
assert_sha compose_ci docker-compose.ci.yml "$compose_ci_sha"
assert_sha compose_clone docker-compose.stage-batch-clone.yml "$compose_clone_sha"
assert_sha compat_migration "$compat_migration" "$compat_migration_sha"
assert_sha compat_rollback "$compat_rollback" "$compat_rollback_sha"
assert_sha compat_test "$compat_test" "$compat_test_sha"

qdrant_before=$(qdrant_signature)
production_before=$(production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)
production_planner_sha=$(production_scalar "
  SELECT encode(public.digest(convert_to(
    pg_get_functiondef(
      'memory.plan_owner_v5_2_atom_stage_v2(uuid)'::regprocedure
    ),'UTF8'),'sha256'),'hex')")
legacy_guard_sha=$(production_scalar "
  SELECT encode(public.digest(convert_to(
    pg_get_functiondef(
      'memory.guard_disposed_evidence_from_stage_v1()'::regprocedure
    ),'UTF8'),'sha256'),'hex')")

assert_equal production_initial_target_stage "$(production_scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.relational_stage_batch
     WHERE owner_user_id='$target_owner'::uuid
       AND evidence_id='$evidence_id'::uuid),
    (SELECT count(*) FROM memory.entity_mention
     WHERE owner_user_id='$target_owner'::uuid
       AND evidence_id='$evidence_id'::uuid),
    (SELECT count(*) FROM memory.observation
     WHERE owner_user_id='$target_owner'::uuid
       AND evidence_id='$evidence_id'::uuid))")" '0,0,0'

docker exec brains-postgres-1 pg_dump -U sage -d memory -Fc >"$backup"
test -s "$backup"
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
test -s "$role_sql"

"${compose[@]}" up -d --wait postgres
run_sql <"$role_sql"
printf '%s\n' \
  "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists <"$backup"

run_sql <"$compat_migration"
run_sql <"$compat_migration"
PGPASSWORD=clone_only_brains_password psql \
  -h 127.0.0.1 -p "$port" -U brains_app -d memory \
  -X -v ON_ERROR_STOP=1 -f "$compat_test"

assert_equal clone_planner_sha "$(scalar "
  SELECT encode(public.digest(convert_to(
    pg_get_functiondef(
      'memory.plan_owner_v5_2_atom_stage_v2(uuid)'::regprocedure
    ),'UTF8'),'sha256'),'hex')")" "$production_planner_sha"
assert_equal legacy_guard_unchanged "$(scalar "
  SELECT encode(public.digest(convert_to(
    pg_get_functiondef(
      'memory.guard_disposed_evidence_from_stage_v1()'::regprocedure
    ),'UTF8'),'sha256'),'hex')")" "$legacy_guard_sha"
assert_equal clone_initial_target_stage "$(target_stage_counts)" \
  '0,0,0,0,0,0,0,0,0,0,0'

target_entities_before=$(scalar "
  SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")
target_claims_before=$(scalar "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$bundle_builder" build \
  --manifest "$build_manifest" \
  --output-root "$reviews" >"$work/build-report.json"

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$bundle_builder" probe \
  --bundle "$reviews/bundle.json" \
  --apply-id "$apply_id" \
  --other-owner-user-id "$other_owner" >"$work/probe-report.json"

assert_equal build_expected_rows \
  "$(jq -r '.expected_new_rows' "$work/build-report.json")" 7
assert_equal build_database_writes \
  "$(jq -r '.database_writes' "$work/build-report.json")" 0
assert_equal probe_cross_owner \
  "$(jq -r '.cross_owner_rejection' "$work/probe-report.json")" P0002
assert_equal probe_tamper \
  "$(jq -r '.tampered_projection_rejection' "$work/probe-report.json")" 23514

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

assert_equal staged_target_counts "$(target_stage_counts)" \
  '1,1,1,1,1,1,1,0,0,0,0'
assert_equal stage_rows \
  "$(jq -r '.database_rows_created' "$reviews/stage-apply.json")" 7
assert_equal stage_outcome \
  "$(jq -r '.applied[0].outcome' "$reviews/stage-apply.json")" applied
assert_equal stage_replay \
  "$(jq -r '.replayed[0].outcome' "$reviews/stage-apply.json")" replayed
assert_equal stage_replay_counts \
  "$(jq -c '.replayed[0].counts' "$reviews/stage-apply.json")" \
  '{"candidates":0,"mentions":0,"observations":0,"resolutions":0,"temporals":0}'

assert_equal self_resolution "$(scalar "
  SELECT concat_ws('|',
    action::text,
    decision_state::text,
    selected_entity_id::text,
    review_reason_codes::text)
  FROM memory.entity_resolution_plan
  WHERE owner_user_id='$target_owner'::uuid
    AND evidence_id='$evidence_id'::uuid")" \
  "link_existing|auto_link_eligible|$self_entity_id|[\"trusted_owner_self_binding\"]"
assert_equal self_candidate "$(scalar "
  SELECT candidate.candidate_entity_id::text
  FROM memory.entity_resolution_candidate AS candidate
  JOIN memory.entity_resolution_plan AS resolution
    USING(owner_user_id,resolution_id)
  WHERE resolution.owner_user_id='$target_owner'::uuid
    AND resolution.evidence_id='$evidence_id'::uuid")" "$self_entity_id"
assert_equal observation_predicate "$(scalar "
  SELECT predicate
  FROM memory.observation
  WHERE owner_user_id='$target_owner'::uuid
    AND evidence_id='$evidence_id'::uuid")" stance.reported
assert_equal target_entities_unchanged "$(scalar "
  SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")" "$target_entities_before"
assert_equal target_claims_unchanged "$(scalar "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")" "$target_claims_before"

run_sql <"$compat_rollback"
assert_equal compatibility_function_removed "$(scalar "
  SELECT to_regprocedure(
    'memory.guard_disposed_evidence_from_stage_v2()'
  ) IS NULL")" t
assert_equal legacy_trigger_restored "$(scalar "
  SELECT procedure.proname
  FROM pg_trigger AS trigger
  JOIN pg_proc AS procedure
    ON procedure.oid=trigger.tgfoid
  WHERE trigger.tgrelid='memory.relational_stage_batch'::regclass
    AND trigger.tgname='v5_local_disposition_stage_guard'
    AND NOT trigger.tgisinternal")" guard_disposed_evidence_from_stage_v1
assert_equal staged_rows_survive_compatibility_rollback \
  "$(target_stage_counts)" '1,1,1,1,1,1,1,0,0,0,0'

assert_equal qdrant_unchanged "$(qdrant_signature)" "$qdrant_before"
assert_equal production_rows_unchanged \
  "$(production_signature)" "$production_before"
assert_equal production_head_unchanged \
  "$(git -C /opt/chat-memory rev-parse HEAD)" "$production_head_before"
assert_equal brains_service "$(systemctl is-active brains.service)" active
docker exec brains-postgres-1 pg_isready -U sage -d memory >/dev/null

printf '%s\n' \
  'MEMORY_V1_V5_2_EVIDENCE_CONTEXT_ATOM_STAGE_CLONE=PASS' \
  "owner=$target_owner" \
  "apply_id=$apply_id" \
  "evidence_id=$evidence_id" \
  'clone_rows_created=7' \
  'same_run_replay_rows=0' \
  'self_resolution=trusted_owner_self_binding' \
  'legacy_terminal_disposition_preserved=true' \
  'exact_admitted_atom_compatibility=passed' \
  'entity_resolution_apply_rows=0' \
  'observation_binding_rows=0' \
  'claim_rows_created=0' \
  'production_writes=0' \
  'qdrant_writes=0' \
  'external_model_calls=0' \
  'retrieval_changes=0' \
  'prompt_changes=0' \
  'hard_stop=before_production_stage_entity_apply_observation_binding_claims_or_retrieval'

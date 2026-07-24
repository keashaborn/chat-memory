#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_2_ATOM_STAGE_CLONE_PORT:-55489}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52atomstageclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
planner=ops/sql/20260724_memory_v1_v5_2_atom_stage_planner.sql
planner_rollback=ops/sql/20260724_memory_v1_v5_2_atom_stage_planner_rollback.sql
planner_test=tests/memory_v1_v5_2_atom_stage_planner.sql
build_manifest=ops/manifests/memory_v1_v5_2_atom_stage_manifest_20260724.json
builder=scripts/memory_v1_v5_2_atom_stage_bundle.py
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
fixture=tests/memory_v1_v5_2_stage_batch_fixture.py
backup=$(mktemp /tmp/memory-v1-v5-2-atom-stage.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-atom-stage-roles.XXXXXX.sql)
work=$(mktemp -d /tmp/memory-v1-v5-2-atom-stage.XXXXXX)
reviews="$work/reviews"
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
apply_id=a94e6005-7142-581c-b4db-df314a358c66
evidence_id=22bd0732-3539-4180-8f89-8f84114131c0
projection_sha=b8371299e70799080778e49f332f1cee7619aa268238ff2ddee0c0816b504715
planner_sha=78f76aff584d19759679344a65fa7971f4e5cd56c4762136dfde685573f352f8
planner_rollback_sha=ebe04293906e16abdaf03709fea38da22d5059b37f6ad40d2bb504b4c49a6744
planner_test_sha=01e814f25c1ffe61a4ad452f34d719270391fa28658736471d122a777e846b16
build_manifest_sha=9839a34fdc5858e9de235a2b13105fa162ef0ab46742a50ccce6c2a417020582
builder_sha=e60070f88259ea88aa49d68a8eb4f42159bd6a8d191ad45a708988bf06172715

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
  local label=$1
  local actual=$2
  local expected=$3
  if [[ "$actual" != "$expected" ]]; then
    printf '%s\n' \
      "ASSERTION_FAILED=$label" \
      "expected=$expected" \
      "actual=$actual" >&2
    exit 1
  fi
}

assert_equal planner_sha "$(sha256sum "$planner" | awk '{print $1}')" "$planner_sha"
assert_equal planner_rollback_sha \
  "$(sha256sum "$planner_rollback" | awk '{print $1}')" "$planner_rollback_sha"
assert_equal planner_test_sha \
  "$(sha256sum "$planner_test" | awk '{print $1}')" "$planner_test_sha"
assert_equal build_manifest_sha \
  "$(sha256sum "$build_manifest" | awk '{print $1}')" "$build_manifest_sha"
assert_equal builder_sha "$(sha256sum "$builder" | awk '{print $1}')" "$builder_sha"

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
      SELECT md5(jsonb_build_object(
        'atom_proposals',(
          SELECT count(*) FROM memory.v5_2_atom_admission_proposal
        ),
        'atom_reviews',(
          SELECT count(*) FROM memory.v5_2_atom_admission_review
        ),
        'atom_applies',(
          SELECT count(*) FROM memory.v5_2_atom_admission_apply
        ),
        'atom_operations',(
          SELECT count(*) FROM memory.v5_2_atom_admission_operation
        ),
        'target_batches',(
          SELECT count(*) FROM memory.relational_stage_batch
          WHERE owner_user_id='$target_owner'::uuid
            AND evidence_id='$evidence_id'::uuid
        ),
        'target_mentions',(
          SELECT count(*) FROM memory.entity_mention
          WHERE owner_user_id='$target_owner'::uuid
            AND evidence_id='$evidence_id'::uuid
        ),
        'target_observations',(
          SELECT count(*) FROM memory.observation
          WHERE owner_user_id='$target_owner'::uuid
            AND evidence_id='$evidence_id'::uuid
        ),
        'claims',(SELECT count(*) FROM memory.claim)
      )::text)
    "
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
      (SELECT count(*) FROM memory.entity_resolution_plan AS resolution
       JOIN memory.entity_mention AS mention
         ON mention.owner_user_id=resolution.owner_user_id
        AND mention.mention_id=resolution.mention_id
       WHERE resolution.owner_user_id='$target_owner'::uuid
         AND mention.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_candidate AS candidate
       JOIN memory.entity_resolution_plan AS resolution
         ON resolution.owner_user_id=candidate.owner_user_id
        AND resolution.resolution_id=candidate.resolution_id
       JOIN memory.entity_mention AS mention
         ON mention.owner_user_id=resolution.owner_user_id
        AND mention.mention_id=resolution.mention_id
       WHERE candidate.owner_user_id='$target_owner'::uuid
         AND mention.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.observation
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.observation_temporal AS temporal
       JOIN memory.observation AS observation
         ON observation.owner_user_id=temporal.owner_user_id
        AND observation.observation_id=temporal.observation_id
       WHERE temporal.owner_user_id='$target_owner'::uuid
         AND observation.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_apply AS applied
       JOIN memory.entity_resolution_plan AS resolution
         ON resolution.owner_user_id=applied.owner_user_id
        AND resolution.resolution_id=applied.resolution_id
       JOIN memory.entity_mention AS mention
         ON mention.owner_user_id=resolution.owner_user_id
        AND mention.mention_id=resolution.mention_id
       WHERE applied.owner_user_id='$target_owner'::uuid
         AND mention.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.observation_entity_binding AS binding
       JOIN memory.observation AS observation
         ON observation.owner_user_id=binding.owner_user_id
        AND observation.observation_id=binding.observation_id
       WHERE binding.owner_user_id='$target_owner'::uuid
         AND observation.evidence_id='$evidence_id'::uuid),
      (SELECT count(*) FROM memory.claim_observation AS linked
       JOIN memory.observation AS observation
         ON observation.owner_user_id=linked.owner_user_id
        AND observation.observation_id=linked.observation_id
       WHERE linked.owner_user_id='$target_owner'::uuid
         AND observation.evidence_id='$evidence_id'::uuid)
    )
  "
}

qdrant_before=$(qdrant_signature)
production_before=$(production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc >"$backup"
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
  "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists <"$backup"

run_sql <"$planner"
run_sql <"$planner"
run_sql \
  -v target_owner="$target_owner" \
  -v other_owner="$other_owner" \
  -v apply_id="$apply_id" \
  -v evidence_id="$evidence_id" \
  -v projection_sha256="$projection_sha" \
  <"$planner_test"

assert_equal initial_target_stage "$(target_stage_counts)" '0,0,0,0,0,0,0,0,0,0'

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$builder" build \
  --manifest "$build_manifest" \
  --output-root "$reviews"

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$builder" probe \
  --bundle "$reviews/bundle.json" \
  --apply-id "$apply_id" \
  --other-owner-user-id "$other_owner"

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$stage_runner" plan \
  --manifest "$reviews/stage-manifest.json" \
  --review-root "$reviews" \
  --output "$reviews/stage-plan.json"

head=$(git -C "$repo_root" rev-parse HEAD)
/opt/chat-memory/venv/bin/python "$fixture" authorize \
  --plan "$reviews/stage-plan.json" \
  --output "$reviews/stage-authorization.json" \
  --head "$head"

MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$stage_runner" apply \
  --plan "$reviews/stage-plan.json" \
  --authorization "$reviews/stage-authorization.json" \
  --review-root "$reviews" \
  --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
  --output "$reviews/stage-apply.json"

assert_equal applied_target_stage "$(target_stage_counts)" '1,1,1,1,1,4,4,0,0,0'
assert_equal report_rows \
  "$(jq -r '.database_rows_created' "$reviews/stage-apply.json")" 13
assert_equal applied_outcome \
  "$(jq -r '.applied[0].outcome' "$reviews/stage-apply.json")" applied
assert_equal replay_outcome \
  "$(jq -r '.replayed[0].outcome' "$reviews/stage-apply.json")" replayed
assert_equal replay_counts \
  "$(jq -c '.replayed[0].counts' "$reviews/stage-apply.json")" \
  '{"candidates":0,"mentions":0,"observations":0,"resolutions":0,"temporals":0}'

run_sql <"$planner_rollback"
assert_equal planner_removed \
  "$(scalar "SELECT to_regprocedure(
    'memory.plan_owner_v5_2_atom_stage_v1(uuid)'
  ) IS NULL")" t
assert_equal staged_rows_survive_planner_rollback \
  "$(target_stage_counts)" '1,1,1,1,1,4,4,0,0,0'

assert_equal qdrant_unchanged "$(qdrant_signature)" "$qdrant_before"
assert_equal production_rows_unchanged \
  "$(production_signature)" "$production_before"
assert_equal production_head_unchanged \
  "$(git -C /opt/chat-memory rev-parse HEAD)" "$production_head_before"
assert_equal brains_service "$(systemctl is-active brains.service)" active
docker exec brains-postgres-1 pg_isready -U sage -d memory >/dev/null

printf '%s\n' \
  'MEMORY_V1_V5_2_ATOM_STAGE_PRODUCTION_CLONE=PASS' \
  "owner=$target_owner" \
  "apply_id=$apply_id" \
  'clone_rows_created=13' \
  'same_run_replay_rows=0' \
  'entity_resolution_apply_rows=0' \
  'observation_binding_rows=0' \
  'claim_links=0' \
  'production_writes=0' \
  'qdrant_writes=0' \
  'external_model_calls=0' \
  'hard_stop=before_live_production_staging'

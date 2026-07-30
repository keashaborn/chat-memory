#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies one exact reviewed compiler-v9 stance packet in
# a disposable production clone and proves zero-write replay and isolation.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

port=${MEMORY_V1_V5_2_V9_STANCE_ATOM_CLONE_PORT:-55519}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
project=memoryv1v52v9stanceatomclone
compose=(
  docker compose
  -p "$project"
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
manifest=manifests/memory_v1_v5_2_semantic_compiler_v9_stance_atom_admission_20260730.json
runner=scripts/memory_v1_v5_2_atom_admission_apply_v2.py
bundle_builder=scripts/memory_v1_v5_2_atom_stage_bundle_v2.py
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
stage_fixture=tests/memory_v1_v5_2_stage_batch_fixture.py
entity_runner=scripts/memory_v1_v5_2_entity_resolution_batch.py
entity_fixture=tests/memory_v1_v5_2_entity_resolution_batch_fixture.py
python_bin=/opt/chat-memory/venv/bin/python
stage_test=${MEMORY_V1_V5_2_V9_STANCE_STAGE_TEST:-0}
entity_test=${MEMORY_V1_V5_2_V9_STANCE_ENTITY_TEST:-0}
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
self_entity=35029129-27bd-457b-8cb5-82dd37ba32ba
packet=50405677-84aa-5d72-b800-89418fc606e4
evidence=61d4fb6f-b211-491e-8edc-d160efefe17e
proposal=e4b17ef6-5012-5f1a-ae2a-90681fd71d8f
review=aa43ff43-3d9d-5eaf-8972-229f6f2e261c
apply=237451bc-2fcc-5272-b357-9dd2f6452e0d
proposal_operation=ac3c747b-d8e4-5d61-b0d4-0c0964495241
review_operation=7eb805c2-2545-5428-a8cd-31e9ca6f6477
apply_operation=239d3aed-e211-5cb1-becd-34c5baac84e1
manifest_file_sha=9ed0a6a5b726da412ca1404ffe6a9746f4e97167099ed4c54196edf2e3ba366a
manifest_contract_sha=8ea964ad2dd9cf2bb6024de775e21f05c96fd4e949b0ca5b8908418a038b54af
runner_sha=721184f8eebf5f4d14b553eb0cf0134960456d8834029263da1d10963868c5e7
bundle_builder_sha=5a611180ecbb3fa3b9220459ef870879e74a8d487847ef76084b379c32ac30bb
stage_runner_sha=3dddef3dbf71862fc42252d6263075ad8d407bc292a4f06760866c816c3699b9
stage_fixture_sha=96461968b5afa0ec2a8aa207ec7ec2096278d851cd51d4e0a7c90541ab5a54ac
entity_runner_sha=7c9c16ba640101acea04b8911fd179d15a13166b4a8069b129e6aa8e4d6487bb
entity_fixture_sha=9d913127b681c0e781daae045357e24cd000a05e3702699d72caefe9f5f682e7
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

backup=$(mktemp /tmp/memory-v9-stance-atom.XXXXXX.dump)
roles=$(mktemp /tmp/memory-v9-stance-atom.XXXXXX.roles)
work=$(mktemp -d /tmp/memory-v9-stance-atom.XXXXXX)
reviews="$work/reviews"
chmod 0600 "$backup" "$roles"
mkdir -m 0700 "$reviews"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$roles"
  rm -rf "$work"
}
trap cleanup EXIT

fail() {
  printf 'ASSERTION_FAILED=%s\n' "$1" >&2
  exit 1
}

equal() {
  [[ "$2" == "$3" ]] || {
    printf 'ASSERTION_FAILED=%s\nexpected=%s\nactual=%s\n' \
      "$1" "$3" "$2" >&2
    exit 1
  }
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
      SELECT encode(public.digest(convert_to(jsonb_build_object(
        'proposals',(SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
        'reviews',(SELECT count(*) FROM memory.v5_2_atom_admission_review),
        'applies',(SELECT count(*) FROM memory.v5_2_atom_admission_apply),
        'operations',(SELECT count(*) FROM memory.v5_2_atom_admission_operation),
        'stage_batches',(SELECT count(*) FROM memory.relational_stage_batch),
        'mentions',(SELECT count(*) FROM memory.entity_mention),
        'observations',(SELECT count(*) FROM memory.observation),
        'claims',(SELECT count(*) FROM memory.claim)
      )::text,'UTF8'),'sha256'),'hex')
    "
}

target_downstream() {
  scalar "
    SELECT concat_ws(',',
      (SELECT count(*) FROM memory.relational_stage_batch
       WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.entity_mention
       WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.observation
       WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.claim_evidence
       WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid)
    )
  "
}

target_stage_counts() {
  scalar "
    SELECT concat_ws(',',
      (SELECT count(*) FROM memory.relational_stage_batch
       WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.relational_operation_request
       WHERE owner_user_id='$owner'::uuid AND target_key='$evidence'),
      (SELECT count(*) FROM memory.entity_mention
       WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_plan
       WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_candidate AS candidate
       JOIN memory.entity_resolution_plan AS resolution
         USING(owner_user_id,resolution_id)
       WHERE resolution.owner_user_id='$owner'::uuid
         AND resolution.evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.observation
       WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.observation_temporal AS temporal
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$owner'::uuid
         AND observation.evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_review AS review_row
       JOIN memory.entity_resolution_plan AS resolution
         USING(owner_user_id,resolution_id)
       WHERE resolution.owner_user_id='$owner'::uuid
         AND resolution.evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.entity_resolution_apply AS applied
       JOIN memory.entity_resolution_plan AS resolution
         USING(owner_user_id,resolution_id)
       WHERE resolution.owner_user_id='$owner'::uuid
         AND resolution.evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.observation_entity_binding AS binding
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$owner'::uuid
         AND observation.evidence_id='$evidence'::uuid),
      (SELECT count(*) FROM memory.claim_observation AS linked
       JOIN memory.observation AS observation
         USING(owner_user_id,observation_id)
       WHERE observation.owner_user_id='$owner'::uuid
         AND observation.evidence_id='$evidence'::uuid)
    )
  "
}

equal manifest_file_sha "$(sha256sum "$manifest" | awk '{print $1}')" \
  "$manifest_file_sha"
equal manifest_contract_sha "$(jq -r .manifest_sha256 "$manifest")" \
  "$manifest_contract_sha"
equal runner_sha "$(sha256sum "$runner" | awk '{print $1}')" "$runner_sha"
equal bundle_builder_sha "$(sha256sum "$bundle_builder" | awk '{print $1}')" \
  "$bundle_builder_sha"
equal stage_runner_sha "$(sha256sum "$stage_runner" | awk '{print $1}')" \
  "$stage_runner_sha"
equal stage_fixture_sha "$(sha256sum "$stage_fixture" | awk '{print $1}')" \
  "$stage_fixture_sha"
equal entity_runner_sha "$(sha256sum "$entity_runner" | awk '{print $1}')" \
  "$entity_runner_sha"
equal entity_fixture_sha "$(sha256sum "$entity_fixture" | awk '{print $1}')" \
  "$entity_fixture_sha"
[[ "$stage_test" == 0 || "$stage_test" == 1 ]] || fail invalid_stage_test
[[ "$entity_test" == 0 || "$entity_test" == 1 ]] || fail invalid_entity_test
[[ "$entity_test" == 0 || "$stage_test" == 1 ]] || fail entity_test_requires_stage
equal manifest_items "$(jq -r '.items|length' "$manifest")" 1
equal manifest_packet "$(jq -r '.items[0].packet_id' "$manifest")" "$packet"
equal manifest_evidence "$(jq -r '.items[0].evidence_id' "$manifest")" "$evidence"
equal manifest_proposal "$(jq -r '.items[0].proposal_id' "$manifest")" "$proposal"
equal manifest_review "$(jq -r '.items[0].review_id' "$manifest")" "$review"
equal manifest_apply "$(jq -r '.items[0].apply_id' "$manifest")" "$apply"

qdrant_before=$(qdrant_signature)
production_before=$(production_signature)
head_before=$(git -C /opt/chat-memory rev-parse HEAD)

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner >"$backup"
[[ -s "$backup" ]] || fail empty_backup
docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c "
  SELECT format(
    'CREATE ROLE %I %s %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;',
    rolname,
    CASE WHEN rolcanlogin THEN 'LOGIN' ELSE 'NOLOGIN' END,
    CASE WHEN rolinherit THEN 'INHERIT' ELSE 'NOINHERIT' END
  )
  FROM pg_roles
  WHERE rolname NOT LIKE 'pg\_%' ESCAPE '\'
    AND rolname NOT IN ('sage','postgres')
  ORDER BY rolname
" >"$roles"
[[ -s "$roles" ]] || fail empty_role_manifest

"${compose[@]}" up -d --wait postgres
run_sql <"$roles"
printf '%s\n' "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner <"$backup"

equal target_atom_rows_before "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal
     WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review
     WHERE owner_user_id='$owner'::uuid AND proposal_id='$proposal'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply
     WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation
     WHERE owner_user_id='$owner'::uuid
       AND operation_id IN (
         '$proposal_operation'::uuid,
         '$review_operation'::uuid,
         '$apply_operation'::uuid
       ))
  )
")" 0,0,0,0
equal target_downstream_before "$(target_downstream)" 0,0,0,0

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" "$python_bin" "$runner" \
  --mode preflight --manifest "$manifest" --output "$work/preflight.json"
equal preflight_writes "$(jq -r .persistent_writes "$work/preflight.json")" 0
equal target_atom_rows_after_preflight "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal
     WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review
     WHERE owner_user_id='$owner'::uuid AND proposal_id='$proposal'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply
     WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid)
  )
")" 0,0,0

MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY_V2=authorized \
POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" "$python_bin" "$runner" \
  --mode apply --manifest "$manifest" --output "$work/apply.json"
equal apply_writes "$(jq -r .persistent_writes "$work/apply.json")" 6
equal apply_outcome "$(jq -r '.results[0].proposal_outcome' "$work/apply.json")" \
  applied
equal target_atom_rows_after_apply "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal
     WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review
     WHERE owner_user_id='$owner'::uuid AND proposal_id='$proposal'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply
     WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation
     WHERE owner_user_id='$owner'::uuid
       AND operation_id IN (
         '$proposal_operation'::uuid,
         '$review_operation'::uuid,
         '$apply_operation'::uuid
       ))
  )
")" 1,1,1,3
equal target_downstream_after_apply "$(target_downstream)" 0,0,0,0

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" "$python_bin" "$runner" \
  --mode replay --manifest "$manifest" --output "$work/replay.json"
equal replay_writes "$(jq -r .persistent_writes "$work/replay.json")" 0
equal replay_outcome "$(jq -r '.results[0].proposal_outcome' "$work/replay.json")" \
  replayed
equal target_atom_rows_after_replay "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal
     WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review
     WHERE owner_user_id='$owner'::uuid AND proposal_id='$proposal'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply
     WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation
     WHERE owner_user_id='$owner'::uuid
       AND operation_id IN (
         '$proposal_operation'::uuid,
         '$review_operation'::uuid,
         '$apply_operation'::uuid
      ))
  )
")" 1,1,1,3

if [[ "$stage_test" == 1 ]]; then
  equal target_stage_before "$(target_stage_counts)" 0,0,0,0,0,0,0,0,0,0,0
  apply_manifest_sha=$(scalar "
    SELECT apply_manifest_sha256
    FROM memory.v5_2_atom_admission_apply
    WHERE owner_user_id='$owner'::uuid AND apply_id='$apply'::uuid
  ")
  stage_projection_sha=$(scalar "
    SELECT stage_projection_sha256
    FROM memory.v5_2_atom_admission_apply
    WHERE owner_user_id='$owner'::uuid AND apply_id='$apply'::uuid
  ")
  jq -n \
    --arg owner "$owner" \
    --arg apply "$apply" \
    --arg proposal "$proposal" \
    --arg review "$review" \
    --arg packet "$packet" \
    --arg evidence "$evidence" \
    --arg apply_manifest_sha "$apply_manifest_sha" \
    --arg proposal_sha 4b0508eb56cc9db8454363834b8eb873ad653d55908a6428cb17ab07f77b8894 \
    --arg stage_projection_sha "$stage_projection_sha" \
    --arg self_entity "$self_entity" \
    '{
      contract_version:"memory_v1_v5_2_atom_stage_build_manifest_v2",
      target_server:"seebx",
      owner_user_id:$owner,
      case_id:"semantic_compiler_v9_human_being_as_fractal_stance",
      apply_id:$apply,
      proposal_id:$proposal,
      review_id:$review,
      packet_id:$packet,
      evidence_id:$evidence,
      apply_manifest_sha256:$apply_manifest_sha,
      proposal_sha256:$proposal_sha,
      stage_projection_sha256:$stage_projection_sha,
      expected_counts:{
        entity_mentions:1,observations:1,comparison_hints:0,deferrals:0
      },
      expected_resolutions:{
        e00:{
          action:"link_existing",
          decision_state:"auto_link_eligible",
          selected_entity_id:$self_entity,
          proposed_entity:null,
          review_reason_codes:["trusted_owner_self_binding"]
        }
      }
    }' >"$work/stage-build-manifest.json"
  chmod 0600 "$work/stage-build-manifest.json"

  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
    "$python_bin" "$bundle_builder" build \
    --manifest "$work/stage-build-manifest.json" \
    --output-root "$reviews" >"$work/stage-build-report.json"
  equal stage_build_writes \
    "$(jq -r .database_writes "$work/stage-build-report.json")" 0
  equal stage_build_expected_rows \
    "$(jq -r .expected_new_rows "$work/stage-build-report.json")" 7

  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
    "$python_bin" "$bundle_builder" probe \
    --bundle "$reviews/bundle.json" \
    --apply-id "$apply" \
    --other-owner-user-id "$other_owner" >"$work/stage-probe.json"
  equal stage_probe_cross_owner \
    "$(jq -r .cross_owner_rejection "$work/stage-probe.json")" P0002
  equal stage_probe_tamper \
    "$(jq -r .tampered_projection_rejection "$work/stage-probe.json")" 23514

  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
    "$python_bin" "$stage_runner" plan \
    --manifest "$reviews/stage-manifest.json" \
    --review-root "$reviews" \
    --output "$reviews/stage-plan.json"
  "$python_bin" "$stage_fixture" authorize \
    --plan "$reviews/stage-plan.json" \
    --output "$reviews/stage-authorization.json" \
    --head "$(git rev-parse HEAD)"

  MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
    "$python_bin" "$stage_runner" apply \
    --plan "$reviews/stage-plan.json" \
    --authorization "$reviews/stage-authorization.json" \
    --review-root "$reviews" \
    --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
    --output "$reviews/stage-apply.json"

  equal stage_rows_created \
    "$(jq -r .database_rows_created "$reviews/stage-apply.json")" 7
  equal stage_apply_outcome \
    "$(jq -r '.applied[0].outcome' "$reviews/stage-apply.json")" applied
  equal stage_replay_outcome \
    "$(jq -r '.replayed[0].outcome' "$reviews/stage-apply.json")" replayed
  equal stage_replay_rows \
    "$(jq -c '.replayed[0].counts' "$reviews/stage-apply.json")" \
    '{"candidates":0,"mentions":0,"observations":0,"resolutions":0,"temporals":0}'
  equal target_stage_after "$(target_stage_counts)" 1,1,1,1,1,1,1,0,0,0,0
  equal self_resolution "$(scalar "
    SELECT concat_ws('|',action::text,decision_state::text,
      selected_entity_id::text,review_reason_codes::text)
    FROM memory.entity_resolution_plan
    WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid
  ")" "link_existing|auto_link_eligible|$self_entity|[\"trusted_owner_self_binding\"]"
  equal observation_predicate "$(scalar "
    SELECT predicate FROM memory.observation
    WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid
  ")" stance.reported

  if [[ "$entity_test" == 1 ]]; then
    resolution=$(scalar "
      SELECT resolution_id::text
      FROM memory.entity_resolution_plan
      WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid
    ")
    observation=$(scalar "
      SELECT observation_id::text
      FROM memory.observation
      WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid
    ")
    [[ "$resolution" =~ ^[0-9a-f-]{36}$ ]] || fail missing_resolution_id
    [[ "$observation" =~ ^[0-9a-f-]{36}$ ]] || fail missing_observation_id

    jq -n \
      --arg owner "$owner" \
      --arg resolution "$resolution" \
      '{
        contract_version:"memory_v1_v5_2_entity_resolution_batch_manifest_v1",
        target_server:"seebx",
        owner_user_id:$owner,
        expected_total_bindings:1,
        expected_new_rows:3,
        items:[{
          resolution_id:$resolution,
          operation:"auto_apply",
          expected_action:"link_existing",
          expected_decision_state:"auto_link_eligible",
          review_reason:null
        }]
      }' >"$reviews/entity-manifest.json"
    chmod 0600 "$reviews/entity-manifest.json"

    POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
      "$python_bin" "$entity_runner" plan \
      --manifest "$reviews/entity-manifest.json" \
      --review-root "$reviews" \
      --output "$reviews/entity-plan.json"

    jq --arg owner "$other_owner" '.owner_user_id=$owner' \
      "$reviews/entity-manifest.json" >"$reviews/entity-cross-owner.json"
    chmod 0600 "$reviews/entity-cross-owner.json"
    if POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
      "$python_bin" "$entity_runner" plan \
      --manifest "$reviews/entity-cross-owner.json" \
      --review-root "$reviews" \
      --output "$reviews/entity-cross-owner-plan.json" >/dev/null 2>&1; then
      fail cross_owner_entity_plan_was_not_rejected
    fi

    PYTHONPATH="$repo_root" "$python_bin" "$entity_fixture" \
      --plan "$reviews/entity-plan.json" \
      --output "$reviews/entity-authorization.json" \
      --head "$(git rev-parse HEAD)"

    MEMORY_V1_V5_2_ENTITY_RESOLUTION_BATCH_APPLY=authorized \
    POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
      "$python_bin" "$entity_runner" apply \
      --plan "$reviews/entity-plan.json" \
      --authorization "$reviews/entity-authorization.json" \
      --review-root "$reviews" \
      --confirm RECONCILE_REVIEW_AND_APPLY_OWNER_V5_2_ENTITY_RESOLUTIONS_ONLY \
      --output "$reviews/entity-apply.json"

    equal entity_rows_created \
      "$(jq -r .database_rows_created "$reviews/entity-apply.json")" 3
    equal entity_bindings_created \
      "$(jq -r .bindings_created "$reviews/entity-apply.json")" 1
    equal entity_apply_operation \
      "$(jq -r '.applied[0].operation' "$reviews/entity-apply.json")" auto_apply
    equal entity_apply_target \
      "$(jq -r '.applied[0].applied_entity_id' "$reviews/entity-apply.json")" \
      "$self_entity"
    equal entity_replay_outcome \
      "$(jq -r '.replayed[0].apply_outcome' "$reviews/entity-apply.json")" replayed
    equal entity_replay_bindings \
      "$(jq -r '.replayed[0].bindings_created' "$reviews/entity-apply.json")" 0
    equal target_stage_after_entity "$(target_stage_counts)" \
      1,2,1,1,1,1,1,0,1,1,0
    equal exact_entity_apply "$(scalar "
      SELECT count(*)
      FROM memory.entity_resolution_apply
      WHERE owner_user_id='$owner'::uuid
        AND resolution_id='$resolution'::uuid
        AND applied_entity_id='$self_entity'::uuid
    ")" 1
    equal exact_observation_binding "$(scalar "
      SELECT count(*)
      FROM memory.observation_entity_binding
      WHERE owner_user_id='$owner'::uuid
        AND observation_id='$observation'::uuid
        AND subject_entity_id='$self_entity'::uuid
        AND subject_resolution_id='$resolution'::uuid
        AND object_entity_id IS NULL
        AND object_resolution_id IS NULL
    ")" 1
  fi
fi

if scalar "
  BEGIN;
  SET LOCAL ROLE brains_app;
  SELECT set_config('app.user_id','$other_owner',true);
  SELECT memory.plan_owner_v5_2_atom_admission_v2('$packet'::uuid);
  ROLLBACK;
" >/dev/null 2>&1; then
  fail cross_owner_plan_was_not_rejected
fi

equal production_signature "$(production_signature)" "$production_before"
equal production_head "$(git -C /opt/chat-memory rev-parse HEAD)" "$head_before"
equal qdrant_signature "$(qdrant_signature)" "$qdrant_before"
equal brains_service "$(systemctl is-active brains.service)" active
docker exec brains-postgres-1 pg_isready -U sage -d memory >/dev/null

stage_rows=0
[[ "$stage_test" == 0 ]] || stage_rows=7
entity_resolution_apply_rows=0
observation_binding_rows=0
if [[ "$entity_test" == 1 ]]; then
  entity_resolution_apply_rows=1
  observation_binding_rows=1
fi
printf '%s\n' \
  'MEMORY_V1_V5_2_SEMANTIC_COMPILER_V9_STANCE_ATOM_CLONE=PASS' \
  "stage_test=$stage_test" \
  "entity_test=$entity_test" \
  'clone_writes=6' \
  'replay_writes=0' \
  "stage_rows=$stage_rows" \
  "entity_resolution_apply_rows=$entity_resolution_apply_rows" \
  "observation_binding_rows=$observation_binding_rows" \
  'claim_rows=0' \
  'qdrant_writes=0' \
  'external_model_calls=0' \
  'cross_owner_rejected=true'

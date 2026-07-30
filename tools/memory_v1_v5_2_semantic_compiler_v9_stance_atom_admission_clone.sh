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
python_bin=/opt/chat-memory/venv/bin/python
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
packet=50405677-84aa-5d72-b800-89418fc606e4
evidence=61d4fb6f-b211-491e-8edc-d160efefe17e
proposal=e4b17ef6-5012-5f1a-ae2a-90681fd71d8f
review=aa43ff43-3d9d-5eaf-8972-229f6f2e261c
apply=237451bc-2fcc-5272-b357-9dd2f6452e0d
manifest_file_sha=9ed0a6a5b726da412ca1404ffe6a9746f4e97167099ed4c54196edf2e3ba366a
manifest_contract_sha=8ea964ad2dd9cf2bb6024de775e21f05c96fd4e949b0ca5b8908418a038b54af
runner_sha=721184f8eebf5f4d14b553eb0cf0134960456d8834029263da1d10963868c5e7
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

backup=$(mktemp /tmp/memory-v9-stance-atom.XXXXXX.dump)
roles=$(mktemp /tmp/memory-v9-stance-atom.XXXXXX.roles)
work=$(mktemp -d /tmp/memory-v9-stance-atom.XXXXXX)
chmod 0600 "$backup" "$roles"

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

equal manifest_file_sha "$(sha256sum "$manifest" | awk '{print $1}')" \
  "$manifest_file_sha"
equal manifest_contract_sha "$(jq -r .manifest_sha256 "$manifest")" \
  "$manifest_contract_sha"
equal runner_sha "$(sha256sum "$runner" | awk '{print $1}')" "$runner_sha"
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
       AND manifest_sha256='$manifest_contract_sha')
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

MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY=authorized \
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
       AND manifest_sha256='$manifest_contract_sha')
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
       AND manifest_sha256='$manifest_contract_sha')
  )
")" 1,1,1,3

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

printf '%s\n' \
  'MEMORY_V1_V5_2_SEMANTIC_COMPILER_V9_STANCE_ATOM_CLONE=PASS' \
  'clone_writes=6' \
  'replay_writes=0' \
  'downstream_writes=0' \
  'qdrant_writes=0' \
  'external_model_calls=0' \
  'cross_owner_rejected=true'

#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_2_ATOM_APPLY_CLONE_PORT:-55453}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52atomapplyclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
manifest=ops/manifests/memory_v1_v5_2_atom_admission_apply_manifest_20260724.json
runner=scripts/memory_v1_v5_2_atom_admission_apply.py
authority_compat=ops/sql/20260724_memory_v1_v5_2_packet_authority_compat.sql
exact_stage_guard=ops/sql/20260724_memory_v1_v5_2_exact_packet_stage_guard.sql
atom_migration=ops/sql/20260724_memory_v1_v5_2_atom_admission.sql
backup=$(mktemp /tmp/memory-v1-v5-2-atom-apply.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-atom-apply-roles.XXXXXX.sql)
work=$(mktemp -d /tmp/memory-v1-v5-2-atom-apply.XXXXXX)
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$role_sql"
  rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup" "$role_sql"

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
      SELECT md5(jsonb_build_object(
        'proposals',(
          SELECT count(*) FROM memory.v5_2_atom_admission_proposal
        ),
        'reviews',(SELECT count(*) FROM memory.v5_2_atom_admission_review),
        'applies',(SELECT count(*) FROM memory.v5_2_atom_admission_apply),
        'operations',(
          SELECT count(*) FROM memory.v5_2_atom_admission_operation
        ),
        'stage_batches',(SELECT count(*) FROM memory.relational_stage_batch),
        'mentions',(SELECT count(*) FROM memory.entity_mention),
        'observations',(SELECT count(*) FROM memory.observation),
        'claims',(SELECT count(*) FROM memory.claim)
      )::text)
    "
}

qdrant_before=$(qdrant_signature)
production_before=$(production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner >"$backup"
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
  --clean --if-exists --no-owner <"$backup"
run_sql <"$authority_compat"
run_sql <"$exact_stage_guard"
run_sql <"$atom_migration"

[[ "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation)
  )
")" == '0,0,0,0' ]]
downstream_before=$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.relational_stage_batch),
    (SELECT count(*) FROM memory.entity_mention),
    (SELECT count(*) FROM memory.observation),
    (SELECT count(*) FROM memory.claim)
  )
")

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" \
  --mode preflight --manifest "$manifest" --output "$work/preflight.json"
[[ "$(jq -r '.mode' "$work/preflight.json")" == preflight ]]
[[ "$(jq -r '.persistent_writes' "$work/preflight.json")" == 0 ]]
[[ "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation)
  )
")" == '0,0,0,0' ]]

MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" \
  --mode apply --manifest "$manifest" --output "$work/apply.json"
[[ "$(jq -r '.mode' "$work/apply.json")" == apply ]]
[[ "$(jq -r '.persistent_writes' "$work/apply.json")" == 10 ]]
[[ "$(jq -r '[.results[].proposal_outcome] | unique | join(\",\")' \
  "$work/apply.json")" == applied ]]
[[ "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation)
  )
")" == '2,2,1,5' ]]

POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$runner" \
  --mode replay --manifest "$manifest" --output "$work/replay.json"
[[ "$(jq -r '.mode' "$work/replay.json")" == replay ]]
[[ "$(jq -r '.persistent_writes' "$work/replay.json")" == 0 ]]
[[ "$(jq -r '[.results[].proposal_outcome] | unique | join(\",\")' \
  "$work/replay.json")" == replayed ]]
[[ "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation)
  )
")" == '2,2,1,5' ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_2_atom_admission_apply AS applied
  JOIN memory.v5_2_atom_admission_proposal AS proposal
    ON proposal.owner_user_id=applied.owner_user_id
   AND proposal.proposal_id=applied.proposal_id
  WHERE applied.owner_user_id=
    '1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
    AND proposal.packet_id=
      '78ca7a3e-e136-535a-9fe6-1d83aefff806'::uuid
")" == 1 ]]
[[ "$(scalar "
  SELECT count(*) FROM memory.v5_2_atom_admission_apply AS applied
  JOIN memory.v5_2_atom_admission_proposal AS proposal
    ON proposal.owner_user_id=applied.owner_user_id
   AND proposal.proposal_id=applied.proposal_id
  WHERE proposal.packet_id=
    'a5624f05-8d75-5b96-bfd7-9f56145f7ad9'::uuid
")" == 0 ]]
[[ "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.relational_stage_batch),
    (SELECT count(*) FROM memory.entity_mention),
    (SELECT count(*) FROM memory.observation),
    (SELECT count(*) FROM memory.claim)
  )
")" == "$downstream_before" ]]

[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(production_signature)" == "$production_before" ]]
[[ "$(git -C /opt/chat-memory rev-parse HEAD)" == "$production_head_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
docker exec brains-postgres-1 pg_isready -U sage -d memory >/dev/null

printf '%s\n' \
  "MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY_CLONE=PASS" \
  "manifest_sha256=$(jq -r '.manifest_sha256' "$manifest")" \
  "clone_rows=2,2,1,5" \
  "production_head=$production_head_before" \
  "qdrant_signature=$qdrant_before"

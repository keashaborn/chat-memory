#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_2_ATOM_ADMISSION_CLONE_PORT:-55452}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52atomadmissionclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260724_memory_v1_v5_2_atom_admission.sql
rollback=ops/sql/20260724_memory_v1_v5_2_atom_admission_rollback.sql
test_sql=tests/memory_v1_v5_2_atom_admission.sql
authority_compat=ops/sql/20260724_memory_v1_v5_2_packet_authority_compat.sql
exact_stage_guard=ops/sql/20260724_memory_v1_v5_2_exact_packet_stage_guard.sql
backup=$(mktemp /tmp/memory-v1-v5-2-atom-admission.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-atom-admission-roles.XXXXXX.sql)
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
stance_packet=78ca7a3e-e136-535a-9fe6-1d83aefff806
preference_packet=a5624f05-8d75-5b96-bfd7-9f56145f7ad9
superseded_packet=e21e39bb-20fe-5b95-bc72-77fd774f0faf

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$role_sql"
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

production_scalar() {
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
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
  production_scalar "
    SELECT md5(jsonb_build_object(
      'stage_batches',(SELECT count(*) FROM memory.relational_stage_batch),
      'mentions',(SELECT count(*) FROM memory.entity_mention),
      'observations',(SELECT count(*) FROM memory.observation),
      'claims',(SELECT count(*) FROM memory.claim),
      'claim_revisions',(SELECT count(*) FROM memory.claim_revision)
    )::text)
  "
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

guard_before=$(scalar "
  SELECT encode(digest(
    pg_get_functiondef(
      'memory.guard_v5_2_terminal_evidence_from_stage_v1()'::regprocedure
    ),'sha256'
  ),'hex')
")

run_sql <"$migration"
run_sql <"$migration"
run_sql \
  -v target_owner="$target_owner" \
  -v other_owner="$other_owner" \
  -v stance_packet="$stance_packet" \
  -v preference_packet="$preference_packet" \
  -v superseded_packet="$superseded_packet" \
  <"$test_sql"

[[ "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation)
  )
")" == "0,0,0,0" ]]

run_sql <"$rollback"

guard_after=$(scalar "
  SELECT encode(digest(
    pg_get_functiondef(
      'memory.guard_v5_2_terminal_evidence_from_stage_v1()'::regprocedure
    ),'sha256'
  ),'hex')
")
assert_equal guard_restored "$guard_after" "$guard_before"
objects_absent=$(scalar "
  SELECT (
    to_regrole('memory_v5_2_atom_admission_maintainer') IS NULL
    AND to_regclass('memory.v5_2_atom_admission_proposal') IS NULL
    AND to_regprocedure(
      'memory.plan_owner_v5_2_atom_admission_v1(uuid)'
    ) IS NULL
  )::int
")
assert_equal rollback_objects_absent "$objects_absent" "1"
assert_equal qdrant_unchanged "$(qdrant_signature)" "$qdrant_before"
assert_equal production_rows_unchanged \
  "$(production_signature)" "$production_before"
assert_equal production_head_unchanged \
  "$(git -C /opt/chat-memory rev-parse HEAD)" "$production_head_before"
assert_equal brains_service "$(systemctl is-active brains.service)" "active"
docker exec brains-postgres-1 pg_isready -U sage -d memory >/dev/null

printf '%s\n' \
  "MEMORY_V1_V5_2_ATOM_ADMISSION_CLONE=PASS" \
  "production_head=$production_head_before" \
  "guard_restored=$guard_after" \
  "qdrant_signature=$qdrant_before" \
  "production_signature=$production_before"

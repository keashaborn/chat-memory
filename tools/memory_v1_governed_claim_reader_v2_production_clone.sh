#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
compose=(docker compose -p memoryv1governedreaderv2 -f docker-compose.ci.yml)
migration=ops/sql/20260722_memory_v1_governed_claim_reader_v2.sql
rollback=ops/sql/20260722_memory_v1_governed_claim_reader_v2_rollback.sql
test_sql=tests/memory_v1_governed_claim_reader_v2.sql
backup=$(mktemp /tmp/memory-v1-governed-reader-v2.XXXXXX.dump)
before=$(mktemp /tmp/memory-v1-governed-reader-v2-before.XXXXXX.tsv)
after=$(mktemp /tmp/memory-v1-governed-reader-v2-after.XXXXXX.tsv)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$before" "$after"
}
trap cleanup EXIT
chmod 0600 "$backup" "$before" "$after"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

run_as_brains() {
  "${compose[@]}" exec -T \
    -e PGPASSWORD=clone_only_brains_password postgres \
    psql -X -v ON_ERROR_STOP=1 -U brains_app -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

capture_state() {
  local output=$1
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(
        public.digest(
          convert_to(
            coalesce(string_agg(row_value,E'\\n' ORDER BY row_value),''),
            'UTF8'
          ),
          'sha256'
        ),
        'hex'
      )
      FROM (
        SELECT to_jsonb(row_value)::text AS row_value
        FROM \"$schema\".\"$table\" AS row_value
      ) AS rows
    ")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done < <(scalar "
    SELECT table_schema || E'\\t' || table_name
    FROM information_schema.tables
    WHERE table_type='BASE TABLE'
      AND table_schema IN ('memory','public')
    ORDER BY table_schema,table_name
  ")
}

for required in "$migration" "$rollback" "$test_sql"; do
  [[ -f "$repo_root/$required" ]]
done

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]

"${compose[@]}" up -d --wait postgres
memory_role_sql=$(docker exec brains-postgres-1 psql -X -A -t \
  -U sage -d memory -v ON_ERROR_STOP=1 -c "
    SELECT format(
      'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;',
      rolname
    )
    FROM pg_roles
    WHERE rolname LIKE 'memory\\_%' ESCAPE '\\'
    ORDER BY rolname
  ")
[[ -n "$memory_role_sql" ]]
printf '%s\n' \
  "CREATE ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;" \
  "$memory_role_sql" \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"

run_sql <<'SQL'
ALTER FUNCTION memory.current_actor_user_id() OWNER TO memory_v5_writer;
ALTER FUNCTION memory.require_v5_reader_context() OWNER TO memory_v5_reader;
ALTER FUNCTION memory.read_v5_shadow_claims(uuid[]) OWNER TO memory_v5_reader;
ALTER FUNCTION memory.read_governed_claims_v1(uuid[]) OWNER TO memory_v5_reader;
GRANT USAGE ON SCHEMA memory TO brains_app,memory_v5_reader;
GRANT SELECT ON
  memory.claim,memory.claim_observation,memory.entity,memory.evidence,
  memory.observation,memory.observation_temporal,
  memory.projection_apply_event
TO memory_v5_reader;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO memory_v5_reader;
GRANT EXECUTE ON FUNCTION memory.require_v5_reader_context() TO memory_v5_reader;
GRANT EXECUTE ON FUNCTION memory.read_v5_shadow_claims(uuid[]) TO memory_v5_reader;
GRANT EXECUTE ON FUNCTION memory.read_governed_claims_v1(uuid[]) TO brains_app;
SQL

capture_state "$before"
run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"

fixture=$(scalar "
  SELECT concat_ws('|',
    claim.owner_user_id::text,
    claim.claim_id::text,
    claim.subject_entity_id::text,
    subject_entity.entity_type,
    coalesce(claim.object_entity_id::text,''),
    coalesce(object_entity.entity_type,''),
    coalesce((
      SELECT other.claim_id::text
      FROM memory.claim AS other
      WHERE other.owner_user_id<>claim.owner_user_id
      ORDER BY other.owner_user_id,other.claim_id
      LIMIT 1
    ),'')
  )
  FROM memory.claim AS claim
  JOIN memory.entity AS subject_entity
    ON subject_entity.owner_user_id=claim.owner_user_id
   AND subject_entity.entity_id=claim.subject_entity_id
  LEFT JOIN memory.entity AS object_entity
    ON object_entity.owner_user_id=claim.owner_user_id
   AND object_entity.entity_id=claim.object_entity_id
  WHERE claim.status::text IN ('supported','uncertain','disputed')
    AND EXISTS (
      SELECT 1
      FROM memory.projection_apply_event AS event
      WHERE event.owner_user_id=claim.owner_user_id
        AND event.resulting_claim_id=claim.claim_id
        AND event.outcome='applied'
    )
  ORDER BY claim.owner_user_id,claim.claim_id
  LIMIT 1
")
[[ -n "$fixture" ]]
IFS='|' read -r target_owner target_claim subject_id subject_type \
  object_id object_type other_claim <<<"$fixture"
[[ -n "$target_owner" && -n "$target_claim" && -n "$subject_id" && -n "$subject_type" ]]

run_as_brains \
  -v target_owner_user_id="$target_owner" \
  -v target_claim_id="$target_claim" \
  -v subject_entity_id="$subject_id" \
  -v subject_entity_type="$subject_type" \
  -v object_entity_id="$object_id" \
  -v object_entity_type="$object_type" \
  -v other_owner_claim_id="$other_claim" \
  <"$repo_root/$test_sql"

capture_state "$after"
diff -u "$before" "$after"

run_sql <"$repo_root/$rollback"
[[ "$(scalar "SELECT (
  to_regprocedure('memory.read_governed_claims_v2(uuid[])') IS NULL
  AND to_regprocedure('memory.read_governed_claims_v1(uuid[])') IS NOT NULL
)::int")" == 1 ]]

echo 'memory_v1_governed_claim_reader_v2_production_clone: PASS'

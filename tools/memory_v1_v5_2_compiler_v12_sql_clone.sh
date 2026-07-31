#!/usr/bin/env bash
set -Eeuo pipefail

wt=/home/ubuntu/chat-memory-v5-2-provenance-coverage-conflict-v1
container=brains-postgres-1
clone="memory_v12_sql_${$}"
dump="/tmp/${clone}.dump"
memory_acl_list="/tmp/${clone}.memory-acl.list"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
manifest_sha=13682f6959b491f1ab8c0e90cd54843c6164cc9b202b0683ccda53c430fe2052
compiler_sha=91e3830e676b6c9abd881a52f3d6b010679809c86fffbc889134d9653deba5f3
phase=preflight

cleanup() {
  rc=$?
  trap - EXIT
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$dump" "$memory_acl_list"
  if [[ $rc -ne 0 ]]; then
    printf 'V12_SQL_CLONE=FAIL phase=%s rc=%s\n' "$phase" "$rc" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT

phase=clone
docker exec "$container" pg_dump -U sage -d memory -Fc >"$dump"
pg_restore -l "$dump" \
  | grep -E ' ACL memory | ACL - SCHEMA memory ' >"$memory_acl_list"
test -s "$memory_acl_list"
! grep -q 'lifeswitch_chat' "$memory_acl_list"
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error --no-acl <"$dump"
pg_restore --exit-on-error --use-list="$memory_acl_list" -f - "$dump" \
  | docker exec -i "$container" psql -U sage -d "$clone" -X \
      -v ON_ERROR_STOP=1 >/dev/null

apply_file() {
  docker exec -i "$container" psql -U sage -d "$clone" -X \
    -v ON_ERROR_STOP=1 <"$wt/$1" >/dev/null
}

phase=apply
apply_file ops/sql/20260731_memory_v1_v5_2_provenance_coverage_conflict_v1.sql
apply_file ops/sql/20260731_memory_v1_v5_2_compiler_v12_persistence_compat.sql
apply_file ops/sql/20260731_memory_v1_v5_2_compiler_v12_exact_two.sql

phase=rollback
apply_file ops/sql/20260731_memory_v1_v5_2_compiler_v12_exact_two_rollback.sql
apply_file ops/sql/20260731_memory_v1_v5_2_compiler_v12_persistence_compat_rollback.sql
apply_file ops/sql/20260731_memory_v1_v5_2_provenance_coverage_conflict_v1_rollback.sql

phase=reapply
apply_file ops/sql/20260731_memory_v1_v5_2_provenance_coverage_conflict_v1.sql
apply_file ops/sql/20260731_memory_v1_v5_2_compiler_v12_persistence_compat.sql
apply_file ops/sql/20260731_memory_v1_v5_2_compiler_v12_exact_two.sql

manifest=$(jq -c . "$wt/manifests/memory_v1_v5_2_compiler_v12_exact_two.json")

run_enqueue() {
  docker exec -i "$container" psql -U sage -d "$clone" -X -At -F '|' \
    -v ON_ERROR_STOP=1 -v owner="$owner" -v manifest="$manifest" \
    -v manifest_sha="$manifest_sha" -v compiler_sha="$compiler_sha" <<'SQL'
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', false);
SELECT evidence_id,job_id,apply_outcome
FROM memory.enqueue_owner_v5_2_compiler_v12_exact_two_v1(
  :'manifest'::jsonb, :'manifest_sha', :'compiler_sha'
)
ORDER BY evidence_id;
SQL
}

phase=enqueue
first=$(run_enqueue | tail -n 2)
second=$(run_enqueue | tail -n 2)
test "$(printf '%s\n' "$first" | grep -c 'applied$')" -eq 2
test "$(printf '%s\n' "$second" | grep -c 'replayed$')" -eq 2
test "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_extraction_job
   WHERE owner_user_id='$owner'::uuid
     AND selector_version='20260731_v5_2_compiler_v12_exact_two_v1'")" -eq 2

phase=cross_owner
set +e
docker exec -i "$container" psql -U sage -d "$clone" -X -At \
  -v ON_ERROR_STOP=1 -v owner="$other" -v manifest="$manifest" \
  -v manifest_sha="$manifest_sha" -v compiler_sha="$compiler_sha" \
  >/dev/null 2>&1 <<'SQL'
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', false);
SELECT count(*)
FROM memory.enqueue_owner_v5_2_compiler_v12_exact_two_v1(
  :'manifest'::jsonb, :'manifest_sha', :'compiler_sha'
);
SQL
cross_rc=$?
set -e
test "$cross_rc" -ne 0

phase=complete
printf 'V12_SQL_CLONE=PASS applied=2 replayed=2 cross_owner_rejected=true\n'

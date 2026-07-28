#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies Workbench diagnostics v2 to a disposable
# production clone, proves owner isolation and replay, then removes the clone.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-/opt/chat-memory/.env}"
set +a

container=brains-postgres-1
production=memory
clone="memory_workbench_diagnostics_v2_${$}"
migration=ops/sql/20260728_memory_v1_owner_workbench_diagnostics_v2.sql
rollback=ops/sql/20260728_memory_v1_owner_workbench_diagnostics_v2_rollback.sql
security_test=tests/memory_v1_owner_workbench_diagnostics_v2_security.sql
backup=$(mktemp /tmp/memory-workbench-diagnostics-v2.XXXXXX.dump)
chmod 0600 "$backup"

cleanup() {
  rc=$?
  trap - EXIT
  sudo docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  exit "$rc"
}
trap cleanup EXIT

sudo docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
test -s "$backup"
sudo docker exec "$container" createdb -U sage -T template0 "$clone"
sudo docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

protected_before=$(sudo docker exec "$container" psql -U sage -d "$clone" \
  -X -Atqc "
    SELECT jsonb_build_object(
      'claims',(SELECT count(*) FROM memory.claim),
      'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5_local),
      'stage',(SELECT count(*) FROM memory.relational_stage_batch),
      'proposals',(SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
      'feedback',(SELECT count(*) FROM memory.owner_packet_feedback_v1)
    )::text
  ")

sudo docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
sudo docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

protected_after_migration=$(
  sudo docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
    SELECT jsonb_build_object(
      'claims',(SELECT count(*) FROM memory.claim),
      'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5_local),
      'stage',(SELECT count(*) FROM memory.relational_stage_batch),
      'proposals',(SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
      'feedback',(SELECT count(*) FROM memory.owner_packet_feedback_v1)
    )::text
  "
)
test "$protected_after_migration" = "$protected_before"

clone_dsn=$(
  /opt/chat-memory/venv/bin/python -c \
    'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
    "$POSTGRES_DSN" "$clone"
)
psql "$clone_dsn" -X -v ON_ERROR_STOP=1 <"$security_test" >/dev/null

/opt/chat-memory/venv/bin/python -m unittest \
  tests.test_admin_memory_workbench_v1 >/dev/null

protected_after_tests=$(
  sudo docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
    SELECT jsonb_build_object(
      'claims',(SELECT count(*) FROM memory.claim),
      'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5_local),
      'stage',(SELECT count(*) FROM memory.relational_stage_batch),
      'proposals',(SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
      'feedback',(SELECT count(*) FROM memory.owner_packet_feedback_v1)
    )::text
  "
)
test "$protected_after_tests" = "$protected_before"

sudo docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$rollback" >/dev/null
test "$(
  sudo docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
    "SELECT to_regprocedure(
      'memory.list_owner_memory_workbench_v2(text,integer,timestamptz,uuid)'
    ) IS NULL"
)" = t
test "$(
  sudo docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
    "SELECT to_regprocedure(
      'memory.list_owner_memory_workbench_v1(text,integer,timestamptz,uuid)'
    ) IS NOT NULL"
)" = t
test "$(
  sudo docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
    'SELECT count(*) FROM memory.owner_packet_feedback_v1'
)" = "$(
  printf '%s' "$protected_before" \
    | /opt/chat-memory/venv/bin/python -c \
      'import json,sys; print(json.load(sys.stdin)["feedback"])'
)"

printf '%s\n' 'memory_v1_owner_workbench_diagnostics_v2_clone: PASS'

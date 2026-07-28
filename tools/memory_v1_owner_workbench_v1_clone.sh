#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a disposable production clone and proves the
# owner-scoped Memory Workbench schema and feedback path.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-/opt/chat-memory/.env}"
set +a

container=brains-postgres-1
production=memory
clone="memory_owner_workbench_v1_${$}"
migration=ops/sql/20260728_memory_v1_owner_workbench_v1.sql
rollback=ops/sql/20260728_memory_v1_owner_workbench_v1_rollback.sql
security_test=tests/memory_v1_owner_workbench_v1_security.sql
guard_test=tests/memory_v1_owner_workbench_v1_guard.sql
backup=$(mktemp /tmp/memory-owner-workbench-v1.XXXXXX.dump)
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
      'proposals',(SELECT count(*) FROM memory.v5_2_atom_admission_proposal)
    )::text
  ")

sudo docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
sudo docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

fixture=$(sudo docker exec "$container" psql -U sage -d "$clone" -X -AtF '|' \
  -c "
    SELECT
      packet.owner_user_id,
      packet.packet_id,
      packet.packet_storage_sha256
    FROM memory.evidence_extraction_packet_v5_local AS packet
    JOIN memory.evidence_extraction_job AS job
      ON job.owner_user_id=packet.owner_user_id
     AND job.job_id=packet.job_id
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=packet.owner_user_id
     AND evidence.evidence_id=packet.evidence_id
    LEFT JOIN memory.v5_2_local_packet_route_event AS route
      ON route.owner_user_id=packet.owner_user_id
     AND route.packet_id=packet.packet_id
    WHERE job.status='review_required'
      AND evidence.status='active'
      AND (
        route.route='manual_review_artifact_ready'
        OR packet.manual_review_required
        OR packet.entity_mention_count>0
        OR packet.observation_count>0
        OR packet.comparison_hint_count>0
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.v5_local_packet_supersession AS supersession
        WHERE supersession.owner_user_id=packet.owner_user_id
          AND supersession.prior_packet_id=packet.packet_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.v5_local_packet_disposition AS disposition
        WHERE disposition.owner_user_id=packet.owner_user_id
          AND disposition.packet_id=packet.packet_id
      )
      AND NOT EXISTS (
        SELECT 1
        FROM memory.v5_local_terminal_reconciliation_v1 AS reconciliation
        WHERE reconciliation.owner_user_id=packet.owner_user_id
          AND reconciliation.packet_id=packet.packet_id
      )
    ORDER BY packet.created_at DESC
    LIMIT 1
  ")
IFS='|' read -r target_owner target_packet target_packet_sha256 <<<"$fixture"
test -n "$target_owner"
test -n "$target_packet"
test -n "$target_packet_sha256"

clone_dsn=$(/opt/chat-memory/venv/bin/python -c \
  'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
  "$POSTGRES_DSN" "$clone")
psql "$clone_dsn" -X -v ON_ERROR_STOP=1 \
  -v target_owner="$target_owner" \
  -v target_packet="$target_packet" \
  -v target_packet_sha256="$target_packet_sha256" \
  <"$security_test" >/dev/null
sudo docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 \
  -v target_owner="$target_owner" \
  -v target_packet="$target_packet" \
  -v target_packet_sha256="$target_packet_sha256" \
  <"$guard_test" >/dev/null

test "$(sudo docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  'SELECT count(*) FROM memory.owner_packet_feedback_v1')" = 0
protected_after=$(sudo docker exec "$container" psql -U sage -d "$clone" \
  -X -Atqc "
    SELECT jsonb_build_object(
      'claims',(SELECT count(*) FROM memory.claim),
      'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5_local),
      'stage',(SELECT count(*) FROM memory.relational_stage_batch),
      'proposals',(SELECT count(*) FROM memory.v5_2_atom_admission_proposal)
    )::text
  ")
test "$protected_after" = "$protected_before"

sudo docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$rollback" >/dev/null
test "$(sudo docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT to_regclass('memory.owner_packet_feedback_v1') IS NULL")" = t

printf '%s\n' 'memory_v1_owner_workbench_v1_clone: PASS'

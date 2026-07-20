#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a disposable production clone and proves the
# owner-scoped semantic-guard re-extraction path without production writes.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-$repo_root/.env}"
set +a
python_bin="${MEMORY_V1_PYTHON:-$repo_root/venv/bin/python}"

container=brains-postgres-1
production=memory
clone="memory_v5_semantic_reextract_v3_${$}"
owner=557ea042-cb82-48f8-9429-472e96c957ef
other=1240822d-ac9a-4096-95aa-e2b24d36ef50
packet=023d9a47-37b3-5897-8a88-d355ee06a0cb
content=2de0989137c018f67b3386726d5222c604dba2ffae4a302bc57f2e55fece4681
storage=6887818344cf9312ec523bb3b5ff74263c9742a8d6bfac8f9838a8623e8dfb59
plan_sha=f68db9cbcd15949587d7e851c4d9c006f23cb4a1cd4c59ef81d9d13157069da5
migration=ops/sql/20260720_memory_v1_v5_local_semantic_reextract_v3.sql
test_sql=tests/memory_v1_v5_local_semantic_reextract_v3.sql
worker=scripts/memory_v1_v5_local_semantic_reextract_v3.py
backup=$(mktemp /tmp/memory-v5-semantic-reextract-v3.XXXXXX.dump)
dry=$(mktemp /tmp/memory-v5-semantic-reextract-v3.XXXXXX.dry)
applied=$(mktemp /tmp/memory-v5-semantic-reextract-v3.XXXXXX.apply)
chmod 0600 "$backup" "$dry" "$applied"

cleanup() {
  rc=$?
  trap - EXIT
  docker exec "$container" dropdb -U sage --if-exists "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup" "$dry" "$applied"
  exit "$rc"
}
trap cleanup EXIT

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 -v target_owner="$owner" -v other_owner="$other" \
  -v prior_packet="$packet" -v content_sha256="$content" \
  -v packet_storage_sha256="$storage" <"$test_sql" >/dev/null

clone_dsn=$("$python_bin" -c \
  'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
  "$POSTGRES_DSN" "$clone")
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" "$python_bin" "$worker" \
  --owner-user-id "$owner" --prior-packet-id "$packet" \
  --expected-content-sha256 "$content" \
  --expected-packet-storage-sha256 "$storage" >"$dry"
jq -e --arg plan "$plan_sha" '
  .apply==false and .outcome=="eligible" and .plan_sha256==$plan and
  .write_counts=={"events":0,"jobs":0,"terminals":0} and
  .external_model_calls==0 and .claim_writes==0 and .qdrant_writes==0 and
  .prompt_influence==0
' "$dry" >/dev/null

MEMORY_V1_V5_LOCAL_SEMANTIC_REEXTRACT_APPLY=memory_v1_v5_local_semantic_reextract_apply_v3 \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" "$python_bin" "$worker" \
  --owner-user-id "$owner" --prior-packet-id "$packet" \
  --expected-content-sha256 "$content" \
  --expected-packet-storage-sha256 "$storage" \
  --expected-plan-sha256 "$plan_sha" --apply >"$applied"
jq -e '
  .apply==true and .outcome=="queued" and
  .write_counts=={"events":1,"jobs":1,"terminals":1} and
  .zero_write_replay_proved==true and .external_model_calls==0 and
  .claim_writes==0 and .qdrant_writes==0 and .prompt_influence==0
' "$applied" >/dev/null
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_extraction_job
   WHERE owner_user_id='$owner'::uuid
     AND selector_version='20260720_v5_semantic_guard_reextract_v1'
     AND status='pending' AND attempts=0")" == 1 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
   WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")" == 1 ]]

printf '%s\n' 'memory_v1_v5_local_semantic_reextract_v3_clone: PASS'

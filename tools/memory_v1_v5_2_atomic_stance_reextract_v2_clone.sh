#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a disposable production clone and proves the
# owner-scoped atomic-stance re-extraction path without production writes.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-$repo_root/.env}"
set +a
python_bin="${MEMORY_V1_PYTHON:-$repo_root/venv/bin/python}"

container=brains-postgres-1
production=memory
clone="memory_v5_v5_2_atomic_stance_reextract_v2_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=e21e39bb-20fe-5b95-bc72-77fd774f0faf
content=70a0cef5245b571bc8fceaa4c744cc273b488bbacac08acb2718f831a2b920a1
storage=36418a3aef75c4656fc00e556a1aa815107e095624419a67a0ff5d51c97228f8
plan_sha=3b6b80d29b37c9d83fe9715f6328d74e4ce71f1a3a879ffb2d4886ea2d3c5e22
migration=ops/sql/20260722_memory_v1_v5_2_atomic_stance_reextract_v2.sql
rollback=ops/sql/20260722_memory_v1_v5_2_atomic_stance_reextract_v2_rollback.sql
test_sql=tests/memory_v1_v5_2_atomic_stance_reextract_v2.sql
worker=scripts/memory_v1_v5_2_atomic_stance_reextract_v2.py
backup=$(mktemp /tmp/memory-v5-2-stance-schema-reextract.XXXXXX.dump)
dry=$(mktemp /tmp/memory-v5-2-stance-schema-reextract.XXXXXX.dry)
applied=$(mktemp /tmp/memory-v5-2-stance-schema-reextract.XXXXXX.apply)
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

MEMORY_V1_V5_2_ATOMIC_STANCE_REEXTRACT_APPLY=memory_v1_v5_2_atomic_stance_reextract_apply_v2 \
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
     AND selector_version='20260722_v5_2_atomic_stance_reextract_v2'
     AND status='pending' AND attempts=0")" == 1 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
   WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")" == 1 ]]

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$rollback" >/dev/null
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT to_regprocedure(
    'memory.plan_owner_v5_2_atomic_stance_reextract_v2(uuid)') IS NULL
    AND to_regprocedure(
    'memory.enqueue_owner_v5_2_atomic_stance_reextract_v2(uuid,uuid,uuid,uuid,text,text,text,text)') IS NULL")" == t ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')")" == \
  78ee8c17d613e9db9d3c040c2e73ed28f50f0b9898cecca7ea5d892c91a95830 ]]

printf '%s\n' 'memory_v1_v5_2_atomic_stance_reextract_v2_clone: PASS'

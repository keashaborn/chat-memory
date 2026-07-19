#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a disposable production clone and proves one
# owner-scoped append-only packet supersession without production writes.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source .env
set +a

container=brains-postgres-1
production=memory
clone="memory_v5_packet_supersession_${$}"
owner=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
other=557ea042-cb82-48f8-9429-472e96c957ef
prior=5455ceae-ae9d-5832-a491-18f4fee41284
replacement=72b091a6-22be-549e-9c63-a1a4360f3eac
prior_storage=e1725c0c94fbaeff146fe6959b6084729495c59eb7c79d1c5feed9a984a6fa1a
replacement_storage=7d57a27543cbc93335cdc31ed3e7da3cdfc828adbcf1c427acbdbbf55758dfc5
plan_sha=42749cf4f464ff71525710a3c068ddbea4698bc79b41786d5d24fdba70db7bff
migration=ops/sql/20260719_memory_v1_v5_local_packet_supersession.sql
rollback=ops/sql/20260719_memory_v1_v5_local_packet_supersession_rollback.sql
test_sql=tests/memory_v1_v5_local_packet_supersession.sql
worker=scripts/memory_v1_v5_local_packet_supersession.py
backup=$(mktemp /tmp/memory-v5-packet-supersession.XXXXXX.dump)
dry=$(mktemp /tmp/memory-v5-packet-supersession.XXXXXX.dry)
applied=$(mktemp /tmp/memory-v5-packet-supersession.XXXXXX.apply)
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
clone_dsn=$(venv/bin/python -c \
  'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
  "$POSTGRES_DSN" "$clone")
psql "$clone_dsn" -X -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" -v other_owner="$other" \
  -v prior_packet="$prior" -v replacement_packet="$replacement" \
  -v prior_storage_sha256="$prior_storage" \
  -v replacement_storage_sha256="$replacement_storage" \
  -v operation_id=00000000-0000-4000-8000-000000000101 \
  -v supersession_id=00000000-0000-4000-8000-000000000102 \
  <"$test_sql" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$rollback" >/dev/null
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT to_regclass('memory.v5_local_packet_supersession') IS NULL
      AND to_regprocedure(
        'memory.plan_owner_v5_local_packet_supersession_v1(uuid,uuid)'
      ) IS NULL
      AND to_regprocedure(
        'memory.plan_owner_v5_local_packet_disposition_v1(integer)'
      ) IS NOT NULL")" == t ]]
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner" --prior-packet-id "$prior" \
  --replacement-packet-id "$replacement" \
  --expected-prior-storage-sha256 "$prior_storage" \
  --expected-replacement-storage-sha256 "$replacement_storage" >"$dry"
jq -e --arg plan "$plan_sha" '
  .apply==false and .outcome=="eligible" and .plan_sha256==$plan and
  .prior_observation_count==4 and .replacement_observation_count==4 and
  .write_counts=={"supersessions":0} and .external_model_calls==0 and
  .claim_writes==0 and .qdrant_writes==0 and .prompt_influence==0
' "$dry" >/dev/null

MEMORY_V1_V5_LOCAL_PACKET_SUPERSESSION_APPLY=memory_v1_v5_local_packet_supersession_apply_v1 \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner" --prior-packet-id "$prior" \
  --replacement-packet-id "$replacement" \
  --expected-prior-storage-sha256 "$prior_storage" \
  --expected-replacement-storage-sha256 "$replacement_storage" \
  --expected-plan-sha256 "$plan_sha" --apply >"$applied"
jq -e '
  .apply==true and .outcome=="superseded" and
  .write_counts=={"supersessions":1} and
  .zero_write_replay_proved==true and .external_model_calls==0 and
  .claim_writes==0 and .qdrant_writes==0 and .prompt_influence==0
' "$applied" >/dev/null
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT count(*) FROM memory.v5_local_packet_supersession
   WHERE owner_user_id='$owner'::uuid AND prior_packet_id='$prior'::uuid
     AND replacement_packet_id='$replacement'::uuid")" == 1 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
   WHERE owner_user_id='$owner'::uuid
     AND packet_id IN ('$prior'::uuid,'$replacement'::uuid)")" == 2 ]]

printf '%s\n' 'memory_v1_v5_local_packet_supersession_clone: PASS'

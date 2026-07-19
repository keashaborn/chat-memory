#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a disposable production clone and proves the
# owner-scoped temporal-contract re-extraction path without production writes.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source .env
set +a

container=brains-postgres-1
production=memory
clone="memory_v5_packet_reextract_v2_${$}"
owner=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=5455ceae-ae9d-5832-a491-18f4fee41284
content=48760fb157c7a4b3776fabe26367fbe5fe8838e9eca079bad26b9f3d60862989
storage=e1725c0c94fbaeff146fe6959b6084729495c59eb7c79d1c5feed9a984a6fa1a
plan_sha=71e86682dd5b3247ffaf993aa64c6567f5902cf2830fb09292eca7586516bd3f
migration=ops/sql/20260719_memory_v1_v5_local_packet_reextract_v2.sql
test_sql=tests/memory_v1_v5_local_packet_reextract_v2.sql
worker=scripts/memory_v1_v5_local_packet_reextract_v2.py
backup=$(mktemp /tmp/memory-v5-packet-reextract-v2.XXXXXX.dump)
dry=$(mktemp /tmp/memory-v5-packet-reextract-v2.XXXXXX.dry)
applied=$(mktemp /tmp/memory-v5-packet-reextract-v2.XXXXXX.apply)
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

clone_dsn=$(venv/bin/python -c \
  'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
  "$POSTGRES_DSN" "$clone")
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner" --prior-packet-id "$packet" \
  --expected-content-sha256 "$content" \
  --expected-packet-storage-sha256 "$storage" >"$dry"
jq -e --arg plan "$plan_sha" '
  .apply==false and .outcome=="eligible" and .plan_sha256==$plan and
  .write_counts=={"events":0,"jobs":0,"terminals":0} and
  .external_model_calls==0 and .claim_writes==0 and .qdrant_writes==0 and
  .prompt_influence==0
' "$dry" >/dev/null

MEMORY_V1_V5_LOCAL_PACKET_REEXTRACT_APPLY=memory_v1_v5_local_packet_reextract_apply_v2 \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
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
     AND selector_version='20260719_v5_temporal_contract_reextract_v1'
     AND status='pending' AND attempts=0")" == 1 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
   WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")" == 1 ]]

printf '%s\n' 'memory_v1_v5_local_packet_reextract_v2_clone: PASS'

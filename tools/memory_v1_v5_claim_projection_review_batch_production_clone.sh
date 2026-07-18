#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Exercises the four-review apply and replay against a
# disposable production-schema clone. It never connects to Qdrant.

if [[ "$#" -ne 3 ]]; then
  echo 'usage: review_batch_production_clone.sh MANIFEST APPLY_RESULT REPLAY_RESULT' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
manifest=$(realpath "$1")
apply_result=$(realpath -m "$2")
replay_result=$(realpath -m "$3")
container=brains-postgres-1
source_db=memory
clone_db="memory_claim_projection_review_$(date -u +%Y%m%d%H%M%S)_$$"
runner=scripts/memory_v1_v5_claim_projection_review_batch.py

set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
[[ "$(jq -er '.required_head_commit' "$manifest")" == "$head" ]]
for output in "$apply_result" "$replay_result"; do
  [[ "$output" == /home/ubuntu/memory-v1-reviews/* && ! -e "$output" ]]
done

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc --no-owner \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db" --no-owner

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit
value=urlsplit(os.environ['SOURCE_DSN'])
print(urlunsplit((value.scheme,value.netloc,'/'+os.environ['CLONE_DB'],value.query,value.fragment)))
PY
)
before_review=$(docker exec "$container" psql -X -A -t -U sage -d "$clone_db" -c 'SELECT count(*) FROM memory.projection_review')
before_apply=$(docker exec "$container" psql -X -A -t -U sage -d "$clone_db" -c 'SELECT count(*) FROM memory.projection_apply_event')
before_claim=$(docker exec "$container" psql -X -A -t -U sage -d "$clone_db" -c 'SELECT count(*) FROM memory.claim')

POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_CLAIM_PROJECTION_REVIEW_BATCH_APPLY=authorized \
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python "$repo_root/$runner" \
  --mode apply --manifest "$manifest" --output "$apply_result"
[[ "$(jq -er '.rows_written' "$apply_result")" == 4 ]]

POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
PYTHONPATH="$repo_root/scripts" /opt/chat-memory/venv/bin/python "$repo_root/$runner" \
  --mode replay --manifest "$manifest" --output "$replay_result"
[[ "$(jq -er '.rows_written' "$replay_result")" == 0 ]]

after_review=$(docker exec "$container" psql -X -A -t -U sage -d "$clone_db" -c 'SELECT count(*) FROM memory.projection_review')
after_apply=$(docker exec "$container" psql -X -A -t -U sage -d "$clone_db" -c 'SELECT count(*) FROM memory.projection_apply_event')
after_claim=$(docker exec "$container" psql -X -A -t -U sage -d "$clone_db" -c 'SELECT count(*) FROM memory.claim')
[[ "$after_review" == "$((before_review+4))" ]]
[[ "$after_apply" == "$before_apply" ]]
[[ "$after_claim" == "$before_claim" ]]

POSTGRES_DSN="$clone_dsn" psql "$clone_dsn" -X -q -v ON_ERROR_STOP=1 <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_projection_review_v5(
      'ce967856-a164-526f-b6b9-25ae83272479'::uuid,'p01','authorized','system',
      'memory_v1_deterministic_claim_projection_v5_1_review_20260718',
      'direct owner-authored evidence with deterministic entity binding and predicate-specific claim rendering; reviewed for initial governed claim projection',
      '["active_owner_evidence","deterministic_entity_binding","predicate_specific_projection","phase_authorized_review"]'::jsonb
    );
    RAISE EXCEPTION 'cross-owner review unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN NULL;
  END;
END
$isolation$;
ROLLBACK;
SQL

printf 'reviews_created=4\nclaims_created=0\napply_events_created=0\n'
printf 'memory_v1_v5_claim_projection_review_batch_production_clone: PASS\n'

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies and replay-proofs the exact four reviewed
# compiler-v8 claims on a disposable production clone. No external model,
# production database, Qdrant, retrieval, or prompt write is made.

if [[ "$#" -ne 4 ]]; then
  echo 'usage: apply_batch_production_clone.sh MANIFEST PREFLIGHT APPLY REPLAY' >&2
  exit 2
fi
repo_root=$(git rev-parse --show-toplevel)
manifest=$(realpath "$1")
preflight=$(realpath -m "$2")
apply=$(realpath -m "$3")
replay=$(realpath -m "$4")
container=brains-postgres-1
source_db=memory
clone_db="memory_claim_projection_apply_$(date -u +%Y%m%d%H%M%S)_$$"
runner=scripts/memory_v1_v5_claim_projection_apply_batch.py
env_file=/opt/chat-memory/.env
expected_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
expected_observations="'a0ea633d-96df-4ad8-a0c1-b3f4f84e30cc','c8ce8cd0-e058-4181-ae94-fd6fb1e7c6eb','70d55f38-1e33-418f-8ec6-6bfd2051f4e6','bbd94cc7-e9d5-429f-8af1-1a029b119db0'"

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

set -a; source "$env_file"; set +a
[[ -n "${POSTGRES_DSN:-}" && -z "$(git -C "$repo_root" status --porcelain)" ]]
head=$(git -C "$repo_root" rev-parse HEAD)
[[ "$(jq -er '.required_head_commit' "$manifest")" == "$head" ]]
item_count=$(jq -er '.items|length' "$manifest")
expected_insert=$(jq -er '.expected_insert_rows' "$manifest")
expected_mutated=$(jq -er '.expected_mutated_rows' "$manifest")
target_owner=$(jq -er '.owner_user_id' "$manifest")
if [[ "$target_owner" == 557ea042-cb82-48f8-9429-472e96c957ef ]]; then
  other_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
else
  other_owner=557ea042-cb82-48f8-9429-472e96c957ef
fi
[[ "$item_count" == 4 ]]
[[ "$target_owner" == "$expected_owner" ]]
[[ "$(jq -er '.defer_projection_outbox' "$manifest")" == true ]]
[[ "$(jq -r '[.items[].predicate]|sort|join(",")' "$manifest")" == "occupation.works_as,occupation.works_as,relationship.caregiver_for,relationship.spouse_of" ]]
qdrant_before=$(qdrant_signature)
for output in "$preflight" "$apply" "$replay"; do
  [[ "$output" == /home/ubuntu/memory-v1-reviews/* && ! -e "$output" ]]
done
cleanup() { docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"
clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" python3 - <<'PY'
import os
from urllib.parse import urlsplit,urlunsplit
v=urlsplit(os.environ['SOURCE_DSN'])
print(urlunsplit((v.scheme,v.netloc,'/'+os.environ['CLONE_DB'],v.query,v.fragment)))
PY
)

before=$(mktemp /tmp/memory-v1-claim-apply-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-claim-apply-after.XXXXXX)
trap 'rm -f "$before" "$after"; cleanup' EXIT
docker exec "$container" psql -X -A -t -F $'\t' -U sage -d "$clone_db" -c \
  "SELECT table_name,(xpath('/row/count/text()',query_to_xml(format('SELECT count(*) AS count FROM memory.%I',table_name),false,true,'')))[1]::text FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" >"$before"

POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" PYTHONPATH="$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$runner" --mode preflight \
  --manifest "$manifest" --output "$preflight"
POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_CLAIM_PROJECTION_APPLY_BATCH=authorized PYTHONPATH="$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$runner" --mode apply \
  --manifest "$manifest" --output "$apply"
POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" PYTHONPATH="$repo_root/scripts" \
  /opt/chat-memory/venv/bin/python "$repo_root/$runner" --mode replay \
  --manifest "$manifest" --apply-result "$apply" --output "$replay"
[[ "$(jq -er '.insert_rows' "$preflight")" == 0 ]]
[[ "$(jq -er '.insert_rows' "$apply")" == "$expected_insert" ]]
[[ "$(jq -er '.mutated_rows' "$apply")" == "$expected_mutated" ]]
[[ "$(jq -er '.insert_rows' "$replay")" == 0 ]]
[[ "$(jq -er '.mutated_rows' "$replay")" == 0 ]]

docker exec "$container" psql -X -A -t -F $'\t' -U sage -d "$clone_db" -c \
  "SELECT table_name,(xpath('/row/count/text()',query_to_xml(format('SELECT count(*) AS count FROM memory.%I',table_name),false,true,'')))[1]::text FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" >"$after"
BEFORE="$before" AFTER="$after" MANIFEST="$manifest" python3 - <<'PY'
import json,os
from pathlib import Path
def load(path):
    out={}
    for line in Path(path).read_text().splitlines():
        table,count=line.split('\t')
        out[table]=int(count)
    return out
before,after=load(os.environ['BEFORE']),load(os.environ['AFTER'])
expected=json.loads(Path(os.environ['MANIFEST']).read_text())['expected_table_rows']
if before.keys()!=after.keys(): raise SystemExit('clone table set changed')
for table in before:
    delta=after[table]-before[table]
    wanted=expected.get(table,0)
    if delta!=wanted: raise SystemExit(f'unexpected clone delta {table}: {delta} != {wanted}')
PY

claim_ids=$(jq -r '[.outcomes[].claim_id]|join(",")' "$apply")
[[ "$(docker exec "$container" psql -X -A -t -U sage -d "$clone_db" -c "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner' AND status='supported' AND claim_id=ANY(string_to_array('$claim_ids',',')::uuid[])")" == "$item_count" ]]
[[ "$(docker exec "$container" psql -X -A -t -U sage -d "$clone_db" -c "SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id='$target_owner' AND aggregate_id=ANY(string_to_array('$claim_ids',',')::uuid[])")" == 0 ]]
[[ "$(docker exec "$container" psql -X -A -t -U sage -d "$clone_db" -c "SELECT count(*) FROM memory.claim_observation WHERE owner_user_id='$target_owner' AND observation_id IN ($expected_observations) AND claim_id=ANY(string_to_array('$claim_ids',',')::uuid[])")" == 4 ]]
[[ "$(docker exec "$container" psql -X -A -t -U sage -d "$clone_db" -c "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner' AND claim_id=ANY(string_to_array('$claim_ids',',')::uuid[]) AND canonical_text IN ('The user is a caregiver for Monika.','The user is a spouse of Monika.','The user formerly worked as BCBA.','The user formerly worked as clinical psychologist.')")" == 4 ]]

probe_plan=$(jq -er '.items[0].plan_id' "$manifest")
probe_review=$(jq -er '.items[0].review_id' "$manifest")
POSTGRES_DSN="$clone_dsn" psql "$clone_dsn" -X -q -v ON_ERROR_STOP=1 \
  -v probe_plan="$probe_plan" -v probe_review="$probe_review" \
  -v other_owner="$other_owner" <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id', :'other_owner', true);
SELECT set_config('test.probe_plan', :'probe_plan', true);
SELECT set_config('test.probe_review', :'probe_review', true);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_projection_apply_v5(
      current_setting('test.probe_plan')::uuid,'p01',
      current_setting('test.probe_review')::uuid
    );
    RAISE EXCEPTION 'cross-owner projection apply preflight unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN NULL;
  END;
END
$isolation$;
ROLLBACK;
SQL
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
printf 'insert_rows=%s\nmutated_rows=%s\nzero_write_replay=true\n' \
  "$expected_insert" "$expected_mutated"
printf 'qdrant_sha256=%s\nqdrant_unchanged=true\n' "$qdrant_before"
printf 'memory_v1_v5_2_compiler_v8_claim_materialization_clone: PASS\n'

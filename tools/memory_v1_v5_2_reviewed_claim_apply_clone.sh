#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Applies and replay-proofs a mixed create/reinforce claim
# batch on a disposable production clone. It makes no model or Qdrant calls and
# creates no projection_outbox rows.

if [[ "$#" -ne 4 ]]; then
  echo 'usage: reviewed_claim_apply_clone.sh MANIFEST PREFLIGHT APPLY REPLAY' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
manifest=$(realpath "$1")
preflight=$(realpath -m "$2")
apply=$(realpath -m "$3")
replay=$(realpath -m "$4")
container=brains-postgres-1
source_db=memory
clone_db="memory_reviewed_claim_apply_$(date -u +%Y%m%d%H%M%S)_$$"
runner=scripts/memory_v1_v5_2_reviewed_claim_apply_batch.py

set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
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
[[ "$item_count" -ge 1 && "$item_count" -le 32 ]]
for output in "$preflight" "$apply" "$replay"; do
  [[ "$output" == /home/ubuntu/memory-v1-reviews/* && ! -e "$output" ]]
done

before=$(mktemp /tmp/memory-reviewed-claim-apply-before.XXXXXX)
after=$(mktemp /tmp/memory-reviewed-claim-apply-after.XXXXXX)
cleanup() {
  rm -f "$before" "$after"
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"
clone_dsn=$(
  SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

value = urlsplit(os.environ["SOURCE_DSN"])
print(
    urlunsplit(
        (
            value.scheme,
            value.netloc,
            "/" + os.environ["CLONE_DB"],
            value.query,
            value.fragment,
        )
    )
)
PY
)

capture_counts() {
  docker exec "$container" psql -X -A -t -F $'\t' -U sage -d "$clone_db" -c \
    "SELECT table_name,(xpath('/row/count/text()',query_to_xml(format('SELECT count(*) AS count FROM memory.%I',table_name),false,true,'')))[1]::text FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name" \
    >"$1"
}

capture_counts "$before"
POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$runner" \
  --mode preflight --manifest "$manifest" --output "$preflight"
POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
MEMORY_V1_V5_2_REVIEWED_CLAIM_APPLY=authorized \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$runner" \
  --mode apply --manifest "$manifest" --output "$apply"
POSTGRES_DSN="$clone_dsn" MEMORY_V1_REQUIRED_HEAD="$head" \
PYTHONPATH="$repo_root/scripts:$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$runner" \
  --mode replay --manifest "$manifest" --apply-result "$apply" --output "$replay"

[[ "$(jq -er '.insert_rows' "$preflight")" == 0 ]]
[[ "$(jq -er '.mutated_rows' "$preflight")" == 0 ]]
[[ "$(jq -er '.insert_rows' "$apply")" == "$expected_insert" ]]
[[ "$(jq -er '.mutated_rows' "$apply")" == "$expected_mutated" ]]
[[ "$(jq -er '.insert_rows' "$replay")" == 0 ]]
[[ "$(jq -er '.mutated_rows' "$replay")" == 0 ]]
[[ "$(jq -er '.qdrant_writes' "$apply")" == 0 ]]
[[ "$(jq -er '.projection_outbox_rows_written' "$apply")" == 0 ]]

capture_counts "$after"
BEFORE="$before" AFTER="$after" MANIFEST="$manifest" python3 - <<'PY'
import json
import os
from pathlib import Path


def load(path: str) -> dict[str, int]:
    result = {}
    for line in Path(path).read_text().splitlines():
        table, count = line.split("\t")
        result[table] = int(count)
    return result


before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
expected = json.loads(Path(os.environ["MANIFEST"]).read_text())[
    "expected_table_rows"
]
if before.keys() != after.keys():
    raise SystemExit("clone table set changed")
for table in before:
    delta = after[table] - before[table]
    wanted = expected.get(table, 0)
    if delta != wanted:
        raise SystemExit(f"unexpected clone delta {table}: {delta} != {wanted}")
PY

claim_ids=$(jq -r '[.outcomes[].claim_id]|join(",")' "$apply")
[[ "$(docker exec "$container" psql -X -A -t -U sage -d "$clone_db" -c \
  "SELECT count(*) FROM memory.claim WHERE owner_user_id='$target_owner' AND status='supported' AND claim_id=ANY(string_to_array('$claim_ids',',')::uuid[])")" == "$item_count" ]]
[[ "$(docker exec "$container" psql -X -A -t -U sage -d "$clone_db" -c \
  "SELECT count(*) FROM memory.projection_outbox WHERE owner_user_id='$target_owner' AND aggregate_id=ANY(string_to_array('$claim_ids',',')::uuid[])")" == 0 ]]

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
      current_setting('test.probe_plan')::uuid,
      'p01',
      current_setting('test.probe_review')::uuid
    );
    RAISE EXCEPTION 'cross-owner projection apply preflight unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$isolation$;
ROLLBACK;
SQL

printf 'insert_rows=%s\n' "$expected_insert"
printf 'mutated_rows=%s\n' "$expected_mutated"
printf '%s\n' 'zero_write_replay=true'
printf '%s\n' 'projection_outbox_rows=0'
printf '%s\n' 'cross_owner_rejected=true'
printf '%s\n' 'memory_v1_v5_2_reviewed_claim_apply_clone: PASS'

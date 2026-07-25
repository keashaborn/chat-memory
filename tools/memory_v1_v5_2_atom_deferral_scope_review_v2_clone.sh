#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
export GIT_OPTIONAL_LOCKS=0
set -a
source "${MEMORY_V1_ENV_FILE:-/opt/chat-memory/.env}"
set +a

container=brains-postgres-1
production=memory
clone="memory_v5_2_atom_scope_v2_${$}"
python_bin=/opt/chat-memory/venv/bin/python
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
profession_packet=6ae4a8e6-b207-5201-a997-53fb2363fc9d
caregiving_packet=b76915b8-0603-50e8-b263-761da39f5651
migration=ops/sql/20260725_memory_v1_v5_2_atom_deferral_scope_review_v2.sql
rollback=ops/sql/20260725_memory_v1_v5_2_atom_deferral_scope_review_v2_rollback.sql
test_sql=tests/memory_v1_v5_2_atom_deferral_scope_review_v2.sql
backup=$(mktemp /tmp/memory-v5-2-atom-scope-v2.XXXXXX.dump)
review_root=$(mktemp -d /tmp/memory-v5-2-atom-scope-v2.XXXXXX)
manifest="$review_root/manifest.json"
preflight="$review_root/preflight.json"
applied="$review_root/applied.json"
replayed="$review_root/replayed.json"
chmod 0600 "$backup"
chmod 0700 "$review_root"

cleanup() {
  rc=$?
  trap - EXIT
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  find "$review_root" -type f -delete
  rmdir "$review_root"
  exit "$rc"
}
trap cleanup EXIT

database_signature() {
  local database=$1
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    WITH rows AS (
      SELECT 'proposal' AS lane,to_jsonb(value) AS value
      FROM memory.v5_2_atom_admission_proposal AS value
      UNION ALL
      SELECT 'review',to_jsonb(value)
      FROM memory.v5_2_atom_admission_review AS value
      UNION ALL
      SELECT 'apply',to_jsonb(value)
      FROM memory.v5_2_atom_admission_apply AS value
      UNION ALL
      SELECT 'operation',to_jsonb(value)
      FROM memory.v5_2_atom_admission_operation AS value
    )
    SELECT encode(public.digest(convert_to(coalesce(string_agg(
      lane||':'||value::text,E'\n' ORDER BY lane,value::text
    ),''),'UTF8'),'sha256'),'hex') FROM rows"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

function_hash() {
  local database=$1
  local function_name=$2
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "
    SELECT encode(public.digest(convert_to(
      pg_get_functiondef('$function_name'::regprocedure),'UTF8'
    ),'sha256'),'hex')"
}

before_database=$(database_signature "$production")
before_qdrant=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"
v1_planner_before=$(function_hash \
  "$clone" 'memory.plan_owner_v5_2_atom_admission_v1(uuid)')
v1_record_before=$(function_hash \
  "$clone" \
  'memory.record_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,text,text)')

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

[[ "$(function_hash \
  "$clone" 'memory.plan_owner_v5_2_atom_admission_v1(uuid)')" \
  == "$v1_planner_before" ]]
[[ "$(function_hash \
  "$clone" \
  'memory.record_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,text,text)')" \
  == "$v1_record_before" ]]

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v other_owner="$other" \
  -v profession_packet="$profession_packet" \
  -v caregiving_packet="$caregiving_packet" \
  <"$test_sql" >/dev/null

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$rollback" >/dev/null
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT to_regprocedure(
    'memory.plan_owner_v5_2_atom_admission_v2(uuid)'
  ) IS NULL")" == t ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT pg_get_constraintdef(oid)
  FROM pg_constraint
  WHERE conrelid='memory.v5_2_atom_admission_proposal'::regclass
    AND conname='v5_2_atom_admission_proposal_policy_version_check'")" \
  != *memory_v1_v5_2_atom_admission_policy_v2* ]]

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v other_owner="$other" \
  -v profession_packet="$profession_packet" \
  -v caregiving_packet="$caregiving_packet" \
  <"$test_sql" >/dev/null

"$python_bin" -m unittest \
  tests.test_memory_v1_v5_2_atom_admission_apply_v2
"$python_bin" -m unittest \
  tests.test_memory_v1_v5_2_atom_admission_manifest_v2

clone_dsn=$("$python_bin" -c \
  'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
  "$POSTGRES_DSN" "$clone")
atom_before=$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation)
  )")
downstream_before=$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.relational_stage_batch),
    (SELECT count(*) FROM memory.entity_mention),
    (SELECT count(*) FROM memory.observation),
    (SELECT count(*) FROM memory.claim)
  )")

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  "$python_bin" scripts/memory_v1_v5_2_atom_admission_manifest_v2.py \
  --output "$manifest" >/dev/null
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  "$python_bin" scripts/memory_v1_v5_2_atom_admission_apply_v2.py \
  --mode preflight --manifest "$manifest" --output "$preflight" >/dev/null
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY_V2=authorized \
  "$python_bin" scripts/memory_v1_v5_2_atom_admission_apply_v2.py \
  --mode apply --manifest "$manifest" --output "$applied" >/dev/null
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  "$python_bin" scripts/memory_v1_v5_2_atom_admission_apply_v2.py \
  --mode replay --manifest "$manifest" --output "$replayed" >/dev/null

MANIFEST="$manifest" PREFLIGHT="$preflight" APPLIED="$applied" \
REPLAYED="$replayed" "$python_bin" - <<'PY'
import json
import os
from pathlib import Path

manifest=json.loads(Path(os.environ["MANIFEST"]).read_text())
preflight=json.loads(Path(os.environ["PREFLIGHT"]).read_text())
applied=json.loads(Path(os.environ["APPLIED"]).read_text())
replayed=json.loads(Path(os.environ["REPLAYED"]).read_text())
assert len(manifest["items"]) == 2
assert preflight["persistent_writes"] == 0
assert applied["persistent_writes"] == 12
assert replayed["persistent_writes"] == 0
assert all(item["proposal_outcome"] == "applied" for item in applied["results"])
assert all(item["review_outcome"] == "applied" for item in applied["results"])
assert all(item["apply_outcome"] == "applied" for item in applied["results"])
assert all(item["proposal_outcome"] == "replayed" for item in replayed["results"])
assert all(item["review_outcome"] == "replayed" for item in replayed["results"])
assert all(item["apply_outcome"] == "replayed" for item in replayed["results"])
PY

atom_after=$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_atom_admission_proposal),
    (SELECT count(*) FROM memory.v5_2_atom_admission_review),
    (SELECT count(*) FROM memory.v5_2_atom_admission_apply),
    (SELECT count(*) FROM memory.v5_2_atom_admission_operation)
  )")
ATOM_BEFORE="$atom_before" ATOM_AFTER="$atom_after" "$python_bin" - <<'PY'
import os
before=[int(value) for value in os.environ["ATOM_BEFORE"].split(",")]
after=[int(value) for value in os.environ["ATOM_AFTER"].split(",")]
assert [right-left for left,right in zip(before,after)] == [2,2,2,6]
PY
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT count(*) FROM memory.v5_2_atom_admission_proposal
  WHERE owner_user_id='$owner'::uuid
    AND policy_version='memory_v1_v5_2_atom_admission_policy_v2'
    AND packet_id IN (
      '$profession_packet'::uuid,'$caregiving_packet'::uuid
    )
    AND admitted_observation_count=2")" == 2 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT count(*) FROM memory.v5_2_atom_admission_review AS review
  JOIN memory.v5_2_atom_admission_proposal AS proposal
    USING(owner_user_id,proposal_id)
  WHERE review.owner_user_id='$owner'::uuid
    AND proposal.policy_version='memory_v1_v5_2_atom_admission_policy_v2'
    AND review.decision='authorized'
    AND review.reviewer_type='user'
    AND review.reviewer_ref='$owner'")" == 2 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT count(*) FROM memory.v5_2_atom_admission_proposal
  WHERE owner_user_id='$owner'::uuid
    AND policy_version='memory_v1_v5_2_atom_admission_policy_v2'
    AND jsonb_array_length(proposal#>'{stage_projection,deferrals}')=0
    AND jsonb_array_length(proposal#>'{stage_projection,observations}')=2")" \
  == 2 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.relational_stage_batch),
    (SELECT count(*) FROM memory.entity_mention),
    (SELECT count(*) FROM memory.observation),
    (SELECT count(*) FROM memory.claim)
  )")" == "$downstream_before" ]]

[[ "$(database_signature "$production")" == "$before_database" ]]
[[ "$(qdrant_signature)" == "$before_qdrant" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

printf '%s\n' \
  'memory_v1_v5_2_atom_deferral_scope_review_v2_clone: PASS'

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Atom-admits one reviewed self identity-name observation
# in a disposable production clone. Production remains read-only.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
production=memory
clone="memory_v5_2_self_name_atom_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=9dd7426d-77eb-4765-9db2-13e33ad7444d
packet=1f7fe393-afc3-5982-b69c-c90877659ef6
spec=manifests/memory_v1_v5_2_self_identity_name_atom_admission_spec_20260730.json
manifest_runner=scripts/memory_v1_v5_2_atom_admission_manifest_batch.py
apply_runner=scripts/memory_v1_v5_2_atom_admission_apply_v2.py
backup=$(mktemp /tmp/memory-v5-2-self-name-atom.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-v5-2-self-name-atom.XXXXXX)
chmod 0600 "$backup"
chmod 0700 "$work"
chown ubuntu:ubuntu "$work"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  rm -rf "$work"
}
trap cleanup EXIT

scalar() {
  local database=$1
  local query=$2
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$query" | tr -d '[:space:]'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for file in "$spec" "$manifest_runner" "$apply_runner"; do
  [[ -f "$file" ]]
done
bash -n "$0"
git diff --check
[[ -z "$(git status --short)" ]]

production_atom_before=$(scalar "$production" "
  SELECT count(*)
  FROM memory.v5_2_atom_admission_apply
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
")
production_stage_before=$(scalar "$production" \
  'SELECT count(*) FROM memory.relational_stage_batch')
production_claims_before=$(scalar "$production" \
  'SELECT count(*) FROM memory.claim')
qdrant_before=$(qdrant_signature)
[[ "$production_atom_before" == 0 ]]

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" <"$backup"

set -a
source /opt/chat-memory/.env
set +a
clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone" python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

value = urlsplit(os.environ["SOURCE_DSN"])
print(urlunsplit((
    value.scheme,
    value.netloc,
    "/" + os.environ["CLONE_DB"],
    value.query,
    value.fragment,
)))
PY
)

runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$manifest_runner" \
  --spec "$repo_root/$spec" \
  --output "$work/manifest.json"

jq -e '
  .contract_version=="memory_v1_v5_2_atom_admission_apply_manifest_v2" and
  (.items | length)==1 and
  .items[0].case_id=="self_identity_name_eric" and
  .items[0].packet_id=="1f7fe393-afc3-5982-b69c-c90877659ef6" and
  .items[0].review_decision=="authorized"
' "$work/manifest.json" >/dev/null

runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$apply_runner" \
  --mode preflight \
  --manifest "$work/manifest.json" \
  --output "$work/preflight.json"
jq -e '.persistent_writes==0' "$work/preflight.json" >/dev/null

runuser -u ubuntu -- env MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY_V2=authorized \
  POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$repo_root/$apply_runner" \
  --mode apply \
  --manifest "$work/manifest.json" \
  --output "$work/apply.json"
jq -e '
  .persistent_writes==6 and
  (.results | length)==1 and
  .results[0].proposal_outcome=="applied" and
  .results[0].review_outcome=="applied" and
  .results[0].apply_outcome=="applied" and
  (.same_transaction_replay | length)==1 and
  .same_transaction_replay[0].proposal_outcome=="replayed" and
  .same_transaction_replay[0].review_outcome=="replayed" and
  .same_transaction_replay[0].apply_outcome=="replayed"
' "$work/apply.json" >/dev/null

runuser -u ubuntu -- env POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$apply_runner" \
  --mode replay \
  --manifest "$work/manifest.json" \
  --output "$work/replay.json"
jq -e '
  .persistent_writes==0 and
  (.results | length)==1 and
  .results[0].proposal_outcome=="replayed" and
  .results[0].review_outcome=="replayed" and
  .results[0].apply_outcome=="replayed" and
  (.same_transaction_replay | length)==0
' \
  "$work/replay.json" >/dev/null

[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.v5_2_atom_admission_proposal
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
")" == 1 ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.v5_2_atom_admission_review AS review
  JOIN memory.v5_2_atom_admission_proposal AS proposal
    USING(owner_user_id,proposal_id)
  WHERE proposal.owner_user_id='$owner'::uuid
    AND proposal.packet_id='$packet'::uuid
")" == 1 ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.v5_2_atom_admission_apply
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
")" == 1 ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.v5_2_atom_admission_apply
  WHERE owner_user_id='$other'::uuid AND packet_id='$packet'::uuid
")" == 0 ]]
[[ "$(scalar "$clone" \
  'SELECT count(*) FROM memory.relational_stage_batch')" \
  == "$production_stage_before" ]]
[[ "$(scalar "$clone" 'SELECT count(*) FROM memory.claim')" \
  == "$production_claims_before" ]]
[[ "$(scalar "$production" "
  SELECT count(*)
  FROM memory.v5_2_atom_admission_apply
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
")" == "$production_atom_before" ]]
[[ "$(scalar "$production" \
  'SELECT count(*) FROM memory.relational_stage_batch')" \
  == "$production_stage_before" ]]
[[ "$(scalar "$production" 'SELECT count(*) FROM memory.claim')" \
  == "$production_claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf 'ATOM_PROPOSALS=1\n'
printf 'ATOM_REVIEWS=1\n'
printf 'ATOM_APPLIES=1\n'
printf 'ATOM_OPERATIONS=3\n'
printf 'STAGE_WRITES=0\n'
printf 'CLAIM_WRITES=0\n'
printf 'QDRANT_WRITES=0\n'
printf 'CROSS_OWNER_VISIBLE=0\n'
printf 'ZERO_WRITE_REPLAY=proved\n'
printf 'memory_v1_v5_2_self_identity_name_atom_clone: PASS\n'

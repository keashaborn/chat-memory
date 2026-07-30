#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Atom-admits exactly one reviewed owner-self identity.name
# packet. No relational staging, claims, Qdrant, retrieval, or prompt influence.

if [[ "${MEMORY_V1_V5_2_SELF_NAME_ATOM_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_SELF_NAME_ATOM_APPLY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
env_file=/opt/chat-memory/.env
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_self_name_atom.lock
required_production=1aab54dfffebb7f1805a31997b6f9b6c5316b3ef
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=9dd7426d-77eb-4765-9db2-13e33ad7444d
packet=1f7fe393-afc3-5982-b69c-c90877659ef6
spec=manifests/memory_v1_v5_2_self_identity_name_atom_admission_spec_20260730.json
manifest_runner=scripts/memory_v1_v5_2_atom_admission_manifest_batch.py
apply_runner=scripts/memory_v1_v5_2_atom_admission_apply_v2.py
expected_spec_sha=64c9cb3b88aab81d527263c16afb75829b4aee5a6e40aac473874a2903197142
expected_manifest_runner_sha=ab16b4ac196d12c6b3928a753e8d85385ec974e6fe396bb764d1c49a00c7a74d
expected_apply_runner_sha=721184f8eebf5f4d14b553eb0cf0134960456d8834029263da1d10963868c5e7

phase=initialization
run_tag=
status_file=
timer_state=
table_list=
timers_quiesced=0
durable_rows_present=0

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

authenticated_health() {
  set -a
  source "$env_file"
  set +a
  [[ -n "${VS_SERVICE_TOKEN:-}" ]]
  [[ "$(systemctl is-active brains.service)" == active ]]
  docker exec "$container" pg_isready -U sage -d "$database" >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 && -s "$timer_state" ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      [[ "$active" == inactive ]]
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  exit_code=$?
  set +e
  if [[ "$timers_quiesced" -eq 1 ]]; then
    failed_phase=$phase
    phase=restore_timers_after_failure
    restore_timers || exit_code=1
    phase=$failed_phase
  fi
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'head=%s\n' "$(git rev-parse HEAD)"
      printf 'durable_rows_present=%s\n' "$durable_rows_present"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

capture_partition() {
  local partition=$1 output=$2 table has_owner predicate state
  : >"$output"
  while IFS=$'\t' read -r table has_owner; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    if [[ "$has_owner" == t ]]; then
      if [[ "$partition" == target ]]; then
        predicate="owner_user_id='$owner'::uuid"
      else
        predicate="owner_user_id<>'$owner'::uuid"
      fi
    elif [[ "$partition" == target ]]; then
      continue
    else
      predicate=true
    fi
    state=$(row "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
        WHERE $predicate
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

verify_target_delta() {
  BEFORE="$1" AFTER="$2" python3 - <<'PY'
import os
from pathlib import Path

def load(path):
    rows = {}
    for line in Path(path).read_text().splitlines():
        table, count, digest = line.split("\t")
        rows[table] = (int(count), digest)
    return rows

before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
if before.keys() != after.keys():
    raise SystemExit("target-owner table set changed")
expected = {
    "v5_2_atom_admission_proposal": 1,
    "v5_2_atom_admission_review": 1,
    "v5_2_atom_admission_apply": 1,
    "v5_2_atom_admission_operation": 3,
}
for table in before:
    delta = after[table][0] - before[table][0]
    wanted = expected.get(table, 0)
    if delta != wanted:
        raise SystemExit(f"unexpected target-owner delta {table}: {delta} != {wanted}")
    if wanted == 0 and before[table][1] != after[table][1]:
        raise SystemExit(f"unexpected target-owner mutation {table}")
PY
}

phase=source_preflight
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_production" HEAD
for pair in \
  "$spec:$expected_spec_sha" \
  "$manifest_runner:$expected_manifest_runner_sha" \
  "$apply_runner:$expected_apply_runner_sha"; do
  file=${pair%%:*}
  expected=${pair##*:}
  [[ -f "$file" ]]
  [[ "$(sha256sum "$file" | awk '{print $1}')" == "$expected" ]]
done
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_2_atom_admission_apply AS applied
  JOIN memory.v5_2_atom_admission_review AS review
    ON review.owner_user_id=applied.owner_user_id
   AND review.review_id=applied.review_id
  JOIN memory.v5_2_atom_admission_proposal AS proposal
    ON proposal.owner_user_id=review.owner_user_id
   AND proposal.proposal_id=review.proposal_id
  WHERE proposal.owner_user_id='$owner'::uuid
    AND proposal.packet_id='$packet'::uuid
")" == 0 ]]
authenticated_health

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_self_name_atom_${run_tag}.status"
timer_state="$snapshot_dir/memory_v1_v5_2_self_name_atom_timers_${run_tag}.tsv"
table_list="$snapshot_dir/memory_v1_v5_2_self_name_atom_tables_${run_tag}.tsv"
target_before="$snapshot_dir/memory_v1_v5_2_self_name_atom_target_before_${run_tag}.tsv"
target_after="$snapshot_dir/memory_v1_v5_2_self_name_atom_target_after_${run_tag}.tsv"
non_target_before="$snapshot_dir/memory_v1_v5_2_self_name_atom_non_target_before_${run_tag}.tsv"
non_target_after="$snapshot_dir/memory_v1_v5_2_self_name_atom_non_target_after_${run_tag}.tsv"
review_dir="$review_root/self-name-atom-$run_tag"
install -d -m 0700 -o ubuntu -g ubuntu "$review_dir"
manifest="$review_dir/manifest.json"
preflight="$review_dir/preflight.json"
apply="$review_dir/apply.json"
replay="$review_dir/replay.json"

phase=capture_timer_state
mapfile -t timers < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
[[ ${#timers[@]} -gt 0 ]]
: >"$timer_state"
for unit in "${timers[@]}"; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done
chmod 0600 "$timer_state"

phase=quiesce_timers
for unit in "${timers[@]}"; do
  sudo -n systemctl stop "$unit"
done
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done <"$timer_state"

phase=fresh_backup
database_size=$(scalar 'SELECT pg_database_size(current_database())')
free_bytes=$(df --output=avail -B1 "$snapshot_dir" | tail -1 | tr -d '[:space:]')
(( free_bytes >= database_size * 2 ))
backup_partial="$snapshot_dir/.memory_pre_v5_2_self_name_atom_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_self_name_atom_${run_tag}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=capture_baseline
docker exec "$container" psql -X -A -t -F $'\t' -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    SELECT table_name, EXISTS (
      SELECT 1 FROM information_schema.columns AS column_info
      WHERE column_info.table_schema='memory'
        AND column_info.table_name=tables.table_name
        AND column_info.column_name='owner_user_id'
    )
    FROM information_schema.tables AS tables
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name" >"$table_list"
[[ -s "$table_list" ]]
chmod 0600 "$table_list"
capture_partition target "$target_before"
capture_partition non_target "$non_target_before"
qdrant_before=$(qdrant_signature)

phase=build_manifest
set -a
source "$env_file"
set +a
runuser -u ubuntu -- env POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$manifest_runner" --spec "$repo_root/$spec" --output "$manifest"
manifest_sha=$(jq -er '.manifest_sha256' "$manifest")
[[ "$(jq -er '.items | length' "$manifest")" == 1 ]]
[[ "$(jq -er '[.expected_new_rows[]] | add' "$manifest")" == 6 ]]
proposal_operation=$(jq -er '.items[0].proposal_operation_id' "$manifest")
review_operation=$(jq -er '.items[0].review_operation_id' "$manifest")
apply_operation=$(jq -er '.items[0].apply_operation_id' "$manifest")

phase=zero_write_preflight
runuser -u ubuntu -- env POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$apply_runner" --mode preflight \
  --manifest "$manifest" --output "$preflight"
jq -e '
  .persistent_writes==0 and (.results|length)==1 and
  .results[0].apply_outcome=="applied" and
  (.same_transaction_replay|length)==1 and
  .same_transaction_replay[0].apply_outcome=="replayed"
' "$preflight" >/dev/null

phase=durable_apply
runuser -u ubuntu -- env \
  MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY_V2=authorized \
  POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$repo_root/$apply_runner" \
  --mode apply --manifest "$manifest" --output "$apply"
jq -e '
  .persistent_writes==6 and (.results|length)==1 and
  .results[0].proposal_outcome=="applied" and
  .results[0].review_outcome=="applied" and
  .results[0].apply_outcome=="applied" and
  (.same_transaction_replay|length)==1 and
  .same_transaction_replay[0].apply_outcome=="replayed"
' "$apply" >/dev/null
durable_rows_present=1

phase=zero_write_replay
runuser -u ubuntu -- env POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$apply_runner" --mode replay \
  --manifest "$manifest" --output "$replay"
jq -e '
  .persistent_writes==0 and (.results|length)==1 and
  .results[0].apply_outcome=="replayed" and
  (.same_transaction_replay|length)==0
' "$replay" >/dev/null

phase=postflight
[[ "$(scalar "
  SELECT concat_ws(',',
    count(*) FILTER (WHERE operation='record_proposal'),
    count(*) FILTER (WHERE operation='record_review'),
    count(*) FILTER (WHERE operation='apply_review')
  )
  FROM memory.v5_2_atom_admission_operation
  WHERE owner_user_id='$owner'::uuid
    AND operation_id IN (
      '$proposal_operation'::uuid,
      '$review_operation'::uuid,
      '$apply_operation'::uuid
    )
")" == '1,1,1' ]]
capture_partition target "$target_after"
capture_partition non_target "$non_target_after"
verify_target_delta "$target_before" "$target_after"
cmp -s "$non_target_before" "$non_target_after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=owner_isolation
runuser -u ubuntu -- env POSTGRES_DSN="$POSTGRES_DSN" \
  OWNER="$owner" OTHER="$other" PACKET="$packet" \
  /opt/chat-memory/venv/bin/python - <<'PY'
import asyncio, os
import asyncpg

async def main():
    connection = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise SystemExit("owner-isolation probe requires brains_app")
        async with connection.transaction(readonly=True):
            await connection.execute(
                "SELECT set_config('app.user_id',$1,true)", os.environ["OTHER"]
            )
            visible = await connection.fetchval(
                """
                SELECT count(*)
                FROM memory.v5_2_atom_admission_proposal
                WHERE owner_user_id=$1::uuid AND packet_id=$2::uuid
                """,
                os.environ["OWNER"], os.environ["PACKET"],
            )
            if visible != 0:
                raise SystemExit("cross-owner atom proposal became visible")
    finally:
        await connection.close()

asyncio.run(main())
PY
authenticated_health

phase=restore_timers
restore_timers
authenticated_health

phase=write_report
report="$snapshot_dir/memory_v1_v5_2_self_name_atom_${run_tag}.json"
jq -n \
  --arg contract_version memory_v1_v5_2_self_identity_name_atom_apply_report_v1 \
  --arg run_tag "$run_tag" --arg head "$(git rev-parse HEAD)" \
  --arg manifest "$manifest" --arg manifest_sha256 "$manifest_sha" \
  --arg backup "$backup" \
  --arg backup_sha256 "$(sha256sum "$backup" | awk '{print $1}')" \
  --arg preflight "$preflight" --arg apply "$apply" --arg replay "$replay" \
  --arg qdrant_signature "$qdrant_before" \
  '{
    contract_version:$contract_version,run_tag:$run_tag,head:$head,
    manifest:{path:$manifest,sha256:$manifest_sha256},
    backup:{path:$backup,sha256:$backup_sha256},
    outputs:{preflight:$preflight,apply:$apply,replay:$replay},
    results:{
      proposals_created:1,reviews_created:1,applies_created:1,
      operation_rows_created:3,zero_write_replay:true,
      account_isolation:true,non_target_memory_unchanged:true,
      relational_staging_rows_created:0,claim_rows_created:0,
      qdrant_unchanged:true,timers_restored:true,
      external_model_calls:0,retrieval_or_prompt_influence:false
    },
    qdrant_signature:$qdrant_signature,
    hard_stop:"before_relational_staging_or_claims_or_projection_or_retrieval"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf '%s\n' \
  'memory_v1_v5_2_self_identity_name_atom_production_apply: PASS' \
  "head=$(git rev-parse HEAD)" \
  "manifest_sha256=$manifest_sha" \
  "backup=$backup" \
  "report=$report" \
  'rows=1,1,1,3' \
  'stage=0 claims=0 qdrant=0 prompt_influence=0'

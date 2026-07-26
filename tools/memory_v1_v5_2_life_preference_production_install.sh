#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Deploys the clone-proven deterministic preference guard
# and installs only its dormant exact-record re-extraction function.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_LIFE_PREFERENCE_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_LIFE_PREFERENCE_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
live=/opt/chat-memory
container=brains-postgres-1
database=memory
current_head=8453f25c4fdce470d70a16a26a3e707c5fc29d95
target_head=ee288d0f04a01b634f1a674c3c957f73a29d89a4
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
evidence=51b03105-4acc-5ded-b703-e139a892db9d
content_sha=524bda462b5ece2b507249a11ea68c02ae0ecd4d7148d21ccec4df7bbf675ec7
prior_packet=83598662-cb38-5e30-abe6-1ed8e57b0d87
prior_storage=ea1ece347f77e1eb013c6d98078cf2c61893e54027d9094997ceeca53c5bc6a1
operation=5ae6faaa-c1b9-5f05-827e-bd5ceaa5ba15
job=8de420a2-ab1f-5830-9f75-d1f4caa7747c
terminal=108a811e-2850-52f0-9071-71e251006f39
selector=20260725_v5_2_life_preference_reextract_v1
provider_sha=12531e9617f6d77017d6500ace01b4821599ebdd833e6a4d44a9eb8ceb5e3e73
manifest_sha=e2ca4a9e693909f5b22eb1327b1df31ee7733dd20b25255106c30be2fb08d422
migration=ops/sql/20260725_memory_v1_v5_2_life_preference_reextract.sql
test_sql=tests/memory_v1_v5_2_life_preference_reextract.sql
clone_test=tools/memory_v1_v5_2_life_preference_reextract_clone.sh
snapshot_dir=/home/ubuntu/brains/snapshots
unit_state=$(mktemp /tmp/memory-v1-life-preference-units.XXXXXX)
memory_before=$(mktemp /tmp/memory-v1-life-preference-before.XXXXXX)
memory_after=$(mktemp /tmp/memory-v1-life-preference-after.XXXXXX)
units_quiesced=0
run_tag=
report=

restore_timers() {
  [[ "$units_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
}

cleanup() {
  rc=$?
  trap - EXIT
  restore_timers || rc=1
  rm -f "$unit_state" "$memory_before" "$memory_after"
  exit "$rc"
}
trap cleanup EXIT

scalar() {
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "$1"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_memory_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(scalar "
      SELECT count(*)::text || E'\\t' ||
        encode(public.digest(convert_to(coalesce(string_agg(
          row_json,E'\\n' ORDER BY row_json
        ),''),'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(
    scalar "
      SELECT table_name
      FROM information_schema.tables
      WHERE table_schema='memory' AND table_type='BASE TABLE'
      ORDER BY table_name"
  )
}

[[ -z "$(git -C "$repo_root" status --short)" ]]
git -C "$repo_root" merge-base --is-ancestor \
  "$target_head" "$(git -C "$repo_root" rev-parse HEAD)"
live_head=$(git -C "$live" rev-parse HEAD)
[[ "$live_head" == "$current_head" || "$live_head" == "$target_head" ]]
[[ -z "$(git -C "$live" status --short)" ]]
if [[ "$live_head" == "$current_head" ]]; then
  git -C "$live" merge-base --is-ancestor "$current_head" "$target_head"
fi

declare -A expected=(
  [scripts/memory_v1_relational_extraction_v5_local_provider.py]=12531e9617f6d77017d6500ace01b4821599ebdd833e6a4d44a9eb8ceb5e3e73
  [tests/test_memory_v1_v5_2_explicit_life_preference_guard.py]=1cf7c74ad4341acff23669e3c0e4d7748f57eee9b7470da12f7359e6b81e2d52
  [ops/sql/20260725_memory_v1_v5_2_life_preference_reextract.sql]=b487fdb9021b975e2b685a1319c3e09c4486a56c173d9ebd46de96469046a73b
  [ops/sql/20260725_memory_v1_v5_2_life_preference_reextract_rollback.sql]=57d2fcc3a6ff5cff1be13ac6ad2326b307041b8222ba8f4b1c1012b9f9be13f4
  [tests/memory_v1_v5_2_life_preference_reextract.sql]=59103b66fe295e3a69d85207675ed723db50ec906b15ecf9ad49501c9df6cb92
  [manifests/memory_v1_v5_2_life_preference_reextract_20260725.json]=e2ca4a9e693909f5b22eb1327b1df31ee7733dd20b25255106c30be2fb08d422
  [tools/memory_v1_v5_2_life_preference_reextract_clone.sh]=05cf7082f0f6008cda68e57e45b1136de760244704f3c5265300491c299d1bcf
  [tools/memory_v1_v5_local_inference_canary_apply.sh]=11b335e59d95e74973e89970bfc2676581750548bdf81592ed5905585ef76f67
)
for path in "${!expected[@]}"; do
  [[ "$(sha256sum "$repo_root/$path" | awk '{print $1}')" \
    == "${expected[$path]}" ]]
done

"$repo_root/$clone_test" >/dev/null

: >"$unit_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' \
    "$unit" \
    "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
[[ -s "$unit_state" ]]
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$unit_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$unit_state"

run_tag="$(date -u +%Y%m%dT%H%M%SZ)_${target_head:0:12}"
backup="$snapshot_dir/memory_pre_v5_2_life_preference_${run_tag}.dump"
report="$snapshot_dir/memory_v1_v5_2_life_preference_install_${run_tag}.json"
docker exec "$container" pg_dump -Fc -U sage -d "$database" >"$backup"
chmod 0600 "$backup"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
[[ "$backup_sha" =~ ^[0-9a-f]{64}$ ]]

capture_memory_state "$memory_before"
qdrant_before=$(qdrant_signature)
[[ "$(scalar "
  SELECT count(*)
  FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" == 0 ]]

if [[ "$live_head" == "$current_head" ]]; then
  git -C "$live" merge --ff-only "$target_head" >/dev/null
fi
[[ "$(git -C "$live" rev-parse HEAD)" == "$target_head" ]]
[[ -z "$(git -C "$live" status --short)" ]]

cd "$live"
PYTHONPATH=. /opt/chat-memory/venv/bin/python \
  tests/test_memory_v1_v5_2_explicit_life_preference_guard.py >/dev/null
PYTHONPATH=. /opt/chat-memory/venv/bin/python -m unittest discover \
  -s tests -p 'test_memory_v1_v5_2_*guard.py' >/dev/null

docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$database" -X \
  -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v other_owner="$other" \
  -v evidence_id="$evidence" \
  -v content_sha256="$content_sha" \
  -v prior_packet_id="$prior_packet" \
  -v prior_packet_storage_sha256="$prior_storage" \
  -v operation_id="$operation" \
  -v job_id="$job" \
  -v terminal_id="$terminal" \
  -v manifest_sha256="$manifest_sha" \
  -v provider_source_sha256="$provider_sha" \
  <"$test_sql" >/dev/null

[[ "$(scalar "
  SELECT count(*)
  FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" == 0 ]]
capture_memory_state "$memory_after"
cmp -s "$memory_before" "$memory_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
curl -fsS http://127.0.0.1:6333/readyz >/dev/null

restore_timers
jq -n \
  --arg contract memory_v1_v5_2_life_preference_install_v1 \
  --arg head "$target_head" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:$contract,
    status:"PASS",
    production_head:$head,
    backup_path:$backup,
    backup_sha256:$backup_sha256,
    manifest_sha256:$manifest_sha256,
    persistent_selector_rows:0,
    memory_rows_changed:0,
    qdrant_sha256:$qdrant_sha256,
    prompt_influence:false
  }' >"$report"
chmod 0600 "$report"

printf 'backup=%s\nreport=%s\n' "$backup" "$report"

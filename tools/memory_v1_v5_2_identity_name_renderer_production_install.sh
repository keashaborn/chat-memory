#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the type-based V5.2 identity-name renderer.
# This changes one function and no memory, claim, projection, or Qdrant row.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_IDENTITY_NAME_RENDERER_PRODUCTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_IDENTITY_NAME_RENDERER_PRODUCTION=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source .env
set +a

required_base=31a926dc64bac018feb298126a90372ba3d70853
required_head=${MEMORY_V1_V5_2_IDENTITY_NAME_RENDERER_EXPECTED_HEAD:-}
container=brains-postgres-1
database=memory
snapshot_root=/home/ubuntu/brains/snapshots
migration=ops/sql/20260729_memory_v1_v5_2_identity_name_renderer.sql
rollback=ops/sql/20260729_memory_v1_v5_2_identity_name_renderer_rollback.sql
security_test=tests/memory_v1_v5_2_identity_name_renderer.sql
clone_test=tools/memory_v1_v5_2_identity_name_renderer_clone.sh
timer_state=$(mktemp /tmp/memory-v5-2-identity-renderer-timers.XXXXXX)
timers_quiesced=0
migration_applied=0
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status="$snapshot_root/memory_v1_v5_2_identity_name_renderer_${run_tag}.status"

declare -A expected_sha256=(
  ["$migration"]="cb76c474cd14dc58636c4b6341dd1b9d5aa63aae1172e5553ab1040aa26212a8"
  ["$rollback"]="9b51b0986c2d579f2f6751558e2c3fd67350db01c2b0c1266d413c6ab770a12e"
  ["$security_test"]="dcf664dcef77688e480ae6c2df71d3d6cfa97ae1eece7f8ed5954e79d5a2b5f0"
  ["$clone_test"]="a320ad9f419f958cdb3a80468c7f27acc22153e3b925faf8de67cc7ff2be0bd5"
)

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    case "$enabled" in
      enabled) systemctl enable "$unit" >/dev/null ;;
      disabled) systemctl disable "$unit" >/dev/null ;;
      *) return 1 ;;
    esac
    case "$active" in
      active) systemctl start "$unit" ;;
      inactive) systemctl stop "$unit" ;;
      *) return 1 ;;
    esac
    [[ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit" 2>/dev/null || true)" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

cleanup() {
  code=$?
  trap - EXIT
  if [[ "$code" -ne 0 && "$migration_applied" -eq 1 ]]; then
    docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
      -U sage -d "$database" <"$rollback" >/dev/null || code=1
  fi
  restore_timers || code=1
  rm -f "$timer_state"
  {
    printf 'exit_code=%s\n' "$code"
    printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$status"
  chmod 0600 "$status"
  exit "$code"
}
trap cleanup EXIT

data_signature() {
  docker exec "$container" pg_dump -U sage -d "$database" \
    --data-only --schema=memory --inserts --rows-per-insert=1 \
    --restrict-key=6d656d6f72797635326964656e7469747972656e64657265727631 \
    | sha256sum | awk '{print $1}'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

[[ -n "$required_head" ]]
[[ "$(git rev-parse HEAD)" == "$required_head" ]]
[[ "$(git merge-base "$required_base" HEAD)" == "$required_base" ]]
[[ -z "$(git status --porcelain)" ]]
for artifact in "${!expected_sha256[@]}"; do
  [[ "$(sha256sum "$artifact" | awk '{print $1}')" == \
    "${expected_sha256[$artifact]}" ]]
done
bash -n "$clone_test"
"$clone_test" >/dev/null

while IFS= read -r unit; do
  enabled=$(systemctl is-enabled "$unit" 2>/dev/null || true)
  active=$(systemctl is-active "$unit" 2>/dev/null || true)
  [[ "$enabled" == enabled || "$enabled" == disabled ]]
  [[ "$active" == active || "$active" == inactive ]]
  printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active" >>"$timer_state"
done < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
[[ -s "$timer_state" ]]

timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 60); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"

partial="$snapshot_root/.memory_pre_v5_2_identity_name_renderer_${run_tag}.dump.partial"
backup="$snapshot_root/memory_pre_v5_2_identity_name_renderer_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
mv "$partial" "$backup"
chmod 0600 "$backup"
backup_sha256=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha256" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

data_before=$(data_signature)
qdrant_before=$(qdrant_signature)
claim_before=$(docker exec "$container" psql -X -A -t -U sage -d "$database" \
  -c 'SELECT count(*) FROM memory.claim' | tr -d '[:space:]')
projection_before=$(docker exec "$container" psql -X -A -t -U sage -d "$database" \
  -c 'SELECT count(*) FROM memory.projection_apply_event' | tr -d '[:space:]')

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$migration" >/dev/null
migration_applied=1
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$security_test" >/dev/null

[[ "$(data_signature)" == "$data_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(docker exec "$container" psql -X -A -t -U sage -d "$database" \
  -c 'SELECT count(*) FROM memory.claim' | tr -d '[:space:]')" == "$claim_before" ]]
[[ "$(docker exec "$container" psql -X -A -t -U sage -d "$database" \
  -c 'SELECT count(*) FROM memory.projection_apply_event' | tr -d '[:space:]')" \
  == "$projection_before" ]]

restore_timers
migration_applied=0
[[ "$(systemctl is-active brains.service)" == active ]]
curl --fail --silent --max-time 5 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null

report="$snapshot_root/memory_v1_v5_2_identity_name_renderer_${run_tag}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha256" \
  --arg migration_sha256 "${expected_sha256[$migration]}" \
  --arg rollback_sha256 "${expected_sha256[$rollback]}" \
  --arg qdrant_sha256 "$qdrant_before" \
  '{
    contract_version:"memory_v1_v5_2_identity_name_renderer_install_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    backup:{path:$backup,sha256:$backup_sha256},
    migration_sha256:$migration_sha256,
    rollback_sha256:$rollback_sha256,
    results:{
      row_changes:0,
      claim_changes:0,
      projection_changes:0,
      qdrant_changes:0,
      account_isolation:true,
      rollback_retained:true
    },
    qdrant_sha256:$qdrant_sha256
  }' >"$report"
chmod 0600 "$report"

printf '%s\n' \
  'IDENTITY_NAME_RENDERER_PRODUCTION=PASS' \
  "HEAD=$(git rev-parse HEAD)" \
  "BACKUP=$backup" \
  "BACKUP_SHA256=$backup_sha256" \
  "REPORT=$report"

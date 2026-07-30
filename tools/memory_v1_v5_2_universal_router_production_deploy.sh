#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Deploys the clone-tested V5.2 router owner-discovery
# correction. No database schema, memory data, or Qdrant writes are allowed.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for backup and timer control' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_UNIVERSAL_ROUTER_DEPLOY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_UNIVERSAL_ROUTER_DEPLOY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
production_repo=/opt/chat-memory
required_production=69a412f105d091f913313b357fa0fe9aec8e5e60
target_commit=${MEMORY_V1_V5_2_UNIVERSAL_ROUTER_TARGET_COMMIT:?target commit is required}
container=brains-postgres-1
database=memory
backup_root=/var/backups/chat-memory
review_root=/home/ubuntu/memory-v1-reviews
worker=scripts/memory_v1_v5_2_local_packet_router.py
service_source=ops/systemd/memory-v1-v5-2-local-packet-router.service
service_target=/etc/systemd/system/memory-v1-v5-2-local-packet-router.service
clone_harness=tools/memory_v1_v5_2_universal_router_clone.sh
expected_worker_sha=7caf1902c394da55f54a51122885227fb5277e94f8bebb309e4479702e8c0a82
expected_service_sha=dc176490a15ee93f032f564f7e8f077fcfc1a72b4b6f9368024591e285f24b38
expected_clone_sha=df26bd93a3267a4ed9c22725ab0e010e78281a3fbeb8e97b5d885dcb4893f6cc

timer_state=$(mktemp /tmp/memory-v5-2-universal-router.XXXXXX.timers)
dry_output=$(mktemp /tmp/memory-v5-2-universal-router.XXXXXX.json)
chmod 0600 "$timer_state" "$dry_output"
timers_quiesced=0

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null
    elif [[ "$enabled" == disabled ]]; then
      systemctl disable "$unit" >/dev/null 2>&1 || true
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit" 2>/dev/null || true)" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

cleanup() {
  rc=$?
  trap - EXIT
  restore_timers || rc=1
  rm -f "$timer_state" "$dry_output"
  exit "$rc"
}
trap cleanup EXIT

cd "$repo_root"
[[ "$(git rev-parse HEAD)" == "$target_commit" ]]
[[ -z "$(git status --porcelain)" ]]
[[ "$(git -C "$production_repo" rev-parse HEAD)" == "$required_production" ]]
[[ -z "$(git -C "$production_repo" status --porcelain)" ]]
git -C "$production_repo" merge-base --is-ancestor \
  "$required_production" "$target_commit"
[[ "$(sha256sum "$worker" | awk '{print $1}')" == "$expected_worker_sha" ]]
[[ "$(sha256sum "$service_source" | awk '{print $1}')" == "$expected_service_sha" ]]
[[ "$(sha256sum "$clone_harness" | awk '{print $1}')" == "$expected_clone_sha" ]]
! grep -q -- '--owner-user-id' "$service_source"
/opt/chat-memory/venv/bin/python -m py_compile "$worker"
/opt/chat-memory/venv/bin/python -m unittest \
  tests.test_memory_v1_v5_2_local_packet_router \
  tests.test_memory_v1_authenticated_owners
systemd-analyze verify "$service_source"

"$clone_harness"

routes_before=$(scalar \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')
stage_before=$(scalar 'SELECT count(*) FROM memory.relational_stage_batch')
claims_before=$(scalar 'SELECT count(*) FROM memory.claim')
qdrant_before=$(qdrant_signature)

while IFS= read -r unit; do
  enabled=$(systemctl is-enabled "$unit" 2>/dev/null || true)
  active=$(systemctl is-active "$unit" 2>/dev/null || true)
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

mkdir -p "$backup_root"
run_tag=$(date -u +%Y%m%dT%H%M%SZ)
backup="$backup_root/memory_pre_v5_2_universal_router_${run_tag}.dump"
backup_sha="$backup.sha256"
umask 077
docker exec "$container" pg_dump -U sage -d "$database" -Fc >"$backup"
[[ -s "$backup" ]]
sha256sum "$backup" >"$backup_sha"
chmod 0600 "$backup" "$backup_sha"
umask 022

sudo -u ubuntu git -C "$production_repo" merge --ff-only "$target_commit"
[[ "$(git -C "$production_repo" rev-parse HEAD)" == "$target_commit" ]]
[[ -z "$(git -C "$production_repo" status --porcelain)" ]]
install -o root -g root -m 0644 \
  "$production_repo/$service_source" "$service_target"
systemctl daemon-reload
systemd-analyze verify "$service_target"
! systemctl cat memory-v1-v5-2-local-packet-router.service --no-pager \
  | grep -q -- '--owner-user-id'

set -a
source "$production_repo/.env"
set +a
sudo -u ubuntu env \
  POSTGRES_DSN="$POSTGRES_DSN" \
  PYTHONPATH="$production_repo" \
  "$production_repo/venv/bin/python" "$production_repo/$worker" \
  --review-root "$review_root" >"$dry_output"
jq -e '
  .apply==false and .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0 and
  (.plans | length)>=2
' "$dry_output" >/dev/null

[[ "$(scalar 'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" \
  == "$routes_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.relational_stage_batch')" \
  == "$stage_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
curl --fail --silent --show-error --max-time 30 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null

restore_timers

printf 'PRODUCTION_HEAD=%s\n' "$target_commit"
printf 'BACKUP=%s\n' "$backup"
printf 'BACKUP_SHA256=%s\n' "$(awk '{print $1}' "$backup_sha")"
printf 'AUTHENTICATED_OWNER_PLANS=%s\n' "$(jq '.plans | length' "$dry_output")"
printf 'DATABASE_WRITES=0\n'
printf 'QDRANT_WRITES=0\n'
printf 'SERVICE_HEALTH=ok\n'
printf 'TIMERS_RESTORED=exact\n'
printf 'memory_v1_v5_2_universal_router_production_deploy: PASS\n'

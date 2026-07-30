#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the clone-tested duplicate-packet authority
# correction, supersedes one exact older packet, and routes its exact
# replacement into manual review. No staging, claims, Qdrant, or prompt writes.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for backup and timer control' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_DUPLICATE_AUTHORITY_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_DUPLICATE_AUTHORITY_APPLY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
production_repo=/opt/chat-memory
required_production=f212103ce143d8b0e80caa4de8977c4afa8f9b26
target_commit=${MEMORY_V1_V5_2_DUPLICATE_AUTHORITY_TARGET_COMMIT:?target commit is required}
container=brains-postgres-1
database=memory
backup_root=/var/backups/chat-memory
review_root=/home/ubuntu/memory-v1-reviews
owner=9dd7426d-77eb-4765-9db2-13e33ad7444d
other=1240822d-ac9a-4096-95aa-e2b24d36ef50
evidence=30ffeb73-3c76-4e84-a9cd-6e50f89a5afe
prior=98868708-3acf-5243-9447-3515f06d83be
replacement=b962f49b-73f8-5a10-8f07-afbe9d0c54dc
prior_sha=865ff509b5694511a4d581eef057e6b421c7c296ec05a356784648fe8b3918bb
replacement_sha=4e640f21cada7a01a5ecb133109e2d9a78c1da3247ebae0e0c05b3ee79cbadf7
operation=28bc15e4-1220-59f7-bbb8-8b1a190d3a68
supersession=1a60c02f-d64a-5bc5-9934-54be7a413d8c
reason=duplicate_active_packet_reconciled
migration=ops/sql/20260730_memory_v1_v5_2_duplicate_packet_authority.sql
rollback=ops/sql/20260730_memory_v1_v5_2_duplicate_packet_authority_rollback.sql
security_test=tests/memory_v1_v5_2_duplicate_packet_authority.sql
clone_harness=tools/memory_v1_v5_2_duplicate_packet_authority_clone.sh
worker=scripts/memory_v1_v5_2_local_packet_router.py
expected_migration_sha=cf8e2ffad9095fe2e191437365ba08caef73405b5ae4c9aedf6373cbbff471a0
expected_rollback_sha=272df8105a4403b0a615ac41271a6d0774b0b6dd0479839b6ce144d482ceb1fe
expected_security_sha=1bb901162d499cc8b56e29e097c78e08f1bbaccf400b498d79c45998af4a6b9a
expected_clone_sha=13dbbe1c664aa1591582a90c55e5867bbf96dfe1ca5ffc05fc1087e2e4f23a09
expected_worker_sha=7caf1902c394da55f54a51122885227fb5277e94f8bebb309e4479702e8c0a82

timer_state=$(mktemp /tmp/memory-v5-2-duplicate-authority.XXXXXX.timers)
work=$(mktemp -d /tmp/memory-v5-2-duplicate-authority.XXXXXX)
chmod 0600 "$timer_state"
chmod 0700 "$work"
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
  rm -rf "$work"
  rm -f "$timer_state"
  exit "$rc"
}
trap cleanup EXIT

cd "$repo_root"
[[ "$(git rev-parse HEAD)" == "$target_commit" ]]
[[ -z "$(git status --porcelain)" ]]
[[ "$(git -C "$production_repo" rev-parse HEAD)" == "$required_production" ]]
[[ -z "$(git -C "$production_repo" status --porcelain)" ]]
git merge-base --is-ancestor "$required_production" "$target_commit"
[[ "$(sha256sum "$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$security_test" | awk '{print $1}')" == "$expected_security_sha" ]]
[[ "$(sha256sum "$clone_harness" | awk '{print $1}')" == "$expected_clone_sha" ]]
[[ "$(sha256sum "$worker" | awk '{print $1}')" == "$expected_worker_sha" ]]
bash -n "$0"
"$clone_harness"

supersessions_before=$(scalar \
  'SELECT count(*) FROM memory.v5_local_packet_supersession')
routes_before=$(scalar \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')
stage_before=$(scalar 'SELECT count(*) FROM memory.relational_stage_batch')
claims_before=$(scalar 'SELECT count(*) FROM memory.claim')
qdrant_before=$(qdrant_signature)

[[ "$(scalar "
  SELECT count(*)
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id='$evidence'::uuid
    AND packet_id IN ('$prior'::uuid, '$replacement'::uuid)
")" == 2 ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id IN ('$prior'::uuid, '$replacement'::uuid)
")" == 0 ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_local_packet_supersession
  WHERE owner_user_id='$owner'::uuid
    AND prior_packet_id='$prior'::uuid
    AND replacement_packet_id='$replacement'::uuid
")" == 0 ]]

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
backup="$backup_root/memory_pre_v5_2_duplicate_authority_${run_tag}.dump"
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

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$production_repo/$migration" >/dev/null

set -a
source "$production_repo/.env"
set +a

psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  -v owner_user_id="$owner" \
  -v other_owner_user_id="$other" \
  -v evidence_id="$evidence" \
  -v prior_packet_id="$prior" \
  -v replacement_packet_id="$replacement" \
  -v prior_packet_storage_sha256="$prior_sha" \
  -v replacement_packet_storage_sha256="$replacement_sha" \
  <"$production_repo/$security_test" >/dev/null

[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_supersession')" \
  == "$supersessions_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" \
  == "$routes_before" ]]

psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  -v owner="$owner" \
  -v prior="$prior" \
  -v replacement="$replacement" \
  -v prior_sha="$prior_sha" \
  -v replacement_sha="$replacement_sha" \
  -v operation="$operation" \
  -v supersession="$supersession" \
  -v reason="$reason" <<'SQL' >"$work/supersession.txt"
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT *
FROM memory.finalize_owner_v5_2_duplicate_packet_supersession_v1(
  :'operation'::uuid,
  :'supersession'::uuid,
  :'prior'::uuid,
  :'replacement'::uuid,
  :'prior_sha',
  :'replacement_sha',
  :'reason'
);
COMMIT;
SQL
grep -q 'applied' "$work/supersession.txt"

psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  -v owner="$owner" \
  -v prior="$prior" \
  -v replacement="$replacement" \
  -v prior_sha="$prior_sha" \
  -v replacement_sha="$replacement_sha" \
  -v operation="$operation" \
  -v supersession="$supersession" \
  -v reason="$reason" <<'SQL' >"$work/supersession-replay.txt"
BEGIN;
SELECT set_config('app.user_id', :'owner', true);
SELECT *
FROM memory.finalize_owner_v5_2_duplicate_packet_supersession_v1(
  :'operation'::uuid,
  :'supersession'::uuid,
  :'prior'::uuid,
  :'replacement'::uuid,
  :'prior_sha',
  :'replacement_sha',
  :'reason'
);
COMMIT;
SQL
grep -q 'replayed' "$work/supersession-replay.txt"

POSTGRES_DSN="$POSTGRES_DSN" \
MEMORY_V1_V5_2_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_2_local_packet_router_apply_v1 \
PYTHONPATH="$production_repo" \
sudo -u ubuntu --preserve-env=POSTGRES_DSN,MEMORY_V1_V5_2_LOCAL_PACKET_ROUTER_APPLY,PYTHONPATH \
  "$production_repo/venv/bin/python" "$production_repo/$worker" \
  --owner-user-id "$owner" --packet-id "$replacement" \
  --review-root "$review_root" --apply >"$work/route.json"
jq -e '
  .apply==true and .outcome=="manual_review_artifact_ready" and
  .zero_write_replay_proved==true and
  .write_counts.route_events==1 and .write_counts.stage==0 and
  .write_counts.claims==0 and .write_counts.qdrant==0 and
  .write_counts.prompt_influence==0 and .external_model_calls==0
' "$work/route.json" >/dev/null

POSTGRES_DSN="$POSTGRES_DSN" \
MEMORY_V1_V5_2_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_2_local_packet_router_apply_v1 \
PYTHONPATH="$production_repo" \
sudo -u ubuntu --preserve-env=POSTGRES_DSN,MEMORY_V1_V5_2_LOCAL_PACKET_ROUTER_APPLY,PYTHONPATH \
  "$production_repo/venv/bin/python" "$production_repo/$worker" \
  --owner-user-id "$owner" --packet-id "$replacement" \
  --review-root "$review_root" --apply >"$work/route-replay.json"
jq -e '
  .apply==true and .outcome=="no_work" and
  .write_counts.route_events==0 and .write_counts.stage==0 and
  .write_counts.claims==0 and .write_counts.qdrant==0 and
  .write_counts.prompt_influence==0 and .external_model_calls==0
' "$work/route-replay.json" >/dev/null

POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$production_repo" \
sudo -u ubuntu --preserve-env=POSTGRES_DSN,PYTHONPATH \
  "$production_repo/venv/bin/python" "$production_repo/$worker" \
  --owner-user-id "$other" --packet-id "$replacement" \
  --review-root "$review_root" >"$work/cross-owner.json"
jq -e '
  .apply==false and (.plans | length)==1 and
  .plans[0].route=="no_work" and .database_writes==0 and
  .qdrant_writes==0 and .prompt_influence==0
' "$work/cross-owner.json" >/dev/null

[[ "$(scalar 'SELECT count(*) FROM memory.v5_local_packet_supersession')" \
  == "$((supersessions_before + 1))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" \
  == "$((routes_before + 1))" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.relational_stage_batch')" \
  == "$stage_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_local_packet_supersession
  WHERE owner_user_id='$owner'::uuid
    AND prior_packet_id='$prior'::uuid
    AND replacement_packet_id='$replacement'::uuid
    AND reason_code='$reason'
")" == 1 ]]
[[ "$(scalar "
  SELECT count(*)
  FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id='$replacement'::uuid
    AND route='manual_review_artifact_ready'
")" == 1 ]]

if docker exec "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    UPDATE memory.v5_local_packet_supersession
    SET reason_code=reason_code
    WHERE supersession_id='$supersession'::uuid
  " >/dev/null 2>&1; then
  echo 'append-only supersession accepted an update' >&2
  exit 1
fi

[[ "$(systemctl is-active brains.service)" == active ]]
curl --fail --silent --show-error --max-time 30 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null

restore_timers

printf 'PRODUCTION_HEAD=%s\n' "$target_commit"
printf 'BACKUP=%s\n' "$backup"
printf 'BACKUP_SHA256=%s\n' "$(awk '{print $1}' "$backup_sha")"
printf 'ROLLBACK=%s\n' "$production_repo/$rollback"
printf 'ROLLBACK_SHA256=%s\n' "$expected_rollback_sha"
printf 'SUPERSESSIONS=1\n'
printf 'ROUTES=1\n'
printf 'STAGE_WRITES=0\n'
printf 'CLAIM_WRITES=0\n'
printf 'QDRANT_WRITES=0\n'
printf 'CROSS_OWNER_VISIBLE=0\n'
printf 'ZERO_WRITE_REPLAY=proved\n'
printf 'SERVICE_HEALTH=ok\n'
printf 'TIMERS_RESTORED=exact\n'
printf 'memory_v1_v5_2_duplicate_packet_authority_production_apply: PASS\n'

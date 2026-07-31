#!/usr/bin/env bash
set -Eeuo pipefail

# seebx backend only. Clone-tests the exact enqueue, one private extraction,
# packet route, and append-only supersession for the reviewed Jerry record.

if [[ ${EUID} -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 2
fi

repo=$(git rev-parse --show-toplevel)
production_repo=/opt/chat-memory
container=brains-postgres-1
production=memory
clone="memory_v11_jerry_full_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
evidence=681ab38d-a742-463c-ad26-c74c65eacaa9
prior_packet=d7cfab30-ebf8-56d6-b8c6-9db8a94d068d
content_sha=46f40455eba48cdd0c4e131cc8f01ea15d8721b761bf0e15365d008a0562ea93
prior_storage_sha=fa634cbe059919421e6d680bde6df7d292e0ee95c2391446a43f1ecf9a01d6de
selector=20260731_v5_2_semantic_compiler_v11_jerry_v1
compiler_sha=275b5150f42e6d90e0afcebe86b32ab691be2e682c4d0c74e61947652230f6a2
manifest="$repo/manifests/memory_v1_v5_2_semantic_compiler_v11_jerry.json"
manifest_sha=2001eec4debcab72f72050326798001dfe8cf8a3383fc045cbb468a78c8a0cce
persistence_migration=ops/sql/20260731_memory_v1_v5_2_compiler_v11_persistence_compat.sql
persistence_rollback=ops/sql/20260731_memory_v1_v5_2_compiler_v11_persistence_compat_rollback.sql
exact_migration=ops/sql/20260731_memory_v1_v5_2_semantic_compiler_v11_jerry.sql
exact_rollback=ops/sql/20260731_memory_v1_v5_2_semantic_compiler_v11_jerry_rollback.sql
run_id=195fe785-e5b6-54ff-a02c-05c39799ad1b
supersession_operation=699af920-abdb-5758-b850-ee4ab6b47124
supersession_id=5016f3d0-9f04-59df-81ff-e522dee70fe9
timer=memory-v1-v5-local-inference-scheduler.timer
service=memory-v1-v5-local-inference-scheduler.service
work=$(mktemp -d /tmp/memory-v11-jerry-full.XXXXXX)
backup="$work/production.dump"
output="$work/canary.json"
route_output="$work/route.json"
review_root="$work/reviews"
mkdir -p "$review_root"
chmod 0700 "$work" "$review_root"
clone_created=0
timer_was_active=0

cleanup() {
  rc=$?
  trap - EXIT
  if [[ "$timer_was_active" -eq 1 ]]; then
    systemctl start "$timer" >/dev/null 2>&1 || rc=1
  fi
  if [[ "$rc" -ne 0 && "${KEEP_FAILED_CLONE:-0}" == 1 ]]; then
    printf 'FAILED_CLONE_RETAINED=%s\n' "$clone" >&2
    printf 'FAILED_WORK_RETAINED=%s\n' "$work" >&2
    exit "$rc"
  fi
  if [[ "$clone_created" -eq 1 ]]; then
    docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
      >/dev/null 2>&1 || rc=1
  fi
  rm -rf "$work"
  exit "$rc"
}
trap cleanup EXIT

scalar() {
  docker exec "$container" psql -U sage -d "$1" -X -Atqc "$2"
}

actor_scalar() {
  docker exec "$container" psql -U sage -d "$1" -X -Atq \
    -v ON_ERROR_STOP=1 -c \
    "SET app.user_id='$2'; SET SESSION AUTHORIZATION brains_app; $3"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

protected_signature() {
  scalar "$1" "
    SELECT encode(public.digest(convert_to(coalesce(string_agg(
      row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(value)::text AS row_json FROM (
        SELECT 'claim' AS source,to_jsonb(row_value) AS value
        FROM memory.claim AS row_value
        UNION ALL
        SELECT 'entity',to_jsonb(row_value) FROM memory.entity AS row_value
        UNION ALL
        SELECT 'observation',to_jsonb(row_value)
        FROM memory.observation AS row_value
        UNION ALL
        SELECT 'binding',to_jsonb(row_value)
        FROM memory.final_answer_memory_binding_v1 AS row_value
        UNION ALL
        SELECT 'projection_plan',to_jsonb(row_value)
        FROM memory.projection_plan AS row_value
      ) AS protected_rows
    ) AS rows"
}

other_owner_signature() {
  scalar "$1" "
    SELECT encode(public.digest(convert_to(coalesce(string_agg(
      row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(value)::text AS row_json
      FROM memory.evidence_extraction_job AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT to_jsonb(value)::text
      FROM memory.evidence_extraction_packet_v5_local AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT to_jsonb(value)::text
      FROM memory.v5_2_local_packet_route_event AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL
      SELECT to_jsonb(value)::text
      FROM memory.v5_local_packet_supersession AS value
      WHERE owner_user_id<>'$owner'::uuid
    ) AS rows"
}

test "$(sha256sum "$manifest" | awk '{print $1}')" = "$manifest_sha"
for file in "$persistence_migration" "$persistence_rollback" \
  "$exact_migration" "$exact_rollback"; do
  test -s "$repo/$file"
done
test "$(git -C "$production_repo" status --short)" = ''
production_head=$(git -C "$production_repo" rev-parse HEAD)
git -C "$repo" merge-base --is-ancestor "$production_head" HEAD
test "$(git -C "$repo" status --short)" = ''
test "$(systemctl is-active brains.service)" = active
test "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" = active
test -r /etc/memory-v1-local-inference/api-key

PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python -m unittest \
  tests.test_memory_v1_semantic_compiler_v10 \
  tests.test_memory_v1_semantic_compiler_v11 \
  tests.test_memory_v1_local_provider_v5_2 \
  tests.test_memory_v1_v5_2_local_packet_router >/dev/null

if [[ "$(systemctl is-active "$timer")" == active ]]; then
  timer_was_active=1
  systemctl stop "$timer"
fi
for _attempt in $(seq 1 60); do
  systemctl is-active --quiet "$service" || break
  sleep 1
done
! systemctl is-active --quiet "$service"

production_qdrant_before=$(qdrant_signature)
production_protected_before=$(protected_signature "$production")

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
test -s "$backup"
docker exec "$container" createdb -U sage -T template0 "$clone"
clone_created=1
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

# Both schema changes have exact rollback/reapply behavior before data writes.
for file in "$persistence_migration" "$exact_migration"; do
  docker exec -i "$container" psql -U sage -d "$clone" -X \
    -v ON_ERROR_STOP=1 <"$repo/$file" >/dev/null
done
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$repo/$exact_rollback" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$repo/$persistence_rollback" >/dev/null
for file in "$persistence_migration" "$exact_migration"; do
  docker exec -i "$container" psql -U sage -d "$clone" -X \
    -v ON_ERROR_STOP=1 <"$repo/$file" >/dev/null
done

set -a
source "$production_repo/.env"
set +a
clone_dsn=$(
  /opt/chat-memory/venv/bin/python - "$POSTGRES_DSN" "$clone" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit

parts = urlsplit(sys.argv[1])
if parts.hostname not in {"127.0.0.1", "::1", "localhost"}:
    raise SystemExit("production DSN is not loopback")
print(urlunsplit((parts.scheme, parts.netloc, "/" + sys.argv[2], parts.query, "")))
PY
)

manifest_json=$(jq -c . "$manifest")
enqueue_result=$(docker exec -i "$container" psql -U sage -d "$clone" \
  -X -At -F $'\t' -v ON_ERROR_STOP=1 \
  -v manifest="$manifest_json" -v manifest_sha="$manifest_sha" \
  -v compiler_sha="$compiler_sha" -v owner="$owner" <<'SQL'
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', false);
SELECT candidate_count,applied_count,replayed_count,job_id,apply_outcome
FROM memory.enqueue_owner_v5_2_semantic_compiler_v11_jerry_v1(
  :'manifest'::jsonb, :'manifest_sha', :'compiler_sha'
);
SQL
)
enqueue_result=$(printf '%s\n' "$enqueue_result" | tail -n 1)
IFS=$'\t' read -r candidates applied replayed job outcome <<<"$enqueue_result"
test "$candidates:$applied:$replayed:$outcome" = '1:1:0:applied'
[[ "$job" =~ ^[0-9a-f-]{36}$ ]]
test "$(actor_scalar "$clone" "$owner" "
  SELECT candidate_count::text||':'||applied_count::text||':'||
    replayed_count::text||':'||apply_outcome
  FROM memory.enqueue_owner_v5_2_semantic_compiler_v11_jerry_v1(
    '$manifest_json'::jsonb,'$manifest_sha','$compiler_sha'
  )
")" = '1:0:1:replayed'

clone_protected_before=$(protected_signature "$clone")
other_before=$(other_owner_signature "$clone")
route_before=$(scalar "$clone" "SELECT count(*)
  FROM memory.v5_2_local_packet_route_event WHERE owner_user_id='$owner'::uuid")
supersession_before=$(scalar "$clone" "SELECT count(*)
  FROM memory.v5_local_packet_supersession WHERE owner_user_id='$owner'::uuid")

POSTGRES_DSN="$clone_dsn" \
MEMORY_V1_V5_LOCAL_INFERENCE_APPLY=memory_v1_v5_local_inference_canary_apply_v1 \
MEMORY_V1_LOCAL_INFERENCE_API_KEY="$(</etc/memory-v1-local-inference/api-key)" \
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python \
  "$repo/scripts/memory_v1_v5_local_inference_canary.py" \
  --owner-user-id "$owner" --evidence-id "$evidence" \
  --expected-job-id "$job" --expected-content-sha256 "$content_sha" \
  --selector-version "$selector" --contract-profile v5_2 \
  --run-id "$run_id" --max-attempts 1 --max-output-tokens 4096 \
  --rolling-window-seconds 3600 --max-reserved-jobs 100 \
  --failure-threshold 3 --apply >"$output"
jq -e '
  .outcome=="accepted" and .local_model_calls==1
  and .external_model_calls==0
  and .audit.policy_compiler_version=="memory_v1_semantic_policy_compiler_v11"
  and .zero_write_replay_proved==true
  and .write_counts.claims==0 and .write_counts.qdrant==0
  and .write_counts.prompt_influence==0
' "$output" >/dev/null

replacement_packet=$(scalar "$clone" "SELECT packet_id
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid
    AND policy_compiler_sha256='$compiler_sha'")
[[ "$replacement_packet" =~ ^[0-9a-f-]{36}$ ]]

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" \
MEMORY_V1_V5_2_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_2_local_packet_router_apply_v1 \
  /opt/chat-memory/venv/bin/python \
  "$repo/scripts/memory_v1_v5_2_local_packet_router.py" \
  --owner-user-id "$owner" --packet-id "$replacement_packet" \
  --review-root "$review_root" --apply >"$route_output"
jq -e '
  .outcome=="manual_review_artifact_ready"
  and .plans[0].route=="manual_review_artifact_ready"
  and .plans[0].reason_code=="reviewable_relational_packet_v5_2"
  and .write_counts.claims==0 and .write_counts.qdrant==0
  and .write_counts.prompt_influence==0
' "$route_output" >/dev/null

replacement_storage=$(scalar "$clone" "SELECT packet_storage_sha256
  FROM memory.evidence_extraction_packet_v5_local
  WHERE packet_id='$replacement_packet'::uuid")
test "$(actor_scalar "$clone" "$owner" "
  SELECT count(*) FROM memory.plan_owner_v5_2_semantic_compiler_v11_jerry_supersession_v1(
    '$prior_packet'::uuid,'$replacement_packet'::uuid
  ) WHERE prior_packet_storage_sha256='$prior_storage_sha'
    AND replacement_packet_storage_sha256='$replacement_storage'
    AND replacement_route='manual_review_artifact_ready'
    AND reason_code='semantic_compiler_v11_reextracted'
")" -eq 1

apply_supersession() {
  actor_scalar "$clone" "$owner" "
    SELECT apply_outcome
    FROM memory.finalize_owner_v5_2_semantic_compiler_v11_jerry_supersession_v1(
      '$supersession_operation'::uuid,'$supersession_id'::uuid,
      '$prior_packet'::uuid,'$replacement_packet'::uuid,
      '$prior_storage_sha','$replacement_storage',
      'manual_review_artifact_ready','semantic_compiler_v11_reextracted'
    )
  "
}
test "$(apply_supersession)" = applied
test "$(apply_supersession)" = replayed

test "$(( $(scalar "$clone" "SELECT count(*)
  FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid") - route_before ))" -eq 1
test "$(( $(scalar "$clone" "SELECT count(*)
  FROM memory.v5_local_packet_supersession
  WHERE owner_user_id='$owner'::uuid") - supersession_before ))" -eq 1
test "$(protected_signature "$clone")" = "$clone_protected_before"
test "$(other_owner_signature "$clone")" = "$other_before"
test "$(actor_scalar "$clone" "$other" "
  SELECT count(*) FROM memory.read_owner_v5_local_packet_review_v1(
    '$replacement_packet'::uuid
  )
")" -eq 0
test "$(actor_scalar "$clone" "$owner" "
  SELECT count(*) FROM memory.read_owner_v5_local_packet_review_v1(
    '$replacement_packet'::uuid
  )
")" -eq 1
test "$(scalar "$clone" "SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid")" -eq 0

test "$(protected_signature "$production")" = "$production_protected_before"
test "$(qdrant_signature)" = "$production_qdrant_before"
test "$(git -C "$production_repo" rev-parse HEAD)" = "$production_head"
test "$(git -C "$production_repo" status --short)" = ''
test "$(systemctl is-active brains.service)" = active

printf '%s\n' 'memory_v1_v5_2_semantic_compiler_v11_jerry_full_clone: PASS'
printf 'production_head=%s\n' "$production_head"
printf 'candidate=1 packet=1 route=1 supersession=1\n'
printf 'local_model_calls=1 external_model_calls=0\n'
printf 'claims=0 entities=0 observations=0 qdrant=0 prompt_influence=0\n'
printf 'owner_rows=1 other_owner_rows=0 zero_write_replay=true\n'

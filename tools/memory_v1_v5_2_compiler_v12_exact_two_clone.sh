#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 2
fi

wt=/home/ubuntu/chat-memory-v5-2-provenance-coverage-conflict-v1
container=brains-postgres-1
production=memory
clone="memory_v12_exact_two_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
selector=20260731_v5_2_compiler_v12_exact_two_v1
compiler_sha=91e3830e676b6c9abd881a52f3d6b010679809c86fffbc889134d9653deba5f3
manifest_sha=13682f6959b491f1ab8c0e90cd54843c6164cc9b202b0683ccda53c430fe2052
stance=61d4fb6f-b211-491e-8edc-d160efefe17e
stance_content=966b7a3d4c47e77bae6b7eaea87d8c8a88d268d3d48dcf3fa9c20dbca3dc41e8
stance_prior=50405677-84aa-5d72-b800-89418fc606e4
stance_prior_storage=6276f9c4cf03ee8276b96451e0c7915691ebfeb459036123a5f0fa977c826cbc
jerry=681ab38d-a742-463c-ad26-c74c65eacaa9
jerry_content=46f40455eba48cdd0c4e131cc8f01ea15d8721b761bf0e15365d008a0562ea93
jerry_prior=c4db1405-ad9d-5c9a-81e3-10ad0200b0ca
jerry_prior_storage=90baad563bd05cdaf93f9d9b72ec5593e86592e4e0f334143a287e5f5e76dc5d
work=$(mktemp -d /tmp/memory-v12-exact-two.XXXXXX)
dump="$work/production.dump"
memory_acl_list="$work/memory-acl.list"
review_root="$work/reviews"
mkdir -p "$review_root"
chmod 0700 "$work" "$review_root"
phase=preflight
clone_created=0

cleanup() {
  rc=$?
  trap - EXIT
  if [[ $rc -ne 0 && ${KEEP_FAILED_CLONE:-0} == 1 ]]; then
    printf 'FAILED_CLONE_RETAINED=%s\nFAILED_WORK_RETAINED=%s\n' \
      "$clone" "$work" >&2
    exit "$rc"
  fi
  if [[ $clone_created -eq 1 ]]; then
    docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
      >/dev/null 2>&1 || rc=1
  fi
  rm -rf "$work"
  if [[ $rc -ne 0 ]]; then
    printf 'V12_FULL_CLONE=FAIL phase=%s rc=%s\n' "$phase" "$rc" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT

scalar() {
  docker exec "$container" psql -U sage -d "$1" -X -Atqc "$2"
}

actor_scalar() {
  docker exec "$container" psql -U sage -d "$1" -X -Atqc \
    "SET app.user_id='$2'; SET SESSION AUTHORIZATION brains_app; $3"
}

apply_file() {
  docker exec -i "$container" psql -U sage -d "$clone" -X \
    -v ON_ERROR_STOP=1 <"$wt/$1" >/dev/null
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
        UNION ALL SELECT 'entity',to_jsonb(row_value) FROM memory.entity AS row_value
        UNION ALL SELECT 'observation',to_jsonb(row_value)
          FROM memory.observation AS row_value
        UNION ALL SELECT 'binding',to_jsonb(row_value)
          FROM memory.final_answer_memory_binding_v1 AS row_value
        UNION ALL SELECT 'projection',to_jsonb(row_value)
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
      UNION ALL SELECT to_jsonb(value)::text
      FROM memory.evidence_extraction_packet_v5_local AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL SELECT to_jsonb(value)::text
      FROM memory.v5_2_local_packet_route_event AS value
      WHERE owner_user_id<>'$owner'::uuid
      UNION ALL SELECT to_jsonb(value)::text
      FROM memory.v5_local_packet_supersession AS value
      WHERE owner_user_id<>'$owner'::uuid
    ) AS rows"
}

target_signature() {
  scalar "$1" "
    SELECT encode(public.digest(convert_to(coalesce(string_agg(
      row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(value)::text AS row_json
      FROM memory.evidence_extraction_job AS value
      WHERE owner_user_id='$owner'::uuid
        AND evidence_id IN ('$stance'::uuid,'$jerry'::uuid)
      UNION ALL SELECT to_jsonb(value)::text
      FROM memory.evidence_extraction_packet_v5_local AS value
      WHERE owner_user_id='$owner'::uuid
        AND evidence_id IN ('$stance'::uuid,'$jerry'::uuid)
      UNION ALL SELECT to_jsonb(value)::text
      FROM memory.v5_2_local_packet_route_event AS value
      WHERE owner_user_id='$owner'::uuid
        AND evidence_id IN ('$stance'::uuid,'$jerry'::uuid)
      UNION ALL SELECT to_jsonb(value)::text
      FROM memory.v5_local_packet_supersession AS value
      WHERE owner_user_id='$owner'::uuid
        AND evidence_id IN ('$stance'::uuid,'$jerry'::uuid)
    ) AS rows"
}

production_head=$(git -C /opt/chat-memory rev-parse HEAD)
test "$(git -C /opt/chat-memory status --short)" = ''
git -C "$wt" merge-base --is-ancestor "$production_head" HEAD
test "$(systemctl is-active brains.service)" = active

production_qdrant_before=$(qdrant_signature)
production_protected_before=$(protected_signature "$production")
production_target_before=$(target_signature "$production")

phase=clone
docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$dump"
pg_restore -l "$dump" \
  | grep -E ' ACL memory | ACL - SCHEMA memory | ACL public TABLE chat_log ' \
  >"$memory_acl_list"
test -s "$memory_acl_list"
! grep -q 'lifeswitch_chat' "$memory_acl_list"
docker exec "$container" createdb -U sage -T template0 "$clone"
clone_created=1
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error --no-acl <"$dump"
pg_restore --exit-on-error --use-list="$memory_acl_list" -f - "$dump" \
  | docker exec -i "$container" psql -U sage -d "$clone" -X \
      -v ON_ERROR_STOP=1 >/dev/null

phase=schema
apply_file ops/sql/20260731_memory_v1_v5_2_provenance_coverage_conflict_v1.sql
apply_file ops/sql/20260731_memory_v1_v5_2_compiler_v12_persistence_compat.sql
apply_file ops/sql/20260731_memory_v1_v5_2_compiler_v12_exact_two.sql
apply_file ops/sql/20260731_memory_v1_v5_2_compiler_v12_exact_two_rollback.sql
apply_file ops/sql/20260731_memory_v1_v5_2_compiler_v12_persistence_compat_rollback.sql
apply_file ops/sql/20260731_memory_v1_v5_2_provenance_coverage_conflict_v1_rollback.sql
apply_file ops/sql/20260731_memory_v1_v5_2_provenance_coverage_conflict_v1.sql
apply_file ops/sql/20260731_memory_v1_v5_2_compiler_v12_persistence_compat.sql
apply_file ops/sql/20260731_memory_v1_v5_2_compiler_v12_exact_two.sql

manifest=$(jq -c . "$wt/manifests/memory_v1_v5_2_compiler_v12_exact_two.json")
enqueue() {
  docker exec -i "$container" psql -U sage -d "$clone" -X -At -F '|' \
    -v ON_ERROR_STOP=1 -v owner="$owner" -v manifest="$manifest" \
    -v manifest_sha="$manifest_sha" -v compiler_sha="$compiler_sha" <<'SQL'
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', false);
SELECT evidence_id,job_id,apply_outcome
FROM memory.enqueue_owner_v5_2_compiler_v12_exact_two_v1(
  :'manifest'::jsonb, :'manifest_sha', :'compiler_sha'
)
ORDER BY evidence_id;
SQL
}

phase=enqueue
first=$(enqueue | tail -n 2)
second=$(enqueue | tail -n 2)
test "$(printf '%s\n' "$first" | grep -c 'applied$')" -eq 2
test "$(printf '%s\n' "$second" | grep -c 'replayed$')" -eq 2
stance_job=$(scalar "$clone" "SELECT job_id FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$stance'::uuid
    AND selector_version='$selector'")
jerry_job=$(scalar "$clone" "SELECT job_id FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$jerry'::uuid
    AND selector_version='$selector'")

set -a
source /opt/chat-memory/.env
set +a
clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone" \
  /opt/chat-memory/venv/bin/python - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit
p = urlsplit(os.environ["SOURCE_DSN"])
if p.hostname not in {"127.0.0.1", "::1", "localhost"}:
    raise SystemExit("production DSN is not loopback")
print(urlunsplit((p.scheme, p.netloc, "/" + os.environ["CLONE_DB"], p.query, "")))
PY
)

clone_protected_before=$(protected_signature "$clone")
other_before=$(other_owner_signature "$clone")
supersession_before=$(scalar "$clone" "
  SELECT count(*) FROM memory.v5_local_packet_supersession
  WHERE owner_user_id='$owner'::uuid")

run_recompile() {
  local evidence=$1 content=$2 job=$3 prior=$4 prior_storage=$5 run_id=$6 output=$7
  POSTGRES_DSN="$clone_dsn" \
  MEMORY_V1_V5_LOCAL_INFERENCE_APPLY=memory_v1_v5_local_inference_canary_apply_v1 \
  MEMORY_V1_V5_2_ZERO_CALL_RECOMPILE=memory_v1_v5_2_zero_call_packet_recompile_v1 \
  PYTHONPATH="$wt" /opt/chat-memory/venv/bin/python \
    "$wt/scripts/memory_v1_v5_local_inference_canary.py" \
    --owner-user-id "$owner" --evidence-id "$evidence" \
    --expected-job-id "$job" --expected-content-sha256 "$content" \
    --recompile-prior-packet-id "$prior" \
    --recompile-prior-packet-storage-sha256 "$prior_storage" \
    --selector-version "$selector" --contract-profile v5_2 \
    --run-id "$run_id" --max-attempts 1 --max-output-tokens 4096 \
    --rolling-window-seconds 3600 --max-reserved-jobs 100 \
    --failure-threshold 10 --apply >"$output"
  jq -e '.outcome=="accepted" and .local_model_calls==0 and
    .external_model_calls==0 and .zero_write_replay_proved==true and
    .audit.policy_compiler_version=="memory_v1_semantic_policy_compiler_v12" and
    .write_counts.claims==0 and .write_counts.qdrant==0 and
    .write_counts.prompt_influence==0' "$output" >/dev/null
}

phase=recompile
run_recompile "$stance" "$stance_content" "$stance_job" \
  "$stance_prior" "$stance_prior_storage" \
  8efb9cc6-4eea-5b68-91b8-12124ae242a1 "$work/stance.json"
run_recompile "$jerry" "$jerry_content" "$jerry_job" \
  "$jerry_prior" "$jerry_prior_storage" \
  16564305-cbe0-5fdb-966d-94bb61dd67bb "$work/jerry.json"

route_packet() {
  local packet=$1 output=$2
  POSTGRES_DSN="$clone_dsn" PYTHONPATH="$wt" \
  MEMORY_V1_V5_2_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_2_local_packet_router_apply_v1 \
    /opt/chat-memory/venv/bin/python \
    "$wt/scripts/memory_v1_v5_2_local_packet_router.py" \
    --owner-user-id "$owner" --packet-id "$packet" \
    --review-root "$review_root" --apply >"$output"
  jq -e '.outcome=="manual_review_artifact_ready" and
    .plans[0].route=="manual_review_artifact_ready" and
    .write_counts.claims==0 and .write_counts.qdrant==0 and
    .write_counts.prompt_influence==0' "$output" >/dev/null
}

stance_packet=$(scalar "$clone" "SELECT packet_id
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id='$stance_job'::uuid")
jerry_packet=$(scalar "$clone" "SELECT packet_id
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id='$jerry_job'::uuid")

supersede_packet() {
  local prior=$1 prior_storage=$2 replacement=$3 operation=$4 supersession=$5
  local replacement_storage first replay
  replacement_storage=$(scalar "$clone" "SELECT packet_storage_sha256
    FROM memory.evidence_extraction_packet_v5_local
    WHERE owner_user_id='$owner'::uuid AND packet_id='$replacement'::uuid")
  first=$(actor_scalar "$clone" "$owner" "
    SELECT apply_outcome
    FROM memory.finalize_owner_v5_2_compiler_v12_exact_two_supersession_v1(
      '$operation'::uuid,'$supersession'::uuid,
      '$prior'::uuid,'$replacement'::uuid,
      '$prior_storage','$replacement_storage',
      'semantic_compiler_v12_reextracted'
    )")
  replay=$(actor_scalar "$clone" "$owner" "
    SELECT apply_outcome
    FROM memory.finalize_owner_v5_2_compiler_v12_exact_two_supersession_v1(
      '$operation'::uuid,'$supersession'::uuid,
      '$prior'::uuid,'$replacement'::uuid,
      '$prior_storage','$replacement_storage',
      'semantic_compiler_v12_reextracted'
    )")
  test "$first" = applied
  test "$replay" = replayed
}

phase=supersession
supersede_packet "$stance_prior" "$stance_prior_storage" "$stance_packet" \
  e024a1f8-d863-5fc8-9336-088b9d7950d7 \
  36be951a-e061-5323-8451-592fe560f7ac
supersede_packet "$jerry_prior" "$jerry_prior_storage" "$jerry_packet" \
  cf3d3d05-a3a9-5e78-93e6-ce8775ba4784 \
  a640b5e8-15e1-50ce-bf48-3a45c673778c
test "$(( $(scalar "$clone" "
  SELECT count(*) FROM memory.v5_local_packet_supersession
  WHERE owner_user_id='$owner'::uuid") - supersession_before ))" -eq 2
test "$(actor_scalar "$clone" "$owner" "
  SELECT (memory.authoritative_owner_v5_2_packet_id_v1('$stance'::uuid)
    ='$stance_packet'::uuid)::integer")" -eq 1
test "$(actor_scalar "$clone" "$owner" "
  SELECT (memory.authoritative_owner_v5_2_packet_id_v1('$jerry'::uuid)
    ='$jerry_packet'::uuid)::integer")" -eq 1
phase=cross_owner_supersession
if actor_scalar "$clone" "$other" "
  SELECT count(*)
  FROM memory.plan_owner_v5_2_compiler_v12_exact_two_supersession_v1(
    '$stance_prior'::uuid,'$stance_packet'::uuid
  )" >/dev/null 2>&1; then
  echo 'cross-owner compiler-v12 supersession plan was not rejected' >&2
  exit 1
fi
phase=route
route_packet "$stance_packet" "$work/stance-route.json"
route_packet "$jerry_packet" "$work/jerry-route.json"

phase=semantics
stance_packet_json=$(scalar "$clone" "SELECT normalized_packet
  FROM memory.evidence_extraction_packet_v5_local
  WHERE packet_id='$stance_packet'::uuid")
jerry_packet_json=$(scalar "$clone" "SELECT normalized_packet
  FROM memory.evidence_extraction_packet_v5_local
  WHERE packet_id='$jerry_packet'::uuid")
jq -e 'any(.observations[];
  .predicate=="stance.reported" and
  any(.source_spans[]; (.quote|ascii_downcase|contains("human being in my perspective is a fractal"))))' \
  <<<"$stance_packet_json" >/dev/null
jq -e 'any(.observations[];
  .predicate=="identity.name" and .object.value=="Jerry" and
  any(.source_spans[]; .quote=="Jerry")) and
  any(.observations[];
  .predicate=="residence.care_setting" and
  .object.value=="assisted_living" and
  any(.source_spans[]; .quote=="assisted-living")) and
  any(.observations[];
  .predicate=="health.user_reported_observation" and
  (.object.value|ascii_downcase|contains("three seconds")))' \
  <<<"$jerry_packet_json" >/dev/null

test "$(actor_scalar "$clone" "$owner" \
  'SELECT count(*) FROM memory.plan_owner_v5_2_observation_conflict_v1(100)')" -eq 1
test "$(actor_scalar "$clone" "$other" \
  'SELECT count(*) FROM memory.plan_owner_v5_2_observation_conflict_v1(100)')" -eq 0
test "$(protected_signature "$clone")" = "$clone_protected_before"
test "$(other_owner_signature "$clone")" = "$other_before"
test "$(qdrant_signature)" = "$production_qdrant_before"
test "$(protected_signature "$production")" = "$production_protected_before"
test "$(target_signature "$production")" = "$production_target_before"
test "$(git -C /opt/chat-memory rev-parse HEAD)" = "$production_head"
test "$(systemctl is-active brains.service)" = active

phase=complete
printf 'V12_FULL_CLONE=PASS jobs=2 packets=2 supersessions=2 routes=2 local_model_calls=0 external_model_calls=0\n'
printf 'stance_span_repaired=true jerry_name=true assisted_living=true temporal_conflict=1\n'
printf 'claims=0 entities=0 observations=0 qdrant=0 prompt_influence=0 cross_owner_isolated=true replay=true\n'

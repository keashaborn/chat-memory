#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Tests exact review/routing of the two compiler-v8
# replacement packets on a disposable production clone. No production writes.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
production=memory
clone="memory_v5_2_compiler_v8_route_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
care_packet=b76915b8-0603-50e8-b263-761da39f5651
profession_packet=6ae4a8e6-b207-5201-a997-53fb2363fc9d
migration=ops/sql/20260725_memory_v1_v5_2_router_zero_call_review_compat.sql
rollback=ops/sql/20260725_memory_v1_v5_2_router_zero_call_review_compat_rollback.sql
sql_test=tests/memory_v1_v5_2_router_zero_call_review_compat.sql
review_test=tests/test_memory_v1_v5_2_review_profile.py
router_test=tests/test_memory_v1_v5_2_local_packet_router.py
worker=scripts/memory_v1_v5_2_local_packet_router.py
python_bin=/opt/chat-memory/venv/bin/python

backup=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.dump)
before_function=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.before)
after_function=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.after)
care_apply=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.care)
profession_apply=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.profession)
care_replay=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.care-replay)
profession_replay=$(mktemp /tmp/memory-v5-2-compiler-v8-route.XXXXXX.profession-replay)
review_root=$(mktemp -d /tmp/memory-v5-2-compiler-v8-route.XXXXXX.reviews)
chmod 0600 "$backup" "$before_function" "$after_function" \
  "$care_apply" "$profession_apply" "$care_replay" "$profession_replay"
chmod 0700 "$review_root"
phase=initialization

cleanup() {
  rc=$?
  trap - EXIT
  if [[ "$rc" -ne 0 ]]; then
    printf 'memory_v1_v5_2_compiler_v8_two_packet_route_clone: FAIL phase=%s\n' \
      "$phase" >&2
    for output in "$care_apply" "$profession_apply" \
      "$care_replay" "$profession_replay"; do
      if [[ -s "$output" ]]; then
        jq -c '{
          outcome,apply,plans,review_resolution_counts,write_counts,
          zero_write_replay_proved,external_model_calls
        }' "$output" >&2 || true
      fi
    done
  fi
  docker exec "$container" dropdb -U sage --if-exists "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup" "$before_function" "$after_function" \
    "$care_apply" "$profession_apply" "$care_replay" "$profession_replay"
  rm -rf "$review_root"
  exit "$rc"
}
trap cleanup EXIT

function_sha() {
  docker exec "$container" psql -U sage -d "$1" -X -Atqc "
    SELECT encode(public.digest(convert_to(string_agg(
      pg_get_functiondef(signature),E'\\n' ORDER BY signature::text
    ),'UTF8'),'sha256'),'hex')
    FROM unnest(ARRAY[
      'memory.authoritative_owner_v5_2_packet_id_v1(uuid)'::regprocedure,
      'memory.plan_owner_v5_2_local_packet_route_v1(integer)'::regprocedure
    ]) AS signature
  "
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

[[ -z "$(git status --porcelain)" ]]
[[ -x "$python_bin" ]]
phase=unit_tests
"$python_bin" -m py_compile \
  scripts/memory_v1_v5_1_review_local_packet.py "$worker"
"$python_bin" -m unittest "$review_test" "$router_test"
qdrant_before=$(qdrant_signature)
production_routes_before=$(docker exec "$container" psql -U sage -d "$production" \
  -X -Atqc 'SELECT count(*) FROM memory.v5_2_local_packet_route_event')

phase=clone_restore
docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

function_sha "$clone" >"$before_function"
phase=migration_security
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 \
  -v owner_user_id="$owner" -v other_owner_user_id="$other" \
  -v zero_call_packet_id="$care_packet" \
  -v one_call_packet_id="$profession_packet" \
  <"$sql_test" >/dev/null

phase=rollback_fidelity
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$rollback" >/dev/null
function_sha "$clone" >"$after_function"
cmp -s "$before_function" "$after_function"
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

set -a
source /opt/chat-memory/.env
set +a
clone_dsn=$("$python_bin" -c \
  'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
  "$POSTGRES_DSN" "$clone")

run_router() {
  local packet=$1 output=$2
  POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  MEMORY_V1_V5_2_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_2_local_packet_router_apply_v1 \
    "$python_bin" "$worker" --owner-user-id "$owner" \
      --packet-id "$packet" --review-root "$review_root" --apply >"$output"
}

phase=caregiving_apply
run_router "$care_packet" "$care_apply"
phase=profession_apply
run_router "$profession_packet" "$profession_apply"
phase=caregiving_replay
run_router "$care_packet" "$care_replay"
phase=profession_replay
run_router "$profession_packet" "$profession_replay"

phase=output_contracts
for output in "$care_apply" "$profession_apply"; do
  jq -e '
    .apply==true and .outcome=="manual_review_artifact_ready" and
    .write_counts.route_events==1 and
    .write_counts.restricted_review_artifacts==2 and
    .write_counts.stage==0 and .write_counts.claims==0 and
    .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
    .zero_write_replay_proved==true and .external_model_calls==0
  ' "$output" >/dev/null
done
for output in "$care_replay" "$profession_replay"; do
  jq -e '
    .apply==true and .outcome=="no_work" and
    (.plans|length)==1 and .plans[0].route=="no_work" and
    .write_counts.route_events==0 and
    .write_counts.restricted_review_artifacts==0 and
    .write_counts.stage==0 and .write_counts.claims==0 and
    .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
    .zero_write_replay_proved==true and .external_model_calls==0
  ' "$output" >/dev/null
done

phase=postflight
[[ "$(find "$review_root" -maxdepth 1 -type f -name '*.json' | wc -l)" == 4 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id IN ('$care_packet'::uuid,'$profession_packet'::uuid)
    AND route='manual_review_artifact_ready'
")" == 2 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$other'::uuid
    AND packet_id IN ('$care_packet'::uuid,'$profession_packet'::uuid)
")" == 0 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id IN (
      'fea59e7e-30f5-4139-b634-97b291c88e14'::uuid,
      'dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5'::uuid
    )
")" == 0 ]]
[[ "$(docker exec "$container" psql -U sage -d "$production" -X -Atqc \
  'SELECT count(*) FROM memory.v5_2_local_packet_route_event')" \
  == "$production_routes_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=complete
printf '%s\n' 'memory_v1_v5_2_compiler_v8_two_packet_route_clone: PASS'

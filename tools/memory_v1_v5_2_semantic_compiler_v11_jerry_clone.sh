#!/usr/bin/env bash
set -Eeuo pipefail

# seebx backend only. Re-extracts the single reviewed Jerry record on a
# disposable production clone. Production Postgres and Qdrant remain read-only.

if [[ ${EUID} -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 2
fi

repo=$(git rev-parse --show-toplevel)
production_repo=/opt/chat-memory
container=brains-postgres-1
production=memory
clone="memory_v11_jerry_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
evidence=681ab38d-a742-463c-ad26-c74c65eacaa9
content_sha=46f40455eba48cdd0c4e131cc8f01ea15d8721b761bf0e15365d008a0562ea93
selector=20260731_v5_2_semantic_compiler_v11_jerry_clone_v1
job=7e4de0cd-1d00-5323-8b77-da7eccc4e2b4
terminal=ef50db5e-576c-557d-b4be-0b81681d0122
event_operation=678bdf79-4b02-5522-90e7-53752d00efbb
run_id=d5e1b900-98dd-5244-8f76-d65bd25ba737
compiler_sha=275b5150f42e6d90e0afcebe86b32ab691be2e682c4d0c74e61947652230f6a2
old_function_sha=8273a2de6dcdb4509c572580670d9bc34474b1b8988bc4172f9d29ae66995c3a
new_function_sha=58c46d113d580376cef0b29fc670ab101e5e3aa8d0ef2095cf111373c8fea3fc
migration=ops/sql/20260731_memory_v1_v5_2_compiler_v11_persistence_compat.sql
rollback=ops/sql/20260731_memory_v1_v5_2_compiler_v11_persistence_compat_rollback.sql
work=$(mktemp -d /tmp/memory-v11-jerry.XXXXXX)
backup="$work/production.dump"
first_output="$work/first.json"
replay_output="$work/replay.json"
timer=memory-v1-v5-local-inference-scheduler.timer
service=memory-v1-v5-local-inference-scheduler.service
clone_created=0
timer_was_active=0

cleanup() {
  rc=$?
  trap - EXIT
  if [[ "$timer_was_active" -eq 1 ]]; then
    systemctl start "$timer" >/dev/null 2>&1 || rc=1
  fi
  if [[ "$clone_created" -eq 1 ]]; then
    docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
      >/dev/null 2>&1 || rc=1
  fi
  if [[ "$rc" -ne 0 && "${KEEP_FAILED_CLONE:-0}" == 1 ]]; then
    printf 'FAILED_CLONE_RETAINED=%s\n' "$clone" >&2
    printf 'FAILED_WORK_RETAINED=%s\n' "$work" >&2
    exit "$rc"
  fi
  rm -rf "$work"
  exit "$rc"
}
trap cleanup EXIT

scalar() {
  local database=$1 query=$2
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "$query"
}

actor_scalar() {
  local database=$1 actor=$2 query=$3
  docker exec "$container" psql -U sage -d "$database" -X -Atq \
    -v ON_ERROR_STOP=1 -c \
    "SET app.user_id='$actor'; SET SESSION AUTHORIZATION brains_app; $query"
}

function_sha() {
  local database=$1
  scalar "$database" "
    SELECT encode(public.digest(convert_to(pg_get_functiondef(
      'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
    ),'UTF8'),'sha256'),'hex')"
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
  local database=$1
  scalar "$database" "
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

test -s "$repo/$migration"
test -s "$repo/$rollback"
test "$(git -C "$production_repo" status --short)" = ''
production_head=$(git -C "$production_repo" rev-parse HEAD)
git -C "$repo" merge-base --is-ancestor "$production_head" HEAD
test "$(git -C "$repo" status --short)" = ''
test "$(systemctl is-active brains.service)" = active
test "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" = active
test -r /etc/memory-v1-local-inference/api-key
test "$(function_sha "$production")" = "$old_function_sha"

PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python -m unittest \
  tests.test_memory_v1_semantic_compiler_v10 \
  tests.test_memory_v1_semantic_compiler_v11 \
  tests.test_memory_v1_local_provider_v5_2 >/dev/null

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

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$repo/$migration" >/dev/null
test "$(function_sha "$clone")" = "$new_function_sha"
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$repo/$migration" >/dev/null
test "$(function_sha "$clone")" = "$new_function_sha"
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$repo/$rollback" >/dev/null
test "$(function_sha "$clone")" = "$old_function_sha"
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$repo/$migration" >/dev/null
test "$(function_sha "$clone")" = "$new_function_sha"
test "$(scalar "$clone" "SELECT (
  has_function_privilege('brains_app',
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)',
    'EXECUTE')
  AND NOT has_function_privilege('public',
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)',
    'EXECUTE'))::integer")" -eq 1

test "$(scalar "$clone" "SELECT count(*) FROM memory.evidence
  WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid
    AND content_sha256='$content_sha' AND status='active'
    AND observed_at='2026-07-30 21:39:53.840736+00'::timestamptz")" -eq 1
test "$(scalar "$clone" "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid")" -eq 0

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 >/dev/null <<SQL
BEGIN;
INSERT INTO memory.evidence_intake_terminal(
  terminal_id,owner_user_id,evidence_id,selector_version,
  outcome,reason_code,evidence_content_sha256,
  decision_fingerprint,actor_user_id,invoked_by_role,details
) VALUES (
  '$terminal'::uuid,'$owner'::uuid,'$evidence'::uuid,'$selector',
  'dispatched','eligible_dispatched','$content_sha',
  encode(public.digest(convert_to(
    '$owner|$evidence|$selector|$compiler_sha','UTF8'
  ),'sha256'),'hex'),
  '$owner'::uuid,'sage',
  jsonb_build_object(
    'route','relational_extraction','extraction_job_id','$job'::uuid,
    'plan_reason_code','eligible_unprocessed',
    'reextract_contract','memory_v1_v5_2_semantic_compiler_v11_jerry_clone_v1',
    'policy_compiler_sha256','$compiler_sha',
    'max_local_model_calls',1
  )
);
INSERT INTO memory.evidence_extraction_job(
  job_id,owner_user_id,evidence_id,intake_terminal_id,
  selector_version,evidence_content_sha256,route,
  intake_reason_code,status
) VALUES (
  '$job'::uuid,'$owner'::uuid,'$evidence'::uuid,'$terminal'::uuid,
  '$selector','$content_sha','relational_extraction',
  'eligible_unprocessed','pending'
);
INSERT INTO memory.evidence_extraction_event(
  owner_user_id,job_id,operation_id,event_type,
  from_status,to_status,actor_type,actor_ref,details
) VALUES (
  '$owner'::uuid,'$job'::uuid,'$event_operation'::uuid,
  'queued',NULL,'pending','system',
  'memory_v1_v5_2_semantic_compiler_v11_jerry_clone_v1',
  jsonb_build_object(
    'intake_terminal_id','$terminal'::uuid,
    'selector_version','$selector',
    'policy_compiler_sha256','$compiler_sha'
  )
);
COMMIT;
SQL

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

clone_protected_before=$(protected_signature "$clone")
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
  --failure-threshold 3 --apply >"$first_output"

jq -e '
  .outcome=="accepted" and .local_model_calls==1
  and .external_model_calls==0
  and .audit.policy_compiler_version=="memory_v1_semantic_policy_compiler_v11"
  and .write_counts.claims==0 and .write_counts.qdrant==0
  and .write_counts.prompt_influence==0
' "$first_output" >/dev/null

test "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid
    AND policy_compiler_sha256='$compiler_sha'
    AND local_model_calls=1 AND external_model_calls=0")" -eq 1
test "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id='$owner'::uuid AND packet.job_id='$job'::uuid
    AND jsonb_array_length(packet.normalized_packet->'entity_mentions')=1
    AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(
        packet.normalized_packet->'entity_mentions'
      ) AS item
      WHERE item->>'name_text'='Jerry'
        AND item->>'entity_type'='person'
        AND item->>'relationship_role'='family:father'
    )
    AND NOT EXISTS (
      SELECT 1 FROM jsonb_array_elements(
        packet.normalized_packet->'entity_mentions'
      ) AS item
      WHERE lower(item->>'name_text')='assisted living'
    )
    AND jsonb_array_length(packet.normalized_packet->'observations')=2
    AND NOT EXISTS (
      SELECT 1 FROM jsonb_array_elements(
        packet.normalized_packet->'observations'
      ) AS item WHERE item->>'predicate'='residence.lives_at'
    )
    AND (
      SELECT count(*) FROM jsonb_array_elements(
        packet.normalized_packet->'observations'
      ) AS item
      WHERE item->'temporal'->'instant_range'->>'lower'
        ='2026-07-30T21:39:53.840736Z'
    )=2
    AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(
        packet.normalized_packet->'observations'
      ) AS item
      WHERE item->>'predicate'='health.user_reported_observation'
        AND item->>'surface_policy'='explicit_recall_only'
        AND item->>'sensitivity'='high'
        AND item->'object'->>'value'='short-term memory lasts about three seconds'
        AND NOT (item->'object'->>'approximate')::boolean
        AND item->'reason_codes' ? 'approximate_reported_duration'
    )
    AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(
        packet.normalized_packet->'deferrals'
      ) AS item
      WHERE item->>'reason_code'='unregistered_predicate'
        AND item->>'memory_shape'='supportive_context'
        AND item->>'sensitivity'='medium'
    )")" -eq 1

# The same operation is a zero-call, zero-write replay.
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
  --failure-threshold 3 --apply >"$replay_output"
jq -e '
  .local_model_calls==0 and .external_model_calls==0
  and .zero_write_replay_proved==true
  and (.write_counts | to_entries | all(.value==0))
' "$replay_output" >/dev/null

test "$(protected_signature "$clone")" = "$clone_protected_before"
test "$(actor_scalar "$clone" "$other" "
  SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE packet_id IN (
    SELECT packet_id FROM memory.evidence_extraction_packet_v5_local
    WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid
  )")" -eq 0
test "$(actor_scalar "$clone" "$owner" "
  SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid")" -eq 1

test "$(function_sha "$production")" = "$old_function_sha"
test "$(protected_signature "$production")" = "$production_protected_before"
test "$(qdrant_signature)" = "$production_qdrant_before"
test "$(git -C "$production_repo" rev-parse HEAD)" = "$production_head"
test "$(systemctl is-active brains.service)" = active

printf '%s\n' 'memory_v1_v5_2_semantic_compiler_v11_jerry_clone: PASS'
printf 'production_head=%s\n' "$production_head"
printf 'compiler_sha=%s\n' "$compiler_sha"
printf 'model_calls=1 external_calls=0 claims=0 qdrant=0 prompt_influence=0\n'
printf 'replacement=Jerry+2_high_sensitivity_observations+care_setting_deferral\n'

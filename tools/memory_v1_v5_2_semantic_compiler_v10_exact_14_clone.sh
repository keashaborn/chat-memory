#!/usr/bin/env bash
set -Eeuo pipefail

# seebx backend only. Re-extracts, routes, and supersedes the exact reviewed
# 14-record cohort on a disposable production clone. Production stores and
# Qdrant remain read-only.

if [[ ${EUID} -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 2
fi

repo=$(git rev-parse --show-toplevel)
production_repo=/opt/chat-memory
container=brains-postgres-1
production=memory
clone="memory_v10_exact14_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
selector=20260731_v5_2_semantic_compiler_v10_exact_14_v1
manifest="$repo/manifests/memory_v1_v5_2_compiler_v10_exact_14.json"
manifest_sha=0df8f548cecab15d5011c2915fda6e854ebbd4b1bf005ff04a22d906d6ffb928
compiler_sha=c50b7e663fa0275051b5ce3522f1124f7f5cf7b0f02aab71bf9535c37e7258ea
persistence_migration=ops/sql/20260731_memory_v1_v5_2_compiler_v10_persistence_compat.sql
exact_migration=ops/sql/20260731_memory_v1_v5_2_semantic_compiler_v10_exact_14.sql
work=$(mktemp -d /tmp/memory-v10-exact14.XXXXXX)
backup="$work/production.dump"
credential_dir="$work/credential"
review_root="$work/reviews"
mkdir -p "$credential_dir" "$review_root"
chmod 0700 "$work" "$credential_dir" "$review_root"
clone_created=0

cleanup() {
  rc=$?
  trap - EXIT
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
  local database=$1 query=$2
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "$query"
}

actor_scalar() {
  local database=$1 actor=$2 query=$3
  docker exec "$container" psql -U sage -d "$database" -X -Atq \
    -v ON_ERROR_STOP=1 -c \
    "SET app.user_id='$actor'; SET SESSION AUTHORIZATION brains_app; $query"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

owner_protected_signature() {
  local database=$1
  scalar "$database" "
    SELECT encode(public.digest(convert_to(coalesce(string_agg(
      row_json,E'\\n' ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(value)::text AS row_json FROM (
        SELECT 'claim' AS source,to_jsonb(row_value) AS value
        FROM memory.claim AS row_value WHERE owner_user_id='$owner'::uuid
        UNION ALL
        SELECT 'entity',to_jsonb(row_value)
        FROM memory.entity AS row_value WHERE owner_user_id='$owner'::uuid
        UNION ALL
        SELECT 'observation',to_jsonb(row_value)
        FROM memory.observation AS row_value WHERE owner_user_id='$owner'::uuid
        UNION ALL
        SELECT 'binding',to_jsonb(row_value)
        FROM memory.final_answer_memory_binding_v1 AS row_value
        WHERE owner_user_id='$owner'::uuid
      ) AS protected_rows
    ) AS rows"
}

other_owner_signature() {
  local database=$1
  scalar "$database" "
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
test -s "$repo/$persistence_migration"
test -s "$repo/$exact_migration"
test "$(git -C "$production_repo" status --short)" = ''
git -C "$production_repo" merge-base --is-ancestor \
  8c52472e6de93fa54a1811c36502ed55643f20ad HEAD
test "$(systemctl is-active brains.service)" = active
test "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" = active
test -r /etc/memory-v1-local-inference/api-key

PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python -m unittest \
  tests.test_memory_v1_semantic_compiler_v10 \
  tests.test_memory_v1_local_provider_v5_2 >/dev/null
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python -m unittest \
  tests.test_memory_v1_v5_2_local_packet_router >/dev/null

production_head=$(git -C "$production_repo" rev-parse HEAD)
production_qdrant_before=$(qdrant_signature)
production_protected_before=$(owner_protected_signature "$production")

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
test -s "$backup"
docker exec "$container" createdb -U sage -T template0 "$clone"
clone_created=1
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$repo/$persistence_migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$repo/$exact_migration" >/dev/null

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
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 -v manifest="$manifest_json" \
  -v owner="$owner" -v manifest_sha="$manifest_sha" \
  -v compiler_sha="$compiler_sha" >/dev/null <<'SQL'
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', false);
SELECT *
FROM memory.enqueue_owner_v5_2_semantic_compiler_v10_exact_14_v1(
  :'manifest'::jsonb, :'manifest_sha', :'compiler_sha'
)
WHERE candidate_count=14 AND applied_count=14
  AND replayed_count=0 AND apply_outcome='applied';
SELECT *
FROM memory.enqueue_owner_v5_2_semantic_compiler_v10_exact_14_v1(
  :'manifest'::jsonb, :'manifest_sha', :'compiler_sha'
)
WHERE candidate_count=14 AND applied_count=0
  AND replayed_count=14 AND apply_outcome='replayed';
SQL

test "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND selector_version='$selector'
    AND status='pending' AND attempts=0")" -eq 14

clone_protected_before=$(owner_protected_signature "$clone")
other_before=$(other_owner_signature "$clone")
packet_before=$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid")
route_before=$(scalar "$clone" "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid")
supersession_before=$(scalar "$clone" "
  SELECT count(*) FROM memory.v5_local_packet_supersession
  WHERE owner_user_id='$owner'::uuid")

install -o root -g root -m 0400 \
  /etc/memory-v1-local-inference/api-key "$credential_dir/local_api_key"
scheduler_output="$work/scheduler.json"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" \
CREDENTIALS_DIRECTORY="$credential_dir" \
MEMORY_V1_V5_LOCAL_SCHEDULER_APPLY=memory_v1_v5_local_inference_scheduler_apply_v1 \
  /opt/chat-memory/venv/bin/python \
  "$repo/scripts/memory_v1_v5_local_inference_scheduler.py" \
  --owner-user-id "$owner" --selector-version "$selector" \
  --contract-profile v5_2 --max-jobs 14 \
  --max-runtime-seconds 3600 --max-attempts 1 --lease-seconds 900 \
  --timeout-seconds 600 --max-output-tokens 4096 \
  --rolling-window-seconds 3600 --max-reserved-jobs 100 \
  --failure-threshold 3 --apply >"$scheduler_output"
chmod 0600 "$scheduler_output"

jq -e '
  .apply==true and .processed==14 and
  (.outcome_counts.accepted // 0)==14 and
  (.outcome_counts.rejected // 0)==0 and
  .local_model_calls==1 and .external_model_calls==0
' "$scheduler_output" >/dev/null

test "$(( $(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid") - packet_before ))" -eq 14
test "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.evidence_extraction_job AS job
  JOIN memory.evidence_extraction_packet_v5_local AS packet
    ON packet.owner_user_id=job.owner_user_id AND packet.job_id=job.job_id
  JOIN memory.evidence_intake_terminal AS terminal
    ON terminal.owner_user_id=job.owner_user_id
   AND terminal.terminal_id=job.intake_terminal_id
  WHERE job.owner_user_id='$owner'::uuid
    AND job.selector_version='$selector'
    AND packet.policy_compiler_sha256='$compiler_sha'
    AND packet.external_model_calls=0
    AND packet.local_model_calls=
        (terminal.details->>'max_local_model_calls')::integer")" -eq 14

route_outputs="$work/routes.jsonl"
: >"$route_outputs"
while IFS= read -r packet_id; do
  POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" \
  MEMORY_V1_V5_2_LOCAL_PACKET_ROUTER_APPLY=memory_v1_v5_2_local_packet_router_apply_v1 \
    /opt/chat-memory/venv/bin/python \
    "$repo/scripts/memory_v1_v5_2_local_packet_router.py" \
    --owner-user-id "$owner" --packet-id "$packet_id" \
    --review-root "$review_root" --apply >>"$route_outputs"
done < <(scalar "$clone" "
  SELECT packet.packet_id
  FROM memory.evidence_extraction_job AS job
  JOIN memory.evidence_extraction_packet_v5_local AS packet
    ON packet.owner_user_id=job.owner_user_id AND packet.job_id=job.job_id
  WHERE job.owner_user_id='$owner'::uuid
    AND job.selector_version='$selector'
  ORDER BY job.evidence_id")

test "$(jq -s '[.[]|select(.outcome=="terminal_no_stage")]|length' \
  "$route_outputs")" -eq 13
test "$(jq -s '[.[]|select(.outcome=="manual_review_artifact_ready")]|length' \
  "$route_outputs")" -eq 1
test "$(( $(scalar "$clone" "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid") - route_before ))" -eq 14

test "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.evidence_extraction_job AS job
  JOIN memory.evidence_extraction_packet_v5_local AS packet
    ON packet.owner_user_id=job.owner_user_id AND packet.job_id=job.job_id
  JOIN memory.evidence_intake_terminal AS terminal
    ON terminal.owner_user_id=job.owner_user_id
   AND terminal.terminal_id=job.intake_terminal_id
  JOIN memory.v5_2_local_packet_route_event AS route
    ON route.owner_user_id=packet.owner_user_id
   AND route.packet_id=packet.packet_id
  WHERE job.owner_user_id='$owner'::uuid
    AND job.selector_version='$selector'
    AND route.route=terminal.details->>'expected_route'")" -eq 14

test "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.evidence_extraction_job AS job
  JOIN memory.evidence_extraction_packet_v5_local AS packet
    ON packet.owner_user_id=job.owner_user_id AND packet.job_id=job.job_id
  JOIN memory.v5_2_local_packet_route_event AS route
    ON route.owner_user_id=packet.owner_user_id
   AND route.packet_id=packet.packet_id
  WHERE job.owner_user_id='$owner'::uuid
    AND job.selector_version='$selector'
    AND job.evidence_id='8fd812bd-e8aa-4f57-bd38-0ec09357ddc8'::uuid
    AND packet.local_model_calls=0
    AND packet.entity_mention_count=0
    AND packet.observation_count=0
    AND packet.manual_review_required
    AND route.route='terminal_no_stage'
    AND route.reason_code='deferral_only_review_unresolved_v5_2'
    AND packet.normalized_packet @?
      '$.deferrals[*] ? (@.reason_code == "entity_resolution_unresolved")'")" -eq 1

test "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.evidence_extraction_job AS job
  JOIN memory.evidence_extraction_packet_v5_local AS packet
    ON packet.owner_user_id=job.owner_user_id AND packet.job_id=job.job_id
  WHERE job.owner_user_id='$owner'::uuid
    AND job.selector_version='$selector'
    AND job.evidence_id='681ab38d-a742-463c-ad26-c74c65eacaa9'::uuid
    AND packet.local_model_calls=1
    AND packet.normalized_packet @? '$.observations[*] ? (
      @.predicate == "residence.lives_at"
    )'
    AND packet.normalized_packet @? '$.observations[*] ? (
      @.predicate == "health.user_reported_observation"
      && @.object.value == "short-term memory lasts about three seconds"
    )'")" -eq 1

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 -v owner="$owner" >/dev/null <<'SQL'
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', false);
DO $apply_supersessions$
DECLARE
  target record;
  first_result record;
  replay_result record;
  operation_id uuid;
  supersession_id uuid;
BEGIN
  FOR target IN
    SELECT
      (terminal.details->>'prior_packet_id')::uuid AS prior_packet_id,
      terminal.details->>'prior_packet_storage_sha256'
        AS prior_storage_sha256,
      packet.packet_id AS replacement_packet_id,
      packet.packet_storage_sha256 AS replacement_storage_sha256,
      route.route
    FROM memory.evidence_extraction_job AS job
    JOIN memory.evidence_intake_terminal AS terminal
      ON terminal.owner_user_id=job.owner_user_id
     AND terminal.terminal_id=job.intake_terminal_id
    JOIN memory.evidence_extraction_packet_v5_local AS packet
      ON packet.owner_user_id=job.owner_user_id AND packet.job_id=job.job_id
    JOIN memory.v5_2_local_packet_route_event AS route
      ON route.owner_user_id=packet.owner_user_id
     AND route.packet_id=packet.packet_id
    WHERE job.owner_user_id=current_setting('app.user_id')::uuid
      AND job.selector_version=
          '20260731_v5_2_semantic_compiler_v10_exact_14_v1'
    ORDER BY job.evidence_id
  LOOP
    operation_id:=gen_random_uuid();
    supersession_id:=gen_random_uuid();
    SELECT * INTO first_result
    FROM memory.finalize_owner_v5_2_semantic_compiler_v10_supersession_v1(
      operation_id,supersession_id,target.prior_packet_id,
      target.replacement_packet_id,target.prior_storage_sha256,
      target.replacement_storage_sha256,target.route,
      'semantic_compiler_v10_reextracted'
    );
    SELECT * INTO replay_result
    FROM memory.finalize_owner_v5_2_semantic_compiler_v10_supersession_v1(
      operation_id,supersession_id,target.prior_packet_id,
      target.replacement_packet_id,target.prior_storage_sha256,
      target.replacement_storage_sha256,target.route,
      'semantic_compiler_v10_reextracted'
    );
    IF first_result.apply_outcome<>'applied'
       OR replay_result.apply_outcome<>'replayed' THEN
      RAISE EXCEPTION 'compiler-v10 supersession replay failed';
    END IF;
  END LOOP;
END
$apply_supersessions$;
SQL

test "$(( $(scalar "$clone" "
  SELECT count(*) FROM memory.v5_local_packet_supersession
  WHERE owner_user_id='$owner'::uuid") - supersession_before ))" -eq 14

authority_count=0
while IFS= read -r evidence_id; do
  is_authoritative=$(actor_scalar "$clone" "$owner" "
    SELECT (
      memory.authoritative_owner_v5_2_packet_id_v1('$evidence_id'::uuid)
      IS NOT NULL
    )::integer")
  test "$is_authoritative" -eq 1
  authority_count=$((authority_count + 1))
done < <(scalar "$clone" "
  SELECT evidence_id
  FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND selector_version='$selector'
  ORDER BY evidence_id")
test "$authority_count" -eq 14

sample_pair=$(scalar "$clone" "
  SELECT prior_packet_id::text || '|' || replacement_packet_id::text
  FROM memory.v5_local_packet_supersession
  WHERE owner_user_id='$owner'::uuid
    AND reason_code='semantic_compiler_v10_reextracted'
  ORDER BY prior_packet_id LIMIT 1")
sample_prior=${sample_pair%%|*}
sample_replacement=${sample_pair##*|}
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 -v other="$other" \
  -v prior="$sample_prior" -v replacement="$sample_replacement" \
  >/dev/null <<'SQL'
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'other', false);
SELECT set_config('test.prior', :'prior', false);
SELECT set_config('test.replacement', :'replacement', false);
DO $cross_owner$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_2_semantic_compiler_v10_supersession_v1(
      current_setting('test.prior')::uuid,
      current_setting('test.replacement')::uuid
    )
  ) THEN
    RAISE EXCEPTION 'cross-owner supersession plan leaked';
  END IF;
END
$cross_owner$;
SQL

test "$(owner_protected_signature "$clone")" = "$clone_protected_before"
test "$(other_owner_signature "$clone")" = "$other_before"
test "$(qdrant_signature)" = "$production_qdrant_before"
test "$(owner_protected_signature "$production")" = \
  "$production_protected_before"
test "$(git -C "$production_repo" rev-parse HEAD)" = "$production_head"
test "$(git -C "$production_repo" status --short)" = ''
test "$(systemctl is-active brains.service)" = active

printf '%s\n' \
  'MEMORY_V1_V5_2_COMPILER_V10_EXACT_14_CLONE=PASS' \
  "PRODUCTION_HEAD=$production_head" \
  'REEXTRACTED=14' \
  'LOCAL_MODEL_CALLS=1' \
  'EXTERNAL_MODEL_CALLS=0' \
  'TERMINAL_ROUTES=13' \
  'MANUAL_REVIEW_ROUTES=1' \
  'SUPERSESSIONS=14' \
  'CLAIMS_WRITTEN=0' \
  'QDRANT_WRITES=0' \
  'PROMPT_INFLUENCE=0' \
  'CROSS_OWNER_ISOLATION=true' \
  'PRODUCTION_UNCHANGED=true'

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a disposable production clone, proves source-
# envelope deduplication, runs five exact private GPU extractions with zero
# durable writes, and verifies production Postgres/Qdrant isolation.

if [[ ${EUID} -ne 0 ]]; then
  echo "run through sudo; root is required for the private endpoint key" >&2
  exit 2
fi

repo_root=$(
  cd "$(dirname "${BASH_SOURCE[0]}")/.."
  pwd
)
container=brains-postgres-1
production=memory
clone="memory_context_quality_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=673d64a3-c4ba-4d1c-89e3-e0c579022fad
test_selector=context_quality_clone_v1
backup=$(mktemp /tmp/memory-context-quality.XXXXXX.dump)
run_tag="$(
  date -u +%Y%m%dT%H%M%SZ
)-$(git -C "$repo_root" rev-parse --short=12 HEAD)"
review="/home/ubuntu/memory-v1-reviews/context-quality-${run_tag}"
before="${review}/production-before.tsv"
after="${review}/production-after.tsv"

targets=(
  "dog|03323af1-c5b1-509a-81be-31f996636cc5|6df993f62a9d749c100dc481fa661700cc48bfca50d63c2aba8cde1fe7b79082"
  "education|0522d532-6b88-5d00-9980-6ea24d0b1af4|9c6993e8aff11eae0b6d2235346055f33b72117c0f89d6e256f2ca8633235912"
  "caregiving|2919855e-cf22-5095-93b4-a881e93b54c0|e669f3377fb0f3936199de063d04375deaa519202b69c20de863f68e6ef9956d"
  "loss|3e59b50f-8e5f-5e65-a487-72f5d482a19c|e56a7f11175bf0c0db2335d93581fbbc77c86402a341e51baf112e95743de7ec"
  "occupation|5480aacd-e5f3-5060-8c6a-95ef0e46c9fe|8e4cfbd41156ccc92fd4392372a220d3e5802597c6d5756f8d48748503d0166b"
)

mkdir -p "$review"
chmod 0700 "$review"
touch "$before" "$after"
chmod 0600 "$before" "$after" "$backup"

cleanup() {
  rc=$?
  trap - EXIT
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  exit "$rc"
}
trap cleanup EXIT

production_state() {
  docker exec "$container" psql -U sage -d "$production" -X -Atqc "
    SELECT 'claim',count(*) FROM memory.claim
    UNION ALL
    SELECT 'entity',count(*) FROM memory.entity
    UNION ALL
    SELECT 'evidence_extraction_event',count(*)
      FROM memory.evidence_extraction_event
    UNION ALL
    SELECT 'evidence_extraction_job',count(*)
      FROM memory.evidence_extraction_job
    UNION ALL
    SELECT 'evidence_extraction_packet_v5_local',count(*)
      FROM memory.evidence_extraction_packet_v5_local
    UNION ALL
    SELECT 'observation',count(*) FROM memory.observation
    UNION ALL
    SELECT 'v5_local_inference_event',count(*)
      FROM memory.v5_local_inference_event
    UNION ALL
    SELECT 'vantage_answer_trace',count(*)
      FROM public.vantage_answer_trace
    ORDER BY 1"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H "content-type: application/json" \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll |
    jq -cS '.result.points | sort_by(.id | tostring)' |
    sha256sum |
    awk '{print $1}'
}

run_plan() {
  local output=$1
  POSTGRES_DSN="$clone_dsn" \
  PYTHONPATH="$repo_root" \
    /opt/chat-memory/venv/bin/python \
    scripts/memory_v1_v5_local_inference_scheduler.py \
    --contract-profile v5_2 \
    --selector-version "$test_selector" \
    --max-attempts 2 \
    --owner-user-id "$owner" >"$output"
}

cd "$repo_root"
set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ -r /etc/memory-v1-local-inference/api-key ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]

production_state >"$before"
qdrant_before=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"
docker exec "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 -c \
  'GRANT USAGE ON SCHEMA memory TO brains_app;
   GRANT SELECT ON
     memory.evidence,
     memory.evidence_extraction_job,
     public.chat_log
     TO brains_app;
   GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app' \
  >/dev/null

clone_dsn=$(
  /opt/chat-memory/venv/bin/python -c \
    'import sys; from urllib.parse import urlsplit,urlunsplit; p=urlsplit(sys.argv[1]); print(urlunsplit((p.scheme,p.netloc,"/"+sys.argv[2],p.query,p.fragment)))' \
    "$POSTGRES_DSN" "$clone"
)

# Clone-only scheduler fixture: two jobs for one exact source envelope.
docker exec "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 \
  -v owner="$owner" \
  -v evidence="03323af1-c5b1-509a-81be-31f996636cc5" \
  -v selector="$test_selector" <<'SQL' >/dev/null
SELECT set_config('app.user_id', :'owner', false);
WITH target AS (
  SELECT metadata
  FROM memory.evidence
  WHERE owner_user_id=:'owner'::uuid
    AND evidence_id=:'evidence'::uuid
),
candidate AS (
  SELECT job.job_id
  FROM memory.evidence_extraction_job AS job
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=job.owner_user_id
   AND evidence.evidence_id=job.evidence_id
  CROSS JOIN target
  WHERE job.owner_user_id=:'owner'::uuid
    AND evidence.metadata->>'source_id'=target.metadata->>'source_id'
    AND evidence.metadata->>'source_content_sha256'
        =target.metadata->>'source_content_sha256'
    AND evidence.metadata->>'source_char_start'
        =target.metadata->>'source_char_start'
    AND evidence.metadata->>'source_char_end'
        =target.metadata->>'source_char_end'
  ORDER BY job.created_at DESC,job.job_id
  LIMIT 2
)
UPDATE memory.evidence_extraction_job AS job
SET status='pending',
    attempts=0,
    selector_version=:'selector',
    available_at=clock_timestamp()
WHERE job.job_id IN (SELECT job_id FROM candidate);
SQL

plan_pending="${review}/dedupe-pending.json"
plan_terminal="${review}/dedupe-terminal.json"
run_plan "$plan_pending"
jq -e '
  .plans[0].raw_context_ready_count==2
  and .plans[0].context_ready_count==1
  and .plans[0].context_duplicate_count==1
  and .plans[0].context_superseded_count==0
  and .local_model_calls==0
  and .external_model_calls==0
' "$plan_pending" >/dev/null

docker exec "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 \
  -v owner="$owner" \
  -v selector="$test_selector" <<'SQL' >/dev/null
SELECT set_config('app.user_id', :'owner', false);
WITH newest AS (
  SELECT job_id
  FROM memory.evidence_extraction_job
  WHERE owner_user_id=:'owner'::uuid
    AND selector_version=:'selector'
  ORDER BY created_at DESC,job_id
  LIMIT 1
)
UPDATE memory.evidence_extraction_job
SET status='review_required'
WHERE job_id IN (SELECT job_id FROM newest);
SQL
run_plan "$plan_terminal"
jq -e '
  .plans[0].raw_context_ready_count==1
  and .plans[0].context_ready_count==0
  and .plans[0].context_duplicate_count==0
  and .plans[0].context_superseded_count==1
  and .local_model_calls==0
  and .external_model_calls==0
' "$plan_terminal" >/dev/null

for target in "${targets[@]}"; do
  IFS='|' read -r label evidence content_sha <<<"$target"
  packet="${review}/${label}-packet.json"
  report="${review}/${label}-report.json"
  MEMORY_V1_EVIDENCE_CONTEXT_CANARY=memory_v1_evidence_context_zero_write_canary_v1 \
  MEMORY_V1_LOCAL_INFERENCE_API_KEY="$(
    </etc/memory-v1-local-inference/api-key
  )" \
  POSTGRES_DSN="$clone_dsn" \
  PYTHONPATH="$repo_root" \
    /opt/chat-memory/venv/bin/python \
    tools/memory_v1_evidence_context_local_canary_v1.py \
    --owner-user-id "$owner" \
    --target-evidence-id "$evidence" \
    --expected-target-content-sha256 "$content_sha" \
    --packet-output "$packet" >"$report"
  chmod 0600 "$packet" "$report"
  jq -e '
    .outcome=="accepted"
    and .local_model_calls==1
    and .external_model_calls==0
    and .write_counts=={
      "database":0,
      "prompt_influence":0,
      "qdrant":0,
      "retrieval":0
    }
  ' "$report" >/dev/null
done

jq -e '
  ([.observations[].predicate] | sort)==[
    "health.user_reported_observation",
    "life_event.died",
    "pet.breed",
    "pet.sex",
    "relationship.has_pet"
  ]
  and (
    [.observations[]
      | select(.predicate=="relationship.has_pet")
      | .temporal.reason_codes[]]
    | index("historical_relationship_ended_before_source")
  )!=null
  and (
    [.observations[]
      | select(.predicate=="health.user_reported_observation")
      | .temporal.reason_codes[]]
    | index("historical_relationship_ended_before_source")
  )!=null
  and (
    .observations[]
    | select(.predicate=="life_event.died")
    | .temporal.semantic=="occurrence"
      and .temporal.source_form=="none"
  )
' "${review}/dog-packet.json" >/dev/null

jq -e '
  ([.observations[].predicate] | sort)==["education.attended"]
  and (
    .observations[0] as $observation
    | [.entity_mentions[]
       | select(
           .entity_ref==$observation.object.entity_ref
           and .entity_type=="organization"
           and (.name_text | ascii_downcase)
             =="forest institute of professional psychology"
         )]
      | length
  )==1
' "${review}/education-packet.json" >/dev/null

jq -e '
  (.observations | length)==0
  and ([.deferrals[].reason_code] | unique)==["context_missing"]
' "${review}/caregiving-packet.json" >/dev/null

jq -e '
  ([.observations[].predicate] | sort)==["relationship.has_pet"]
  and (
    [.observations[0].temporal.reason_codes[]]
    | index("historical_relationship_ended_before_source")
  )!=null
' "${review}/loss-packet.json" >/dev/null

jq -e '
  ([.observations[].predicate] | sort)==["occupation.works_as"]
  and (
    .observations[0] as $observation
    | [.entity_mentions[]
       | select(
           .entity_ref==$observation.subject_entity_ref
           and .entity_type=="person"
           and .name_text=="Bob Fry"
         )]
      | length
  )==1
' "${review}/occupation-packet.json" >/dev/null

cross_owner_report="${review}/cross-owner.json"
set +e
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python \
  tools/memory_v1_evidence_context_window_probe.py \
  --owner-user-id "$other_owner" \
  --target-evidence-id "03323af1-c5b1-509a-81be-31f996636cc5" \
  --expected-target-content-sha256 \
    "6df993f62a9d749c100dc481fa661700cc48bfca50d63c2aba8cde1fe7b79082" \
  >"$cross_owner_report"
cross_owner_rc=$?
set -e
[[ "$cross_owner_rc" == 1 ]]
jq -e '
  .outcome=="rejected"
  and .write_counts=={
    "database":0,
    "prompt_influence":0,
    "qdrant":0,
    "retrieval":0
  }
' "$cross_owner_report" >/dev/null

production_state >"$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(systemctl is-active memory-v1-v5-local-inference-tunnel.service)" == active ]]

jq -n \
  --arg review "$review" \
  '{
    outcome:"passed",
    unique_sources:5,
    local_model_calls:5,
    external_model_calls:0,
    scheduler_deduplication:"passed",
    semantic_guards:"passed",
    account_isolation:"passed",
    production_database:"unchanged",
    qdrant:"unchanged",
    retrieval_activation:false,
    prompt_influence:false,
    review:$review
  }'

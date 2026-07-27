#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a disposable production database clone, builds a
# bounded context window, runs one private GPU extraction, and proves that the
# production database and Qdrant did not change.

if [[ ${EUID} -ne 0 ]]; then
  echo "run through sudo; root is required for the private endpoint key" >&2
  exit 2
fi
if [[ $# -ne 3 ]]; then
  echo "usage: $0 OWNER_UUID TARGET_EVIDENCE_UUID TARGET_CONTENT_SHA256" >&2
  exit 2
fi

owner=$1
target=$2
target_sha=$3
repo_root=$(
  cd "$(dirname "${BASH_SOURCE[0]}")/.."
  pwd
)
container=brains-postgres-1
production=memory
clone="memory_context_window_${$}"
backup=$(mktemp /tmp/memory-context-window.XXXXXX.dump)
run_tag="$(
  date -u +%Y%m%dT%H%M%SZ
)-$(git -C "$repo_root" rev-parse --short=12 HEAD)"
review="/home/ubuntu/memory-v1-reviews/context-window-clone-${run_tag}"
report="${review}/report.json"
packet="${review}/packet.json"
cross_owner_report="${review}/cross-owner.json"
before="${review}/production-before.tsv"
after="${review}/production-after.tsv"
other_owner=557ea042-cb82-48f8-9429-472e96c957ef

mkdir -p "$review"
chmod 0700 "$review"
touch "$report" "$cross_owner_report" "$before" "$after"
chmod 0600 "$report" "$cross_owner_report" "$before" "$after" "$backup"

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
    SELECT 'evidence_extraction_job',count(*),max(updated_at)
    FROM memory.evidence_extraction_job
    UNION ALL
    SELECT 'v5_local_inference_event',count(*),max(created_at)
    FROM memory.v5_local_inference_event
    UNION ALL
    SELECT 'evidence_extraction_packet_v5_local',count(*),max(created_at)
    FROM memory.evidence_extraction_packet_v5_local
    UNION ALL
    SELECT 'claim',count(*),max(created_at)
    FROM memory.claim
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

cd "$repo_root"
set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ -r /etc/memory-v1-local-inference/api-key ]]
[[ "$(systemctl is-active brains.service)" == active ]]
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
   GRANT SELECT ON memory.evidence,public.chat_log TO brains_app;
   GRANT EXECUTE ON FUNCTION memory.current_actor_user_id() TO brains_app' \
  >/dev/null

clone_dsn=$(
  /opt/chat-memory/venv/bin/python -c \
    'import sys; from urllib.parse import urlsplit,urlunsplit; p=urlsplit(sys.argv[1]); print(urlunsplit((p.scheme,p.netloc,"/"+sys.argv[2],p.query,p.fragment)))' \
    "$POSTGRES_DSN" "$clone"
)

MEMORY_V1_EVIDENCE_CONTEXT_CANARY=memory_v1_evidence_context_zero_write_canary_v1 \
MEMORY_V1_LOCAL_INFERENCE_API_KEY="$(
  </etc/memory-v1-local-inference/api-key
)" \
POSTGRES_DSN="$clone_dsn" \
PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python \
  tools/memory_v1_evidence_context_local_canary_v1.py \
  --owner-user-id "$owner" \
  --target-evidence-id "$target" \
  --expected-target-content-sha256 "$target_sha" \
  --packet-output "$packet" >"$report"
chmod 0600 "$packet" "$report"

set +e
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python \
  tools/memory_v1_evidence_context_window_probe.py \
  --owner-user-id "$other_owner" \
  --target-evidence-id "$target" \
  --expected-target-content-sha256 "$target_sha" \
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
jq -e '
  .outcome=="accepted"
  and .local_model_calls==1
  and .external_model_calls==0
  and .entity_count==2
  and .observation_count==1
  and .deferral_count==0
  and .predicates==["occupation.works_as"]
  and .projection_classes==["direct_claim"]
  and .write_counts=={
    "database":0,
    "prompt_influence":0,
    "qdrant":0,
    "retrieval":0
  }
' "$report" >/dev/null
jq -e '
  (.observations | length)==1
  and (.entity_mentions | length)==2
  and (.deferrals | length)==0
  and .observations[0].predicate=="occupation.works_as"
  and .observations[0].object.kind=="entity"
  and (
    .observations[0] as $observation
    | (
        .entity_mentions
        | map(
            select(
              .entity_ref==$observation.subject_entity_ref
              and .entity_type=="person"
              and .name_text=="Bob Fry"
            )
          )
        | length
      )==1
    and (
        .entity_mentions
        | map(
            select(
              .entity_ref==$observation.object.entity_ref
              and .entity_type=="concept"
              and (.name_text | ascii_downcase)=="president"
            )
          )
        | length
      )==1
  )
' "$packet" >/dev/null

jq -c '{
  outcome,
  local_model_calls,
  external_model_calls,
  entity_count,
  observation_count,
  deferral_count,
  predicates,
  projection_classes,
  context_envelope_sha256,
  packet_sha256,
  write_counts
}' "$report"
printf '%s\n' \
  "review=${review}" \
  "production_database=UNCHANGED" \
  "qdrant=UNCHANGED" \
  "account_isolation=PASS" \
  "retrieval_activation=OFF" \
  "prompt_influence=OFF"

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Builds the read-only V1 evidence-context envelope from
# an exact production clone and proves owner isolation and production stasis.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
production=memory
clone="memory_evidence_context_v1_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
source_id=ed91f3b9-a4b8-4e63-aac7-53e370412483
target=049205b4-9a6c-5e1a-bb8f-2ab9f05f8964
target_sha=579427858fe9e181b02fc4c622a56cdda4f75e86356baf91664504a16396899d
backup=$(mktemp /tmp/memory-evidence-context-v1.XXXXXX.dump)
source_json=$(mktemp /tmp/memory-evidence-context-source.XXXXXX.json)
evidence_json=$(mktemp /tmp/memory-evidence-context-spans.XXXXXX.json)
input_json=$(mktemp /tmp/memory-evidence-context-input.XXXXXX.json)
report_json=$(mktemp /tmp/memory-evidence-context-report.XXXXXX.json)
chmod 0600 \
  "$backup" "$source_json" "$evidence_json" "$input_json" "$report_json"

cleanup() {
  rc=$?
  trap - EXIT
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f \
    "$backup" "$source_json" "$evidence_json" "$input_json" "$report_json"
  exit "$rc"
}
trap cleanup EXIT

scalar() {
  local database=$1
  local query=$2
  docker exec "$container" \
    psql -U sage -d "$database" -X -Atqc "$query"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

production_before=$(scalar "$production" "
  SELECT encode(public.digest(convert_to(
    jsonb_build_object(
      'source',(
        SELECT to_jsonb(value)
        FROM (
          SELECT id,owner_user_id,thread_id,request_id,created_at,
                 encode(public.digest(convert_to(text,'UTF8'),'sha256'),'hex')
                   AS source_content_sha256
          FROM public.chat_log
          WHERE id='$source_id'::uuid
        ) AS value
      ),
      'evidence',(
        SELECT coalesce(jsonb_agg(to_jsonb(value)
          ORDER BY value.evidence_id),'[]'::jsonb)
        FROM (
          SELECT evidence_id,owner_user_id,content_sha256,status,metadata
          FROM memory.evidence
          WHERE metadata->>'source_id'='$source_id'
        ) AS value
      )
    )::text,
    'UTF8'
  ),'sha256'),'hex')")
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
   GRANT SELECT ON memory.evidence,public.chat_log TO brains_app' \
  >/dev/null

docker exec -i "$container" \
  psql -U sage -d "$clone" -X -qAt -v ON_ERROR_STOP=1 \
  >"$source_json" <<SQL
BEGIN;
SET LOCAL app.user_id='$owner';
SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT jsonb_build_object(
  'id',id,
  'owner_user_id',owner_user_id,
  'thread_id',thread_id,
  'request_id',request_id,
  'created_at',created_at,
  'text',text
)::text
FROM public.chat_log
WHERE id='$source_id'::uuid
  AND owner_user_id='$owner'::uuid;
ROLLBACK;
SQL

docker exec -i "$container" \
  psql -U sage -d "$clone" -X -qAt -v ON_ERROR_STOP=1 \
  >"$evidence_json" <<SQL
BEGIN;
SET LOCAL app.user_id='$owner';
SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT coalesce(
  jsonb_agg(
    jsonb_build_object(
      'evidence_id',evidence_id,
      'owner_user_id',owner_user_id,
      'source_system',source_system,
      'content',content,
      'content_sha256',content_sha256,
      'metadata',metadata
    )
    ORDER BY (metadata->>'source_char_start')::integer,evidence_id
  ),
  '[]'::jsonb
)::text
FROM memory.evidence
WHERE owner_user_id='$owner'::uuid
  AND status='active'
  AND source_system='public.chat_log'
  AND metadata->>'source_id'='$source_id';
ROLLBACK;
SQL

[[ "$(jq -s length "$source_json")" == 1 ]]
[[ "$(jq 'length' "$evidence_json")" == 3 ]]
jq -n \
  --slurpfile source "$source_json" \
  --slurpfile evidence "$evidence_json" \
  '{source:$source[0],evidence:$evidence[0]}' >"$input_json"

cross_owner_visible=$(docker exec -i "$container" \
  psql -U sage -d "$clone" -X -qAt -v ON_ERROR_STOP=1 <<SQL
BEGIN;
SET LOCAL app.user_id='$other';
SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT (
  (SELECT count(*) FROM public.chat_log
   WHERE id='$source_id'::uuid)
  +
  (SELECT count(*) FROM memory.evidence
   WHERE evidence_id='$target'::uuid)
)::integer;
ROLLBACK;
SQL
)
[[ "$cross_owner_visible" == 0 ]]

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  tests/test_memory_v1_evidence_context_v1.py
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python \
  tools/memory_v1_evidence_context_v1_report.py \
  --input "$input_json" \
  --expected-owner-user-id "$owner" \
  --target-evidence-id "$target" \
  --expected-target-content-sha256 "$target_sha" \
  >"$report_json"

jq -e \
  --arg target_sha "$target_sha" \
  '
  .contract_version=="memory_evidence_context_envelope_v1"
  and .context_policy=="target_assertions_sibling_disambiguation_only_v1"
  and .target_content_sha256==$target_sha
  and .source_content_sha256
      =="28eb53cd7701ecae6c9d6286a1008fc5a4f1cff7940d0347483d22f3d97729a1"
  and .span_count==3
  and .context_only_span_count==2
  and .assertion_origin_count==1
  and .target_offsets=={"char_start":239,"char_end":293}
  and .ordered_context_roles==["before","before","target"]
  and .lane_counts=={
    "contextual_project":1,
    "technical_project":1,
    "user_viewpoint":1
  }
  and .raw_source_text_retained==false
  and .retrieval_activation==false
  and .prompt_influence==false
  ' "$report_json" >/dev/null

production_after=$(scalar "$production" "
  SELECT encode(public.digest(convert_to(
    jsonb_build_object(
      'source',(
        SELECT to_jsonb(value)
        FROM (
          SELECT id,owner_user_id,thread_id,request_id,created_at,
                 encode(public.digest(convert_to(text,'UTF8'),'sha256'),'hex')
                   AS source_content_sha256
          FROM public.chat_log
          WHERE id='$source_id'::uuid
        ) AS value
      ),
      'evidence',(
        SELECT coalesce(jsonb_agg(to_jsonb(value)
          ORDER BY value.evidence_id),'[]'::jsonb)
        FROM (
          SELECT evidence_id,owner_user_id,content_sha256,status,metadata
          FROM memory.evidence
          WHERE metadata->>'source_id'='$source_id'
        ) AS value
      )
    )::text,
    'UTF8'
  ),'sha256'),'hex')")
qdrant_after=$(qdrant_signature)

[[ "$production_before" == "$production_after" ]]
[[ "$qdrant_before" == "$qdrant_after" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ -z "$(git status --short --untracked-files=no)" ]]

printf '%s\n' \
  'memory_v1_evidence_context_v1_clone: PASS' \
  'owner_isolation=PASS' \
  'target_assertion_origins=1' \
  'sibling_context_only=2' \
  'production_rows=UNCHANGED' \
  'qdrant=UNCHANGED' \
  'retrieval_activation=OFF' \
  'prompt_influence=OFF'

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Routes the exact three remaining context-generation
# packets in a disposable production clone using already-deployed restricted
# finalizers. Production Postgres and Qdrant remain read-only.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
production=memory
clone="memory_context_exact_three_$(date -u +%Y%m%dT%H%M%SZ)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=673d64a3-c4ba-4d1c-89e3-e0c579022fad
selector=20260729_v5_context_generation_retry_v1
terminal_worker=scripts/memory_v1_v5_2_exact_terminal_batch.py
decision_builder=scripts/memory_v1_v5_build_local_review_deferral.py
decision_worker=scripts/memory_v1_v5_local_review_deferral.py
backup=$(mktemp /tmp/memory-context-exact-three.XXXXXX.dump)
work=$(mktemp -d /tmp/memory-context-exact-three.XXXXXX)
review_root="$work/reviews"
mkdir -m 0700 "$review_root"
chmod 0600 "$backup"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  rm -rf "$work"
}
trap cleanup EXIT

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$1" -c "$2" | tr -d '[:space:]'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

protected_snapshot() {
  local database=$1
  local output=$2
  docker exec -i "$container" psql -X -U sage -d "$database" \
    -Atq -v ON_ERROR_STOP=1 >"$output" <<'SQL'
CREATE TEMP TABLE protected_state(
  table_name text PRIMARY KEY,
  row_count bigint NOT NULL,
  content_sha256 text NOT NULL
);
DO $snapshot$
DECLARE
  item record;
  item_count bigint;
  item_sha text;
BEGIN
  FOR item IN
    SELECT namespace.nspname AS schema_name,class.relname AS table_name
    FROM pg_class AS class
    JOIN pg_namespace AS namespace ON namespace.oid=class.relnamespace
    WHERE namespace.nspname='memory'
      AND class.relkind IN ('r','p')
      AND class.relname NOT IN (
        'v5_2_local_packet_route_event',
        'v5_local_packet_disposition'
      )
    ORDER BY class.relname
  LOOP
    EXECUTE format(
      'SELECT count(*) FROM %I.%I',
      item.schema_name,item.table_name
    ) INTO item_count;
    EXECUTE format(
      $format$
      SELECT encode(
        public.digest(
          convert_to(
            coalesce(
              jsonb_agg(to_jsonb(value) ORDER BY to_jsonb(value)::text)::text,
              '[]'
            ),
            'UTF8'
          ),
          'sha256'
        ),
        'hex'
      )
      FROM %I.%I AS value
      $format$,
      item.schema_name,item.table_name
    ) INTO item_sha;
    INSERT INTO protected_state VALUES (
      item.table_name,item_count,item_sha
    );
  END LOOP;
END
$snapshot$;
SELECT table_name||E'\t'||row_count||E'\t'||content_sha256
FROM protected_state
ORDER BY table_name;
SQL
}

for file in "$terminal_worker" "$decision_builder" "$decision_worker"; do
  [[ -f "$file" ]]
done
bash -n "$0"
git diff --check
[[ -z "$(git status --short)" ]]

production_head=$(git rev-parse HEAD)
printf 'PHASE=production_baseline\n'
production_qdrant_before=$(qdrant_signature)
protected_snapshot "$production" "$work/production-protected-before.tsv"
production_route_before=$(scalar "$production" \
  "SELECT count(*) FROM memory.v5_2_local_packet_route_event")
production_disposition_before=$(scalar "$production" \
  "SELECT count(*) FROM memory.v5_local_packet_disposition")

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc >"$backup"
[[ -s "$backup" ]]
printf 'PHASE=clone_restore\n'
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" <"$backup"

set -a
source /opt/chat-memory/.env
set +a
clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone" python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

value = urlsplit(os.environ["SOURCE_DSN"])
print(urlunsplit((
    value.scheme,
    value.netloc,
    "/" + os.environ["CLONE_DB"],
    value.query,
    value.fragment,
)))
PY
)

targets="$work/targets.tsv"
zero_items="$work/zero-items.tsv"
ambiguous="$work/ambiguous.tsv"
: >"$zero_items"
: >"$ambiguous"
printf 'PHASE=target_inventory\n'
docker exec -i "$container" psql -X -U sage -d "$clone" \
  -Atq -F $'\t' -v ON_ERROR_STOP=1 >"$targets" <<SQL
SET app.user_id='$owner';
WITH target AS (
  SELECT packet.packet_id,packet.packet_storage_sha256
  FROM memory.evidence_extraction_packet_v5_local AS packet
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=packet.owner_user_id
   AND job.job_id=packet.job_id
  WHERE packet.owner_user_id='$owner'::uuid
    AND job.selector_version='$selector'
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_2_local_packet_route_event AS route
      WHERE route.owner_user_id=packet.owner_user_id
        AND route.packet_id=packet.packet_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_packet_disposition AS disposition
      WHERE disposition.owner_user_id=packet.owner_user_id
        AND disposition.packet_id=packet.packet_id
    )
)
SELECT packet_id,packet_storage_sha256
FROM target
ORDER BY packet_id;
SQL
[[ "$(wc -l <"$targets" | tr -d '[:space:]')" == 3 ]]

printf 'PHASE=route_classification\n'
while IFS=$'\t' read -r packet_id storage_sha256; do
  row=$(psql "$clone_dsn" -X -Atq -F $'\t' -v ON_ERROR_STOP=1 <<SQL | tail -n 1
BEGIN READ ONLY;
SET LOCAL app.user_id='$owner';
SELECT packet_id,packet_storage_sha256,reason_code
FROM memory.plan_owner_v5_2_zero_atom_deferral_route_v1(
  '$packet_id'::uuid
);
ROLLBACK;
SQL
)
  if [[ -n "$row" ]]; then
    printf '%s\n' "$row" >>"$zero_items"
  else
    printf '%s\t%s\n' "$packet_id" "$storage_sha256" >>"$ambiguous"
  fi
done <"$targets"

printf 'CLASSIFIED_ZERO=%s CLASSIFIED_AMBIGUOUS=%s\n' \
  "$(wc -l <"$zero_items" | tr -d '[:space:]')" \
  "$(wc -l <"$ambiguous" | tr -d '[:space:]')"
if [[ -s "$zero_items" ]]; then
  cut -f3 "$zero_items" | sort | uniq -c |
    sed 's/^ *\\([0-9][0-9]*\\) /CLASSIFIED_REASON_COUNT=\\1 reason=/'
fi
[[ "$(wc -l <"$zero_items" | tr -d '[:space:]')" == 2 ]]
[[ "$(wc -l <"$ambiguous" | tr -d '[:space:]')" == 1 ]]
[[ "$(cut -f3 "$zero_items" | sort -u)" \
  == deferral_only_review_unresolved_v5_2 ]]

ambiguous_packet=$(cut -f1 "$ambiguous")
ambiguous_storage=$(cut -f2 "$ambiguous")
[[ "$(scalar "$clone" \
  "SELECT count(*)
   FROM memory.evidence_extraction_packet_v5_local AS packet
   WHERE packet.owner_user_id='$owner'::uuid
     AND packet.packet_id='$ambiguous_packet'::uuid
     AND packet.packet_storage_sha256='$ambiguous_storage'
     AND packet.entity_mention_count=0
     AND packet.observation_count=0
     AND packet.comparison_hint_count=0
     AND packet.deferral_count=1
     AND packet.manual_review_required
     AND packet.normalized_packet->'deferrals' @>
       '[{\"reason_code\":\"ambiguous_transcription\"}]'::jsonb")" == 1 ]]

protected_snapshot "$clone" "$work/clone-protected-before.tsv"
clone_route_before=$(scalar "$clone" \
  "SELECT count(*) FROM memory.v5_2_local_packet_route_event")
clone_disposition_before=$(scalar "$clone" \
  "SELECT count(*) FROM memory.v5_local_packet_disposition")

printf 'PHASE=dry_runs\n'
terminal_args=(--owner-user-id "$owner")
while IFS=$'\t' read -r packet_id storage_sha256 reason_code; do
  terminal_args+=(
    --disposition-item
    "$packet_id:$storage_sha256:${reason_code%_v5_2}"
  )
done <"$zero_items"

terminal_dry="$work/terminal-dry.json"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$terminal_worker" \
  "${terminal_args[@]}" >"$terminal_dry"
jq -e '
  .apply==false and .packet_count==2 and .terminal_route_count==0 and
  .disposition_count==2 and .database_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$terminal_dry" >/dev/null

decision="$review_root/ambiguous-decision.json"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$decision_builder" \
  --owner-user-id "$owner" \
  --packet-id "$ambiguous_packet" \
  --reviewer-ref owner_authorized_context_generation_review_20260729 \
  --output "$decision" \
  --review-root "$review_root" >"$work/decision-build.json"
chmod 0600 "$decision"
jq -e '
  .review_decision=="deferred" and
  .reason_code=="ambiguous_transcription" and
  .promotion_eligible==false and
  .source_prose_included==false and
  .database_writes==0
' "$work/decision-build.json" >/dev/null

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$decision_worker" \
  --owner-user-id "$owner" \
  --packet-id "$ambiguous_packet" \
  --decision "$decision" >"$work/decision-dry.json"
jq -e '
  .apply==false and .outcome=="eligible" and
  .reason_code=="ambiguous_transcription" and
  .promotion_eligible==false and
  .write_counts.dispositions==0 and
  .write_counts.staging==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .external_model_calls==0
' "$work/decision-dry.json" >/dev/null

printf 'PHASE=isolation\n'
while IFS=$'\t' read -r packet_id _storage _reason; do
  foreign_count=$(psql "$clone_dsn" -X -Atq -v ON_ERROR_STOP=1 <<SQL | tail -n 1
BEGIN READ ONLY;
SET LOCAL app.user_id='$other';
SELECT count(*)
FROM memory.plan_owner_v5_2_zero_atom_deferral_route_v1(
  '$packet_id'::uuid
);
ROLLBACK;
SQL
)
  [[ "$foreign_count" == 0 ]]
done <"$zero_items"

psql "$clone_dsn" -X -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v other_owner="$other" \
  -v packet_id="$ambiguous_packet" \
  -v packet_storage_sha256="$ambiguous_storage" \
  < tests/memory_v1_v5_2_ambiguous_review_deferral.sql >/dev/null

printf 'PHASE=apply\n'
terminal_apply="$work/terminal-apply.json"
MEMORY_V1_V5_2_EXACT_TERMINAL_BATCH_APPLY=memory_v1_v5_2_exact_terminal_batch_apply_v1 \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$terminal_worker" \
  "${terminal_args[@]}" --apply >"$terminal_apply"
jq -e '
  .apply==true and .packet_count==2 and .terminal_route_count==0 and
  .disposition_count==2 and .database_writes==2 and
  .transactional_apply_proved==true and
  .zero_write_replay_proved==true and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$terminal_apply" >/dev/null

decision_apply="$work/decision-apply.json"
MEMORY_V1_V5_2_REVIEW_DEFERRAL_APPLY=memory_v1_v5_2_local_review_deferral_apply_v1 \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$decision_worker" \
  --owner-user-id "$owner" \
  --packet-id "$ambiguous_packet" \
  --decision "$decision" \
  --apply >"$decision_apply"
jq -e '
  .apply==true and .outcome=="deferred" and
  .reason_code=="ambiguous_transcription" and
  .promotion_eligible==false and
  .write_counts.dispositions==1 and
  .zero_write_replay_proved==true and
  .write_counts.staging==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .external_model_calls==0
' "$decision_apply" >/dev/null

printf 'PHASE=postconditions\n'
[[ "$(scalar "$clone" \
  "SELECT count(*) FROM memory.v5_2_local_packet_route_event")" \
  == "$((clone_route_before + 2))" ]]
[[ "$(scalar "$clone" \
  "SELECT count(*) FROM memory.v5_local_packet_disposition")" \
  == "$((clone_disposition_before + 1))" ]]
[[ "$(scalar "$clone" \
  "SELECT count(*)
   FROM memory.v5_local_packet_disposition
   WHERE owner_user_id='$owner'::uuid
     AND packet_id='$ambiguous_packet'::uuid
     AND disposition='terminal_no_stage'
     AND review_decision='deferred'
     AND reason_code='ambiguous_transcription'
     AND NOT promotion_eligible")" == 1 ]]

protected_snapshot "$clone" "$work/clone-protected-after.tsv"
cmp "$work/clone-protected-before.tsv" "$work/clone-protected-after.tsv"

[[ "$(git rev-parse HEAD)" == "$production_head" ]]
[[ "$(scalar "$production" \
  "SELECT count(*) FROM memory.v5_2_local_packet_route_event")" \
  == "$production_route_before" ]]
[[ "$(scalar "$production" \
  "SELECT count(*) FROM memory.v5_local_packet_disposition")" \
  == "$production_disposition_before" ]]
protected_snapshot "$production" "$work/production-protected-after.tsv"
cmp "$work/production-protected-before.tsv" \
  "$work/production-protected-after.tsv"
[[ "$(qdrant_signature)" == "$production_qdrant_before" ]]

printf 'CLONE_GATE=PASS exact_packets=3 unresolved_terminal=2 ambiguous_deferred=1 replay_writes=0 cross_owner=0 protected_delta=0 qdrant_delta=0\n'

#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Deploys the already clone-tested operational wrapper,
# then append-only routes exactly three reviewed context-generation packets:
# two unresolved zero-atom packets and one ambiguous-transcript deferral.
# It makes no model calls and permits no staging, claims, projections, Qdrant
# writes, retrieval, or prompt influence.

live_repo=/opt/chat-memory
candidate_repo=$(git rev-parse --show-toplevel)
required_live_head=6cc11242cc8f216738488355881b665503ea0322
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=673d64a3-c4ba-4d1c-89e3-e0c579022fad
selector=20260729_v5_context_generation_retry_v1
container=brains-postgres-1
python_bin=/opt/chat-memory/venv/bin/python
terminal_worker=scripts/memory_v1_v5_2_exact_terminal_batch.py
decision_builder=scripts/memory_v1_v5_build_local_review_deferral.py
decision_worker=scripts/memory_v1_v5_local_review_deferral.py
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
snapshot_dir=/home/ubuntu/brains/snapshots/memory_context_exact_three_route_"$timestamp"
timer_state="$snapshot_dir/timers_before.tsv"
review_root="$snapshot_dir/reviews"
restored=0

restore_timers() {
  local unit enabled active
  if [[ ! -f "$timer_state" || "$restored" -eq 1 ]]; then
    return
  fi
  while IFS=$'\t' read -r unit enabled active; do
    [[ -n "$unit" ]] || continue
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit" 2>/dev/null || true)" == "$active" ]]
  done <"$timer_state"
  restored=1
}

cleanup() {
  local status=$?
  restore_timers || true
  exit "$status"
}
trap cleanup EXIT

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1" | tr -d '[:space:]'
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
  local output=$1
  docker exec -i "$container" psql -X -U sage -d memory \
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

[[ "$(id -u)" -eq 0 ]]
exec 9>/run/lock/memory-context-exact-three-route.lock
flock -n 9

target_head=$(git -C "$candidate_repo" rev-parse HEAD)
[[ "$(git -C "$live_repo" rev-parse HEAD)" == "$required_live_head" ]]
[[ -z "$(git -C "$live_repo" status --porcelain)" ]]
[[ -z "$(git -C "$candidate_repo" status --porcelain)" ]]
git -C "$candidate_repo" merge-base --is-ancestor \
  "$required_live_head" "$target_head"
git -C "$candidate_repo" diff --check "$required_live_head..$target_head"

mapfile -t changed_files < <(
  git -C "$candidate_repo" diff --name-only \
    "$required_live_head..$target_head" | sort
)
[[ "${#changed_files[@]}" -eq 2 ]]
[[ "${changed_files[0]}" == \
  tools/memory_v1_context_generation_exact_three_route_clone.sh ]]
[[ "${changed_files[1]}" == \
  tools/memory_v1_context_generation_exact_three_route_production.sh ]]

for file in "$terminal_worker" "$decision_builder" "$decision_worker"; do
  [[ -f "$candidate_repo/$file" ]]
done
[[ -f \
  "$candidate_repo/tests/memory_v1_v5_2_ambiguous_review_deferral.sql" ]]

install -d -o root -g root -m 0700 "$snapshot_dir"
install -d -o root -g root -m 0700 "$review_root"

while read -r unit enabled _rest; do
  [[ -n "$unit" ]] || continue
  active=$(systemctl is-active "$unit" 2>/dev/null || true)
  printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active"
done < <(
  systemctl list-unit-files --type=timer --no-legend 'memory-v1-*'
) >"$timer_state"
[[ "$(wc -l <"$timer_state" | tr -d '[:space:]')" -eq 17 ]]

while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
  systemctl stop "${unit%.timer}.service" 2>/dev/null || true
done <"$timer_state"

set -a
. "$live_repo/.env"
. /etc/verbalsage/brains.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
[[ -n "${VS_SERVICE_TOKEN:-}" ]]

[[ "$(systemctl is-active brains.service)" == active ]]
curl -fsS --max-time 10 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz |
  jq -e '.status=="ok"' >/dev/null

backup="$snapshot_dir/memory_before_exact_three_route.dump"
docker exec "$container" pg_dump -U sage -d memory -Fc >"$backup"
[[ -s "$backup" ]]
docker exec -i "$container" pg_restore -l <"$backup" \
  >"$snapshot_dir/memory_before_exact_three_route.catalog"
sha256sum "$backup" \
  >"$snapshot_dir/memory_before_exact_three_route.dump.sha256"
git -C "$candidate_repo" diff --name-status \
  "$required_live_head..$target_head" \
  >"$snapshot_dir/code_diff.name-status"
git -C "$candidate_repo" diff --stat \
  "$required_live_head..$target_head" \
  >"$snapshot_dir/code_diff.stat"
printf '%s\n' "$required_live_head" >"$snapshot_dir/rollback_commit.txt"
printf '%s\n' "$target_head" >"$snapshot_dir/target_commit.txt"

protected_snapshot "$snapshot_dir/protected_before.tsv"
qdrant_signature >"$snapshot_dir/qdrant_before.sha256"
route_before=$(scalar \
  "SELECT count(*) FROM memory.v5_2_local_packet_route_event")
disposition_before=$(scalar \
  "SELECT count(*) FROM memory.v5_local_packet_disposition")

git -C "$live_repo" merge --ff-only "$target_head"
[[ "$(git -C "$live_repo" rev-parse HEAD)" == "$target_head" ]]
[[ -z "$(git -C "$live_repo" status --porcelain)" ]]

targets="$snapshot_dir/targets.tsv"
zero_items="$snapshot_dir/zero-items.tsv"
ambiguous="$snapshot_dir/ambiguous.tsv"
: >"$zero_items"
: >"$ambiguous"

psql "$POSTGRES_DSN" -X -Atq -F $'\t' -v ON_ERROR_STOP=1 \
  >"$targets" <<SQL
BEGIN READ ONLY;
SET LOCAL app.user_id='$owner';
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
ROLLBACK;
SQL
[[ "$(wc -l <"$targets" | tr -d '[:space:]')" -eq 3 ]]

while IFS=$'\t' read -r packet_id storage_sha256; do
  row=$(
    psql "$POSTGRES_DSN" -X -Atq -F $'\t' -v ON_ERROR_STOP=1 <<SQL
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

[[ "$(wc -l <"$zero_items" | tr -d '[:space:]')" -eq 2 ]]
[[ "$(wc -l <"$ambiguous" | tr -d '[:space:]')" -eq 1 ]]
[[ "$(cut -f3 "$zero_items" | sort -u)" == \
  deferral_only_review_unresolved_v5_2 ]]

ambiguous_packet=$(cut -f1 "$ambiguous")
ambiguous_storage=$(cut -f2 "$ambiguous")
ambiguous_valid=$(
  psql "$POSTGRES_DSN" -X -Atq -v ON_ERROR_STOP=1 <<SQL
BEGIN READ ONLY;
SET LOCAL app.user_id='$owner';
SELECT count(*)
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
    '[{"reason_code":"ambiguous_transcription"}]'::jsonb;
ROLLBACK;
SQL
)
[[ "$ambiguous_valid" -eq 1 ]]

{
  while IFS=$'\t' read -r packet_id storage_sha256 reason_code; do
    printf '%s\t%s\tterminal_no_stage\t%s\n' \
      "$packet_id" "$storage_sha256" "$reason_code"
  done <"$zero_items"
  printf '%s\t%s\tterminal_no_stage\tambiguous_transcription\n' \
    "$ambiguous_packet" "$ambiguous_storage"
} | sort >"$snapshot_dir/route-plan.tsv"
[[ "$(wc -l <"$snapshot_dir/route-plan.tsv" |
  tr -d '[:space:]')" -eq 3 ]]
sha256sum "$snapshot_dir/route-plan.tsv" \
  >"$snapshot_dir/route-plan.tsv.sha256"

cd "$live_repo"
terminal_args=(--owner-user-id "$owner")
while IFS=$'\t' read -r packet_id storage_sha256 reason_code; do
  terminal_args+=(
    --disposition-item
    "$packet_id:$storage_sha256:${reason_code%_v5_2}"
  )
done <"$zero_items"

POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$live_repo" \
  "$python_bin" "$terminal_worker" \
  "${terminal_args[@]}" >"$snapshot_dir/terminal-dry.json"
jq -e '
  .apply==false and .packet_count==2 and .terminal_route_count==0 and
  .disposition_count==2 and .database_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$snapshot_dir/terminal-dry.json" >/dev/null

decision="$review_root/ambiguous-decision.json"
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$live_repo" \
  "$python_bin" "$decision_builder" \
  --owner-user-id "$owner" \
  --packet-id "$ambiguous_packet" \
  --reviewer-ref owner_authorized_context_generation_review_20260729 \
  --output "$decision" \
  --review-root "$review_root" >"$snapshot_dir/decision-build.json"
chmod 0600 "$decision"
jq -e '
  .review_decision=="deferred" and
  .reason_code=="ambiguous_transcription" and
  .promotion_eligible==false and
  .source_prose_included==false and
  .database_writes==0
' "$snapshot_dir/decision-build.json" >/dev/null

POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$live_repo" \
  "$python_bin" "$decision_worker" \
  --owner-user-id "$owner" \
  --packet-id "$ambiguous_packet" \
  --decision "$decision" >"$snapshot_dir/decision-dry.json"
jq -e '
  .apply==false and .outcome=="eligible" and
  .reason_code=="ambiguous_transcription" and
  .promotion_eligible==false and
  .write_counts.dispositions==0 and
  .write_counts.staging==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .external_model_calls==0
' "$snapshot_dir/decision-dry.json" >/dev/null

while IFS=$'\t' read -r packet_id _storage _reason; do
  foreign_count=$(
    psql "$POSTGRES_DSN" -X -Atq -v ON_ERROR_STOP=1 <<SQL
BEGIN READ ONLY;
SET LOCAL app.user_id='$other_owner';
SELECT count(*)
FROM memory.plan_owner_v5_2_zero_atom_deferral_route_v1(
  '$packet_id'::uuid
);
ROLLBACK;
SQL
  )
  [[ "$foreign_count" -eq 0 ]]
done <"$zero_items"

psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v other_owner="$other_owner" \
  -v packet_id="$ambiguous_packet" \
  -v packet_storage_sha256="$ambiguous_storage" \
  <tests/memory_v1_v5_2_ambiguous_review_deferral.sql \
  >"$snapshot_dir/ambiguous-rollback-test.txt"

MEMORY_V1_V5_2_EXACT_TERMINAL_BATCH_APPLY=memory_v1_v5_2_exact_terminal_batch_apply_v1 \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$live_repo" \
  "$python_bin" "$terminal_worker" \
  "${terminal_args[@]}" --apply >"$snapshot_dir/terminal-apply.json"
jq -e '
  .apply==true and .packet_count==2 and .terminal_route_count==0 and
  .disposition_count==2 and .database_writes==2 and
  .transactional_apply_proved==true and
  .zero_write_replay_proved==true and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$snapshot_dir/terminal-apply.json" >/dev/null

MEMORY_V1_V5_2_REVIEW_DEFERRAL_APPLY=memory_v1_v5_2_local_review_deferral_apply_v1 \
POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$live_repo" \
  "$python_bin" "$decision_worker" \
  --owner-user-id "$owner" \
  --packet-id "$ambiguous_packet" \
  --decision "$decision" \
  --apply >"$snapshot_dir/decision-apply.json"
jq -e '
  .apply==true and .outcome=="deferred" and
  .reason_code=="ambiguous_transcription" and
  .promotion_eligible==false and
  .write_counts.dispositions==1 and
  .zero_write_replay_proved==true and
  .write_counts.staging==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .external_model_calls==0
' "$snapshot_dir/decision-apply.json" >/dev/null

[[ "$(scalar \
  "SELECT count(*) FROM memory.v5_2_local_packet_route_event")" \
  -eq "$((route_before + 2))" ]]
[[ "$(scalar \
  "SELECT count(*) FROM memory.v5_local_packet_disposition")" \
  -eq "$((disposition_before + 1))" ]]
[[ "$(scalar \
  "SELECT count(*)
   FROM memory.v5_local_packet_disposition
   WHERE owner_user_id='$owner'::uuid
     AND packet_id='$ambiguous_packet'::uuid
     AND disposition='terminal_no_stage'
     AND review_decision='deferred'
     AND reason_code='ambiguous_transcription'
     AND NOT promotion_eligible")" -eq 1 ]]

selector_accounted=$(
  psql "$POSTGRES_DSN" -X -Atq -v ON_ERROR_STOP=1 <<SQL
BEGIN READ ONLY;
SET LOCAL app.user_id='$owner';
WITH packet AS (
  SELECT p.owner_user_id,p.packet_id
  FROM memory.evidence_extraction_packet_v5_local AS p
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id=p.owner_user_id
   AND job.job_id=p.job_id
  WHERE job.owner_user_id='$owner'::uuid
    AND job.selector_version='$selector'
),
accounted AS (
  SELECT route.owner_user_id,route.packet_id
  FROM memory.v5_2_local_packet_route_event AS route
  JOIN packet
    ON packet.owner_user_id=route.owner_user_id
   AND packet.packet_id=route.packet_id
  UNION
  SELECT disposition.owner_user_id,disposition.packet_id
  FROM memory.v5_local_packet_disposition AS disposition
  JOIN packet
    ON packet.owner_user_id=disposition.owner_user_id
   AND packet.packet_id=disposition.packet_id
)
SELECT count(*) FROM accounted;
ROLLBACK;
SQL
)
[[ "$selector_accounted" -eq 18 ]]

selector_unaccounted=$(
  psql "$POSTGRES_DSN" -X -Atq -v ON_ERROR_STOP=1 <<SQL
BEGIN READ ONLY;
SET LOCAL app.user_id='$owner';
SELECT count(*)
FROM memory.evidence_extraction_packet_v5_local AS packet
JOIN memory.evidence_extraction_job AS job
  ON job.owner_user_id=packet.owner_user_id
 AND job.job_id=packet.job_id
WHERE job.owner_user_id='$owner'::uuid
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
  );
ROLLBACK;
SQL
)
[[ "$selector_unaccounted" -eq 0 ]]

protected_snapshot "$snapshot_dir/protected_after.tsv"
cmp "$snapshot_dir/protected_before.tsv" \
  "$snapshot_dir/protected_after.tsv"
qdrant_signature >"$snapshot_dir/qdrant_after.sha256"
cmp "$snapshot_dir/qdrant_before.sha256" \
  "$snapshot_dir/qdrant_after.sha256"

restore_timers
[[ "$(systemctl is-active brains.service)" == active ]]
curl -fsS --max-time 10 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz |
  jq -e '.status=="ok"' >/dev/null

plan_sha=$(cut -d' ' -f1 "$snapshot_dir/route-plan.tsv.sha256")
backup_sha=$(
  cut -d' ' -f1 \
    "$snapshot_dir/memory_before_exact_three_route.dump.sha256"
)
jq -cn \
  --arg production_commit "$target_head" \
  --arg rollback_commit "$required_live_head" \
  --arg snapshot_dir "$snapshot_dir" \
  --arg plan_sha256 "$plan_sha" \
  --arg backup_sha256 "$backup_sha" \
  '{
    outcome:"pass",
    production_commit:$production_commit,
    rollback_commit:$rollback_commit,
    snapshot_dir:$snapshot_dir,
    plan_sha256:$plan_sha256,
    backup_sha256:$backup_sha256,
    exact_packets:3,
    unresolved_terminal:2,
    ambiguous_deferred:1,
    selector_packets_accounted:18,
    selector_packets_unaccounted:0,
    database_writes:3,
    replay_writes:0,
    local_model_calls:0,
    external_model_calls:0,
    staging_writes:0,
    claim_writes:0,
    qdrant_writes:0,
    prompt_influence:0,
    cross_owner_visible:0,
    protected_store_unchanged:true,
    qdrant_unchanged:true,
    timers_restored:true,
    service_health:"ok"
  }'

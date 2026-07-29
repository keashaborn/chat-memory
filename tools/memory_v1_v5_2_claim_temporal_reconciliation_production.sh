#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the generic temporal reconciliation path and
# revises exactly the existing Neko pet relationship from current to historical.
# It does not write Qdrant or change retrieval/prompt behavior.

if [[ "${MEMORY_V1_V5_2_CLAIM_TEMPORAL_PRODUCTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_CLAIM_TEMPORAL_PRODUCTION=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=b3fc960dede82c76300a35affef84d5f0ce532fc
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
claim=bd20dd0a-9fa0-4a21-8a93-e828c8044150
observation=bc8866ad-95e8-4413-832e-813f601eece6
death_claim=186335e4-ef19-43fb-ad05-f350c18461c1
review_request=aeb81d43-ec6f-4c4f-9a55-a5300a018394
apply_request=10f1d718-a8c6-447a-a15b-c8ec026e847f
reason_codes='["historical_relationship_reviewed","supported_terminal_life_event","bounded_temporal_upper_bound"]'
rationale='The governed historical pet observation and supported death claim supersede the current-tense rendering while preserving the relationship claim identity.'
reviewer_ref=memory_v1_v5_2_claim_temporal_reconciliation_20260729
review_root=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_claim_temporal.lock

migration=ops/sql/20260729_memory_v1_v5_2_claim_temporal_reconciliation.sql
rollback=ops/sql/20260729_memory_v1_v5_2_claim_temporal_reconciliation_rollback.sql
security_test=tests/memory_v1_v5_2_claim_temporal_reconciliation_security.sql
migration_sha=f61301211e1d23d35046001c7c30b44fadf0b9095405b0ebadd175ed8ebc750c
rollback_sha=11332a0c2df398b0714620500ed1df66e3e1d1878556e69fbeb5a75ee519df7b
security_sha=b8dfcff69591da4288ca4c5c920506186d94d0805f2389fcad1738f659056766

phase=initialization
timers_quiesced=0
brains_quiesced=0
brains_state_before=
artifact_dir=
status_file=
run_id=
timer_state=$(mktemp /tmp/memory-v1-claim-temporal-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-claim-temporal-tables.XXXXXX)

psql_row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

authenticated_health() {
  set -a
  source /opt/chat-memory/.env
  set +a
  [[ -n "${VS_SERVICE_TOKEN:-}" ]]
  for _attempt in $(seq 1 30); do
    if [[ "$(systemctl is-active brains.service)" == active ]] \
       && curl --fail --silent --max-time 5 \
          -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
          http://127.0.0.1:8088/healthz \
          | jq -e '.status=="ok"' >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

restore_runtime() {
  if [[ "$brains_quiesced" -eq 1 ]]; then
    if [[ "$brains_state_before" == active ]]; then
      sudo -n systemctl start brains.service
    else
      sudo -n systemctl stop brains.service
    fi
    [[ "$(systemctl is-active brains.service)" == "$brains_state_before" ]]
    brains_quiesced=0
  fi
  if [[ "$timers_quiesced" -eq 1 ]]; then
    while IFS=$'\t' read -r unit enabled active; do
      [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
      if [[ "$active" == active ]]; then
        sudo -n systemctl start "$unit"
      else
        sudo -n systemctl stop "$unit"
      fi
      [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
      [[ "$(systemctl is-active "$unit")" == "$active" ]]
    done <"$timer_state"
    timers_quiesced=0
  fi
}

record_exit() {
  code=$?
  if [[ "$code" -eq 0 && "$phase" != complete ]]; then
    code=1
  fi
  restore_runtime || code=1
  rm -f "$timer_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_id=%s\n' "$run_id"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$code"
}
trap record_exit EXIT

capture_partition() {
  local partition=$1 output=$2 table has_owner predicate state
  : >"$output"
  while IFS=$'\t' read -r table has_owner; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    if [[ "$has_owner" == t ]]; then
      if [[ "$partition" == target_protected ]]; then
        case "$table" in
          claim|claim_revision|claim_observation|\
          claim_temporal_reconciliation_review_v5_2|\
          claim_temporal_reconciliation_apply_v5_2)
            predicate="owner_user_id='$owner'::uuid AND claim_id<>'$claim'::uuid"
            ;;
          relational_operation_request)
            predicate="owner_user_id='$owner'::uuid AND request_id NOT IN ('$review_request'::uuid,'$apply_request'::uuid)"
            ;;
          *)
            predicate="owner_user_id='$owner'::uuid"
            ;;
        esac
      else
        predicate="owner_user_id IS DISTINCT FROM '$owner'::uuid"
      fi
    else
      predicate=true
    fi
    state=$(psql_row "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
        WHERE $predicate
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
head=$(git -C "$repo_root" rev-parse HEAD)
for path in "$migration" "$rollback" "$security_test"; do
  [[ -f "$repo_root/$path" ]]
done
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == "$rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$security_test" | awk '{print $1}')" == "$security_sha" ]]
authenticated_health

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_${head:0:12}"
artifact_dir="$review_root/claim-temporal-reconciliation-production-$run_id"
status_file="$snapshot_root/memory_v1_claim_temporal_${run_id}.status"
mkdir -m 0700 "$artifact_dir"

phase=capture_timer_state
: >"$timer_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
[[ -s "$timer_state" ]]
cp "$timer_state" "$artifact_dir/timer-state-before.tsv"
chmod 0600 "$artifact_dir/timer-state-before.tsv"

phase=quiesce_timers
while IFS=$'\t' read -r unit _enabled active; do
  [[ "$active" != active ]] || sudo -n systemctl stop "$unit"
done <"$timer_state"
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"

phase=quiesce_brains
brains_state_before=$(systemctl is-active brains.service)
if [[ "$brains_state_before" == active ]]; then
  sudo -n systemctl stop brains.service
fi
brains_quiesced=1
! systemctl is-active --quiet brains.service

phase=backup
backup_partial="$snapshot_root/.memory_pre_claim_temporal_${run_id}.dump.partial"
backup="$snapshot_root/memory_pre_claim_temporal_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=capture_global_baseline
qdrant_before=$(qdrant_signature)
old_revisions_before=$(psql_row "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(
    to_jsonb(r)::text,E'\\n' ORDER BY revision_number),''),
    'UTF8'),'sha256'),'hex')
  FROM memory.claim_revision r
  WHERE owner_user_id='$owner'::uuid
    AND claim_id='$claim'::uuid")
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim_revision
  WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid")" == 2 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim_observation
  WHERE owner_user_id='$owner'::uuid
    AND claim_id='$claim'::uuid
    AND observation_id='$observation'::uuid")" == 0 ]]

phase=install_schema
docker exec -i "$container" psql -X -q -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$migration"
docker exec -i "$container" psql -X -q -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$migration"
docker exec -i "$container" psql -X -q -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$repo_root/$security_test"
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim_temporal_reconciliation_review_v5_2")" == 0 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim_temporal_reconciliation_apply_v5_2")" == 0 ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

docker exec "$container" psql -X -A -F $'\t' -t -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    SELECT table_name, EXISTS (
      SELECT 1 FROM information_schema.columns AS column_row
      WHERE column_row.table_schema='memory'
        AND column_row.table_name=table_row.table_name
        AND column_row.column_name='owner_user_id'
    )
    FROM information_schema.tables AS table_row
    WHERE table_schema='memory' AND table_type='BASE TABLE'
    ORDER BY table_name
  " >"$table_list"
[[ -s "$table_list" ]]
target_before="$artifact_dir/target-protected-before.tsv"
target_after="$artifact_dir/target-protected-after.tsv"
non_target_before="$artifact_dir/non-target-before.tsv"
non_target_after="$artifact_dir/non-target-after.tsv"
capture_partition target_protected "$target_before"
capture_partition non_target "$non_target_before"

phase=transactional_apply
apply_log="$artifact_dir/apply.log"
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" \
  -v owner="$owner" -v other_owner="$other_owner" \
  -v claim="$claim" -v observation="$observation" \
  -v death_claim="$death_claim" \
  -v review_request="$review_request" -v apply_request="$apply_request" \
  -v reason_codes="$reason_codes" -v rationale="$rationale" \
  -v reviewer_ref="$reviewer_ref" >"$apply_log" 2>&1 <<'SQL'
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', true);
SELECT * FROM memory.preflight_claim_temporal_reconciliation_v5_2(
  :'claim'::uuid,:'observation'::uuid
) \gset pre_
SELECT 1 / ((:'pre_current_revision_number'::integer=2)::integer);
SELECT 1 / ((:'pre_corroborating_claim_id'=:'death_claim')::integer);
SELECT 1 / ((:'pre_desired_canonical_text'=
  'The user formerly had a pet named Neko.')::integer);

SELECT * FROM memory.review_claim_temporal_reconciliation_v5_2(
  :'review_request'::uuid,:'claim'::uuid,:'observation'::uuid,
  :'reason_codes'::jsonb,:'rationale','system',:'reviewer_ref',
  :'pre_authorization_manifest_sha256'
) \gset review_
SELECT 1 / ((:'review_outcome'='applied')::integer);
SELECT 1 / ((:'review_rows_written'::integer=2)::integer);

SELECT *
FROM memory.preflight_claim_temporal_reconciliation_apply_v5_2(
  :'claim'::uuid,:'review_review_id'::uuid
) \gset apply_pre_
SELECT * FROM memory.apply_claim_temporal_reconciliation_v5_2(
  :'apply_request'::uuid,:'claim'::uuid,:'review_review_id'::uuid,
  :'apply_pre_apply_manifest_sha256'
) \gset apply_
SELECT 1 / ((:'apply_outcome'='applied')::integer);
SELECT 1 / ((:'apply_rows_written'::integer=5)::integer);
SELECT 1 / ((:'apply_resulting_revision_number'::integer=3)::integer);

SELECT set_config('app.user_id', :'other_owner', true);
DO $isolation$
BEGIN
  BEGIN
    PERFORM *
    FROM memory.preflight_claim_temporal_reconciliation_v5_2(
      'bd20dd0a-9fa0-4a21-8a93-e828c8044150'::uuid,
      'bc8866ad-95e8-4413-832e-813f601eece6'::uuid
    );
    RAISE EXCEPTION 'cross-owner temporal reconciliation succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$isolation$;
RESET SESSION AUTHORIZATION;
COMMIT;
\echo REVIEW_ID=:'review_review_id'
\echo REVIEW_MANIFEST=:'pre_authorization_manifest_sha256'
\echo APPLY_MANIFEST=:'apply_pre_apply_manifest_sha256'
\echo APPLY_EVENT_ID=:'apply_event_id'
SQL
chmod 0600 "$apply_log"
review_id=$(sed -n 's/^REVIEW_ID=//p' "$apply_log" | tr -d "'")
review_manifest=$(sed -n 's/^REVIEW_MANIFEST=//p' "$apply_log" | tr -d "'")
apply_manifest=$(sed -n 's/^APPLY_MANIFEST=//p' "$apply_log" | tr -d "'")
apply_event=$(sed -n 's/^APPLY_EVENT_ID=//p' "$apply_log" | tr -d "'")
[[ "$review_id" =~ ^[0-9a-f-]{36}$ ]]
[[ "$apply_event" =~ ^[0-9a-f-]{36}$ ]]
[[ "$review_manifest" =~ ^[0-9a-f]{64}$ ]]
[[ "$apply_manifest" =~ ^[0-9a-f]{64}$ ]]

phase=zero_write_replay
replay_log="$artifact_dir/replay.log"
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" \
  -v owner="$owner" -v claim="$claim" -v observation="$observation" \
  -v review_request="$review_request" -v apply_request="$apply_request" \
  -v reason_codes="$reason_codes" -v rationale="$rationale" \
  -v reviewer_ref="$reviewer_ref" -v review_id="$review_id" \
  -v review_manifest="$review_manifest" -v apply_manifest="$apply_manifest" \
  >"$replay_log" 2>&1 <<'SQL'
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner', true);
SELECT * FROM memory.review_claim_temporal_reconciliation_v5_2(
  :'review_request'::uuid,:'claim'::uuid,:'observation'::uuid,
  :'reason_codes'::jsonb,:'rationale','system',:'reviewer_ref',
  :'review_manifest'
) \gset review_
SELECT 1 / ((:'review_outcome'='replayed')::integer);
SELECT 1 / ((:'review_rows_written'::integer=0)::integer);
SELECT 1 / ((:'review_review_id'=:'review_id')::integer);
SELECT * FROM memory.apply_claim_temporal_reconciliation_v5_2(
  :'apply_request'::uuid,:'claim'::uuid,:'review_id'::uuid,
  :'apply_manifest'
) \gset apply_
SELECT 1 / ((:'apply_outcome'='replayed')::integer);
SELECT 1 / ((:'apply_rows_written'::integer=0)::integer);
SELECT 1 / ((:'apply_resulting_revision_number'::integer=3)::integer);
RESET SESSION AUTHORIZATION;
COMMIT;
SQL
chmod 0600 "$replay_log"

phase=postflight
capture_partition target_protected "$target_after"
capture_partition non_target "$non_target_after"
cmp -s "$target_before" "$target_after"
cmp -s "$non_target_before" "$non_target_after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$owner'::uuid
    AND claim_id='$claim'::uuid
    AND status='supported'
    AND canonical_text='The user formerly had a pet named Neko.'
    AND valid_to='2026-07-28 04:04:34.272603+00'::timestamptz
    AND qualifiers->>'temporal_state'='historical'
    AND qualifiers->>'valid_to_semantics'='exclusive_upper_bound'")" == 1 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim_revision
  WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid")" == 3 ]]
old_revisions_after=$(psql_row "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(
    to_jsonb(r)::text,E'\\n' ORDER BY revision_number),''),
    'UTF8'),'sha256'),'hex')
  FROM memory.claim_revision r
  WHERE owner_user_id='$owner'::uuid
    AND claim_id='$claim'::uuid AND revision_number<=2")
[[ "$old_revisions_after" == "$old_revisions_before" ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim_observation
  WHERE owner_user_id='$owner'::uuid
    AND claim_id='$claim'::uuid
    AND observation_id='$observation'::uuid
    AND stance='supports'")" == 1 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim_temporal_reconciliation_review_v5_2
  WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid")" == 1 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim_temporal_reconciliation_apply_v5_2
  WHERE owner_user_id='$owner'::uuid AND claim_id='$claim'::uuid")" == 1 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.relational_operation_request
  WHERE owner_user_id='$owner'::uuid
    AND request_id IN ('$review_request'::uuid,'$apply_request'::uuid)")" == 2 ]]
phase=restore_runtime
restore_runtime
authenticated_health
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  [[ "$(systemctl is-active "$unit")" == "$active" ]]
done <"$timer_state"

phase=report
jq -n \
  --arg contract memory_v1_v5_2_claim_temporal_reconciliation_production_v1 \
  --arg production_head "$head" \
  --arg owner_user_id "$owner" \
  --arg claim_id "$claim" \
  --arg observation_id "$observation" \
  --arg corroborating_claim_id "$death_claim" \
  --arg canonical_text 'The user formerly had a pet named Neko.' \
  --arg review_id "$review_id" \
  --arg apply_event_id "$apply_event" \
  --arg migration_sha256 "$migration_sha" \
  --arg rollback_sha256 "$rollback_sha" \
  --arg backup "$backup" \
  --arg qdrant_sha256 "$qdrant_before" \
  '{
    contract_version:$contract,
    production_head:$production_head,
    owner_user_id:$owner_user_id,
    claim_id:$claim_id,
    observation_id:$observation_id,
    corroborating_claim_id:$corroborating_claim_id,
    resulting_revision_number:3,
    canonical_text:$canonical_text,
    review_id:$review_id,
    apply_event_id:$apply_event_id,
    inserted_rows:{
      review:1,
      apply:1,
      operation_requests:2,
      claim_observation:1,
      claim_revision:1
    },
    claim_rows_updated:1,
    old_revisions_preserved:true,
    zero_write_replay:true,
    cross_owner_rejected:true,
    non_target_records_unchanged:true,
    qdrant_unchanged:true,
    retrieval_activated:false,
    prompt_influence_activated:false,
    migration_sha256:$migration_sha256,
    rollback_sha256:$rollback_sha256,
    backup:$backup,
    qdrant_sha256:$qdrant_sha256,
    service_health:"ok",
    timers_restored:true
  }' >"$artifact_dir/production-report.json"
chmod 0600 "$artifact_dir/production-report.json"

phase=complete
printf 'ARTIFACT_DIR=%s\n' "$artifact_dir"
printf 'REPORT=%s\n' "$artifact_dir/production-report.json"
printf 'BACKUP=%s\n' "$backup"

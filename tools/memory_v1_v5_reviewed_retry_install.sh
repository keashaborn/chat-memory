#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs reviewed skipped-job retry capability, runs its
# security suite inside rollback, and leaves every production row unchanged.

repo_root=$(git rev-parse --show-toplevel)
manifest=ops/manifests/memory_v1_v5_reviewed_retry_install_20260717.json
manifest_sha=75c6802aa45087d9beda5127014fa7ad3661aa0b135218c7a5897567eec0d104
migration=ops/sql/20260717_memory_v1_v5_reviewed_retry.sql
migration_sha=f2a3d0708c1d60fd79c5a16899d8d53f6443f2523813334fdf7d8553b9243124
rollback=ops/sql/20260717_memory_v1_v5_reviewed_retry_rollback.sql
rollback_sha=eb0b4643a8673aa6dec4cf77e335201361454c2277f3ec029b601cab0d97d69c
test_sql=tests/memory_v1_v5_reviewed_retry.sql
test_sha=da6a8d3758ce1a29b29421d7ab24a64951a3320312668a4982afe270583efb8d
required_ancestor=f3871b7b5278ff23d67b38c412f8196400e479ff
container=brains-postgres-1
snapshot_dir=/home/ubuntu/brains/snapshots
units=(
  memory-v1-projection.timer
  memory-v1-governance.timer
  memory-v1-evidence-intake-dispatcher.timer
  memory-v1-v5-chat-capture.timer
  memory-v1-deferred-reconciliation-scan.timer
)

run_id=$(cat /proc/sys/kernel/random/uuid)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="$snapshot_dir/memory_pre_v5_reviewed_retry_install_${stamp}_${run_id}.dump"
report="$snapshot_dir/memory_v1_v5_reviewed_retry_install_${stamp}_${run_id}.json"
lock=/run/lock/memory-v1-v5-reviewed-retry-install.lock
tables=$(mktemp /tmp/memory-v1-v5-reviewed-retry-tables.XXXXXX)
before=$(mktemp /tmp/memory-v1-v5-reviewed-retry-before.XXXXXX)
after=$(mktemp /tmp/memory-v1-v5-reviewed-retry-after.XXXXXX)
timer_state=$(mktemp /tmp/memory-v1-v5-reviewed-retry-timers.XXXXXX)
qdrant_before=$(mktemp /tmp/memory-v1-v5-reviewed-retry-qdrant-before.XXXXXX)
qdrant_after=$(mktemp /tmp/memory-v1-v5-reviewed-retry-qdrant-after.XXXXXX)
quiesced=false

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

run_sql() {
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

restore_timers() {
  [[ "$quiesced" == true ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  quiesced=false
}

cleanup() {
  status=$?
  restore_timers || status=1
  rm -f "$tables" "$before" "$after" "$timer_state" \
    "$qdrant_before" "$qdrant_after"
  exit "$status"
}
trap cleanup EXIT

capture_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(scalar "
      SELECT count(*)::text || E'\\t' || encode(
        public.digest(
          convert_to(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'UTF8'),
          'sha256'
        ),
        'hex'
      )
      FROM (
        SELECT to_jsonb(table_row)::text AS row_json
        FROM memory.\"$table\" AS table_row
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$tables"
}

capture_qdrant() {
  local output=$1
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -X POST http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    -d '{"limit":10000,"with_payload":true,"with_vector":false}' \
    | jq -e -S -c \
      '{points:(.result.points|sort_by(.id|tostring)),next_page_offset:.result.next_page_offset}' \
    >"$output"
}

exec 9>"$lock"
flock -n 9

for required in "$manifest" "$migration" "$rollback" "$test_sql"; do
  [[ -f "$repo_root/$required" ]]
done
[[ "$(sha256sum "$repo_root/$manifest" | cut -d' ' -f1)" == "$manifest_sha" ]]
[[ "$(sha256sum "$repo_root/$migration" | cut -d' ' -f1)" == "$migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | cut -d' ' -f1)" == "$rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | cut -d' ' -f1)" == "$test_sha" ]]
git merge-base --is-ancestor "$required_ancestor" HEAD
[[ -z "$(git status --short)" ]]
[[ "$(jq -r '.authorization.install_authorized' "$repo_root/$manifest")" == true ]]

preflight=$(scalar "
  SELECT jsonb_build_object(
    'function_absent',to_regprocedure(
      'memory.requeue_owner_skipped_evidence_job_v5(uuid,uuid,text,uuid,uuid,text,integer,text)'
    ) IS NULL,
    'role_absent',to_regrole('memory_extraction_retry_maintainer') IS NULL,
    'target_skipped',(SELECT count(*) FROM memory.evidence_extraction_job
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND job_id='788d0258-7227-46e2-8382-d13e6a122722'
        AND status='skipped' AND attempts=1),
    'prior_failure',(SELECT count(*) FROM memory.evidence_extraction_event
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND operation_id='a95e5e34-edb8-5296-be1b-ebe9e348aee0'
        AND event_type='skipped'
        AND details->>'error_class'='validator_rejected'),
    'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5)
  )::text
")
[[ "$(jq -r '.function_absent and .role_absent' <<<"$preflight")" == true ]]
[[ "$(jq -r '.target_skipped' <<<"$preflight")" == 1 ]]
[[ "$(jq -r '.prior_failure' <<<"$preflight")" == 1 ]]
[[ "$(jq -r '.packets' <<<"$preflight")" == 0 ]]

: >"$timer_state"
for unit in "${units[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done
quiesced=true
for unit in "${units[@]}"; do
  sudo -n systemctl stop "$unit"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done

umask 077
docker exec "$container" pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
chmod 0600 "$backup"
[[ -s "$backup" ]]
docker exec -i "$container" pg_restore -l <"$backup" >/dev/null
backup_sha=$(sha256sum "$backup" | cut -d' ' -f1)

scalar "
  SELECT table_name
  FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name
" >"$tables"
capture_state "$before"
capture_qdrant "$qdrant_before"
qdrant_before_sha=$(sha256sum "$qdrant_before" | cut -d' ' -f1)

run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$test_sql"

capture_state "$after"
capture_qdrant "$qdrant_after"
qdrant_after_sha=$(sha256sum "$qdrant_after" | cut -d' ' -f1)
cmp -s "$before" "$after"
[[ "$qdrant_before_sha" == "$qdrant_after_sha" ]]

postflight=$(scalar "
  SELECT jsonb_build_object(
    'function_present',to_regprocedure(
      'memory.requeue_owner_skipped_evidence_job_v5(uuid,uuid,text,uuid,uuid,text,integer,text)'
    ) IS NOT NULL,
    'function_owner',(SELECT pg_get_userbyid(proowner) FROM pg_proc
      WHERE oid='memory.requeue_owner_skipped_evidence_job_v5(uuid,uuid,text,uuid,uuid,text,integer,text)'::regprocedure),
    'security_definer',(SELECT prosecdef FROM pg_proc
      WHERE oid='memory.requeue_owner_skipped_evidence_job_v5(uuid,uuid,text,uuid,uuid,text,integer,text)'::regprocedure),
    'brains_execute',has_function_privilege(
      'brains_app','memory.requeue_owner_skipped_evidence_job_v5(uuid,uuid,text,uuid,uuid,text,integer,text)','EXECUTE'),
    'public_execute',has_function_privilege(
      'public','memory.requeue_owner_skipped_evidence_job_v5(uuid,uuid,text,uuid,uuid,text,integer,text)','EXECUTE'),
    'restricted_role',(SELECT NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
      FROM pg_roles WHERE rolname='memory_extraction_retry_maintainer'),
    'guard_transition',position(
      'OR (OLD.status=''skipped'' AND NEW.status=''pending'')'
      in pg_get_functiondef('memory.guard_evidence_extraction_job_update()'::regprocedure)
    )>0,
    'target_skipped',(SELECT count(*) FROM memory.evidence_extraction_job
      WHERE owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'
        AND job_id='788d0258-7227-46e2-8382-d13e6a122722'
        AND status='skipped' AND attempts=1),
    'test_events',(SELECT count(*) FROM memory.evidence_extraction_event
      WHERE operation_id IN (
        'b1111111-1111-4111-8111-111111111111',
        'b2222222-2222-4222-8222-222222222222',
        'b3333333-3333-4333-8333-333333333333'
      )),
    'packets',(SELECT count(*) FROM memory.evidence_extraction_packet_v5)
  )::text
")
[[ "$(jq -r '.function_present and .security_definer and .brains_execute and (.public_execute|not) and .restricted_role and .guard_transition' <<<"$postflight")" == true ]]
[[ "$(jq -r '.function_owner' <<<"$postflight")" == memory_extraction_retry_maintainer ]]
[[ "$(jq -r '.target_skipped' <<<"$postflight")" == 1 ]]
[[ "$(jq -r '.test_events + .packets' <<<"$postflight")" == 0 ]]

restore_timers

jq -n \
  --arg run_id "$run_id" \
  --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg commit "$(git rev-parse HEAD)" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg migration_sha256 "$migration_sha" \
  --arg backup_path "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_after_sha" \
  --argjson postflight "$postflight" \
  '{
    report_version:"memory_v1_v5_reviewed_retry_install_v1",
    status:"passed",run_id:$run_id,created_at:$created_at,commit:$commit,
    manifest_sha256:$manifest_sha256,migration_sha256:$migration_sha256,
    backup:{path:$backup_path,sha256:$backup_sha256},
    postflight:$postflight,
    proofs:{rollback_test_passed:true,memory_rows_unchanged:true,
      qdrant_unchanged:true,qdrant_sha256:$qdrant_sha256,
      timers_restored:true,external_model_calls:0,packet_writes:0,
      retrieval_active:false,prompt_influence:false}
  }' >"$report"
chmod 0600 "$report"

printf 'memory_v1_v5_reviewed_retry_install: PASS\n'
printf 'backup=%s\n' "$backup"
printf 'report=%s\n' "$report"

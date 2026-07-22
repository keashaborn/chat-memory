#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Read-only recovery verification for the apply run whose
# final service probe used the obsolete port. Makes no durable database writes.

repo_root=$(git rev-parse --show-toplevel)
run_tag=${1:?usage: recovery_verify.sh RUN_TAG}
[[ "$run_tag" =~ ^[0-9]{8}T[0-9]{6}Z_[0-9a-f]{12}$ ]]
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
job=20ba21a9-855b-4606-93fb-28065e2e13d1
operation=eeb053e5-7737-46d8-8349-b5f6ce7909e9
expected_qdrant_sha=1b3e3b6b38ba90ee039b21bff10cf6a1cddb0e0ce63cc446b6305c584bf583b2
snapshot_dir=/home/ubuntu/brains/snapshots
status="$snapshot_dir/memory_v1_v5_local_incomplete_retry_${run_tag}.status"
before="$snapshot_dir/memory_v1_v5_local_incomplete_retry_protected_before_${run_tag}.tsv"
after="$snapshot_dir/memory_v1_v5_local_incomplete_retry_protected_after_${run_tag}.tsv"
apply_output="$snapshot_dir/memory_v1_v5_local_incomplete_retry_apply_${run_tag}.tsv"
replay_output="$snapshot_dir/memory_v1_v5_local_incomplete_retry_replay_${run_tag}.tsv"
backup="$snapshot_dir/memory_pre_v5_local_incomplete_retry_${run_tag}.dump"
container=brains-postgres-1

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for required in "$status" "$before" "$after" "$apply_output" \
  "$replay_output" "$backup" "$backup.catalog" "$backup.sha256"; do
  [[ -s "$required" ]]
done
[[ "$(awk -F= '$1=="phase"{print $2}' "$status")" == postflight ]]
[[ "$(awk -F= '$1=="exit_code"{print $2}' "$status")" == 7 ]]
cmp -s "$before" "$after"
[[ "$(grep -c '|pending|1|applied$' "$apply_output")" == 1 ]]
[[ "$(grep -c '|pending|1|replayed$' "$replay_output")" == 1 ]]
sha256sum -c "$backup.sha256" >/dev/null

[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid
    AND status='pending' AND attempts=1 AND last_error IS NULL
    AND lease_token IS NULL AND lease_expires_at IS NULL")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_event
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid
    AND operation_id='$operation'::uuid AND event_type='queued'
    AND from_status='skipped' AND to_status='pending'
    AND actor_ref='local_incomplete_retry'")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.v5_local_inference_event
  WHERE owner_user_id='$owner'::uuid AND job_id='$job'::uuid")" == 2 ]]
[[ "$(psql_scalar "SELECT (
  p.prosecdef AND p.proowner='memory_v5_local_inference_maintainer'::regrole
  AND p.proconfig=ARRAY['search_path=pg_catalog']::text[]
  AND has_function_privilege('brains_app',p.oid,'EXECUTE')
  AND NOT EXISTS (SELECT 1 FROM aclexplode(p.proacl) a
    WHERE a.grantee=0 AND a.privilege_type='EXECUTE')
)::integer FROM pg_proc p WHERE p.oid=
  'memory.requeue_owner_local_incomplete_failure_v1(uuid,uuid,text,uuid,uuid,integer,text)'::regprocedure")" == 1 ]]

set -a
source "$repo_root/.env"
set +a
psql "$POSTGRES_DSN" -X -q -v ON_ERROR_STOP=1 >/dev/null <<SQL
BEGIN;
SELECT set_config('app.user_id','00000000-0000-4000-8000-000000000123',true);
DO \$probe\$
BEGIN
  PERFORM * FROM memory.requeue_owner_local_incomplete_failure_v1(
    '00000000-0000-4000-8000-000000000124','$job',repeat('0',64),
    '00000000-0000-4000-8000-000000000125',
    '00000000-0000-4000-8000-000000000126',1,
    'bounded_output_ceiling_increase'
  );
  RAISE EXCEPTION 'cross-owner retry unexpectedly succeeded';
EXCEPTION WHEN check_violation THEN NULL;
END
\$probe\$;
ROLLBACK;
SQL

[[ "$(qdrant_signature)" == "$expected_qdrant_sha" ]]
curl --fail --silent --show-error --max-time 10 \
  -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
  http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
[[ "$(systemctl list-timers 'memory-v1-*.timer' --all --no-legend --no-pager \
  | awk 'NF{count++} END{print count+0}')" == 13 ]]

report="$snapshot_dir/memory_v1_v5_local_incomplete_retry_recovery_${run_tag}.json"
jq -n \
  --arg verified_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg prior_status "$status" --arg backup "$backup" \
  --arg qdrant_sha "$expected_qdrant_sha" \
  '{contract_version:"memory_v1_v5_local_incomplete_retry_recovery_report_v1",
    verified_at:$verified_at,head_commit:$head_commit,
    recovered_from:{status:$prior_status,failed_probe:"127.0.0.1:8000/health"},
    backup:$backup,qdrant_sha256:$qdrant_sha,
    checks:{database_apply_complete:true,exact_replay_zero_write:true,
      table_hashes_unchanged_except_target_job_and_audit:true,
      cross_owner_rejected:true,packets_unchanged:true,
      local_call_ledger_unchanged:true,qdrant_unchanged:true,
      correct_service_health_passed:true,timers_restored:true},
    hard_stop:"before_private_local_retry"}' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
printf 'memory_v1_v5_local_incomplete_retry_recovery_verify: PASS\n'
printf 'report=%s\n' "$report"

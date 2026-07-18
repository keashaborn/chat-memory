#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the additive, owner-scoped, append-only V5
# local-inference persistence surface. It runs only rollback-scoped security
# tests and stops before any model call, packet persistence, promotion,
# retrieval activation, prompt influence, or Qdrant write.

if [[ "${MEMORY_V1_V5_LOCAL_INFERENCE_INSTALL:-}" != "authorized" ]]; then
  echo 'MEMORY_V1_V5_LOCAL_INFERENCE_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
migration=ops/sql/20260718_memory_v1_v5_local_inference.sql
rollback=ops/sql/20260718_memory_v1_v5_local_inference_rollback.sql
test_sql=tests/memory_v1_v5_local_inference.sql
provider=scripts/memory_v1_relational_extraction_v5_local_provider.py
provider_test=scripts/memory_v1_relational_extraction_v5_local_provider_test.py
canary=scripts/memory_v1_v5_local_inference_canary.py
required_ancestor=82803717773245fcfe953d343fdd01cdee828f11
expected_migration_sha=3db6e453f3becf76bc84b28e9908c17cdc2d1a8327b0bc165e663f4652863ab5
expected_rollback_sha=42c9cda1830e81e1eb105014be6e3895b4fa98bebaecc56de93fa6d3d0dec0fa
expected_test_sha=cbd64ecaeb593e84ec4aacf150b5de4aba9e87f6819659ebe9425c81e75061d9
expected_provider_sha=5d0bd5b146986bf85a3dbf51e48234d5402c9fd4f917c6fff24dab500d48c484
expected_provider_test_sha=21b613a973dfa1a338b763b35c4543ba07e88e43cb40c6781c13c3c5ebc87187
expected_canary_sha=9abaacba470a23ce0ed874101eb70f44f433862d0e145d562c33bfff61af58d3
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_local_inference_install.lock
phase=initialization
run_id=
status_file=
units_quiesced=0
table_list=$(mktemp /tmp/memory-v1-v5-local-inference-tables.XXXXXX)
unit_state=$(mktemp /tmp/memory-v1-v5-local-inference-units.XXXXXX)
baseline=
post=

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

run_sql_file() {
  docker exec -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=180s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 -U sage -d "$database" \
    <"$repo_root/$1"
}

restore_timers() {
  [[ "$units_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$unit_state"
  units_quiesced=0
}

record_exit() {
  exit_code=$?
  restore_timers || exit_code=1
  rm -f "$table_list" "$unit_state"
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_id=%s\n' "$run_id"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_state() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' || encode(
        public.digest(
          convert_to(
            coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
            'UTF8'
          ),
          'sha256'
        ),
        'hex'
      )
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

for file in "$migration" "$rollback" "$test_sql" "$provider" \
  "$provider_test" "$canary"; do
  [[ -f "$repo_root/$file" ]]
done
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(sha256sum "$repo_root/$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
[[ "$(sha256sum "$repo_root/$rollback" | awk '{print $1}')" == "$expected_rollback_sha" ]]
[[ "$(sha256sum "$repo_root/$test_sql" | awk '{print $1}')" == "$expected_test_sha" ]]
[[ "$(sha256sum "$repo_root/$provider" | awk '{print $1}')" == "$expected_provider_sha" ]]
[[ "$(sha256sum "$repo_root/$provider_test" | awk '{print $1}')" == "$expected_provider_test_sha" ]]
[[ "$(sha256sum "$repo_root/$canary" | awk '{print $1}')" == "$expected_canary_sha" ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_local_inference_install_${run_id}.status"
baseline="$snapshot_dir/memory_v1_v5_local_inference_before_${run_id}.tsv"
post="$snapshot_dir/memory_v1_v5_local_inference_after_${run_id}.tsv"

phase=preflight
[[ "$(psql_scalar "SELECT (
  to_regclass('memory.v5_local_inference_event') IS NULL
  AND to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
  AND to_regprocedure(
    'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'
  ) IS NULL
  AND to_regprocedure(
    'memory.persist_owner_v5_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'
  ) IS NULL
  AND to_regprocedure(
    'memory.complete_owner_v5_local_inference_v1(uuid,uuid,uuid,uuid,text,integer,text,text,text,text)'
  ) IS NULL
  AND to_regprocedure(
    'memory.requeue_owner_local_transport_failure_v1(uuid,uuid,text,uuid,uuid,integer,text,text)'
  ) IS NULL
)::integer")" == 1 ]]

: >"$unit_state"
while IFS= read -r unit; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$unit_state"
done < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
[[ -s "$unit_state" ]]
units_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$unit_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
  [[ "$(systemctl is-active "$unit")" == inactive ]]
done <"$unit_state"

phase=backup
backup_partial="$snapshot_dir/.memory_pre_v5_local_inference_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_local_inference_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline_capture
psql_scalar "
  SELECT table_name
  FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
  ORDER BY table_name
" >"$table_list"
capture_state "$baseline"
qdrant_before=$(qdrant_signature)

phase=schema_install
install_log="$snapshot_dir/memory_v1_v5_local_inference_install_${run_id}.log"
run_sql_file "$migration" >"$install_log" 2>&1

phase=rollback_only_security_test
run_sql_file "$test_sql" >>"$install_log" 2>&1
chmod 0600 "$install_log"

phase=postflight
capture_state "$post"
cmp -s "$baseline" "$post" || {
  diff -u "$baseline" "$post" >&2 || true
  echo 'preexisting Memory V1 row state changed' >&2
  exit 1
}
[[ "$(psql_scalar 'SELECT count(*) FROM memory.v5_local_inference_event')" == 0 ]]
[[ "$(psql_scalar 'SELECT count(*) FROM memory.evidence_extraction_packet_v5_local')" == 0 ]]
[[ "$(psql_scalar "SELECT (
  (SELECT count(*)=2 FROM pg_class
    WHERE oid IN (
      'memory.v5_local_inference_event'::regclass,
      'memory.evidence_extraction_packet_v5_local'::regclass
    ) AND relrowsecurity AND relforcerowsecurity)
  AND NOT (
    has_table_privilege('brains_app','memory.v5_local_inference_event','SELECT')
    OR has_table_privilege('brains_app','memory.v5_local_inference_event','INSERT')
    OR has_table_privilege('brains_app','memory.v5_local_inference_event','UPDATE')
    OR has_table_privilege('brains_app','memory.v5_local_inference_event','DELETE')
    OR has_table_privilege(
      'brains_app','memory.evidence_extraction_packet_v5_local','SELECT'
    )
    OR has_table_privilege(
      'brains_app','memory.evidence_extraction_packet_v5_local','INSERT'
    )
    OR has_table_privilege(
      'brains_app','memory.evidence_extraction_packet_v5_local','UPDATE'
    )
    OR has_table_privilege(
      'brains_app','memory.evidence_extraction_packet_v5_local','DELETE'
    )
  )
  AND (SELECT count(*)=4 FROM pg_proc
    WHERE oid IN (
      'memory.claim_owner_v5_local_inference_job_v1(uuid,uuid,uuid,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,integer)'::regprocedure,
      'memory.persist_owner_v5_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure,
      'memory.complete_owner_v5_local_inference_v1(uuid,uuid,uuid,uuid,text,integer,text,text,text,text)'::regprocedure,
      'memory.requeue_owner_local_transport_failure_v1(uuid,uuid,text,uuid,uuid,integer,text,text)'::regprocedure
    )
    AND prosecdef
    AND proowner='memory_v5_local_inference_maintainer'::regrole
    AND proconfig=ARRAY['search_path=pg_catalog']::text[])
)::integer")" == 1 ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

restore_timers

phase=report
report="$snapshot_dir/memory_v1_v5_local_inference_install_${run_id}.json"
jq -n \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head_commit "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg backup_path "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg baseline "$baseline" \
  --arg post "$post" \
  --arg log "$install_log" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:"memory_v1_v5_local_inference_install_report_v1",
    completed_at:$completed_at,
    head_commit:$head_commit,
    backup:{path:$backup_path,sha256:$backup_sha256},
    evidence:{baseline:$baseline,post:$post,log:$log,qdrant_sha256:$qdrant_sha256},
    checks:{
      owner_scoped:true,append_only:true,forced_rls:true,
      restricted_writer:true,rollback_only_security_test:true,
      replay_zero_write:true,preexisting_memory_rows_unchanged:true,
      new_tables_empty:true,qdrant_unchanged:true,timers_restored:true,
      external_model_calls:0,claim_promotion:false,
      retrieval_activation:false,prompt_influence:false
    },
    hard_stop:"before_local_canary_or_any_promotion_or_live_retrieval"
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_local_inference_production_install: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"

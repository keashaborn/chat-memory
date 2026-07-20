#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the additive V5.1 registry and restricted
# append-only local-packet persistence function. It does not activate a
# scheduler, process evidence, create claims, write Qdrant, or influence prompts.

repo=/opt/chat-memory
container=brains-postgres-1
database=memory
database_role=sage
authorized_base=76cc4736dad0bc2d8ebca3534b9d4d8a7cf299a7
implementation_ancestor=6e7149a
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_predicate_runtime_v5_1_install.lock
env_file=/opt/chat-memory/.env
registry_generator=scripts/memory_v1_predicate_registry_v5_1.py
registry_integration=specs/memory_v1_predicate_registry_v5_1_integration.json
relationship_registry=specs/memory_v1_relationship_registry_v5_1.json
base_migration=ops/sql/20260718_memory_v1_v5_local_inference.sql
migration=ops/sql/20260720_memory_v1_predicate_runtime_v5_1_local_persistence.sql
test_sql=tests/memory_v1_predicate_runtime_v5_1_local_persistence.sql
audit=docs/audits/memory_v1_predicate_runtime_v5_1_v14_20260720.json
phase=initialization
timers_quiesced=0
timer_state=
status_file=
registry_install=
timers=()

restore_timers() {
  [[ "$timers_quiesced" -eq 1 && -s "$timer_state" ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]] || return 1
    [[ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" == "$enabled" ]] || return 1
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      [[ "$active" == inactive ]] || return 1
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit" 2>/dev/null || true)" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  exit_code=$?
  if [[ "$timers_quiesced" -eq 1 ]]; then
    failed_phase=$phase
    phase=restore_timers_after_failure
    restore_timers || exit_code=1
    phase=$failed_phase
  fi
  [[ -z "$registry_install" ]] || rm -f "$registry_install"
  if [[ -n "$status_file" ]]; then
    {
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U "$database_role" -d "$database" -c "$1"
}

run_sql_file() {
  local file=$1
  docker exec \
    -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=180s' \
    -i "$container" psql -X -v ON_ERROR_STOP=1 \
    -U "$database_role" -d "$database" <"$file"
}

authenticated_health() {
  set -a
  source "$env_file"
  set +a
  test -n "${VS_SERVICE_TOKEN:-}"
  [[ "$(systemctl is-active brains.service)" == active ]]
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/readyz \
    | jq -e '.ok==true and .postgres==true' >/dev/null
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_memory_state() {
  local table_list=$1 output=$2 state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]] || return 1
    state=$(psql_scalar "
      SELECT count(*)::text || E'\\t' ||
             encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
               ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

phase=source_preflight
cd "$repo"
git merge-base --is-ancestor "$authorized_base" HEAD
git merge-base --is-ancestor "$implementation_ancestor" HEAD
[[ -z "$(git status --porcelain)" ]]
sha256sum -c <<'HASHES'
4b3ca8357f5b269024b531c157de84cf8bcf6fc3a13ae3cb8829ee3f83708f90  scripts/memory_v1_predicate_registry_v5_1.py
12e132f03554adb041090fa74ed99c178a20c5625cade85dc1bfb52b43a61eaf  specs/memory_v1_predicate_registry_v5_1_integration.json
01d0045cf5607b55eb7d2b97611c5810b81c6098c6118435a2015d5cc8102a4f  specs/memory_v1_relationship_registry_v5_1.json
2825d5e0d0d079f1ba647c047985cef886e116eab75e2719e24357748775fd65  ops/sql/20260718_memory_v1_v5_local_inference.sql
658fce782842ddb096433c6ad07f49184eefb35000bf60608e584e86656edc23  ops/sql/20260720_memory_v1_predicate_runtime_v5_1_local_persistence.sql
1382ebaa5cd67a774562e68870c92dea833cebe57b18a57800ae4fbd983b4d8b  tests/memory_v1_predicate_runtime_v5_1_local_persistence.sql
c08d12be4df4cef6a24888f383069cca3fc09e63870ef4581b4a29c106c3c511  docs/audits/memory_v1_predicate_runtime_v5_1_v14_20260720.json
HASHES
/opt/chat-memory/venv/bin/python -m unittest discover -s tests >/dev/null

umask 077
registry_install=$(mktemp /tmp/memory-v1-v5-1-registry-install.XXXXXX.sql)
/opt/chat-memory/venv/bin/python "$registry_generator" --emit-install-sql >"$registry_install"
[[ "$(sha256sum "$registry_install" | awk '{print $1}')" == c14b08169710553c7e6f003f88ff78ada5e4e67a877df09e8a96048acdd96d66 ]]

exec 9>"$lock_file"
flock -n 9
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_predicate_runtime_v5_1_install_${run_id}.status"
timer_state="$snapshot_dir/memory_v1_predicate_runtime_v5_1_timers_before_${run_id}.tsv"
install_log="$snapshot_dir/memory_v1_predicate_runtime_v5_1_install_${run_id}.log"
table_list="$snapshot_dir/memory_v1_predicate_runtime_v5_1_tables_${run_id}.txt"
before_state="$snapshot_dir/memory_v1_predicate_runtime_v5_1_before_${run_id}.tsv"
after_state="$snapshot_dir/memory_v1_predicate_runtime_v5_1_after_${run_id}.tsv"

phase=database_preflight
[[ "$(psql_scalar "SELECT current_setting('server_version_num')::integer/10000")" == 16 ]]
[[ "$(psql_scalar "SELECT (to_regprocedure('memory.persist_owner_v5_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)') IS NOT NULL)::integer")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.predicate_registry_version WHERE registry_version='memory_predicate_registry_v5_1'")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.predicate_contract WHERE registry_version='memory_predicate_registry_v5_1'")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.predicate_registry_seed WHERE registry_version='memory_predicate_registry_v5_1'")" == 0 ]]
[[ "$(psql_scalar "SELECT (to_regprocedure('memory.persist_owner_v5_1_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)') IS NULL)::integer")" == 1 ]]
authenticated_health

phase=capture_timer_state
mapfile -t timers < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
[[ ${#timers[@]} -gt 0 ]]
: >"$timer_state"
for unit in "${timers[@]}"; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  enabled=$(systemctl is-enabled "$unit" 2>/dev/null || true)
  active=$(systemctl is-active "$unit" 2>/dev/null || true)
  [[ -n "$enabled" && "$active" =~ ^(active|inactive)$ ]]
  printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active" >>"$timer_state"
done
chmod 0600 "$timer_state"

phase=quiesce_memory_v1_timers
for unit in "${timers[@]}"; do sudo -n systemctl stop "$unit"; done
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
  [[ "$(systemctl is-active "$unit" 2>/dev/null || true)" == inactive ]]
done <"$timer_state"

phase=capture_baseline
psql_scalar "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name NOT IN (
      'predicate','predicate_registry_version','predicate_registry_seed',
      'predicate_contract','predicate_registry_source_binding_v5_1',
      'relationship_predicate_contract_v5_1'
    )
  ORDER BY table_name
" >"$table_list"
chmod 0600 "$table_list"
capture_memory_state "$table_list" "$before_state"
qdrant_before=$(qdrant_signature)
predicate_before=$(psql_scalar "SELECT count(*) FROM memory.predicate")
registry_version_before=$(psql_scalar "SELECT count(*) FROM memory.predicate_registry_version")
registry_seed_before=$(psql_scalar "SELECT count(*) FROM memory.predicate_registry_seed")
predicate_contract_before=$(psql_scalar "SELECT count(*) FROM memory.predicate_contract")
legacy_predicate_hash_before=$(psql_scalar "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
    ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
  FROM (SELECT to_jsonb(value)::text AS row_json
    FROM memory.predicate AS value) AS rows")
legacy_registry_version_hash_before=$(psql_scalar "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
    ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
  FROM (SELECT to_jsonb(value)::text AS row_json
    FROM memory.predicate_registry_version AS value) AS rows")
legacy_registry_seed_hash_before=$(psql_scalar "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
    ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
  FROM (SELECT to_jsonb(value)::text AS row_json
    FROM memory.predicate_registry_seed AS value) AS rows")
legacy_predicate_contract_hash_before=$(psql_scalar "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
    ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
  FROM (SELECT to_jsonb(value)::text AS row_json
    FROM memory.predicate_contract AS value) AS rows")
v5_function_before=$(psql_scalar "
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.persist_owner_v5_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')")

phase=fresh_backup
backup_partial="$snapshot_dir/.memory_pre_predicate_runtime_v5_1_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_predicate_runtime_v5_1_${run_id}.dump"
catalog="$backup.catalog"
checksum="$backup.sha256"
docker exec "$container" pg_dump -U "$database_role" -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$checksum"
chmod 0600 "$checksum"

phase=revalidate_before_write
git merge-base --is-ancestor "$implementation_ancestor" HEAD
[[ -z "$(git status --porcelain)" ]]
[[ "$(sha256sum "$registry_install" | awk '{print $1}')" == c14b08169710553c7e6f003f88ff78ada5e4e67a877df09e8a96048acdd96d66 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.predicate_registry_version WHERE registry_version='memory_predicate_registry_v5_1'")" == 0 ]]

phase=install_v5_1_registry_and_persistence
: >"$install_log"
run_sql_file "$registry_install" >>"$install_log" 2>&1
run_sql_file "$registry_install" >>"$install_log" 2>&1
run_sql_file "$repo/$migration" >>"$install_log" 2>&1
run_sql_file "$repo/$migration" >>"$install_log" 2>&1

phase=rollback_only_security_test
run_sql_file "$repo/$test_sql" >>"$install_log" 2>&1
chmod 0600 "$install_log"

phase=verify_exact_schema_delta
[[ "$(psql_scalar "SELECT count(*) FROM memory.predicate")" == "$((predicate_before + 38))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.predicate_registry_version")" == "$((registry_version_before + 1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.predicate_registry_seed")" == "$((registry_seed_before + 82))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.predicate_contract")" == "$((predicate_contract_before + 82))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.predicate_contract WHERE registry_version='memory_predicate_registry_v5_1'")" == 82 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.predicate_registry_seed WHERE registry_version='memory_predicate_registry_v5_1'")" == 82 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.predicate_registry_source_binding_v5_1")" == 2 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.relationship_predicate_contract_v5_1")" == 41 ]]
[[ "$(psql_scalar "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
    ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
  FROM (SELECT to_jsonb(value)::text AS row_json
    FROM memory.predicate AS value
    WHERE predicate NOT IN (
      SELECT predicate FROM memory.predicate_registry_seed
      WHERE registry_version='memory_predicate_registry_v5_1'
        AND base_predicate_created
    )) AS rows")" == "$legacy_predicate_hash_before" ]]
[[ "$(psql_scalar "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
    ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
  FROM (SELECT to_jsonb(value)::text AS row_json
    FROM memory.predicate_registry_version AS value
    WHERE registry_version<>'memory_predicate_registry_v5_1') AS rows")" == "$legacy_registry_version_hash_before" ]]
[[ "$(psql_scalar "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
    ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
  FROM (SELECT to_jsonb(value)::text AS row_json
    FROM memory.predicate_registry_seed AS value
    WHERE registry_version<>'memory_predicate_registry_v5_1') AS rows")" == "$legacy_registry_seed_hash_before" ]]
[[ "$(psql_scalar "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
    ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
  FROM (SELECT to_jsonb(value)::text AS row_json
    FROM memory.predicate_contract AS value
    WHERE registry_version<>'memory_predicate_registry_v5_1') AS rows")" == "$legacy_predicate_contract_hash_before" ]]
[[ "$(psql_scalar "
  SELECT count(*) FROM memory.predicate_registry_version
  WHERE registry_version='memory_predicate_registry_v5_1'
    AND contract_version='memory_v1_relational_extraction_v5_1'
    AND status='proposed' AND runtime_active=false
    AND unknown_predicate_action='defer_unregistered_predicate'
    AND registry_sha256='091fc66b325beba1e84f47916fe3e38cf170ab25f362c5b397c14daa793ec16e'
")" == 1 ]]
[[ "$(psql_scalar "
  SELECT (
    has_function_privilege(
      'brains_app',
      'memory.persist_owner_v5_1_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)',
      'EXECUTE'
    )
    AND NOT EXISTS (
      SELECT 1 FROM pg_proc AS procedure
      CROSS JOIN LATERAL aclexplode(procedure.proacl) AS acl
      WHERE procedure.oid=
        'memory.persist_owner_v5_1_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
        AND acl.grantee=0 AND acl.privilege_type='EXECUTE'
    )
    AND EXISTS (
      SELECT 1 FROM pg_proc
      WHERE oid='memory.persist_owner_v5_1_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
        AND prosecdef
        AND proowner='memory_v5_local_inference_maintainer'::regrole
        AND EXISTS (
          SELECT 1 FROM unnest(proconfig) AS setting
          WHERE setting LIKE 'search_path=%'
        )
    )
  )::integer
")" == 1 ]]
[[ "$(psql_scalar "
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.persist_owner_v5_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
  ),'UTF8'),'sha256'),'hex')")" == "$v5_function_before" ]]

capture_memory_state "$table_list" "$after_state"
cmp -s "$before_state" "$after_state"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=restore_timers
restore_timers
authenticated_health

phase=complete
printf '%s\n' "memory_v1_predicate_runtime_v5_1_production_install: PASS run_id=$run_id"

#!/usr/bin/env bash
set -euo pipefail
umask 077

candidate=/tmp/chat-memory-governed-phase8a-controller-20260811
expected_head=fe1bd715ed9fcb86d5085ab80efc9782935c09a3
expected_tree=5010e2664ac56b0558ce4c79fee9d62dead0d276
expected_parent=5c5253099e0350d79e68c0d1c09c46c25f8b54b6
expected_package=5db42bb942b2a96d453cf3b7db28687f7c227c24f07098add73666302be8a22e
expected_runtime=e4c851e6cba93bf79835d0f7abd4708ca53e3471fe315caf16a7c0fadfe0d9a1
expected_forward=5e3238856349ca9928d9a20d7658959effdaf6472a795d959ad0707fa9bbbae0
expected_rollback=5adec472f5b3277b5a2137fc5e7e8f1d50e587a54302246f6b87cef28442d277

package_manifest=$candidate/ops/governed_memory/installation/package_manifest.json
runtime_manifest=$candidate/ops/governed_memory/runtime_manifest.json
forward_sql=$candidate/ops/governed_memory/installation/postgres/canonical_cluster.pgsql.in
rollback_sql=$candidate/ops/governed_memory/installation/postgres/canonical_cluster_rollback.pgsql.in

image_ref='postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777'
invocation=fe1bd71-5db42bb9-selfbound
scope=phase8a-disposable-pg16
container_name=gm8a-pg16-$invocation
network_name=gm8a-net-$invocation
receipt=/tmp/governed-memory-phase8a-pg16-proof-$invocation.json
bootstrap_role=governed_memory_bootstrap
synthetic_password='phase8a-disposable-only-not-a-live-secret'

container_id=
network_id=
image_id=
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
positive_scenarios=()
refusal_scenarios=()

die() {
  printf 'PROOF_ERROR=%s\n' "$1" >&2
  exit "${2:-1}"
}

sha256_file() {
  sha256sum "$1" | awk '{print $1}'
}

assert_equal() {
  local actual=$1
  local expected=$2
  local label=$3
  [[ "$actual" == "$expected" ]] || die "$label differs"
}

harness_path=${BASH_SOURCE[0]}
[[ "$harness_path" == /* ]] || die 'proof harness path is not absolute'
harness_sha256=$(sha256_file "$harness_path")

safe_cleanup() {
  local actual network_attachment tmpfs_keys expected_tmpfs

  if [[ -n "$container_id" ]] && docker container inspect "$container_id" >/dev/null 2>&1; then
    actual=$(docker container inspect "$container_id" --format '{{.Id}}')
    [[ "$actual" == "$container_id" ]] || return 97
    [[ "$(docker container inspect "$container_id" --format '{{.Name}}')" == "/$container_name" ]] || return 97
    [[ "$(docker container inspect "$container_id" --format '{{.Image}}')" == "$image_id" ]] || return 97
    [[ "$(docker container inspect "$container_id" --format '{{index .Config.Labels "org.lifeswitch.governed-memory.invocation"}}')" == "$invocation" ]] || return 97
    [[ "$(docker container inspect "$container_id" --format '{{index .Config.Labels "org.lifeswitch.governed-memory.scope"}}')" == "$scope" ]] || return 97
    [[ "$(docker container inspect "$container_id" --format '{{.HostConfig.RestartPolicy.Name}}')" == no ]] || return 97
    [[ "$(docker container inspect "$container_id" --format '{{.HostConfig.ReadonlyRootfs}}')" == true ]] || return 97
    [[ "$(docker container inspect "$container_id" --format '{{.HostConfig.PublishAllPorts}}')" == false ]] || return 97
    [[ "$(docker container inspect "$container_id" --format '{{.HostConfig.LogConfig.Type}}')" == none ]] || return 97
    [[ "$(docker container inspect "$container_id" --format '{{len .HostConfig.PortBindings}}')" == 0 ]] || return 97
    [[ "$(docker container inspect "$container_id" --format '{{len .Mounts}}')" == 0 ]] || return 97
    [[ "$(docker container inspect "$container_id" --format '{{len .HostConfig.Binds}}')" == 0 ]] || return 97
    [[ "$(docker container inspect "$container_id" --format '{{len .HostConfig.Tmpfs}}')" == 3 ]] || return 97
    tmpfs_keys=$(docker container inspect "$container_id" --format '{{range $key, $value := .HostConfig.Tmpfs}}{{printf "%s\n" $key}}{{end}}' | sed '/^$/d' | sort)
    expected_tmpfs=$(printf '/tmp\n/var/lib/postgresql/data\n/var/run/postgresql')
    [[ "$tmpfs_keys" == "$expected_tmpfs" ]] || return 97
    [[ "$(docker container inspect "$container_id" --format '{{len .NetworkSettings.Networks}}')" == 1 ]] || return 97
    network_attachment=$(docker container inspect "$container_id" --format "{{with index .NetworkSettings.Networks \"$network_name\"}}{{.NetworkID}}{{end}}")
    [[ "$network_attachment" == "$network_id" ]] || return 97
    docker container rm --force --volumes "$container_id" >/dev/null || return 97
  else
    [[ -z "$(docker container ls -a --filter "name=^/${container_name}$" --format '{{.ID}}')" ]] || return 97
  fi

  if [[ -n "$network_id" ]] && docker network inspect "$network_id" >/dev/null 2>&1; then
    actual=$(docker network inspect "$network_id" --format '{{.Id}}')
    [[ "$actual" == "$network_id" ]] || return 97
    [[ "$(docker network inspect "$network_id" --format '{{.Name}}')" == "$network_name" ]] || return 97
    [[ "$(docker network inspect "$network_id" --format '{{.Internal}}')" == true ]] || return 97
    [[ "$(docker network inspect "$network_id" --format '{{index .Labels "org.lifeswitch.governed-memory.invocation"}}')" == "$invocation" ]] || return 97
    [[ "$(docker network inspect "$network_id" --format '{{index .Labels "org.lifeswitch.governed-memory.scope"}}')" == "$scope" ]] || return 97
    [[ "$(docker network inspect "$network_id" --format '{{len .Containers}}')" == 0 ]] || return 97
    docker network rm "$network_id" >/dev/null || return 97
  else
    [[ -z "$(docker network ls --filter "name=^${network_name}$" --format '{{.ID}}')" ]] || return 97
  fi

  [[ -z "$(docker container ls -a --filter "name=^/${container_name}$" --format '{{.ID}}')" ]] || return 97
  [[ -z "$(docker network ls --filter "name=^${network_name}$" --format '{{.ID}}')" ]] || return 97
  [[ -z "$(docker container ls -a --filter "label=org.lifeswitch.governed-memory.invocation=$invocation" --format '{{.ID}}')" ]] || return 97
  [[ -z "$(docker network ls --filter "label=org.lifeswitch.governed-memory.invocation=$invocation" --format '{{.ID}}')" ]] || return 97
}

cleanup_on_exit() {
  local rc=$?
  trap - EXIT INT TERM
  if ! safe_cleanup; then
    printf 'PROOF_ERROR=owned resource identity mismatch; cleanup refused\n' >&2
    exit 97
  fi
  exit "$rc"
}
trap cleanup_on_exit EXIT INT TERM

psql_postgres() {
  docker exec -e "PGPASSWORD=$synthetic_password" -i "$container_id" \
    psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 \
    -U "$bootstrap_role" -d postgres "$@"
}

psql_database() {
  docker exec -e "PGPASSWORD=$synthetic_password" -i "$container_id" \
    psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 \
    -U "$bootstrap_role" -d governed_memory "$@"
}

run_postgres_sql() {
  printf '%s\n' "$1" | psql_postgres
}

run_database_sql() {
  printf '%s\n' "$1" | psql_database
}

run_forward() {
  psql_postgres -f - < "$forward_sql"
}

run_rollback() {
  docker exec -e "PGPASSWORD=$synthetic_password" -i "$container_id" \
    psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 \
    -v governed_memory_empty_cluster_rollback=on \
    -U "$bootstrap_role" -d postgres -f - < "$rollback_sql"
}

verify_absent() {
  local result
  result=$(docker exec -e "PGPASSWORD=$synthetic_password" "$container_id" \
    psql -X -qAt -v ON_ERROR_STOP=1 -h 127.0.0.1 \
    -U "$bootstrap_role" -d postgres -c "
      SELECT
        NOT EXISTS (
          SELECT 1 FROM pg_catalog.pg_database
          WHERE datname = 'governed_memory'
        )
        AND NOT EXISTS (
          SELECT 1 FROM pg_catalog.pg_roles
          WHERE rolname IN (
            'governed_memory_owner',
            'governed_memory_api',
            'governed_memory_worker',
            'memory_ingest_writer',
            'memory_erasure_requester'
          )
        )
        AND (
          SELECT pg_catalog.array_agg(rolname ORDER BY rolname)
          FROM pg_catalog.pg_roles
          WHERE rolname !~ '^pg_'
        ) = ARRAY['governed_memory_bootstrap']::name[]
        AND (
          SELECT pg_catalog.array_agg(datname ORDER BY datname)
          FROM pg_catalog.pg_database
        ) = ARRAY['postgres','template0','template1']::name[];")
  [[ "$result" == t ]] || die 'final absence assertion failed'
}

verify_full() {
  local cluster database
  cluster=$(docker exec -e "PGPASSWORD=$synthetic_password" "$container_id" \
    psql -X -qAt -v ON_ERROR_STOP=1 -h 127.0.0.1 \
    -U "$bootstrap_role" -d postgres -c "
      SELECT
        (SELECT pg_catalog.count(*) = 5 FROM pg_catalog.pg_roles
          WHERE rolname IN (
            'governed_memory_owner','governed_memory_api',
            'governed_memory_worker','memory_ingest_writer',
            'memory_erasure_requester'
          ))
        AND EXISTS (
          SELECT 1 FROM pg_catalog.pg_database AS d
          JOIN pg_catalog.pg_roles AS r ON r.oid = d.datdba
          WHERE d.datname = 'governed_memory'
            AND r.rolname = 'governed_memory_owner'
        )
        AND (SELECT pg_catalog.count(*) = 1
          FROM pg_catalog.pg_auth_members AS m
          JOIN pg_catalog.pg_roles AS granted ON granted.oid = m.roleid
          JOIN pg_catalog.pg_roles AS member ON member.oid = m.member
          WHERE granted.rolname = 'governed_memory_owner'
            AND member.rolname = 'governed_memory_bootstrap');")
  database=$(docker exec -e "PGPASSWORD=$synthetic_password" "$container_id" \
    psql -X -qAt -v ON_ERROR_STOP=1 -h 127.0.0.1 \
    -U "$bootstrap_role" -d governed_memory -c "
      SELECT
        EXISTS (
          SELECT 1 FROM pg_catalog.pg_extension AS e
          JOIN pg_catalog.pg_roles AS r ON r.oid = e.extowner
          WHERE e.extname = 'pgcrypto'
            AND e.extversion = '1.3'
            AND r.rolname = 'governed_memory_owner'
        )
        AND NOT EXISTS (
          SELECT 1
          FROM pg_catalog.pg_database AS database
          CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(
              database.datacl,
              pg_catalog.acldefault('d', database.datdba)
            )
          ) AS privilege
          WHERE database.datname = 'governed_memory'
            AND privilege.grantee = 0
            AND privilege.privilege_type = 'CONNECT'
        )
        AND pg_catalog.has_database_privilege(
          'governed_memory_api', 'governed_memory', 'CONNECT'
        )
        AND pg_catalog.has_database_privilege(
          'governed_memory_worker', 'governed_memory', 'CONNECT'
        )
        AND NOT EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(
              namespace.nspacl,
              pg_catalog.acldefault('n', namespace.nspowner)
            )
          ) AS privilege
          WHERE namespace.nspname = 'public'
            AND privilege.grantee = 0
            AND privilege.privilege_type = 'CREATE'
        );")
  [[ "$cluster" == t && "$database" == t ]] || die 'full forward assertion failed'
}

verify_roles_only() {
  local result
  result=$(docker exec -e "PGPASSWORD=$synthetic_password" "$container_id" \
    psql -X -qAt -v ON_ERROR_STOP=1 -h 127.0.0.1 \
    -U "$bootstrap_role" -d postgres -c "
      SELECT
        NOT EXISTS (
          SELECT 1 FROM pg_catalog.pg_database
          WHERE datname = 'governed_memory'
        )
        AND (SELECT pg_catalog.count(*) = 5 FROM pg_catalog.pg_roles
          WHERE rolname IN (
            'governed_memory_owner','governed_memory_api',
            'governed_memory_worker','memory_ingest_writer',
            'memory_erasure_requester'
          ))
        AND (SELECT pg_catalog.count(*) = 1
          FROM pg_catalog.pg_auth_members AS m
          JOIN pg_catalog.pg_roles AS granted ON granted.oid = m.roleid
          JOIN pg_catalog.pg_roles AS member ON member.oid = m.member
          WHERE granted.rolname = 'governed_memory_owner'
            AND member.rolname = 'governed_memory_bootstrap');")
  [[ "$result" == t ]] || die 'roles-only recovery assertion failed'
}

state_fingerprint() {
  docker exec -e "PGPASSWORD=$synthetic_password" "$container_id" \
    pg_dumpall \
      --host=127.0.0.1 \
      --username="$bootstrap_role" \
      --schema-only \
      --no-role-passwords \
    | LC_ALL=C sed -e '/^\\restrict /d' -e '/^\\unrestrict /d' \
    | sha256sum \
    | awk '{print $1}'
}

create_roles() {
  local count=$1
  local index
  local roles=(
    governed_memory_owner
    governed_memory_api
    governed_memory_worker
    memory_ingest_writer
    memory_erasure_requester
  )
  for ((index=0; index<count; index++)); do
    run_postgres_sql "CREATE ROLE ${roles[$index]} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;"
  done
}

create_database() {
  run_postgres_sql "CREATE DATABASE governed_memory OWNER governed_memory_owner ENCODING 'UTF8' TEMPLATE template0;"
}

revoke_database_public() {
  run_postgres_sql 'REVOKE ALL ON DATABASE governed_memory FROM PUBLIC;'
}

grant_runtime_connect() {
  run_postgres_sql 'GRANT CONNECT ON DATABASE governed_memory TO governed_memory_api, governed_memory_worker;'
}

grant_owner_membership() {
  run_postgres_sql 'GRANT governed_memory_owner TO governed_memory_bootstrap;'
}

revoke_public_create() {
  run_database_sql 'REVOKE CREATE ON SCHEMA public FROM PUBLIC;'
}

install_pgcrypto() {
  run_database_sql 'SET ROLE governed_memory_owner; CREATE EXTENSION pgcrypto WITH SCHEMA public; RESET ROLE;'
}

prepare_prefix() {
  local prefix=$1
  local line_count
  case "$prefix" in
    absence) line_count=0 ;;
    owner) line_count=65 ;;
    owner_api) line_count=67 ;;
    owner_api_worker) line_count=69 ;;
    owner_api_worker_ingest) line_count=71 ;;
    all_roles) line_count=73 ;;
    database_default) line_count=78 ;;
    database_public_revoked) line_count=79 ;;
    runtime_connect) line_count=81 ;;
    owner_membership) line_count=82 ;;
    public_create_revoked) line_count=85 ;;
    pgcrypto_installed) line_count=88 ;;
    *) die "unknown prefix $prefix" ;;
  esac
  if (( line_count > 0 )); then
    head -n "$line_count" "$forward_sql" | psql_postgres -f -
  fi
}

expect_refusal() {
  local name=$1
  local expected_message=$2
  local mode=$3
  local before after output rc
  before=$(state_fingerprint)
  set +e
  if [[ "$mode" == missing ]]; then
    output=$(docker exec -e "PGPASSWORD=$synthetic_password" -i "$container_id" \
      psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 \
      -U "$bootstrap_role" -d postgres -f - < "$rollback_sql" 2>&1)
    rc=$?
  elif [[ "$mode" == off ]]; then
    output=$(docker exec -e "PGPASSWORD=$synthetic_password" -i "$container_id" \
      psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 \
      -v governed_memory_empty_cluster_rollback=off \
      -U "$bootstrap_role" -d postgres -f - < "$rollback_sql" 2>&1)
    rc=$?
  else
    output=$(docker exec -e "PGPASSWORD=$synthetic_password" -i "$container_id" \
      psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 \
      -v governed_memory_empty_cluster_rollback=on \
      -U "$bootstrap_role" -d postgres -f - < "$rollback_sql" 2>&1)
    rc=$?
  fi
  set -e
  [[ "$rc" == 3 ]] || die "$name returned $rc instead of 3"
  [[ "$output" == *"$expected_message"* ]] || die "$name message differs"
  after=$(state_fingerprint)
  [[ "$after" == "$before" ]] || die "$name changed state"
  refusal_scenarios+=("$name")
}

for command_name in docker jq sha256sum git; do
  command -v "$command_name" >/dev/null || die "$command_name is unavailable"
done

assert_equal "$(git -C "$candidate" rev-parse HEAD)" "$expected_head" candidate_head
assert_equal "$(git -C "$candidate" rev-parse HEAD^{tree})" "$expected_tree" candidate_tree
assert_equal "$(git -C "$candidate" rev-parse HEAD^)" "$expected_parent" candidate_parent
[[ -z "$(git -C "$candidate" status --porcelain=v1 --untracked-files=all)" ]] || die 'candidate worktree is not clean'
assert_equal "$(sha256_file "$package_manifest")" "$expected_package" package_manifest_sha256
assert_equal "$(sha256_file "$runtime_manifest")" "$expected_runtime" runtime_manifest_sha256
assert_equal "$(sha256_file "$forward_sql")" "$expected_forward" forward_sql_sha256
assert_equal "$(sha256_file "$rollback_sql")" "$expected_rollback" rollback_sql_sha256
[[ "$(rg -n '^DROP DATABASE governed_memory;$' "$rollback_sql" | wc -l)" == 1 ]] || die 'rollback DROP DATABASE count differs'

assert_equal "$(docker context show)" default docker_context
assert_equal "$(docker context inspect default --format '{{.Endpoints.docker.Host}}')" \
  unix:///var/run/docker.sock docker_endpoint
image_id=$(docker image inspect "$image_ref" --format '{{.Id}}')
image_platform="$(docker image inspect "$image_ref" --format '{{.Os}}')/$(docker image inspect "$image_ref" --format '{{.Architecture}}')"
assert_equal "$(docker image inspect "$image_ref" --format '{{index .RepoDigests 0}}')" \
  'postgres@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777' image_digest
[[ -z "$(docker container ls -a --filter "name=^/${container_name}$" --format '{{.ID}}')" ]] || die 'container name already exists'
[[ -z "$(docker network ls --filter "name=^${network_name}$" --format '{{.ID}}')" ]] || die 'network name already exists'
[[ -z "$(docker container ls -a --filter "label=org.lifeswitch.governed-memory.invocation=$invocation" --format '{{.ID}}')" ]] || die 'invocation label already exists'
[[ -z "$(env | awk -F= '/^(DATABASE_URL|POSTGRES_URL|SUPABASE_DB_URL|OPENAI_API_KEY|QDRANT_URL|QDRANT_API_KEY)=/{print $1}')" ]] || die 'ambient authority variable is present'

network_id=$(docker network create \
  --internal \
  --label "org.lifeswitch.governed-memory.invocation=$invocation" \
  --label "org.lifeswitch.governed-memory.scope=$scope" \
  "$network_name")

container_id=$(docker container run --detach \
  --pull never \
  --name "$container_name" \
  --network "$network_name" \
  --restart no \
  --read-only \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --cap-add CHOWN \
  --cap-add DAC_OVERRIDE \
  --cap-add FOWNER \
  --cap-add SETGID \
  --cap-add SETUID \
  --log-driver none \
  --label "org.lifeswitch.governed-memory.invocation=$invocation" \
  --label "org.lifeswitch.governed-memory.scope=$scope" \
  --tmpfs /var/lib/postgresql/data:rw,nosuid,nodev,noexec,size=768m \
  --tmpfs /var/run/postgresql:rw,nosuid,nodev,noexec,size=16m \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
  --env POSTGRES_USER="$bootstrap_role" \
  --env POSTGRES_PASSWORD="$synthetic_password" \
  --env POSTGRES_DB=postgres \
  --env PGDATA=/var/lib/postgresql/data/pgdata \
  --env POSTGRES_INITDB_ARGS='--encoding=UTF8 --locale=C' \
  "$image_ref" \
  postgres \
    -c log_statement=none \
    -c log_duration=off \
    -c log_min_duration_statement=-1 \
    -c log_min_duration_sample=-1 \
    -c log_transaction_sample_rate=0 \
    -c log_parameter_max_length=0 \
    -c log_parameter_max_length_on_error=0 \
    -c password_encryption=scram-sha-256)

ready=false
for _ in $(seq 1 120); do
  if docker exec "$container_id" pg_isready -q -h 127.0.0.1 -U "$bootstrap_role" -d postgres; then
    ready=true
    break
  fi
  sleep 0.25
done
[[ "$ready" == true ]] || die 'PostgreSQL did not become ready'

postgres_version=$(docker exec -e "PGPASSWORD=$synthetic_password" "$container_id" \
  psql -X -qAt -h 127.0.0.1 -U "$bootstrap_role" -d postgres \
  -c 'SHOW server_version;')
[[ "$postgres_version" == 16.* ]] || die 'PostgreSQL server is not major version 16'
verify_absent

prefixes=(
  absence
  owner
  owner_api
  owner_api_worker
  owner_api_worker_ingest
  all_roles
  database_default
  database_public_revoked
  runtime_connect
  owner_membership
  public_create_revoked
  pgcrypto_installed
)
for prefix in "${prefixes[@]}"; do
  prepare_prefix "$prefix"
  run_rollback
  verify_absent
  positive_scenarios+=("prefix_${prefix}_authorized_rollback")
done

expect_refusal rollback_missing_authorization \
  'required governed_memory_empty_cluster_rollback variable is absent' missing
expect_refusal rollback_wrong_authorization \
  'governed_memory_empty_cluster_rollback must be exactly on' off

prepare_prefix owner
run_postgres_sql 'ALTER ROLE governed_memory_owner LOGIN;'
expect_refusal rollback_role_attribute_drift \
  'canonical role attributes differ' role_drift
run_postgres_sql 'ALTER ROLE governed_memory_owner NOLOGIN;'
run_rollback
verify_absent
positive_scenarios+=(repaired_role_drift_authorized_rollback)

run_forward
verify_full
positive_scenarios+=(full_forward_unchanged_template)

run_database_sql 'ALTER FUNCTION public.digest(bytea,text) OWNER TO governed_memory_owner;'
expect_refusal rollback_pgcrypto_member_owner_drift \
  'pgcrypto extension member properties differ' member_owner_drift
run_database_sql 'ALTER FUNCTION public.digest(bytea,text) OWNER TO governed_memory_bootstrap;'

run_database_sql 'SET ROLE governed_memory_owner; CREATE TABLE public.phase8a_disposable_guard(id integer); RESET ROLE;'
expect_refusal rollback_nonempty_database \
  'unexpected public relation or data remains' nonempty
run_database_sql 'SET ROLE governed_memory_owner; DROP TABLE public.phase8a_disposable_guard; RESET ROLE;'

run_rollback
verify_absent
positive_scenarios+=(full_state_authorized_rollback)

run_rollback
verify_absent
positive_scenarios+=(final_absence_retry)

run_forward
verify_full
positive_scenarios+=(full_forward_reapply_after_rollback)

run_postgres_sql 'DROP DATABASE governed_memory;'
verify_roles_only
positive_scenarios+=(simulated_crash_after_exact_database_drop)

run_rollback
verify_absent
positive_scenarios+=(roles_only_recovery_rollback)

run_forward
verify_full
positive_scenarios+=(final_full_forward_reapply)

run_rollback
verify_absent
positive_scenarios+=(final_authorized_rollback)

assert_equal "$(git -C "$candidate" rev-parse HEAD)" "$expected_head" final_candidate_head
assert_equal "$(git -C "$candidate" rev-parse HEAD^{tree})" "$expected_tree" final_candidate_tree
[[ -z "$(git -C "$candidate" status --porcelain=v1 --untracked-files=all)" ]] || die 'candidate changed during proof'
assert_equal "$(sha256_file "$harness_path")" "$harness_sha256" final_harness_sha256

captured_container_id=$container_id
captured_network_id=$network_id
safe_cleanup || die 'owned resource cleanup failed' 97
container_id=
network_id=

ended_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
docker_server_version=$(docker version --format '{{.Server.Version}}')
positive_json=$(printf '%s\n' "${positive_scenarios[@]}" | jq -R . | jq -s .)
refusal_json=$(printf '%s\n' "${refusal_scenarios[@]}" | jq -R . | jq -s .)

jq -n \
  --arg started_at "$started_at" \
  --arg ended_at "$ended_at" \
  --arg head "$expected_head" \
  --arg tree "$expected_tree" \
  --arg parent "$expected_parent" \
  --arg package "$expected_package" \
  --arg runtime "$expected_runtime" \
  --arg forward "$expected_forward" \
  --arg rollback "$expected_rollback" \
  --arg harness_path "$harness_path" \
  --arg harness_sha256 "$harness_sha256" \
  --arg image_ref "$image_ref" \
  --arg image_id "$image_id" \
  --arg image_platform "$image_platform" \
  --arg docker_server_version "$docker_server_version" \
  --arg postgres_version "$postgres_version" \
  --arg invocation "$invocation" \
  --arg container_name "$container_name" \
  --arg container_id "$captured_container_id" \
  --arg network_name "$network_name" \
  --arg network_id "$captured_network_id" \
  --argjson positive "$positive_json" \
  --argjson refusals "$refusal_json" \
  '{
    schema_version: "governed-memory-phase8a-pg16-disposable-proof-v2",
    phase: "phase8a_inactive_installation_controller",
    result: "pass",
    scope: "isolated_disposable_postgresql16_only_not_installation_authority",
    started_at: $started_at,
    ended_at: $ended_at,
    candidate: {
      head: $head,
      tree: $tree,
      parent: $parent,
      worktree_clean_before_and_after: true
    },
    bindings: {
      package_manifest_sha256: $package,
      runtime_manifest_sha256: $runtime,
      canonical_cluster_forward_sha256: $forward,
      canonical_cluster_rollback_sha256: $rollback,
      proof_harness_executed_path: $harness_path,
      proof_harness_sha256: $harness_sha256,
      proof_harness_self_bound: true
    },
    runtime: {
      docker_context: "default",
      docker_server_version: $docker_server_version,
      image_reference: $image_ref,
      image_id: $image_id,
      platform: $image_platform,
      postgresql_server_version: $postgres_version
    },
    execution: {
      positive_scenarios: $positive,
      positive_scenario_count: ($positive | length),
      refusal_scenarios: $refusals,
      refusal_scenario_count: ($refusals | length),
      refusal_state_unchanged: true,
      final_database_absent: true,
      final_target_roles_absent: true
    },
    isolation: {
      invocation: $invocation,
      container_name: $container_name,
      container_id: $container_id,
      network_name: $network_name,
      network_id: $network_id,
      network_internal: true,
      published_ports: false,
      bind_mounts: false,
      persistent_volumes: false,
      tmpfs_only: true,
      read_only_rootfs: true,
      no_new_privileges: true,
      container_log_driver_none: true,
      source_database_endpoints_supplied: false,
      source_database_reads: 0,
      source_database_writes: 0,
      qdrant_operations: 0,
      provider_calls: 0,
      live_mutations: 0
    },
    cleanup: {
      exact_owned_resources_removed: true,
      proof_image_retained: true
    },
    authority: {
      installation_authorized: false,
      activation_authorized: false,
      production_state_changed: false,
      proof_metadata_promotion_required: true,
      clearable_blocker_after_promotion: "canonical_cluster_rollback_not_disposable_postgresql_executed"
    }
  }' > "$receipt"

printf 'RESULT=pass\n'
printf 'POSITIVE_SCENARIOS=%s\n' "${#positive_scenarios[@]}"
printf 'REFUSAL_SCENARIOS=%s\n' "${#refusal_scenarios[@]}"
printf 'POSTGRES_VERSION=%s\n' "$postgres_version"
printf 'RECEIPT=%s\n' "$receipt"
printf 'RECEIPT_SHA256=%s\n' "$(sha256_file "$receipt")"
printf 'RESOURCES_REMOVED=true\n'
printf 'LIVE_MUTATIONS=0\n'

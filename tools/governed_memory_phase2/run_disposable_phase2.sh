#!/usr/bin/env bash
set -Eeuo pipefail

# Server: seebx backend only.
#
# This is a one-shot disposable verifier. It refuses production ports, stale
# Phase 2 resources, dirty/unbound candidate bytes, persistent Docker mounts,
# ambient test environment variables, and every mode other than `full`.
#
# Required explicit authorization and immutable candidate binding:
#   GM_PHASE2_DISPOSABLE_AUTHORIZATION='019fe927:DISPOSABLE_ONLY:NO_PRODUCTION_DATA'
#   GM_PHASE2_EXPECTED_HEAD='<exact 40-character candidate commit>'
#   GM_PHASE2_EXPECTED_TREE='<exact 40-character candidate tree>'
#
# The EXIT trap is installed before Docker creation. It removes only resources
# whose captured ID, exact name, and three ownership labels still agree.

umask 077
export LC_ALL=C
export LANG=C
readonly PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
export PATH

readonly EXPECTED_HOST='ip-172-31-32-171'
readonly EXPECTED_USER='ubuntu'
readonly EXPECTED_ROOT='/tmp/chat-memory-clean-successor-phase0-20260810'
readonly EXPECTED_BRANCH='codex/clean-memory-successor-phase0-20260810'
readonly EXPECTED_BASE='fd12ce9acc331a82a78612c3410ac0e3337dda5e'
readonly RUN_ID='019fe927'
readonly AUTHORIZATION_VALUE='019fe927:DISPOSABLE_ONLY:NO_PRODUCTION_DATA'
readonly EXPECTED_MANIFEST_CANDIDATE='clean-governed-memory-phase2-2026-08-10'

readonly LEGACY_LABEL_KEY='governed-memory-phase2'
readonly LABEL_SCOPE_KEY='com.verbalsage.governed-memory.scope'
readonly LABEL_SCOPE_VALUE='phase2-disposable'
readonly LABEL_RUN_KEY='com.verbalsage.governed-memory.run-id'
readonly LABEL_INVOCATION_KEY='com.verbalsage.governed-memory.invocation-id'

readonly POSTGRES_IMAGE='postgres:16-alpine'
readonly POSTGRES_IMAGE_ID='sha256:de3a4eab8fdfa507ea92aac488b916b08089e515db49b055fe71dfa271ba3a28'
readonly QDRANT_IMAGE='qdrant/qdrant:v1.11.0'
readonly QDRANT_IMAGE_ID='sha256:dc764734fcd6f947f2c626d3731fbfafd17d098a702dda8aab5b645c51b6c408'
readonly QDRANT_IMAGE_DIGEST='qdrant/qdrant@sha256:cc802bd2841ec2026725e19619075982311ce4d7182dc8c03a0c8e6817bb9170'
readonly POSTGRES_PORT='55442'
readonly QDRANT_PORT='6338'
readonly POSTGRES_PASSWORD='phase2_disposable_only'
readonly TEST_PYTHON='/opt/chat-memory/venv/bin/python'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd -P)"
MIGRATIONS="${ROOT}/governed-memory-migrations"
readonly SCRIPT_DIR ROOT MIGRATIONS

EXPECTED_HEAD="${GM_PHASE2_EXPECTED_HEAD:-}"
EXPECTED_TREE="${GM_PHASE2_EXPECTED_TREE:-}"
readonly EXPECTED_HEAD EXPECTED_TREE

INVOCATION_ID=''
INVOCATION_TOKEN=''
NETWORK_NAME=''
POSTGRES_CONTAINER_NAME=''
QDRANT_CONTAINER_NAME=''
NETWORK_ID=''
POSTGRES_CONTAINER_ID=''
QDRANT_CONTAINER_ID=''
RUN_TMP=''
MIGRATION_MANIFEST_SHA256=''
FOUNDATION_DUMP_SHA256=''
BRIDGE_DUMP_SHA256=''
INTEGRATION_RECEIPT_SHA256=''
POSTGRES_SERVER_VERSION=''
QDRANT_SERVER_VERSION=''

die() {
  printf 'PHASE2_REFUSED=%s\n' "$1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "command_missing:$1"
}

assert_candidate_binding() {
  local actual_branch actual_head actual_root actual_status actual_tree commit_count

  [[ "${EXPECTED_HEAD}" =~ ^[0-9a-f]{40}$ ]] || die 'expected_head_invalid'
  [[ "${EXPECTED_TREE}" =~ ^[0-9a-f]{40}$ ]] || die 'expected_tree_invalid'

  actual_root="$(git -C "${ROOT}" rev-parse --show-toplevel)" \
    || die 'candidate_git_root_unreadable'
  [[ "${actual_root}" == "${EXPECTED_ROOT}" ]] || die 'candidate_git_root_mismatch'

  actual_branch="$(git -C "${ROOT}" symbolic-ref --quiet --short HEAD)" \
    || die 'candidate_detached'
  [[ "${actual_branch}" == "${EXPECTED_BRANCH}" ]] || die 'candidate_branch_mismatch'

  actual_head="$(git -C "${ROOT}" rev-parse --verify HEAD)" \
    || die 'candidate_head_unreadable'
  actual_tree="$(git -C "${ROOT}" rev-parse --verify 'HEAD^{tree}')" \
    || die 'candidate_tree_unreadable'
  [[ "${actual_head}" == "${EXPECTED_HEAD}" ]] || die 'candidate_head_drift'
  [[ "${actual_tree}" == "${EXPECTED_TREE}" ]] || die 'candidate_tree_drift'
  git -C "${ROOT}" merge-base --is-ancestor "${EXPECTED_BASE}" "${actual_head}" \
    || die 'candidate_base_not_ancestor'
  commit_count="$(
    git -C "${ROOT}" rev-list --count "${EXPECTED_BASE}..${actual_head}"
  )" || die 'candidate_commit_count_unreadable'
  [[ "${commit_count}" == '1' ]] || die 'candidate_not_single_commit'

  actual_status="$(git -C "${ROOT}" status --porcelain=v1 --untracked-files=all)" \
    || die 'candidate_status_unreadable'
  [[ -z "${actual_status}" ]] || die 'candidate_not_clean'
}

acquire_lock() {
  local lock_file='/tmp/governed-memory-phase2-019fe927.lock'
  local current_uid
  current_uid="$(id -u)"

  if [[ -e "${lock_file}" || -L "${lock_file}" ]]; then
    [[ -f "${lock_file}" && ! -L "${lock_file}" ]] \
      || die 'runner_lock_not_regular'
    [[ "$(stat -c '%u' "${lock_file}")" == "${current_uid}" ]] \
      || die 'runner_lock_wrong_owner'
  fi

  exec 9>"${lock_file}"
  chmod 600 "${lock_file}"
  [[ -f "${lock_file}" && ! -L "${lock_file}" ]] \
    || die 'runner_lock_replaced'
  [[ "$(stat -c '%u' "${lock_file}")" == "${current_uid}" ]] \
    || die 'runner_lock_owner_changed'
  flock -n 9 || die 'runner_already_active'
}

verify_migration_manifest() {
  local receipt
  local -a fields

  receipt="$(
    "${TEST_PYTHON}" \
      "${SCRIPT_DIR}/verify_migration_manifest.py" "${MIGRATIONS}"
  )" || die 'migration_manifest_verification_failed'

  mapfile -t fields < <(
    "${TEST_PYTHON}" - "${receipt}" <<'PY'
import json
import sys

value = json.loads(sys.argv[1])
print(value.get("schema_version", ""))
print(value.get("result", ""))
print(value.get("candidate_id", ""))
print(value.get("file_count", ""))
print(value.get("manifest_sha256", ""))
PY
  )
  [[ "${#fields[@]}" -eq 5 ]] || die 'migration_verification_receipt_invalid'
  [[ "${fields[0]}" == 'governed-memory-migration-verification-v2' ]] \
    || die 'migration_verification_schema_invalid'
  [[ "${fields[1]}" == 'verified' ]] || die 'migration_verification_not_verified'
  [[ "${fields[2]}" == "${EXPECTED_MANIFEST_CANDIDATE}" ]] \
    || die 'migration_candidate_id_mismatch'
  [[ "${fields[3]}" == '9' ]] || die 'migration_file_count_mismatch'
  [[ "${fields[4]}" =~ ^[0-9a-f]{64}$ ]] \
    || die 'migration_manifest_sha256_invalid'
  MIGRATION_MANIFEST_SHA256="${fields[4]}"
}

assert_image_binding() {
  local actual_id repo_digests

  actual_id="$(docker image inspect "${POSTGRES_IMAGE}" --format '{{.Id}}')" \
    || die 'postgres_image_missing'
  [[ "${actual_id}" == "${POSTGRES_IMAGE_ID}" ]] || die 'postgres_image_drift'
  docker image inspect "${POSTGRES_IMAGE_ID}" >/dev/null \
    || die 'postgres_image_id_missing'

  actual_id="$(docker image inspect "${QDRANT_IMAGE}" --format '{{.Id}}')" \
    || die 'qdrant_image_missing'
  [[ "${actual_id}" == "${QDRANT_IMAGE_ID}" ]] || die 'qdrant_image_drift'
  docker image inspect "${QDRANT_IMAGE_ID}" >/dev/null \
    || die 'qdrant_image_id_missing'
  repo_digests="$(
    docker image inspect "${QDRANT_IMAGE_ID}" --format '{{json .RepoDigests}}'
  )" || die 'qdrant_repo_digests_unreadable'
  "${TEST_PYTHON}" - "${repo_digests}" "${QDRANT_IMAGE_DIGEST}" <<'PY' \
    || die 'qdrant_image_digest_mismatch'
import json
import sys

if sys.argv[2] not in json.loads(sys.argv[1]):
    raise SystemExit(1)
PY
}

assert_no_listening_port() {
  local port="$1"
  local listeners
  listeners="$(ss -H -ltn "sport = :${port}")" \
    || die "port_inspection_failed:${port}"
  [[ -z "${listeners}" ]] || die "port_already_listening:${port}"
}

assert_ports_bindable() {
  "${TEST_PYTHON}" - "${POSTGRES_PORT}" "${QDRANT_PORT}" <<'PY' \
    || die 'loopback_ports_not_bindable'
import socket
import sys

sockets = []
try:
    for value in sys.argv[1:]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", int(value)))
        sockets.append(sock)
finally:
    for sock in sockets:
        sock.close()
PY
}

assert_resource_namespace_empty() {
  local found legacy_name port resource_type

  for resource_type in container network; do
    if [[ "${resource_type}" == 'container' ]]; then
      found="$(
        docker container ls -aq \
          --filter "label=${LABEL_SCOPE_KEY}=${LABEL_SCOPE_VALUE}"
      )" || die 'container_label_scan_failed'
      [[ -z "${found}" ]] || die 'preexisting_phase2_container'
      found="$(
        docker container ls -aq \
          --filter "label=${LABEL_RUN_KEY}=${RUN_ID}"
      )" || die 'container_run_label_scan_failed'
      [[ -z "${found}" ]] || die 'preexisting_phase2_run_container'
      found="$(
        docker container ls -aq \
          --filter "label=${LEGACY_LABEL_KEY}=${RUN_ID}"
      )" || die 'legacy_container_label_scan_failed'
      [[ -z "${found}" ]] || die 'preexisting_legacy_phase2_container'
    else
      found="$(
        docker network ls -q \
          --filter "label=${LABEL_SCOPE_KEY}=${LABEL_SCOPE_VALUE}"
      )" || die 'network_label_scan_failed'
      [[ -z "${found}" ]] || die 'preexisting_phase2_network'
      found="$(
        docker network ls -q \
          --filter "label=${LABEL_RUN_KEY}=${RUN_ID}"
      )" || die 'network_run_label_scan_failed'
      [[ -z "${found}" ]] || die 'preexisting_phase2_run_network'
      found="$(
        docker network ls -q \
          --filter "label=${LEGACY_LABEL_KEY}=${RUN_ID}"
      )" || die 'legacy_network_label_scan_failed'
      [[ -z "${found}" ]] || die 'preexisting_legacy_phase2_network'
    fi
  done

  for legacy_name in \
    "gm-phase2-pg-${RUN_ID}" \
    "gm-phase2-qdrant-${RUN_ID}"
  do
    ! docker container inspect "${legacy_name}" >/dev/null 2>&1 \
      || die "preexisting_legacy_named_container:${legacy_name}"
  done
  ! docker network inspect "gm-phase2-${RUN_ID}" >/dev/null 2>&1 \
    || die 'preexisting_legacy_named_network'

  if [[ -n "${NETWORK_NAME}" ]]; then
    ! docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1 \
      || die 'preexisting_invocation_named_network'
    ! docker container inspect "${POSTGRES_CONTAINER_NAME}" >/dev/null 2>&1 \
      || die 'preexisting_invocation_postgres_container'
    ! docker container inspect "${QDRANT_CONTAINER_NAME}" >/dev/null 2>&1 \
      || die 'preexisting_invocation_qdrant_container'
  fi

  for port in "${POSTGRES_PORT}" "${QDRANT_PORT}"; do
    found="$(docker container ls -aq --filter "publish=${port}")" \
      || die "container_port_scan_failed:${port}"
    [[ -z "${found}" ]] || die "preexisting_container_port:${port}"
    assert_no_listening_port "${port}"
  done
  assert_ports_bindable
}

initialize_invocation() {
  local compact
  [[ -r /proc/sys/kernel/random/uuid ]] || die 'kernel_uuid_source_missing'
  INVOCATION_ID="$(</proc/sys/kernel/random/uuid)"
  [[ "${INVOCATION_ID}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] \
    || die 'invocation_uuid_invalid'
  compact="${INVOCATION_ID//-/}"
  INVOCATION_TOKEN="${compact:0:12}"
  NETWORK_NAME="gm-p2-${RUN_ID}-${INVOCATION_TOKEN}-net"
  POSTGRES_CONTAINER_NAME="gm-p2-${RUN_ID}-${INVOCATION_TOKEN}-pg"
  QDRANT_CONTAINER_NAME="gm-p2-${RUN_ID}-${INVOCATION_TOKEN}-qd"
}

container_exists() {
  docker container inspect "$1" >/dev/null 2>&1
}

network_exists() {
  docker network inspect "$1" >/dev/null 2>&1
}

assert_container_identity() {
  local actual binding container_id expected_host_port expected_image
  local expected_name expected_target_port label mount_type mounts network_mode

  container_id="$1"
  expected_name="$2"
  expected_image="$3"
  expected_target_port="$4"
  expected_host_port="$5"

  actual="$(
    docker container inspect "${container_id}" --format '{{.Id}}'
  )" || return 1
  [[ "${actual}" == "${container_id}" ]] || return 1
  actual="$(
    docker container inspect "${container_id}" --format '{{.Name}}'
  )" || return 1
  [[ "${actual}" == "/${expected_name}" ]] || return 1
  actual="$(
    docker container inspect "${container_id}" --format '{{.Image}}'
  )" || return 1
  [[ "${actual}" == "${expected_image}" ]] || return 1
  actual="$(
    docker container inspect "${container_id}" \
      --format '{{.HostConfig.RestartPolicy.Name}}'
  )" || return 1
  [[ "${actual}" == 'no' ]] || return 1
  network_mode="$(
    docker container inspect "${container_id}" \
      --format '{{.HostConfig.NetworkMode}}'
  )" || return 1
  [[ "${network_mode}" == "${NETWORK_NAME}" ]] || return 1

  label="$(
    docker container inspect "${container_id}" \
      --format "{{ index .Config.Labels \"${LABEL_SCOPE_KEY}\" }}"
  )" || return 1
  [[ "${label}" == "${LABEL_SCOPE_VALUE}" ]] || return 1
  label="$(
    docker container inspect "${container_id}" \
      --format "{{ index .Config.Labels \"${LABEL_RUN_KEY}\" }}"
  )" || return 1
  [[ "${label}" == "${RUN_ID}" ]] || return 1
  label="$(
    docker container inspect "${container_id}" \
      --format "{{ index .Config.Labels \"${LABEL_INVOCATION_KEY}\" }}"
  )" || return 1
  [[ "${label}" == "${INVOCATION_ID}" ]] || return 1

  binding="$(
    docker container inspect "${container_id}" --format \
      "{{ with (index .HostConfig.PortBindings \"${expected_target_port}/tcp\") }}{{ (index . 0).HostIp }}:{{ (index . 0).HostPort }}{{ end }}"
  )" || return 1
  [[ "${binding}" == "127.0.0.1:${expected_host_port}" ]] || return 1

  mounts="$(
    docker container inspect "${container_id}" \
      --format '{{range .Mounts}}{{println .Type}}{{end}}'
  )" || return 1
  while IFS= read -r mount_type; do
    [[ -z "${mount_type}" || "${mount_type}" == 'tmpfs' ]] || return 1
  done <<< "${mounts}"
}

assert_network_identity() {
  local actual label network_id="$1"

  actual="$(docker network inspect "${network_id}" --format '{{.Id}}')" \
    || return 1
  [[ "${actual}" == "${network_id}" ]] || return 1
  actual="$(docker network inspect "${network_id}" --format '{{.Name}}')" \
    || return 1
  [[ "${actual}" == "${NETWORK_NAME}" ]] || return 1
  actual="$(docker network inspect "${network_id}" --format '{{.Internal}}')" \
    || return 1
  [[ "${actual}" == 'false' ]] || return 1

  label="$(
    docker network inspect "${network_id}" \
      --format "{{ index .Labels \"${LABEL_SCOPE_KEY}\" }}"
  )" || return 1
  [[ "${label}" == "${LABEL_SCOPE_VALUE}" ]] || return 1
  label="$(
    docker network inspect "${network_id}" \
      --format "{{ index .Labels \"${LABEL_RUN_KEY}\" }}"
  )" || return 1
  [[ "${label}" == "${RUN_ID}" ]] || return 1
  label="$(
    docker network inspect "${network_id}" \
      --format "{{ index .Labels \"${LABEL_INVOCATION_KEY}\" }}"
  )" || return 1
  [[ "${label}" == "${INVOCATION_ID}" ]] || return 1
}

cleanup_container() {
  local container_id="$1" expected_host_port="$5" expected_image="$3"
  local expected_name="$2" expected_target_port="$4"

  if [[ -z "${container_id}" ]]; then
    if [[ -n "${expected_name}" ]] && container_exists "${expected_name}"; then
      printf 'PHASE2_CLEANUP_REFUSED=uncaptured_named_container:%s\n' \
        "${expected_name}" >&2
      return 1
    fi
    return 0
  fi
  if container_exists "${container_id}"; then
    if ! assert_container_identity \
      "${container_id}" "${expected_name}" "${expected_image}" \
      "${expected_target_port}" "${expected_host_port}"
    then
      printf 'PHASE2_CLEANUP_REFUSED=container_identity_mismatch:%s\n' \
        "${container_id}" >&2
      return 1
    fi
    docker container rm -fv "${container_id}" >/dev/null || return 1
  elif container_exists "${expected_name}"; then
    printf 'PHASE2_CLEANUP_REFUSED=container_name_reused:%s\n' \
      "${expected_name}" >&2
    return 1
  fi

  ! container_exists "${container_id}" || return 1
  ! container_exists "${expected_name}" || return 1
}

cleanup_network() {
  local network_id="$1"

  if [[ -z "${network_id}" ]]; then
    if [[ -n "${NETWORK_NAME}" ]] && network_exists "${NETWORK_NAME}"; then
      printf 'PHASE2_CLEANUP_REFUSED=uncaptured_named_network:%s\n' \
        "${NETWORK_NAME}" >&2
      return 1
    fi
    return 0
  fi
  if network_exists "${network_id}"; then
    if ! assert_network_identity "${network_id}"; then
      printf 'PHASE2_CLEANUP_REFUSED=network_identity_mismatch:%s\n' \
        "${network_id}" >&2
      return 1
    fi
    docker network rm "${network_id}" >/dev/null || return 1
  elif network_exists "${NETWORK_NAME}"; then
    printf 'PHASE2_CLEANUP_REFUSED=network_name_reused:%s\n' \
      "${NETWORK_NAME}" >&2
    return 1
  fi

  ! network_exists "${network_id}" || return 1
  ! network_exists "${NETWORK_NAME}" || return 1
}

cleanup_temp() {
  [[ -n "${RUN_TMP}" ]] || return 0
  [[ "${RUN_TMP}" =~ ^/tmp/gm-phase2-019fe927-[0-9a-f]{12}\.[A-Za-z0-9]{6}$ ]] \
    || return 1
  if [[ -e "${RUN_TMP}" ]]; then
    [[ -d "${RUN_TMP}" && ! -L "${RUN_TMP}" ]] || return 1
    rm -rf -- "${RUN_TMP}" || return 1
  fi
}

cleanup_all() {
  local failed=0

  cleanup_container \
    "${QDRANT_CONTAINER_ID}" "${QDRANT_CONTAINER_NAME}" \
    "${QDRANT_IMAGE_ID}" 6333 "${QDRANT_PORT}" || failed=1
  cleanup_container \
    "${POSTGRES_CONTAINER_ID}" "${POSTGRES_CONTAINER_NAME}" \
    "${POSTGRES_IMAGE_ID}" 5432 "${POSTGRES_PORT}" || failed=1
  cleanup_network "${NETWORK_ID}" || failed=1
  cleanup_temp || failed=1
  return "${failed}"
}

on_exit() {
  local cleanup_status original_status="$1"
  trap - EXIT INT TERM HUP
  set +e
  cleanup_all
  cleanup_status=$?
  if [[ "${cleanup_status}" -ne 0 ]]; then
    printf 'PHASE2_REFUSED=cleanup_incomplete\n' >&2
    exit 97
  fi
  exit "${original_status}"
}

wait_postgres() {
  local attempt
  for ((attempt = 1; attempt <= 120; attempt += 1)); do
    if docker exec "${POSTGRES_CONTAINER_ID}" \
      pg_isready -h 127.0.0.1 -U postgres -d postgres >/dev/null 2>&1
    then
      return 0
    fi
    sleep 0.25
  done
  die 'postgres_not_ready'
}

wait_qdrant() {
  local attempt
  for ((attempt = 1; attempt <= 120; attempt += 1)); do
    if curl --noproxy '*' -fsS \
      "http://127.0.0.1:${QDRANT_PORT}/healthz" >/dev/null 2>&1
    then
      return 0
    fi
    sleep 0.25
  done
  die 'qdrant_not_ready'
}

capture_runtime_versions() {
  local qdrant_root

  POSTGRES_SERVER_VERSION="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -A -t -v ON_ERROR_STOP=1 -h 127.0.0.1 -U postgres -d postgres \
      -c 'SHOW server_version'
  )" || die 'postgres_version_unreadable'
  [[ "${POSTGRES_SERVER_VERSION}" =~ ^16\.[0-9]+([.][0-9]+)?$ ]] \
    || die 'postgres_version_unexpected'

  qdrant_root="$(
    curl --noproxy '*' -fsS --max-time 5 \
      "http://127.0.0.1:${QDRANT_PORT}/"
  )" || die 'qdrant_version_unreadable'
  QDRANT_SERVER_VERSION="$(
    "${TEST_PYTHON}" - "${qdrant_root}" <<'PY'
import json
import sys


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate Qdrant response key: {key}")
        result[key] = value
    return result


value = json.loads(sys.argv[1], object_pairs_hook=unique_object)
print(value.get("version", ""))
PY
  )" || die 'qdrant_version_invalid'
  [[ "${QDRANT_SERVER_VERSION}" == '1.11.0' ]] \
    || die 'qdrant_version_unexpected'
}

create_resources() {
  assert_candidate_binding
  assert_resource_namespace_empty

  NETWORK_ID="$(
    docker network create \
      --label "${LABEL_SCOPE_KEY}=${LABEL_SCOPE_VALUE}" \
      --label "${LABEL_RUN_KEY}=${RUN_ID}" \
      --label "${LABEL_INVOCATION_KEY}=${INVOCATION_ID}" \
      "${NETWORK_NAME}"
  )" || die 'network_create_failed'
  [[ "${NETWORK_ID}" =~ ^[0-9a-f]{64}$ ]] || die 'network_id_invalid'
  assert_network_identity "${NETWORK_ID}" || die 'network_identity_invalid'

  assert_no_listening_port "${POSTGRES_PORT}"
  POSTGRES_CONTAINER_ID="$(
    docker container create --pull=never --restart=no \
      --name "${POSTGRES_CONTAINER_NAME}" \
      --label "${LABEL_SCOPE_KEY}=${LABEL_SCOPE_VALUE}" \
      --label "${LABEL_RUN_KEY}=${RUN_ID}" \
      --label "${LABEL_INVOCATION_KEY}=${INVOCATION_ID}" \
      --network "${NETWORK_NAME}" \
      --cpus 1 --memory 2g --pids-limit 256 \
      --tmpfs /var/lib/postgresql/data:rw,nosuid,noexec,size=1536m \
      --tmpfs /tmp:rw,nosuid,noexec,size=64m \
      --log-driver local --log-opt max-size=10m --log-opt max-file=1 \
      --log-opt compress=false \
      -p "127.0.0.1:${POSTGRES_PORT}:5432" \
      -e "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}" \
      "${POSTGRES_IMAGE_ID}" \
      -c log_statement=none \
      -c log_connections=off \
      -c log_disconnections=off \
      -c log_parameter_max_length=0 \
      -c log_parameter_max_length_on_error=0
  )" || die 'postgres_container_create_failed'
  [[ "${POSTGRES_CONTAINER_ID}" =~ ^[0-9a-f]{64}$ ]] \
    || die 'postgres_container_id_invalid'
  assert_container_identity \
    "${POSTGRES_CONTAINER_ID}" "${POSTGRES_CONTAINER_NAME}" \
    "${POSTGRES_IMAGE_ID}" 5432 "${POSTGRES_PORT}" \
    || die 'postgres_container_identity_invalid'
  docker container start "${POSTGRES_CONTAINER_ID}" >/dev/null \
    || die 'postgres_container_start_failed'

  assert_no_listening_port "${QDRANT_PORT}"
  QDRANT_CONTAINER_ID="$(
    docker container create --pull=never --restart=no \
      --name "${QDRANT_CONTAINER_NAME}" \
      --label "${LABEL_SCOPE_KEY}=${LABEL_SCOPE_VALUE}" \
      --label "${LABEL_RUN_KEY}=${RUN_ID}" \
      --label "${LABEL_INVOCATION_KEY}=${INVOCATION_ID}" \
      --network "${NETWORK_NAME}" \
      --cpus 1 --memory 2g --pids-limit 256 \
      --tmpfs /qdrant/storage:rw,nosuid,noexec,size=1536m \
      --tmpfs /tmp:rw,nosuid,noexec,size=64m \
      --log-driver local --log-opt max-size=10m --log-opt max-file=1 \
      --log-opt compress=false \
      -p "127.0.0.1:${QDRANT_PORT}:6333" \
      -e QDRANT__TELEMETRY_DISABLED=true \
      "${QDRANT_IMAGE_ID}"
  )" || die 'qdrant_container_create_failed'
  [[ "${QDRANT_CONTAINER_ID}" =~ ^[0-9a-f]{64}$ ]] \
    || die 'qdrant_container_id_invalid'
  assert_container_identity \
    "${QDRANT_CONTAINER_ID}" "${QDRANT_CONTAINER_NAME}" \
    "${QDRANT_IMAGE_ID}" 6333 "${QDRANT_PORT}" \
    || die 'qdrant_container_identity_invalid'
  docker container start "${QDRANT_CONTAINER_ID}" >/dev/null \
    || die 'qdrant_container_start_failed'

  wait_postgres
  wait_qdrant
  capture_runtime_versions
  assert_container_identity \
    "${POSTGRES_CONTAINER_ID}" "${POSTGRES_CONTAINER_NAME}" \
    "${POSTGRES_IMAGE_ID}" 5432 "${POSTGRES_PORT}" \
    || die 'postgres_runtime_identity_invalid'
  assert_container_identity \
    "${QDRANT_CONTAINER_ID}" "${QDRANT_CONTAINER_NAME}" \
    "${QDRANT_IMAGE_ID}" 6333 "${QDRANT_PORT}" \
    || die 'qdrant_runtime_identity_invalid'
}

run_migration() {
  local database="$1" role="$2" lock_name="$3" migration_file="$4"

  assert_candidate_binding
  docker exec -i "${POSTGRES_CONTAINER_ID}" psql \
    -X -1 -v ON_ERROR_STOP=1 -U postgres -d "${database}" \
    -c "SET ROLE ${role}" \
    -c "SET LOCAL lock_timeout = '5s'" \
    -c "SET LOCAL statement_timeout = '60s'" \
    -c "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('${lock_name}', 0))" \
    -f - < "${migration_file}" >/dev/null
}

apply_migrations() {
  run_migration governed_memory governed_memory_owner \
    governed_memory_roles_preflight_0001 "${MIGRATIONS}/roles_preflight.pgsql"
  run_migration governed_memory governed_memory_owner \
    governed_memory_foundation_0001 \
    "${MIGRATIONS}/0001_foundation/forward.pgsql"
  run_migration phase2_conversation sage \
    governed_memory_conversation_bridge_0002 \
    "${MIGRATIONS}/0002_conversation_bridge/forward.pgsql"
}

rollback_migrations() {
  run_migration phase2_conversation sage \
    governed_memory_conversation_bridge_0002 \
    "${MIGRATIONS}/0002_conversation_bridge/rollback.pgsql"
  run_migration governed_memory governed_memory_owner \
    governed_memory_foundation_0001 \
    "${MIGRATIONS}/0001_foundation/rollback.pgsql"
}

bootstrap_postgres() {
  local marker
  assert_candidate_binding
  docker exec -i "${POSTGRES_CONTAINER_ID}" psql \
    -X -v ON_ERROR_STOP=1 -U postgres -d postgres \
    < "${SCRIPT_DIR}/postgres_bootstrap.pgsql" >/dev/null

  marker="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -A -t -v ON_ERROR_STOP=1 -U postgres -d postgres \
      -c "SELECT pg_catalog.shobj_description(oid, 'pg_database') FROM pg_catalog.pg_database WHERE datname = 'governed_memory'"
  )" || die 'governed_memory_marker_unreadable'
  [[ "${marker}" == "governed-memory-phase2-disposable:${RUN_ID}" ]] \
    || die 'governed_memory_marker_invalid'
  marker="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -A -t -v ON_ERROR_STOP=1 -U postgres -d postgres \
      -c "SELECT pg_catalog.shobj_description(oid, 'pg_database') FROM pg_catalog.pg_database WHERE datname = 'phase2_conversation'"
  )" || die 'conversation_marker_unreadable'
  [[ "${marker}" == "governed-memory-phase2-disposable:${RUN_ID}" ]] \
    || die 'conversation_marker_invalid'
}

normalized_schema_dump() {
  local database="$1" destination="$2"

  docker exec "${POSTGRES_CONTAINER_ID}" pg_dump \
    -U postgres -d "${database}" --schema-only \
    | awk '
        /^-- Dumped from database version / { next }
        /^-- Dumped by pg_dump version / { next }
        /^\\restrict / { next }
        /^\\unrestrict / { next }
        { sub(/[[:space:]]+$/, ""); print }
      ' > "${destination}"
  [[ -s "${destination}" ]] || die "logical_dump_empty:${database}"
}

sha256_file() {
  local digest
  digest="$(sha256sum "$1")" || die 'sha256_failed'
  digest="${digest%% *}"
  [[ "${digest}" =~ ^[0-9a-f]{64}$ ]] || die 'sha256_invalid'
  printf '%s\n' "${digest}"
}

assert_rollback_absence() {
  local result

  result="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -A -t -v ON_ERROR_STOP=1 -U postgres -d governed_memory \
      -c "SELECT CASE WHEN pg_catalog.to_regnamespace('memory') IS NULL AND pg_catalog.to_regnamespace('memory_private') IS NULL THEN 'absent' ELSE 'present' END"
  )" || die 'foundation_absence_query_failed'
  [[ "${result}" == 'absent' ]] || die 'foundation_rollback_objects_remain'

  result="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -A -t -v ON_ERROR_STOP=1 -U postgres -d phase2_conversation \
      -c "SELECT CASE WHEN pg_catalog.to_regnamespace('memory_ingest_private') IS NULL AND pg_catalog.to_regclass('public.memory_ingest_outbox') IS NULL THEN 'absent' ELSE 'present' END"
  )" || die 'bridge_absence_query_failed'
  [[ "${result}" == 'absent' ]] || die 'bridge_rollback_objects_remain'
}

verify_apply_rollback_reapply() {
  local bridge_first="${RUN_TMP}/bridge-first.sql"
  local bridge_second="${RUN_TMP}/bridge-second.sql"
  local foundation_first="${RUN_TMP}/foundation-first.sql"
  local foundation_second="${RUN_TMP}/foundation-second.sql"

  apply_migrations
  normalized_schema_dump governed_memory "${foundation_first}"
  normalized_schema_dump phase2_conversation "${bridge_first}"

  rollback_migrations
  assert_rollback_absence

  apply_migrations
  normalized_schema_dump governed_memory "${foundation_second}"
  normalized_schema_dump phase2_conversation "${bridge_second}"

  cmp -s "${foundation_first}" "${foundation_second}" \
    || die 'foundation_reapply_logical_dump_mismatch'
  cmp -s "${bridge_first}" "${bridge_second}" \
    || die 'bridge_reapply_logical_dump_mismatch'
  FOUNDATION_DUMP_SHA256="$(sha256_file "${foundation_second}")"
  BRIDGE_DUMP_SHA256="$(sha256_file "${bridge_second}")"
  printf 'PHASE2_MIGRATION_CYCLE=apply-rollback-absence-reapply-equivalent\n'
}

validate_integration_receipt() {
  local receipt_json="$1" validated

  validated="$(
    "${TEST_PYTHON}" - "${receipt_json}" <<'PY'
from __future__ import annotations

import hashlib
import json
import re
import sys


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate receipt key: {key}")
        result[key] = value
    return result


receipt = json.loads(sys.argv[1], object_pairs_hook=unique_object)
if receipt.get("schema") != "governed-memory-phase2-integration-receipt-v1":
    raise ValueError("unexpected integration receipt schema")
if receipt.get("provider_external_calls") != 0:
    raise ValueError("integration receipt reports an external provider call")
if receipt.get("production_data_read") is not False:
    raise ValueError("integration receipt does not deny production reads")

required_hashes = (
    "rebuild_manifest_sha256",
    "review_surface_sha256",
    "answer_binding_sha256",
    "deletion_receipt_sha256",
)
for key in required_hashes:
    if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(key, ""))):
        raise ValueError(f"invalid receipt digest: {key}")

canonical = json.dumps(
    receipt,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
).encode("utf-8")
print(hashlib.sha256(canonical).hexdigest())
PY
  )" || die 'integration_receipt_invalid'
  [[ "${validated}" =~ ^[0-9a-f]{64}$ ]] \
    || die 'integration_receipt_sha256_invalid'
  INTEGRATION_RECEIPT_SHA256="${validated}"
}

run_integration() {
  local receipt_count receipt_json status test_log="${RUN_TMP}/integration.log"

  assert_candidate_binding
  assert_container_identity \
    "${POSTGRES_CONTAINER_ID}" "${POSTGRES_CONTAINER_NAME}" \
    "${POSTGRES_IMAGE_ID}" 5432 "${POSTGRES_PORT}" \
    || die 'postgres_identity_drift_before_test'
  assert_container_identity \
    "${QDRANT_CONTAINER_ID}" "${QDRANT_CONTAINER_NAME}" \
    "${QDRANT_IMAGE_ID}" 6333 "${QDRANT_PORT}" \
    || die 'qdrant_identity_drift_before_test'

  set +e
  (
    cd "${ROOT}"
    timeout --signal=TERM --kill-after=10s 600s env -i \
      PATH="${PATH}" \
      LC_ALL=C.UTF-8 \
      LANG=C.UTF-8 \
      TMPDIR="${RUN_TMP}" \
      NO_PROXY=127.0.0.1,localhost \
      no_proxy=127.0.0.1,localhost \
      PYTHONUTF8=1 \
      PYTHONHASHSEED=0 \
      PYTHONDONTWRITEBYTECODE=1 \
      PYTHONPATH="${ROOT}" \
      GM_PHASE2_RUN=1 \
      GM_PHASE2_POSTGRES_HOST=127.0.0.1 \
      GM_PHASE2_POSTGRES_PORT="${POSTGRES_PORT}" \
      GM_PHASE2_QDRANT_URL="http://127.0.0.1:${QDRANT_PORT}" \
      "${TEST_PYTHON}" -B -m unittest \
        tests.memory_integration.test_phase2_vertical_slice.Phase2VerticalSliceTests.test_real_chat_a_to_chat_b_rebuild_and_deletion \
        -v
  ) > "${test_log}" 2>&1
  status=$?
  set -e

  if [[ "${status}" -ne 0 ]]; then
    sed '/^PHASE2_VERTICAL_SLICE_RECEIPT=/d' "${test_log}" >&2
    die "integration_failed:${status}"
  fi

  receipt_count="$(
    awk '/^PHASE2_VERTICAL_SLICE_RECEIPT=/{count += 1} END{print count + 0}' \
      "${test_log}"
  )"
  [[ "${receipt_count}" == '1' ]] || die 'integration_receipt_count_invalid'
  receipt_json="$(
    awk -F= '/^PHASE2_VERTICAL_SLICE_RECEIPT=/{sub(/^[^=]*=/, ""); print}' \
      "${test_log}"
  )"
  validate_integration_receipt "${receipt_json}"
  sed '/^PHASE2_VERTICAL_SLICE_RECEIPT=/d' "${test_log}"
  printf 'PHASE2_INTEGRATION=passed synthetic_provider_external_calls=0 production_data_read=false\n'
}

preflight() {
  local command

  [[ "${1:-full}" == 'full' && "$#" -le 1 ]] \
    || die 'usage:run_disposable_phase2.sh_full_only'
  [[ "$(hostname -s)" == "${EXPECTED_HOST}" ]] || die 'wrong_host'
  [[ "$(id -un)" == "${EXPECTED_USER}" ]] || die 'wrong_user'
  [[ "$(id -u)" -ne 0 ]] || die 'root_execution_refused'
  [[ "${ROOT}" == "${EXPECTED_ROOT}" ]] || die 'wrong_candidate_root'
  [[ "${GM_PHASE2_DISPOSABLE_AUTHORIZATION:-}" == "${AUTHORIZATION_VALUE}" ]] \
    || die 'explicit_disposable_authorization_missing'
  [[ "${POSTGRES_PORT}" != '5432' && "${QDRANT_PORT}" != '6333' ]] \
    || die 'production_port_constant_detected'

  for command in \
    awk chmod cmp curl docker env flock git hostname id mapfile mktemp \
    rm sed sha256sum sleep ss stat timeout
  do
    require_command "${command}"
  done
  [[ -x "${TEST_PYTHON}" ]] || die 'test_python_missing'
  docker version --format '{{.Server.Version}}' >/dev/null \
    || die 'docker_daemon_unavailable'

  acquire_lock
  assert_candidate_binding
  verify_migration_manifest
  assert_image_binding
  assert_resource_namespace_empty
  initialize_invocation
}

full() {
  preflight "$@"

  trap 'on_exit $?' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  RUN_TMP="$(
    mktemp -d "/tmp/gm-phase2-${RUN_ID}-${INVOCATION_TOKEN}.XXXXXX"
  )" || die 'temporary_directory_create_failed'
  [[ "${RUN_TMP}" =~ ^/tmp/gm-phase2-019fe927-[0-9a-f]{12}\.[A-Za-z0-9]{6}$ ]] \
    || die 'temporary_directory_path_invalid'

  create_resources
  bootstrap_postgres
  verify_apply_rollback_reapply
  run_integration

  cleanup_all || die 'cleanup_incomplete'
  assert_resource_namespace_empty
  assert_no_listening_port "${POSTGRES_PORT}"
  assert_no_listening_port "${QDRANT_PORT}"
  assert_candidate_binding

  trap - EXIT INT TERM HUP
  printf 'PHASE2_DISPOSABLE_RECEIPT={"branch":"%s","candidate_head":"%s","candidate_tree":"%s","candidate_unchanged":true,"external_calls":0,"provider_external_calls":0,"production_data_read":false,"production_resources_changed":false,"docker_persistent_mounts":false,"ports_released":true,"resources_removed":true,"result":"passed","run_id":"%s","invocation_id":"%s","network_id":"%s","postgres_container_id":"%s","qdrant_container_id":"%s","postgres_image_id":"%s","qdrant_image_id":"%s","qdrant_image_digest":"%s","postgres_server_version":"%s","qdrant_server_version":"%s","manifest_sha256":"%s","foundation_logical_dump_sha256":"%s","bridge_logical_dump_sha256":"%s","integration_receipt_sha256":"%s","rollback_reapply":"passed","schema_version":"governed-memory-phase2-disposable-run-v2"}\n' \
    "${EXPECTED_BRANCH}" "${EXPECTED_HEAD}" "${EXPECTED_TREE}" \
    "${RUN_ID}" "${INVOCATION_ID}" "${NETWORK_ID}" \
    "${POSTGRES_CONTAINER_ID}" "${QDRANT_CONTAINER_ID}" \
    "${POSTGRES_IMAGE_ID}" "${QDRANT_IMAGE_ID}" "${QDRANT_IMAGE_DIGEST}" \
    "${POSTGRES_SERVER_VERSION}" "${QDRANT_SERVER_VERSION}" \
    "${MIGRATION_MANIFEST_SHA256}" \
    "${FOUNDATION_DUMP_SHA256}" "${BRIDGE_DUMP_SHA256}" \
    "${INTEGRATION_RECEIPT_SHA256}"
}

full "$@"

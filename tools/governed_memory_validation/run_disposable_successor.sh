#!/usr/bin/env bash
set -Eeuo pipefail

# Server: seebx backend only.
#
# This is a one-shot disposable verifier. It refuses production ports, stale
# Governed Memory successor resources, dirty/unbound candidate bytes, persistent Docker mounts,
# ambient test environment variables, and every mode other than `full`.
#
# Required explicit authorization and immutable candidate binding:
#   GM_VALIDATION_DISPOSABLE_AUTHORIZATION='019fe927:SUCCESSOR_DISPOSABLE_ONLY:NO_PRODUCTION_DATA:NO_PROVIDER_CALLS'
#   GM_VALIDATION_EXPECTED_ROOT='<exact absolute candidate worktree>'
#   GM_VALIDATION_EXPECTED_BRANCH='<exact candidate branch>'
#   GM_VALIDATION_EXPECTED_HEAD='<exact 40-character candidate commit>'
#   GM_VALIDATION_EXPECTED_TREE='<exact 40-character candidate tree>'
# Preliminary migration proof only, before validation metadata is promoted:
#   GM_VALIDATION_PRELIMINARY_MIGRATION_PROOF='019fe927:PRELIMINARY_MIGRATION_PROOF_ONLY:NO_PRODUCTION_DATA:NO_PROVIDER_CALLS'
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
readonly EXPECTED_BASE='7693d9db459f81f4d89e680108867ce31dd4c7ed'
readonly RUN_ID='019fe927'
readonly AUTHORIZATION_VALUE='019fe927:SUCCESSOR_DISPOSABLE_ONLY:NO_PRODUCTION_DATA:NO_PROVIDER_CALLS'
readonly PRELIMINARY_PROOF_AUTHORIZATION_VALUE='019fe927:PRELIMINARY_MIGRATION_PROOF_ONLY:NO_PRODUCTION_DATA:NO_PROVIDER_CALLS'
readonly EXPECTED_MANIFEST_SHA256='bb87fc8e585a879c07fcdff3313f1d7628fdc850edab2008e1937bf125c7cace'
readonly EXPECTED_RUNTIME_PACKAGES_SHA256='ed9273d6bd6dad6cf5680c478dff1beab453f66ab607914994fe8dc2b9d4e882'
readonly EXPECTED_RUNTIME_BUILD_RECEIPT_SHA256='ecedbab61970ac00cf40431073b5cbd359afed289cf90e951a41eb0b4c081e69'

readonly LABEL_SCOPE_KEY='com.verbalsage.governed-memory.scope'
readonly LABEL_SCOPE_VALUE='successor-disposable'
readonly LABEL_RUN_KEY='com.verbalsage.governed-memory.run-id'
readonly LABEL_INVOCATION_KEY='com.verbalsage.governed-memory.invocation-id'

readonly POSTGRES_IMAGE='postgres:16-alpine'
readonly POSTGRES_IMAGE_ID='sha256:de3a4eab8fdfa507ea92aac488b916b08089e515db49b055fe71dfa271ba3a28'
readonly QDRANT_IMAGE='qdrant/qdrant@sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc'
readonly QDRANT_IMAGE_ID='sha256:92c4050629efe895f87dafd2830f1cd4d0532bc9967b777cab979ebda71612b3'
readonly QDRANT_IMAGE_DIGEST='qdrant/qdrant@sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc'
readonly POSTGRES_PORT='55443'
readonly QDRANT_PORT='6339'
readonly JWKS_PORT='18091'
readonly API_PORT='18092'
readonly POSTGRES_PASSWORD='successor_disposable_only'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd -P)"
MIGRATIONS="${ROOT}/governed-memory-migrations"
RUNTIME_PACKAGES="${SCRIPT_DIR}/runtime_packages.json"
RUNTIME_LOCK="${ROOT}/ops/governed_memory/runtime-requirements.lock"
BUILD_LOCK="${ROOT}/ops/governed_memory/build-requirements.lock"
RUNTIME_BUILD_RECEIPT="${ROOT}/ops/governed_memory/runtime_build_receipt.json"
VALIDATION_RUNTIME_PYTHON="${GM_VALIDATION_RUNTIME_PYTHON:-}"
PRELIMINARY_PROOF_AUTHORIZATION="${GM_VALIDATION_PRELIMINARY_MIGRATION_PROOF:-}"
TEST_PYTHON=''
readonly SCRIPT_DIR ROOT MIGRATIONS RUNTIME_PACKAGES RUNTIME_LOCK BUILD_LOCK
readonly RUNTIME_BUILD_RECEIPT
readonly VALIDATION_RUNTIME_PYTHON
readonly PRELIMINARY_PROOF_AUTHORIZATION

EXPECTED_ROOT="${GM_VALIDATION_EXPECTED_ROOT:-}"
EXPECTED_BRANCH="${GM_VALIDATION_EXPECTED_BRANCH:-}"
EXPECTED_HEAD="${GM_VALIDATION_EXPECTED_HEAD:-}"
EXPECTED_TREE="${GM_VALIDATION_EXPECTED_TREE:-}"
readonly EXPECTED_ROOT EXPECTED_BRANCH EXPECTED_HEAD EXPECTED_TREE

INVOCATION_ID=''
INVOCATION_TOKEN=''
NETWORK_NAME=''
POSTGRES_CONTAINER_NAME=''
QDRANT_CONTAINER_NAME=''
NETWORK_ID=''
POSTGRES_CONTAINER_ID=''
QDRANT_CONTAINER_ID=''
POSTGRES_CONTAINER_IP=''
QDRANT_CONTAINER_IP=''
RUN_TMP=''
RUN_TMP_IDENTITY=''
MIGRATION_MANIFEST_SHA256=''
RUNTIME_PACKAGES_SHA256=''
RUNTIME_LOCK_SHA256=''
SOURCE_TREE_SHA256=''
RUNTIME_BUILD_RECEIPT_SHA256=''
FOUNDATION_DUMP_SHA256=''
BRIDGE_DUMP_SHA256=''
INTEGRATION_RECEIPT_SHA256=''
INTEGRATION_RECEIPT_JSON=''
CONNECT_TRACE_SHA256=''
POSTGRES_SERVER_VERSION=''
QDRANT_SERVER_VERSION=''
SERVICE_TOKEN=''

die() {
  printf 'SUCCESSOR_REFUSED=%s\n' "$1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "command_missing:$1"
}

bind_validation_runtime_python() {
  local canonical lock_sha receipt runtime_gid runtime_mode runtime_root runtime_sha runtime_uid source_receipt source_sha

  [[ "${VALIDATION_RUNTIME_PYTHON}" == /* ]] \
    || die 'validation_runtime_python_not_absolute'
  canonical="$(realpath -e -- "${VALIDATION_RUNTIME_PYTHON}")" \
    || die 'validation_runtime_python_unresolvable'
  [[ "${canonical}" == "${VALIDATION_RUNTIME_PYTHON}" ]] \
    || die 'validation_runtime_python_not_real_path'
  [[ -f "${canonical}" && -x "${canonical}" && ! -L "${canonical}" ]] \
    || die 'validation_runtime_python_invalid_file'
  [[ "${canonical}" != /opt/chat-memory/* \
     && "${canonical}" != "${ROOT}"/* \
     && "${canonical}" != */.local/* \
     && "${canonical}" != */site-packages/* ]] \
    || die 'validation_runtime_python_old_or_user_environment'
  [[ -f "${RUNTIME_LOCK}" && ! -L "${RUNTIME_LOCK}" ]] \
    || die 'runtime_lock_invalid'
  lock_sha="$(sha256sum "${RUNTIME_LOCK}")" || die 'runtime_lock_sha256_failed'
  lock_sha="${lock_sha%% *}"
  [[ "${lock_sha}" =~ ^[0-9a-f]{64}$ ]] || die 'runtime_lock_sha256_invalid'
  [[ "${canonical}" =~ ^/tmp/governed-memory-successor-runtime-([0-9a-f]{64})-([0-9a-f]{64})/bin/python$ ]] \
    || die 'validation_runtime_python_not_invocation_owned'
  runtime_sha="${BASH_REMATCH[1]}"
  source_sha="${BASH_REMATCH[2]}"
  [[ "${runtime_sha}" == "${lock_sha}" ]] \
    || die 'validation_runtime_python_lock_binding_mismatch'
  runtime_root="${canonical%/bin/python}"
  [[ -d "${runtime_root}" && ! -L "${runtime_root}" ]] \
    || die 'validation_runtime_root_invalid'
  runtime_uid="$(stat -c '%u' -- "${runtime_root}")" \
    || die 'validation_runtime_root_owner_unreadable'
  runtime_gid="$(stat -c '%g' -- "${runtime_root}")" \
    || die 'validation_runtime_root_group_unreadable'
  [[ "${runtime_uid}" == "$(id -u)" && "${runtime_gid}" == "$(id -g)" ]] \
    || die 'validation_runtime_root_not_invocation_owned'
  runtime_mode="$(stat -c '%a' -- "${runtime_root}")" \
    || die 'validation_runtime_root_mode_unreadable'
  (( (8#${runtime_mode} & 8#022) == 0 )) \
    || die 'validation_runtime_root_group_or_world_writable'
  [[ "$(stat -c '%u:%g' -- "${canonical}")" == "${runtime_uid}:${runtime_gid}" ]] \
    || die 'validation_runtime_python_not_invocation_owned'
  receipt="$(
    "${canonical}" -I -B - "${canonical}" "${runtime_root}" <<'PY'
import platform
from pathlib import Path
import site
import sys

expected_executable = Path(sys.argv[1])
expected_prefix = Path(sys.argv[2])
if platform.python_implementation() != "CPython":
    raise SystemExit("runtime is not CPython")
if platform.python_version() != "3.12.3":
    raise SystemExit("runtime Python version differs")
if site.ENABLE_USER_SITE is not False:
    raise SystemExit("runtime user site is enabled")
if sys.prefix == sys.base_prefix:
    raise SystemExit("runtime interpreter is not a virtual environment")
if Path(sys.executable) != expected_executable:
    raise SystemExit("runtime executable differs")
if Path(sys.prefix) != expected_prefix:
    raise SystemExit("runtime prefix differs")
print(platform.python_implementation())
print(platform.python_version())
PY
  )" || die 'validation_runtime_python_identity_failed'
  [[ "${receipt}" == $'CPython\n3.12.3' ]] \
    || die 'validation_runtime_python_receipt_invalid'
  source_receipt="$(
    "${canonical}" -I -B - "${ROOT}" <<'PY'
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import sys


ALLOWED_NON_PYTHON_SOURCE_PATHS = {
    "provider_assets/extraction_instructions.txt",
    "provider_assets/extraction_output.schema.json",
    "provider_assets/predicate_catalog.json",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


source_root = Path(sys.argv[1])
if (
    not source_root.is_absolute()
    or not source_root.is_dir()
    or source_root.is_symlink()
):
    raise SystemExit("source root invalid")
package_root = source_root / "rag_engine" / "governed_memory"
required = (
    ("pyproject.toml", source_root / "pyproject.toml"),
    ("rag_engine/__init__.py", source_root / "rag_engine" / "__init__.py"),
)
material: list[tuple[str, str]] = []
for relative_text, path in required:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SystemExit("source packaging file invalid") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not 0 <= metadata.st_size <= 128 * 1024
    ):
        raise SystemExit("source packaging file invalid")
    material.append((relative_text, sha256(path)))
observed_assets: set[str] = set()
for path in sorted(package_root.rglob("*")):
    relative = path.relative_to(package_root)
    relative_text = relative.as_posix()
    if "__pycache__" in relative.parts:
        continue
    if path.is_symlink():
        raise SystemExit("source package symlink")
    if path.is_dir():
        continue
    if not path.is_file():
        raise SystemExit("source package inventory invalid")
    if path.suffix != ".py":
        if relative_text not in ALLOWED_NON_PYTHON_SOURCE_PATHS:
            raise SystemExit("source package inventory invalid")
        observed_assets.add(relative_text)
    material.append(
        (f"rag_engine/governed_memory/{relative_text}", sha256(path))
    )
material.sort()
if not material:
    raise SystemExit("source package inventory empty")
if observed_assets != ALLOWED_NON_PYTHON_SOURCE_PATHS:
    raise SystemExit("source package inventory invalid")
encoded = json.dumps(
    material,
    ensure_ascii=True,
    separators=(",", ":"),
).encode("ascii")
print(hashlib.sha256(encoded).hexdigest())
PY
  )" || die 'validation_runtime_source_inventory_failed'
  [[ "${source_receipt}" == "${source_sha}" ]] \
    || die 'validation_runtime_python_source_binding_mismatch'
  TEST_PYTHON="${canonical}"
  RUNTIME_LOCK_SHA256="${lock_sha}"
  SOURCE_TREE_SHA256="${source_sha}"
  readonly TEST_PYTHON
  readonly SOURCE_TREE_SHA256
}

verify_runtime_build_receipt() {
  local receipt_sha

  [[ -f "${RUNTIME_BUILD_RECEIPT}" && ! -L "${RUNTIME_BUILD_RECEIPT}" ]] \
    || die 'runtime_build_receipt_invalid_file'
  receipt_sha="$(
    "${TEST_PYTHON}" -I -B - \
      "${RUNTIME_BUILD_RECEIPT}" "${TEST_PYTHON}" \
      "${SOURCE_TREE_SHA256}" "${RUNTIME_LOCK}" "${BUILD_LOCK}" \
      "${RUNTIME_PACKAGES}" <<'PY'
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate runtime build receipt key")
        result[key] = value
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


receipt_path = Path(sys.argv[1])
candidate_python = Path(sys.argv[2])
source_tree_sha256 = sys.argv[3]
runtime_lock = Path(sys.argv[4])
build_lock = Path(sys.argv[5])
runtime_packages_path = Path(sys.argv[6])
raw = receipt_path.read_bytes()
if not raw or len(raw) > 128 * 1024:
    raise ValueError("runtime build receipt size invalid")
receipt = json.loads(raw.decode("ascii"), object_pairs_hook=unique_object)
runtime_packages_manifest = json.loads(
    runtime_packages_path.read_bytes().decode("ascii"),
    object_pairs_hook=unique_object,
)
expected_keys = {
    "build_lock",
    "build_lock_sha256",
    "candidate_python",
    "candidate_python_is_symlink",
    "candidate_python_sha256",
    "legacy_environment_imported",
    "network_calls",
    "persistent_resources_created",
    "pip_present",
    "platform",
    "production_state_changed",
    "project_distribution",
    "project_wheel",
    "project_wheel_sha256",
    "provider_calls",
    "python_version",
    "runtime_lock",
    "runtime_lock_sha256",
    "runtime_package_count",
    "runtime_packages",
    "schema_version",
    "setuptools_present",
    "source_tree_sha256",
    "user_site_enabled",
    "wheel_present",
}
if not isinstance(receipt, dict) or set(receipt) != expected_keys:
    raise ValueError("runtime build receipt is not closed")
if (
    receipt["schema_version"] != "governed-memory-runtime-build-receipt-v1"
    or receipt["candidate_python"] != str(candidate_python)
    or receipt["candidate_python_sha256"] != sha256(candidate_python)
    or receipt["python_version"] != "3.12.3"
    or receipt["platform"] != "linux_x86_64"
    or receipt["runtime_lock"] != "ops/governed_memory/runtime-requirements.lock"
    or receipt["runtime_lock_sha256"] != sha256(runtime_lock)
    or receipt["build_lock"] != "ops/governed_memory/build-requirements.lock"
    or receipt["build_lock_sha256"] != sha256(build_lock)
    or receipt["source_tree_sha256"] != source_tree_sha256
    or receipt["runtime_package_count"] != 19
    or receipt["project_distribution"]
    != {"name": "governed-memory-successor", "version": "0.0.0"}
    or receipt["candidate_python_is_symlink"] is not False
    or receipt["pip_present"] is not False
    or receipt["setuptools_present"] is not False
    or receipt["wheel_present"] is not False
    or receipt["user_site_enabled"] is not False
    or receipt["legacy_environment_imported"] is not False
    or receipt["network_calls"] != 0
    or receipt["provider_calls"] != 0
    or receipt["persistent_resources_created"] is not False
    or receipt["production_state_changed"] is not False
):
    raise ValueError("runtime build receipt differs")
project_wheel = Path(receipt["project_wheel"])
expected_build_root = Path(
    "/tmp/governed-memory-successor-build-"
    f"{receipt['build_lock_sha256']}-{source_tree_sha256}"
)
if (
    project_wheel.name != "governed_memory_successor-0.0.0-py3-none-any.whl"
    or project_wheel.parent != expected_build_root / "dist"
    or not project_wheel.is_file()
    or project_wheel.is_symlink()
    or receipt["project_wheel_sha256"] != sha256(project_wheel)
):
    raise ValueError("runtime build wheel differs")
if (
    not isinstance(receipt["runtime_packages"], dict)
    or len(receipt["runtime_packages"]) != 19
    or any(
        not isinstance(name, str) or not isinstance(version, str)
        for name, version in receipt["runtime_packages"].items()
    )
):
    raise ValueError("runtime build package inventory invalid")
if not isinstance(runtime_packages_manifest, dict) or not isinstance(
    runtime_packages_manifest.get("packages"), dict
):
    raise ValueError("runtime package manifest invalid")
canonical_manifest_packages = {
    re.sub(r"[-_.]+", "-", name).lower(): version
    for name, version in runtime_packages_manifest["packages"].items()
}
if receipt["runtime_packages"] != canonical_manifest_packages:
    raise ValueError("runtime build package receipt differs")
if re.fullmatch(r"[0-9a-f]{64}", source_tree_sha256) is None:
    raise ValueError("runtime source hash invalid")
print(hashlib.sha256(raw).hexdigest())
PY
  )" || die 'runtime_build_receipt_verification_failed'
  [[ "${receipt_sha}" =~ ^[0-9a-f]{64}$ ]] \
    || die 'runtime_build_receipt_sha256_invalid'
  [[ "${receipt_sha}" == "${EXPECTED_RUNTIME_BUILD_RECEIPT_SHA256}" ]] \
    || die 'runtime_build_receipt_sha256_mismatch'
  RUNTIME_BUILD_RECEIPT_SHA256="${receipt_sha}"
  readonly RUNTIME_BUILD_RECEIPT_SHA256
}

verify_installed_successor_source() {
  local observed

  observed="$(
    "${TEST_PYTHON}" -I -B - \
      "${ROOT}/rag_engine/governed_memory" <<'PY'
from __future__ import annotations

import hashlib
import importlib
import importlib.util
from importlib import metadata
import json
from pathlib import Path
import pkgutil
import sys


ALLOWED_NON_PYTHON_SOURCE_PATHS = {
    "provider_assets/extraction_instructions.txt",
    "provider_assets/extraction_output.schema.json",
    "provider_assets/predicate_catalog.json",
}
MAX_SUCCESSOR_MODULES = 128
SUCCESSOR_PACKAGE = "rag_engine.governed_memory"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def material(package_root: Path) -> list[tuple[str, str]]:
    if (
        not package_root.is_absolute()
        or not package_root.is_dir()
        or package_root.is_symlink()
    ):
        raise ValueError("package root invalid")
    result: list[tuple[str, str]] = []
    observed_assets: set[str] = set()
    for path in sorted(package_root.rglob("*")):
        relative = path.relative_to(package_root)
        relative_text = relative.as_posix()
        if "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError("package symlink invalid")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("package inventory invalid")
        if path.suffix != ".py":
            if relative_text not in ALLOWED_NON_PYTHON_SOURCE_PATHS:
                raise ValueError("package inventory invalid")
            observed_assets.add(relative_text)
        result.append((relative_text, sha256(path)))
    if not result:
        raise ValueError("package inventory empty")
    if observed_assets != ALLOWED_NON_PYTHON_SOURCE_PATHS:
        raise ValueError("package inventory invalid")
    return result


def tree_sha256(value: list[tuple[str, str]]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


source_root = Path(sys.argv[1])
distribution = metadata.distribution("governed-memory-successor")
installed_root = Path(
    distribution.locate_file("rag_engine/governed_memory")
).resolve(strict=True)
prefix = Path(sys.prefix).resolve(strict=True)
if not installed_root.is_relative_to(prefix):
    raise ValueError("installed successor is outside candidate runtime")
package = importlib.import_module(SUCCESSOR_PACKAGE)
module_names = [SUCCESSOR_PACKAGE]
module_names.extend(
    item.name
    for item in pkgutil.walk_packages(
        package.__path__,
        SUCCESSOR_PACKAGE + ".",
    )
)
module_names = sorted(set(module_names))
if not module_names or len(module_names) > MAX_SUCCESSOR_MODULES:
    raise ValueError("installed successor module inventory invalid")
for module_name in module_names:
    importlib.import_module(module_name)
successor_modules = sorted(
    (name, module)
    for name, module in sys.modules.items()
    if name == SUCCESSOR_PACKAGE or name.startswith(SUCCESSOR_PACKAGE + ".")
)
if not successor_modules or len(successor_modules) > MAX_SUCCESSOR_MODULES:
    raise ValueError("installed successor import inventory invalid")
for _name, module in successor_modules:
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str):
        raise ValueError("installed successor module origin missing")
    try:
        resolved = Path(module_file).resolve(strict=True)
    except OSError as exc:
        raise ValueError("installed successor module origin invalid") from exc
    if not resolved.is_relative_to(prefix):
        raise ValueError("installed successor module is outside candidate runtime")
if any(
    name.startswith("rag_engine.")
    and name != SUCCESSOR_PACKAGE
    and not name.startswith(SUCCESSOR_PACKAGE + ".")
    for name in sys.modules
):
    raise ValueError("installed successor imported external rag_engine module")
if importlib.util.find_spec("openai") is not None:
    raise ValueError("openai unexpectedly importable")
if any(name == "openai" or name.startswith("openai.") for name in sys.modules):
    raise ValueError("openai unexpectedly loaded")
source_material = material(source_root)
installed_material = material(installed_root)
if source_material != installed_material:
    raise ValueError("installed successor source differs")
source_hash = tree_sha256(source_material)
if tree_sha256(installed_material) != source_hash:
    raise ValueError("installed successor source hash differs")
print(source_hash)
PY
  )" || die 'installed_successor_source_verification_failed'
  [[ "${observed}" =~ ^[0-9a-f]{64}$ ]] \
    || die 'installed_successor_source_receipt_invalid'
}

assert_ambient_authority_clean() {
  local variable_name

  while IFS= read -r variable_name; do
    case "${variable_name}" in
      DOCKER_*|GIT_*|PYTHON*)
        die "ambient_authority_variable:${variable_name}"
        ;;
    esac
  done < <(compgen -A variable)
}

bind_local_docker_authority() {
  local context daemon_name endpoint

  [[ -S /var/run/docker.sock && ! -L /var/run/docker.sock ]] \
    || die 'local_docker_socket_invalid'
  readonly DOCKER_HOST='unix:///var/run/docker.sock'
  export DOCKER_HOST

  context="$(docker context show)" || die 'docker_context_unreadable'
  [[ "${context}" == 'default' ]] || die 'docker_context_not_default'
  endpoint="$(
    docker context inspect default --format '{{.Endpoints.docker.Host}}'
  )" || die 'docker_endpoint_unreadable'
  [[ "${endpoint}" == "${DOCKER_HOST}" ]] || die 'docker_endpoint_not_local'
  daemon_name="$(docker info --format '{{.Name}}')" \
    || die 'docker_daemon_identity_unreadable'
  [[ "${daemon_name}" == "${EXPECTED_HOST}" ]] \
    || die 'docker_daemon_identity_mismatch'
}

verify_runtime_packages() {
  local receipt
  local -a fields

  [[ -f "${RUNTIME_PACKAGES}" && ! -L "${RUNTIME_PACKAGES}" ]] \
    || die 'runtime_packages_manifest_invalid'
  receipt="$(
    "${TEST_PYTHON}" -I -B - "${RUNTIME_PACKAGES}" "${RUNTIME_LOCK}" <<'PY'
from __future__ import annotations

import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import sys


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate runtime manifest key: {key}")
        result[key] = value
    return result


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


path = Path(sys.argv[1])
lock_path = Path(sys.argv[2])
raw = path.read_bytes()
value = json.loads(raw, object_pairs_hook=unique_object)
if not isinstance(value, dict) or set(value) != {
    "candidate_project",
    "packages",
    "python_implementation",
    "python_version",
    "runtime_lock_path",
    "runtime_lock_sha256",
    "schema_version",
}:
    raise ValueError("runtime package manifest is not closed")
if value["schema_version"] != "governed-memory-validation-runtime-v2":
    raise ValueError("unexpected runtime package schema")
if value["python_implementation"] != "CPython":
    raise ValueError("unexpected Python implementation")
if value["python_version"] != "3.12.3":
    raise ValueError("unexpected declared Python version")
if sys.version.split()[0] != value["python_version"]:
    raise ValueError("runtime Python version mismatch")
if value["runtime_lock_path"] != "ops/governed_memory/runtime-requirements.lock":
    raise ValueError("runtime lock path differs")
lock_raw = lock_path.read_bytes()
lock_sha256 = hashlib.sha256(lock_raw).hexdigest()
if value["runtime_lock_sha256"] != lock_sha256:
    raise ValueError("runtime lock digest differs")

lock_packages: dict[str, str] = {}
lines = lock_raw.decode("utf-8").splitlines()
index = 0
while index < len(lines):
    line = lines[index]
    if not line or line.startswith("#"):
        index += 1
        continue
    match = re.fullmatch(
        r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\\\s]+) " + r"\\",
        line,
    )
    if match is None or index + 1 >= len(lines):
        raise ValueError("runtime lock package line is malformed")
    hash_match = re.fullmatch(r"    --hash=sha256:([0-9a-f]{64})", lines[index + 1])
    if hash_match is None:
        raise ValueError("runtime lock hash line is malformed")
    name = canonical_name(match.group(1))
    if name in lock_packages:
        raise ValueError("duplicate runtime lock package")
    lock_packages[name] = match.group(2)
    index += 2

declared_packages = {
    canonical_name(name): version for name, version in value["packages"].items()
}
if declared_packages != lock_packages:
    raise ValueError("runtime package declaration differs from lock")
actual_packages = {name: metadata.version(name) for name in lock_packages}
if actual_packages != lock_packages:
    raise ValueError("installed runtime package mismatch")
project = value["candidate_project"]
if not isinstance(project, dict) or project != {
    "name": "governed-memory-successor",
    "version": "0.0.0",
}:
    raise ValueError("candidate project declaration differs")
if metadata.version(project["name"]) != project["version"]:
    raise ValueError("candidate project is not installed")

installed = {
    canonical_name(distribution.metadata["Name"]): distribution.version
    for distribution in metadata.distributions()
    if distribution.metadata.get("Name")
}
expected_installed = {
    **lock_packages,
    canonical_name(project["name"]): project["version"],
}
if installed != expected_installed:
    raise ValueError("candidate runtime contains unlocked distributions")
prefix = Path(sys.prefix).resolve()
for distribution in metadata.distributions():
    location = Path(distribution.locate_file("")).resolve()
    if not location.is_relative_to(prefix):
        raise ValueError("runtime distribution is outside candidate venv")
print(value["python_version"])
print(hashlib.sha256(raw).hexdigest())
print(lock_sha256)
print(project["version"])
PY
  )" || die 'runtime_packages_verification_failed'
  mapfile -t fields <<< "${receipt}"
  [[ "${#fields[@]}" -eq 4 && "${fields[0]}" == '3.12.3' ]] \
    || die 'runtime_packages_receipt_invalid'
  [[ "${fields[1]}" == "${EXPECTED_RUNTIME_PACKAGES_SHA256}" ]] \
    || die 'runtime_packages_sha256_mismatch'
  [[ "${fields[2]}" == "${RUNTIME_LOCK_SHA256}" \
     && "${fields[3]}" == '0.0.0' ]] \
    || die 'runtime_lock_or_project_receipt_invalid'
  RUNTIME_PACKAGES_SHA256="${fields[1]}"
}

assert_candidate_binding() {
  local actual_branch actual_head actual_root actual_status actual_tree commit_count

  [[ "${EXPECTED_ROOT}" =~ ^/tmp/chat-memory-clean-successor-[A-Za-z0-9._-]+$ ]] \
    || die 'expected_root_invalid'
  [[ "${EXPECTED_BRANCH}" =~ ^codex/clean-memory-successor-[a-z0-9._/-]+$ ]] \
    || die 'expected_branch_invalid'
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
  local lock_file='/tmp/governed-memory-successor-019fe927.lock'
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
  local expected_validation_state receipt
  local -a fields
  local -a verifier_arguments

  verifier_arguments=("${MIGRATIONS}")
  expected_validation_state='disposable_validated'
  if [[ -n "${PRELIMINARY_PROOF_AUTHORIZATION}" ]]; then
    [[ "${PRELIMINARY_PROOF_AUTHORIZATION}" == \
       "${PRELIMINARY_PROOF_AUTHORIZATION_VALUE}" ]] \
      || die 'preliminary_migration_proof_authorization_invalid'
    verifier_arguments=(--preliminary-disposable-proof "${MIGRATIONS}")
    expected_validation_state='preliminary_disposable_proof_candidate'
  fi

  receipt="$(
    "${TEST_PYTHON}" -I -B \
      "${SCRIPT_DIR}/verify_migration_manifest.py" "${verifier_arguments[@]}"
  )" || die 'migration_manifest_verification_failed'

  mapfile -t fields < <(
    "${TEST_PYTHON}" -I -B - "${receipt}" <<'PY'
import json
import sys

value = json.loads(sys.argv[1])
print(value.get("schema_version", ""))
print(value.get("result", ""))
print(value.get("migration_package_id_sha256", ""))
print(value.get("file_count", ""))
print(value.get("manifest_sha256", ""))
print(value.get("validation_state", ""))
PY
  )
  [[ "${#fields[@]}" -eq 6 ]] || die 'migration_verification_receipt_invalid'
  [[ "${fields[0]}" == 'governed-memory-migration-verification-v4' ]] \
    || die 'migration_verification_schema_invalid'
  [[ "${fields[1]}" == 'verified' ]] || die 'migration_verification_not_verified'
  [[ "${fields[2]}" =~ ^[0-9a-f]{64}$ ]] \
    || die 'migration_package_id_sha256_invalid'
  [[ "${fields[3]}" == '15' ]] || die 'migration_file_count_mismatch'
  [[ "${fields[4]}" =~ ^[0-9a-f]{64}$ ]] \
    || die 'migration_manifest_sha256_invalid'
  [[ "${fields[4]}" == "${EXPECTED_MANIFEST_SHA256}" ]] \
    || die 'migration_manifest_sha256_mismatch'
  [[ "${fields[5]}" == "${expected_validation_state}" ]] \
    || die 'migration_validation_state_mismatch'
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
  "${TEST_PYTHON}" -I -B - "${repo_digests}" "${QDRANT_IMAGE_DIGEST}" <<'PY' \
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
  "${TEST_PYTHON}" -I -B - \
    "${POSTGRES_PORT}" "${QDRANT_PORT}" "${JWKS_PORT}" "${API_PORT}" <<'PY' \
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
  local found port resource_type

  for resource_type in container network; do
    if [[ "${resource_type}" == 'container' ]]; then
      found="$(
        docker container ls -aq \
          --filter "label=${LABEL_SCOPE_KEY}=${LABEL_SCOPE_VALUE}"
      )" || die 'container_label_scan_failed'
      [[ -z "${found}" ]] || die 'preexisting_successor_container'
      found="$(
        docker container ls -aq \
          --filter "label=${LABEL_RUN_KEY}=${RUN_ID}"
      )" || die 'container_run_label_scan_failed'
      [[ -z "${found}" ]] || die 'preexisting_successor_run_container'
    else
      found="$(
        docker network ls -q \
          --filter "label=${LABEL_SCOPE_KEY}=${LABEL_SCOPE_VALUE}"
      )" || die 'network_label_scan_failed'
      [[ -z "${found}" ]] || die 'preexisting_successor_network'
      found="$(
        docker network ls -q \
          --filter "label=${LABEL_RUN_KEY}=${RUN_ID}"
      )" || die 'network_run_label_scan_failed'
      [[ -z "${found}" ]] || die 'preexisting_successor_run_network'
    fi
  done

  if [[ -n "${NETWORK_NAME}" ]]; then
    ! docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1 \
      || die 'preexisting_invocation_named_network'
    ! docker container inspect "${POSTGRES_CONTAINER_NAME}" >/dev/null 2>&1 \
      || die 'preexisting_invocation_postgres_container'
    ! docker container inspect "${QDRANT_CONTAINER_NAME}" >/dev/null 2>&1 \
      || die 'preexisting_invocation_qdrant_container'
  fi

  for port in \
    "${POSTGRES_PORT}" "${QDRANT_PORT}" "${JWKS_PORT}" "${API_PORT}"
  do
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
  SERVICE_TOKEN="successor-disposable-${compact}"
  NETWORK_NAME="gm-successor-${RUN_ID}-${INVOCATION_TOKEN}-net"
  POSTGRES_CONTAINER_NAME="gm-successor-${RUN_ID}-${INVOCATION_TOKEN}-pg"
  QDRANT_CONTAINER_NAME="gm-successor-${RUN_ID}-${INVOCATION_TOKEN}-qd"
}

container_exists() {
  docker container inspect "$1" >/dev/null 2>&1
}

network_exists() {
  docker network inspect "$1" >/dev/null 2>&1
}

assert_container_identity() {
  local actual bindings container_id expected_host_port expected_image
  local expected_name expected_target_port label mount_type mounts network_mode
  local publish_all_ports

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

  [[ "${expected_target_port}" =~ ^[1-9][0-9]*$ ]] || return 1
  [[ "${expected_host_port}" =~ ^[1-9][0-9]*$ ]] || return 1
  bindings="$(
    docker container inspect "${container_id}" \
      --format '{{json .HostConfig.PortBindings}}'
  )" || return 1
  [[ "${bindings}" == '{}' ]] || return 1
  publish_all_ports="$(
    docker container inspect "${container_id}" \
      --format '{{.HostConfig.PublishAllPorts}}'
  )" || return 1
  [[ "${publish_all_ports}" == 'false' ]] || return 1

  mounts="$(
    docker container inspect "${container_id}" \
      --format '{{range .Mounts}}{{println .Type}}{{end}}'
  )" || return 1
  while IFS= read -r mount_type; do
    [[ -z "${mount_type}" || "${mount_type}" == 'tmpfs' ]] || return 1
  done <<< "${mounts}"
}

capture_container_attachment_ip() {
  local container_id="$1" container_ip network_ip

  container_ip="$(
    docker container inspect "${container_id}" --format \
      "{{ with (index .NetworkSettings.Networks \"${NETWORK_NAME}\") }}{{ .IPAddress }}{{ end }}"
  )" || return 1
  network_ip="$(
    docker network inspect "${NETWORK_ID}" --format \
      "{{ with (index .Containers \"${container_id}\") }}{{ .IPv4Address }}{{ end }}"
  )" || return 1
  network_ip="${network_ip%%/*}"
  [[ -n "${container_ip}" && "${container_ip}" == "${network_ip}" ]] \
    || return 1
  "${TEST_PYTHON}" -I -B - "${container_ip}" <<'PY' || return 1
from ipaddress import IPv4Address
import sys

try:
    address = IPv4Address(sys.argv[1])
except ValueError:
    raise SystemExit(1)
if (
    not address.is_private
    or address.is_loopback
    or address.is_link_local
    or address.is_multicast
    or address.is_unspecified
    or address.is_reserved
):
    raise SystemExit(1)
PY
  printf '%s\n' "${container_ip}"
}

assert_container_attachment_ip() {
  local actual container_id="$1" expected_ip="$2"

  actual="$(capture_container_attachment_ip "${container_id}")" || return 1
  [[ "${actual}" == "${expected_ip}" ]]
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
  [[ "${actual}" == 'true' ]] || return 1

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
      printf 'SUCCESSOR_CLEANUP_REFUSED=uncaptured_named_container:%s\n' \
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
      printf 'SUCCESSOR_CLEANUP_REFUSED=container_identity_mismatch:%s\n' \
        "${container_id}" >&2
      return 1
    fi
    docker container rm -fv "${container_id}" >/dev/null || return 1
  elif container_exists "${expected_name}"; then
    printf 'SUCCESSOR_CLEANUP_REFUSED=container_name_reused:%s\n' \
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
      printf 'SUCCESSOR_CLEANUP_REFUSED=uncaptured_named_network:%s\n' \
        "${NETWORK_NAME}" >&2
      return 1
    fi
    return 0
  fi
  if network_exists "${network_id}"; then
    if ! assert_network_identity "${network_id}"; then
      printf 'SUCCESSOR_CLEANUP_REFUSED=network_identity_mismatch:%s\n' \
        "${network_id}" >&2
      return 1
    fi
    docker network rm "${network_id}" >/dev/null || return 1
  elif network_exists "${NETWORK_NAME}"; then
    printf 'SUCCESSOR_CLEANUP_REFUSED=network_name_reused:%s\n' \
      "${NETWORK_NAME}" >&2
    return 1
  fi

  ! network_exists "${network_id}" || return 1
  ! network_exists "${NETWORK_NAME}" || return 1
}

cleanup_temp() {
  local actual_identity

  [[ -n "${RUN_TMP}" ]] || return 0
  [[ "${RUN_TMP}" =~ ^/tmp/gm-successor-019fe927-[0-9a-f]{12}\.[A-Za-z0-9]{6}$ ]] \
    || return 1
  [[ -n "${RUN_TMP_IDENTITY}" ]] || return 1
  [[ -e "${RUN_TMP}" || -L "${RUN_TMP}" ]] || return 1
  [[ -d "${RUN_TMP}" && ! -L "${RUN_TMP}" ]] || return 1
  actual_identity="$(stat -c '%u:%a:%d:%i' "${RUN_TMP}")" || return 1
  [[ "${actual_identity}" == "${RUN_TMP_IDENTITY}" ]] || return 1
  rm -rf -- "${RUN_TMP}" || return 1
  [[ ! -e "${RUN_TMP}" && ! -L "${RUN_TMP}" ]] || return 1
  RUN_TMP=''
  RUN_TMP_IDENTITY=''
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
    printf 'SUCCESSOR_REFUSED=cleanup_incomplete\n' >&2
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
      "http://${QDRANT_CONTAINER_IP}:6333/healthz" >/dev/null 2>&1
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
      "http://${QDRANT_CONTAINER_IP}:6333/"
  )" || die 'qdrant_version_unreadable'
  QDRANT_SERVER_VERSION="$(
    "${TEST_PYTHON}" -I -B - "${qdrant_root}" <<'PY'
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
  [[ "${QDRANT_SERVER_VERSION}" == '1.19.0' ]] \
    || die 'qdrant_version_unexpected'
}

create_resources() {
  assert_candidate_binding
  assert_resource_namespace_empty

  NETWORK_ID="$(
    docker network create --internal \
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
  POSTGRES_CONTAINER_IP="$(
    capture_container_attachment_ip "${POSTGRES_CONTAINER_ID}"
  )" || die 'postgres_container_attachment_ip_invalid'

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
  QDRANT_CONTAINER_IP="$(
    capture_container_attachment_ip "${QDRANT_CONTAINER_ID}"
  )" || die 'qdrant_container_attachment_ip_invalid'
  [[ "${POSTGRES_CONTAINER_IP}" != "${QDRANT_CONTAINER_IP}" ]] \
    || die 'container_attachment_ips_not_distinct'

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
  assert_container_attachment_ip \
    "${POSTGRES_CONTAINER_ID}" "${POSTGRES_CONTAINER_IP}" \
    || die 'postgres_runtime_attachment_ip_invalid'
  assert_container_attachment_ip \
    "${QDRANT_CONTAINER_ID}" "${QDRANT_CONTAINER_IP}" \
    || die 'qdrant_runtime_attachment_ip_invalid'
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
  run_migration governed_memory governed_memory_owner \
    governed_memory_owner_claim_detail_0003 \
    "${MIGRATIONS}/0003_owner_claim_detail/forward.pgsql"
  run_migration governed_memory governed_memory_owner \
    governed_memory_pilot_marker_0004 \
    "${MIGRATIONS}/0004_pilot_marker/forward.pgsql"
  run_migration memory sage \
    governed_memory_conversation_bridge_0002 \
    "${MIGRATIONS}/0002_conversation_bridge/forward.pgsql"
  docker exec "${POSTGRES_CONTAINER_ID}" psql \
    -X -v ON_ERROR_STOP=1 -U postgres -d memory \
    -c 'GRANT memory_ingest_writer TO brains_app' >/dev/null \
    || die 'disposable_capture_membership_grant_failed'
}

rollback_migrations() {
  local marker_rows

  docker exec "${POSTGRES_CONTAINER_ID}" psql \
    -X -v ON_ERROR_STOP=1 -U postgres -d memory \
    -c 'REVOKE memory_ingest_writer FROM brains_app' >/dev/null \
    || die 'disposable_capture_membership_revoke_failed'
  run_migration memory sage \
    governed_memory_conversation_bridge_0002 \
    "${MIGRATIONS}/0002_conversation_bridge/rollback.pgsql"
  marker_rows="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -A -t -v ON_ERROR_STOP=1 -U postgres -d governed_memory \
      -c 'SELECT pg_catalog.count(*) FROM memory.pilot_marker'
  )" || die 'pilot_marker_empty_rollback_query_failed'
  [[ "${marker_rows}" == '0' ]] || die 'pilot_marker_not_empty_before_rollback'
  run_migration governed_memory governed_memory_owner \
    governed_memory_pilot_marker_0004 \
    "${MIGRATIONS}/0004_pilot_marker/rollback.pgsql"
  run_migration governed_memory governed_memory_owner \
    governed_memory_owner_claim_detail_0003 \
    "${MIGRATIONS}/0003_owner_claim_detail/rollback.pgsql"
  run_migration governed_memory governed_memory_owner \
    governed_memory_foundation_0001 \
    "${MIGRATIONS}/0001_foundation/rollback.pgsql"
}

verify_rollback_refuses_inflight_enqueue() {
  local attempt observed ready=0 rollback_pid writer_pid
  local rollback_log="${RUN_TMP}/bridge-rollback-race.log"
  local writer_log="${RUN_TMP}/bridge-enqueue-race.log"

  docker exec -i "${POSTGRES_CONTAINER_ID}" psql \
    -X -v ON_ERROR_STOP=1 -U postgres -d memory \
    >"${writer_log}" 2>&1 <<'SQL' &
SET SESSION AUTHORIZATION brains_app;
BEGIN;
SELECT set_config(
  'app.user_id', '71111111-1111-4111-8111-111111111111', true
);
SELECT set_config('app.auth_context_sha256', repeat('a', 64), true);
INSERT INTO public.threads(id,owner_user_id,user_id,title)
VALUES (
  '72222222-2222-4222-8222-222222222222'::uuid,
  '71111111-1111-4111-8111-111111111111'::uuid,
  '71111111-1111-4111-8111-111111111111',
  'Synthetic rollback race'
);
INSERT INTO public.chat_log(
  id,owner_user_id,user_id,source,text,thread_id,created_at
)
VALUES (
  '73333333-3333-4333-8333-333333333333'::uuid,
  '71111111-1111-4111-8111-111111111111'::uuid,
  '71111111-1111-4111-8111-111111111111',
  'frontend/chat:user',
  'Synthetic in-flight enqueue retained by empty-only rollback.',
  '72222222-2222-4222-8222-222222222222'::uuid,
  transaction_timestamp()
);
SELECT outcome,outbox_id
FROM memory_ingest_private.enqueue_chat_log_message(
  '73333333-3333-4333-8333-333333333333'::uuid,
  repeat('b', 64)
);
\! touch /tmp/governed-memory-bridge-race-ready
\! while [ ! -f /tmp/governed-memory-bridge-race-release ]; do sleep 1; done
COMMIT;
SQL
  writer_pid="$!"

  for ((attempt = 0; attempt < 30; attempt++)); do
    if docker exec "${POSTGRES_CONTAINER_ID}" \
      test -f /tmp/governed-memory-bridge-race-ready
    then
      ready=1
      break
    fi
    sleep 1
  done
  if [[ "${ready}" -ne 1 ]]; then
    docker exec "${POSTGRES_CONTAINER_ID}" \
      touch /tmp/governed-memory-bridge-race-release >/dev/null 2>&1 || true
    wait "${writer_pid}" || true
    die 'bridge_rollback_race_writer_not_ready'
  fi

  docker exec "${POSTGRES_CONTAINER_ID}" psql \
    -X -v ON_ERROR_STOP=1 -U postgres -d memory \
    -c 'REVOKE memory_ingest_writer FROM brains_app' >/dev/null \
    || die 'bridge_rollback_race_membership_revoke_failed'

  run_migration memory sage governed_memory_conversation_bridge_0002 \
    "${MIGRATIONS}/0002_conversation_bridge/rollback.pgsql" \
    >"${rollback_log}" 2>&1 &
  rollback_pid="$!"

  observed=0
  for ((attempt = 0; attempt < 30; attempt++)); do
    observed="$(
      docker exec "${POSTGRES_CONTAINER_ID}" psql \
        -X -A -t -v ON_ERROR_STOP=1 -U postgres -d memory \
        -c "SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE datname='memory' AND wait_event_type='Lock' AND query LIKE '%LOCK TABLE memory_ingest_private.memory_ingest_outbox%'"
    )" || die 'bridge_rollback_race_lock_wait_query_failed'
    [[ "${observed}" == '1' ]] && break
    sleep 1
  done
  if [[ "${observed}" != '1' ]]; then
    docker exec "${POSTGRES_CONTAINER_ID}" \
      touch /tmp/governed-memory-bridge-race-release >/dev/null 2>&1 || true
    wait "${writer_pid}" || true
    wait "${rollback_pid}" || true
    die 'bridge_rollback_race_lock_wait_not_observed'
  fi

  docker exec "${POSTGRES_CONTAINER_ID}" \
    touch /tmp/governed-memory-bridge-race-release >/dev/null \
    || die 'bridge_rollback_race_writer_release_failed'
  wait "${writer_pid}" || die 'bridge_rollback_race_writer_commit_failed'
  if wait "${rollback_pid}"; then
    die 'bridge_rollback_race_unexpectedly_succeeded'
  fi
  [[ "$(<"${rollback_log}")" == \
    *'conversation bridge rollback is empty-only; rows exist'* ]] \
    || die 'bridge_rollback_race_refusal_missing'

  observed="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -A -t -v ON_ERROR_STOP=1 -U postgres -d memory \
      -c "SELECT pg_catalog.to_regclass('memory_ingest_private.memory_ingest_outbox')::text"
  )" || die 'bridge_rollback_race_table_query_failed'
  [[ "${observed}" == 'memory_ingest_private.memory_ingest_outbox' ]] \
    || die 'bridge_rollback_race_table_not_preserved'
  observed="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -A -t -v ON_ERROR_STOP=1 -U postgres -d memory \
      -c "SELECT count(*) FROM memory_ingest_private.memory_ingest_outbox WHERE message_id='73333333-3333-4333-8333-333333333333'::uuid"
  )" || die 'bridge_rollback_race_row_query_failed'
  [[ "${observed}" == '1' ]] || die 'bridge_rollback_race_row_not_preserved'

  docker exec "${POSTGRES_CONTAINER_ID}" psql \
    -X -v ON_ERROR_STOP=1 -U postgres -d memory \
    -c "DELETE FROM memory_ingest_private.memory_ingest_outbox WHERE message_id='73333333-3333-4333-8333-333333333333'::uuid; DELETE FROM public.chat_log WHERE id='73333333-3333-4333-8333-333333333333'::uuid; DELETE FROM public.threads WHERE id='72222222-2222-4222-8222-222222222222'::uuid" \
    >/dev/null || die 'bridge_rollback_race_cleanup_failed'
  printf 'SUCCESSOR_BRIDGE_ROLLBACK_RACE=inflight-commit-refused-row-preserved\n'
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
  [[ "${marker}" == "governed-memory-successor-disposable:${RUN_ID}" ]] \
    || die 'governed_memory_marker_invalid'
  marker="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -A -t -v ON_ERROR_STOP=1 -U postgres -d postgres \
      -c "SELECT pg_catalog.shobj_description(oid, 'pg_database') FROM pg_catalog.pg_database WHERE datname = 'memory'"
  )" || die 'conversation_marker_unreadable'
  [[ "${marker}" == "governed-memory-successor-disposable:${RUN_ID}" ]] \
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

validate_connect_trace() {
  local observed_count trace_path="$1" postgres_ip="$2" qdrant_ip="$3"

  [[ -f "${trace_path}" && ! -L "${trace_path}" && -s "${trace_path}" ]] \
    || die 'connect_trace_missing'
  observed_count="$(
    "${TEST_PYTHON}" -I -B - \
      "${trace_path}" "${postgres_ip}" "${qdrant_ip}" <<'PY'
from __future__ import annotations

from pathlib import Path
import re
import sys


allowed = {
    ("127.0.0.1", 6339),
    ("127.0.0.1", 18091),
    ("127.0.0.1", 18092),
    ("127.0.0.1", 55443),
    (sys.argv[2], 5432),
    (sys.argv[3], 6333),
}
pattern = re.compile(
    r'connect\([^,]+, \{sa_family=AF_INET, '
    r'sin_port=htons\(([0-9]+)\), '
    r'sin_addr=inet_addr\("([0-9.]+)"\)\}, [0-9]+\)'
)
nscd_pattern = re.compile(
    r'[0-9]+ +connect\([0-9]+, \{sa_family=AF_UNIX, '
    r'sun_path="/var/run/nscd/socket"\}, 110\) = -1 ENOENT '
    r'\(No such file or directory\)'
)
observed: list[tuple[str, int]] = []
failed_local_nscd_connects = 0
for line_number, line in enumerate(
    Path(sys.argv[1]).read_text(encoding="utf-8").splitlines(), start=1
):
    if not line:
        continue
    if nscd_pattern.fullmatch(line):
        failed_local_nscd_connects += 1
        if failed_local_nscd_connects > 8:
            raise ValueError("too many failed local nscd connect attempts")
        continue
    match = pattern.search(line)
    if match is None:
        raise ValueError(f"unexpected connect trace record at line {line_number}")
    endpoint = (match.group(2), int(match.group(1)))
    if endpoint not in allowed:
        raise ValueError(f"forbidden connect endpoint at line {line_number}")
    observed.append(endpoint)
if not observed:
    raise ValueError("connect trace is empty")
if set(observed) != allowed:
    raise ValueError("connect trace did not exercise every isolated endpoint")
print(len(observed))
PY
  )" || die 'connect_trace_invalid'
  [[ "${observed_count}" =~ ^[1-9][0-9]*$ ]] \
    || die 'connect_trace_count_invalid'
  CONNECT_TRACE_SHA256="$(sha256_file "${trace_path}")"
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
      -X -A -t -v ON_ERROR_STOP=1 -U postgres -d governed_memory \
      -c "SELECT CASE WHEN pg_catalog.to_regprocedure('memory_private.read_claim(uuid)') IS NULL AND pg_catalog.to_regclass('memory.pilot_marker') IS NULL AND pg_catalog.to_regprocedure('memory_private.pilot_marker_receipt_sha256(text,uuid,text,text,timestamp with time zone)') IS NULL AND pg_catalog.to_regprocedure('memory_private.guard_pilot_marker_append_only()') IS NULL AND pg_catalog.to_regprocedure('memory_private.mark_pilot_started(text,uuid,text,text,timestamp with time zone)') IS NULL AND pg_catalog.to_regprocedure('memory_private.read_pilot_marker()') IS NULL THEN 'absent' ELSE 'present' END"
  )" || die 'successor_additive_absence_query_failed'
  [[ "${result}" == 'absent' ]] || die 'successor_additive_rollback_objects_remain'

  result="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -A -t -v ON_ERROR_STOP=1 -U postgres -d memory \
      -c "SELECT CASE WHEN pg_catalog.to_regnamespace('memory_ingest_private') IS NULL AND pg_catalog.to_regclass('memory_ingest_private.memory_ingest_outbox') IS NULL THEN 'absent' ELSE 'present' END"
  )" || die 'bridge_absence_query_failed'
  [[ "${result}" == 'absent' ]] || die 'bridge_rollback_objects_remain'
}

verify_disposable_pilot_marker_semantics() {
  local conflict_output first_insert first_replayed first_state marker_rows
  local marker_receipt_sha256 read_result read_rows replay started_at
  local -r pilot_id='governed_memory_disposable_019fe927'
  local -r operation_id='14444444-4444-4444-8444-444444444444'
  local -r pilot_contract_sha256='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
  local -r authorization_receipt_sha256='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'

  marker_rows="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -q -A -t -v ON_ERROR_STOP=1 -U postgres -d governed_memory \
      -c 'SET ROLE governed_memory_owner' \
      -c 'SELECT pg_catalog.count(*) FROM memory.pilot_marker'
  )" || die 'pilot_marker_initial_row_count_failed'
  [[ "${marker_rows}" == '0' ]] || die 'pilot_marker_initial_row_count_not_zero'

  read_rows="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -q -A -t -v ON_ERROR_STOP=1 -U postgres -d governed_memory \
      -c 'SET ROLE governed_memory_worker' \
      -c 'SELECT pg_catalog.count(*) FROM memory_private.read_pilot_marker()'
  )" || die 'pilot_marker_initial_read_failed'
  [[ "${read_rows}" == '0' ]] || die 'pilot_marker_absence_did_not_read_false'
  printf 'SUCCESSOR_PILOT_MARKER_ABSENCE=false rows=0\n'

  started_at="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -q -A -t -v ON_ERROR_STOP=1 -U postgres -d governed_memory \
      -c "SELECT pg_catalog.to_char(pg_catalog.clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
  )" || die 'pilot_marker_database_time_failed'
  [[ "${started_at}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$ ]] \
    || die 'pilot_marker_database_time_invalid'

  first_insert="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -q -A -t -F '|' -v ON_ERROR_STOP=1 \
      -U postgres -d governed_memory \
      -c 'SET ROLE governed_memory_owner' \
      -c "SELECT CASE WHEN value.pilot_ever_started THEN 'true' ELSE 'false' END, value.marker_receipt_sha256, CASE WHEN value.replayed THEN 'true' ELSE 'false' END FROM memory_private.mark_pilot_started('${pilot_id}', '${operation_id}'::uuid, '${pilot_contract_sha256}', '${authorization_receipt_sha256}', '${started_at}'::timestamptz) AS value"
  )" || die 'pilot_marker_first_insert_failed'
  IFS='|' read -r first_state marker_receipt_sha256 first_replayed \
    <<< "${first_insert}"
  [[ "${first_state}" == 'true' \
     && "${marker_receipt_sha256}" =~ ^[0-9a-f]{64}$ \
     && "${first_replayed}" == 'false' ]] \
    || die 'pilot_marker_first_insert_result_mismatch'

  replay="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -q -A -t -F '|' -v ON_ERROR_STOP=1 \
      -U postgres -d governed_memory \
      -c 'SET ROLE governed_memory_owner' \
      -c "SELECT CASE WHEN value.pilot_ever_started THEN 'true' ELSE 'false' END, value.marker_receipt_sha256, CASE WHEN value.replayed THEN 'true' ELSE 'false' END FROM memory_private.mark_pilot_started('${pilot_id}', '${operation_id}'::uuid, '${pilot_contract_sha256}', '${authorization_receipt_sha256}', '${started_at}'::timestamptz) AS value"
  )" || die 'pilot_marker_exact_replay_failed'
  [[ "${replay}" == "true|${marker_receipt_sha256}|true" ]] \
    || die 'pilot_marker_exact_replay_result_mismatch'

  if conflict_output="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -q -A -t -v ON_ERROR_STOP=1 -U postgres -d governed_memory \
      -c 'SET ROLE governed_memory_owner' \
      -c "SELECT * FROM memory_private.mark_pilot_started('${pilot_id}', '${operation_id}'::uuid, '${pilot_contract_sha256}', '${authorization_receipt_sha256}', '${started_at}'::timestamptz + interval '1 microsecond')" \
      2>&1
  )"; then
    die 'pilot_marker_conflicting_replay_unexpectedly_succeeded'
  fi
  [[ "${conflict_output}" == *'pilot marker conflicting replay'* ]] \
    || die 'pilot_marker_conflicting_replay_refusal_missing'

  marker_rows="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -q -A -t -v ON_ERROR_STOP=1 -U postgres -d governed_memory \
      -c 'SET ROLE governed_memory_owner' \
      -c 'SELECT pg_catalog.count(*) FROM memory.pilot_marker'
  )" || die 'pilot_marker_final_row_count_failed'
  [[ "${marker_rows}" == '1' ]] || die 'pilot_marker_final_row_count_not_one'

  read_result="$(
    docker exec "${POSTGRES_CONTAINER_ID}" psql \
      -X -q -A -t -F '|' -v ON_ERROR_STOP=1 \
      -U postgres -d governed_memory \
      -c 'SET ROLE governed_memory_worker' \
      -c "SELECT CASE WHEN value.pilot_ever_started THEN 'true' ELSE 'false' END, value.pilot_id, value.operation_id, value.pilot_contract_sha256, value.authorization_receipt_sha256, pg_catalog.to_char(value.started_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), value.marker_receipt_sha256 FROM memory_private.read_pilot_marker() AS value"
  )" || die 'pilot_marker_final_read_failed'
  [[ "${read_result}" == "true|${pilot_id}|${operation_id}|${pilot_contract_sha256}|${authorization_receipt_sha256}|${started_at}|${marker_receipt_sha256}" ]] \
    || die 'pilot_marker_final_read_result_mismatch'
  printf 'SUCCESSOR_PILOT_MARKER_SEMANTICS=insert-replay-conflict-refusal-read-true\n'
}

verify_apply_rollback_reapply() {
  local bridge_first="${RUN_TMP}/bridge-first.sql"
  local bridge_second="${RUN_TMP}/bridge-second.sql"
  local foundation_first="${RUN_TMP}/foundation-first.sql"
  local foundation_second="${RUN_TMP}/foundation-second.sql"

  apply_migrations
  normalized_schema_dump governed_memory "${foundation_first}"
  normalized_schema_dump memory "${bridge_first}"

  verify_rollback_refuses_inflight_enqueue
  rollback_migrations
  assert_rollback_absence

  apply_migrations
  normalized_schema_dump governed_memory "${foundation_second}"
  normalized_schema_dump memory "${bridge_second}"

  cmp -s "${foundation_first}" "${foundation_second}" \
    || die 'foundation_reapply_logical_dump_mismatch'
  cmp -s "${bridge_first}" "${bridge_second}" \
    || die 'bridge_reapply_logical_dump_mismatch'
  FOUNDATION_DUMP_SHA256="$(sha256_file "${foundation_second}")"
  BRIDGE_DUMP_SHA256="$(sha256_file "${bridge_second}")"
  verify_disposable_pilot_marker_semantics
  printf 'SUCCESSOR_MIGRATION_CYCLE=apply-rollback-absence-reapply-equivalent\n'
}

validate_integration_receipt() {
  local receipt_json="$1" validated
  local -a fields

  validated="$(
    "${TEST_PYTHON}" -I -B - "${receipt_json}" <<'PY'
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
expected_keys = {
    "answer_binding_sha256",
    "auth_negative_matrix_sha256",
    "chat_a_thread_id",
    "chat_b_thread_id",
    "claim_id",
    "cold_extraction_lease",
    "cold_projection_rebuild",
    "corrected_revision_id",
    "deletion_receipt_sha256",
    "embedding_dispatch_adversarial",
    "embedding_dispatch_marker_count",
    "embedding_dispatch_marker_sha256",
    "http_lifecycle_sha256",
    "http_owner_lifecycle",
    "inactive_worker_runtime_sha256",
    "initial_revision_id",
    "jwks_fetch_count",
    "jwks_manifest_sha256",
    "owner_isolation_sha256",
    "production_data_read",
    "production_endpoint_calls",
    "production_service_invoked",
    "projection_lease_version_fenced",
    "provider_external_calls",
    "rebuild_manifest_sha256",
    "review_surface_sha256",
    "route_manifest_sha256",
    "schema",
    "semantic_threshold_calibrated",
    "synthetic_jwt_only",
}
if set(receipt) != expected_keys:
    raise ValueError("integration receipt key set is not closed")
if receipt.get("schema") != "governed-memory-successor-http-integration-receipt-v2":
    raise ValueError("unexpected integration receipt schema")
if receipt.get("cold_extraction_lease") is not True:
    raise ValueError("integration receipt lacks cold extraction proof")
if receipt.get("cold_projection_rebuild") is not True:
    raise ValueError("integration receipt lacks cold rebuild proof")
if receipt.get("provider_external_calls") != 0:
    raise ValueError("integration receipt reports an external provider call")
if receipt.get("embedding_dispatch_adversarial") is not True:
    raise ValueError("integration receipt lacks embedding dispatch adversarial proof")
if receipt.get("embedding_dispatch_marker_count") != 2:
    raise ValueError("integration receipt has wrong embedding marker count")
if receipt.get("projection_lease_version_fenced") is not True:
    raise ValueError("integration receipt lacks projection lease version fence")
if receipt.get("production_data_read") is not False:
    raise ValueError("integration receipt does not deny production reads")
if receipt.get("production_endpoint_calls") != 0:
    raise ValueError("integration receipt reports a production endpoint call")
if receipt.get("production_service_invoked") is not False:
    raise ValueError("integration receipt reports production service invocation")
if receipt.get("http_owner_lifecycle") is not True:
    raise ValueError("integration receipt lacks HTTP owner lifecycle proof")
if receipt.get("synthetic_jwt_only") is not True:
    raise ValueError("integration receipt lacks synthetic JWT confinement")
if receipt.get("semantic_threshold_calibrated") is not False:
    raise ValueError("integration receipt misstates threshold calibration")
if type(receipt.get("jwks_fetch_count")) is not int or receipt["jwks_fetch_count"] < 1:
    raise ValueError("integration receipt lacks a JWKS fetch")

required_hashes = (
    "auth_negative_matrix_sha256",
    "http_lifecycle_sha256",
    "inactive_worker_runtime_sha256",
    "jwks_manifest_sha256",
    "owner_isolation_sha256",
    "rebuild_manifest_sha256",
    "review_surface_sha256",
    "answer_binding_sha256",
    "deletion_receipt_sha256",
    "embedding_dispatch_marker_sha256",
    "route_manifest_sha256",
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
print(canonical.decode("utf-8"))
PY
  )" || die 'integration_receipt_invalid'
  mapfile -t fields <<< "${validated}"
  [[ "${#fields[@]}" -eq 2 && "${fields[0]}" =~ ^[0-9a-f]{64}$ ]] \
    || die 'integration_receipt_sha256_invalid'
  [[ "${fields[1]}" == \{*\} ]] || die 'integration_receipt_canonical_invalid'
  INTEGRATION_RECEIPT_SHA256="${fields[0]}"
  INTEGRATION_RECEIPT_JSON="${fields[1]}"
}

run_integration() {
  local connect_trace="${RUN_TMP}/connect.trace"
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
  assert_container_attachment_ip \
    "${POSTGRES_CONTAINER_ID}" "${POSTGRES_CONTAINER_IP}" \
    || die 'postgres_attachment_ip_drift_before_test'
  assert_container_attachment_ip \
    "${QDRANT_CONTAINER_ID}" "${QDRANT_CONTAINER_IP}" \
    || die 'qdrant_attachment_ip_drift_before_test'

  set +e
  (
    cd "${ROOT}"
    strace -f -qqq -e trace=connect -e signal=none -s 256 -o "${connect_trace}" \
      timeout --signal=TERM --kill-after=10s 600s env -i \
      PATH="${PATH}" \
      LC_ALL=C.UTF-8 \
      LANG=C.UTF-8 \
      TMPDIR="${RUN_TMP}" \
      NO_PROXY=127.0.0.1,localhost \
      no_proxy=127.0.0.1,localhost \
      PYTHONUTF8=1 \
      PYTHONHASHSEED=0 \
      PYTHONNOUSERSITE=1 \
      PYTHONDONTWRITEBYTECODE=1 \
      PYTHONPATH="${ROOT}" \
      GOVERNED_MEMORY_EXCLUSIVE_MODE=successor_pilot \
      GM_VALIDATION_RUN=1 \
      GM_VALIDATION_POSTGRES_HOST=127.0.0.1 \
      GM_VALIDATION_POSTGRES_PORT="${POSTGRES_PORT}" \
      GM_VALIDATION_POSTGRES_CONTAINER_IP="${POSTGRES_CONTAINER_IP}" \
      GM_VALIDATION_QDRANT_URL="http://127.0.0.1:${QDRANT_PORT}" \
      GM_VALIDATION_QDRANT_CONTAINER_IP="${QDRANT_CONTAINER_IP}" \
      GM_VALIDATION_JWKS_URL="http://127.0.0.1:${JWKS_PORT}/auth/v1/.well-known/jwks.json" \
      GM_VALIDATION_API_URL="http://127.0.0.1:${API_PORT}" \
      GM_VALIDATION_INVOCATION_ID="${INVOCATION_ID}" \
      GM_VALIDATION_SERVICE_TOKEN="${SERVICE_TOKEN}" \
      "${TEST_PYTHON}" -B -P -m unittest \
        tests.memory_integration.test_governed_memory_http_vertical_slice.GovernedMemoryHttpVerticalSliceTests.test_http_chat_a_to_chat_b_rebuild_and_deletion \
        -v
  ) > "${test_log}" 2>&1
  status=$?
  set -e

  if [[ "${status}" -ne 0 ]]; then
    sed '/^SUCCESSOR_HTTP_VERTICAL_SLICE_RECEIPT=/d' "${test_log}" >&2
    die "integration_failed:${status}"
  fi

  validate_connect_trace \
    "${connect_trace}" "${POSTGRES_CONTAINER_IP}" "${QDRANT_CONTAINER_IP}"
  receipt_count="$(
    awk '/^SUCCESSOR_HTTP_VERTICAL_SLICE_RECEIPT=/{count += 1} END{print count + 0}' \
      "${test_log}"
  )"
  [[ "${receipt_count}" == '1' ]] || die 'integration_receipt_count_invalid'
  receipt_json="$(
    awk -F= '/^SUCCESSOR_HTTP_VERTICAL_SLICE_RECEIPT=/{sub(/^[^=]*=/, ""); print}' \
      "${test_log}"
  )"
  validate_integration_receipt "${receipt_json}"
  sed '/^SUCCESSOR_HTTP_VERTICAL_SLICE_RECEIPT=/d' "${test_log}"
  printf 'SUCCESSOR_HTTP_VERTICAL_SLICE_RECEIPT=%s\n' \
    "${INTEGRATION_RECEIPT_JSON}"
  printf 'SUCCESSOR_INTEGRATION=passed loopback_application_endpoints=true traced_internal_bridge_connects=true synthetic_provider_external_calls=0 production_data_read=false production_endpoint_calls=0\n'
}

assert_temp_identity() {
  local current_uid

  [[ "${RUN_TMP}" =~ ^/tmp/gm-successor-019fe927-[0-9a-f]{12}\.[A-Za-z0-9]{6}$ ]] \
    || die 'temporary_directory_path_invalid'
  [[ -d "${RUN_TMP}" && ! -L "${RUN_TMP}" ]] \
    || die 'temporary_directory_type_invalid'
  RUN_TMP_IDENTITY="$(stat -c '%u:%a:%d:%i' "${RUN_TMP}")" \
    || die 'temporary_directory_identity_unreadable'
  current_uid="$(id -u)"
  [[ "${RUN_TMP_IDENTITY}" =~ ^${current_uid}:700:[0-9]+:[1-9][0-9]*$ ]] \
    || die 'temporary_directory_identity_invalid'
}

preflight() {
  local command

  assert_ambient_authority_clean
  [[ "${1:-full}" == 'full' && "$#" -le 1 ]] \
    || die 'usage:run_disposable_successor.sh_full_only'
  [[ "$(hostname -s)" == "${EXPECTED_HOST}" ]] || die 'wrong_host'
  [[ "$(id -un)" == "${EXPECTED_USER}" ]] || die 'wrong_user'
  [[ "$(id -u)" -ne 0 ]] || die 'root_execution_refused'
  [[ "${ROOT}" == "${EXPECTED_ROOT}" ]] || die 'wrong_candidate_root'
  [[ "${GM_VALIDATION_DISPOSABLE_AUTHORIZATION:-}" == "${AUTHORIZATION_VALUE}" ]] \
    || die 'explicit_disposable_authorization_missing'
  [[ "${POSTGRES_PORT}" != '5432' && "${QDRANT_PORT}" != '6333' \
     && "${JWKS_PORT}" != '8088' && "${API_PORT}" != '8088' ]] \
    || die 'production_port_constant_detected'

  for command in \
    awk chmod cmp curl docker env flock git hostname id mapfile mktemp \
    realpath rm sed sha256sum sleep ss stat strace timeout
  do
    require_command "${command}"
  done
  bind_validation_runtime_python
  bind_local_docker_authority
  docker version --format '{{.Server.Version}}' >/dev/null \
    || die 'docker_daemon_unavailable'

  acquire_lock
  assert_candidate_binding
  verify_migration_manifest
  verify_runtime_build_receipt
  verify_runtime_packages
  verify_installed_successor_source
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
    mktemp -d "/tmp/gm-successor-${RUN_ID}-${INVOCATION_TOKEN}.XXXXXX"
  )" || die 'temporary_directory_create_failed'
  assert_temp_identity

  create_resources
  bootstrap_postgres
  verify_apply_rollback_reapply
  run_integration

  cleanup_all || die 'cleanup_incomplete'
  assert_resource_namespace_empty
  assert_no_listening_port "${POSTGRES_PORT}"
  assert_no_listening_port "${QDRANT_PORT}"
  assert_no_listening_port "${JWKS_PORT}"
  assert_no_listening_port "${API_PORT}"
  assert_candidate_binding
  [[ "${SOURCE_TREE_SHA256}" =~ ^[0-9a-f]{64}$ ]] \
    || die 'final_source_tree_sha256_invalid'
  [[ "${RUNTIME_BUILD_RECEIPT_SHA256}" =~ ^[0-9a-f]{64}$ ]] \
    || die 'final_runtime_build_receipt_sha256_invalid'

  trap - EXIT INT TERM HUP
  printf 'SUCCESSOR_DISPOSABLE_RECEIPT={"branch":"%s","candidate_head":"%s","candidate_tree":"%s","source_tree_sha256":"%s","runtime_build_receipt_sha256":"%s","candidate_unchanged":true,"connect_trace_sha256":"%s","external_network_calls":0,"loopback_application_endpoints":true,"traced_internal_bridge_connects":true,"published_container_ports":false,"provider_external_calls":0,"production_data_read":false,"production_endpoint_calls":0,"production_service_invoked":false,"docker_persistent_mounts":false,"ports_released":true,"resources_removed":true,"result":"passed","run_id":"%s","invocation_id":"%s","network_id":"%s","postgres_container_id":"%s","qdrant_container_id":"%s","postgres_image_id":"%s","qdrant_image_id":"%s","qdrant_image_digest":"%s","postgres_server_version":"%s","qdrant_server_version":"%s","manifest_sha256":"%s","runtime_packages_sha256":"%s","runtime_lock_sha256":"%s","foundation_logical_dump_sha256":"%s","bridge_logical_dump_sha256":"%s","integration_receipt_sha256":"%s","rollback_reapply":"passed","semantic_threshold_calibrated":false,"schema_version":"governed-memory-successor-disposable-run-v5"}\n' \
    "${EXPECTED_BRANCH}" "${EXPECTED_HEAD}" "${EXPECTED_TREE}" \
    "${SOURCE_TREE_SHA256}" "${RUNTIME_BUILD_RECEIPT_SHA256}" \
    "${CONNECT_TRACE_SHA256}" "${RUN_ID}" "${INVOCATION_ID}" "${NETWORK_ID}" \
    "${POSTGRES_CONTAINER_ID}" "${QDRANT_CONTAINER_ID}" \
    "${POSTGRES_IMAGE_ID}" "${QDRANT_IMAGE_ID}" "${QDRANT_IMAGE_DIGEST}" \
    "${POSTGRES_SERVER_VERSION}" "${QDRANT_SERVER_VERSION}" \
    "${MIGRATION_MANIFEST_SHA256}" "${RUNTIME_PACKAGES_SHA256}" \
    "${RUNTIME_LOCK_SHA256}" \
    "${FOUNDATION_DUMP_SHA256}" "${BRIDGE_DUMP_SHA256}" \
    "${INTEGRATION_RECEIPT_SHA256}"
}

full "$@"

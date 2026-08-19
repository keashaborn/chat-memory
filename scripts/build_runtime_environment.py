#!/usr/bin/env python3
from __future__ import annotations

"""Build a hash-bound SeeBx runtime without activating it."""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "seebx-runtime-environment-receipt-v1"
WHEELHOUSE_SCHEMA = "seebx-runtime-wheelhouse-manifest-v1"
PYTHON = "/usr/bin/python3.12"
GIT = "/usr/bin/git"
EXPECTED_PYTHON = (3, 12)
EXPECTED_ARCHITECTURE = "x86_64"
RUN_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
IMPORT_MODULES = (
    "asyncpg",
    "cryptography",
    "fastapi",
    "httpx",
    "jsonschema",
    "openai",
    "pydantic",
    "jwt",
    "requests",
    "uvicorn",
    "websockets",
    "zep_cloud",
)


class RuntimeBuildContractError(RuntimeError):
    pass


class RuntimeBuildExecutionError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _secure_regular_file(path: Path) -> Path:
    try:
        item = path.lstat()
    except OSError as error:
        raise RuntimeBuildContractError("required_file_unavailable") from error
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
        raise RuntimeBuildContractError("required_file_type_invalid")
    return path.resolve(strict=True)


def _secure_private_directory(path: Path) -> Path:
    try:
        item = path.lstat()
    except OSError as error:
        raise RuntimeBuildContractError("private_directory_unavailable") from error
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
        raise RuntimeBuildContractError("private_directory_type_invalid")
    if item.st_uid != os.geteuid() or stat.S_IMODE(item.st_mode) & 0o077:
        raise RuntimeBuildContractError("private_directory_permissions_invalid")
    return path.resolve(strict=True)


def _repository_file(repository: Path, relative: str) -> Path:
    root = repository.resolve(strict=True)
    path = _secure_regular_file(root / relative)
    if not path.is_relative_to(root):
        raise RuntimeBuildContractError("repository_file_outside_root")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeBuildContractError("json_contract_invalid") from error
    if not isinstance(value, dict):
        raise RuntimeBuildContractError("json_contract_not_object")
    return value


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    command = [
        GIT,
        "-c",
        f"safe.directory={repository}",
        "-C",
        str(repository),
        *arguments,
    ]
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeBuildContractError("repository_identity_unavailable") from error


def validate_repository(repository: Path, expected_production_commit: str) -> dict[str, Any]:
    if not HEX40.fullmatch(expected_production_commit):
        raise RuntimeBuildContractError("production_commit_invalid")
    try:
        item = repository.lstat()
    except OSError as error:
        raise RuntimeBuildContractError("repository_unavailable") from error
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
        raise RuntimeBuildContractError("repository_type_invalid")
    repository = repository.resolve(strict=True)
    top = Path(_git(repository, "rev-parse", "--show-toplevel").stdout.decode().strip())
    if top.resolve(strict=True) != repository:
        raise RuntimeBuildContractError("repository_root_mismatch")
    if _git(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout:
        raise RuntimeBuildContractError("repository_not_clean")
    head = _git(repository, "rev-parse", "HEAD").stdout.decode().strip()
    tree = _git(repository, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
    if not HEX40.fullmatch(head) or not HEX40.fullmatch(tree):
        raise RuntimeBuildContractError("repository_revision_invalid")
    ancestor = _git(
        repository,
        "merge-base",
        "--is-ancestor",
        expected_production_commit,
        head,
        check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeBuildContractError("candidate_not_fast_forward")
    behind = int(
        _git(repository, "rev-list", "--count", f"{head}..{expected_production_commit}")
        .stdout.decode()
        .strip()
    )
    if behind != 0:
        raise RuntimeBuildContractError("candidate_behind_production")
    ahead = int(
        _git(repository, "rev-list", "--count", f"{expected_production_commit}..{head}")
        .stdout.decode()
        .strip()
    )
    return {
        "commit": head,
        "tree": tree,
        "production_commit": expected_production_commit,
        "ahead_count": ahead,
        "clean": True,
    }


def validate_wheelhouse(
    manifest_path: Path,
    lock_path: Path,
    requirements_path: Path,
    wheelhouse: Path,
) -> dict[str, Any]:
    manifest_path = _secure_regular_file(manifest_path)
    lock_path = _secure_regular_file(lock_path)
    requirements_path = _secure_regular_file(requirements_path)
    wheelhouse = _secure_private_directory(wheelhouse)
    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != WHEELHOUSE_SCHEMA:
        raise RuntimeBuildContractError("wheelhouse_schema_invalid")
    python = manifest.get("python")
    if not isinstance(python, Mapping) or (
        python.get("implementation") != "CPython"
        or (python.get("major"), python.get("minor")) != EXPECTED_PYTHON
        or python.get("architecture") != EXPECTED_ARCHITECTURE
        or python.get("platform") != "linux"
    ):
        raise RuntimeBuildContractError("wheelhouse_platform_invalid")
    if manifest.get("source_requirements_sha256") != sha256_file(requirements_path):
        raise RuntimeBuildContractError("requirements_hash_mismatch")
    if manifest.get("lock_sha256") != sha256_file(lock_path):
        raise RuntimeBuildContractError("runtime_lock_hash_mismatch")
    records = manifest.get("wheels")
    if not isinstance(records, list) or manifest.get("wheel_count") != len(records):
        raise RuntimeBuildContractError("wheelhouse_inventory_invalid")
    expected_files: set[str] = set()
    normalized_names: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise RuntimeBuildContractError("wheel_record_invalid")
        filename = record.get("filename")
        normalized = record.get("normalized_name")
        digest = record.get("sha256")
        size = record.get("bytes")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.endswith(".whl")
            or not isinstance(normalized, str)
            or not normalized
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(size, int)
            or size <= 0
            or filename in expected_files
            or normalized in normalized_names
        ):
            raise RuntimeBuildContractError("wheel_record_invalid")
        wheel = _secure_regular_file(wheelhouse / filename)
        wheel_stat = wheel.stat()
        if (
            wheel.parent != wheelhouse
            or wheel_stat.st_uid != os.geteuid()
            or stat.S_IMODE(wheel_stat.st_mode) & 0o022
            or wheel_stat.st_size != size
            or sha256_file(wheel) != digest
        ):
            raise RuntimeBuildContractError("wheel_artifact_mismatch")
        expected_files.add(filename)
        normalized_names.add(normalized)
    actual_files = {path.name for path in wheelhouse.iterdir()}
    if actual_files != expected_files:
        raise RuntimeBuildContractError("wheelhouse_file_set_mismatch")
    return {
        "manifest_sha256": sha256_file(manifest_path),
        "lock_sha256": sha256_file(lock_path),
        "wheel_count": len(records),
        "wheel_set_sha256": hashlib.sha256(canonical_bytes(sorted(expected_files))).hexdigest(),
    }


def build_pip_install_command(venv: Path, wheelhouse: Path, lock_path: Path) -> list[str]:
    return [
        str(venv / "bin/python"),
        "-m",
        "pip",
        "install",
        "--isolated",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--no-index",
        f"--find-links={wheelhouse}",
        "--only-binary=:all:",
        "--require-hashes",
        "--requirement",
        str(lock_path),
    ]


def _runtime_environment() -> dict[str, str]:
    return {
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PIP_CONFIG_FILE": "/dev/null",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _run(command: Sequence[str], *, label: str, timeout: int = 900) -> str:
    try:
        result = subprocess.run(
            list(command),
            env=_runtime_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeBuildExecutionError(label + "_execution_failed") from error
    if result.returncode != 0:
        raise RuntimeBuildExecutionError(label + "_failed")
    return result.stdout.strip()


def _atomic_write(path: Path, value: bytes, mode: int = 0o400) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _freeze_tree(root: Path) -> None:
    for directory, names, filenames in os.walk(root, topdown=False, followlinks=False):
        base = Path(directory)
        for name in filenames:
            path = base / name
            item = path.lstat()
            if stat.S_ISLNK(item.st_mode):
                target = path.resolve(strict=True)
                if not target.is_relative_to(root.resolve(strict=True)):
                    raise RuntimeBuildExecutionError("runtime_symlink_outside_environment")
                continue
            if not stat.S_ISREG(item.st_mode):
                raise RuntimeBuildExecutionError("runtime_file_type_invalid")
            os.chmod(path, 0o555 if item.st_mode & 0o111 else 0o444)
        for name in names:
            path = base / name
            if not path.is_symlink():
                os.chmod(path, 0o555)
    os.chmod(root, 0o555)


def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeBuildContractError("root_execution_required")
    if not RUN_ID.fullmatch(arguments.run_id):
        raise RuntimeBuildContractError("run_id_invalid")
    if Path(PYTHON).resolve(strict=True) != Path(PYTHON):
        raise RuntimeBuildContractError("python_binary_invalid")
    if (sys.version_info.major, sys.version_info.minor) != EXPECTED_PYTHON:
        raise RuntimeBuildContractError("builder_python_version_invalid")
    if platform.machine() != EXPECTED_ARCHITECTURE or sys.platform != "linux":
        raise RuntimeBuildContractError("builder_platform_invalid")

    repository = arguments.repository.resolve(strict=True)
    output_root = _secure_private_directory(arguments.output_root)
    wheelhouse = _secure_private_directory(arguments.wheelhouse)
    if output_root.is_relative_to(repository) or wheelhouse.is_relative_to(repository):
        raise RuntimeBuildContractError("mutable_build_path_inside_repository")
    repository_state = validate_repository(repository, arguments.production_commit)
    lock_path = _repository_file(repository, "requirements-runtime.lock")
    requirements_path = _repository_file(repository, "requirements-ci.txt")
    manifest_path = _repository_file(
        repository,
        "ops/runtime/wheelhouse-manifest-linux-x86_64-py312.json",
    )
    wheelhouse_state = validate_wheelhouse(
        manifest_path,
        lock_path,
        requirements_path,
        wheelhouse,
    )

    target = output_root / arguments.run_id
    staging = output_root / (".staging-" + arguments.run_id)
    if target.exists() or staging.exists():
        raise RuntimeBuildContractError("runtime_output_already_exists")
    staging.mkdir(mode=0o700)
    venv = staging / "venv"
    published = False
    try:
        _run([PYTHON, "-m", "venv", "--copies", str(venv)], label="venv_create")
        _run(
            build_pip_install_command(venv, wheelhouse, lock_path),
            label="offline_install",
            timeout=1800,
        )
        pip_check = _run(
            [str(venv / "bin/python"), "-m", "pip", "check"],
            label="pip_check",
        )
        preflight_output = _run(
            [
                str(venv / "bin/python"),
                str(repository / "scripts/verify_runtime_dependencies.py"),
                "--pyproject",
                str(repository / "pyproject.toml"),
            ],
            label="dependency_preflight",
        )
        try:
            preflight = json.loads(preflight_output)
        except json.JSONDecodeError as error:
            raise RuntimeBuildExecutionError("dependency_preflight_output_invalid") from error
        if (
            preflight.get("status") != "pass"
            or preflight.get("dependencies", {}).get("verified_count") != 12
            or preflight.get("dependencies", {}).get("expected_count") != 12
        ):
            raise RuntimeBuildExecutionError("dependency_preflight_contract_failed")
        import_program = ";".join(f"import {name}" for name in IMPORT_MODULES)
        _run(
            [str(venv / "bin/python"), "-I", "-c", import_program],
            label="runtime_import_smoke",
        )
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "status": "pass",
            "created_at_utc": datetime.now(UTC).isoformat(),
            "run_id": arguments.run_id,
            "server": "seebx",
            "activated": False,
            "repository": repository_state,
            "runtime": {
                "python": ".".join(map(str, EXPECTED_PYTHON)),
                "architecture": EXPECTED_ARCHITECTURE,
                "dependency_preflight": preflight,
                "pip_check": pip_check,
                "import_count": len(IMPORT_MODULES),
            },
            "wheelhouse": wheelhouse_state,
            "bindings": {
                "builder_sha256": sha256_file(Path(__file__).resolve()),
                "pyproject_sha256": sha256_file(repository / "pyproject.toml"),
                "requirements_sha256": sha256_file(requirements_path),
                "preflight_sha256": sha256_file(
                    repository / "scripts/verify_runtime_dependencies.py"
                ),
            },
        }
        receipt_bytes = canonical_bytes(receipt)
        _atomic_write(staging / "runtime-environment-receipt.json", receipt_bytes)
        _freeze_tree(venv)
        os.chmod(staging, 0o555)
        os.replace(staging, target)
        published = True
        directory_descriptor = os.open(output_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "pass",
            "activated": False,
            "runtime_path": str(target),
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "repository_commit": repository_state["commit"],
            "runtime_lock_sha256": wheelhouse_state["lock_sha256"],
        }
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--production-commit", required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    arguments = parser.parse_args(argv)
    exit_code = 0
    try:
        result = execute(arguments)
    except RuntimeBuildContractError as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": "runtime_build_contract_error",
            "reason": str(error),
        }
        exit_code = 2
    except RuntimeBuildExecutionError as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": "runtime_build_execution_error",
            "reason": str(error),
        }
        exit_code = 3
    except Exception:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": "runtime_build_unexpected_error",
        }
        exit_code = 3
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

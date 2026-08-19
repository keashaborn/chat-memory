#!/usr/bin/env python3
from __future__ import annotations

"""Offline integrity verifier for a paired SeeBx/Verbal Sage release package."""

import argparse
import hashlib
import json
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

SCHEMA_VERSION = "seebx-atomic-release-package-verification-v1"
PACKAGE_SCHEMA = "seebx-atomic-release-package-v1"
RUNTIME_SCHEMA = "seebx-runtime-environment-receipt-v1"
MIGRATION_SCHEMA = "seebx-zep-chat-history-migration-receipt-v1"
RECOVERY_SCHEMA = "seebx-legacy-memory-recovery-receipt-v1"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_ARTIFACTS = (
    "backend_source_bundle",
    "frontend_source_bundle",
    "frontend_build_archive",
    "frontend_build_manifest",
    "runtime_receipt",
    "migration_package",
    "migration_receipt",
    "recovery_receipt",
    "database_backup",
    "backend_systemd_snapshot",
    "backend_environment_manifest",
    "frontend_systemd_snapshot",
    "frontend_environment_manifest",
)


class ReleasePackageError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleasePackageError(reason) from error
    if not isinstance(value, dict):
        raise ReleasePackageError(reason)
    return value


def _secure_root(path: Path) -> Path:
    try:
        item = path.lstat()
    except OSError as error:
        raise ReleasePackageError("artifact_root_unavailable") from error
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
        raise ReleasePackageError("artifact_root_type_invalid")
    return path.resolve(strict=True)


def _secure_artifact(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ReleasePackageError("artifact_path_invalid")
    path = root / relative
    try:
        item = path.lstat()
    except OSError as error:
        raise ReleasePackageError("artifact_unavailable") from error
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
        raise ReleasePackageError("artifact_type_invalid")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ReleasePackageError("artifact_outside_root")
    return resolved


def _expect(condition: bool, reason: str) -> None:
    if not condition:
        raise ReleasePackageError(reason)


def _run_git(arguments: list[str], *, cwd: Path | None = None) -> str:
    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    try:
        result = subprocess.run(
            ["/usr/bin/git", *arguments],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleasePackageError("git_bundle_verification_failed") from error
    if result.returncode != 0:
        raise ReleasePackageError("git_bundle_verification_failed")
    return result.stdout.strip()


def _verify_source_bundle(bundle: Path, source: Mapping[str, Any], server: str) -> None:
    with tempfile.TemporaryDirectory(prefix="seebx-release-bundle-") as temporary:
        repository = Path(temporary) / "repository.git"
        _run_git(["init", "--bare", "--quiet", str(repository)])
        _run_git(["-C", str(repository), "bundle", "verify", str(bundle)])
        _run_git(
            [
                "-C",
                str(repository),
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                str(bundle),
                source["branch_ref"] + ":refs/verify/candidate",
            ]
        )
        candidate = _run_git(
            ["-C", str(repository), "rev-parse", "refs/verify/candidate"]
        )
        _expect(candidate == source["candidate_commit"], server + "_bundle_candidate_mismatch")
        candidate_tree = _run_git(
            ["-C", str(repository), "rev-parse", candidate + "^{tree}"]
        )
        _expect(
            candidate_tree == source["candidate_tree"],
            server + "_bundle_candidate_tree_mismatch",
        )
        production = source["production_commit"]
        production_tree = _run_git(
            ["-C", str(repository), "rev-parse", production + "^{tree}"]
        )
        _expect(
            production_tree == source["production_tree"],
            server + "_bundle_production_tree_mismatch",
        )
        _run_git(
            [
                "-C",
                str(repository),
                "merge-base",
                "--is-ancestor",
                production,
                candidate,
            ]
        )
        ahead = int(
            _run_git(
                [
                    "-C",
                    str(repository),
                    "rev-list",
                    "--count",
                    production + ".." + candidate,
                ]
            )
        )
        _expect(
            ahead == source["ahead_count"],
            server + "_bundle_ahead_count_mismatch",
        )


def _validate_source(source: Mapping[str, Any], server: str) -> None:
    _expect(source.get("server") == server, server + "_server_binding_mismatch")
    production = source.get("production_commit")
    candidate = source.get("candidate_commit")
    remote = source.get("remote_commit")
    _expect(isinstance(production, str) and bool(HEX40.fullmatch(production)), server + "_production_commit_invalid")
    _expect(isinstance(candidate, str) and bool(HEX40.fullmatch(candidate)), server + "_candidate_commit_invalid")
    _expect(candidate != production, server + "_candidate_equals_production")
    _expect(candidate == remote, server + "_remote_commit_mismatch")
    _expect(source.get("clean") is True, server + "_candidate_not_clean")
    _expect(isinstance(source.get("ahead_count"), int) and source["ahead_count"] > 0, server + "_ahead_count_invalid")


def verify_release_package(
    package: Mapping[str, Any],
    *,
    artifact_root: Path,
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(package), key=lambda error: list(error.absolute_path))
    if errors:
        raise ReleasePackageError("package_schema_invalid")
    _expect(package.get("schema_version") == PACKAGE_SCHEMA, "package_schema_version_invalid")
    root = _secure_root(artifact_root)
    artifacts = package["artifacts"]
    _expect(set(artifacts) == set(REQUIRED_ARTIFACTS), "artifact_set_invalid")
    paths = [artifacts[name]["path"] for name in REQUIRED_ARTIFACTS]
    _expect(len(paths) == len(set(paths)), "artifact_paths_not_unique")

    resolved: dict[str, Path] = {}
    actual_hashes: dict[str, str] = {}
    for name in REQUIRED_ARTIFACTS:
        path = _secure_artifact(root, artifacts[name]["path"])
        digest = sha256_file(path)
        _expect(digest == artifacts[name]["sha256"], name + "_sha256_mismatch")
        resolved[name] = path
        actual_hashes[name] = digest

    sources = package["sources"]
    _validate_source(sources["backend"], "seebx")
    _validate_source(sources["frontend"], "verbalsage")
    _verify_source_bundle(
        resolved["backend_source_bundle"],
        sources["backend"],
        "seebx",
    )
    _verify_source_bundle(
        resolved["frontend_source_bundle"],
        sources["frontend"],
        "verbalsage",
    )

    frontend_build = _read_json(
        resolved["frontend_build_manifest"],
        "frontend_build_manifest_invalid",
    )
    frontend = sources["frontend"]
    _expect(
        frontend_build.get("schema_version")
        == "verbalsage-build-artifact-manifest-v1"
        and frontend_build.get("status") == "pass",
        "frontend_build_manifest_state_invalid",
    )
    _expect(
        frontend_build.get("source_commit") == frontend["candidate_commit"]
        and frontend_build.get("source_tree") == frontend["candidate_tree"],
        "frontend_build_source_binding_mismatch",
    )
    _expect(
        frontend_build.get("archive_sha256")
        == actual_hashes["frontend_build_archive"],
        "frontend_build_archive_binding_mismatch",
    )
    _expect(
        frontend_build.get("production_environment_preflight") == "pass"
        and frontend_build.get("artifact_preflight") == "pass"
        and isinstance(frontend_build.get("static_page_count"), int)
        and frontend_build["static_page_count"] > 0,
        "frontend_build_verification_incomplete",
    )

    runtime = _read_json(resolved["runtime_receipt"], "runtime_receipt_invalid")
    runtime_binding = package["runtime_binding"]
    _expect(runtime.get("schema_version") == RUNTIME_SCHEMA, "runtime_receipt_schema_invalid")
    _expect(runtime.get("status") == "pass" and runtime.get("activated") is False, "runtime_receipt_state_invalid")
    runtime_repository = runtime.get("repository") or {}
    runtime_details = runtime.get("runtime") or {}
    wheelhouse = runtime.get("wheelhouse") or {}
    backend = sources["backend"]
    _expect(runtime_repository.get("commit") == backend["candidate_commit"], "runtime_backend_commit_mismatch")
    _expect(runtime_repository.get("tree") == backend["candidate_tree"], "runtime_backend_tree_mismatch")
    _expect(runtime_binding["repository_commit"] == backend["candidate_commit"], "declared_runtime_commit_mismatch")
    _expect(runtime_binding["repository_tree"] == backend["candidate_tree"], "declared_runtime_tree_mismatch")
    _expect(runtime_binding["runtime_lock_sha256"] == runtime.get("runtime_lock_sha256", wheelhouse.get("lock_sha256")), "runtime_lock_binding_mismatch")
    _expect(runtime_binding["wheelhouse_manifest_sha256"] == wheelhouse.get("manifest_sha256"), "wheelhouse_manifest_binding_mismatch")
    dependencies = runtime_details.get("dependency_preflight", {}).get("dependencies", {})
    _expect(dependencies.get("verified_count") == 12 and dependencies.get("expected_count") == 12, "runtime_dependency_count_mismatch")
    _expect(runtime_details.get("import_count") == 12, "runtime_import_count_mismatch")

    migration_package = _read_json(
        resolved["migration_package"],
        "migration_package_invalid",
    )
    migration = _read_json(resolved["migration_receipt"], "migration_receipt_invalid")
    migration_binding = package["migration_binding"]
    _expect(
        migration_package.get("schema_version")
        == "seebx-zep-chat-history-migration-package-v1",
        "migration_package_schema_invalid",
    )
    execution = migration_package.get("execution") or {}
    _expect(
        execution.get("production_database_mutated") is False
        and execution.get("zep_called") is False
        and execution.get("temporary_database_required") is True
        and execution.get("forward_and_rollback_required") is True
        and execution.get("exact_baseline_restoration_required") is True,
        "migration_package_execution_boundary_invalid",
    )
    _expect(migration.get("schema_version") == MIGRATION_SCHEMA and migration.get("status") == "pass", "migration_receipt_state_invalid")
    _expect(
        migration.get("bindings", {}).get("package_sha256")
        == actual_hashes["migration_package"],
        "migration_package_receipt_binding_mismatch",
    )
    _expect(migration.get("temporary_database_dropped") is True, "migration_temporary_database_not_dropped")
    _expect(migration.get("rollback", {}).get("exact_baseline_restored") is True, "migration_exact_rollback_missing")
    _expect(migration.get("forward", {}).get("outbox_rows") == 0, "migration_forward_outbox_not_empty")
    _expect(migration_binding["backup_sha256"] == migration.get("source", {}).get("backup_sha256"), "migration_backup_binding_mismatch")
    _expect(migration_binding["recovery_receipt_sha256"] == migration.get("source", {}).get("recovery_receipt_sha256"), "migration_recovery_binding_mismatch")
    _expect(migration_binding["backup_sha256"] == actual_hashes["database_backup"], "migration_backup_artifact_mismatch")
    _expect(migration_binding["recovery_receipt_sha256"] == actual_hashes["recovery_receipt"], "migration_recovery_artifact_mismatch")

    recovery = _read_json(resolved["recovery_receipt"], "recovery_receipt_invalid")
    recovery_binding = package["recovery_binding"]
    _expect(recovery.get("schema_version") == RECOVERY_SCHEMA and recovery.get("status") == "pass", "recovery_receipt_state_invalid")
    restore = recovery.get("restore") or {}
    backup = recovery.get("backup") or {}
    _expect(restore.get("verified") is True and restore.get("disposable_database_dropped") is True, "recovery_restore_state_invalid")
    _expect(backup.get("sha256") == actual_hashes["database_backup"], "recovery_backup_artifact_mismatch")
    _expect(recovery_binding["backup_sha256"] == actual_hashes["database_backup"], "declared_recovery_backup_mismatch")

    package_bytes = (json.dumps(package, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "package_integrity_ready": True,
        "production_activation_authorized": False,
        "artifact_count": len(REQUIRED_ARTIFACTS),
        "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
        "backend_commit": backend["candidate_commit"],
        "frontend_commit": sources["frontend"]["candidate_commit"],
        "backend_source_bundle_sha256": actual_hashes["backend_source_bundle"],
        "frontend_source_bundle_sha256": actual_hashes["frontend_source_bundle"],
        "frontend_build_archive_sha256": actual_hashes["frontend_build_archive"],
        "frontend_build_manifest_sha256": actual_hashes["frontend_build_manifest"],
        "runtime_receipt_sha256": actual_hashes["runtime_receipt"],
        "migration_receipt_sha256": actual_hashes["migration_receipt"],
        "migration_package_sha256": actual_hashes["migration_package"],
        "recovery_receipt_sha256": actual_hashes["recovery_receipt"],
        "database_backup_sha256": actual_hashes["database_backup"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "ops/releases/seebx_atomic_release_package_v1.schema.json",
    )
    arguments = parser.parse_args(argv)
    exit_code = 0
    try:
        package = _read_json(arguments.package, "package_invalid")
        schema = _read_json(arguments.schema, "schema_invalid")
        result = verify_release_package(package, artifact_root=arguments.artifact_root, schema=schema)
    except ReleasePackageError as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": "release_package_invalid",
            "reason": str(error),
        }
        exit_code = 2
    except Exception:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": "release_package_unexpected_error",
        }
        exit_code = 3
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

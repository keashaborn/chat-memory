#!/usr/bin/env python3
"""Fail-closed verifier for the test-only V5 production recovery path."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid


PLAN_CONTRACT = "memory_v1_projection_v5_production_recovery_plan_v1"
AUTH_CONTRACT = "memory_v1_projection_v5_production_recovery_authorization_v1"
QUALIFIED_RE = re.compile(r"^memory\.[a-z][a-z0-9_]*$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class RecoveryError(RuntimeError):
    pass


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RecoveryError(f"JSON root must be an object: {path}")
    return value


def canonical_sha256(value: dict) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RecoveryError(f"command failed: {' '.join(args)}: {detail.strip()}") from exc
    return result.stdout.strip()


def git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args])


def psql(plan: dict, sql: str) -> str:
    target = plan["target"]
    return run(
        [
            "docker",
            "exec",
            target["postgres_container"],
            "psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            target["maintenance_role"],
            "-d",
            target["database"],
            "-c",
            sql,
        ]
    )


def validate_identifiers(values: object, field: str) -> list[str]:
    if not isinstance(values, list) or not values:
        raise RecoveryError(f"{field} must be a nonempty list")
    if len(values) != len(set(values)):
        raise RecoveryError(f"{field} contains duplicates")
    if any(not isinstance(value, str) or not QUALIFIED_RE.fullmatch(value) for value in values):
        raise RecoveryError(f"{field} contains an invalid identifier")
    return values


def validate_plan(plan: dict, repo: Path) -> dict:
    errors: list[str] = []
    if plan.get("contract_version") != PLAN_CONTRACT:
        errors.append("unexpected recovery plan contract")
    if plan.get("production_authorized") is not False:
        errors.append("committed recovery plan must remain unauthorized")
    target = plan.get("target", {})
    expected_target = {
        "server": "seebx",
        "postgres_container": "brains-postgres-1",
        "database": "memory",
        "maintenance_role": "sage",
        "application_role": "brains_app",
        "postgres_major": 16,
    }
    if target != expected_target:
        errors.append("recovery target is not exact")
    auth_policy = plan.get("authorization_policy", {})
    if auth_policy != {
        "contract_version": AUTH_CONTRACT,
        "file_mode": "0600",
        "maximum_validity_minutes": 30,
        "environment_gate": "MEMORY_V1_PRODUCTION_RECOVERY=authorized",
    }:
        errors.append("recovery authorization policy is not exact")

    source = plan.get("source", {})
    ancestor = source.get("required_ancestor_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", ancestor):
        errors.append("required ancestor must be a full commit hash")

    source_files = plan.get("source_files")
    if not isinstance(source_files, list) or len(source_files) != 4:
        errors.append("recovery plan must contain exactly four test files")
        source_files = []
    repo_resolved = repo.resolve()
    verified_files: list[dict] = []
    ordinals: list[int] = []
    seen: set[str] = set()
    for entry in source_files:
        path = entry.get("path") if isinstance(entry, dict) else None
        sha = entry.get("sha256") if isinstance(entry, dict) else None
        ordinal = entry.get("ordinal") if isinstance(entry, dict) else None
        if not isinstance(path, str) or path in seen:
            errors.append("invalid or duplicate recovery test path")
            continue
        seen.add(path)
        candidate = (repo / path).resolve()
        if repo_resolved not in candidate.parents:
            errors.append(f"test path escapes repository: {path}")
            continue
        if not SHA_RE.fullmatch(str(sha)):
            errors.append(f"invalid test hash: {path}")
            continue
        if not isinstance(ordinal, int):
            errors.append(f"invalid test ordinal: {path}")
            continue
        ordinals.append(ordinal)
        if not candidate.is_file():
            errors.append(f"missing recovery test: {path}")
            continue
        actual = file_sha256(candidate)
        if actual != sha:
            errors.append(f"recovery test hash mismatch: {path}")
        verified_files.append({"path": path, "sha256": actual})
    if sorted(ordinals) != [1, 2, 3, 4]:
        errors.append("recovery test ordinals must be exactly 1..4")

    try:
        installed = validate_identifiers(plan.get("required_installed_tables"), "required_installed_tables")
        empty = validate_identifiers(plan.get("required_empty_tables"), "required_empty_tables")
        if not set(empty).issubset(installed):
            errors.append("required_empty_tables must be installed tables")
    except RecoveryError as exc:
        errors.append(str(exc))

    failed = plan.get("failed_install", {})
    required_failed_fields = {
        "run_id",
        "authorization_id",
        "original_plan_sha256",
        "original_head_commit",
        "status_file",
        "expected_phase",
        "expected_exit_code",
        "baseline_tables_file",
        "baseline_counts_file",
        "backup_file",
        "backup_catalog",
        "backup_sha256",
    }
    if set(failed) != required_failed_fields:
        errors.append("failed_install keys are not exact")
    if not SHA_RE.fullmatch(str(failed.get("backup_sha256", ""))):
        errors.append("failed install backup hash is invalid")
    if failed.get("expected_phase") != "rolled_back_security_tests" or failed.get("expected_exit_code") != 3:
        errors.append("failed install phase/exit contract is invalid")

    registry = plan.get("predicate_registry")
    if registry != {
        "registry_version": "memory_predicate_registry_v5",
        "registry_sha256": "4d626433109c89c18d5ea374e173ca6785de6f9c20ecc05fef9f6447bfc671f4",
        "status": "proposed",
        "runtime_active": False,
        "expected_contract_rows": 44,
    }:
        errors.append("predicate registry recovery boundary is not exact")

    required_forbidden = {
        "install_or_modify_schema_or_functions",
        "invoke_live_extraction",
        "stage_live_projection_plan",
        "invoke_live_durable_apply",
        "write_qdrant_or_redis",
        "modify_user_memory_rows",
    }
    if not required_forbidden.issubset(set(plan.get("forbidden_effects", []))):
        errors.append("recovery forbidden effects are incomplete")

    head = None
    try:
        head = git(repo, "rev-parse", "HEAD")
        git(repo, "merge-base", "--is-ancestor", ancestor, head)
    except RecoveryError as exc:
        errors.append(str(exc))
    if errors:
        raise RecoveryError("; ".join(errors))
    return {
        "contract_version": PLAN_CONTRACT,
        "plan_id": plan["plan_id"],
        "plan_sha256": canonical_sha256(plan),
        "head_commit": head,
        "production_authorized": False,
        "verified_source_files": verified_files,
        "repository_valid": True,
    }


def parse_utc(value: object, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecoveryError(f"{field} must be ISO-8601 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise RecoveryError(f"{field} must use UTC")
    return parsed


def validate_authorization(path: Path, plan: dict, verification: dict, repo: Path) -> dict:
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RecoveryError("recovery authorization mode must be 0600")
    auth = load_json(path)
    expected_keys = {
        "contract_version",
        "authorization_id",
        "authorized",
        "authorized_by",
        "authorized_at",
        "expires_at",
        "plan_sha256",
        "expected_head_commit",
        "target_server",
        "failed_run_id",
    }
    if set(auth) != expected_keys:
        raise RecoveryError("recovery authorization keys are not exact")
    if auth["contract_version"] != AUTH_CONTRACT or auth["authorized"] is not True:
        raise RecoveryError("recovery authorization contract is invalid")
    if not isinstance(auth["authorized_by"], str) or not auth["authorized_by"].strip():
        raise RecoveryError("recovery authorized_by is required")
    try:
        uuid.UUID(auth["authorization_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise RecoveryError("authorization_id must be a UUID") from exc
    if auth["plan_sha256"] != verification["plan_sha256"]:
        raise RecoveryError("recovery authorization plan hash mismatch")
    head = git(repo, "rev-parse", "HEAD")
    if auth["expected_head_commit"] != head:
        raise RecoveryError("recovery authorization commit mismatch")
    if auth["target_server"] != "seebx":
        raise RecoveryError("recovery authorization server mismatch")
    if auth["failed_run_id"] != plan["failed_install"]["run_id"]:
        raise RecoveryError("recovery authorization failed-run mismatch")
    issued = parse_utc(auth["authorized_at"], "authorized_at")
    expires = parse_utc(auth["expires_at"], "expires_at")
    now = dt.datetime.now(dt.timezone.utc)
    if expires <= issued or expires - issued > dt.timedelta(minutes=30):
        raise RecoveryError("recovery authorization window is invalid")
    if now < issued - dt.timedelta(seconds=30) or now >= expires:
        raise RecoveryError("recovery authorization is not currently valid")
    return {
        "authorization_id": auth["authorization_id"],
        "expected_head_commit": head,
        "expires_at": auth["expires_at"],
        "authorization_valid": True,
    }


def parse_status(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            raise RecoveryError(f"invalid status line: {line!r}")
        key, value = line.split("=", 1)
        result[key] = value
    return result


def parse_baseline(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        table, count = line.split("\t", 1)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", table) or table in result:
            raise RecoveryError("invalid baseline table file")
        result[table] = int(count)
    return result


def production_preflight(plan: dict) -> dict:
    failed = plan["failed_install"]
    status_path = Path(failed["status_file"])
    backup_path = Path(failed["backup_file"])
    catalog_path = Path(failed["backup_catalog"])
    baseline_tables_path = Path(failed["baseline_tables_file"])
    baseline_counts_path = Path(failed["baseline_counts_file"])
    for path in (
        status_path,
        backup_path,
        catalog_path,
        baseline_tables_path,
        baseline_counts_path,
    ):
        if not path.is_file() or path.stat().st_size == 0:
            raise RecoveryError(f"required recovery artifact missing or empty: {path}")
    status = parse_status(status_path)
    baseline = parse_baseline(baseline_counts_path)
    listed_tables = baseline_tables_path.read_text(encoding="utf-8").splitlines()
    if listed_tables != list(baseline):
        raise RecoveryError("baseline table and count files disagree")

    current_counts = {
        table: int(psql(plan, f'SELECT count(*) FROM memory."{table}";'))
        for table in baseline
    }
    installed = plan["required_installed_tables"]
    array_sql = "ARRAY[" + ",".join(f"'{name}'" for name in installed) + "]::text[]"
    missing = psql(
        plan,
        f"SELECT COALESCE(string_agg(name, ',' ORDER BY name),'') "
        f"FROM unnest({array_sql}) item(name) WHERE to_regclass(name) IS NULL;",
    )
    nonempty: dict[str, int] = {}
    direct_privileges: list[str] = []
    for qualified in plan["required_empty_tables"]:
        table = qualified.removeprefix("memory.")
        count = int(psql(plan, f'SELECT count(*) FROM memory."{table}";'))
        if count:
            nonempty[qualified] = count
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            if psql(plan, f"SELECT has_table_privilege('brains_app','{qualified}','{privilege}')") == "t":
                direct_privileges.append(f"{qualified}:{privilege}")

    role_safe = psql(
        plan,
        "SELECT (NOT rolcanlogin AND NOT rolinherit AND NOT rolbypassrls "
        "AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole)::text "
        "FROM pg_roles WHERE rolname='memory_v5_writer';",
    ) == "true"
    registry = psql(
        plan,
        "SELECT concat_ws('|',registry_version,registry_sha256,status,runtime_active::text,"
        "(SELECT count(*) FROM memory.predicate_contract WHERE registry_version='memory_predicate_registry_v5')) "
        "FROM memory.predicate_registry_version WHERE registry_version='memory_predicate_registry_v5';",
    )
    expected_registry = (
        "memory_predicate_registry_v5|"
        "4d626433109c89c18d5ea374e173ca6785de6f9c20ecc05fef9f6447bfc671f4|"
        "proposed|false|44"
    )
    checks = {
        "failed_run_matches": status.get("run_id") == failed["run_id"],
        "failed_phase_matches": status.get("phase") == failed["expected_phase"],
        "failed_exit_code_matches": status.get("exit_code") == str(failed["expected_exit_code"]),
        "backup_sha256_matches": file_sha256(backup_path) == failed["backup_sha256"],
        "baseline_counts_unchanged": current_counts == baseline,
        "all_v5_tables_installed": missing == "",
        "all_v5_data_tables_empty": nonempty == {},
        "memory_v5_writer_safe": role_safe,
        "brains_app_direct_v5_privileges_absent": direct_privileges == [],
        "predicate_registry_proposed_inactive": registry == expected_registry,
        "temporary_test_role_absent": psql(
            plan, "SELECT count(*) FROM pg_roles WHERE rolname='memory_v5_clone_writer';"
        ) == "0",
    }
    return {
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "checks": checks,
        "baseline_counts": baseline,
        "current_counts": current_counts,
        "missing_installed_tables": missing.split(",") if missing else [],
        "nonempty_v5_tables": nonempty,
        "direct_application_privileges": direct_privileges,
        "production_writes": 0,
        "ready_for_authorization": all(checks.values()),
        "ready_for_execution": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--production-preflight", action="store_true")
    parser.add_argument("--list-tests", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        plan = load_json(args.manifest)
        verification = validate_plan(plan, args.repo_root)
        if args.list_tests:
            for entry in sorted(plan["source_files"], key=lambda item: item["ordinal"]):
                print(entry["path"])
            return 0
        result: dict = {"plan_verification": verification}
        if args.authorization:
            result["authorization_verification"] = validate_authorization(
                args.authorization, plan, verification, args.repo_root
            )
        if args.production_preflight:
            result["production_preflight"] = production_preflight(plan)
            if not result["production_preflight"]["ready_for_authorization"]:
                raise RecoveryError("production recovery preflight failed")
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(encoded, encoding="utf-8")
        sys.stdout.write(encoded)
        return 0
    except (RecoveryError, OSError, ValueError) as exc:
        sys.stderr.write(f"memory_v1_projection_v5_recovery: FAIL: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

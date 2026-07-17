#!/usr/bin/env python3
"""Fail-closed verifier for the V5 component/projection production upgrade.

Repository verification and production preflight are read-only. This program
does not stop timers, create backups, install schema, or activate runtime code.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid


PLAN_CONTRACT = "memory_v1_v5_component_projection_install_plan_v1"
AUTH_CONTRACT = "memory_v1_v5_component_projection_install_authorization_v1"
RECOVERY_CONTRACT = "memory_v1_v5_component_projection_recovery_plan_v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
QUALIFIED_TABLE_RE = re.compile(r"^memory\.[a-z][a-z0-9_]*$")
QUALIFIED_COLUMN_RE = re.compile(
    r"^memory\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"
)
QUALIFIED_CONSTRAINT_RE = re.compile(
    r"^memory\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"
)
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
UNIT_RE = re.compile(r"^memory-v1-[a-z0-9-]+\.timer$")
SAFE_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class PlanError(RuntimeError):
    pass


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PlanError(f"JSON root must be an object: {path}")
    return value


def canonical_sha256(value: dict) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(args: list[str], *, input_text: str | None = None) -> str:
    try:
        result = subprocess.run(
            args,
            input=input_text,
            text=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise PlanError(f"command failed: {' '.join(args)}: {detail.strip()}") from exc
    return result.stdout.strip()


def git(repo_root: Path, *args: str) -> str:
    return run(["git", "-C", str(repo_root), *args])


def _validate_identifier_list(
    errors: list[str], name: str, values: object, pattern: re.Pattern[str]
) -> list[str]:
    if not isinstance(values, list) or not values:
        errors.append(f"{name} must be a nonempty list")
        return []
    if len(values) != len(set(values)):
        errors.append(f"{name} contains duplicates")
    if any(not isinstance(value, str) or not pattern.fullmatch(value) for value in values):
        errors.append(f"{name} contains an invalid identifier")
    return values


def validate_plan(plan: dict, repo_root: Path, *, require_git: bool = True) -> dict:
    errors: list[str] = []
    if plan.get("contract_version") != PLAN_CONTRACT:
        errors.append("unexpected plan contract_version")
    if plan.get("production_authorized") is not False:
        errors.append("committed plan must permanently set production_authorized=false")

    source = plan.get("source")
    target = plan.get("target")
    auth = plan.get("authorization_policy")
    backup = plan.get("backup_policy")
    quiescence = plan.get("maintenance_quiescence")
    preinstall = plan.get("preinstall_requirements")
    postinstall = plan.get("postinstall_requirements")
    for name, value in (
        ("source", source),
        ("target", target),
        ("authorization_policy", auth),
        ("backup_policy", backup),
        ("maintenance_quiescence", quiescence),
        ("preinstall_requirements", preinstall),
        ("postinstall_requirements", postinstall),
    ):
        if not isinstance(value, dict):
            errors.append(f"{name} must be an object")
    if errors:
        raise PlanError("; ".join(errors))

    ancestor = source.get("required_ancestor_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", ancestor):
        errors.append("required_ancestor_commit must be a full lowercase commit hash")
    if source.get("branch") != "memory_v1_v5_component_scope":
        errors.append("source.branch mismatch")
    expected_target = {
        "server": "seebx",
        "postgres_container": "brains-postgres-1",
        "database": "memory",
        "maintenance_role": "sage",
        "application_role": "brains_app",
        "postgres_major": 16,
        "private_schema": "memory",
    }
    if target != expected_target:
        errors.append("target contract is not exact")

    expected_auth = {
        "required": True,
        "contract_version": AUTH_CONTRACT,
        "file_mode": "0600",
        "maximum_validity_minutes": 30,
        "bind_plan_sha256": True,
        "bind_exact_head_commit": True,
        "environment_gate": "MEMORY_V1_COMPONENT_PROJECTION_INSTALL=authorized",
    }
    if auth != expected_auth:
        errors.append("authorization policy is not exact")

    expected_backup = {
        "format": "postgres_custom",
        "destination_directory": "/home/ubuntu/brains/snapshots",
        "file_mode": "0600",
        "require_nonzero_bytes": True,
        "require_sha256": True,
        "require_restore_catalog": True,
        "must_complete_before_first_schema_write": True,
        "minimum_free_space_multiplier": 2,
    }
    if backup != expected_backup:
        errors.append("backup policy is not exact")

    expected_units = [
        "memory-v1-projection.timer",
        "memory-v1-governance.timer",
        "memory-v1-evidence-intake-dispatcher.timer",
        "memory-v1-v5-chat-capture.timer",
        "memory-v1-deferred-reconciliation-scan.timer",
    ]
    if quiescence != {
        "temporary_only": True,
        "restore_exact_active_state_on_exit": True,
        "configuration_changes_allowed": False,
        "units": expected_units,
    }:
        errors.append("maintenance quiescence contract is not exact")
    if any(not UNIT_RE.fullmatch(unit) for unit in quiescence.get("units", [])):
        errors.append("maintenance quiescence contains an unsafe unit")

    _validate_identifier_list(
        errors, "required_tables", preinstall.get("required_tables"), QUALIFIED_TABLE_RE
    )
    _validate_identifier_list(
        errors,
        "required_absent_tables",
        preinstall.get("required_absent_tables"),
        QUALIFIED_TABLE_RE,
    )
    _validate_identifier_list(
        errors,
        "required_absent_columns",
        preinstall.get("required_absent_columns"),
        QUALIFIED_COLUMN_RE,
    )
    _validate_identifier_list(
        errors, "required_roles", preinstall.get("required_roles"), ROLE_RE
    )
    functions = preinstall.get("required_functions")
    if not isinstance(functions, list) or not functions or len(functions) != len(set(functions)):
        errors.append("preinstall required_functions is invalid")
    if preinstall.get("required_extensions") != ["pgcrypto", "unaccent"]:
        errors.append("required extensions must be exactly pgcrypto and unaccent")

    source_files = plan.get("source_files")
    if not isinstance(source_files, list) or not source_files:
        errors.append("source_files must be a nonempty list")
        source_files = []
    expected_ordinals = {
        "migration": list(range(1, 6)),
        "logical_rollback": [1],
        "rolled_back_test": list(range(1, 6)),
        "clone_rehearsal": list(range(1, 5)),
        "recovery_plan": [1],
    }
    seen_paths: set[str] = set()
    ordinals: dict[str, list[int]] = {}
    verified_files: list[dict] = []
    repo_resolved = repo_root.resolve()
    for entry in source_files:
        if not isinstance(entry, dict) or set(entry) != {
            "path", "sha256", "kind", "ordinal"
        }:
            errors.append("source_files entries must have exact keys")
            continue
        relpath = entry["path"]
        expected_sha = entry["sha256"]
        kind = entry["kind"]
        ordinal = entry["ordinal"]
        if not isinstance(relpath, str) or not relpath or relpath in seen_paths:
            errors.append(f"invalid or duplicate source path: {relpath!r}")
            continue
        seen_paths.add(relpath)
        candidate = (repo_root / relpath).resolve()
        if repo_resolved not in candidate.parents:
            errors.append(f"source path escapes repository: {relpath}")
            continue
        if not SHA256_RE.fullmatch(str(expected_sha)):
            errors.append(f"invalid sha256 for {relpath}")
            continue
        if kind not in expected_ordinals:
            errors.append(f"invalid source kind for {relpath}")
        if not isinstance(ordinal, int) or ordinal < 1:
            errors.append(f"invalid source ordinal for {relpath}")
        else:
            ordinals.setdefault(str(kind), []).append(ordinal)
        if not candidate.is_file():
            errors.append(f"source file missing: {relpath}")
            continue
        actual_sha = file_sha256(candidate)
        if actual_sha != expected_sha:
            errors.append(f"source hash mismatch: {relpath}")
        if kind == "recovery_plan":
            recovery = load_json(candidate)
            if recovery.get("contract_version") != RECOVERY_CONTRACT:
                errors.append("recovery plan contract mismatch")
            if recovery.get("production_authorized") is not False:
                errors.append("recovery plan must remain unauthorized")
        verified_files.append({"path": relpath, "sha256": actual_sha})
    for kind, expected in expected_ordinals.items():
        if sorted(ordinals.get(kind, [])) != expected:
            errors.append(f"{kind} ordinals must be exactly {expected}")

    required_forbidden = {
        "change_systemd_enablement_or_unit_files",
        "change_environment_or_configuration",
        "restart_brains_or_verbal_sage",
        "register_project_or_component_rows",
        "invoke_live_extraction_or_projection_apply",
        "write_qdrant_or_redis",
        "activate_retrieval_or_prompt_influence",
        "delete_redact_or_restore_user_data",
    }
    forbidden = plan.get("forbidden_effects")
    if not isinstance(forbidden, list) or not required_forbidden.issubset(forbidden):
        errors.append("forbidden_effects is missing a required boundary")

    _validate_identifier_list(
        errors, "postinstall required_tables", postinstall.get("required_tables"), QUALIFIED_TABLE_RE
    )
    _validate_identifier_list(
        errors, "postinstall required_columns", postinstall.get("required_columns"), QUALIFIED_COLUMN_RE
    )
    _validate_identifier_list(
        errors,
        "postinstall required_constraints",
        postinstall.get("required_constraints"),
        QUALIFIED_CONSTRAINT_RE,
    )
    _validate_identifier_list(
        errors,
        "postinstall required_empty_tables",
        postinstall.get("required_empty_tables"),
        QUALIFIED_TABLE_RE,
    )
    for key, expected in (
        ("all_component_tables_force_rls", True),
        ("component_tables_owned_by_sage", True),
        ("preexisting_memory_rows_unchanged", True),
        ("qdrant_memory_claim_v1_unchanged", True),
        ("timer_enablement_and_active_state_restored", True),
        ("component_rows_created", 0),
        ("runtime_activated", False),
    ):
        if postinstall.get(key) != expected:
            errors.append(f"postinstall {key} mismatch")
    if postinstall.get("brains_app_component_table_privileges") != []:
        errors.append("brains_app component table privileges must be empty")
    if postinstall.get("public_component_table_privileges") != []:
        errors.append("PUBLIC component privileges must be empty")

    head = None
    if require_git:
        try:
            head = git(repo_root, "rev-parse", "HEAD")
            git(repo_root, "merge-base", "--is-ancestor", ancestor, head)
        except PlanError as exc:
            errors.append(str(exc))
    if errors:
        raise PlanError("; ".join(errors))
    return {
        "contract_version": PLAN_CONTRACT,
        "plan_id": plan["plan_id"],
        "plan_sha256": canonical_sha256(plan),
        "production_authorized": False,
        "head_commit": head,
        "verified_source_files": verified_files,
        "repository_valid": True,
    }


def parse_utc(value: str, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PlanError(f"authorization {field} must be ISO-8601 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise PlanError(f"authorization {field} must use UTC")
    return parsed


def validate_authorization(
    path: Path, plan: dict, verification: dict, repo_root: Path
) -> dict:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise PlanError(f"authorization file mode must be 0600, got {mode:04o}")
    authorization = load_json(path)
    expected_keys = {
        "contract_version", "authorization_id", "authorized", "authorized_by",
        "authorized_at", "expires_at", "plan_sha256", "expected_head_commit",
        "target_server",
    }
    if set(authorization) != expected_keys:
        raise PlanError("authorization keys do not exactly match the contract")
    if authorization["contract_version"] != AUTH_CONTRACT:
        raise PlanError("authorization contract mismatch")
    if authorization["authorized"] is not True:
        raise PlanError("authorization flag is not true")
    try:
        uuid.UUID(authorization["authorization_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise PlanError("authorization_id must be a UUID") from exc
    if authorization["target_server"] != "seebx":
        raise PlanError("authorization target_server mismatch")
    if authorization["plan_sha256"] != verification["plan_sha256"]:
        raise PlanError("authorization plan_sha256 mismatch")
    head = git(repo_root, "rev-parse", "HEAD")
    if authorization["expected_head_commit"] != head:
        raise PlanError("authorization expected_head_commit mismatch")
    issued = parse_utc(authorization["authorized_at"], "authorized_at")
    expires = parse_utc(authorization["expires_at"], "expires_at")
    maximum = dt.timedelta(minutes=plan["authorization_policy"]["maximum_validity_minutes"])
    now = dt.datetime.now(dt.timezone.utc)
    if expires <= issued or expires - issued > maximum:
        raise PlanError("authorization validity window is invalid")
    if now < issued - dt.timedelta(seconds=30) or now >= expires:
        raise PlanError("authorization is not currently valid")
    return {
        "authorization_id": authorization["authorization_id"],
        "authorized_by": authorization["authorized_by"],
        "authorized_at": authorization["authorized_at"],
        "expires_at": authorization["expires_at"],
        "expected_head_commit": head,
        "authorization_valid": True,
    }


def psql(plan: dict, sql: str) -> str:
    target = plan["target"]
    return run([
        "docker", "exec", target["postgres_container"], "psql", "-X", "-A", "-t",
        "-v", "ON_ERROR_STOP=1", "-U", target["maintenance_role"], "-d",
        target["database"], "-c", sql,
    ])


def sql_text_array(values: list[str]) -> str:
    if any("'" in value for value in values):
        raise PlanError("unsafe SQL array value")
    return "ARRAY[" + ",".join(f"'{value}'" for value in values) + "]::text[]"


def unit_state(unit: str, operation: str) -> str:
    result = subprocess.run(
        ["systemctl", operation, unit], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    return result.stdout.strip()


def production_preflight(plan: dict) -> dict:
    preinstall = plan["preinstall_requirements"]
    required_tables = preinstall["required_tables"]
    absent_tables = preinstall["required_absent_tables"]
    required_functions = preinstall["required_functions"]
    required_roles = preinstall["required_roles"]
    required_extensions = preinstall["required_extensions"]

    metadata_sql = f"""
      SELECT jsonb_build_object(
        'current_user', current_user,
        'current_database', current_database(),
        'postgres_version_num', current_setting('server_version_num')::integer,
        'database_size_bytes', pg_database_size(current_database()),
        'missing_tables', COALESCE((SELECT jsonb_agg(name ORDER BY name)
          FROM unnest({sql_text_array(required_tables)}) item(name)
          WHERE to_regclass(name) IS NULL),'[]'::jsonb),
        'existing_component_tables', COALESCE((SELECT jsonb_agg(name ORDER BY name)
          FROM unnest({sql_text_array(absent_tables)}) item(name)
          WHERE to_regclass(name) IS NOT NULL),'[]'::jsonb),
        'missing_functions', COALESCE((SELECT jsonb_agg(name ORDER BY name)
          FROM unnest({sql_text_array(required_functions)}) item(name)
          WHERE to_regprocedure(name) IS NULL),'[]'::jsonb),
        'missing_roles', COALESCE((SELECT jsonb_agg(name ORDER BY name)
          FROM unnest({sql_text_array(required_roles)}) item(name)
          WHERE to_regrole(name) IS NULL),'[]'::jsonb),
        'missing_extensions', COALESCE((SELECT jsonb_agg(name ORDER BY name)
          FROM unnest({sql_text_array(required_extensions)}) item(name)
          WHERE NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname=name)),'[]'::jsonb),
        'roles_safe', COALESCE((SELECT bool_and(
          NOT rolsuper AND NOT rolbypassrls AND NOT rolcreatedb AND NOT rolcreaterole
          AND (rolname='brains_app' OR (NOT rolcanlogin AND NOT rolinherit)))
          FROM pg_roles WHERE rolname=ANY({sql_text_array(required_roles)})),false)
      )::text;
    """
    try:
        metadata = json.loads(psql(plan, metadata_sql))
    except json.JSONDecodeError as exc:
        raise PlanError("production preflight returned invalid metadata JSON") from exc

    missing_columns: list[str] = []
    unexpectedly_present_columns: list[str] = []
    for qualified in preinstall["required_absent_columns"]:
        _, table, column = qualified.split(".")
        value = psql(plan, f"SELECT count(*) FROM information_schema.columns WHERE table_schema='memory' AND table_name='{table}' AND column_name='{column}';")
        if value != "0":
            unexpectedly_present_columns.append(qualified)

    tables_output = psql(
        plan,
        "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' "
        "AND table_type='BASE TABLE' ORDER BY table_name;",
    )
    baseline_counts: dict[str, int] = {}
    for table in filter(None, tables_output.splitlines()):
        if not SAFE_TABLE_RE.fullmatch(table):
            raise PlanError(f"unsafe table identifier from PostgreSQL: {table!r}")
        baseline_counts[table] = int(psql(plan, f'SELECT count(*) FROM memory."{table}";'))

    units = plan["maintenance_quiescence"]["units"]
    timer_states = {
        unit: {
            "enabled": unit_state(unit, "is-enabled"),
            "active": unit_state(unit, "is-active"),
        }
        for unit in units
    }
    snapshot_dir = Path(plan["backup_policy"]["destination_directory"])
    snapshot_exists = snapshot_dir.is_dir()
    snapshot_writable = snapshot_exists and os.access(snapshot_dir, os.W_OK | os.X_OK)
    free_bytes = shutil.disk_usage(snapshot_dir).free if snapshot_exists else 0
    minimum_free = (
        int(metadata["database_size_bytes"])
        * int(plan["backup_policy"]["minimum_free_space_multiplier"])
    )
    host_tools = {
        name: shutil.which(name)
        for name in (
            "curl", "docker", "flock", "git", "jq", "python3", "realpath",
            "sha256sum", "sudo", "systemctl",
        )
    }
    sudo_ready = subprocess.run(
        ["sudo", "-n", "true"], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0
    checks = {
        "maintenance_role_matches": metadata["current_user"] == "sage",
        "database_matches": metadata["current_database"] == "memory",
        "postgres_major_matches": metadata["postgres_version_num"] // 10000 == 16,
        "required_tables_present": metadata["missing_tables"] == [],
        "component_tables_absent": metadata["existing_component_tables"] == [],
        "component_columns_absent": unexpectedly_present_columns == [],
        "required_functions_present": metadata["missing_functions"] == [],
        "required_roles_present": metadata["missing_roles"] == [],
        "required_roles_safe": metadata["roles_safe"] is True,
        "required_extensions_present": metadata["missing_extensions"] == [],
        "timers_enabled_and_active": all(
            state == {"enabled": "enabled", "active": "active"}
            for state in timer_states.values()
        ),
        "snapshot_directory_ready": snapshot_exists and snapshot_writable,
        "snapshot_free_space_sufficient": free_bytes >= minimum_free,
        "host_tools_available": all(host_tools.values()),
        "passwordless_sudo_ready": sudo_ready,
    }
    return {
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "metadata": metadata,
        "unexpectedly_present_columns": unexpectedly_present_columns,
        "baseline_counts": baseline_counts,
        "timer_states": timer_states,
        "snapshot": {
            "directory": str(snapshot_dir),
            "free_bytes": free_bytes,
            "minimum_required_free_bytes": minimum_free,
        },
        "host_tools": host_tools,
        "checks": checks,
        "ready_for_authorization": all(checks.values()),
        "ready_for_execution": False,
        "production_writes": 0,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--production-preflight", action="store_true")
    parser.add_argument(
        "--list-kind",
        choices=("migration", "logical_rollback", "rolled_back_test", "clone_rehearsal", "recovery_plan"),
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        plan = load_json(args.manifest)
        verification = validate_plan(plan, args.repo_root)
        if args.list_kind:
            selected = sorted(
                (entry for entry in plan["source_files"] if entry["kind"] == args.list_kind),
                key=lambda entry: entry["ordinal"],
            )
            sys.stdout.write("".join(f"{entry['path']}\n" for entry in selected))
            return 0
        result: dict = {"plan_verification": verification}
        if args.authorization:
            result["authorization_verification"] = validate_authorization(
                args.authorization, plan, verification, args.repo_root
            )
        if args.production_preflight:
            result["production_preflight"] = production_preflight(plan)
            if not result["production_preflight"]["ready_for_authorization"]:
                raise PlanError("production preflight is not ready for authorization")
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(encoded, encoding="utf-8")
        sys.stdout.write(encoded)
        return 0
    except (PlanError, OSError) as exc:
        sys.stderr.write(f"memory_v1_v5_component_projection_install_plan: FAIL: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

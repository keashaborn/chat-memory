#!/usr/bin/env python3
"""Fail-closed verification for the Memory V1 V5 production install plan.

Repository verification and production preflight are read-only.  This program
does not create backups, install schema, stage extraction, or apply memory.
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


PLAN_CONTRACT = "memory_v1_projection_v5_production_install_plan_v1"
AUTH_CONTRACT = "memory_v1_projection_v5_production_install_authorization_v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
QUALIFIED_OBJECT_RE = re.compile(r"^memory\.[a-z][a-z0-9_]*$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
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
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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


def validate_plan(plan: dict, repo_root: Path, *, require_git: bool = True) -> dict:
    errors: list[str] = []
    if plan.get("contract_version") != PLAN_CONTRACT:
        errors.append("unexpected plan contract_version")
    if plan.get("production_authorized") is not False:
        errors.append("committed plan must permanently set production_authorized=false")

    source = plan.get("source")
    target = plan.get("target")
    backup = plan.get("backup_policy")
    auth_policy = plan.get("authorization_policy")
    preinstall = plan.get("preinstall_requirements")
    postinstall = plan.get("postinstall_requirements")
    for name, value in (
        ("source", source),
        ("target", target),
        ("backup_policy", backup),
        ("authorization_policy", auth_policy),
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
    if target.get("server") != "seebx":
        errors.append("target.server must be seebx")
    for key, expected in (
        ("postgres_container", "brains-postgres-1"),
        ("database", "memory"),
        ("maintenance_role", "sage"),
        ("application_role", "brains_app"),
    ):
        if target.get(key) != expected:
            errors.append(f"target.{key} must be {expected}")
    if target.get("postgres_major") != 16:
        errors.append("target.postgres_major must be 16")
    if target.get("private_schema") != "memory":
        errors.append("target.private_schema must be memory")

    expected_backup = {
        "format": "postgres_custom",
        "file_mode": "0600",
        "require_nonzero_bytes": True,
        "require_sha256": True,
        "require_restore_catalog": True,
        "must_complete_before_first_schema_write": True,
    }
    for key, expected in expected_backup.items():
        if backup.get(key) != expected:
            errors.append(f"backup_policy.{key} must be {expected!r}")

    if auth_policy.get("required") is not True:
        errors.append("authorization_policy.required must be true")
    if auth_policy.get("contract_version") != AUTH_CONTRACT:
        errors.append("unexpected authorization contract_version")
    if auth_policy.get("file_mode") != "0600":
        errors.append("authorization file mode must be 0600")
    if auth_policy.get("maximum_validity_minutes") != 30:
        errors.append("authorization maximum validity must be 30 minutes")
    if auth_policy.get("environment_gate") != "MEMORY_V1_PRODUCTION_INSTALL=authorized":
        errors.append("unexpected production environment gate")

    required_extensions = preinstall.get("required_extensions")
    if required_extensions != ["pgcrypto", "unaccent"]:
        errors.append("required extensions must be exactly pgcrypto and unaccent")
    required_base = preinstall.get("required_base_objects")
    absent_objects = plan.get("preinstall_absent_objects")
    absent_roles = preinstall.get("required_absent_roles")
    for name, values, pattern in (
        ("required_base_objects", required_base, QUALIFIED_OBJECT_RE),
        ("preinstall_absent_objects", absent_objects, QUALIFIED_OBJECT_RE),
        ("required_absent_roles", absent_roles, ROLE_RE),
    ):
        if not isinstance(values, list) or not values:
            errors.append(f"{name} must be a nonempty list")
            continue
        if len(values) != len(set(values)):
            errors.append(f"{name} contains duplicates")
        if any(not isinstance(value, str) or not pattern.fullmatch(value) for value in values):
            errors.append(f"{name} contains an invalid identifier")

    source_files = plan.get("source_files")
    if not isinstance(source_files, list) or not source_files:
        errors.append("source_files must be a nonempty list")
        source_files = []
    seen_paths: set[str] = set()
    ordinals: dict[str, list[int]] = {}
    verified_files: list[dict] = []
    repo_resolved = repo_root.resolve()
    for entry in source_files:
        if not isinstance(entry, dict):
            errors.append("source_files entries must be objects")
            continue
        relpath = entry.get("path")
        expected_sha = entry.get("sha256")
        kind = entry.get("kind")
        ordinal = entry.get("ordinal")
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
        if kind not in {"migration", "rolled_back_test", "clone_rehearsal"}:
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
        verified_files.append({"path": relpath, "sha256": actual_sha})

    for kind, expected in (
        ("migration", list(range(1, 6))),
        ("rolled_back_test", list(range(1, 5))),
        ("clone_rehearsal", [1]),
    ):
        if sorted(ordinals.get(kind, [])) != expected:
            errors.append(f"{kind} ordinals must be exactly {expected}")

    required_forbidden = {
        "start_or_enable_worker_or_timer",
        "modify_systemd_or_environment",
        "invoke_live_extraction",
        "stage_live_projection_plan",
        "invoke_live_durable_apply",
        "write_qdrant_or_redis",
        "change_retrieval_or_prompt_behavior",
        "restart_brains_or_verbal_sage",
    }
    forbidden = plan.get("forbidden_effects")
    if not isinstance(forbidden, list) or not required_forbidden.issubset(forbidden):
        errors.append("forbidden_effects is missing a required production boundary")

    expected_postconditions = {
        "all_synthetic_tests_rolled_back": True,
        "v5_owner_and_staging_tables_empty": True,
        "preexisting_user_data_counts_unchanged": True,
        "memory_v5_writer_nologin_noinherit_nobypassrls": True,
        "brains_app_direct_v5_table_access": False,
        "runtime_active": False,
    }
    for key, expected in expected_postconditions.items():
        if postinstall.get(key) is not expected:
            errors.append(f"postinstall_requirements.{key} must be true/false as specified")
    empty_tables = postinstall.get("empty_tables")
    if not isinstance(empty_tables, list) or not empty_tables:
        errors.append("postinstall_requirements.empty_tables must be a nonempty list")
    elif (
        len(empty_tables) != len(set(empty_tables))
        or any(
            not isinstance(value, str) or not QUALIFIED_OBJECT_RE.fullmatch(value)
            for value in empty_tables
        )
    ):
        errors.append("postinstall_requirements.empty_tables is invalid")
    expected_registry = {
        "registry_version": "memory_predicate_registry_v5",
        "registry_sha256": "4d626433109c89c18d5ea374e173ca6785de6f9c20ecc05fef9f6447bfc671f4",
        "status": "proposed",
        "runtime_active": False,
        "expected_contract_rows": 44,
    }
    if postinstall.get("predicate_registry") != expected_registry:
        errors.append("postinstall predicate registry contract is not exact")

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
    path: Path, plan: dict, plan_verification: dict, repo_root: Path
) -> dict:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise PlanError(f"authorization file mode must be 0600, got {mode:04o}")
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
    }
    if set(auth) != expected_keys:
        raise PlanError("authorization keys do not exactly match the contract")
    if auth["contract_version"] != AUTH_CONTRACT or auth["authorized"] is not True:
        raise PlanError("authorization contract or authorized flag is invalid")
    try:
        uuid.UUID(auth["authorization_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise PlanError("authorization_id must be a UUID") from exc
    if not isinstance(auth["authorized_by"], str) or not auth["authorized_by"].strip():
        raise PlanError("authorized_by is required")
    if auth["target_server"] != plan["target"]["server"]:
        raise PlanError("authorization target_server mismatch")
    if auth["plan_sha256"] != plan_verification["plan_sha256"]:
        raise PlanError("authorization plan_sha256 mismatch")
    head = git(repo_root, "rev-parse", "HEAD")
    if auth["expected_head_commit"] != head:
        raise PlanError("authorization expected_head_commit mismatch")
    issued = parse_utc(auth["authorized_at"], "authorized_at")
    expires = parse_utc(auth["expires_at"], "expires_at")
    maximum = dt.timedelta(minutes=plan["authorization_policy"]["maximum_validity_minutes"])
    now = dt.datetime.now(dt.timezone.utc)
    if expires <= issued or expires - issued > maximum:
        raise PlanError("authorization validity window is invalid")
    if now < issued - dt.timedelta(seconds=30) or now >= expires:
        raise PlanError("authorization is not currently valid")
    return {
        "authorization_id": auth["authorization_id"],
        "authorized_by": auth["authorized_by"],
        "authorized_at": auth["authorized_at"],
        "expires_at": auth["expires_at"],
        "expected_head_commit": head,
        "authorization_valid": True,
    }


def psql(container: str, database: str, role: str, sql: str) -> str:
    return run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            role,
            "-d",
            database,
            "-c",
            sql,
        ]
    )


def sql_text_array(values: list[str]) -> str:
    if any("'" in value for value in values):
        raise PlanError("unsafe value in SQL text array")
    return "ARRAY[" + ",".join(f"'{value}'" for value in values) + "]::text[]"


def production_preflight(plan: dict) -> dict:
    target = plan["target"]
    container = target["postgres_container"]
    database = target["database"]
    role = target["maintenance_role"]
    absent = plan["preinstall_absent_objects"]
    required = plan["preinstall_requirements"]["required_base_objects"]
    extensions = plan["preinstall_requirements"]["required_extensions"]
    absent_roles = plan["preinstall_requirements"]["required_absent_roles"]

    metadata_sql = f"""
      SELECT jsonb_build_object(
        'current_user', current_user,
        'current_database', current_database(),
        'postgres_version_num', current_setting('server_version_num')::integer,
        'database_size_bytes', pg_database_size(current_database()),
        'missing_base_objects', COALESCE((
          SELECT jsonb_agg(name ORDER BY name)
          FROM unnest({sql_text_array(required)}) AS item(name)
          WHERE to_regclass(name) IS NULL
        ), '[]'::jsonb),
        'existing_planned_objects', COALESCE((
          SELECT jsonb_agg(name ORDER BY name)
          FROM unnest({sql_text_array(absent)}) AS item(name)
          WHERE to_regclass(name) IS NOT NULL
        ), '[]'::jsonb),
        'missing_extensions', COALESCE((
          SELECT jsonb_agg(name ORDER BY name)
          FROM unnest({sql_text_array(extensions)}) AS item(name)
          WHERE NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname=name)
        ), '[]'::jsonb),
        'existing_forbidden_roles', COALESCE((
          SELECT jsonb_agg(name ORDER BY name)
          FROM unnest({sql_text_array(absent_roles)}) AS item(name)
          WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname=name)
        ), '[]'::jsonb),
        'application_role_safe', COALESCE((
          SELECT NOT rolsuper AND NOT rolbypassrls AND NOT rolcreatedb
                 AND NOT rolcreaterole
          FROM pg_roles
          WHERE rolname='{target["application_role"]}'
        ), false)
      )::text;
    """
    try:
        metadata = json.loads(psql(container, database, role, metadata_sql))
    except json.JSONDecodeError as exc:
        raise PlanError("production preflight returned invalid metadata JSON") from exc

    table_output = psql(
        container,
        database,
        role,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name;",
    )
    counts: dict[str, int] = {}
    for table in filter(None, table_output.splitlines()):
        if not SAFE_TABLE_RE.fullmatch(table):
            raise PlanError(f"unsafe table identifier returned by PostgreSQL: {table!r}")
        counts[table] = int(
            psql(container, database, role, f'SELECT count(*) FROM memory."{table}";')
        )

    tool_versions = {
        "pg_dump": run(["docker", "exec", container, "pg_dump", "--version"]),
        "pg_restore": run(["docker", "exec", container, "pg_restore", "--version"]),
        "psql": run(["docker", "exec", container, "psql", "--version"]),
    }
    host_tools = {
        name: shutil.which(name)
        for name in ("docker", "flock", "git", "python3", "realpath", "sha256sum")
    }
    snapshot_dir = Path(plan["backup_policy"]["destination_directory"])
    snapshot_exists = snapshot_dir.is_dir()
    snapshot_writable = snapshot_exists and os.access(snapshot_dir, os.W_OK | os.X_OK)
    free_bytes = shutil.disk_usage(snapshot_dir).free if snapshot_exists else 0
    minimum_free = (
        int(metadata["database_size_bytes"])
        * int(plan["preinstall_requirements"]["minimum_backup_free_space_multiplier"])
    )
    checks = {
        "maintenance_role_matches": metadata["current_user"] == role,
        "database_matches": metadata["current_database"] == database,
        "application_role_safe": metadata["application_role_safe"] is True,
        "postgres_major_matches": metadata["postgres_version_num"] // 10000
        == target["postgres_major"],
        "required_base_objects_present": metadata["missing_base_objects"] == [],
        "planned_objects_absent": metadata["existing_planned_objects"] == [],
        "restricted_writer_role_absent": metadata["existing_forbidden_roles"] == [],
        "required_extensions_present": metadata["missing_extensions"] == [],
        "snapshot_directory_exists": snapshot_exists,
        "snapshot_directory_writable": snapshot_writable,
        "snapshot_free_space_sufficient": free_bytes >= minimum_free,
        "backup_tools_available": len(tool_versions) == 3,
        "host_install_tools_available": all(host_tools.values()),
    }
    return {
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "target": {
            "server": target["server"],
            "container": container,
            "database": database,
            "maintenance_role": role,
        },
        "metadata": metadata,
        "baseline_counts": counts,
        "snapshot": {
            "directory": str(snapshot_dir),
            "free_bytes": free_bytes,
            "minimum_required_free_bytes": minimum_free,
        },
        "tool_versions": tool_versions,
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
        "--list-kind", choices=("migration", "rolled_back_test", "clone_rehearsal")
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
                (
                    entry
                    for entry in plan["source_files"]
                    if entry["kind"] == args.list_kind
                ),
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
        sys.stderr.write(f"memory_v1_projection_v5_install_plan: FAIL: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

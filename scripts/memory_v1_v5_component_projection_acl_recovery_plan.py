#!/usr/bin/env python3
"""Fail-closed verifier for the V5 component/projection ACL recovery."""

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


PLAN_CONTRACT = "memory_v1_v5_component_projection_acl_recovery_plan_v1"
AUTH_CONTRACT = (
    "memory_v1_v5_component_projection_acl_recovery_authorization_v1"
)
VERIFY_PLAN_CONTRACT = (
    "memory_v1_v5_component_projection_acl_verification_plan_v1"
)
VERIFY_AUTH_CONTRACT = (
    "memory_v1_v5_component_projection_acl_verification_authorization_v1"
)
ANCESTOR = "9174a41ed31cb7ce7c1f5766b499aa9c3c3bfaa3"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UNIT_RE = re.compile(r"^memory-v1-[a-z0-9-]+\.timer$")


class RecoveryError(RuntimeError):
    pass


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RecoveryError(f"JSON root must be an object: {path}")
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


def run(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args,
            text=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RecoveryError(
            f"command failed: {' '.join(args)}: {detail.strip()}"
        ) from exc
    return result.stdout.strip()


def git(repo_root: Path, *args: str) -> str:
    return run(["git", "-C", str(repo_root), *args])


def validate_plan(plan: dict, repo_root: Path) -> dict:
    errors: list[str] = []
    if plan.get("contract_version") != PLAN_CONTRACT:
        errors.append("unexpected plan contract_version")
    if plan.get("production_authorized") is not False:
        errors.append("committed plan must remain unauthorized")
    if plan.get("source") != {
        "branch": "memory_v1_v5_component_scope",
        "required_ancestor_commit": ANCESTOR,
    }:
        errors.append("source contract mismatch")
    if plan.get("target") != {
        "server": "seebx",
        "postgres_container": "brains-postgres-1",
        "database": "memory",
        "maintenance_role": "sage",
        "application_role": "brains_app",
        "postgres_major": 16,
        "private_schema": "memory",
    }:
        errors.append("target contract mismatch")
    if plan.get("authorization_policy") != {
        "required": True,
        "contract_version": AUTH_CONTRACT,
        "file_mode": "0600",
        "maximum_validity_minutes": 30,
        "bind_plan_sha256": True,
        "bind_exact_head_commit": True,
        "environment_gate": (
            "MEMORY_V1_COMPONENT_PROJECTION_ACL_RECOVERY=authorized"
        ),
    }:
        errors.append("authorization policy mismatch")
    if plan.get("backup_policy") != {
        "format": "postgres_custom",
        "destination_directory": "/home/ubuntu/brains/snapshots",
        "file_mode": "0600",
        "require_nonzero_bytes": True,
        "require_sha256": True,
        "require_restore_catalog": True,
        "must_complete_before_first_schema_write": True,
        "minimum_free_space_multiplier": 2,
    }:
        errors.append("backup policy mismatch")
    units = [
        "memory-v1-projection.timer",
        "memory-v1-governance.timer",
        "memory-v1-evidence-intake-dispatcher.timer",
        "memory-v1-v5-chat-capture.timer",
        "memory-v1-deferred-reconciliation-scan.timer",
    ]
    if plan.get("maintenance_quiescence") != {
        "temporary_only": True,
        "restore_exact_active_state_on_exit": True,
        "configuration_changes_allowed": False,
        "units": units,
    }:
        errors.append("maintenance quiescence mismatch")
    if any(not UNIT_RE.fullmatch(unit) for unit in units):
        errors.append("unsafe timer name")

    expected_preconditions = {
        "component_schema_installed": True,
        "component_tables_empty": True,
        "component_projection_rows": 0,
        "validator_execute_for_extraction_maintainer": False,
        "all_component_tables_force_rls": True,
        "component_tables_owned_by_sage": True,
        "brains_app_component_table_privileges": [],
        "required_roles_safe": True,
        "timers_enabled_and_active": True,
    }
    if plan.get("preconditions") != expected_preconditions:
        errors.append("preconditions mismatch")

    source_files = plan.get("source_files")
    expected_ordinals = {
        "migration": [1],
        "logical_rollback": [1],
        "rolled_back_test": [1, 2, 3, 4, 5],
    }
    ordinals: dict[str, list[int]] = {}
    verified_files: list[dict] = []
    seen: set[str] = set()
    root = repo_root.resolve()
    if not isinstance(source_files, list):
        errors.append("source_files must be a list")
        source_files = []
    for entry in source_files:
        if not isinstance(entry, dict) or set(entry) != {
            "path", "sha256", "kind", "ordinal"
        }:
            errors.append("source file entry has invalid keys")
            continue
        relpath = entry["path"]
        kind = entry["kind"]
        ordinal = entry["ordinal"]
        expected_sha = entry["sha256"]
        if not isinstance(relpath, str) or relpath in seen:
            errors.append(f"invalid or duplicate source path: {relpath!r}")
            continue
        seen.add(relpath)
        candidate = (repo_root / relpath).resolve()
        if root not in candidate.parents:
            errors.append(f"source path escapes repository: {relpath}")
            continue
        if kind not in expected_ordinals or not isinstance(ordinal, int):
            errors.append(f"invalid kind or ordinal: {relpath}")
            continue
        ordinals.setdefault(kind, []).append(ordinal)
        if not SHA256_RE.fullmatch(str(expected_sha)):
            errors.append(f"invalid source hash: {relpath}")
            continue
        if not candidate.is_file():
            errors.append(f"missing source file: {relpath}")
            continue
        actual_sha = file_sha256(candidate)
        if actual_sha != expected_sha:
            errors.append(f"source hash mismatch: {relpath}")
        verified_files.append({**entry, "actual_sha256": actual_sha})
    if ordinals != expected_ordinals:
        errors.append("source file kinds or ordinals mismatch")

    required_forbidden = {
        "apply_any_other_schema_or_privilege_change",
        "register_project_or_component_rows",
        "invoke_live_extraction_or_projection_apply",
        "write_qdrant_or_redis",
        "activate_retrieval_or_prompt_influence",
        "delete_redact_restore_or_modify_user_data",
        "deploy_component_aware_runtime_code",
    }
    forbidden = plan.get("forbidden_effects")
    if not isinstance(forbidden, list) or not required_forbidden.issubset(forbidden):
        errors.append("forbidden effect boundary incomplete")
    if plan.get("hard_stop") != (
        "before_component_registration_runtime_deployment_retrieval_activation_"
        "or_prompt_influence"
    ):
        errors.append("hard stop mismatch")

    try:
        head = git(repo_root, "rev-parse", "HEAD")
        git(repo_root, "merge-base", "--is-ancestor", ANCESTOR, head)
    except RecoveryError as exc:
        errors.append(str(exc))
        head = None
    if errors:
        raise RecoveryError("; ".join(errors))
    return {
        "contract_version": PLAN_CONTRACT,
        "plan_id": plan["plan_id"],
        "plan_sha256": canonical_sha256(plan),
        "head_commit": head,
        "verified_source_files": verified_files,
        "repository_valid": True,
    }


def validate_verification_plan(plan: dict, repo_root: Path) -> dict:
    errors: list[str] = []
    if plan.get("contract_version") != VERIFY_PLAN_CONTRACT:
        errors.append("unexpected verification plan contract_version")
    if plan.get("production_authorized") is not False:
        errors.append("committed verification plan must remain unauthorized")
    if plan.get("source") != {
        "branch": "memory_v1_v5_component_scope",
        "required_ancestor_commit": "ff9da469fc7cfdefd3ce968a6424fa3e026d34ce",
    }:
        errors.append("verification source contract mismatch")
    if plan.get("target") != {
        "server": "seebx",
        "postgres_container": "brains-postgres-1",
        "database": "memory",
        "maintenance_role": "sage",
        "application_role": "brains_app",
        "postgres_major": 16,
        "private_schema": "memory",
    }:
        errors.append("verification target contract mismatch")
    if plan.get("authorization_policy") != {
        "required": True,
        "contract_version": VERIFY_AUTH_CONTRACT,
        "file_mode": "0600",
        "maximum_validity_minutes": 30,
        "bind_plan_sha256": True,
        "bind_exact_head_commit": True,
        "environment_gate": (
            "MEMORY_V1_COMPONENT_PROJECTION_ACL_VERIFY=authorized"
        ),
    }:
        errors.append("verification authorization policy mismatch")
    if plan.get("backup_policy") != {
        "format": "postgres_custom",
        "destination_directory": "/home/ubuntu/brains/snapshots",
        "file_mode": "0600",
        "require_nonzero_bytes": True,
        "require_sha256": True,
        "require_restore_catalog": True,
        "must_complete_before_rollback_tests": True,
        "minimum_free_space_multiplier": 2,
    }:
        errors.append("verification backup policy mismatch")
    units = [
        "memory-v1-projection.timer",
        "memory-v1-governance.timer",
        "memory-v1-evidence-intake-dispatcher.timer",
        "memory-v1-v5-chat-capture.timer",
        "memory-v1-deferred-reconciliation-scan.timer",
    ]
    if plan.get("maintenance_quiescence") != {
        "temporary_only": True,
        "restore_exact_active_state_on_exit": True,
        "configuration_changes_allowed": False,
        "units": units,
    }:
        errors.append("verification maintenance quiescence mismatch")
    if plan.get("preconditions") != {
        "component_schema_installed": True,
        "component_tables_empty": True,
        "component_projection_rows": 0,
        "validator_execute_for_extraction_maintainer": True,
        "brains_app_validator_access": False,
        "all_component_tables_force_rls": True,
        "component_tables_owned_by_sage": True,
        "brains_app_component_table_privileges": [],
        "required_roles_safe": True,
        "timers_enabled_and_active": True,
    }:
        errors.append("verification preconditions mismatch")

    source_files = plan.get("source_files")
    ordinals: list[int] = []
    verified_files: list[dict] = []
    seen: set[str] = set()
    root = repo_root.resolve()
    if not isinstance(source_files, list):
        errors.append("verification source_files must be a list")
        source_files = []
    for entry in source_files:
        if not isinstance(entry, dict) or set(entry) != {
            "path", "sha256", "kind", "ordinal"
        }:
            errors.append("verification source entry has invalid keys")
            continue
        relpath = entry["path"]
        expected_sha = entry["sha256"]
        if (
            not isinstance(relpath, str)
            or relpath in seen
            or entry["kind"] != "rolled_back_test"
            or not isinstance(entry["ordinal"], int)
        ):
            errors.append(f"invalid verification source entry: {relpath!r}")
            continue
        seen.add(relpath)
        ordinals.append(entry["ordinal"])
        candidate = (repo_root / relpath).resolve()
        if root not in candidate.parents:
            errors.append(f"verification source escapes repository: {relpath}")
            continue
        if not SHA256_RE.fullmatch(str(expected_sha)):
            errors.append(f"invalid verification source hash: {relpath}")
            continue
        if not candidate.is_file():
            errors.append(f"missing verification source: {relpath}")
            continue
        actual_sha = file_sha256(candidate)
        if actual_sha != expected_sha:
            errors.append(f"verification source hash mismatch: {relpath}")
        verified_files.append({**entry, "actual_sha256": actual_sha})
    if ordinals != [1, 2, 3, 4, 5]:
        errors.append("verification test ordinals must be exactly 1 through 5")

    required_forbidden = {
        "apply_schema_or_privilege_changes",
        "register_project_or_component_rows",
        "invoke_live_extraction_or_projection_apply",
        "write_qdrant_or_redis",
        "activate_retrieval_or_prompt_influence",
        "delete_redact_restore_or_modify_user_data",
        "deploy_component_aware_runtime_code",
    }
    forbidden = plan.get("forbidden_effects")
    if not isinstance(forbidden, list) or not required_forbidden.issubset(forbidden):
        errors.append("verification forbidden boundary incomplete")
    if plan.get("hard_stop") != (
        "after_rollback_only_verification_before_component_registration_"
        "runtime_deployment_retrieval_activation_or_prompt_influence"
    ):
        errors.append("verification hard stop mismatch")

    try:
        head = git(repo_root, "rev-parse", "HEAD")
        git(
            repo_root, "merge-base", "--is-ancestor",
            "ff9da469fc7cfdefd3ce968a6424fa3e026d34ce", head,
        )
    except RecoveryError as exc:
        errors.append(str(exc))
        head = None
    if errors:
        raise RecoveryError("; ".join(errors))
    return {
        "contract_version": VERIFY_PLAN_CONTRACT,
        "plan_id": plan["plan_id"],
        "plan_sha256": canonical_sha256(plan),
        "head_commit": head,
        "verified_source_files": verified_files,
        "repository_valid": True,
    }


def parse_utc(value: str, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise RecoveryError(f"authorization {field} must be ISO-8601 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise RecoveryError(f"authorization {field} must use UTC")
    return parsed


def validate_authorization(
    path: Path, plan: dict, verification: dict, repo_root: Path
) -> dict:
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RecoveryError("authorization file mode must be 0600")
    auth = load_json(path)
    expected_keys = {
        "contract_version", "authorization_id", "authorized", "authorized_by",
        "authorized_at", "expires_at", "plan_sha256", "expected_head_commit",
        "target_server",
    }
    if set(auth) != expected_keys:
        raise RecoveryError("authorization keys do not exactly match contract")
    expected_contract = plan["authorization_policy"]["contract_version"]
    if auth["contract_version"] != expected_contract or auth["authorized"] is not True:
        raise RecoveryError("authorization contract or flag mismatch")
    try:
        uuid.UUID(auth["authorization_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise RecoveryError("authorization_id must be a UUID") from exc
    if auth["target_server"] != "seebx":
        raise RecoveryError("authorization target mismatch")
    if auth["plan_sha256"] != verification["plan_sha256"]:
        raise RecoveryError("authorization plan SHA mismatch")
    head = git(repo_root, "rev-parse", "HEAD")
    if auth["expected_head_commit"] != head:
        raise RecoveryError("authorization head mismatch")
    issued = parse_utc(auth["authorized_at"], "authorized_at")
    expires = parse_utc(auth["expires_at"], "expires_at")
    now = dt.datetime.now(dt.timezone.utc)
    maximum = dt.timedelta(
        minutes=plan["authorization_policy"]["maximum_validity_minutes"]
    )
    if expires <= issued or expires - issued > maximum:
        raise RecoveryError("authorization validity window is invalid")
    if now < issued - dt.timedelta(seconds=30) or now >= expires:
        raise RecoveryError("authorization is not currently valid")
    return {
        "authorization_id": auth["authorization_id"],
        "authorized_by": auth["authorized_by"],
        "expected_head_commit": head,
        "authorization_valid": True,
    }


def psql(plan: dict, sql: str) -> str:
    target = plan["target"]
    return run([
        "docker", "exec", target["postgres_container"], "psql", "-X", "-A",
        "-t", "-v", "ON_ERROR_STOP=1", "-U", target["maintenance_role"],
        "-d", target["database"], "-c", sql,
    ])


def service_state(unit: str, operation: str) -> str:
    result = subprocess.run(
        ["systemctl", operation, unit], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    return result.stdout.strip()


def production_preflight(plan: dict) -> dict:
    expected_grant = bool(
        plan["preconditions"]["validator_execute_for_extraction_maintainer"]
    )
    sql = """
      SELECT jsonb_build_object(
        'current_user',current_user,
        'current_database',current_database(),
        'postgres_version_num',current_setting('server_version_num')::integer,
        'database_size_bytes',pg_database_size(current_database()),
        'schema_installed',(
          to_regclass('memory.project_component_v5') IS NOT NULL
          AND to_regclass('memory.project_component_alias_v5') IS NOT NULL
          AND to_regclass('memory.project_component_registration_event_v5') IS NOT NULL
          AND to_regprocedure('memory.v5_project_scope_valid(jsonb)') IS NOT NULL
          AND to_regprocedure('memory.apply_owner_project_component_v5(uuid,uuid,text,text,uuid,text[],jsonb)') IS NOT NULL
          AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='memory' AND table_name='project_knowledge_head_v5' AND column_name='component_key')
          AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='memory' AND table_name='projection_project_payload' AND column_name='component_key')
        ),
        'component_table_rows',(
          (SELECT count(*) FROM memory.project_component_v5)
          +(SELECT count(*) FROM memory.project_component_alias_v5)
          +(SELECT count(*) FROM memory.project_component_registration_event_v5)
        ),
        'component_projection_rows',(
          (SELECT count(*) FROM memory.project_knowledge_head_v5 WHERE component_key IS NOT NULL)
          +(SELECT count(*) FROM memory.projection_project_payload WHERE component_key IS NOT NULL)
        ),
        'grant_present',has_function_privilege(
          'memory_v5_extraction_maintainer',
          'memory.v5_project_scope_valid(jsonb)','EXECUTE'),
        'brains_app_validator_access',has_function_privilege(
          'brains_app','memory.v5_project_scope_valid(jsonb)','EXECUTE'),
        'component_rls_owner_ok',(
          SELECT count(*)=3 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='memory'
            AND c.relname IN ('project_component_v5','project_component_alias_v5','project_component_registration_event_v5')
            AND c.relrowsecurity AND c.relforcerowsecurity
            AND pg_get_userbyid(c.relowner)='sage'
        ),
        'brains_app_table_access',(
          SELECT bool_or(
            has_table_privilege('brains_app','memory.'||name,'SELECT')
            OR has_table_privilege('brains_app','memory.'||name,'INSERT')
            OR has_table_privilege('brains_app','memory.'||name,'UPDATE')
            OR has_table_privilege('brains_app','memory.'||name,'DELETE')
            OR has_table_privilege('brains_app','memory.'||name,'TRUNCATE'))
          FROM unnest(ARRAY['project_component_v5','project_component_alias_v5','project_component_registration_event_v5']) item(name)
        ),
        'roles_safe',(
          SELECT count(*)=2 AND bool_and(
            NOT rolsuper AND NOT rolbypassrls AND NOT rolcreatedb AND NOT rolcreaterole
            AND ((rolname='brains_app' AND rolcanlogin)
              OR (rolname='memory_v5_extraction_maintainer' AND NOT rolcanlogin AND NOT rolinherit)))
          FROM pg_roles
          WHERE rolname IN ('brains_app','memory_v5_extraction_maintainer')
        )
      )::text;
    """
    try:
        metadata = json.loads(psql(plan, sql))
    except json.JSONDecodeError as exc:
        raise RecoveryError("production preflight returned invalid JSON") from exc
    units = plan["maintenance_quiescence"]["units"]
    timer_states = {
        unit: {
            "enabled": service_state(unit, "is-enabled"),
            "active": service_state(unit, "is-active"),
        }
        for unit in units
    }
    snapshot_dir = Path(plan["backup_policy"]["destination_directory"])
    snapshot_ready = snapshot_dir.is_dir() and os.access(
        snapshot_dir, os.W_OK | os.X_OK
    )
    free_bytes = shutil.disk_usage(snapshot_dir).free if snapshot_ready else 0
    minimum_free = (
        int(metadata["database_size_bytes"])
        * int(plan["backup_policy"]["minimum_free_space_multiplier"])
    )
    tools = [
        "curl", "docker", "flock", "git", "jq", "python3", "realpath",
        "sha256sum", "sudo", "systemctl",
    ]
    missing_tools = [tool for tool in tools if shutil.which(tool) is None]
    sudo_ready = subprocess.run(
        ["sudo", "-n", "true"], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0
    checks = {
        "maintenance_role_matches": metadata["current_user"] == "sage",
        "database_matches": metadata["current_database"] == "memory",
        "postgres_major_matches": metadata["postgres_version_num"] // 10000 == 16,
        "component_schema_installed": metadata["schema_installed"] is True,
        "component_tables_empty": metadata["component_table_rows"] == 0,
        "component_projection_rows_zero": metadata["component_projection_rows"] == 0,
        "validator_grant_state_matches": metadata["grant_present"] is expected_grant,
        "brains_app_validator_access_absent": metadata["brains_app_validator_access"] is False,
        "component_rls_and_owner_ok": metadata["component_rls_owner_ok"] is True,
        "brains_app_component_access_absent": metadata["brains_app_table_access"] is False,
        "roles_safe": metadata["roles_safe"] is True,
        "five_timers_enabled_and_active": all(
            state == {"enabled": "enabled", "active": "active"}
            for state in timer_states.values()
        ),
        "snapshot_directory_ready": snapshot_ready,
        "backup_space_ready": free_bytes >= minimum_free,
        "host_tools_ready": not missing_tools,
        "passwordless_sudo_ready": sudo_ready,
    }
    result = {
        "contract_version": plan["contract_version"],
        "checks": checks,
        "timer_states": timer_states,
        "database_size_bytes": metadata["database_size_bytes"],
        "snapshot_free_bytes": free_bytes,
        "missing_tools": missing_tools,
        "production_preflight_passed": all(checks.values()),
    }
    if not result["production_preflight_passed"]:
        failed = [name for name, passed in checks.items() if not passed]
        raise RecoveryError("production preflight failed: " + ", ".join(failed))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--production-preflight", action="store_true")
    parser.add_argument("--list-kind", choices=("migration", "logical_rollback", "rolled_back_test"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        plan = load_json(args.manifest)
        if plan.get("contract_version") == VERIFY_PLAN_CONTRACT:
            verification = validate_verification_plan(plan, args.repo_root)
        else:
            verification = validate_plan(plan, args.repo_root)
        result: dict = {"plan": verification}
        if args.authorization:
            result["authorization"] = validate_authorization(
                args.authorization, plan, verification, args.repo_root
            )
        if args.production_preflight:
            if not args.authorization:
                raise RecoveryError("production preflight requires authorization")
            result["production"] = production_preflight(plan)
        if args.list_kind:
            paths = [
                entry["path"] for entry in plan["source_files"]
                if entry["kind"] == args.list_kind
            ]
            print("\n".join(paths))
            return 0
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(encoded, encoding="utf-8")
        else:
            sys.stdout.write(encoded)
        return 0
    except (RecoveryError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

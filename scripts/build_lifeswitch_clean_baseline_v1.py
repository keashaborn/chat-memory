#!/usr/bin/env python3
from __future__ import annotations

"""Build a schema-only LifeSwitch clean baseline from an exact disposition."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "seebx-lifeswitch-clean-baseline-receipt-v1"
DISPOSITION_VERSION = "seebx-lifeswitch-database-object-disposition-v1"
CONTAINER = "lifeswitch-postgres-current"
DATABASE = "lifeswitch"
ADMIN_ROLE = "lifeswitch_bootstrap"
DOCKER = "/usr/bin/docker"
PG_RESTORE = "/usr/bin/pg_restore"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")

EXCLUDED_RELATIONS = {
    "catalog_dev.exercise_muscle_rule",
    "catalog_dev.food_alias",
    "catalog_dev.food_nutrient",
    "catalog_dev.food_portion",
    "catalog_dev.nutrient",
    "lifeswitch_snapshot.analysis_source_row",
    "lifeswitch_snapshot.personalization_source_row",
}
EXCLUDED_FUNCTIONS = {
    "catalog_dev.search_foods(q text, max_results integer, p_locale text)",
}
CANONICAL_MUSCLE_RELATIONS = {
    "catalog_dev.exercise_muscle",
    "catalog_dev.muscle",
    "catalog_dev.muscle_alias",
}

ROLE_SQL = """\
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lifeswitch_bootstrap') THEN
    EXECUTE 'CREATE ROLE lifeswitch_bootstrap NOLOGIN SUPERUSER INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lifeswitch_owner') THEN
    EXECUTE 'CREATE ROLE lifeswitch_owner NOLOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lifeswitch_app') THEN
    EXECUTE 'CREATE ROLE lifeswitch_app NOLOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lifeswitch_app_login') THEN
    EXECUTE 'CREATE ROLE lifeswitch_app_login NOLOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lifeswitch_chat_account_writer_v1') THEN
    EXECUTE 'CREATE ROLE lifeswitch_chat_account_writer_v1 NOLOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lifeswitch_chat_reader') THEN
    EXECUTE 'CREATE ROLE lifeswitch_chat_reader NOLOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lifeswitch_chat_login') THEN
    EXECUTE 'CREATE ROLE lifeswitch_chat_login NOLOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS';
  END IF;
END
$roles$;

GRANT lifeswitch_app TO lifeswitch_app_login;
GRANT lifeswitch_chat_account_writer_v1 TO lifeswitch_app_login;
GRANT lifeswitch_chat_reader TO lifeswitch_chat_login;
"""

INSTALL_SQL = """\
\\set ON_ERROR_STOP on
\\ir lifeswitch-clean-roles-v1.sql
SET ROLE lifeswitch_bootstrap;
\\ir lifeswitch-clean-schema-v1.sql
RESET ROLE;
"""


class BaselineContractError(RuntimeError):
    pass


class BaselineExecutionError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_disposition(path: Path, expected_sha256: str) -> dict[str, Any]:
    if SHA256.fullmatch(expected_sha256) is None:
        raise BaselineContractError("disposition_sha256_invalid")
    if path.is_symlink() or not path.is_file():
        raise BaselineContractError("disposition_not_regular")
    data = path.read_bytes()
    if sha256_bytes(data) != expected_sha256:
        raise BaselineContractError("disposition_sha256_mismatch")
    try:
        value = json.loads(data)
    except json.JSONDecodeError as error:
        raise BaselineContractError("disposition_json_invalid") from error
    if not isinstance(value, dict):
        raise BaselineContractError("disposition_shape_invalid")
    return value


def validate_disposition(manifest: dict[str, Any]) -> dict[str, list[str]]:
    if manifest.get("schema_version") != DISPOSITION_VERSION:
        raise BaselineContractError("disposition_version_invalid")
    if manifest.get("status") != "candidate_baseline_ready":
        raise BaselineContractError("disposition_status_invalid")
    if manifest.get("baseline_generation_allowed") is not True:
        raise BaselineContractError("baseline_generation_not_allowed")
    if manifest.get("deletion_authority") is not False:
        raise BaselineContractError("deletion_authority_invalid")
    if manifest.get("production_change_authority") is not False:
        raise BaselineContractError("production_change_authority_invalid")
    objects = manifest.get("objects")
    if not isinstance(objects, list) or len(objects) != manifest.get("object_count"):
        raise BaselineContractError("disposition_objects_invalid")

    excluded_relations: set[str] = set()
    excluded_functions: set[str] = set()
    canonical_retained: set[str] = set()
    seen: set[str] = set()
    for item in objects:
        if not isinstance(item, dict):
            raise BaselineContractError("disposition_object_invalid")
        identity = str(item.get("identity") or "")
        if not identity or identity in seen:
            raise BaselineContractError("disposition_identity_invalid")
        seen.add(identity)
        action = item.get("baseline_action")
        object_type = item.get("object_type")
        if action == "hold":
            raise BaselineContractError("disposition_hold_present")
        if action == "exclude":
            if object_type == "relation":
                excluded_relations.add(identity)
            elif object_type == "function":
                excluded_functions.add(identity)
            else:
                raise BaselineContractError("excluded_object_type_unsupported")
        if item.get("disposition") == "retained_canonical_product_data":
            canonical_retained.add(identity)

    if excluded_relations != EXCLUDED_RELATIONS:
        raise BaselineContractError("excluded_relations_changed")
    if excluded_functions != EXCLUDED_FUNCTIONS:
        raise BaselineContractError("excluded_functions_changed")
    if canonical_retained != CANONICAL_MUSCLE_RELATIONS:
        raise BaselineContractError("canonical_muscle_relations_changed")
    return {
        "excluded_functions": sorted(excluded_functions),
        "excluded_relations": sorted(excluded_relations),
        "retained_canonical_product_data": sorted(canonical_retained),
    }


def dump_command(plan: dict[str, list[str]]) -> list[str]:
    command = [
        DOCKER,
        "exec",
        CONTAINER,
        "pg_dump",
        "-U",
        ADMIN_ROLE,
        "-d",
        DATABASE,
        "--schema-only",
        "--format=custom",
        "--compress=6",
        "--no-password",
        "--exclude-schema=lifeswitch_snapshot",
    ]
    for identity in plan["excluded_relations"]:
        if identity.startswith("lifeswitch_snapshot."):
            continue
        command.append("--exclude-table=" + identity)
    return command


def run_dump(command: list[str], output: Path) -> None:
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.PIPE,
                check=False,
                timeout=300,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
            )
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, subprocess.SubprocessError) as error:
        output.unlink(missing_ok=True)
        raise BaselineExecutionError("schema_dump_execution_failed") from error
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        output.unlink(missing_ok=True)
        raise BaselineExecutionError("schema_dump_failed")


def run_text(command: list[str], *, label: str, timeout: int = 120) -> str:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BaselineExecutionError(f"{label}_execution_failed") from error
    if completed.returncode != 0:
        raise BaselineExecutionError(f"{label}_failed")
    return completed.stdout


def filter_restore_list(raw: str) -> tuple[str, int]:
    entries: list[str] = []
    removed = 0
    for line in raw.splitlines():
        clean = line.strip()
        if not clean or clean.startswith(";"):
            continue
        lowered = clean.lower()
        if " table data " in lowered:
            raise BaselineContractError("schema_archive_contains_table_data")
        if (
            " function catalog_dev search_foods(" in lowered
            or " acl catalog_dev function search_foods(" in lowered
        ):
            removed += 1
            continue
        entries.append(clean)
    if removed != 2:
        raise BaselineContractError("retired_function_toc_count_invalid")
    filtered = "\n".join(entries) + "\n"
    lowered = filtered.lower()
    for token in (
        "exercise_muscle_rule",
        "food_alias",
        "food_nutrient",
        "food_portion",
        "catalog_dev nutrient",
        "lifeswitch_snapshot",
        "search_foods(",
    ):
        if token in lowered:
            raise BaselineContractError("excluded_object_remains_in_toc")
    for token in (
        " table catalog_dev exercise_muscle ",
        " table catalog_dev muscle ",
        " table catalog_dev muscle_alias ",
    ):
        if token not in lowered:
            raise BaselineContractError("canonical_muscle_object_missing_from_toc")
    return filtered, removed


def run_pg_restore(archive: Path, restore_list: Path, output: Path) -> None:
    command = [
        PG_RESTORE,
        "--use-list=" + str(restore_list),
        "--file=" + str(output),
        str(archive),
    ]
    run_text(command, label="plain_schema_render", timeout=300)
    if not output.is_file() or output.stat().st_size == 0:
        raise BaselineExecutionError("plain_schema_missing")
    os.chmod(output, 0o600)


def validate_plain_schema(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    if re.search(r"(?m)^copy\s", lowered):
        raise BaselineContractError("plain_schema_forbidden_content")
    for forbidden in (
        "catalog_dev.exercise_muscle_rule",
        "catalog_dev.food_alias",
        "catalog_dev.food_nutrient",
        "catalog_dev.food_portion",
        "catalog_dev.nutrient",
        "lifeswitch_snapshot.",
        "catalog_dev.search_foods(",
    ):
        if forbidden in lowered:
            raise BaselineContractError("plain_schema_forbidden_content")
    for required in (
        "create table catalog_dev.exercise_muscle",
        "create table catalog_dev.muscle",
        "create table catalog_dev.muscle_alias",
    ):
        if required not in lowered:
            raise BaselineContractError("plain_schema_canonical_object_missing")


def write_exclusive(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def prepare_output(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise BaselineContractError("output_already_exists")
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise BaselineContractError("output_parent_invalid")
    path.mkdir(mode=0o700)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise BaselineContractError("output_mode_invalid")
    return path


def execute(
    *,
    disposition_path: Path,
    disposition_sha256: str,
    candidate_commit: str,
    output: Path,
) -> dict[str, Any]:
    if COMMIT.fullmatch(candidate_commit) is None:
        raise BaselineContractError("candidate_commit_invalid")
    manifest = read_disposition(disposition_path, disposition_sha256)
    plan = validate_disposition(manifest)
    output = prepare_output(output)
    archive = output / "lifeswitch-clean-schema-v1.pgcustom"
    restore_list = output / "lifeswitch-clean-restore-v1.list"
    schema_sql = output / "lifeswitch-clean-schema-v1.sql"
    roles_sql = output / "lifeswitch-clean-roles-v1.sql"
    install_sql = output / "lifeswitch-clean-install-v1.sql"
    receipt_path = output / "lifeswitch-clean-baseline-receipt-v1.json"

    if run_text(
        [DOCKER, "inspect", "--format", "{{.State.Running}}", CONTAINER],
        label="container_state",
    ).strip() != "true":
        raise BaselineExecutionError("container_not_running")
    run_dump(dump_command(plan), archive)
    raw_list = run_text([PG_RESTORE, "--list", str(archive)], label="archive_list")
    filtered_list, removed_count = filter_restore_list(raw_list)
    write_exclusive(restore_list, filtered_list.encode())
    run_pg_restore(archive, restore_list, schema_sql)
    validate_plain_schema(schema_sql)
    write_exclusive(roles_sql, ROLE_SQL.encode())
    write_exclusive(install_sql, INSTALL_SQL.encode())

    artifacts = {}
    for path in (archive, restore_list, schema_sql, roles_sql, install_sql):
        artifacts[path.name] = {
            "bytes": path.stat().st_size,
            "mode": oct(stat.S_IMODE(path.stat().st_mode)),
            "sha256": sha256_file(path),
        }
    receipt = {
        "artifacts": artifacts,
        "candidate_commit": candidate_commit,
        "completed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "deletion_authority": False,
        "disposition_sha256": disposition_sha256,
        "object_plan": plan,
        "production_change_authority": False,
        "production_database_writes": 0,
        "retired_function_toc_entries_removed": removed_count,
        "schema_only": True,
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
    }
    receipt_bytes = canonical_bytes(receipt) + b"\n"
    write_exclusive(receipt_path, receipt_bytes)
    return {
        "baseline_receipt": str(receipt_path),
        "baseline_receipt_sha256": sha256_bytes(receipt_bytes),
        "candidate_commit": candidate_commit,
        "schema_sql_sha256": artifacts[schema_sql.name]["sha256"],
        "status": "pass",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--disposition", required=True, type=Path)
    parser.add_argument("--disposition-sha256", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = execute(
            disposition_path=arguments.disposition,
            disposition_sha256=arguments.disposition_sha256,
            candidate_commit=arguments.candidate_commit,
            output=arguments.output,
        )
    except (BaselineContractError, BaselineExecutionError) as error:
        print(json.dumps({"error": str(error), "status": "failed"}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

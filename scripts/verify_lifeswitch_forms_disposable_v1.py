#!/usr/bin/env python3
from __future__ import annotations

"""Verify the legacy Forms migration against a disposable LifeSwitch clone.

The platform Forms source and the isolated LifeSwitch production database are
read only. All schema/data writes occur in a temporary database that is
destroyed after exact rollback verification. Invalid legacy rows are retained
only in a root-owned artifact on an AWS-proven encrypted filesystem.
"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid5


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = ROOT / "ops/migrations/20260822_lifeswitch_forms_disposable_v1/package.json"
FORWARD_SQL = ROOT / "ops/sql/20260819_lifeswitch_forms_v1.sql"
ROLLBACK_SQL = ROOT / "ops/sql/20260819_lifeswitch_forms_v1_rollback.sql"
OUTPUT_ROOT = Path("/var/backups/seebx-cleanup/lifeswitch-forms-migration-v1")
CONFIG_PATH = Path("/etc/verbalsage/lifeswitch-postgres.env")
SOURCE_CONFIG_PATH = Path("/opt/chat-memory/.env")
INSTANCE_ID = "i-04fcc2707e434e450"
AWS_ACCOUNT_ID = "339712834334"
AWS_REGION = "us-east-2"
SCHEMA_VERSION = "seebx-lifeswitch-forms-disposable-verification-v1"
ENCRYPTION_SCHEMA_VERSION = "seebx-ebs-encryption-evidence-v1"
PACKAGE_SCHEMA_VERSION = "seebx-lifeswitch-forms-disposable-package-v1"
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,39}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FormsVerificationContractError(RuntimeError):
    pass


class FormsVerificationExecutionError(RuntimeError):
    pass


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise FormsVerificationContractError(f"{name}_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RESTORE = _load_module(
    "lifeswitch_disposable_restore_v1",
    ROOT / "scripts/verify_lifeswitch_disposable_restore_v1.py",
)
MIGRATION = _load_module(
    "lifeswitch_forms_migration_v1",
    ROOT / "scripts/migrate_lifeswitch_forms_v1.py",
)
AUDIT = _load_module(
    "lifeswitch_database_consumer_audit_v1",
    ROOT / "scripts/database_consumer_audit_v1.py",
)


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


def validate_run_id(value: str) -> str:
    if RUN_ID.fullmatch(value) is None:
        raise FormsVerificationContractError("run_id_invalid")
    return value


def target_database(run_id: str) -> str:
    value = "lifeswitch_forms_verify_" + validate_run_id(run_id).replace("-", "_")
    if len(value) > 63 or re.fullmatch(r"[a-z][a-z0-9_]+", value) is None:
        raise FormsVerificationContractError("target_database_invalid")
    return value


def _require_private_regular_file(path: Path, label: str) -> os.stat_result:
    observed = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(observed.st_mode):
        raise FormsVerificationContractError(f"{label}_not_regular")
    if observed.st_uid != 0 or stat.S_IMODE(observed.st_mode) != 0o600:
        raise FormsVerificationContractError(f"{label}_permissions_invalid")
    return observed


def _parse_utc(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise FormsVerificationContractError(f"{label}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FormsVerificationContractError(f"{label}_invalid") from error
    if parsed.tzinfo is None:
        raise FormsVerificationContractError(f"{label}_invalid")
    return parsed.astimezone(UTC)


def runtime_filesystem(path: Path) -> dict[str, str]:
    output = RESTORE.run_text(
        [
            "/usr/bin/findmnt",
            "-n",
            "-P",
            "-o",
            "TARGET,SOURCE,FSTYPE",
            "--target",
            str(path),
        ],
        label="artifact_filesystem",
    )
    values: dict[str, str] = {}
    for match in re.finditer(r'(TARGET|SOURCE|FSTYPE)="([^"]*)"', output):
        values[match.group(1).lower()] = match.group(2)
    if set(values) != {"target", "source", "fstype"}:
        raise FormsVerificationExecutionError("artifact_filesystem_shape_invalid")
    if values["target"] != "/" or not values["source"].startswith("/dev/"):
        raise FormsVerificationContractError("artifact_filesystem_not_root_device")
    return values


def validate_encryption_evidence(
    path: Path,
    *,
    now: datetime | None = None,
    filesystem: dict[str, str] | None = None,
) -> dict[str, Any]:
    _require_private_regular_file(path, "encryption_evidence")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FormsVerificationContractError("encryption_evidence_json_invalid") from error
    if not isinstance(document, dict) or document.get("schema_version") != ENCRYPTION_SCHEMA_VERSION:
        raise FormsVerificationContractError("encryption_evidence_schema_invalid")
    if document.get("region") != AWS_REGION:
        raise FormsVerificationContractError("encryption_evidence_region_invalid")
    observed_at = _parse_utc(document.get("observed_at"), "encryption_evidence_time")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if observed_at > current + timedelta(minutes=5) or current - observed_at > timedelta(hours=24):
        raise FormsVerificationContractError("encryption_evidence_stale")

    caller = document.get("caller_identity")
    if not isinstance(caller, dict) or str(caller.get("Account")) != AWS_ACCOUNT_ID:
        raise FormsVerificationContractError("encryption_evidence_account_invalid")
    caller_arn = str(caller.get("Arn") or "")
    if not caller_arn.startswith("arn:aws:sts::") or caller_arn.endswith(":root"):
        raise FormsVerificationContractError("encryption_evidence_caller_invalid")

    describe_instances = document.get("describe_instances")
    reservations = (
        describe_instances.get("Reservations")
        if isinstance(describe_instances, dict)
        else None
    )
    if not isinstance(reservations, list):
        raise FormsVerificationContractError("encryption_evidence_instances_invalid")
    instances = [
        instance
        for reservation in reservations
        if isinstance(reservation, dict)
        for instance in reservation.get("Instances", [])
        if isinstance(instance, dict) and instance.get("InstanceId") == INSTANCE_ID
    ]
    if len(instances) != 1 or instances[0].get("State", {}).get("Name") != "running":
        raise FormsVerificationContractError("encryption_evidence_instance_invalid")
    instance = instances[0]
    root_device = instance.get("RootDeviceName")
    mappings = [
        mapping
        for mapping in instance.get("BlockDeviceMappings", [])
        if isinstance(mapping, dict) and mapping.get("DeviceName") == root_device
    ]
    if len(mappings) != 1:
        raise FormsVerificationContractError("encryption_evidence_root_mapping_invalid")
    volume_id = str(mappings[0].get("Ebs", {}).get("VolumeId") or "")
    if re.fullmatch(r"vol-[0-9a-f]+", volume_id) is None:
        raise FormsVerificationContractError("encryption_evidence_volume_id_invalid")

    describe_volumes = document.get("describe_volumes")
    volumes = describe_volumes.get("Volumes") if isinstance(describe_volumes, dict) else None
    matches = [
        volume
        for volume in (volumes or [])
        if isinstance(volume, dict) and volume.get("VolumeId") == volume_id
    ]
    if len(matches) != 1:
        raise FormsVerificationContractError("encryption_evidence_volume_missing")
    volume = matches[0]
    if volume.get("Encrypted") is not True or volume.get("State") != "in-use":
        raise FormsVerificationContractError("encryption_evidence_volume_unencrypted")
    attachments = [
        attachment
        for attachment in volume.get("Attachments", [])
        if isinstance(attachment, dict)
        and attachment.get("InstanceId") == INSTANCE_ID
        and attachment.get("State") == "attached"
    ]
    if len(attachments) != 1:
        raise FormsVerificationContractError("encryption_evidence_attachment_invalid")

    observed_filesystem = filesystem or runtime_filesystem(OUTPUT_ROOT.parent)
    return {
        "evidence_sha256": sha256_file(path),
        "observed_at": observed_at.replace(microsecond=0).isoformat(),
        "caller_arn_sha256": sha256_bytes(caller_arn.encode()),
        "instance_id": INSTANCE_ID,
        "root_device_name": root_device,
        "volume_id": volume_id,
        "volume_encrypted": True,
        "volume_state": "in-use",
        "filesystem": observed_filesystem,
    }


def validate_package() -> dict[str, Any]:
    try:
        package = json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FormsVerificationContractError("package_invalid") from error
    if package.get("schema_version") != PACKAGE_SCHEMA_VERSION:
        raise FormsVerificationContractError("package_schema_invalid")
    if package.get("status") != "candidate_not_applied":
        raise FormsVerificationContractError("package_status_invalid")
    paths: list[dict[str, Any]] = []
    for section in ("inputs", "tool_dependencies"):
        values = package.get(section)
        if not isinstance(values, dict):
            raise FormsVerificationContractError(f"package_{section}_invalid")
        paths.extend(values.values())
    paths.append(package.get("tool"))
    for item in paths:
        if not isinstance(item, dict):
            raise FormsVerificationContractError("package_file_invalid")
        relative = item.get("path")
        expected = item.get("sha256")
        if not isinstance(relative, str) or relative.startswith("/") or ".." in Path(relative).parts:
            raise FormsVerificationContractError("package_path_invalid")
        if not isinstance(expected, str) or SHA256.fullmatch(expected) is None:
            raise FormsVerificationContractError("package_hash_invalid")
        candidate = (ROOT / relative).resolve()
        if not candidate.is_relative_to(ROOT.resolve()) or not candidate.is_file():
            raise FormsVerificationContractError("package_file_missing")
        if sha256_file(candidate) != expected:
            raise FormsVerificationContractError("package_file_hash_mismatch")
    return package


def expected_plan(package: dict[str, Any], plan: Any) -> dict[str, Any]:
    expected = package.get("expected")
    summary = plan.summary()
    if not isinstance(expected, dict):
        raise FormsVerificationContractError("package_expected_invalid")
    if expected.get("source_counts") != summary.get("source_counts"):
        raise FormsVerificationContractError("forms_source_counts_changed")
    if expected.get("eligible_counts") != summary.get("eligible_counts"):
        raise FormsVerificationContractError("forms_eligible_counts_changed")
    if expected.get("quarantine_count") != summary.get("quarantine_count"):
        raise FormsVerificationContractError("forms_quarantine_count_changed")
    expected_hashes = expected.get("sha256")
    actual_hashes = {
        "source": plan.source_bundle_sha256,
        "valid": plan.valid_bundle_sha256,
        "quarantine": plan.quarantine_sha256,
    }
    if expected_hashes != actual_hashes:
        raise FormsVerificationContractError("forms_plan_hashes_changed")
    return summary


def read_lifeswitch_dsn(target: str) -> str:
    _require_private_regular_file(CONFIG_PATH, "lifeswitch_config")
    value = ""
    for raw_line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, candidate = line.split("=", 1)
        if name.strip() == "LIFESWITCH_POSTGRES_DSN":
            value = candidate.strip().strip('"').strip("'")
            break
    if not value:
        raise FormsVerificationContractError("lifeswitch_dsn_missing")
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise FormsVerificationContractError("lifeswitch_dsn_scheme_invalid")
    if parsed.username != RESTORE.APP_ROLE:
        raise FormsVerificationContractError("lifeswitch_dsn_role_invalid")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port != 55433:
        raise FormsVerificationContractError("lifeswitch_dsn_endpoint_invalid")
    if parsed.path != "/" + RESTORE.SOURCE_DATABASE or parsed.fragment:
        raise FormsVerificationContractError("lifeswitch_dsn_database_invalid")
    return urlunsplit((parsed.scheme, parsed.netloc, "/" + target, parsed.query, ""))


def read_platform_source_dsn() -> str:
    _require_private_regular_file(SOURCE_CONFIG_PATH, "platform_source_config")
    value = ""
    for raw_line in SOURCE_CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, candidate = line.split("=", 1)
        if name.strip() == "POSTGRES_DSN":
            value = candidate.strip().strip('"').strip("'")
            break
    if not value:
        raise FormsVerificationContractError("platform_source_dsn_missing")
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise FormsVerificationContractError("platform_source_dsn_scheme_invalid")
    if parsed.username != "brains_app":
        raise FormsVerificationContractError("platform_source_dsn_role_invalid")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port != 5432:
        raise FormsVerificationContractError("platform_source_dsn_endpoint_invalid")
    if parsed.path != "/memory" or parsed.fragment:
        raise FormsVerificationContractError("platform_source_dsn_database_invalid")
    return value


def prepare_output(run_id: str) -> Path:
    OUTPUT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_stat = OUTPUT_ROOT.lstat()
    if OUTPUT_ROOT.is_symlink() or not OUTPUT_ROOT.is_dir():
        raise FormsVerificationContractError("output_root_invalid")
    if root_stat.st_uid != 0 or stat.S_IMODE(root_stat.st_mode) != 0o700:
        raise FormsVerificationContractError("output_root_permissions_invalid")
    output = OUTPUT_ROOT / run_id
    output.mkdir(mode=0o700)
    observed = output.lstat()
    if output.is_symlink() or observed.st_uid != 0 or stat.S_IMODE(observed.st_mode) != 0o700:
        raise FormsVerificationContractError("output_directory_invalid")
    return output


def apply_sql_file(database: str, path: Path, label: str) -> None:
    command = RESTORE.docker_command(
        "psql",
        "-X",
        "--no-psqlrc",
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        RESTORE.ADMIN_ROLE,
        "-d",
        database,
        interactive=True,
    )
    try:
        with path.open("rb") as source:
            completed = subprocess.run(
                command,
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=180,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
            )
    except (OSError, subprocess.SubprocessError) as error:
        raise FormsVerificationExecutionError(f"{label}_execution_failed") from error
    if completed.returncode != 0:
        raise FormsVerificationExecutionError(f"{label}_failed")


async def verify_forms_rls(destination_dsn: str, plan: Any) -> dict[str, Any]:
    asyncpg = MIGRATION.asyncpg
    owners = MIGRATION._owners(plan)
    if not owners:
        raise FormsVerificationExecutionError("forms_owner_set_empty")
    conn = await asyncpg.connect(destination_dsn, command_timeout=60)
    read_checks = 0
    try:
        await MIGRATION._destination_preflight(conn)
        for owner in owners:
            async with conn.transaction(readonly=True):
                await conn.fetchval("select set_config('app.user_id', $1, true)", owner)
                for relation in ("form_template", "form_version", "form_entry"):
                    foreign = await conn.fetchval(
                        f"select count(*) from lifeswitch_forms.{relation} "
                        "where owner_user_id <> $1::uuid",
                        owner,
                    )
                    if int(foreign or 0) != 0:
                        raise FormsVerificationExecutionError("forms_cross_owner_read_visible")
                    read_checks += 1
    finally:
        await conn.close()

    actor = owners[0]
    other = owners[1] if len(owners) > 1 else "00000000-0000-4000-8000-000000000099"
    probe_id = str(uuid5(NAMESPACE_URL, f"{destination_dsn.rsplit('/', 1)[-1]}:forms-rls"))
    conn = await asyncpg.connect(destination_dsn, command_timeout=60)
    denied = False
    try:
        try:
            async with conn.transaction():
                await conn.fetchval("select set_config('app.user_id', $1, true)", actor)
                await conn.execute(
                    "insert into lifeswitch_forms.form_template "
                    "(form_template_id,owner_user_id,name,status) values ($1::uuid,$2::uuid,$3,$4)",
                    probe_id,
                    other,
                    "cross-owner denial probe",
                    "draft",
                )
        except Exception as error:
            if getattr(error, "sqlstate", None) != "42501":
                raise FormsVerificationExecutionError("forms_cross_owner_write_wrong_failure") from error
            denied = True
    finally:
        await conn.close()
    if not denied:
        raise FormsVerificationExecutionError("forms_cross_owner_write_succeeded")
    return {
        "owner_count": len(owners),
        "owner_set_sha256": sha256_bytes(canonical_bytes(owners)),
        "cross_owner_read_checks": read_checks,
        "cross_owner_write_denied": True,
    }


def service_health() -> dict[str, Any]:
    state = RESTORE.run_text(
        ["/usr/bin/systemctl", "is-active", "brains.service"],
        label="brains_service",
    )
    status = RESTORE.run_text(
        [
            "/usr/bin/curl",
            "-sS",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "http://127.0.0.1:8088/openapi.json",
        ],
        label="brains_openapi",
    )
    if state != "active" or status != "200":
        raise FormsVerificationExecutionError("service_health_invalid")
    return {"brains_service": "active", "openapi_http_status": 200}


def execute(
    run_id: str,
    candidate_commit: str,
    repository: Path,
    encryption_evidence_path: Path,
    approval_id: str,
) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise FormsVerificationContractError("root_required")
    run_id = validate_run_id(run_id)
    if COMMIT.fullmatch(candidate_commit) is None:
        raise FormsVerificationContractError("candidate_commit_invalid")
    if not approval_id.strip():
        raise FormsVerificationContractError("approval_id_required")
    repository = repository.resolve()
    if repository != ROOT.resolve():
        raise FormsVerificationContractError("repository_path_mismatch")
    try:
        AUDIT.verify_repository_state(repository, candidate_commit)
    except Exception as error:
        raise FormsVerificationContractError(str(error)) from error
    package = validate_package()
    encryption = validate_encryption_evidence(encryption_evidence_path.resolve())
    source_dsn = read_platform_source_dsn()
    health_before = service_health()
    plan, source_fingerprint = asyncio.run(MIGRATION._load_source_plan(source_dsn))
    plan_summary = expected_plan(package, plan)

    target = target_database(run_id)
    if RESTORE.database_exists(target):
        raise FormsVerificationContractError("target_database_already_exists")
    destination_dsn = read_lifeswitch_dsn(target)
    output = prepare_output(run_id)
    archive = output / "lifeswitch-before-forms.pgcustom"
    baseline_path = output / "lifeswitch-before-forms-manifest.json"
    overall_receipt_path = output / "verification-receipt.json"
    target_created = False
    phase = "lifeswitch_backup"
    try:
        RESTORE.run_dump(archive)
        baseline = RESTORE.collect_manifest(RESTORE.SOURCE_DATABASE)
        RESTORE.atomic_write(baseline_path, canonical_bytes(baseline) + b"\n")
        phase = "temporary_database_create"
        RESTORE.run_text(
            RESTORE.docker_command(
                "createdb",
                "-U",
                RESTORE.ADMIN_ROLE,
                "--maintenance-db",
                RESTORE.SOURCE_DATABASE,
                "--template",
                "template0",
                "--encoding",
                "UTF8",
                target,
            ),
            label="forms_createdb",
        )
        target_created = True
        phase = "temporary_database_restore"
        RESTORE.run_restore(target, archive)
        restored = RESTORE.collect_manifest(target)
        if restored != baseline:
            difference = RESTORE.manifest_difference(baseline, restored)
            RESTORE.atomic_write(
                output / "restore-manifest-difference.json",
                canonical_bytes(difference) + b"\n",
            )
            raise FormsVerificationExecutionError("forms_restore_manifest_mismatch")

        phase = "forms_forward_schema"
        apply_sql_file(target, FORWARD_SQL, "forms_forward_schema")
        artifact_output = output / "migration-artifact"
        phase = "quarantine_prepare"
        partial = MIGRATION._prepare_artifact(
            artifact_output,
            plan,
            source_fingerprint,
            approval_id.strip(),
            encryption["evidence_sha256"],
        )
        phase = "forms_data_migration"
        destination_fingerprint = asyncio.run(
            MIGRATION._apply_destination(destination_dsn, plan, source_fingerprint)
        )
        migration_receipt = MIGRATION._finish_artifact(
            partial,
            artifact_output,
            plan,
            source_fingerprint,
            destination_fingerprint,
            approval_id.strip(),
            encryption["evidence_sha256"],
        )
        phase = "owner_isolation_verification"
        forms_rls = asyncio.run(verify_forms_rls(destination_dsn, plan))
        retained_rls = RESTORE.verify_owner_rls(target)
        roles = RESTORE.role_manifest(target)
        configuration = RESTORE.read_nonsecret_config()

        phase = "forms_exact_rollback"
        apply_sql_file(target, ROLLBACK_SQL, "forms_rollback_schema")
        rolled_back = RESTORE.collect_manifest(target)
        if rolled_back != restored:
            difference = RESTORE.manifest_difference(restored, rolled_back)
            RESTORE.atomic_write(
                output / "rollback-manifest-difference.json",
                canonical_bytes(difference) + b"\n",
            )
            raise FormsVerificationExecutionError("forms_rollback_manifest_mismatch")
        phase = "temporary_database_cleanup"
        RESTORE.drop_database(target, label="forms_dropdb")
        target_created = False
        if RESTORE.database_exists(target):
            raise FormsVerificationExecutionError("forms_target_cleanup_unproved")

        phase = "production_unchanged_verification"
        production_lifeswitch_after = RESTORE.collect_manifest(RESTORE.SOURCE_DATABASE)
        if production_lifeswitch_after != baseline:
            raise FormsVerificationExecutionError("production_lifeswitch_database_changed")
        final_plan, final_source_fingerprint = asyncio.run(
            MIGRATION._load_source_plan(source_dsn)
        )
        final_summary = expected_plan(package, final_plan)
        if final_summary != plan_summary or final_source_fingerprint != source_fingerprint:
            raise FormsVerificationExecutionError("production_forms_source_changed")
        health_after = service_health()

        phase = "final_receipt"
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "status": "pass",
            "run_id": run_id,
            "completed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "candidate_commit": candidate_commit,
            "package_sha256": sha256_file(PACKAGE_PATH),
            "approval_id": approval_id.strip(),
            "encryption": encryption,
            "source_forms": {
                "database_fingerprint": source_fingerprint,
                "plan": plan_summary,
                "before_after_exact_match": True,
            },
            "lifeswitch_backup": {
                "relative_path": archive.name,
                "bytes": archive.stat().st_size,
                "sha256": sha256_file(archive),
                "mode": oct(stat.S_IMODE(archive.stat().st_mode)),
                "baseline_manifest_sha256": baseline["manifest_sha256"],
            },
            "disposable_migration": {
                "forward_sql_sha256": sha256_file(FORWARD_SQL),
                "migration_receipt_sha256": sha256_bytes(
                    canonical_bytes(migration_receipt) + b"\n"
                ),
                "forms_rls": forms_rls,
                "retained_owner_rls": retained_rls,
                "roles": roles,
                "configuration": configuration,
                "rollback_sql_sha256": sha256_file(ROLLBACK_SQL),
                "exact_rollback_manifest_match": True,
            },
            "cleanup": {"temporary_database_absent": True},
            "service_health": {"before": health_before, "after": health_after},
            "production_changes": {
                "platform_forms_source_writes": 0,
                "lifeswitch_database_writes": 0,
                "service_restarts": 0,
                "deployments": 0,
                "environment_changes": 0,
            },
        }
        receipt_bytes = canonical_bytes(receipt) + b"\n"
        RESTORE.atomic_write(overall_receipt_path, receipt_bytes)
        return {
            "status": "pass",
            "receipt": str(overall_receipt_path),
            "receipt_sha256": sha256_bytes(receipt_bytes),
            "backup_sha256": receipt["lifeswitch_backup"]["sha256"],
            "source_sha256": plan.source_bundle_sha256,
            "valid_sha256": plan.valid_bundle_sha256,
            "quarantine_sha256": plan.quarantine_sha256,
            "cleanup": "proved",
        }
    except Exception as error:
        failure = {
            "schema_version": "seebx-lifeswitch-forms-disposable-failure-v1",
            "status": "failed",
            "run_id": run_id,
            "candidate_commit": candidate_commit,
            "phase": phase,
            "error_type": type(error).__name__,
            "error_message_sha256": sha256_bytes(str(error).encode()),
        }
        try:
            RESTORE.atomic_write(
                output / "failure-receipt.json",
                canonical_bytes(failure) + b"\n",
            )
        except Exception:
            pass
        raise
    finally:
        if target_created:
            RESTORE.drop_database(target, label="forms_failure_cleanup_dropdb")
            if RESTORE.database_exists(target):
                raise FormsVerificationExecutionError("forms_failure_cleanup_unproved")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--encryption-evidence", type=Path, required=True)
    parser.add_argument("--approval-id", required=True)
    arguments = parser.parse_args(argv)
    try:
        result = execute(
            arguments.run_id,
            arguments.candidate_commit,
            arguments.repository,
            arguments.encryption_evidence,
            arguments.approval_id,
        )
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "error_message_sha256": sha256_bytes(str(error).encode()),
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

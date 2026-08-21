#!/usr/bin/env python3
from __future__ import annotations

"""Offline verifier for the legacy-memory retirement security package."""

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "seebx-legacy-memory-retirement-security-boundary-verification-v1"
PACKAGE_VERSION = "seebx-legacy-memory-retirement-security-boundary-package-v1"
ACCOUNT_ID = "339712834334"
REGION = "us-east-2"
SECRET_NAME = "lifeswitch/seebx/legacy-attestation-quarantine-v1"
SOURCE_ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/SeeBxSessionManagerRole"
RECOVERY_ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/LifeSwitchLegacyAttestationRecoveryV1"
SECRET_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:{SECRET_NAME}-*"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BoundaryError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def load_json(path: Path) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise BoundaryError(f"duplicate_json_key:{path.name}:{key}")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BoundaryError(f"json_invalid:{path.name}") from error


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(value: bool, reason: str) -> None:
    if not value:
        raise BoundaryError(reason)


def verify(package_path: Path) -> dict[str, Any]:
    package_path = package_path.resolve()
    root = package_path.parents[3]
    package = load_json(package_path)
    _require(package.get("schema_version") == PACKAGE_VERSION, "package_schema_version_invalid")
    _require(package.get("status") == "candidate_not_applied", "package_status_invalid")
    _require(package.get("execution") == {
        "aws_resources_created": False,
        "database_mutated": False,
        "deployment_authorized": False,
        "production_permissions_changed": False,
    }, "execution_boundary_invalid")
    _require(package.get("target") == {
        "account_id": ACCOUNT_ID,
        "database": "memory",
        "database_admin_role": "sage",
        "region": REGION,
        "server": "seebx",
        "source_instance_role_arn": SOURCE_ROLE_ARN,
    }, "target_invalid")
    _require(package.get("identities") == {
        "application_role": "brains_app",
        "inspection_role": "lifeswitch_retirement_auditor",
        "recovery_role_arn": RECOVERY_ROLE_ARN,
    }, "identity_split_invalid")
    _require(package.get("secret") == {
        "algorithm": "AES-256-GCM",
        "key_bytes": 32,
        "name": SECRET_NAME,
        "plaintext_in_repository": False,
        "version_id_required": True,
    }, "secret_contract_invalid")

    inputs = package.get("inputs")
    _require(isinstance(inputs, dict) and inputs, "package_inputs_invalid")
    for name, entry in inputs.items():
        _require(set(entry) == {"path", "sha256"}, f"input_shape_invalid:{name}")
        relative = entry["path"]
        digest = entry["sha256"]
        _require(isinstance(relative, str) and not relative.startswith("/") and ".." not in Path(relative).parts, f"input_path_invalid:{name}")
        _require(isinstance(digest, str) and SHA256.fullmatch(digest) is not None, f"input_sha256_invalid:{name}")
        path = root / relative
        _require(path.is_file() and not path.is_symlink(), f"input_missing:{name}")
        _require(file_sha256(path) == digest, f"input_hash_mismatch:{name}")

    trust = load_json(root / inputs["recovery_role_trust_policy"]["path"])
    _require(trust == {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "SeeBxInstanceRoleOnly",
            "Effect": "Allow",
            "Principal": {"AWS": SOURCE_ROLE_ARN},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"sts:ExternalId": "lifeswitch-legacy-attestation-recovery-v1"}},
        }],
    }, "trust_policy_invalid")

    source_identity = load_json(root / inputs["source_role_assume_policy"]["path"])
    _require(source_identity == {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AssumeExactLegacyAttestationRecoveryRole",
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Resource": RECOVERY_ROLE_ARN,
            "Condition": {"StringEquals": {"sts:ExternalId": "lifeswitch-legacy-attestation-recovery-v1"}},
        }],
    }, "source_role_policy_invalid")

    identity = load_json(root / inputs["recovery_role_identity_policy"]["path"])
    _require(identity == {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "ReadExactQuarantineKeyOnly",
            "Effect": "Allow",
            "Action": ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue", "secretsmanager:ListSecretVersionIds"],
            "Resource": SECRET_ARN,
        }],
    }, "identity_policy_invalid")

    resource = load_json(root / inputs["secret_resource_policy"]["path"])
    statements = resource.get("Statement") if isinstance(resource, dict) else None
    _require(isinstance(statements, list) and len(statements) == 2, "resource_policy_statement_count_invalid")
    _require(statements[0] == {
        "Sid": "DenyInsecureTransport",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "secretsmanager:*",
        "Resource": "*",
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
    }, "resource_policy_tls_deny_invalid")
    _require(statements[1] == {
        "Sid": "AllowBoundRecoveryRoleRead",
        "Effect": "Allow",
        "Principal": {"AWS": RECOVERY_ROLE_ARN},
        "Action": ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue", "secretsmanager:ListSecretVersionIds"],
        "Resource": "*",
    }, "resource_policy_reader_invalid")

    provision = (root / inputs["database_provision"]["path"]).read_text(encoding="utf-8")
    required_sql = (
        "CREATE ROLE lifeswitch_retirement_auditor LOGIN NOINHERIT",
        "ALTER ROLE lifeswitch_retirement_auditor SET default_transaction_read_only TO 'on'",
        "SECURITY DEFINER",
        "SET search_path = pg_catalog, pg_temp",
        "REVOKE ALL ON FUNCTION memory.legacy_retirement_evidence_v1() FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION memory.legacy_retirement_evidence_v1() TO lifeswitch_retirement_auditor",
        "unexpected executable security-definer function",
    )
    for marker in required_sql:
        _require(marker in provision, f"database_provision_marker_missing:{marker}")
    forbidden_sql = (
        "GRANT SELECT ON memory.assistant_transcript_attestation_v1",
        "GRANT SELECT ON public.chat_log",
        "GRANT SELECT ON memory_ingest_private",
        "GRANT INSERT",
        "GRANT UPDATE",
        "GRANT DELETE",
        "GRANT TRUNCATE",
    )
    for marker in forbidden_sql:
        _require(marker not in provision.upper() if marker.isupper() else marker not in provision, f"database_provision_forbidden:{marker}")
    _require("LIFESWITCH_RETIREMENT_AUDITOR_PASSWORD" in provision, "password_environment_gate_missing")
    _require("password" not in canonical_bytes(package).decode("ascii").lower(), "package_must_not_contain_password_field")

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "package_sha256": file_sha256(package_path),
        "input_set_sha256": hashlib.sha256(canonical_bytes({name: entry["sha256"] for name, entry in sorted(inputs.items())})).hexdigest(),
        "application_role": "brains_app",
        "inspection_role": "lifeswitch_retirement_auditor",
        "recovery_role_arn": RECOVERY_ROLE_ARN,
        "secret_name": SECRET_NAME,
        "production_changed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, default=Path(__file__).resolve().parents[1] / "ops/security/20260821_legacy_memory_retirement_security_boundary_v1/package.json")
    arguments = parser.parse_args(argv)
    try:
        result = verify(arguments.package)
    except BoundaryError as error:
        result = {"schema_version": SCHEMA_VERSION, "status": "fail", "reason": str(error), "production_changed": False}
        exit_code = 2
    except Exception:
        result = {"schema_version": SCHEMA_VERSION, "status": "error", "reason": "unexpected_verifier_error", "production_changed": False}
        exit_code = 3
    else:
        exit_code = 0
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

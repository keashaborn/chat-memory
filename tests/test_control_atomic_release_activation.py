from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.control_atomic_release_activation import (
    APPROVED_OPERATIONS,
    ActivationControlError,
    build_activation_plan,
    validate_authorization,
)


BACKEND = "1" * 40
FRONTEND = "2" * 40


def package() -> dict:
    return {
        "package_id": "release-test-1",
        "authority": {
            "production_activation_authorized": False,
            "authorization_id": None,
        },
        "quiescence": {"approved": False},
        "sources": {
            "backend": {"candidate_commit": BACKEND},
            "frontend": {"candidate_commit": FRONTEND},
        },
    }


def package_sha(value: dict) -> str:
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return hashlib.sha256(raw).hexdigest()


def authorization(value: dict) -> dict:
    return {
        "schema_version": "seebx-atomic-release-authorization-v1",
        "authorization_id": "auth-test-0001",
        "decision": "approved",
        "package_id": value["package_id"],
        "package_sha256": package_sha(value),
        "backend_commit": BACKEND,
        "frontend_commit": FRONTEND,
        "approved_operations": list(APPROVED_OPERATIONS),
        "quiescence_approved": True,
        "targets": {
            "backend": {
                "aws_account_id": "339712834334",
                "instance_id": "i-04fcc2707e434e450",
                "region": "us-east-2",
            },
            "frontend": {
                "aws_account_id": "017820690695",
                "instance_id": "i-0508bfc4d4df4a63d",
                "region": "us-east-2",
            },
        },
        "issued_at_utc": "2026-08-19T20:00:00Z",
        "expires_at_utc": "2026-08-19T22:00:00Z",
    }


def verification(value: dict) -> dict:
    return {
        "status": "pass",
        "package_integrity_ready": True,
        "production_activation_authorized": False,
        "package_sha256": package_sha(value),
        "backend_commit": BACKEND,
        "frontend_commit": FRONTEND,
        "runtime_archive_sha256": "3" * 64,
        "database_backup_sha256": "4" * 64,
        "migration_package_sha256": "5" * 64,
    }


class AtomicReleaseActivationControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.package = package()
        self.authorization = authorization(self.package)
        self.now = datetime(2026, 8, 19, 21, 0, tzinfo=timezone.utc)

    def validate(self) -> None:
        validate_authorization(
            self.authorization,
            package=self.package,
            package_sha256=package_sha(self.package),
            now=self.now,
        )

    def test_exact_authorization_builds_bound_ordered_plan(self) -> None:
        self.validate()
        plan = build_activation_plan(
            self.package,
            verification(self.package),
            self.authorization,
        )
        self.assertEqual(plan["status"], "authorized_not_executed")
        self.assertFalse(plan["production_mutated"])
        self.assertEqual(
            plan["bindings"]["authorization_expires_at_utc"],
            "2026-08-19T22:00:00Z",
        )
        self.assertEqual(
            plan["bindings"]["targets"],
            self.authorization["targets"],
        )
        self.assertEqual(plan["forward"][2]["action"], "stop_frontend_service")
        self.assertEqual(plan["forward"][3]["action"], "stop_backend_service")
        self.assertEqual(
            plan["rollback_on_any_failure"][4]["action"],
            "prove_zep_outbox_empty",
        )
        self.assertEqual(plan["database_rollback_gate"], "zep_outbox_empty")
        self.assertEqual(len(plan["plan_sha256"]), 64)

    def test_package_hash_mismatch_fails_closed(self) -> None:
        self.authorization["package_sha256"] = "9" * 64
        with self.assertRaisesRegex(ActivationControlError, "package_sha_mismatch"):
            self.validate()

    def test_candidate_commit_mismatch_fails_closed(self) -> None:
        self.authorization["backend_commit"] = "9" * 40
        with self.assertRaisesRegex(ActivationControlError, "backend_commit_mismatch"):
            self.validate()

    def test_operation_reordering_fails_closed(self) -> None:
        operations = self.authorization["approved_operations"]
        operations[0], operations[1] = operations[1], operations[0]
        with self.assertRaisesRegex(ActivationControlError, "operations_invalid"):
            self.validate()

    def test_missing_quiescence_fails_closed(self) -> None:
        self.authorization["quiescence_approved"] = False
        with self.assertRaisesRegex(ActivationControlError, "quiescence_missing"):
            self.validate()

    def test_expired_authorization_fails_closed(self) -> None:
        self.authorization["expires_at_utc"] = "2026-08-19T20:30:00Z"
        with self.assertRaisesRegex(ActivationControlError, "time_window_invalid"):
            self.validate()

    def test_invalid_target_instance_fails_closed(self) -> None:
        self.authorization["targets"]["backend"]["instance_id"] = "i-wrong"
        with self.assertRaisesRegex(ActivationControlError, "target_instance_invalid"):
            self.validate()

    def test_missing_target_fails_closed(self) -> None:
        self.authorization["targets"].pop("frontend")
        with self.assertRaisesRegex(ActivationControlError, "targets_invalid"):
            self.validate()

    def test_extra_authorization_field_fails_closed(self) -> None:
        self.authorization["comment"] = "not bound"
        with self.assertRaisesRegex(ActivationControlError, "fields_invalid"):
            self.validate()

    def test_release_package_cannot_self_authorize(self) -> None:
        changed = copy.deepcopy(self.package)
        changed["authority"] = {
            "production_activation_authorized": True,
            "authorization_id": "self",
        }
        with self.assertRaisesRegex(ActivationControlError, "authority_boundary_invalid"):
            build_activation_plan(changed, verification(changed), self.authorization)

    def test_failed_integrity_verification_cannot_build_plan(self) -> None:
        failed = verification(self.package)
        failed["status"] = "error"
        with self.assertRaisesRegex(ActivationControlError, "verification_not_ready"):
            build_activation_plan(self.package, failed, self.authorization)

    def test_direct_cli_import_path_fails_closed_without_authorization(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing-authorization.json"
            process = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/control_atomic_release_activation.py"),
                    "--package",
                    str(missing),
                    "--artifact-root",
                    temporary,
                    "--authorization",
                    str(missing),
                    "--schema",
                    str(missing),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(process.returncode, 2)
        result = json.loads(process.stdout)
        self.assertEqual(result["reason"], "authorization_unavailable")
        self.assertFalse(result["production_mutated"])


if __name__ == "__main__":
    unittest.main()

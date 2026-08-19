from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timezone
from typing import Any, Mapping

from scripts.control_atomic_release_activation import build_activation_plan
from scripts.execute_atomic_release_plan import (
    ReleaseExecutionError,
    ReleaseExecutionFailed,
    execute_authorized_plan,
)


NOW = datetime(2026, 8, 19, 21, 0, tzinfo=timezone.utc)


def plan() -> dict[str, Any]:
    package = {
        "package_id": "release-test-1",
        "authority": {
            "production_activation_authorized": False,
            "authorization_id": None,
        },
        "quiescence": {"approved": False},
    }
    verification = {
        "status": "pass",
        "package_integrity_ready": True,
        "production_activation_authorized": False,
        "package_sha256": "1" * 64,
        "backend_commit": "2" * 40,
        "frontend_commit": "3" * 40,
        "runtime_archive_sha256": "4" * 64,
        "database_backup_sha256": "5" * 64,
        "migration_package_sha256": "6" * 64,
    }
    authorization = {
        "authorization_id": "auth-test-0001",
        "issued_at_utc": "2026-08-19T20:00:00Z",
        "expires_at_utc": "2026-08-19T22:00:00Z",
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
    }
    return build_activation_plan(package, verification, authorization)


class FakeDriver:
    def __init__(self, failures: set[tuple[str, str]] | None = None):
        self.failures = failures or set()
        self.calls: list[tuple[str, str]] = []

    def execute(self, phase: str, action: Mapping[str, Any]) -> Mapping[str, Any]:
        key = (phase, str(action["action"]))
        self.calls.append(key)
        if key in self.failures:
            raise RuntimeError("synthetic_failure")
        evidence = hashlib.sha256((phase + ":" + str(action["action"])).encode()).hexdigest()
        return {"status": "pass", "evidence_sha256": evidence}


class AtomicReleaseExecutorTests(unittest.TestCase):
    def test_exact_plan_executes_all_forward_actions(self) -> None:
        driver = FakeDriver()
        receipt = execute_authorized_plan(
            plan(), driver, run_id="release-run-0001", now=NOW
        )
        self.assertEqual(receipt["status"], "activated")
        self.assertTrue(receipt["production_mutated"])
        self.assertEqual(len(receipt["forward_events"]), 16)
        self.assertEqual(receipt["rollback_events"], [])
        self.assertEqual(len(driver.calls), 16)

    def test_read_only_preflight_failure_never_rolls_back(self) -> None:
        driver = FakeDriver({("forward", "verify_live_baseline")})
        with self.assertRaises(ReleaseExecutionFailed) as caught:
            execute_authorized_plan(
                plan(), driver, run_id="release-run-0002", now=NOW
            )
        receipt = caught.exception.receipt
        self.assertEqual(receipt["status"], "preflight_failed")
        self.assertFalse(receipt["production_mutated"])
        self.assertEqual(driver.calls, [("forward", "verify_live_baseline")])

    def test_post_quiescence_failure_runs_complete_rollback(self) -> None:
        driver = FakeDriver({("forward", "apply_bound_forward_migrations")})
        with self.assertRaises(ReleaseExecutionFailed) as caught:
            execute_authorized_plan(
                plan(), driver, run_id="release-run-0003", now=NOW
            )
        receipt = caught.exception.receipt
        self.assertEqual(receipt["status"], "rolled_back")
        self.assertTrue(receipt["production_mutated"])
        rollback_calls = [call for call in driver.calls if call[0] == "rollback"]
        self.assertEqual(len(rollback_calls), 9)
        self.assertEqual(rollback_calls[4][1], "prove_zep_outbox_empty")
        self.assertEqual(rollback_calls[5][1], "apply_bound_database_rollback")

    def test_rollback_failure_is_reported_but_later_steps_continue(self) -> None:
        driver = FakeDriver(
            {
                ("forward", "install_bound_backend_runtime"),
                ("rollback", "restore_frontend_service_build_and_source"),
            }
        )
        with self.assertRaises(ReleaseExecutionFailed) as caught:
            execute_authorized_plan(
                plan(), driver, run_id="release-run-0004", now=NOW
            )
        receipt = caught.exception.receipt
        self.assertEqual(receipt["status"], "rollback_failed")
        self.assertEqual(len(receipt["rollback_errors"]), 1)
        self.assertEqual(len([call for call in driver.calls if call[0] == "rollback"]), 9)

    def test_expired_plan_is_rejected_before_driver(self) -> None:
        driver = FakeDriver()
        with self.assertRaisesRegex(ReleaseExecutionError, "authorization_expired"):
            execute_authorized_plan(
                plan(),
                driver,
                run_id="release-run-0005",
                now=datetime(2026, 8, 19, 22, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(driver.calls, [])

    def test_plan_hash_tampering_is_rejected_before_driver(self) -> None:
        changed = plan()
        changed["bindings"]["backend_commit"] = "9" * 40
        driver = FakeDriver()
        with self.assertRaisesRegex(ReleaseExecutionError, "plan_hash_mismatch"):
            execute_authorized_plan(
                changed, driver, run_id="release-run-0006", now=NOW
            )
        self.assertEqual(driver.calls, [])

    def test_invalid_bound_target_is_rejected_before_driver(self) -> None:
        changed = plan()
        changed["bindings"]["targets"]["backend"]["instance_id"] = "i-wrong"
        unsigned = dict(changed)
        unsigned.pop("plan_sha256")
        import json

        changed["plan_sha256"] = hashlib.sha256(
            (json.dumps(unsigned, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest()
        driver = FakeDriver()
        with self.assertRaisesRegex(
            ReleaseExecutionError,
            "plan_binding_targets_invalid",
        ):
            execute_authorized_plan(
                changed, driver, run_id="release-run-0008", now=NOW
            )
        self.assertEqual(driver.calls, [])

    def test_forward_reordering_is_rejected_even_with_rehashed_plan(self) -> None:
        changed = plan()
        changed["forward"][0], changed["forward"][1] = (
            changed["forward"][1],
            changed["forward"][0],
        )
        unsigned = dict(changed)
        unsigned.pop("plan_sha256")
        import json

        changed["plan_sha256"] = hashlib.sha256(
            (json.dumps(unsigned, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest()
        driver = FakeDriver()
        with self.assertRaisesRegex(ReleaseExecutionError, "forward_plan_invalid"):
            execute_authorized_plan(
                changed, driver, run_id="release-run-0007", now=NOW
            )
        self.assertEqual(driver.calls, [])


if __name__ == "__main__":
    unittest.main()

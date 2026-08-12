from __future__ import annotations

import copy
import gc
import inspect
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tools.governed_memory_install import disposable_proof_harness as harness
from tools.governed_memory_install.synthetic_backend import (
    FAULT_NONE,
    SyntheticBackend,
    SyntheticBackendError,
    SyntheticScenario,
)
from tools.governed_memory_validation import run_installation_synthetic_proof as runner


ROOT = Path(__file__).resolve().parents[2]
HASH_KEYS = {
    "scenario_matrix_sha256",
    "scenario_result_set_sha256",
    "journal_head_set_sha256",
    "state_result_set_sha256",
    "controller_plan_model_sha256",
    "controller_plan_document_sha256",
    "proof_contract_sha256",
    "receipt_schema_sha256",
    "proof_sources_sha256",
    "package_manifest_sha256",
    "receipt_sha256",
}


class DormantStoreInstallDisposableHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.receipt = runner.run_synthetic_proof()

    def test_exact_matrix_passes_with_content_free_aggregate_receipt(self) -> None:
        receipt = self.receipt
        self.assertEqual(
            receipt["schema_version"],
            "governed-memory-dormant-store-install-disposable-proof-receipt-v2",
        )
        self.assertEqual(receipt["proof_scope"], "synthetic_in_process_model_only")
        self.assertEqual(receipt["outcome"], "synthetic_matrix_passed")
        self.assertEqual(receipt["scenario_count"], 124)
        self.assertEqual(receipt["plan_step_count"], 19)
        self.assertEqual(receipt["compensable_step_count"], 14)
        self.assertEqual(
            receipt["scenario_family_counts"],
            harness.EXPECTED_FAMILY_COUNTS,
        )
        self.assertEqual(receipt["outcome_counts"], harness.EXPECTED_OUTCOME_COUNTS)
        self.assertEqual(receipt["operation_counts"]["interruption"], 85)
        self.assertEqual(receipt["operation_counts"]["ordinary_failure"], 66)
        for key in HASH_KEYS:
            self.assertRegex(receipt[key], r"^[0-9a-f]{64}$")
        self.assertEqual(
            receipt["package_manifest_sha256"],
            harness._sha256_file(harness.PACKAGE_MANIFEST_PATH),
        )
        unsigned = copy.deepcopy(receipt)
        receipt_sha256 = unsigned.pop("receipt_sha256")
        self.assertEqual(receipt_sha256, harness._sha256_projection(unsigned))

        encoded = json.dumps(receipt, sort_keys=True)
        for forbidden in (
            "/etc/",
            "/opt/",
            "/var/",
            "POSTGRES_PASSWORD",
            "QDRANT__SERVICE__API_KEY",
            "pilot.env",
            "authorization_id",
            "nonce",
            "chat_attachments",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_receipt_is_deterministic(self) -> None:
        self.assertEqual(runner.run_synthetic_proof(), self.receipt)

    def test_claims_remain_strictly_synthetic_and_non_authoritative(self) -> None:
        claims = self.receipt["claims"]
        self.assertTrue(claims["synthetic_atomic_step_plan_model_exercised"])
        self.assertTrue(claims["single_interruption_resume_model_exercised"])
        self.assertTrue(
            claims["same_attempt_compensation_sequence_model_exercised"]
        )
        for key in (
            "live_execution_proven",
            "live_installation_proven",
            "live_rollback_proven",
            "durable_process_crash_recovery_proven",
            "composite_crash_recovery_proven",
            "docker_compatibility_proven",
            "systemd_compatibility_proven",
            "external_store_readiness_proven",
            "activation_proven",
            "secrets_accessed",
            "images_staged_or_pulled",
            "full_pre_attempt_state_restoration_proven",
            "empty_rollback_proven",
            "kernel_confinement_proven",
            "hostile_same_process_replacement_resisted",
        ):
            self.assertIs(claims[key], False)
        self.assertEqual(
            self.receipt["guard"],
            {
                "instrumentation": (
                    "python_audit_hook_best_effort_not_kernel_confinement"
                ),
                "audit_hook_configured": True,
                "blocked_effect_count": 0,
                "environment_access_count": 0,
            },
        )

    def test_exact_sealed_backend_is_internal_and_not_injectable(self) -> None:
        scenario = SyntheticScenario(
            scenario_id="TEST_HAPPY",
            family=harness.FAMILY_HAPPY_PATH,
            fault_point=FAULT_NONE,
            target_step_id=None,
        )
        with self.assertRaisesRegex(
            SyntheticBackendError,
            "construction_not_authorized",
        ):
            SyntheticBackend(scenario)
        parameters = inspect.signature(harness.run_disposable_proof).parameters
        self.assertEqual(tuple(parameters), ("disposable_root",))
        self.assertNotIn("backend", parameters)
        self.assertNotIn("executor", parameters)
        self.assertNotIn("live", parameters)

    def test_scenario_matrix_has_exact_full_step_and_compensation_coverage(self) -> None:
        scenarios = harness._scenario_matrix()
        self.assertEqual(len(scenarios), 124)
        family_counts = harness._verify_scenario_matrix(scenarios)
        self.assertEqual(family_counts, harness.EXPECTED_FAMILY_COUNTS)
        self.assertEqual(
            max(
                sum(
                    value is not None
                    for value in (
                        scenario.target_step_id,
                        scenario.trigger_failure_step_id,
                    )
                )
                for scenario in scenarios
            ),
            2,
        )
        self.assertEqual(
            sum(
                scenario.trigger_failure_step_id is not None
                for scenario in scenarios
            ),
            28,
        )
        self.assertEqual(
            sum(
                1
                for scenario in scenarios
                if scenario.family
                in {
                    harness.FAMILY_INTERRUPT_BEFORE_COMPENSATION,
                    harness.FAMILY_INTERRUPT_AFTER_COMPENSATION,
                }
            ),
            28,
        )

    def test_contract_and_schema_pin_false_live_claims_and_exact_counts(self) -> None:
        contract = json.loads(harness.CONTRACT_PATH.read_text(encoding="ascii"))
        schema = json.loads(
            harness.RECEIPT_SCHEMA_PATH.read_text(encoding="ascii")
        )
        self.assertEqual(contract["scenario_matrix"]["scenario_count"], 124)
        self.assertEqual(
            contract["scenario_matrix"]["family_counts"],
            harness.EXPECTED_FAMILY_COUNTS,
        )
        self.assertFalse(contract["execution_boundary"]["live_mode_exists"])
        self.assertFalse(contract["execution_boundary"]["backend_injection_allowed"])
        self.assertFalse(contract["execution_boundary"]["runner_accepts_arguments"])
        for key, value in contract["claims"].items():
            if key.endswith("_proven") and not key.startswith("synthetic_"):
                self.assertIs(value, False, key)
        self.assertEqual(
            schema["properties"]["scenario_count"]["const"],
            124,
        )
        self.assertEqual(
            schema["properties"]["claims"]["properties"]
            ["composite_crash_recovery_proven"]["const"],
            False,
        )

    def test_runner_import_guard_precedes_local_import_and_has_no_live_mode(self) -> None:
        self.assertTrue(runner.IMPORT_PHASE_GUARD_COMPLETED)
        with self.assertRaisesRegex(
            runner.RunnerRefusal,
            "accepts_no_arguments",
        ):
            runner.main(["--live"])
        source = runner.RUNNER_SOURCE_PATH.read_text(encoding="utf-8") if hasattr(runner, "RUNNER_SOURCE_PATH") else (
            ROOT
            / "tools/governed_memory_validation/run_installation_synthetic_proof.py"
        ).read_text(encoding="utf-8")
        hook_position = source.index("sys.addaudithook(_import_audit_hook)")
        local_import_position = source.index(
            "from tools.governed_memory_install.disposable_proof_harness"
        )
        self.assertLess(hook_position, local_import_position)
        self.assertNotIn("--live", source)
        self.assertNotIn("argparse", source)

    def test_runtime_fence_refuses_environment_and_external_writes(self) -> None:
        self.assertFalse(harness._resolved_allowed(3, write=False))
        self.assertFalse(harness._resolved_allowed("execution.lock", write=True))
        with tempfile.TemporaryDirectory(
            prefix="dormant_store_install-disposable-proof-"
        ) as temporary:
            root = Path(temporary).resolve(strict=True)
            os.chmod(root, 0o700)
            with self.assertRaisesRegex(
                harness.DisposableProofAuditViolation,
                "environment_access_forbidden",
            ):
                with harness._runtime_fence(root):
                    unused = os.environ["PATH"]
        with tempfile.TemporaryDirectory(
            prefix="dormant_store_install-disposable-proof-"
        ) as temporary:
            root = Path(temporary).resolve(strict=True)
            os.chmod(root, 0o700)
            escape = root.parent / (root.name + "-escape")
            self.assertFalse(escape.exists())
            with self.assertRaisesRegex(
                harness.DisposableProofAuditViolation,
                "filesystem_write_forbidden",
            ):
                with harness._runtime_fence(root):
                    escape.write_bytes(b"forbidden")
            self.assertFalse(escape.exists())

    def test_fence_setup_failures_restore_process_state(self) -> None:
        garbage_collection_was_enabled = gc.isenabled()
        original_environ = os.environ
        original_environb = getattr(os, "environb", None)
        missing = (
            Path(tempfile.gettempdir())
            / "dormant_store_install-disposable-proof-nonexistent-fence-root"
        )
        self.assertFalse(missing.exists())
        with self.assertRaisesRegex(
            harness.DisposableProofError,
            "disposable_root_invalid",
        ):
            harness.run_disposable_proof(disposable_root=missing)
        self.assertFalse(harness._RUNTIME_AUDIT.active)
        self.assertIsNone(harness._RUNTIME_AUDIT.repository_root)
        self.assertIsNone(harness._RUNTIME_AUDIT.disposable_root)
        self.assertIs(os.environ, original_environ)
        self.assertIs(getattr(os, "environb", None), original_environb)
        self.assertEqual(gc.isenabled(), garbage_collection_was_enabled)

        with tempfile.TemporaryDirectory(
            prefix="dormant_store_install-disposable-proof-"
        ) as temporary:
            root = Path(temporary).resolve(strict=True)
            os.chmod(root, 0o700)
            with mock.patch.object(
                harness,
                "REPOSITORY_ROOT",
                root / "missing-repository-root",
            ):
                with self.assertRaisesRegex(
                    harness.DisposableProofError,
                    "proof_fence_setup_invalid",
                ):
                    with harness._runtime_fence(root):
                        self.fail("unreachable")
        self.assertFalse(harness._RUNTIME_AUDIT.active)
        self.assertIsNone(harness._RUNTIME_AUDIT.repository_root)
        self.assertIsNone(harness._RUNTIME_AUDIT.disposable_root)
        self.assertIs(os.environ, original_environ)
        self.assertIs(getattr(os, "environb", None), original_environb)
        self.assertEqual(gc.isenabled(), garbage_collection_was_enabled)

    def test_sources_do_not_import_retired_versioned_or_live_execution_stack(
        self,
    ) -> None:
        source_paths = (
            ROOT / "tools/governed_memory_install/synthetic_backend.py",
            ROOT / "tools/governed_memory_install/disposable_proof_harness.py",
            ROOT
            / "tools/governed_memory_validation/run_installation_synthetic_proof.py",
        )
        joined = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)
        for forbidden in (
            "authority_v2",
            "controller_v2",
            "durable_journal_v2",
            "execution_capability_v2",
            "package_v3",
            "synthetic_backend_v2",
            "run_disposable_installation_controller",
            "import subprocess",
            "import socket",
            "import requests",
            "import urllib",
            "import docker",
        ):
            self.assertNotIn(forbidden, joined)

    def test_disposable_root_must_be_exact_empty_owned_mode_0700(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="dormant_store_install-disposable-proof-"
        ) as temporary:
            root = Path(temporary).resolve(strict=True)
            os.chmod(root, 0o755)
            with self.assertRaisesRegex(
                harness.DisposableProofError,
                "disposable_root_invalid",
            ):
                harness.run_disposable_proof(disposable_root=root)
            os.chmod(root, 0o700)
            marker = root / "preexisting"
            marker.write_bytes(b"x")
            with self.assertRaisesRegex(
                harness.DisposableProofError,
                "disposable_root_invalid",
            ):
                harness.run_disposable_proof(disposable_root=root)


if __name__ == "__main__":
    unittest.main()

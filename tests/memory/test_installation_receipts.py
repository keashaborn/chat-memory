from __future__ import annotations

import copy
import unittest

from tools.governed_memory_install.receipts import (
    ReceiptError,
    build_empty_rollback_receipt,
    build_install_receipt,
    receipt_sha256,
    verify_empty_rollback_receipt,
    verify_install_receipt,
)
from tools.governed_memory_install.rollback import ROLLBACK_RESOURCE_KEYS


H64 = "a" * 64
C40 = "b" * 40


class InstallationReceiptTests(unittest.TestCase):
    def install_receipt(self) -> dict[str, object]:
        runtime_receipt_sha256 = "e" * 64
        runtime_root = (
            "/opt/governed-memory-controller/runtimes/"
            + runtime_receipt_sha256
        )
        release_root = (
            "/opt/governed-memory-controller/releases/" + "d" * 64
        )
        return build_install_receipt(
            execution_id=H64,
            attempt_id="install-" + C40,
            candidate_git_commit=C40,
            candidate_git_tree="c" * 40,
            package_manifest_sha256="d" * 64,
            controller_runtime_receipt_sha256=runtime_receipt_sha256,
            controller_runtime_root=runtime_root,
            controller_runtime_tree_sha256="0" * 64,
            controller_release_root=release_root,
            controller_release_tree_sha256="6" * 64,
            controller_release_package_manifest_path=(
                release_root
                + "/ops/governed_memory/installation/current/package_manifest.json"
            ),
            controller_runtime_interpreter_path=runtime_root + "/bin/python",
            controller_runtime_interpreter_sha256="7" * 64,
            controller_runtime_inventory_path=(
                runtime_root + "/controller-distributions.json"
            ),
            controller_runtime_inventory_sha256="8" * 64,
            controller_requirements_lock_sha256="9" * 64,
            supervisor_launcher_path=(
                release_root
                + "/tools/governed_memory_install/store_supervisor_launcher.py"
            ),
            supervisor_launcher_sha256="a" * 64,
            authorization_sha256="f" * 64,
            plan_sha256="1" * 64,
            journal_head_sha256="2" * 64,
            journal_sequence=41,
            resource_ledger_head_sha256="3" * 64,
            resource_ledger_sequence=12,
            image_identity_set_sha256="4" * 64,
            postflight_receipt_sha256="5" * 64,
            terminal_store_readiness_sha256="6" * 64,
        )

    def rollback_receipt(self) -> dict[str, object]:
        runtime_receipt_sha256 = "e" * 64
        runtime_root = (
            "/opt/governed-memory-controller/runtimes/"
            + runtime_receipt_sha256
        )
        release_root = (
            "/opt/governed-memory-controller/releases/" + "d" * 64
        )
        return build_empty_rollback_receipt(
            execution_id="6" * 64,
            attempt_id="rollback-" + C40,
            candidate_git_commit=C40,
            candidate_git_tree="c" * 40,
            package_manifest_sha256="d" * 64,
            controller_runtime_receipt_sha256=runtime_receipt_sha256,
            controller_runtime_root=runtime_root,
            controller_runtime_tree_sha256="0" * 64,
            controller_release_root=release_root,
            controller_release_tree_sha256="6" * 64,
            controller_release_package_manifest_path=(
                release_root
                + "/ops/governed_memory/installation/current/package_manifest.json"
            ),
            controller_runtime_interpreter_path=runtime_root + "/bin/python",
            controller_runtime_interpreter_sha256="7" * 64,
            controller_runtime_inventory_path=(
                runtime_root + "/controller-distributions.json"
            ),
            controller_runtime_inventory_sha256="8" * 64,
            controller_requirements_lock_sha256="9" * 64,
            supervisor_launcher_path=(
                release_root
                + "/tools/governed_memory_install/store_supervisor_launcher.py"
            ),
            supervisor_launcher_sha256="a" * 64,
            installation_receipt_sha256="7" * 64,
            authorization_sha256="8" * 64,
            plan_sha256="9" * 64,
            journal_head_sha256="a" * 64,
            journal_sequence=31,
            resource_ledger_head_sha256="b" * 64,
            resource_ledger_sequence=18,
            eligibility_receipt_sha256="c" * 64,
            retained_audit_set_sha256="d" * 64,
            exact_targets_absent_count=len(ROLLBACK_RESOURCE_KEYS),
        )

    def test_install_receipt_is_closed_content_free_and_inactive(self) -> None:
        receipt = self.install_receipt()
        self.assertEqual(receipt["receipt_sha256"], receipt_sha256(receipt))
        self.assertFalse(receipt["application_services_installed"])
        self.assertFalse(receipt["activation_performed"])
        self.assertFalse(receipt["production_data_read"])
        self.assertEqual(receipt["provider_calls"], 0)
        self.assertEqual(verify_install_receipt(receipt), receipt)
        rendered = repr(receipt).lower()
        for forbidden in ("password", "secret_value", "api_key", "message"):
            self.assertNotIn(forbidden, rendered)

    def test_empty_rollback_receipt_retains_audit_and_removes_only_stores(self) -> None:
        receipt = self.rollback_receipt()
        self.assertTrue(receipt["exact_resources_absent"])
        self.assertFalse(receipt["stores_installed"])
        self.assertFalse(receipt["stores_supervisor_installed"])
        self.assertEqual(receipt["receipt_sha256"], receipt_sha256(receipt))
        self.assertEqual(verify_empty_rollback_receipt(receipt), receipt)

    def test_receipts_reject_extra_tamper_and_forbidden_effects(self) -> None:
        install = self.install_receipt()
        variants = []
        extra = copy.deepcopy(install)
        extra["unexpected"] = True
        variants.append(extra)
        production = copy.deepcopy(install)
        production["production_data_read"] = True
        production["receipt_sha256"] = receipt_sha256(production)
        variants.append(production)
        provider = copy.deepcopy(install)
        provider["provider_calls"] = 1
        provider["receipt_sha256"] = receipt_sha256(provider)
        variants.append(provider)
        digest = copy.deepcopy(install)
        digest["journal_sequence"] = 42
        variants.append(digest)
        runtime_path = copy.deepcopy(install)
        runtime_path["controller_runtime_interpreter_path"] = "/usr/bin/python3"
        runtime_path["receipt_sha256"] = receipt_sha256(runtime_path)
        variants.append(runtime_path)
        launcher_path = copy.deepcopy(install)
        launcher_path["supervisor_launcher_path"] = (
            "/tmp/store_supervisor_launcher.py"
        )
        launcher_path["receipt_sha256"] = receipt_sha256(launcher_path)
        variants.append(launcher_path)
        release_tree = copy.deepcopy(install)
        release_tree["controller_release_tree_sha256"] = "not-a-hash"
        release_tree["receipt_sha256"] = receipt_sha256(release_tree)
        variants.append(release_tree)
        release_manifest = copy.deepcopy(install)
        release_manifest["controller_release_package_manifest_path"] = (
            "/tmp/package_manifest.json"
        )
        release_manifest["receipt_sha256"] = receipt_sha256(release_manifest)
        variants.append(release_manifest)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ReceiptError):
                verify_install_receipt(variant)

    def test_rollback_receipt_rejects_nonempty_or_zero_removal(self) -> None:
        for key, value in (
            ("exact_resources_absent", False),
            ("stores_installed", True),
            ("exact_targets_absent_count", 0),
        ):
            receipt = self.rollback_receipt()
            receipt[key] = value
            receipt["receipt_sha256"] = receipt_sha256(receipt)
            with self.subTest(key=key), self.assertRaises(ReceiptError):
                verify_empty_rollback_receipt(receipt)

    def test_receipt_attempt_prefixes_are_operation_specific(self) -> None:
        install = self.install_receipt()
        install["attempt_id"] = "rollback-" + C40
        install["receipt_sha256"] = receipt_sha256(install)
        with self.assertRaises(ReceiptError):
            verify_install_receipt(install)

        rollback = self.rollback_receipt()
        rollback["attempt_id"] = "install-" + C40
        rollback["receipt_sha256"] = receipt_sha256(rollback)
        with self.assertRaises(ReceiptError):
            verify_empty_rollback_receipt(rollback)

    def test_zero_counters_reject_bool_and_rollback_count_is_exact(self) -> None:
        for verifier, baseline in (
            (verify_install_receipt, self.install_receipt()),
            (verify_empty_rollback_receipt, self.rollback_receipt()),
        ):
            for key in (
                "source_postgres_read_count",
                "source_postgres_write_count",
                "provider_calls",
            ):
                receipt = copy.deepcopy(baseline)
                receipt[key] = False
                receipt["receipt_sha256"] = receipt_sha256(receipt)
                with self.subTest(verifier=verifier.__name__, key=key), self.assertRaises(
                    ReceiptError
                ):
                    verifier(receipt)

        for value in (False, 0, len(ROLLBACK_RESOURCE_KEYS) - 1, len(ROLLBACK_RESOURCE_KEYS) + 1):
            receipt = self.rollback_receipt()
            receipt["exact_targets_absent_count"] = value
            receipt["receipt_sha256"] = receipt_sha256(receipt)
            with self.subTest(exact_targets_absent_count=value), self.assertRaises(
                ReceiptError
            ):
                verify_empty_rollback_receipt(receipt)


if __name__ == "__main__":
    unittest.main()

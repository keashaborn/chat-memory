from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.governed_memory_install.durable_receipts import (
    DurableReceiptError,
    DurableReceiptStore,
    PRODUCTION_EXECUTIONS_ROOT,
    ReceiptArtifact,
    StoreBackedEmptyRollbackReceiptSink,
)
from tools.governed_memory_install.receipts import (
    build_empty_rollback_receipt,
    receipt_sha256,
)
from tools.governed_memory_install.postgres_native_stages import _receipt
from tools.governed_memory_install.rollback import (
    build_empty_rollback_eligibility_receipt,
    eligibility_receipt_sha256,
)


EXECUTION_ID = "a" * 64


def _eligibility(*, observation: str = "b" * 64) -> dict[str, object]:
    return build_empty_rollback_eligibility_receipt(
        candidate_git_commit="c" * 40,
        candidate_git_tree="d" * 40,
        package_manifest_sha256="e" * 64,
        installation_execution_id=EXECUTION_ID,
        installation_receipt_sha256="f" * 64,
        exact_targets_sha256="1" * 64,
        resource_ledger_head_sha256="2" * 64,
        observation_set_sha256=observation,
        pilot_ever_started=False,
        postgresql_user_rows=0,
        projection_queue_rows=0,
        qdrant_points=0,
        active_clients=0,
        legacy_imports=0,
        application_services_installed=False,
        source_postgres_read_count=0,
        production_data_read=False,
        provider_calls=0,
    )


def _rollback_receipt() -> dict[str, object]:
    runtime_receipt = "e" * 64
    package = "d" * 64
    runtime_root = "/opt/governed-memory-controller/runtimes/" + runtime_receipt
    release_root = "/opt/governed-memory-controller/releases/" + package
    return build_empty_rollback_receipt(
        execution_id="6" * 64,
        attempt_id="rollback-" + "b" * 40,
        candidate_git_commit="b" * 40,
        candidate_git_tree="c" * 40,
        package_manifest_sha256=package,
        controller_runtime_receipt_sha256=runtime_receipt,
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
        journal_sequence=45,
        resource_ledger_head_sha256="b" * 64,
        resource_ledger_sequence=18,
        eligibility_receipt_sha256="c" * 64,
        retained_audit_set_sha256="d" * 64,
        exact_targets_absent_count=15,
    )


class DurableReceiptStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve() / "executions"
        self.execution_root = self.root / EXECUTION_ID
        self.execution_root.mkdir(parents=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.execution_root, 0o700)
        self.store = DurableReceiptStore.synthetic(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_paths_are_closed_for_all_receipt_kinds(self) -> None:
        self.assertEqual(
            self.store.path(ReceiptArtifact.INSTALL, EXECUTION_ID),
            self.execution_root / "install-receipt.json",
        )
        self.assertEqual(
            self.store.path(
                ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
                EXECUTION_ID,
            ),
            self.execution_root / "empty-rollback-eligibility-receipt.json",
        )
        self.assertEqual(
            self.store.path(ReceiptArtifact.EMPTY_ROLLBACK, EXECUTION_ID),
            self.execution_root / "empty-rollback-receipt.json",
        )
        with self.assertRaisesRegex(
            DurableReceiptError, "execution_id_invalid"
        ):
            self.store.path(ReceiptArtifact.INSTALL, "../escape")

    def test_native_postgres_stage_receipt_is_exact_and_create_once(self) -> None:
        receipt = _receipt(
            mode="rollback",
            final_state="empty",
            runtime_receipt_sha256="3" * 64,
            driver_runtime_identity_sha256="4" * 64,
            terminal_catalog_sha256=None,
            rollback_empty_proof_sha256="5" * 64,
            operations=("r01_drop_exact_role_prefix_transaction",),
        )
        native = {
            "schema_version": (
                "governed-memory-postgres-native-stage-receipt-v1"
            ),
            "mode": receipt.mode,
            "final_state": receipt.final_state,
            "source_closure_sha256": receipt.source_closure_sha256,
            "runtime_receipt_sha256": receipt.runtime_receipt_sha256,
            "driver_runtime_identity_sha256": (
                receipt.driver_runtime_identity_sha256
            ),
            "terminal_catalog_sha256": receipt.terminal_catalog_sha256,
            "rollback_empty_proof_sha256": (
                receipt.rollback_empty_proof_sha256
            ),
            "operations": list(receipt.operations),
            "receipt_sha256": receipt.receipt_sha256,
        }
        wrapper = {
            "schema_version": (
                "governed-memory-postgres-native-stage-durable-receipt-v1"
            ),
            "execution_id": EXECUTION_ID,
            "native_receipt": native,
            "receipt_sha256": receipt.receipt_sha256,
        }
        evidence = self.store.write_once(
            ReceiptArtifact.POSTGRES_ROLLBACK_I11,
            EXECUTION_ID,
            wrapper,
        )
        self.assertEqual(evidence.receipt_sha256, receipt.receipt_sha256)
        self.assertEqual(
            evidence.canonical_file_sha256,
            hashlib.sha256(
                __import__("json").dumps(
                    wrapper,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            ).hexdigest(),
        )
        replay = self.store.write_once(
            ReceiptArtifact.POSTGRES_ROLLBACK_I11,
            EXECUTION_ID,
            wrapper,
        )
        self.assertEqual(replay.canonical_file_sha256, evidence.canonical_file_sha256)

        wrong_stage = copy.deepcopy(wrapper)
        with self.assertRaisesRegex(
            DurableReceiptError, "postgres_native_receipt_invalid"
        ):
            self.store.write_once(
                ReceiptArtifact.POSTGRES_ROLLBACK_I12,
                EXECUTION_ID,
                wrong_stage,
            )

    def test_create_once_is_canonical_fsynced_and_idempotent(self) -> None:
        fsync_descriptors: list[int] = []
        real_fsync = os.fsync

        def tracking_fsync(descriptor: int) -> None:
            fsync_descriptors.append(descriptor)
            real_fsync(descriptor)

        receipt = _eligibility()
        with patch(
            "tools.governed_memory_install.durable_receipts.os.fsync",
            side_effect=tracking_fsync,
        ):
            evidence = self.store.write_once(
                ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
                EXECUTION_ID,
                receipt,
            )
        self.assertGreaterEqual(len(fsync_descriptors), 2)
        path = Path(evidence.path)
        metadata = path.stat(follow_symlinks=False)
        self.assertEqual(metadata.st_mode & 0o777, 0o600)
        self.assertEqual(metadata.st_nlink, 1)
        self.assertEqual(metadata.st_uid, os.geteuid())
        self.assertEqual(
            path.read_bytes(),
            __import__("json").dumps(
                receipt,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii"),
        )
        replay = self.store.write_once(
            ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
            EXECUTION_ID,
            receipt,
        )
        self.assertEqual(replay.canonical_file_sha256, evidence.canonical_file_sha256)
        self.assertEqual(dict(self.store.read(
            ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
            EXECUTION_ID,
        ).canonical_receipt), receipt)

    def test_different_valid_receipt_cannot_replace_existing(self) -> None:
        original = _eligibility()
        path = self.store.path(
            ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
            EXECUTION_ID,
        )
        self.store.write_once(
            ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
            EXECUTION_ID,
            original,
        )
        before = path.read_bytes()
        with self.assertRaisesRegex(
            DurableReceiptError, "replace_refused"
        ):
            self.store.write_once(
                ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
                EXECUTION_ID,
                _eligibility(observation="3" * 64),
            )
        self.assertEqual(path.read_bytes(), before)

    def test_reader_rejects_symlink_hardlink_mode_and_noncanonical_bytes(self) -> None:
        artifact = ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY
        path = self.store.path(artifact, EXECUTION_ID)
        external = Path(self.temporary.name) / "external"
        external.write_bytes(b"{}")
        path.symlink_to(external)
        with self.assertRaises(DurableReceiptError):
            self.store.read(artifact, EXECUTION_ID)
        path.unlink()

        receipt = _eligibility()
        self.store.write_once(artifact, EXECUTION_ID, receipt)
        hardlink = Path(self.temporary.name) / "hardlink"
        os.link(path, hardlink)
        with self.assertRaisesRegex(DurableReceiptError, "file_invalid"):
            self.store.read(artifact, EXECUTION_ID)
        hardlink.unlink()

        os.chmod(path, 0o640)
        with self.assertRaisesRegex(DurableReceiptError, "file_invalid"):
            self.store.read(artifact, EXECUTION_ID)
        os.chmod(path, 0o600)
        path.write_bytes((path.read_text(encoding="ascii") + "\n").encode("ascii"))
        with self.assertRaisesRegex(DurableReceiptError, "not_canonical"):
            self.store.read(artifact, EXECUTION_ID)

    def test_parent_and_intermediate_symlink_are_refused(self) -> None:
        os.chmod(self.execution_root, 0o755)
        with self.assertRaisesRegex(DurableReceiptError, "directory_invalid"):
            self.store.write_once(
                ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
                EXECUTION_ID,
                _eligibility(),
            )
        os.chmod(self.execution_root, 0o700)

        real_root = Path(self.temporary.name) / "real-executions"
        real_execution = real_root / EXECUTION_ID
        real_execution.mkdir(parents=True, mode=0o700)
        os.chmod(real_execution, 0o700)
        linked_root = Path(self.temporary.name) / "linked-executions"
        linked_root.symlink_to(real_root, target_is_directory=True)
        linked_store = DurableReceiptStore.synthetic(linked_root)
        with self.assertRaisesRegex(DurableReceiptError, "directory_invalid"):
            linked_store.write_once(
                ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
                EXECUTION_ID,
                _eligibility(),
            )

    def test_group_or_world_writable_ancestry_is_refused(self) -> None:
        os.chmod(self.root, 0o777)
        try:
            with self.assertRaisesRegex(
                DurableReceiptError, "directory_ancestry_invalid"
            ):
                self.store.write_once(
                    ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
                    EXECUTION_ID,
                    _eligibility(),
                )
        finally:
            os.chmod(self.root, 0o755)

    def test_invalid_or_wrong_kind_receipt_never_creates_file(self) -> None:
        invalid = copy.deepcopy(_eligibility())
        invalid["production_data_read"] = True
        invalid["receipt_sha256"] = eligibility_receipt_sha256(invalid)
        path = self.store.path(
            ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
            EXECUTION_ID,
        )
        with self.assertRaisesRegex(DurableReceiptError, "content_invalid"):
            self.store.write_once(
                ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
                EXECUTION_ID,
                invalid,
            )
        self.assertFalse(path.exists())

        wrong_kind = _eligibility()
        wrong_kind["receipt_sha256"] = receipt_sha256(wrong_kind)
        with self.assertRaisesRegex(DurableReceiptError, "content_invalid"):
            self.store.write_once(
                ReceiptArtifact.INSTALL,
                EXECUTION_ID,
                wrong_kind,
            )

    def test_arbitrary_root_requires_explicit_synthetic_constructor(self) -> None:
        with self.assertRaisesRegex(DurableReceiptError, "store_invalid"):
            DurableReceiptStore(self.root, expected_uid=os.geteuid())

    def test_production_and_synthetic_modes_are_not_interchangeable(self) -> None:
        production = DurableReceiptStore.production()
        production.require_production_binding()
        self.store.require_synthetic_binding()
        with self.assertRaisesRegex(
            DurableReceiptError, "store_not_production"
        ):
            self.store.require_production_binding()
        with self.assertRaisesRegex(
            DurableReceiptError, "store_not_synthetic"
        ):
            production.require_synthetic_binding()
        with self.assertRaisesRegex(DurableReceiptError, "store_invalid"):
            DurableReceiptStore(
                PRODUCTION_EXECUTIONS_ROOT,
                expected_uid=0,
            )

    def test_receipt_execution_identity_must_match_exact_path(self) -> None:
        from tests.memory.test_installation_receipts import (
            InstallationReceiptTests,
        )

        install = InstallationReceiptTests().install_receipt()
        wrong_execution_root = self.root / ("b" * 64)
        wrong_execution_root.mkdir(mode=0o700)
        os.chmod(wrong_execution_root, 0o700)
        with self.assertRaisesRegex(
            DurableReceiptError, "path_binding_mismatch"
        ):
            self.store.write_once(
                ReceiptArtifact.INSTALL,
                "b" * 64,
                install,
            )
        misplaced = self.root / ("c" * 64)
        misplaced.mkdir(mode=0o700)
        os.chmod(misplaced, 0o700)
        with self.assertRaisesRegex(
            DurableReceiptError, "path_binding_mismatch"
        ):
            self.store.write_once(
                ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
                "c" * 64,
                _eligibility(),
            )

    def test_final_rollback_sink_writes_only_rollback_receipt(self) -> None:
        receipt = _rollback_receipt()
        execution_id = str(receipt["execution_id"])
        execution_root = self.root / execution_id
        execution_root.mkdir(mode=0o700)
        os.chmod(execution_root, 0o700)
        evidence = StoreBackedEmptyRollbackReceiptSink(self.store).persist(
            execution_id,
            receipt,
        )
        self.assertEqual(
            Path(evidence.path).name, "empty-rollback-receipt.json"
        )
        self.assertEqual(evidence.receipt_sha256, receipt["receipt_sha256"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest import mock

from tools.governed_memory_validation import process_death_arm_receipt as arm


COMMIT = "1" * 40
TREE = "2" * 40
PACKAGE = "3" * 64
RUNTIME = "4" * 64
CAPSULE = "5" * 64
RESERVATION = "6" * 64
INSTALL_CLAIM = "7" * 64
EXECUTION = "8" * 64
PLAN = "9" * 64
BOUNDARY_RECORD = "a" * 64
JOURNAL_HEAD = BOUNDARY_RECORD
WORKER = "c" * 64


def receipt(boundary_kind: str = arm.INSTALL_BOUNDARY_KIND) -> dict[str, object]:
    if boundary_kind == arm.INSTALL_BOUNDARY_KIND:
        journal_name = "journal.jsonl"
        attempt_id = "install-" + "d" * 40
        step_id = arm.INSTALL_BOUNDARY_STEP_ID
    else:
        journal_name = "rollback.jsonl"
        attempt_id = "rollback-" + "e" * 40
        step_id = arm.EMPTY_ROLLBACK_BOUNDARY_STEP_ID
    document: dict[str, object] = {
        "schema_version": arm.PROCESS_DEATH_ARM_RECEIPT_SCHEMA,
        "result": arm.PROCESS_DEATH_ARM_RECEIPT_RESULT,
        "boundary_kind": boundary_kind,
        "candidate_git_commit": COMMIT,
        "candidate_git_tree": TREE,
        "package_manifest_sha256": PACKAGE,
        "controller_runtime_receipt_sha256": RUNTIME,
        "recovery_capsule_sha256": CAPSULE,
        "recovery_reservation_claim_sha256": RESERVATION,
        "install_authority_claim_sha256": INSTALL_CLAIM,
        "execution_id": EXECUTION,
        "journal_name": journal_name,
        "journal_plan_sha256": PLAN,
        "journal_attempt_id": attempt_id,
        "boundary_step_id": step_id,
        "boundary_event": "applied",
        "boundary_record_sequence": 4,
        "boundary_record_sha256": BOUNDARY_RECORD,
        "journal_sequence_at_arm": 4,
        "journal_head_sha256_at_arm": JOURNAL_HEAD,
        "worker_identity_sha256": WORKER,
        "worker_parent_death_sigkill_armed": True,
        "worker_cooperative_post_fsync_sigstop_observed": True,
        "worker_sigstop_observed": True,
        "worker_child_process_set_empty_at_arm": True,
        "global_execution_lock_exclusion_observed": True,
        "sigkill_required_before_resume": True,
    }
    document["receipt_sha256"] = hashlib.sha256(
        arm._canonical(document)
    ).hexdigest()
    return document


def reseal(document: dict[str, object]) -> dict[str, object]:
    document["receipt_sha256"] = hashlib.sha256(
        arm._canonical(
            {key: value for key, value in document.items() if key != "receipt_sha256"}
        )
    ).hexdigest()
    return document


def synthetic_complete_staging(
    *,
    parent_fd: int,
    parent_path: Path,
    name: str,
    raw: bytes,
    expected_uid: int,
    expected_gid: int,
) -> tuple[int, int]:
    del parent_path
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o400,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(descriptor, 0o400)
        metadata = os.fstat(descriptor)
        if metadata.st_uid != expected_uid or metadata.st_gid != expected_gid:
            os.fchown(descriptor, expected_uid, expected_gid)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        complete = os.fstat(descriptor)
        return complete.st_dev, complete.st_ino
    finally:
        os.close(descriptor)


class ProcessDeathArmReceiptTests(unittest.TestCase):
    def temporary_store(self, temporary: str) -> tuple[Path, Path, int, int]:
        root = Path(temporary) / "executions-v4"
        execution = root / EXECUTION
        root.mkdir(mode=0o700)
        execution.mkdir(mode=0o700)
        os.chmod(root, 0o700)
        os.chmod(execution, 0o700)
        return root, execution, os.getuid(), os.getgid()

    def persist_synthetic(
        self,
        *,
        root: Path,
        uid: int,
        gid: int,
        document: dict[str, object],
    ) -> MappingProxyType[str, object] | object:
        with mock.patch.object(
            arm,
            "_write_complete_staging_from_anonymous",
            side_effect=synthetic_complete_staging,
        ):
            return arm._persist_for_test(
                executions_root=root,
                expected_uid=uid,
                expected_gid=gid,
                receipt=document,
            )

    def test_verifier_requires_boundary_as_exact_head_at_cooperative_stop(self) -> None:
        document = receipt()
        verified = arm.verify_process_death_arm_receipt(
            MappingProxyType(document),
            expected_execution_id=EXECUTION,
            expected_boundary_kind=arm.INSTALL_BOUNDARY_KIND,
        )
        self.assertEqual(dict(verified), document)
        self.assertEqual(
            verified["boundary_record_sequence"],
            verified["journal_sequence_at_arm"],
        )
        self.assertEqual(
            verified["boundary_record_sha256"],
            verified["journal_head_sha256_at_arm"],
        )

    def test_verifier_rejects_non_exact_structure_and_semantics(self) -> None:
        changes = (
            {"schema_version": "v0"},
            {"result": "worker_killed"},
            {"candidate_git_commit": "1" * 64},
            {"package_manifest_sha256": "A" * 64},
            {"journal_name": "other.jsonl"},
            {"journal_attempt_id": "caller-selected"},
            {"boundary_step_id": arm.EMPTY_ROLLBACK_BOUNDARY_STEP_ID},
            {"boundary_event": "intent"},
            {"boundary_record_sequence": False},
            {"boundary_record_sequence": 0},
            {"boundary_record_sequence": 7},
            {"journal_sequence_at_arm": True},
            {"journal_sequence_at_arm": 5},
            {"journal_head_sha256_at_arm": "b" * 64},
            {"worker_parent_death_sigkill_armed": False},
            {"worker_cooperative_post_fsync_sigstop_observed": False},
            {"worker_sigstop_observed": 1},
            {"worker_child_process_set_empty_at_arm": False},
            {"global_execution_lock_exclusion_observed": False},
            {"sigkill_required_before_resume": False},
            {"receipt_sha256": "f" * 64},
        )
        for changed in changes:
            document = reseal(dict(receipt(), **changed))
            if "receipt_sha256" in changed:
                document["receipt_sha256"] = str(changed["receipt_sha256"])
            with self.subTest(changed=changed), self.assertRaises(
                arm.ProcessDeathArmReceiptError
            ):
                arm.verify_process_death_arm_receipt(document)

    def test_verifier_rejects_extra_or_missing_key_and_wrong_binding(self) -> None:
        extra = dict(receipt(), unexpected=True)
        missing = receipt()
        del missing["worker_identity_sha256"]
        for document in (extra, missing):
            with self.assertRaises(arm.ProcessDeathArmReceiptError):
                arm.verify_process_death_arm_receipt(document)
        with self.assertRaises(arm.ProcessDeathArmReceiptError):
            arm.verify_process_death_arm_receipt(
                receipt(), expected_execution_id="f" * 64
            )
        with self.assertRaises(arm.ProcessDeathArmReceiptError):
            arm.verify_process_death_arm_receipt(
                receipt(), expected_boundary_kind=arm.EMPTY_ROLLBACK_BOUNDARY_KIND
            )

    def test_strict_parser_rejects_duplicate_keys_and_noncanonical_json(self) -> None:
        duplicate = b'{"schema_version":"x","schema_version":"y"}'
        with self.assertRaises(arm.ProcessDeathArmReceiptError):
            arm._parse_and_verify(
                duplicate,
                expected_execution_id=EXECUTION,
                expected_boundary_kind=arm.INSTALL_BOUNDARY_KIND,
            )
        noncanonical = arm._canonical(receipt()) + b"\n"
        with self.assertRaises(arm.ProcessDeathArmReceiptError):
            arm._parse_and_verify(
                noncanonical,
                expected_execution_id=EXECUTION,
                expected_boundary_kind=arm.INSTALL_BOUNDARY_KIND,
            )

    def test_exact_production_paths_are_derived_without_traversal(self) -> None:
        self.assertEqual(
            arm.process_death_arm_receipt_path(
                execution_id=EXECUTION,
                boundary_kind=arm.INSTALL_BOUNDARY_KIND,
            ),
            arm.EXECUTIONS_ROOT
            / EXECUTION
            / "install-process-death-arm-receipt.json",
        )
        self.assertEqual(
            arm.process_death_arm_receipt_path(
                execution_id=EXECUTION,
                boundary_kind=arm.EMPTY_ROLLBACK_BOUNDARY_KIND,
            ),
            arm.EXECUTIONS_ROOT
            / EXECUTION
            / "rollback-process-death-arm-receipt.json",
        )
        for execution_id, boundary_kind in (
            ("../" + EXECUTION, arm.INSTALL_BOUNDARY_KIND),
            (EXECUTION, "rollback"),
        ):
            with self.assertRaises(arm.ProcessDeathArmReceiptError):
                arm.process_death_arm_receipt_path(
                    execution_id=execution_id, boundary_kind=boundary_kind
                )

    def test_create_once_replay_is_0400_nlink1_and_conflict_never_replaces(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, execution, uid, gid = self.temporary_store(temporary)
            first_document = receipt()
            first = self.persist_synthetic(
                root=root, uid=uid, gid=gid, document=first_document
            )
            second = self.persist_synthetic(
                root=root, uid=uid, gid=gid, document=first_document
            )
            final = execution / "install-process-death-arm-receipt.json"
            original = final.read_bytes()
            metadata = final.stat(follow_symlinks=False)
            self.assertEqual(dict(first), dict(second))
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o400)
            self.assertEqual(metadata.st_uid, uid)
            self.assertEqual(metadata.st_gid, gid)
            self.assertEqual(metadata.st_nlink, 1)
            self.assertFalse(Path(str(final) + ".publishing").exists())

            conflicting = reseal(
                dict(first_document, worker_identity_sha256="f" * 64)
            )
            with self.assertRaisesRegex(
                arm.ProcessDeathArmReceiptError, "conflict"
            ):
                self.persist_synthetic(
                    root=root, uid=uid, gid=gid, document=conflicting
                )
            self.assertEqual(final.read_bytes(), original)

    def test_complete_staging_is_safely_finished_by_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, execution, uid, gid = self.temporary_store(temporary)
            final = execution / "install-process-death-arm-receipt.json"
            staging = Path(str(final) + ".publishing")
            raw = arm._canonical(receipt())
            staging.write_bytes(raw)
            os.chmod(staging, 0o400)
            verified = arm._read_for_test(
                executions_root=root,
                expected_uid=uid,
                expected_gid=gid,
                execution_id=EXECUTION,
                boundary_kind=arm.INSTALL_BOUNDARY_KIND,
            )
            self.assertEqual(dict(verified or {}), receipt())
            self.assertTrue(final.exists())
            self.assertFalse(staging.exists())
            self.assertEqual(final.stat(follow_symlinks=False).st_nlink, 1)

    def test_dual_link_crash_window_is_safely_finished_by_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, execution, uid, gid = self.temporary_store(temporary)
            final = execution / "install-process-death-arm-receipt.json"
            staging = Path(str(final) + ".publishing")
            staging.write_bytes(arm._canonical(receipt()))
            os.chmod(staging, 0o400)
            os.link(staging, final)
            self.assertEqual(staging.stat(follow_symlinks=False).st_nlink, 2)
            verified = arm._read_for_test(
                executions_root=root,
                expected_uid=uid,
                expected_gid=gid,
                execution_id=EXECUTION,
                boundary_kind=arm.INSTALL_BOUNDARY_KIND,
            )
            self.assertEqual(dict(verified or {}), receipt())
            self.assertFalse(staging.exists())
            self.assertEqual(final.stat(follow_symlinks=False).st_nlink, 1)

    def test_partial_or_foreign_staging_is_refused_without_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, execution, uid, gid = self.temporary_store(temporary)
            staging = execution / (
                "install-process-death-arm-receipt.json.publishing"
            )
            staging.write_bytes(b'{"partial":')
            os.chmod(staging, 0o400)
            with self.assertRaises(arm.ProcessDeathArmReceiptError):
                arm._read_for_test(
                    executions_root=root,
                    expected_uid=uid,
                    expected_gid=gid,
                    execution_id=EXECUTION,
                    boundary_kind=arm.INSTALL_BOUNDARY_KIND,
                )
            self.assertEqual(staging.read_bytes(), b'{"partial":')

    def test_separate_valid_final_and_staging_inodes_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, execution, uid, gid = self.temporary_store(temporary)
            raw = arm._canonical(receipt())
            final = execution / "install-process-death-arm-receipt.json"
            staging = Path(str(final) + ".publishing")
            final.write_bytes(raw)
            staging.write_bytes(raw)
            os.chmod(final, 0o400)
            os.chmod(staging, 0o400)
            with self.assertRaisesRegex(
                arm.ProcessDeathArmReceiptError, "conflict"
            ):
                arm._read_for_test(
                    executions_root=root,
                    expected_uid=uid,
                    expected_gid=gid,
                    execution_id=EXECUTION,
                    boundary_kind=arm.INSTALL_BOUNDARY_KIND,
                )
            self.assertTrue(final.exists())
            self.assertTrue(staging.exists())

    def test_exact_parent_modes_and_identity_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, execution, uid, gid = self.temporary_store(temporary)
            os.chmod(execution, 0o750)
            with self.assertRaisesRegex(
                arm.ProcessDeathArmReceiptError, "parent_invalid"
            ):
                arm._read_for_test(
                    executions_root=root,
                    expected_uid=uid,
                    expected_gid=gid,
                    execution_id=EXECUTION,
                    boundary_kind=arm.INSTALL_BOUNDARY_KIND,
                )
        with tempfile.TemporaryDirectory() as temporary:
            root, _, uid, gid = self.temporary_store(temporary)
            os.chmod(root, 0o755)
            with self.assertRaisesRegex(
                arm.ProcessDeathArmReceiptError, "parent_invalid"
            ):
                arm._read_for_test(
                    executions_root=root,
                    expected_uid=uid,
                    expected_gid=gid,
                    execution_id=EXECUTION,
                    boundary_kind=arm.INSTALL_BOUNDARY_KIND,
                )

    def test_install_and_rollback_receipts_have_independent_create_once_names(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, execution, uid, gid = self.temporary_store(temporary)
            install = self.persist_synthetic(
                root=root, uid=uid, gid=gid, document=receipt()
            )
            rollback = self.persist_synthetic(
                root=root,
                uid=uid,
                gid=gid,
                document=receipt(arm.EMPTY_ROLLBACK_BOUNDARY_KIND),
            )
            self.assertEqual(install["boundary_kind"], arm.INSTALL_BOUNDARY_KIND)
            self.assertEqual(
                rollback["boundary_kind"], arm.EMPTY_ROLLBACK_BOUNDARY_KIND
            )
            self.assertTrue(
                (execution / "install-process-death-arm-receipt.json").exists()
            )
            self.assertTrue(
                (execution / "rollback-process-death-arm-receipt.json").exists()
            )

    def test_public_storage_requires_root_before_opening_production_path(self) -> None:
        with (
            mock.patch.object(arm.os, "geteuid", return_value=1000),
            self.assertRaisesRegex(
                arm.ProcessDeathArmReceiptError, "root_required"
            ),
        ):
            arm.persist_process_death_arm_receipt(receipt())

    def test_anonymous_writer_closes_inode_on_zero_write(self) -> None:
        created = SimpleNamespace(
            st_dev=1,
            st_ino=2,
            st_mode=stat.S_IFREG | 0o400,
            st_uid=0,
            st_gid=0,
            st_nlink=0,
            st_size=0,
            st_mtime_ns=1,
            st_ctime_ns=1,
        )
        with (
            mock.patch.object(arm.os, "O_TMPFILE", 0x400000, create=True),
            mock.patch.object(arm.os, "open", return_value=77),
            mock.patch.object(arm.os, "fstat", return_value=created),
            mock.patch.object(arm.os, "fchown"),
            mock.patch.object(arm.os, "fchmod"),
            mock.patch.object(arm.os, "write", return_value=0),
            mock.patch.object(arm.os, "close") as close,
            self.assertRaisesRegex(
                arm.ProcessDeathArmReceiptError, "write_failed"
            ),
        ):
            arm._open_complete_anonymous_inode(
                parent_fd=9,
                raw=b"proof",
                expected_uid=0,
                expected_gid=0,
            )
        close.assert_called_once_with(77)

    @unittest.skipUnless(
        sys.platform.startswith("linux") and hasattr(os, "O_TMPFILE"),
        "Linux O_TMPFILE and linkat AT_EMPTY_PATH required",
    )
    def test_real_linux_anonymous_publication_is_create_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, execution, uid, gid = self.temporary_store(temporary)
            try:
                verified = arm._persist_for_test(
                    executions_root=root,
                    expected_uid=uid,
                    expected_gid=gid,
                    receipt=receipt(),
                )
            except arm.ProcessDeathArmReceiptError as error:
                self.skipTest(str(error))
            final = execution / "install-process-death-arm-receipt.json"
            self.assertEqual(dict(verified), receipt())
            self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o400)
            self.assertEqual(final.stat().st_nlink, 1)


if __name__ == "__main__":
    unittest.main()

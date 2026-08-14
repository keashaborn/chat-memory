from __future__ import annotations

import hashlib
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest import mock

from tools.governed_memory_validation import (
    durable_live_proof_receipt as durable,
)
from tools.governed_memory_validation import (
    run_disposable_installation_live_proof as proof,
)


COMMIT = "1" * 40
TREE = "2" * 40
PACKAGE = "3" * 64
RUNTIME = "4" * 64
CAPSULE = "5" * 64
SCHEMA = b"exact-live-proof-schema"


def inputs() -> proof.ProofInputs:
    return proof.ProofInputs(
        candidate_git_commit=COMMIT,
        candidate_git_tree=TREE,
        package_manifest_sha256=PACKAGE,
        controller_runtime_receipt_sha256=RUNTIME,
    )


def artifacts() -> MappingProxyType[str, bytes]:
    return MappingProxyType(
        {durable.LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE: SCHEMA}
    )


def live_receipt() -> dict[str, object]:
    hashes = {
        key: "a" * 64
        for key in durable._RECEIPT_KEYS
        if key.endswith("sha256") or key.endswith("execution_id")
    }
    receipt: dict[str, object] = {
        **hashes,
        "schema_version": durable.LIVE_PROOF_RECEIPT_SCHEMA,
        "result": "disposable_install_crash_resume_and_empty_rollback_proved",
        "observation_scope": (
            "one_bounded_disposable_linux_execution_terminal_snapshot"
        ),
        "candidate_git_commit": COMMIT,
        "candidate_git_tree": TREE,
        "package_manifest_sha256": PACKAGE,
        "controller_runtime_receipt_sha256": RUNTIME,
        "live_proof_receipt_schema_sha256": hashlib.sha256(SCHEMA).hexdigest(),
        "recovery_capsule_sha256": CAPSULE,
        "authorization_text_sha256": durable.EXPECTED_AUTHORIZATION_TEXT_SHA256,
        "install_process_death_boundary_sha256": "b" * 64,
        "install_process_death_arm_receipt_sha256": "c" * 64,
        "rollback_process_death_boundary_sha256": "d" * 64,
        "rollback_process_death_arm_receipt_sha256": "e" * 64,
        "start_authority_pair_claimed_atomically": True,
        "recovery_capsule_published_before_first_install_effect": True,
        "recovery_reservation_claimed_before_first_install_effect": True,
        "ephemeral_private_signer_retained_at_execution_start": False,
        "recovery_capsule_retained_at_terminal_observation": True,
        "install_process_death_observed": True,
        "install_exact_resume_completed": True,
        "cold_controller_process_restart_terminal_postflight_verified": True,
        "cold_restart_scope": "controller_worker_process_only",
        "host_reboot_proven": False,
        "persistent_store_restart_supervision_and_boot_recovery_proven": False,
        "rollback_process_death_observed": True,
        "rollback_exact_resume_completed": True,
        "public_install_entrypoint_used": True,
        "public_empty_rollback_entrypoint_used": True,
        "fresh_r06_semantic_empty_recheck_required": True,
        "completed_public_rollback_replayed": True,
        "exact_rollback_resources_absent_count": 15,
        "exact_resources_absent_at_terminal_observation": True,
        "terminal_absence_is_continuous_guarantee": False,
        "stores_installed_at_terminal_observation": False,
        "stores_supervisor_installed_at_terminal_observation": False,
        "controller_runtime_capability_reverified": True,
        "host_clock_synchronization_preflight_passed": True,
        "source_postgres_read_count": 0,
        "source_postgres_write_count": 0,
        "provider_calls": 0,
        "production_data_read": False,
        "application_services_installed": False,
        "activation_performed": False,
        "issuer_or_host_death_durable_cleanup_proven": False,
    }
    receipt["receipt_sha256"] = durable._sha(
        durable._canonical(
            {
                key: value
                for key, value in receipt.items()
                if key != "receipt_sha256"
            }
        )
    )
    return receipt


def synthetic_complete_staging(
    parent_fd: int,
    parent_path: Path,
    name: str,
    raw: bytes,
    *,
    expected_uid: int,
    expected_gid: int,
) -> tuple[int, int]:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o400,
        dir_fd=parent_fd,
    )
    try:
        os.fchown(descriptor, expected_uid, expected_gid)
        os.fchmod(descriptor, 0o400)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        inode = (metadata.st_dev, metadata.st_ino)
    finally:
        os.close(descriptor)
    os.fsync(parent_fd)
    return inode


class DurableLiveProofReceiptPublicationTests(unittest.TestCase):
    def open_parent(self, temporary: str) -> tuple[Path, int, int, int]:
        parent = Path(temporary)
        os.chmod(parent, 0o700)
        descriptor = os.open(parent, os.O_RDONLY | os.O_CLOEXEC)
        return parent, descriptor, os.getuid(), os.getgid()

    def persist_synthetic(
        self,
        parent: Path,
        descriptor: int,
        uid: int,
        gid: int,
        receipt: dict[str, object],
    ) -> MappingProxyType[str, object] | object:
        with mock.patch.object(
            durable,
            "_write_complete_staging_from_anonymous",
            side_effect=synthetic_complete_staging,
        ):
            return durable._persist_at_open_parent(
                parent_fd=descriptor,
                parent_path=parent,
                final_name="proof.json",
                expected_uid=uid,
                expected_gid=gid,
                inputs=inputs(),
                artifacts=artifacts(),
                capsule_sha256=CAPSULE,
                receipt=receipt,
            )

    def test_verifier_accepts_mapping_proxy_and_binds_exact_success(self) -> None:
        self.assertEqual(
            durable.EXPECTED_AUTHORIZATION_TEXT_SHA256,
            proof.AUTHORIZED_TEXT_SHA256,
        )
        self.assertEqual(
            durable.LIVE_PROOF_RECEIPT_SCHEMA,
            "governed-memory-phase9-live-proof-receipt-v6",
        )
        verified = durable.verify_promotable_live_proof_receipt(
            inputs=inputs(),
            artifacts=artifacts(),
            capsule_sha256=CAPSULE,
            receipt=MappingProxyType(live_receipt()),
        )
        self.assertEqual(verified["candidate_git_commit"], COMMIT)
        self.assertTrue(verified["install_process_death_observed"])
        self.assertTrue(verified["rollback_process_death_observed"])
        self.assertTrue(verified["exact_resources_absent_at_terminal_observation"])

    def test_recovery_or_missing_fault_boundary_is_never_promotable(self) -> None:
        for changed in (
            {"schema_version": proof.RECOVERY_RECEIPT_SCHEMA},
            {"install_process_death_observed": False},
            {"rollback_process_death_observed": False},
            {"stores_installed_at_terminal_observation": True},
            {"start_authority_pair_claimed_atomically": False},
        ):
            receipt = dict(live_receipt(), **changed)
            receipt["receipt_sha256"] = durable._sha(
                durable._canonical(
                    {
                        key: value
                        for key, value in receipt.items()
                        if key != "receipt_sha256"
                    }
                )
            )
            with self.subTest(changed=changed), self.assertRaises(
                durable.DurableLiveProofReceiptError
            ):
                durable.verify_promotable_live_proof_receipt(
                    inputs=inputs(),
                    artifacts=artifacts(),
                    capsule_sha256=CAPSULE,
                    receipt=receipt,
                )

    def test_verifier_rejects_forged_bindings_and_non_integer_counters(self) -> None:
        for changed in (
            {"authorization_text_sha256": "b" * 64},
            {"install_process_death_boundary_sha256": "b" * 63},
            {"rollback_process_death_arm_receipt_sha256": "b" * 63},
            {"source_postgres_read_count": False},
            {"source_postgres_write_count": 0.0},
            {"provider_calls": False},
            {"exact_rollback_resources_absent_count": 15.0},
        ):
            receipt = dict(live_receipt(), **changed)
            receipt["receipt_sha256"] = durable._sha(
                durable._canonical(
                    {
                        key: value
                        for key, value in receipt.items()
                        if key != "receipt_sha256"
                    }
                )
            )
            with self.subTest(changed=changed), self.assertRaises(
                durable.DurableLiveProofReceiptError
            ):
                durable.verify_promotable_live_proof_receipt(
                    inputs=inputs(),
                    artifacts=artifacts(),
                    capsule_sha256=CAPSULE,
                    receipt=receipt,
                )

    def test_create_once_publication_is_0400_nlink1_and_exact_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent, descriptor, uid, gid = self.open_parent(temporary)
            try:
                first = self.persist_synthetic(
                    parent, descriptor, uid, gid, live_receipt()
                )
                second = self.persist_synthetic(
                    parent, descriptor, uid, gid, live_receipt()
                )
                metadata = (parent / "proof.json").stat(follow_symlinks=False)
                self.assertEqual(dict(first), dict(second))
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o400)
                self.assertEqual(metadata.st_nlink, 1)
                self.assertEqual(metadata.st_uid, uid)
                self.assertEqual(metadata.st_gid, gid)
                self.assertFalse((parent / "proof.json.publishing").exists())
            finally:
                os.close(descriptor)

    def test_public_reader_reconciles_two_link_receipt_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent, descriptor, uid, gid = self.open_parent(temporary)
            raw = durable._canonical(live_receipt())
            try:
                inode = synthetic_complete_staging(
                    descriptor,
                    parent,
                    "proof.json.publishing",
                    raw,
                    expected_uid=uid,
                    expected_gid=gid,
                )
                os.link(
                    "proof.json.publishing",
                    "proof.json",
                    src_dir_fd=descriptor,
                    dst_dir_fd=descriptor,
                    follow_symlinks=False,
                )
                os.fsync(descriptor)
                self.assertEqual(
                    os.stat(
                        "proof.json.publishing",
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    ).st_nlink,
                    2,
                )
                result = durable._read_at_open_parent(
                    parent_fd=descriptor,
                    parent_path=parent,
                    final_name="proof.json",
                    expected_uid=uid,
                    expected_gid=gid,
                    inputs=inputs(),
                    artifacts=artifacts(),
                    capsule_sha256=CAPSULE,
                )
                self.assertEqual(dict(result or {}), live_receipt())
                final = os.stat(
                    "proof.json", dir_fd=descriptor, follow_symlinks=False
                )
                self.assertEqual((final.st_dev, final.st_ino), inode)
                self.assertEqual(final.st_nlink, 1)
                self.assertFalse((parent / "proof.json.publishing").exists())
            finally:
                os.close(descriptor)

    def test_conflicting_final_fails_closed_without_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent, descriptor, uid, gid = self.open_parent(temporary)
            try:
                self.persist_synthetic(parent, descriptor, uid, gid, live_receipt())
                original = (parent / "proof.json").read_bytes()
                conflicting = live_receipt()
                conflicting["retained_audit_set_sha256"] = "b" * 64
                conflicting["receipt_sha256"] = durable._sha(
                    durable._canonical(
                        {
                            key: value
                            for key, value in conflicting.items()
                            if key != "receipt_sha256"
                        }
                    )
                )
                with self.assertRaisesRegex(
                    durable.DurableLiveProofReceiptError, "conflict"
                ):
                    self.persist_synthetic(
                        parent, descriptor, uid, gid, conflicting
                    )
                self.assertEqual((parent / "proof.json").read_bytes(), original)
            finally:
                os.close(descriptor)

    def test_complete_staging_reconciles_after_process_death_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent, descriptor, uid, gid = self.open_parent(temporary)
            try:
                raw = durable._canonical(live_receipt())
                inode = synthetic_complete_staging(
                    descriptor,
                    parent,
                    "proof.json.publishing",
                    raw,
                    expected_uid=uid,
                    expected_gid=gid,
                )
                result = durable._reconcile_at_path(
                    parent_fd=descriptor,
                    parent_path=parent,
                    final_name="proof.json",
                    expected_uid=uid,
                    expected_gid=gid,
                    inputs=inputs(),
                    artifacts=artifacts(),
                    capsule_sha256=CAPSULE,
                    expected_raw=raw,
                )
                final = (parent / "proof.json").stat(follow_symlinks=False)
                self.assertEqual(dict(result or {}), live_receipt())
                self.assertEqual((final.st_dev, final.st_ino), inode)
                self.assertEqual(final.st_nlink, 1)
                self.assertFalse((parent / "proof.json.publishing").exists())
            finally:
                os.close(descriptor)

    def test_anonymous_writer_handles_short_writes(self) -> None:
        raw = b"abcdef"
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
        complete = SimpleNamespace(**{**vars(created), "st_size": len(raw)})
        with (
            mock.patch.object(durable.os, "O_TMPFILE", 0x400000, create=True),
            mock.patch.object(durable.os, "open", return_value=77),
            mock.patch.object(
                durable.os, "fstat", side_effect=(created, complete, complete)
            ),
            mock.patch.object(durable.os, "fchown"),
            mock.patch.object(durable.os, "fchmod"),
            mock.patch.object(
                durable.os, "write", side_effect=(2, len(raw) - 2)
            ) as write,
            mock.patch.object(durable.os, "fsync"),
            mock.patch.object(durable.os, "lseek", return_value=0),
            mock.patch.object(durable.os, "read", side_effect=(raw, b"")),
            mock.patch.object(durable.os, "close") as close,
        ):
            descriptor, inode = durable._open_complete_anonymous_inode(
                9, raw, expected_uid=0, expected_gid=0
            )
            self.assertEqual((descriptor, inode), (77, (1, 2)))
        self.assertEqual(write.call_count, 2)
        close.assert_not_called()

    def test_zero_write_or_fsync_failure_closes_anonymous_inode(self) -> None:
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
        for operation in ("write", "fsync"):
            operation_patch = (
                mock.patch.object(durable.os, "write", return_value=0)
                if operation == "write"
                else mock.patch.object(
                    durable.os, "fsync", side_effect=OSError("fsync")
                )
            )
            with self.subTest(operation=operation):
                with (
                mock.patch.object(durable.os, "O_TMPFILE", 0x400000, create=True),
                mock.patch.object(durable.os, "open", return_value=77),
                mock.patch.object(durable.os, "fstat", return_value=created),
                mock.patch.object(durable.os, "fchown"),
                mock.patch.object(durable.os, "fchmod"),
                operation_patch,
                mock.patch.object(durable.os, "close") as close,
                self.assertRaisesRegex(
                    durable.DurableLiveProofReceiptError, "write_failed"
                ),
                ):
                    durable._open_complete_anonymous_inode(
                        9, b"proof", expected_uid=0, expected_gid=0
                    )
                close.assert_called_once_with(77)

    def test_close_failure_blocks_staging_success(self) -> None:
        metadata = SimpleNamespace(st_dev=1, st_ino=2)
        with (
            mock.patch.object(
                durable,
                "_open_complete_anonymous_inode",
                return_value=(77, (1, 2)),
            ),
            mock.patch.object(durable, "_link_anonymous_inode"),
            mock.patch.object(
                durable, "_read_member", return_value=(b"proof", metadata)
            ),
            mock.patch.object(durable.os, "close", side_effect=OSError("close")),
            self.assertRaisesRegex(
                durable.DurableLiveProofReceiptError, "close_failed"
            ),
        ):
            durable._write_complete_staging_from_anonymous(
                9,
                Path("/fixed"),
                "proof.publishing",
                b"proof",
                expected_uid=0,
                expected_gid=0,
            )

    def test_preflight_leaves_no_named_probe_and_reconciles_exact_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent, descriptor, uid, gid = self.open_parent(temporary)
            try:
                with mock.patch.object(
                    durable,
                    "_write_complete_staging_from_anonymous",
                    side_effect=synthetic_complete_staging,
                ):
                    result = durable._preflight_at_open_parent(
                        parent_fd=descriptor,
                        parent_path=parent,
                        final_name="proof.json",
                        expected_uid=uid,
                        expected_gid=gid,
                        inputs=inputs(),
                        artifacts=artifacts(),
                        capsule_sha256=CAPSULE,
                    )
                self.assertIsNone(result)
                self.assertFalse((parent / "proof.json").exists())
                self.assertFalse((parent / "proof.json.publishing").exists())
                synthetic_complete_staging(
                    descriptor,
                    parent,
                    "proof.json.publishing",
                    durable.ANONYMOUS_PUBLICATION_PREFLIGHT_BYTES,
                    expected_uid=uid,
                    expected_gid=gid,
                )
                with mock.patch.object(
                    durable,
                    "_write_complete_staging_from_anonymous",
                    side_effect=synthetic_complete_staging,
                ):
                    durable._preflight_at_open_parent(
                        parent_fd=descriptor,
                        parent_path=parent,
                        final_name="proof.json",
                        expected_uid=uid,
                        expected_gid=gid,
                        inputs=inputs(),
                        artifacts=artifacts(),
                        capsule_sha256=CAPSULE,
                    )
                self.assertFalse((parent / "proof.json.publishing").exists())
            finally:
                os.close(descriptor)

    def test_public_reader_removes_exact_killed_preflight_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent, descriptor, uid, gid = self.open_parent(temporary)
            fixed = parent / "proof.json"
            try:
                synthetic_complete_staging(
                    descriptor,
                    parent,
                    "proof.json.publishing",
                    durable.ANONYMOUS_PUBLICATION_PREFLIGHT_BYTES,
                    expected_uid=uid,
                    expected_gid=gid,
                )
                result = durable._read_at_open_parent(
                    parent_fd=descriptor,
                    parent_path=parent,
                    final_name=fixed.name,
                    expected_uid=uid,
                    expected_gid=gid,
                    inputs=inputs(),
                    artifacts=artifacts(),
                    capsule_sha256=CAPSULE,
                )
                self.assertIsNone(result)
                self.assertFalse((parent / "proof.json.publishing").exists())
                self.assertFalse(fixed.exists())
            finally:
                os.close(descriptor)

    @unittest.skipUnless(
        sys.platform.startswith("linux") and hasattr(os, "O_TMPFILE"),
        "Linux O_TMPFILE and linkat AT_EMPTY_PATH required",
    )
    def test_sigkill_during_preflight_is_recovered_by_public_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent, descriptor, uid, gid = self.open_parent(temporary)
            fixed = parent / "proof.json"
            try:
                try:
                    durable._write_complete_staging_from_anonymous(
                        descriptor,
                        parent,
                        "capability-probe",
                        b"probe",
                        expected_uid=uid,
                        expected_gid=gid,
                    )
                except durable.DurableLiveProofReceiptError as error:
                    self.skipTest(str(error))
                os.unlink("capability-probe", dir_fd=descriptor)
                os.fsync(descriptor)
                pid = os.fork()
                if pid == 0:
                    with mock.patch.object(
                        durable,
                        "_read_member",
                        side_effect=lambda *args, **kwargs: os.kill(
                            os.getpid(), signal.SIGKILL
                        ),
                    ):
                        durable._write_complete_staging_from_anonymous(
                            descriptor,
                            parent,
                            "proof.json.publishing",
                            durable.ANONYMOUS_PUBLICATION_PREFLIGHT_BYTES,
                            expected_uid=uid,
                            expected_gid=gid,
                        )
                    os._exit(3)
                waited, status = os.waitpid(pid, 0)
                self.assertEqual(waited, pid)
                self.assertTrue(os.WIFSIGNALED(status))
                self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
                result = durable._read_at_open_parent(
                    parent_fd=descriptor,
                    parent_path=parent,
                    final_name=fixed.name,
                    expected_uid=uid,
                    expected_gid=gid,
                    inputs=inputs(),
                    artifacts=artifacts(),
                    capsule_sha256=CAPSULE,
                )
                self.assertIsNone(result)
                self.assertFalse((parent / "proof.json.publishing").exists())
                self.assertFalse(fixed.exists())
            finally:
                os.close(descriptor)

    @unittest.skipUnless(
        sys.platform.startswith("linux") and hasattr(os, "O_TMPFILE"),
        "Linux O_TMPFILE and linkat AT_EMPTY_PATH required",
    )
    def test_sigkill_after_anonymous_link_leaves_complete_reconcilable_staging(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent, descriptor, uid, gid = self.open_parent(temporary)
            raw = durable._canonical(live_receipt())
            try:
                try:
                    durable._write_complete_staging_from_anonymous(
                        descriptor,
                        parent,
                        "capability-probe",
                        b"probe",
                        expected_uid=uid,
                        expected_gid=gid,
                    )
                except durable.DurableLiveProofReceiptError as error:
                    self.skipTest(str(error))
                os.unlink("capability-probe", dir_fd=descriptor)
                os.fsync(descriptor)
                pid = os.fork()
                if pid == 0:
                    with mock.patch.object(
                        durable,
                        "_read_member",
                        side_effect=lambda *args, **kwargs: os.kill(
                            os.getpid(), signal.SIGKILL
                        ),
                    ):
                        durable._write_complete_staging_from_anonymous(
                            descriptor,
                            parent,
                            "proof.json.publishing",
                            raw,
                            expected_uid=uid,
                            expected_gid=gid,
                        )
                    os._exit(3)
                waited, status = os.waitpid(pid, 0)
                self.assertEqual(waited, pid)
                self.assertTrue(os.WIFSIGNALED(status))
                self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
                result = durable._reconcile_at_path(
                    parent_fd=descriptor,
                    parent_path=parent,
                    final_name="proof.json",
                    expected_uid=uid,
                    expected_gid=gid,
                    inputs=inputs(),
                    artifacts=artifacts(),
                    capsule_sha256=CAPSULE,
                    expected_raw=raw,
                )
                self.assertEqual(dict(result or {}), live_receipt())
                self.assertFalse((parent / "proof.json.publishing").exists())
            finally:
                os.close(descriptor)


if __name__ == "__main__":
    unittest.main()

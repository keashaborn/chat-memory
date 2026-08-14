from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
from typing import Sequence
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools.governed_memory_install.authority_state import (
    AuthorityState,
    nonce_sha256,
    operation_sha256,
)
from tools.governed_memory_install.execution_lock import GlobalExecutionLock
from tools.governed_memory_validation import pre_effect_disposition as disposition


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
EXECUTION_ID = "e" * 64
JOURNAL_BINDING = "f" * 64
RESOURCE_LEDGER_BINDING = "9" * 64
AUTHORIZATION_TEXT_SHA256 = hashlib.sha256(
    b"AUTHORIZE EXACT PRE-EFFECT DISPOSITION"
).hexdigest()


class _AbsentHostRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.stdout_by_command: dict[tuple[str, ...], bytes] = {}
        self.stderr_by_command: dict[tuple[str, ...], bytes] = {}
        self.returncode_by_command: dict[tuple[str, ...], int] = {}

    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        command = tuple(argv)
        self.calls.append(command)
        return subprocess.CompletedProcess(
            command,
            self.returncode_by_command.get(command, 0),
            stdout=self.stdout_by_command.get(command, b""),
            stderr=self.stderr_by_command.get(command, b""),
        )


def _write_exact(path: Path, raw: bytes, mode: int) -> None:
    if path.exists() or path.is_symlink():
        path.chmod(0o600)
    path.write_bytes(raw)
    path.chmod(mode)


def _signed(private_key: Ed25519PrivateKey, key_id: str, payload: dict) -> dict:
    raw = disposition.canonical_json_bytes(payload)
    return {
        "schema_version": "synthetic-signed-envelope-v1",
        "payload": payload,
        "signature": {
            "algorithm": "Ed25519",
            "key_id": key_id,
            "value_base64": base64.b64encode(private_key.sign(raw)).decode("ascii"),
        },
    }


class PreEffectFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.uid = os.geteuid()
        self.gid = os.getegid()
        self.state_root = root / "state"
        self.execution_root = self.state_root / "executions"
        self.execution = self.execution_root / EXECUTION_ID
        self.contract_root = self.state_root / "dispositions"
        self.receipt_root = self.state_root / "receipts"
        self.lock_root = root / "locks"
        for directory in (
            self.state_root,
            self.execution_root,
            self.execution,
            self.contract_root,
            self.receipt_root,
            self.lock_root,
        ):
            directory.mkdir(exist_ok=True)
            directory.chmod(0o700)
        self.journal = self.execution / "journal.jsonl"
        self.resources = self.execution / "resources.jsonl"
        _write_exact(self.journal, b"", 0o600)
        _write_exact(self.resources, b"", 0o600)

        self.capsule_path = self.state_root / "old-recovery-capsule.json"
        self.state_path = self.state_root / "authority-state.sqlite3"
        self.contract_path = self.contract_root / "failed-attempt.contract.json"
        self.receipt_path = self.receipt_root / "failed-attempt.receipt.json"
        self.global_lock_path = self.lock_root / "execution.lock"
        self.live_guard_path = self.lock_root / "old-live-proof.lock"
        self.successor_state_path = self.state_root / "authority-state-v2.sqlite3"
        self.successor_capsule_path = self.state_root / "recovery-capsule-000002.json"
        self.old_host_path = root / "old-store-000001"
        self.new_host_path = root / "new-store-000002"

        self.private_key = Ed25519PrivateKey.generate()
        public_raw = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        self.public_key_base64 = base64.b64encode(public_raw).decode("ascii")
        self.key_id = hashlib.sha256(public_raw).hexdigest()
        self.install_nonce = "install-nonce-" + "1" * 40
        self.reservation_nonce = "reservation-nonce-" + "2" * 40
        self.rollback_nonce = "rollback-nonce-" + "3" * 40
        self.capsule = self._build_capsule()
        self._write_capsule()
        self.state = self._build_state()
        self.paths = disposition.DispositionPaths(
            contract_path=self.contract_path,
            receipt_path=self.receipt_path,
            predecessor_capsule_path=self.capsule_path,
            predecessor_authority_state_path=self.state_path,
            predecessor_execution_directory=self.execution,
            global_lock_path=self.global_lock_path,
            live_guard_path=self.live_guard_path,
            expected_uid=self.uid,
            expected_gid=self.gid,
        )
        self.contract = self._build_contract()
        self.expectation = self._write_contract()

    def _build_capsule(self) -> dict:
        install_scope = {"exact": "synthetic-predecessor"}
        install_payload = {
            "authorization_namespace": "phase9-old-proof-v1",
            "thread_id": "thread-000001",
            "scope_id": "install-scope-000001",
            "scope_sha256": hashlib.sha256(
                disposition.canonical_json_bytes(install_scope)
            ).hexdigest(),
            "key_id": self.key_id,
            "nonce": self.install_nonce,
            "issued_at": "2026-08-12T10:00:00Z",
            "not_before": "2026-08-12T10:00:00Z",
            "expires_at": "2026-08-12T10:15:00Z",
            "single_use": True,
        }
        rollback_payload = {
            "authorization_namespace": "phase9-old-proof-v1",
            "thread_id": "thread-000001",
            "install_scope_id": "install-scope-000001",
            "rollback_scope_id": "rollback-scope-000001",
            "authorization_text_sha256": AUTHORIZATION_TEXT_SHA256,
            "installation_execution_id": EXECUTION_ID,
            "key_id": self.key_id,
            "rollback_nonce": self.rollback_nonce,
            "recovery_reservation_nonce": self.reservation_nonce,
            "issued_at": "2026-08-12T10:00:00Z",
            "not_before": "2026-08-12T10:00:00Z",
            "expires_at": "2026-08-12T10:15:00Z",
            "single_use": True,
            "empty_only": True,
            "source_postgres_read_count": 0,
            "production_data_read": False,
            "provider_calls": 0,
            "activation_allowed": False,
        }
        return {
            "schema_version": "synthetic-old-recovery-capsule-v1",
            "thread_id": "thread-000001",
            "authorization_text_sha256": AUTHORIZATION_TEXT_SHA256,
            "authorization_namespace": "phase9-old-proof-v1",
            "install_scope_id": "install-scope-000001",
            "rollback_scope_id": "rollback-scope-000001",
            "candidate_git_commit": "1" * 40,
            "candidate_git_tree": "2" * 40,
            "package_manifest_sha256": HASH_A,
            "controller_runtime_receipt_sha256": HASH_B,
            "public_key_base64": self.public_key_base64,
            "key_id": self.key_id,
            "issued_at": "2026-08-12T10:00:00Z",
            "not_before": "2026-08-12T10:00:00Z",
            "expires_at": "2026-08-12T10:15:00Z",
            "single_use": True,
            "install_scope": install_scope,
            "install_authorization": _signed(
                self.private_key, self.key_id, install_payload
            ),
            "trust_bundle": {
                "schema_version": "synthetic-trust-bundle-v1",
                "authorization_namespace": "phase9-old-proof-v1",
                "keys": [
                    {
                        "key_id": self.key_id,
                        "algorithm": "Ed25519",
                        "public_key_base64": self.public_key_base64,
                    }
                ],
            },
            "rollback_delegation": _signed(
                self.private_key, self.key_id, rollback_payload
            ),
        }

    def _write_capsule(self) -> None:
        _write_exact(
            self.capsule_path,
            disposition.canonical_json_bytes(self.capsule),
            0o400,
        )

    def _build_state(self) -> AuthorityState:
        state = AuthorityState(self.state_path, expected_uid=self.uid, create=True)
        with GlobalExecutionLock(
            self.global_lock_path,
            expected_uid=self.uid,
            expected_gid=self.gid,
        ) as lock:
            held = lock.held_capability()
            state.claim_nonce(
                self.install_nonce,
                operation="dormant_install",
                execution_sha256=HASH_A,
                authorization_sha256=HASH_B,
                scope_sha256=HASH_C,
                trust_bundle_sha256=HASH_D,
                held_lock=held,
            )
            state.claim_nonce(
                self.reservation_nonce,
                operation="empty_store_rollback_recovery_reservation",
                execution_sha256=HASH_B,
                authorization_sha256=HASH_C,
                scope_sha256=HASH_D,
                trust_bundle_sha256=HASH_A,
                held_lock=held,
            )
            directory_fd = os.open(self.execution, os.O_RDONLY)
            try:
                for kind, target, binding in (
                    ("journal", self.journal, JOURNAL_BINDING),
                    ("resource_ledger", self.resources, RESOURCE_LEDGER_BINDING),
                ):
                    file_fd = os.open(target, os.O_RDONLY)
                    try:
                        state.seal_filesystem_identity(
                            binding,
                            artifact_kind=kind,
                            path=target,
                            directory_fd=directory_fd,
                            file_fd=file_fd,
                            held_lock=held,
                        )
                    finally:
                        os.close(file_fd)
            finally:
                os.close(directory_fd)
        return state

    def _state_snapshot(self) -> dict:
        connection = sqlite3.connect(
            self.state_path.as_uri() + "?mode=ro&immutable=1",
            uri=True,
            isolation_level=None,
        )
        try:
            schema = disposition._schema_rows(connection)
            claims = disposition._dict_rows(
                connection, "nonce_claim_v1", disposition._NONCE_COLUMNS
            )
            seals = disposition._dict_rows(
                connection, "filesystem_identity_seal_v1", disposition._SEAL_COLUMNS
            )
            identities = disposition._dict_rows(
                connection,
                "authority_state_identity_v1",
                disposition._STATE_IDENTITY_COLUMNS,
            )
        finally:
            connection.close()
        return {
            "path": str(self.state_path),
            "sha256": hashlib.sha256(self.state_path.read_bytes()).hexdigest(),
            "application_id": disposition.STATE_APPLICATION_ID,
            "user_version": disposition.STATE_SCHEMA_VERSION,
            "schema_sha256": hashlib.sha256(
                disposition.canonical_json_bytes(schema)
            ).hexdigest(),
            "nonce_claims": claims,
            "claim_roles": {
                "install": nonce_sha256(self.install_nonce),
                "recovery_reservation": nonce_sha256(self.reservation_nonce),
            },
            "rollback_nonce_sha256": nonce_sha256(self.rollback_nonce),
            "journal_anchors": [],
            "resource_ledger_anchors": [],
            "filesystem_identity_seals": seals,
            "state_identity": identities[0],
            "database_sidecars": [],
        }

    def _execution_snapshot(self) -> dict:
        members = []
        for kind, target in (
            ("journal", self.journal),
            ("resource_ledger", self.resources),
        ):
            observed = target.stat(follow_symlinks=False)
            members.append(
                {
                    "artifact_kind": kind,
                    "name": target.name,
                    "sha256": disposition.EMPTY_SHA256,
                    "size": 0,
                    "mode": 0o600,
                    "device": observed.st_dev,
                    "inode": observed.st_ino,
                }
            )
        return {
            "path": str(self.execution),
            "execution_id": EXECUTION_ID,
            "journal_binding_sha256": JOURNAL_BINDING,
            "resource_ledger_binding_sha256": RESOURCE_LEDGER_BINDING,
            "members": members,
            "expected_absent_receipts": list(
                disposition._EXPECTED_ABSENT_RECEIPTS
            ),
        }

    def _build_contract(self) -> dict:
        capsule_raw = disposition.canonical_json_bytes(self.capsule)
        install_payload = self.capsule["install_authorization"]["payload"]
        rollback_payload = self.capsule["rollback_delegation"]["payload"]
        state_snapshot = self._state_snapshot()
        execution_snapshot = self._execution_snapshot()
        scalar = {
            key: value
            for key, value in self.capsule.items()
            if type(value) in {str, int, bool} or value is None
        }
        physical_resources = sorted(
            [
                {
                    "kind": "path",
                    "identity": str(self.new_host_path),
                    "generation": "000002",
                },
                *[
                    {
                        "kind": "tcp_listener",
                        "identity": identity,
                        "generation": "000002",
                    }
                    for identity, (owner, _) in disposition._REQUIRED_TCP_LISTENERS.items()
                    if owner == "successor"
                ],
            ],
            key=lambda item: (item["kind"], item["identity"]),
        )
        host_resources = sorted(
            [
                {
                    "kind": "path",
                    "identity": str(self.old_host_path),
                    "owner": "predecessor",
                    "required_state": "absent",
                },
                {
                    "kind": "path",
                    "identity": str(self.new_host_path),
                    "owner": "successor",
                    "required_state": "absent",
                },
                *[
                    {
                        "kind": "tcp_listener",
                        "identity": identity,
                        "owner": owner,
                        "required_state": "absent",
                    }
                    for identity, (owner, _) in disposition._REQUIRED_TCP_LISTENERS.items()
                ],
            ],
            key=lambda item: (item["kind"], item["identity"]),
        )
        return {
            "schema_version": disposition.CONTRACT_SCHEMA,
            "disposition_id": "failed-attempt-000001-to-000002",
            "contract_path": str(self.contract_path),
            "receipt_path": str(self.receipt_path),
            "authorization": {
                "authorization_id": "pre-effect-disposition-000001",
                "thread_id": "thread-000001",
                "authorization_text_sha256": AUTHORIZATION_TEXT_SHA256,
                "authority_materialized_at": "2026-08-13T10:00:00Z",
            },
            "predecessor": {
                "attempt_identity_sha256": (
                    disposition.predecessor_attempt_identity_sha256(
                        generation="000001",
                        capsule_sha256=hashlib.sha256(capsule_raw).hexdigest(),
                        authority_state_sha256=state_snapshot["sha256"],
                        execution_id=execution_snapshot["execution_id"],
                        scalar_bindings=scalar,
                    )
                ),
                "generation": "000001",
                "capsule": {
                    "path": str(self.capsule_path),
                    "sha256": hashlib.sha256(capsule_raw).hexdigest(),
                    "scalar_bindings": scalar,
                    "install_authorization_payload_sha256": hashlib.sha256(
                        disposition.canonical_json_bytes(install_payload)
                    ).hexdigest(),
                    "rollback_delegation_payload_sha256": hashlib.sha256(
                        disposition.canonical_json_bytes(rollback_payload)
                    ).hexdigest(),
                },
                "authority_state": state_snapshot,
                "execution": execution_snapshot,
            },
            "successor": {
                "attempt_identity_sha256": HASH_B,
                "generation": "000002",
                "authority_state_path": str(self.successor_state_path),
                "capsule_path": str(self.successor_capsule_path),
                "authorization_namespace": "phase9-new-proof-v2",
                "install_scope_id": "install-scope-000002",
                "rollback_scope_id": "rollback-scope-000002",
                "physical_resources": physical_resources,
            },
            "host_resources": host_resources,
        }

    def _write_contract(self) -> disposition.ReviewedDispositionExpectation:
        raw = disposition.canonical_json_bytes(self.contract)
        _write_exact(self.contract_path, raw, 0o400)
        return disposition.ReviewedDispositionExpectation(
            contract_sha256=hashlib.sha256(raw).hexdigest(),
            disposition_id="failed-attempt-000001-to-000002",
            authorization_text_sha256=AUTHORIZATION_TEXT_SHA256,
            predecessor_attempt_identity_sha256=self.contract["predecessor"][
                "attempt_identity_sha256"
            ],
            successor_attempt_identity_sha256=HASH_B,
            successor_generation="000002",
        )

    def refresh_contract(self) -> None:
        self.contract["predecessor"] = self._build_contract()["predecessor"]
        self.expectation = self._write_contract()


class ClosedHostCommandRunnerTests(unittest.TestCase):
    def test_transport_refuses_every_unreviewed_argv_without_execution(self) -> None:
        runner = disposition._ClosedHostCommandRunner()
        with mock.patch.object(disposition.subprocess, "run") as run:
            with self.assertRaisesRegex(
                OSError, "pre_effect_disposition_host_command_refused"
            ):
                runner.run(("/bin/true",))
        run.assert_not_called()


class PreEffectDispositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = PreEffectFixture(Path(self.temporary.name))
        self.host_runner = _AbsentHostRunner()
        self.runner_patch = mock.patch.object(
            disposition._ClosedHostCommandRunner,
            "run",
            side_effect=self.host_runner.run,
        )
        self.runner_patch.start()

    def tearDown(self) -> None:
        self.runner_patch.stop()
        self.temporary.cleanup()

    def test_success_preserves_evidence_is_idempotent_and_fences_old_entrypoint(self) -> None:
        capsule_before = self.fixture.capsule_path.read_bytes()
        state_before = self.fixture.state_path.read_bytes()
        journal_inode = self.fixture.journal.stat().st_ino
        first = disposition.execute_pre_effect_disposition(
            self.fixture.paths, self.fixture.expectation
        )
        second = disposition.execute_pre_effect_disposition(
            self.fixture.paths, self.fixture.expectation
        )
        self.assertEqual(dict(first), dict(second))
        self.assertEqual(first["result"], disposition.RESULT)
        self.assertTrue(first["predecessor_evidence_preserved_in_place"])
        self.assertFalse(first["predecessor_state_mutated"])
        self.assertEqual(self.fixture.capsule_path.read_bytes(), capsule_before)
        self.assertEqual(self.fixture.state_path.read_bytes(), state_before)
        self.assertEqual(self.fixture.journal.stat().st_ino, journal_inode)
        self.assertEqual(stat.S_IMODE(self.fixture.receipt_path.stat().st_mode), 0o400)
        with self.assertRaises(disposition.PredecessorRetiredError):
            disposition.refuse_retired_predecessor_entrypoint(
                self.fixture.paths, self.fixture.expectation
            )
        self.assertTrue(
            set(disposition._TCP_LISTENER_COMMANDS.values()).issubset(
                set(self.host_runner.calls)
            )
        )

    def test_receipt_complete_staging_is_reconciled_exactly(self) -> None:
        raw = b'{"receipt":"exact"}'
        staging = self.fixture.receipt_path.with_name(
            "." + self.fixture.receipt_path.name + ".publishing"
        )
        _write_exact(staging, raw, 0o400)
        staged_inode = staging.stat().st_ino
        disposition._write_create_once(
            self.fixture.receipt_path,
            raw,
            expected_uid=self.fixture.uid,
            expected_gid=self.fixture.gid,
        )
        self.assertFalse(staging.exists())
        self.assertEqual(self.fixture.receipt_path.read_bytes(), raw)
        self.assertEqual(self.fixture.receipt_path.stat().st_ino, staged_inode)
        self.assertEqual(self.fixture.receipt_path.stat().st_nlink, 1)

    def test_sigkill_partial_staging_and_foreign_final_are_untouched(self) -> None:
        raw = b'{"receipt":"exact"}'
        for kind in ("partial_staging", "foreign_staging", "foreign_final"):
            with self.subTest(kind=kind):
                path = self.fixture.receipt_path
                staging = path.with_name("." + path.name + ".publishing")
                selected = path if kind == "foreign_final" else staging
                selected_raw = raw[:7] if kind == "partial_staging" else b"foreign"
                _write_exact(selected, selected_raw, 0o400)
                inode = selected.stat().st_ino
                with self.assertRaises(disposition.PreEffectDispositionError):
                    disposition._write_create_once(
                        path,
                        raw,
                        expected_uid=self.fixture.uid,
                        expected_gid=self.fixture.gid,
                    )
                self.assertEqual(selected.read_bytes(), selected_raw)
                self.assertEqual(selected.stat().st_ino, inode)
                if kind != "foreign_final":
                    self.assertFalse(path.exists())
                    staging.unlink()
                else:
                    path.chmod(0o600)
                    path.unlink()

    def test_receipt_short_writes_complete_before_publication(self) -> None:
        raw = b'{"receipt":"exact-and-long-enough-for-short-writes"}'
        original_write = os.write
        calls = 0

        def short_write(descriptor: int, value: object) -> int:
            nonlocal calls
            calls += 1
            view = memoryview(value)
            return original_write(descriptor, view[: max(1, len(view) // 3)])

        with mock.patch.object(
            disposition.os, "write", side_effect=short_write
        ):
            disposition._write_create_once(
                self.fixture.receipt_path,
                raw,
                expected_uid=self.fixture.uid,
                expected_gid=self.fixture.gid,
            )
        self.assertGreater(calls, 1)
        self.assertEqual(self.fixture.receipt_path.read_bytes(), raw)

    def test_receipt_enospc_preserves_partial_private_staging(self) -> None:
        raw = b'{"receipt":"exact"}'
        original_write = os.write
        calls = 0

        def fail_after_prefix(descriptor: int, value: object) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return original_write(descriptor, memoryview(value)[:6])
            raise OSError(errno.ENOSPC, "full")

        with (
            mock.patch.object(
                disposition.os, "write", side_effect=fail_after_prefix
            ),
            self.assertRaises(disposition.PreEffectDispositionSecurityError),
        ):
            disposition._write_create_once(
                self.fixture.receipt_path,
                raw,
                expected_uid=self.fixture.uid,
                expected_gid=self.fixture.gid,
            )
        staging = self.fixture.receipt_path.with_name(
            "." + self.fixture.receipt_path.name + ".publishing"
        )
        self.assertEqual(staging.read_bytes(), raw[:6])
        self.assertFalse(self.fixture.receipt_path.exists())

    def test_receipt_fsync_failure_leaves_complete_reconcilable_staging(self) -> None:
        raw = b'{"receipt":"exact"}'
        original_fsync = os.fsync
        failed = False

        def fail_first(descriptor: int) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise OSError(errno.EIO, "fsync")
            original_fsync(descriptor)

        with mock.patch.object(
            disposition.os, "fsync", side_effect=fail_first
        ):
            with self.assertRaises(disposition.PreEffectDispositionSecurityError):
                disposition._write_create_once(
                    self.fixture.receipt_path,
                    raw,
                    expected_uid=self.fixture.uid,
                    expected_gid=self.fixture.gid,
                )
            staging = self.fixture.receipt_path.with_name(
                "." + self.fixture.receipt_path.name + ".publishing"
            )
            self.assertEqual(staging.read_bytes(), raw)
            self.assertFalse(self.fixture.receipt_path.exists())
        disposition._write_create_once(
            self.fixture.receipt_path,
            raw,
            expected_uid=self.fixture.uid,
            expected_gid=self.fixture.gid,
        )
        self.assertFalse(staging.exists())
        self.assertEqual(self.fixture.receipt_path.read_bytes(), raw)

    def test_receipt_close_failure_leaves_complete_reconcilable_staging(self) -> None:
        raw = b'{"receipt":"exact"}'
        original_close = os.close
        failed = False

        def fail_regular_once(descriptor: int) -> None:
            nonlocal failed
            metadata = os.fstat(descriptor)
            original_close(descriptor)
            if stat.S_ISREG(metadata.st_mode) and not failed:
                failed = True
                raise OSError(errno.EIO, "close")

        with mock.patch.object(
            disposition.os, "close", side_effect=fail_regular_once
        ):
            with self.assertRaises(disposition.PreEffectDispositionSecurityError):
                disposition._write_create_once(
                    self.fixture.receipt_path,
                    raw,
                    expected_uid=self.fixture.uid,
                    expected_gid=self.fixture.gid,
                )
            staging = self.fixture.receipt_path.with_name(
                "." + self.fixture.receipt_path.name + ".publishing"
            )
            self.assertEqual(staging.read_bytes(), raw)
            self.assertFalse(self.fixture.receipt_path.exists())
        disposition._write_create_once(
            self.fixture.receipt_path,
            raw,
            expected_uid=self.fixture.uid,
            expected_gid=self.fixture.gid,
        )
        self.assertFalse(staging.exists())
        self.assertEqual(self.fixture.receipt_path.read_bytes(), raw)

    def test_distinct_v5_execution_bindings_are_required_and_accepted(self) -> None:
        execution = self.fixture.contract["predecessor"]["execution"]
        self.assertNotEqual(
            execution["journal_binding_sha256"],
            execution["resource_ledger_binding_sha256"],
        )
        receipt = disposition.execute_pre_effect_disposition(
            self.fixture.paths, self.fixture.expectation
        )
        self.assertEqual(receipt["result"], disposition.RESULT)

    def test_resource_ledger_binding_mismatch_is_refused(self) -> None:
        execution = self.fixture.contract["predecessor"]["execution"]
        execution["resource_ledger_binding_sha256"] = JOURNAL_BINDING
        self.fixture.expectation = self.fixture._write_contract()
        with self.assertRaisesRegex(
            disposition.PreEffectDispositionIntegrityError,
            "pre_effect_disposition_filesystem_seals_invalid",
        ):
            disposition.execute_pre_effect_disposition(
                self.fixture.paths, self.fixture.expectation
            )

    def test_legacy_recovery_reservation_operation_is_refused(self) -> None:
        reservation_hash = nonce_sha256(self.fixture.reservation_nonce)
        connection = sqlite3.connect(self.fixture.state_path)
        try:
            columns = disposition._NONCE_COLUMNS[:-1]
            row = connection.execute(
                f"SELECT {', '.join(columns)} FROM nonce_claim_v1 "
                "WHERE nonce_sha256 = ?",
                (reservation_hash,),
            ).fetchone()
            self.assertIsNotNone(row)
            values = dict(zip(columns, row, strict=True))
            values["operation_sha256"] = operation_sha256(
                "reserve_empty_rollback_recovery"
            )
            material = b"\x00".join(
                str(values[column]).encode("ascii") for column in columns
            )
            claim_sha256 = hashlib.sha256(
                disposition._CLAIM_DOMAIN + material
            ).hexdigest()
            connection.execute(
                "UPDATE nonce_claim_v1 SET operation_sha256 = ?, "
                "claim_sha256 = ? WHERE nonce_sha256 = ?",
                (values["operation_sha256"], claim_sha256, reservation_hash),
            )
            connection.commit()
        finally:
            connection.close()
        self.fixture.refresh_contract()
        with self.assertRaisesRegex(
            disposition.PreEffectDispositionIntegrityError,
            "pre_effect_disposition_authority_claim_role_invalid",
        ):
            disposition.execute_pre_effect_disposition(
                self.fixture.paths, self.fixture.expectation
            )

    def test_exact_tombstone_remains_terminal_after_control_only_old_drift(self) -> None:
        expected = disposition.execute_pre_effect_disposition(
            self.fixture.paths, self.fixture.expectation
        )
        self.fixture.journal.chmod(0o600)
        self.fixture.journal.write_bytes(b"old-control-only-drift\n")
        self.fixture.journal.chmod(0o600)
        resumed = disposition.execute_pre_effect_disposition(
            self.fixture.paths, self.fixture.expectation
        )
        self.assertEqual(dict(resumed), dict(expected))

    def test_dangling_tombstone_symlink_fails_closed(self) -> None:
        self.fixture.receipt_path.symlink_to(self.fixture.root / "missing")
        with self.assertRaises(disposition.PreEffectDispositionSecurityError):
            disposition.refuse_retired_predecessor_entrypoint(
                self.fixture.paths, self.fixture.expectation
            )

    def test_nonempty_journal_is_refused_before_receipt(self) -> None:
        self.fixture.journal.write_bytes(b"unexpected")
        self.fixture.journal.chmod(0o600)
        with self.assertRaises(disposition.PreEffectDispositionError):
            disposition.execute_pre_effect_disposition(
                self.fixture.paths, self.fixture.expectation
            )
        self.assertFalse(self.fixture.receipt_path.exists())

    def test_extra_exactly_reviewed_claim_is_still_refused(self) -> None:
        with GlobalExecutionLock(
            self.fixture.global_lock_path,
            expected_uid=self.fixture.uid,
            expected_gid=self.fixture.gid,
        ) as lock:
            self.fixture.state.claim_nonce(
                "unexpected-claim-" + "4" * 40,
                operation="dormant_install",
                execution_sha256=HASH_C,
                authorization_sha256=HASH_D,
                scope_sha256=HASH_A,
                trust_bundle_sha256=HASH_B,
                held_lock=lock.held_capability(),
            )
        self.fixture.refresh_contract()
        with self.assertRaisesRegex(
            disposition.PreEffectDispositionIntegrityError,
            "pre_effect_disposition_authority_claim_set_invalid",
        ):
            disposition.execute_pre_effect_disposition(
                self.fixture.paths, self.fixture.expectation
            )
        self.assertFalse(self.fixture.receipt_path.exists())

    def test_reviewed_but_invalid_claim_hash_is_refused(self) -> None:
        connection = sqlite3.connect(self.fixture.state_path)
        try:
            connection.execute(
                "UPDATE nonce_claim_v1 SET claim_sha256 = ? "
                "WHERE nonce_sha256 = ?",
                ("0" * 64, nonce_sha256(self.fixture.install_nonce)),
            )
            connection.commit()
        finally:
            connection.close()
        self.fixture.refresh_contract()
        with self.assertRaisesRegex(
            disposition.PreEffectDispositionIntegrityError,
            "pre_effect_disposition_authority_claim_integrity_invalid",
        ):
            disposition.execute_pre_effect_disposition(
                self.fixture.paths, self.fixture.expectation
            )

    def test_capsule_signature_tamper_is_refused_even_when_hash_is_reviewed(self) -> None:
        signature = self.fixture.capsule["install_authorization"]["signature"]
        raw_signature = bytearray(base64.b64decode(signature["value_base64"]))
        raw_signature[0] ^= 1
        signature["value_base64"] = base64.b64encode(raw_signature).decode("ascii")
        self.fixture._write_capsule()
        self.fixture.refresh_contract()
        with self.assertRaisesRegex(
            disposition.PreEffectDispositionIntegrityError,
            "pre_effect_disposition_install_authorization_invalid",
        ):
            disposition.execute_pre_effect_disposition(
                self.fixture.paths, self.fixture.expectation
            )

    def test_present_host_resource_is_refused(self) -> None:
        self.fixture.new_host_path.mkdir()
        with self.assertRaises(disposition.PreEffectDispositionHostStateError):
            disposition.execute_pre_effect_disposition(
                self.fixture.paths, self.fixture.expectation
            )
        self.assertFalse(self.fixture.receipt_path.exists())

    def test_present_fixed_tcp_listener_is_refused(self) -> None:
        command = disposition._TCP_LISTENER_COMMANDS["127.0.0.1:55433@000002"]
        self.host_runner.stdout_by_command[command] = (
            b"LISTEN 0 4096 127.0.0.1:55433 0.0.0.0:*\n"
        )
        with self.assertRaisesRegex(
            disposition.PreEffectDispositionHostStateError,
            "pre_effect_disposition_host_resource_present",
        ):
            disposition.execute_pre_effect_disposition(
                self.fixture.paths, self.fixture.expectation
            )
        self.assertFalse(self.fixture.receipt_path.exists())

    def test_missing_fixed_tcp_listener_probe_is_refused(self) -> None:
        self.fixture.contract["host_resources"] = [
            item
            for item in self.fixture.contract["host_resources"]
            if not (
                item["kind"] == "tcp_listener"
                and item["identity"] == "127.0.0.1:6343@000001"
            )
        ]
        self.fixture.expectation = self.fixture._write_contract()
        with self.assertRaisesRegex(
            disposition.PreEffectDispositionIntegrityError,
            "pre_effect_disposition_host_contract_invalid",
        ):
            disposition.execute_pre_effect_disposition(
                self.fixture.paths, self.fixture.expectation
            )
        self.assertFalse(self.fixture.receipt_path.exists())

    def test_tcp_listener_probe_failure_is_fail_closed(self) -> None:
        command = disposition._TCP_LISTENER_COMMANDS["127.0.0.1:6343@000001"]
        self.host_runner.stderr_by_command[command] = b"permission denied\n"
        self.host_runner.returncode_by_command[command] = 1
        with self.assertRaisesRegex(
            disposition.PreEffectDispositionHostStateError,
            "pre_effect_disposition_host_absence_unproved",
        ):
            disposition.execute_pre_effect_disposition(
                self.fixture.paths, self.fixture.expectation
            )
        self.assertFalse(self.fixture.receipt_path.exists())

    def test_existing_different_receipt_is_never_replaced(self) -> None:
        wrong = disposition.canonical_json_bytes({"wrong": True})
        _write_exact(self.fixture.receipt_path, wrong, 0o400)
        with self.assertRaisesRegex(
            disposition.PreEffectDispositionIntegrityError,
            "pre_effect_disposition_receipt_binding_invalid",
        ):
            disposition.execute_pre_effect_disposition(
                self.fixture.paths, self.fixture.expectation
            )
        self.assertEqual(self.fixture.receipt_path.read_bytes(), wrong)

    def test_production_receipt_path_is_the_old_store_spec_slot(self) -> None:
        self.assertEqual(
            disposition.PRODUCTION_OLD_STORE_SPEC_PATH,
            Path("/etc/governed-memory-controller/store_spec.json"),
        )

    def test_production_wrapper_uses_fixed_paths_and_exact_successor_identity(self) -> None:
        bindings = {
            "package_manifest_sha256": "3" * 64,
            "controller_runtime_receipt_sha256": "4" * 64,
        }
        contract_sha256 = "5" * 64
        predecessor_identity = "6" * 64
        successor_identity = (
            disposition.production_successor_attempt_identity_sha256(
                **bindings
            )
        )
        receipt = {
            "schema_version": disposition.RECEIPT_SCHEMA,
            "result": disposition.RESULT,
            "contract_sha256": contract_sha256,
            "authorization_text_sha256": (
                disposition.PRODUCTION_AUTHORIZATION_TEXT_SHA256
            ),
            "predecessor_attempt_identity_sha256": predecessor_identity,
            "predecessor_execution_id": (
                disposition.PRODUCTION_PREDECESSOR_EXECUTION_ID
            ),
            "successor_attempt_identity_sha256": successor_identity,
            "successor_generation": (
                disposition.PRODUCTION_SUCCESSOR_GENERATION
            ),
            "successor_authority_state_path": str(
                disposition.PRODUCTION_SUCCESSOR_AUTHORITY_STATE_PATH
            ),
            "successor_capsule_path": str(
                disposition.PRODUCTION_SUCCESSOR_CAPSULE_PATH
            ),
            "successor_authorization_namespace": (
                disposition.PRODUCTION_SUCCESSOR_AUTHORIZATION_NAMESPACE
            ),
            "successor_install_scope_id": (
                disposition.PRODUCTION_SUCCESSOR_INSTALL_SCOPE_ID
            ),
            "successor_rollback_scope_id": (
                disposition.PRODUCTION_SUCCESSOR_ROLLBACK_SCOPE_ID
            ),
            "no_host_effects_proven": True,
            "predecessor_evidence_preserved_in_place": True,
            "predecessor_state_mutated": False,
            "deletion_performed": False,
            "provider_calls": 0,
            "production_data_read": False,
            "activation_performed": False,
        }
        with (
            mock.patch.multiple(
                disposition,
                PRODUCTION_CONTRACT_SHA256=contract_sha256,
                PRODUCTION_PREDECESSOR_ATTEMPT_IDENTITY_SHA256=(
                    predecessor_identity
                ),
            ),
            mock.patch.object(
                disposition,
                "verify_disposition_receipt",
                return_value=receipt,
            ) as verify,
        ):
            observed = disposition.require_production_disposition_receipt(
                **bindings,
            )
        self.assertIs(observed, receipt)
        paths, expectation = verify.call_args.args
        self.assertTrue(paths.production)
        self.assertEqual(paths.contract_path, disposition.PRODUCTION_CONTRACT_PATH)
        self.assertEqual(
            paths.receipt_path,
            Path("/etc/governed-memory-controller/store_spec.json"),
        )
        self.assertEqual(
            paths.predecessor_execution_directory.name,
            disposition.PRODUCTION_PREDECESSOR_EXECUTION_ID,
        )
        self.assertEqual(
            expectation.successor_attempt_identity_sha256,
            successor_identity,
        )

    def test_successor_identity_changes_with_each_pr_binding(self) -> None:
        bindings = {
            "package_manifest_sha256": "3" * 64,
            "controller_runtime_receipt_sha256": "4" * 64,
        }
        baseline = disposition.production_successor_attempt_identity_sha256(
            **bindings
        )
        replacements = {
            "package_manifest_sha256": "9" * 64,
            "controller_runtime_receipt_sha256": "a" * 64,
        }
        for key, replacement in replacements.items():
            with self.subTest(key=key):
                changed = dict(bindings)
                changed[key] = replacement
                self.assertNotEqual(
                    disposition.production_successor_attempt_identity_sha256(
                        **changed
                    ),
                    baseline,
                )

    def test_tracked_production_contract_is_exact_and_complete(self) -> None:
        contract_path = (
            Path(__file__).resolve().parents[2]
            / "ops/governed_memory/pre_effect_disposition_contract.json"
        )
        raw = contract_path.read_bytes()
        successor_identity = (
            disposition.production_successor_attempt_identity_sha256(
                package_manifest_sha256=(
                    "062ea00564e5edfb138dca9240fd5d70cec2c640563d6ad02e1e89de15d7db39"
                ),
                controller_runtime_receipt_sha256=(
                    "c9b6721985c4840f555d583d77fcfb82c4f20d609c0af651a7171744d58c9a11"
                ),
            )
        )
        expectation = disposition.ReviewedDispositionExpectation(
            contract_sha256=hashlib.sha256(raw).hexdigest(),
            disposition_id=disposition.PRODUCTION_DISPOSITION_ID,
            authorization_text_sha256=(
                disposition.PRODUCTION_AUTHORIZATION_TEXT_SHA256
            ),
            predecessor_attempt_identity_sha256=(
                disposition.PRODUCTION_PREDECESSOR_ATTEMPT_IDENTITY_SHA256
            ),
            successor_attempt_identity_sha256=successor_identity,
            successor_generation=disposition.PRODUCTION_SUCCESSOR_GENERATION,
        )
        document = disposition._verify_contract(
            raw,
            paths=disposition.production_disposition_paths(),
            expectation=expectation,
        )
        physical = {
            (item["kind"], item["identity"])
            for item in document["successor"]["physical_resources"]
        }
        probed = {
            (item["kind"], item["identity"])
            for item in document["host_resources"]
            if item["owner"] == "successor"
        }
        self.assertEqual(
            physical,
            probed
            - disposition._PRODUCTION_SUCCESSOR_TRANSIENT_REQUIRED_ABSENT_IDENTITIES,
        )
        self.assertEqual(len(physical), 16)
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            disposition.PRODUCTION_CONTRACT_SHA256,
        )

    def test_foreign_old_store_spec_tombstone_fails_old_i03_preflight(self) -> None:
        from tests.memory.test_linux_store_effects import LinuxStoreEffectsTests
        from tools.governed_memory_install.host_boundary import HostOperationProfile
        from tools.governed_memory_install.linux_store_effects import (
            BoundResourceSnapshot,
            EffectPresence,
        )

        old = LinuxStoreEffectsTests(
            methodName="test_closed_physical_driver_revalidates_revision_and_fixed_dispatch"
        )
        old.setUp()
        old.files.observe_resolved_store_spec = lambda: BoundResourceSnapshot(
            EffectPresence.DRIFT
        )
        request = old._request(
            "I03_VERIFY_LIVE_PREFLIGHT",
            HostOperationProfile.VERIFY_LIVE_PREFLIGHT,
        )
        self.assertEqual(old.operations.observe(request).state, "drift")

    def test_successor_resource_without_generation_is_refused(self) -> None:
        physical = self.fixture.contract["successor"]["physical_resources"][0]
        physical["identity"] = str(self.fixture.root / "new-store")
        self.fixture.expectation = self.fixture._write_contract()
        with self.assertRaisesRegex(
            disposition.PreEffectDispositionIntegrityError,
            "pre_effect_disposition_successor_resource_invalid",
        ):
            disposition.execute_pre_effect_disposition(
                self.fixture.paths, self.fixture.expectation
            )


if __name__ == "__main__":
    unittest.main()

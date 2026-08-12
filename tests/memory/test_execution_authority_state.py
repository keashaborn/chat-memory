from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools.governed_memory_install import authority as authority

from tools.governed_memory_install.authority_state import (
    AuthorityReplayError,
    AuthorityState,
    AuthorityStateSecurityError,
    JournalAnchorError,
    ZERO_HEAD,
)
from tools.governed_memory_install.execution_authority import (
    ExecutionAuthorityError,
    claim_execution_authority,
    read_trusted_utc,
)
from tools.governed_memory_install.execution_lock import (
    ExecutionLockBusyError,
    ExecutionLockSecurityError,
    GlobalExecutionLock,
)


BINDING = "1" * 64
EXECUTION = "2" * 64
AUTHORIZATION = "3" * 64
SCOPE = "4" * 64
TRUST_BUNDLE = "5" * 64
HEAD_ONE = "6" * 64
HEAD_TWO = "7" * 64
NONCE = "phase8b_nonce_000000000000000000000099"
NAMESPACE = "governed-memory.installation.phase8b.v1"
THREAD_ID = "019fe927-8367-7f52-86f2-e2b5b43a2390"
SCOPE_ID = "phase8b-fresh-stores-000001"


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value
        self.read_count = 0

    def read_utc(self) -> datetime:
        self.read_count += 1
        return self.value


class RaisingClock:
    def read_utc(self) -> datetime:
        raise RuntimeError("untrusted detail must not escape")


class SecureTemporaryDirectory:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.path = Path(self._temporary.name) / "authority"
        self.path.mkdir(mode=0o700)
        os.chmod(self.path, 0o700)

    def close(self) -> None:
        self._temporary.cleanup()

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *unused: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class ScopeEvidence:
    result_type: str
    operation: str
    nonce: str
    scope_sha256: str
    authorization_sha256: str
    trust_bundle_sha256: str
    not_before: str
    expires_at: str


def evidence() -> ScopeEvidence:
    return ScopeEvidence(
        result_type="cryptographically_valid_scope_not_execution",
        operation="dormant_install",
        nonce=NONCE,
        scope_sha256=SCOPE,
        authorization_sha256=AUTHORIZATION,
        trust_bundle_sha256=TRUST_BUNDLE,
        not_before="2026-08-11T12:00:00Z",
        expires_at="2026-08-11T12:10:00Z",
    )


def execution_capability() -> object:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = hashlib.sha256(public_key).hexdigest()
    scope = {
        "schema_version": "governed-memory-dormant-install-scope-v1",
        "phase": "8B",
        "operation": "dormant_install",
        "authorization_namespace": NAMESPACE,
        "thread_id": THREAD_ID,
        "scope_id": SCOPE_ID,
        "candidate_git_commit": "a" * 40,
        "candidate_git_tree": "b" * 40,
        "package_manifest_sha256": "c" * 64,
        "controller_contract_sha256": "d" * 64,
        "execution_plan_sha256": "e" * 64,
        "exact_targets_sha256": "f" * 64,
        "source_boundary": {
            "source_postgres_connection_count": 0,
            "source_postgres_read_count": 0,
            "source_postgres_write_count": 0,
            "source_preparation_phase": "separate_source_preparation_authorization_required",
        },
        "store_policy": {
            "postgresql": "fresh_isolated_empty",
            "qdrant": "fresh_isolated_empty",
            "legacy_imports_allowed": False,
            "snapshot_restore_allowed": False,
            "unprocessed_prefill_allowed": False,
            "initial_postgresql_user_row_count": 0,
            "initial_qdrant_point_count": 0,
        },
        "secret_policy": {
            "allowed_secret_names": [
                "GOVERNED_MEMORY_BOOTSTRAP_PASSWORD",
                "GOVERNED_MEMORY_QDRANT_API_KEY",
            ],
            "forbidden_secret_classes": [
                "pilot",
                "provider",
                "runtime",
                "service",
                "supabase",
            ],
            "runtime_secret_count": 0,
            "provider_secret_count": 0,
            "supabase_secret_count": 0,
            "service_secret_count": 0,
            "pilot_secret_count": 0,
            "secret_values_present": False,
        },
    }
    scope_sha256 = authority.canonical_json_sha256(scope)
    payload = {
        "schema_version": "governed-memory-dormant-install-authorization-v1",
        "authorization_id": "phase8b-auth-000099",
        "authorization_namespace": NAMESPACE,
        "thread_id": THREAD_ID,
        "scope_id": SCOPE_ID,
        "scope_sha256": scope_sha256,
        "key_id": key_id,
        "approval_phrase": f"APPROVE PHASE 8B DORMANT INSTALL {scope_sha256}",
        "nonce": NONCE,
        "issued_at": "2026-08-11T12:00:00Z",
        "not_before": "2026-08-11T12:00:00Z",
        "expires_at": "2026-08-11T12:10:00Z",
        "single_use": True,
    }
    envelope = {
        "schema_version": "governed-memory-external-authorization-envelope-v1",
        "payload": payload,
        "signature": {
            "algorithm": "Ed25519",
            "key_id": key_id,
            "value_base64": base64.b64encode(
                private_key.sign(authority.canonical_json_bytes(payload))
            ).decode("ascii"),
        },
    }
    bundle = {
        "schema_version": "governed-memory-ed25519-public-key-trust-bundle-v1",
        "authorization_namespace": NAMESPACE,
        "keys": [
            {
                "key_id": key_id,
                "algorithm": "Ed25519",
                "public_key_base64": base64.b64encode(public_key).decode("ascii"),
            }
        ],
    }
    return authority.verify_dormant_install_execution_capability(
        authority.canonical_json_bytes(scope),
        authority.canonical_json_bytes(envelope),
        authority.canonical_json_bytes(bundle),
        expected_namespace=NAMESPACE,
        expected_thread_id=THREAD_ID,
        expected_scope_id=SCOPE_ID,
        expected_key_id=key_id,
        expected_trust_bundle_sha256=authority.canonical_json_sha256(bundle),
        expected_bindings=authority.DormantInstallExpectedBindings(
            candidate_git_commit="a" * 40,
            candidate_git_tree="b" * 40,
            package_manifest_sha256="c" * 64,
            controller_contract_sha256="d" * 64,
            execution_plan_sha256="e" * 64,
            exact_targets_sha256="f" * 64,
        ),
    )


class AuthorityStateTests(unittest.TestCase):
    def test_nonce_claim_is_atomic_under_thread_race_and_content_free(self) -> None:
        with SecureTemporaryDirectory() as directory:
            database = directory / "authority.sqlite3"
            state = AuthorityState(database, create=True)
            worker_count = 8
            barrier = threading.Barrier(worker_count)

            def claim() -> str:
                barrier.wait()
                return state.claim_nonce(
                    NONCE,
                    operation="dormant_install",
                    execution_sha256=EXECUTION,
                    authorization_sha256=AUTHORIZATION,
                    scope_sha256=SCOPE,
                    trust_bundle_sha256=TRUST_BUNDLE,
                ).result

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                results = list(
                    executor.map(lambda _index: claim(), range(worker_count))
                )

            self.assertEqual(results.count("nonce_claimed"), 1)
            self.assertEqual(results.count("exact_execution_resumed"), 7)
            self.assertNotIn(NONCE.encode("utf-8"), database.read_bytes())
            self.assertFalse(Path(str(database) + "-wal").exists())
            self.assertFalse(Path(str(database) + "-shm").exists())

    def test_replay_and_cross_operation_are_refused_but_exact_resume_is_allowed(
        self,
    ) -> None:
        with SecureTemporaryDirectory() as directory:
            state = AuthorityState(
                directory / "authority.sqlite3",
                create=True,
            )
            claimed = state.claim_nonce(
                NONCE,
                operation="dormant_install",
                execution_sha256=EXECUTION,
                authorization_sha256=AUTHORIZATION,
                scope_sha256=SCOPE,
                trust_bundle_sha256=TRUST_BUNDLE,
            )
            reopened = AuthorityState(directory / "authority.sqlite3")
            resumed = reopened.claim_nonce(
                NONCE,
                operation="dormant_install",
                execution_sha256=EXECUTION,
                authorization_sha256=AUTHORIZATION,
                scope_sha256=SCOPE,
                trust_bundle_sha256=TRUST_BUNDLE,
            )
            self.assertEqual(claimed.result, "nonce_claimed")
            self.assertEqual(resumed.result, "exact_execution_resumed")
            self.assertEqual(claimed.claim_sha256, resumed.claim_sha256)

            cases = (
                {"operation": "rollback"},
                {"execution_sha256": "9" * 64},
                {"authorization_sha256": "a" * 64},
                {"scope_sha256": "b" * 64},
                {"trust_bundle_sha256": "e" * 64},
            )
            baseline = {
                "operation": "dormant_install",
                "execution_sha256": EXECUTION,
                "authorization_sha256": AUTHORIZATION,
                "scope_sha256": SCOPE,
                "trust_bundle_sha256": TRUST_BUNDLE,
            }
            for replacement in cases:
                arguments = {**baseline, **replacement}
                with self.subTest(replacement=replacement):
                    with self.assertRaisesRegex(
                        AuthorityReplayError,
                        "authority_nonce_replayed",
                    ):
                        state.claim_nonce(NONCE, **arguments)

    def test_state_creation_is_explicit_and_existing_state_is_not_recreated(
        self,
    ) -> None:
        with SecureTemporaryDirectory() as directory:
            database = directory / "authority.sqlite3"
            with self.assertRaises(AuthorityStateSecurityError):
                AuthorityState(database)
            AuthorityState(database, create=True)
            AuthorityState(database)
            database.unlink()
            with self.assertRaises(AuthorityStateSecurityError):
                AuthorityState(database)

    def test_anchor_cas_allows_only_exact_one_entry_recovery(self) -> None:
        with SecureTemporaryDirectory() as directory:
            state = AuthorityState(
                directory / "authority.sqlite3",
                create=True,
            )
            empty = state.read_anchor(BINDING)
            self.assertEqual((empty.sequence, empty.head_sha256), (0, ZERO_HEAD))

            first = state.advance_anchor(
                BINDING,
                journal_sequence=1,
                journal_head_sha256=HEAD_ONE,
                journal_prior_head_sha256=ZERO_HEAD,
            )
            self.assertEqual(first.result, "anchor_advanced_one_entry")
            exact = state.advance_anchor(
                BINDING,
                journal_sequence=1,
                journal_head_sha256=HEAD_ONE,
                journal_prior_head_sha256=ZERO_HEAD,
            )
            self.assertEqual(exact.result, "anchor_exact_resume")

            second = state.advance_anchor(
                BINDING,
                journal_sequence=2,
                journal_head_sha256=HEAD_TWO,
                journal_prior_head_sha256=HEAD_ONE,
            )
            self.assertEqual(second.sequence, 2)
            self.assertEqual(state.read_anchor(BINDING).head_sha256, HEAD_TWO)

            with self.assertRaisesRegex(
                JournalAnchorError,
                "journal_anchor_ahead_of_journal",
            ):
                state.advance_anchor(
                    BINDING,
                    journal_sequence=1,
                    journal_head_sha256=HEAD_ONE,
                    journal_prior_head_sha256=ZERO_HEAD,
                )

            unanchored = "c" * 64
            with self.assertRaisesRegex(
                JournalAnchorError,
                "journal_anchor_suffix_too_long",
            ):
                state.advance_anchor(
                    unanchored,
                    journal_sequence=2,
                    journal_head_sha256=HEAD_TWO,
                    journal_prior_head_sha256=HEAD_ONE,
                )

            mismatch = "d" * 64
            with self.assertRaisesRegex(
                JournalAnchorError,
                "journal_anchor_compare_failed",
            ):
                state.advance_anchor(
                    mismatch,
                    journal_sequence=1,
                    journal_head_sha256=HEAD_ONE,
                    journal_prior_head_sha256=HEAD_TWO,
                )


class TrustedClockAndExecutionAuthorityTests(unittest.TestCase):
    def test_clock_is_read_once_and_must_be_exact_utc(self) -> None:
        valid = FixedClock(datetime(2026, 8, 11, 12, 5, tzinfo=timezone.utc))
        reading = read_trusted_utc(valid)
        self.assertEqual(valid.read_count, 1)
        self.assertEqual(reading.observed_at_utc, "2026-08-11T12:05:00.000000Z")

        invalid = (
            FixedClock(datetime(2026, 8, 11, 12, 5)),
            FixedClock(
                datetime(
                    2026,
                    8,
                    11,
                    12,
                    5,
                    tzinfo=timezone(timedelta(0), "UTC-like"),
                )
            ),
            FixedClock(datetime(2200, 1, 1, tzinfo=timezone.utc)),
            RaisingClock(),
        )
        for clock in invalid:
            with self.subTest(clock=type(clock).__name__):
                with self.assertRaises(ExecutionAuthorityError):
                    read_trusted_utc(clock)

    def test_new_claim_requires_window_but_exact_execution_can_resume_after_expiry(
        self,
    ) -> None:
        with SecureTemporaryDirectory() as directory:
            state = AuthorityState(
                directory / "authority.sqlite3",
                create=True,
            )
            with GlobalExecutionLock(directory / "execution.lock") as lock:
                held = lock.held_capability()
                valid_clock = FixedClock(
                    datetime(2026, 8, 11, 12, 5, tzinfo=timezone.utc)
                )
                permit = claim_execution_authority(
                    execution_capability(),
                    state=state,
                    clock=valid_clock,
                    held_lock=held,
                    expected_operation="dormant_install",
                )
                self.assertEqual(permit.result, "execution_authority_claimed")
                self.assertNotIn(NONCE, repr(permit))

                expired_clock = FixedClock(
                    datetime(2026, 8, 11, 12, 20, tzinfo=timezone.utc)
                )
                resumed = claim_execution_authority(
                    execution_capability(),
                    state=state,
                    clock=expired_clock,
                    held_lock=held,
                    expected_operation="dormant_install",
                )
                self.assertEqual(
                    resumed.result, "execution_authority_exact_resume"
                )

                other_state = AuthorityState(
                    directory / "other.sqlite3",
                    create=True,
                )
                with self.assertRaisesRegex(
                    ExecutionAuthorityError,
                    "execution_authority_expired",
                ):
                    claim_execution_authority(
                        execution_capability(),
                        state=other_state,
                        clock=expired_clock,
                        held_lock=held,
                        expected_operation="dormant_install",
                    )

                backwards = FixedClock(
                    datetime(2026, 8, 11, 11, 59, tzinfo=timezone.utc)
                )
                with self.assertRaisesRegex(
                    ExecutionAuthorityError,
                    "execution_authority_not_yet_valid",
                ):
                    claim_execution_authority(
                        execution_capability(),
                        state=state,
                        clock=backwards,
                        held_lock=held,
                        expected_operation="dormant_install",
                    )

    def test_structural_evidence_and_closed_lock_cannot_claim_nonce(self) -> None:
        with SecureTemporaryDirectory() as directory:
            state = AuthorityState(
                directory / "authority.sqlite3",
                create=True,
            )
            clock = FixedClock(
                datetime(2026, 8, 11, 12, 5, tzinfo=timezone.utc)
            )
            lock = GlobalExecutionLock(directory / "execution.lock")
            held = lock.held_capability()
            with self.assertRaisesRegex(
                ExecutionAuthorityError,
                "execution_authority_evidence_invalid",
            ):
                claim_execution_authority(
                    evidence(),
                    state=state,
                    clock=clock,
                    held_lock=held,
                    expected_operation="dormant_install",
                )
            self.assertEqual(state.read_anchor(BINDING).result, "anchor_empty")

            capability = execution_capability()
            lock.close()
            with self.assertRaisesRegex(
                ExecutionAuthorityError,
                "execution_authority_lock_not_held",
            ):
                claim_execution_authority(
                    capability,
                    state=state,
                    clock=clock,
                    held_lock=held,
                    expected_operation="dormant_install",
                )


class GlobalExecutionLockTests(unittest.TestCase):
    def test_lock_contention_is_nonblocking(self) -> None:
        with SecureTemporaryDirectory() as directory:
            path = directory / "execution.lock"
            with GlobalExecutionLock(path) as first:
                first.validate()
                with self.assertRaisesRegex(
                    ExecutionLockBusyError,
                    "execution_lock_busy",
                ):
                    GlobalExecutionLock(path)
            with GlobalExecutionLock(path) as reacquired:
                reacquired.validate()

    def test_symlink_file_mode_and_directory_mode_are_refused(self) -> None:
        with SecureTemporaryDirectory() as directory:
            target = directory / "target"
            target.touch(mode=0o600)
            symlink = directory / "symlink.lock"
            symlink.symlink_to(target)
            with self.assertRaises(ExecutionLockSecurityError):
                GlobalExecutionLock(symlink)

            wrong_mode = directory / "wrong-mode.lock"
            wrong_mode.touch(mode=0o600)
            wrong_mode.chmod(0o640)
            with self.assertRaisesRegex(
                ExecutionLockSecurityError,
                "execution_lock_file_invalid",
            ):
                GlobalExecutionLock(wrong_mode)

            directory.chmod(0o750)
            try:
                with self.assertRaisesRegex(
                    ExecutionLockSecurityError,
                    "execution_lock_directory_invalid",
                ):
                    GlobalExecutionLock(directory / "directory-mode.lock")
            finally:
                directory.chmod(0o700)

    def test_named_inode_replacement_is_detected(self) -> None:
        with SecureTemporaryDirectory() as directory:
            path = directory / "execution.lock"
            with GlobalExecutionLock(path) as lock:
                path.unlink()
                path.touch(mode=0o600)
                with self.assertRaisesRegex(
                    ExecutionLockSecurityError,
                    "execution_lock_file_invalid",
                ):
                    lock.validate()


if __name__ == "__main__":
    unittest.main()

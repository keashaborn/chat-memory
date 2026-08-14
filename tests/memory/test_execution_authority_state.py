from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools.governed_memory_install import authority as authority

from tools.governed_memory_install.authority_state import (
    AuthorityClaimNotAllowedError,
    AuthorityReplayError,
    AuthorityState,
    AuthorityStateIntegrityError,
    AuthorityStateSecurityError,
    JournalAnchorError,
    NonceClaimIdentity,
    ZERO_HEAD,
    derive_nonce_claim_identity,
)
from tools.governed_memory_install.execution_authority import (
    ExecutionAuthorityError,
    claim_execution_authority,
    read_trusted_utc,
    resume_execution_authority,
)
from tools.governed_memory_install.execution_lock import (
    ExecutionLockError,
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
NONCE = "dormant_store_install_nonce_000000000000000000000099"
SECOND_NONCE = "empty_rollback_nonce_0000000000000000000000000100"
NAMESPACE = "governed-memory.installation.dormant_store_install.v1"
THREAD_ID = "019fe927-8367-7f52-86f2-e2b5b43a2390"
SCOPE_ID = "dormant_store_install-fresh-stores-000001"


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


def exact_nonce_pair() -> tuple[NonceClaimIdentity, NonceClaimIdentity]:
    first = derive_nonce_claim_identity(
        NONCE,
        operation="dormant_install",
        execution_sha256=EXECUTION,
        authorization_sha256=AUTHORIZATION,
        scope_sha256=SCOPE,
        trust_bundle_sha256=TRUST_BUNDLE,
    )
    second = derive_nonce_claim_identity(
        SECOND_NONCE,
        operation="empty_rollback",
        execution_sha256="8" * 64,
        authorization_sha256="9" * 64,
        scope_sha256="a" * 64,
        trust_bundle_sha256="b" * 64,
    )
    return first, second


def execution_capability() -> object:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = hashlib.sha256(public_key).hexdigest()
    scope = {
        "schema_version": "governed-memory-dormant-install-scope-v2",
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
        "controller_runtime_receipt_sha256": "9" * 64,
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
        "authorization_id": "dormant_store_install-auth-000099",
        "authorization_namespace": NAMESPACE,
        "thread_id": THREAD_ID,
        "scope_id": SCOPE_ID,
        "scope_sha256": scope_sha256,
        "key_id": key_id,
        "approval_phrase": (
            f"APPROVE GOVERNED MEMORY DORMANT STORE INSTALL {scope_sha256}"
        ),
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
            controller_runtime_receipt_sha256="9" * 64,
        ),
    )


class AuthorityStateTests(unittest.TestCase):
    def test_nonce_claim_is_atomic_under_thread_race_and_content_free(self) -> None:
        with SecureTemporaryDirectory() as directory:
            database = directory / "authority.sqlite3"
            state = AuthorityState(database, create=True)
            worker_count = 8
            barrier = threading.Barrier(worker_count)
            with GlobalExecutionLock(directory / "execution.lock") as lock:
                held = lock.held_capability()

                def claim() -> str:
                    barrier.wait()
                    return state.claim_nonce(
                        NONCE,
                        operation="dormant_install",
                        execution_sha256=EXECUTION,
                        authorization_sha256=AUTHORIZATION,
                        scope_sha256=SCOPE,
                        trust_bundle_sha256=TRUST_BUNDLE,
                        held_lock=held,
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
            with GlobalExecutionLock(directory / "execution.lock") as lock:
                held = lock.held_capability()
                claimed = state.claim_nonce(
                    NONCE,
                    operation="dormant_install",
                    execution_sha256=EXECUTION,
                    authorization_sha256=AUTHORIZATION,
                    scope_sha256=SCOPE,
                    trust_bundle_sha256=TRUST_BUNDLE,
                    held_lock=held,
                )
                reopened = AuthorityState(directory / "authority.sqlite3")
                resumed = reopened.claim_nonce(
                    NONCE,
                    operation="dormant_install",
                    execution_sha256=EXECUTION,
                    authorization_sha256=AUTHORIZATION,
                    scope_sha256=SCOPE,
                    trust_bundle_sha256=TRUST_BUNDLE,
                    held_lock=held,
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
                    "held_lock": held,
                }
                for replacement in cases:
                    arguments = {**baseline, **replacement}
                    with self.subTest(replacement=replacement):
                        with self.assertRaisesRegex(
                            AuthorityReplayError,
                            "authority_nonce_replayed",
                        ):
                            state.claim_nonce(NONCE, **arguments)

    def test_exact_nonce_pair_is_one_transaction_and_atomically_visible(self) -> None:
        with SecureTemporaryDirectory() as directory:
            database = directory / "authority.sqlite3"
            state = AuthorityState(database, create=True)
            first, second = exact_nonce_pair()
            with GlobalExecutionLock(directory / "execution.lock") as lock:
                held = lock.held_capability()
                with (
                    mock.patch.object(
                        state,
                        "_begin_immediate",
                        wraps=state._begin_immediate,
                    ) as begin,
                    mock.patch.object(
                        state,
                        "_validate_path",
                        wraps=state._validate_path,
                    ) as validate_path,
                ):
                    claimed = state.claim_exact_nonce_pair(
                        first,
                        second,
                        held_lock=held,
                        allow_new_pair=True,
                    )
                self.assertEqual(begin.call_count, 1)
                self.assertGreaterEqual(validate_path.call_count, 2)
                self.assertTrue(
                    state.inspect_nonce_claim(NONCE, held_lock=held).present
                )
                self.assertTrue(
                    state.inspect_nonce_claim(
                        SECOND_NONCE, held_lock=held
                    ).present
                )
            self.assertEqual(claimed.result, "nonce_claimed")
            self.assertEqual(claimed.first.result, "nonce_claimed")
            self.assertEqual(claimed.second.result, "nonce_claimed")
            self.assertEqual(claimed.first.claim_sha256, first.claim_sha256)
            self.assertEqual(claimed.second.claim_sha256, second.claim_sha256)
            raw = database.read_bytes()
            self.assertNotIn(NONCE.encode("utf-8"), raw)
            self.assertNotIn(SECOND_NONCE.encode("utf-8"), raw)

    def test_exact_nonce_pair_is_atomic_under_thread_race(self) -> None:
        with SecureTemporaryDirectory() as directory:
            state = AuthorityState(directory / "authority.sqlite3", create=True)
            first, second = exact_nonce_pair()
            worker_count = 6
            barrier = threading.Barrier(worker_count)
            with GlobalExecutionLock(directory / "execution.lock") as lock:
                held = lock.held_capability()

                def claim() -> str:
                    barrier.wait()
                    return state.claim_exact_nonce_pair(
                        first,
                        second,
                        held_lock=held,
                        allow_new_pair=True,
                    ).result

                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    results = list(
                        executor.map(lambda _index: claim(), range(worker_count))
                    )
            self.assertEqual(results.count("nonce_claimed"), 1)
            self.assertEqual(
                results.count("exact_execution_resumed"), worker_count - 1
            )

    def test_exact_nonce_pair_replay_and_expiry_policy_remain_caller_controlled(
        self,
    ) -> None:
        with SecureTemporaryDirectory() as directory:
            state = AuthorityState(directory / "authority.sqlite3", create=True)
            first, second = exact_nonce_pair()
            with GlobalExecutionLock(directory / "execution.lock") as lock:
                held = lock.held_capability()
                with self.assertRaisesRegex(
                    AuthorityClaimNotAllowedError,
                    "authority_new_pair_claim_not_allowed",
                ):
                    state.claim_exact_nonce_pair(
                        first,
                        second,
                        held_lock=held,
                        allow_new_pair=False,
                    )
                self.assertFalse(
                    state.inspect_nonce_claim(NONCE, held_lock=held).present
                )
                self.assertFalse(
                    state.inspect_nonce_claim(
                        SECOND_NONCE, held_lock=held
                    ).present
                )
                state.claim_exact_nonce_pair(
                    first,
                    second,
                    held_lock=held,
                    allow_new_pair=True,
                )
                resumed = state.claim_exact_nonce_pair(
                    first,
                    second,
                    held_lock=held,
                    allow_new_pair=False,
                )
            self.assertEqual(resumed.result, "exact_execution_resumed")
            self.assertEqual(resumed.first.result, "exact_execution_resumed")
            self.assertEqual(resumed.second.result, "exact_execution_resumed")

    def test_exact_nonce_pair_refuses_mixed_durable_state_without_second_claim(
        self,
    ) -> None:
        with SecureTemporaryDirectory() as directory:
            state = AuthorityState(directory / "authority.sqlite3", create=True)
            first, second = exact_nonce_pair()
            with GlobalExecutionLock(directory / "execution.lock") as lock:
                held = lock.held_capability()
                state.claim_nonce(
                    NONCE,
                    operation="dormant_install",
                    execution_sha256=EXECUTION,
                    authorization_sha256=AUTHORIZATION,
                    scope_sha256=SCOPE,
                    trust_bundle_sha256=TRUST_BUNDLE,
                    held_lock=held,
                )
                with self.assertRaisesRegex(
                    AuthorityReplayError,
                    "authority_nonce_pair_mixed_state",
                ):
                    state.claim_exact_nonce_pair(
                        first,
                        second,
                        held_lock=held,
                        allow_new_pair=True,
                    )
                self.assertTrue(
                    state.inspect_nonce_claim(NONCE, held_lock=held).present
                )
                self.assertFalse(
                    state.inspect_nonce_claim(
                        SECOND_NONCE, held_lock=held
                    ).present
                )

    def test_exact_nonce_pair_refuses_reverse_mixed_state_without_first_claim(
        self,
    ) -> None:
        with SecureTemporaryDirectory() as directory:
            state = AuthorityState(directory / "authority.sqlite3", create=True)
            first, second = exact_nonce_pair()
            with GlobalExecutionLock(directory / "execution.lock") as lock:
                held = lock.held_capability()
                state.claim_nonce(
                    SECOND_NONCE,
                    operation="empty_rollback",
                    execution_sha256="8" * 64,
                    authorization_sha256="9" * 64,
                    scope_sha256="a" * 64,
                    trust_bundle_sha256="b" * 64,
                    held_lock=held,
                )
                with self.assertRaisesRegex(
                    AuthorityReplayError,
                    "authority_nonce_pair_mixed_state",
                ):
                    state.claim_exact_nonce_pair(
                        first,
                        second,
                        held_lock=held,
                        allow_new_pair=True,
                    )
                self.assertFalse(
                    state.inspect_nonce_claim(NONCE, held_lock=held).present
                )
                self.assertTrue(
                    state.inspect_nonce_claim(
                        SECOND_NONCE, held_lock=held
                    ).present
                )

    def test_exact_nonce_pair_refuses_binding_mismatch_and_duplicate_nonce(self) -> None:
        with SecureTemporaryDirectory() as directory:
            state = AuthorityState(directory / "authority.sqlite3", create=True)
            first, second = exact_nonce_pair()
            changed_second = derive_nonce_claim_identity(
                SECOND_NONCE,
                operation="empty_rollback",
                execution_sha256="c" * 64,
                authorization_sha256="9" * 64,
                scope_sha256="a" * 64,
                trust_bundle_sha256="b" * 64,
            )
            with GlobalExecutionLock(directory / "execution.lock") as lock:
                held = lock.held_capability()
                state.claim_exact_nonce_pair(
                    first,
                    second,
                    held_lock=held,
                    allow_new_pair=True,
                )
                with self.assertRaisesRegex(
                    AuthorityReplayError,
                    "authority_nonce_pair_replayed",
                ):
                    state.claim_exact_nonce_pair(
                        first,
                        changed_second,
                        held_lock=held,
                        allow_new_pair=True,
                    )
                resumed = state.claim_exact_nonce_pair(
                    first,
                    second,
                    held_lock=held,
                    allow_new_pair=False,
                )
                self.assertEqual(resumed.result, "exact_execution_resumed")
                with self.assertRaisesRegex(
                    AuthorityStateIntegrityError,
                    "authority_nonce_pair_not_distinct",
                ):
                    state.claim_exact_nonce_pair(
                        first,
                        first,
                        held_lock=held,
                        allow_new_pair=True,
                    )

    def test_exact_nonce_pair_invalid_inputs_refuse_before_mutation(self) -> None:
        with SecureTemporaryDirectory() as directory:
            state = AuthorityState(directory / "authority.sqlite3", create=True)
            first, second = exact_nonce_pair()
            invalid = NonceClaimIdentity(
                nonce_sha256=first.nonce_sha256,
                operation_sha256=first.operation_sha256,
                execution_sha256=first.execution_sha256,
                authorization_sha256=first.authorization_sha256,
                scope_sha256=first.scope_sha256,
                trust_bundle_sha256=first.trust_bundle_sha256,
                claim_sha256="f" * 64,
            )
            with GlobalExecutionLock(directory / "execution.lock") as lock:
                held = lock.held_capability()
                cases = (
                    (invalid, second, True),
                    (first, object(), True),
                    (first, second, "yes"),
                )
                for candidate_first, candidate_second, policy in cases:
                    with (
                        self.subTest(policy=policy),
                        self.assertRaises(AuthorityStateIntegrityError),
                    ):
                        state.claim_exact_nonce_pair(
                            candidate_first,  # type: ignore[arg-type]
                            candidate_second,  # type: ignore[arg-type]
                            held_lock=held,
                            allow_new_pair=policy,  # type: ignore[arg-type]
                        )
                self.assertFalse(
                    state.inspect_nonce_claim(NONCE, held_lock=held).present
                )
                self.assertFalse(
                    state.inspect_nonce_claim(
                        SECOND_NONCE, held_lock=held
                    ).present
                )

    def test_exact_nonce_pair_second_insert_failure_rolls_back_first(self) -> None:
        with SecureTemporaryDirectory() as directory:
            state = AuthorityState(directory / "authority.sqlite3", create=True)
            first, second = exact_nonce_pair()
            original = AuthorityState._insert_exact_nonce_identity
            insertion_count = 0

            def insert(
                connection: sqlite3.Connection,
                identity: tuple[str, str, str, str, str, str, str],
            ) -> None:
                nonlocal insertion_count
                insertion_count += 1
                if insertion_count == 2:
                    raise sqlite3.IntegrityError("injected_second_insert_failure")
                original(connection, identity)

            with GlobalExecutionLock(directory / "execution.lock") as lock:
                held = lock.held_capability()
                with (
                    mock.patch.object(
                        AuthorityState,
                        "_insert_exact_nonce_identity",
                        side_effect=insert,
                    ),
                    self.assertRaisesRegex(
                        AuthorityReplayError,
                        "authority_nonce_pair_collision",
                    ),
                ):
                    state.claim_exact_nonce_pair(
                        first,
                        second,
                        held_lock=held,
                        allow_new_pair=True,
                    )
                self.assertEqual(insertion_count, 2)
                self.assertFalse(
                    state.inspect_nonce_claim(NONCE, held_lock=held).present
                )
                self.assertFalse(
                    state.inspect_nonce_claim(
                        SECOND_NONCE, held_lock=held
                    ).present
                )

    def test_exact_nonce_pair_lock_loss_before_commit_rolls_back_both(self) -> None:
        with SecureTemporaryDirectory() as directory:
            state = AuthorityState(directory / "authority.sqlite3", create=True)
            first, second = exact_nonce_pair()
            with GlobalExecutionLock(directory / "execution.lock") as lock:
                held = lock.held_capability()
                with (
                    mock.patch(
                        "tools.governed_memory_install.authority_state."
                        "validate_held_execution_lock",
                        side_effect=(
                            None,
                            ExecutionLockError("execution_lock_lost"),
                        ),
                    ),
                    self.assertRaisesRegex(
                        AuthorityStateSecurityError,
                        "authority_nonce_pair_claim_lock_not_held",
                    ),
                ):
                    state.claim_exact_nonce_pair(
                        first,
                        second,
                        held_lock=held,
                        allow_new_pair=True,
                    )
                self.assertFalse(
                    state.inspect_nonce_claim(NONCE, held_lock=held).present
                )
                self.assertFalse(
                    state.inspect_nonce_claim(
                        SECOND_NONCE, held_lock=held
                    ).present
                )

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

    def test_explicit_create_recovers_inode_left_before_schema_initialization(
        self,
    ) -> None:
        with SecureTemporaryDirectory() as directory:
            database = directory / "authority.sqlite3"
            with mock.patch.object(
                AuthorityState,
                "_initialize_schema",
                side_effect=SystemExit("synthetic_process_death"),
            ):
                with self.assertRaisesRegex(SystemExit, "synthetic_process_death"):
                    AuthorityState(database, create=True)
            metadata = database.stat(follow_symlinks=False)
            self.assertEqual(metadata.st_size, 0)
            self.assertEqual(metadata.st_nlink, 1)
            recovered = AuthorityState(database, create=True)
            reopened = AuthorityState(database)
            self.assertTrue(recovered._schema_ready)
            self.assertTrue(reopened._schema_ready)

    def test_explicit_create_recovers_empty_sqlite_header_but_not_foreign_schema(
        self,
    ) -> None:
        with SecureTemporaryDirectory() as directory:
            empty = directory / "empty.sqlite3"
            connection = sqlite3.connect(empty)
            connection.close()
            os.chmod(empty, 0o600)
            with self.assertRaises(AuthorityStateIntegrityError):
                AuthorityState(empty)
            AuthorityState(empty, create=True)
            AuthorityState(empty)

            foreign = directory / "foreign.sqlite3"
            connection = sqlite3.connect(foreign)
            connection.execute("CREATE TABLE foreign_state (value TEXT)")
            connection.commit()
            connection.close()
            os.chmod(foreign, 0o600)
            with self.assertRaises(AuthorityStateIntegrityError):
                AuthorityState(foreign, create=True)

    def test_anchor_cas_allows_only_exact_one_entry_recovery(self) -> None:
        with SecureTemporaryDirectory() as directory:
            state = AuthorityState(
                directory / "authority.sqlite3",
                create=True,
            )
            empty = state.read_anchor(BINDING)
            self.assertEqual((empty.sequence, empty.head_sha256), (0, ZERO_HEAD))
            with GlobalExecutionLock(directory / "execution.lock") as lock:
                held = lock.held_capability()
                first = state.advance_anchor(
                    BINDING,
                    journal_sequence=1,
                    journal_head_sha256=HEAD_ONE,
                    journal_prior_head_sha256=ZERO_HEAD,
                    held_lock=held,
                )
                self.assertEqual(first.result, "anchor_advanced_one_entry")
                exact = state.advance_anchor(
                    BINDING,
                    journal_sequence=1,
                    journal_head_sha256=HEAD_ONE,
                    journal_prior_head_sha256=ZERO_HEAD,
                    held_lock=held,
                )
                self.assertEqual(exact.result, "anchor_exact_resume")

                second = state.advance_anchor(
                    BINDING,
                    journal_sequence=2,
                    journal_head_sha256=HEAD_TWO,
                    journal_prior_head_sha256=HEAD_ONE,
                    held_lock=held,
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
                        held_lock=held,
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
                        held_lock=held,
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
                        held_lock=held,
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

    def test_resume_only_refuses_absent_claim_and_resumes_exact_claim_after_expiry(
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
                with self.assertRaisesRegex(
                    ExecutionAuthorityError,
                    "execution_authority_claim_absent",
                ):
                    resume_execution_authority(
                        execution_capability(),
                        state=state,
                        clock=valid_clock,
                        held_lock=held,
                        expected_operation="dormant_install",
                    )
                self.assertFalse(
                    state.inspect_nonce_claim(NONCE, held_lock=held).present
                )

                claimed = claim_execution_authority(
                    execution_capability(),
                    state=state,
                    clock=valid_clock,
                    held_lock=held,
                    expected_operation="dormant_install",
                )
                self.assertEqual(claimed.result, "execution_authority_claimed")

                resumed = resume_execution_authority(
                    execution_capability(),
                    state=state,
                    clock=FixedClock(
                        datetime(2026, 8, 11, 12, 20, tzinfo=timezone.utc)
                    ),
                    held_lock=held,
                    expected_operation="dormant_install",
                )
                self.assertEqual(
                    resumed.result, "execution_authority_exact_resume"
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

    def test_only_contention_errnos_are_reported_as_lock_busy(self) -> None:
        with SecureTemporaryDirectory() as directory:
            path = directory / "execution.lock"

            def fail_exclusive_lock(
                unused_descriptor: int,
                operation: int,
            ) -> None:
                if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                    raise OSError(self.selected_errno, "synthetic flock failure")

            for selected_errno in (errno.EACCES, errno.EAGAIN):
                with self.subTest(selected_errno=selected_errno):
                    self.selected_errno = selected_errno
                    with mock.patch.object(
                        fcntl,
                        "flock",
                        side_effect=fail_exclusive_lock,
                    ), self.assertRaisesRegex(
                        ExecutionLockBusyError,
                        "execution_lock_busy",
                    ):
                        GlobalExecutionLock(path)

            for selected_errno in (errno.EBADF, errno.EINTR, errno.EIO):
                with self.subTest(selected_errno=selected_errno):
                    self.selected_errno = selected_errno
                    with mock.patch.object(
                        fcntl,
                        "flock",
                        side_effect=fail_exclusive_lock,
                    ), self.assertRaisesRegex(
                        ExecutionLockSecurityError,
                        "execution_lock_acquire_failed",
                    ):
                        GlobalExecutionLock(path)

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

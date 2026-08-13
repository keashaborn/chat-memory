from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from tools.governed_memory_install import authority
from tools.governed_memory_install.authority_state import AuthorityState
from tools.governed_memory_install.controller import (
    EXECUTION_MODE,
    JournalEvent,
    JournalRecord,
    DormantStoreInstallController,
    StateDriftError,
    StepState,
    STORES_ONLY_PLAN,
    validate_plan,
)
from tools.governed_memory_install.journal import (
    DurableJournal,
    DurableJournalAnchorError,
    DurableJournalIntegrityError,
    DurableJournalSecurityError,
)
from tools.governed_memory_install.execution_capability import (
    ClaimedExecutionBindingError,
    _claimed_execution_binding_evidence,
    claim_dormant_store_install_execution_binding,
)
from tools.governed_memory_install.execution_lock import GlobalExecutionLock
from tools.governed_memory_install.controller_runtime import (
    EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256,
    VerifiedControllerRuntimeEvidence,
    _RUNTIME_TOKEN,
    _VerifiedControllerRuntimeCapability,
)
from tools.governed_memory_install.package_capability import (
    PackageCapabilityError,
    verify_install_package_capability,
)


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64
COMMIT_A = "1" * 40
TREE_A = "2" * 40
RUNTIME_RECEIPT_SHA256 = "9" * 64
REPO_ROOT = Path(__file__).resolve().parents[2]


class _Clock:
    def read_utc(self) -> datetime:
        return datetime(2026, 8, 11, 12, 5, tzinfo=timezone.utc)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


class _Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_directory = root / "state"
        self.journal_directory = root / "journal"
        self.lock_directory = root / "lock"
        for directory in (
            self.state_directory,
            self.journal_directory,
            self.lock_directory,
        ):
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
        self.state = AuthorityState(
            self.state_directory / "authority.sqlite3",
            expected_uid=os.geteuid(),
            create=True,
        )
        self.execution_lock = GlobalExecutionLock(
            self.lock_directory / "execution.lock"
        )
        self.store_spec_json = _canonical(
            json.loads(
                (
                    REPO_ROOT
                    / "ops/governed_memory/installation/store_spec-v2.json"
                ).read_text(encoding="ascii")
            )
        )
        self.resource_ledger_source = b"# test resource identity ledger\n"
        self.controller_runtime_contract_json = _canonical({})
        self.controller_requirements_lock = b"runtime-lock\n"
        self.supervisor_launcher_source = b"# synthetic launcher\n"
        self.execution_plan_json = _canonical(
            {
                "schema_version": "test-plan-v1",
                "install_steps": [
                    {
                        "id": step.step_id,
                        "effect": step.effect,
                        "rollback": step.rollback,
                    }
                    for step in STORES_ONLY_PLAN
                ],
            }
        )
        exact_targets = {
            "global_lock": str(self.execution_lock.path),
            "nonce_state": str(self.state.path),
            "resource_identity_ledger": str(
                root / "executions" / "{execution_id}" / "resources.jsonl"
            ),
            "execution_journal": str(
                root / "executions" / "{execution_id}" / "journal.jsonl"
            ),
        }
        self.controller_contract_json = _canonical(
            {
                "schema_version": "test-contract-v1",
                "exact_targets": exact_targets,
            }
        )
        self.store_spec_sha256 = hashlib.sha256(self.store_spec_json).hexdigest()
        self.resource_ledger_sha256 = hashlib.sha256(
            self.resource_ledger_source
        ).hexdigest()
        self.contract_sha256 = hashlib.sha256(
            self.controller_contract_json
        ).hexdigest()
        self.plan_sha256 = hashlib.sha256(self.execution_plan_json).hexdigest()
        self.exact_targets_sha256 = hashlib.sha256(
            _canonical(exact_targets)
        ).hexdigest()
        self.artifact_bytes = {
            "ops/governed_memory/installation/current/contract.json": (
                self.controller_contract_json
            ),
            "ops/governed_memory/installation/current/controller_plan.json": (
                self.execution_plan_json
            ),
            "ops/governed_memory/installation/store_spec-v2.json": (
                self.store_spec_json
            ),
            "ops/governed_memory/installation/current/"
            "controller_runtime_contract.json": (
                self.controller_runtime_contract_json
            ),
            "ops/governed_memory/controller-requirements.lock": (
                self.controller_requirements_lock
            ),
            "tools/governed_memory_install/resource_identity.py": (
                self.resource_ledger_source
            ),
            "tools/governed_memory_install/store_supervisor_launcher.py": (
                self.supervisor_launcher_source
            ),
        }
        manifest = {
            "schema_version": "test-dormant_store_install-package-v1",
            "state": "test_only",
            "artifacts": {
                path: hashlib.sha256(raw).hexdigest()
                for path, raw in self.artifact_bytes.items()
            },
        }
        self.package_manifest_json = _canonical(manifest)
        self.package_manifest_sha256 = hashlib.sha256(
            self.package_manifest_json
        ).hexdigest()
        self.scope = {
            "candidate_git_commit": COMMIT_A,
            "candidate_git_tree": TREE_A,
            "package_manifest_sha256": self.package_manifest_sha256,
            "controller_contract_sha256": self.contract_sha256,
            "execution_plan_sha256": self.plan_sha256,
            "exact_targets_sha256": self.exact_targets_sha256,
            "controller_runtime_receipt_sha256": RUNTIME_RECEIPT_SHA256,
        }
        self.scope_json = _canonical(self.scope)
        evidence = authority.CryptographicallyValidScopeNotExecution(
            result_type="cryptographically_valid_scope_not_execution",
            operation="dormant_install",
            authorization_namespace="test.dormant_store_install",
            thread_id="test-thread",
            scope_id="test-scope",
            authorization_id="test-authorization",
            key_id=HASH_A,
            nonce="N" * 48,
            scope_sha256=hashlib.sha256(self.scope_json).hexdigest(),
            authorization_sha256=HASH_A,
            trust_bundle_sha256=HASH_B,
            not_before="2026-08-11T12:00:00Z",
            expires_at="2026-08-11T12:10:00Z",
        )
        self.verified_capability = authority._VerifiedDormantInstallCapability(
            evidence,
            authority._EXECUTION_CAPABILITY_TOKEN,
        )

    def synthetic_runtime_capability(self) -> object:
        runtime_root = (
            "/opt/governed-memory-controller/runtimes/"
            + RUNTIME_RECEIPT_SHA256
        )
        launcher_path = (
            "/opt/governed-memory-controller/releases/"
            + self.package_manifest_sha256
            + "/tools/governed_memory_install/store_supervisor_launcher.py"
        )
        release_root = (
            "/opt/governed-memory-controller/releases/"
            + self.package_manifest_sha256
        )
        evidence = VerifiedControllerRuntimeEvidence(
            result_type="verified_controller_runtime_v1",
            controller_runtime_receipt_sha256=RUNTIME_RECEIPT_SHA256,
            build_plan_sha256="9" * 64,
            package_manifest_sha256=self.package_manifest_sha256,
            controller_runtime_contract_sha256=hashlib.sha256(
                self.controller_runtime_contract_json
            ).hexdigest(),
            controller_requirements_lock_sha256=hashlib.sha256(
                self.controller_requirements_lock
            ).hexdigest(),
            standalone_cpython_specification_sha256="a" * 64,
            standalone_cpython_archive_sha256="b" * 64,
            standalone_cpython_payload_tree_sha256="c" * 64,
            wheelhouse_tree_sha256="d" * 64,
            runtime_root=runtime_root,
            runtime_tree_sha256="3" * 64,
            release_root=release_root,
            release_tree_sha256="8" * 64,
            package_manifest_path=(
                release_root
                + "/ops/governed_memory/installation/current/package_manifest.json"
            ),
            interpreter_path=runtime_root + "/bin/python",
            interpreter_sha256="4" * 64,
            inventory_path=runtime_root + "/controller-distributions.json",
            installed_distribution_inventory_sha256="5" * 64,
            interpreter_path_facts_sha256="6" * 64,
            postgresql_driver_identity_sha256=(
                EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256
            ),
            supervisor_launcher_path=launcher_path,
            supervisor_launcher_sha256=hashlib.sha256(
                self.supervisor_launcher_source
            ).hexdigest(),
            launcher_help_probe_sha256="7" * 64,
            python_implementation="CPython",
            python_version="3.12.11",
            platform_os="linux",
            platform_architecture="x86_64",
            persistent_controller_substrate_created=True,
            persistent_store_resources_created=False,
        )
        return _VerifiedControllerRuntimeCapability(evidence, _RUNTIME_TOKEN)

    def claimed_binding(self) -> object:
        package_capability = verify_install_package_capability(
            self.verified_capability,
            signed_scope_json=self.scope_json,
            package_manifest_json=self.package_manifest_json,
            artifact_bytes=self.artifact_bytes,
        )
        return claim_dormant_store_install_execution_binding(
            self.verified_capability,
            verified_package_capability=package_capability,
            verified_controller_runtime_capability=(
                self.synthetic_runtime_capability()
            ),
            state=self.state,
            clock=_Clock(),
            held_lock=self.execution_lock.held_capability(),
        )

    def close(self) -> None:
        self.execution_lock.close()


def _encoded_record(record: JournalRecord) -> bytes:
    return _canonical(
        {
            "schema_version": "governed-memory-dormant_store_install-journal-v1",
            "plan_sha256": record.plan_sha256,
            "attempt_id": record.attempt_id,
            "sequence": record.sequence,
            "step_id": record.step_id,
            "event": record.event,
            "prior_record_sha256": record.prior_record_sha256,
            "record_sha256": record.record_sha256,
        }
    ) + b"\n"


class DormantStoreInstallDurableJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = _Fixture(Path(self.temporary.name))
        self.binding = self.fixture.claimed_binding()
        evidence = _claimed_execution_binding_evidence(self.binding)
        self.path = Path(evidence.execution_journal_path)
        self.path.parent.mkdir(parents=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)

    def tearDown(self) -> None:
        self.fixture.close()
        self.temporary.cleanup()

    def _record(
        self,
        journal: DurableJournal,
        *,
        sequence: int,
        prior: str,
        step_index: int = 0,
        event: JournalEvent = JournalEvent.INTENT,
    ) -> JournalRecord:
        return JournalRecord.create(
            plan_sha256=journal.plan_sha256,
            attempt_id=journal.attempt_id,
            sequence=sequence,
            step_id=STORES_ONLY_PLAN[step_index].step_id,
            event=event,
            prior_record_sha256=prior,
        )

    def test_opaque_claim_derives_stable_ids_and_binds_all_artifacts(self) -> None:
        first = _claimed_execution_binding_evidence(self.binding)
        resumed = _claimed_execution_binding_evidence(
            self.fixture.claimed_binding()
        )
        self.assertEqual(
            repr(self.binding),
            "ClaimedExecutionBinding(<content-redacted>)",
        )
        self.assertEqual(first.claim_result, "execution_authority_claimed")
        self.assertEqual(
            resumed.claim_result,
            "execution_authority_exact_resume",
        )
        self.assertEqual(first.execution_id, resumed.execution_id)
        self.assertEqual(first.attempt_id, resumed.attempt_id)
        self.assertEqual(
            first.journal_binding_sha256,
            resumed.journal_binding_sha256,
        )
        self.assertEqual(
            first.package_manifest_sha256,
            self.fixture.package_manifest_sha256,
        )
        self.assertEqual(
            first.controller_contract_sha256,
            self.fixture.contract_sha256,
        )
        self.assertEqual(first.execution_plan_sha256, self.fixture.plan_sha256)
        self.assertEqual(
            first.controller_runtime_receipt_sha256,
            RUNTIME_RECEIPT_SHA256,
        )
        self.assertEqual(len(self.fixture.artifact_bytes), 7)
        self.assertEqual(
            first.controller_model_sha256,
            validate_plan(STORES_ONLY_PLAN),
        )
        self.assertEqual(first.store_spec_sha256, self.fixture.store_spec_sha256)
        self.assertEqual(
            first.resource_identity_implementation_sha256,
            self.fixture.resource_ledger_sha256,
        )
        self.assertRegex(first.execution_id, r"^[0-9a-f]{64}$")
        self.assertRegex(first.attempt_id, r"^install-[0-9a-f]{40}$")

    def test_forged_scope_package_and_artifact_refuse_before_new_claim(self) -> None:
        alternate = _Fixture(Path(self.temporary.name) / "alternate")
        try:
            with self.assertRaisesRegex(
                PackageCapabilityError,
                "package_scope_mismatch",
            ):
                verify_install_package_capability(
                    alternate.verified_capability,
                    signed_scope_json=alternate.scope_json + b" ",
                    package_manifest_json=alternate.package_manifest_json,
                    artifact_bytes=alternate.artifact_bytes,
                )
            with self.assertRaisesRegex(
                PackageCapabilityError,
                "package_manifest_scope_mismatch",
            ):
                verify_install_package_capability(
                    alternate.verified_capability,
                    signed_scope_json=alternate.scope_json,
                    package_manifest_json=alternate.package_manifest_json + b" ",
                    artifact_bytes=alternate.artifact_bytes,
                )
            with self.assertRaisesRegex(
                PackageCapabilityError,
                "package_artifact_hash_mismatch",
            ):
                verify_install_package_capability(
                    alternate.verified_capability,
                    signed_scope_json=alternate.scope_json,
                    package_manifest_json=alternate.package_manifest_json,
                    artifact_bytes={
                        **alternate.artifact_bytes,
                        "ops/governed_memory/installation/store_spec-v2.json": (
                            alternate.store_spec_json + b" "
                        ),
                    },
                )
            package_capability = verify_install_package_capability(
                alternate.verified_capability,
                signed_scope_json=alternate.scope_json,
                package_manifest_json=alternate.package_manifest_json,
                artifact_bytes=alternate.artifact_bytes,
            )
            with self.assertRaisesRegex(
                ClaimedExecutionBindingError,
                "claimed_execution_controller_runtime_invalid",
            ):
                claim_dormant_store_install_execution_binding(
                    alternate.verified_capability,
                    verified_package_capability=package_capability,
                    verified_controller_runtime_capability=object(),
                    state=alternate.state,
                    clock=_Clock(),
                    held_lock=alternate.execution_lock.held_capability(),
                )
            claimed = _claimed_execution_binding_evidence(
                alternate.claimed_binding()
            )
            self.assertEqual(claimed.claim_result, "execution_authority_claimed")
        finally:
            alternate.close()

    def test_forged_binding_is_rejected_before_journal_path_access(self) -> None:
        missing = Path(self.temporary.name) / "does-not-exist" / "journal"
        with self.assertRaisesRegex(
            DurableJournalSecurityError,
            "binding_invalid",
        ):
            DurableJournal(
                missing,
                claimed_execution_binding=object(),
                authority_state=self.fixture.state,
                held_lock=self.fixture.execution_lock.held_capability(),
                create=True,
            )
        self.assertFalse(missing.parent.exists())

        wrong = self.path.with_name("other.jsonl")
        with self.assertRaisesRegex(
            DurableJournalSecurityError,
            "path_invalid",
        ):
            DurableJournal(
                wrong,
                claimed_execution_binding=self.binding,
                authority_state=self.fixture.state,
                held_lock=self.fixture.execution_lock.held_capability(),
                create=True,
            )
        self.assertFalse(wrong.exists())

    def test_forged_or_released_global_lock_refuses_before_path_access(self) -> None:
        missing = Path(self.temporary.name) / "no-journal-parent" / "journal"
        with self.assertRaisesRegex(
            DurableJournalSecurityError,
            "lock_not_held",
        ):
            DurableJournal(
                missing,
                claimed_execution_binding=self.binding,
                authority_state=self.fixture.state,
                held_lock=object(),  # type: ignore[arg-type]
                create=True,
            )
        held = self.fixture.execution_lock.held_capability()
        self.fixture.execution_lock.close()
        with self.assertRaisesRegex(
            DurableJournalSecurityError,
            "lock_not_held",
        ):
            DurableJournal(
                missing,
                claimed_execution_binding=self.binding,
                authority_state=self.fixture.state,
                held_lock=held,
                create=True,
            )
        self.assertFalse(missing.parent.exists())

    def test_append_is_canonical_fsynced_anchored_and_exactly_resumable(self) -> None:
        with DurableJournal(
            self.path,
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
            expected_uid=os.geteuid(),
            create=True,
        ) as journal:
            record = self._record(journal, sequence=1, prior="0" * 64)
            with mock.patch(
                "tools.governed_memory_install.journal.os.fsync",
                wraps=os.fsync,
            ) as fsync:
                journal.append_journal(record)
                self.assertGreaterEqual(fsync.call_count, 1)
            self.assertEqual(journal.journal_records(), (record,))
            self.assertEqual(journal.anchor_result, "anchor_exact_resume")
        raw = self.path.read_bytes()
        self.assertEqual(raw, _encoded_record(record))
        metadata = self.path.stat(follow_symlinks=False)
        self.assertEqual(metadata.st_mode & 0o777, 0o600)
        self.assertEqual(metadata.st_nlink, 1)
        with DurableJournal(
            self.path,
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
        ) as reopened:
            self.assertEqual(reopened.anchor_result, "anchor_exact_resume")
            self.assertEqual(reopened.journal_records(), (record,))

    def test_forged_append_is_refused_without_file_or_anchor_advance(self) -> None:
        with DurableJournal(
            self.path,
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
            create=True,
        ) as journal:
            forged = JournalRecord.create(
                plan_sha256=journal.plan_sha256,
                attempt_id="caller-chosen-attempt",
                sequence=1,
                step_id=STORES_ONLY_PLAN[0].step_id,
                event=JournalEvent.INTENT,
                prior_record_sha256="0" * 64,
            )
            with self.assertRaisesRegex(
                DurableJournalIntegrityError,
                "append_binding_invalid",
            ):
                journal.append_journal(forged)
            with self.assertRaisesRegex(
                DurableJournalIntegrityError,
                "record_type_invalid",
            ):
                journal.append_journal(object())  # type: ignore[arg-type]
            self.assertEqual(journal.journal_records(), ())
            self.assertEqual(journal.anchor_result, "anchor_exact_resume")
        self.assertEqual(self.path.read_bytes(), b"")

    def test_released_lock_refuses_an_already_open_journal(self) -> None:
        alternate = _Fixture(Path(self.temporary.name) / "released-open")
        journal: DurableJournal | None = None
        try:
            binding = alternate.claimed_binding()
            journal_path = Path(
                _claimed_execution_binding_evidence(binding).execution_journal_path
            )
            journal_path.parent.mkdir(parents=True, mode=0o700)
            os.chmod(journal_path.parent, 0o700)
            journal = DurableJournal(
                journal_path,
                claimed_execution_binding=binding,
                authority_state=alternate.state,
                held_lock=alternate.execution_lock.held_capability(),
                create=True,
            )
            alternate.execution_lock.close()
            with self.assertRaisesRegex(
                DurableJournalSecurityError,
                "lock_not_held",
            ):
                journal.journal_records()
        finally:
            if journal is not None:
                journal.close()
            alternate.close()

    def test_exactly_one_complete_file_entry_ahead_repairs_anchor(self) -> None:
        with DurableJournal(
            self.path,
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
            create=True,
        ) as journal:
            first = self._record(journal, sequence=1, prior="0" * 64)
            journal.append_journal(first)
            second = self._record(
                journal,
                sequence=2,
                prior=first.record_sha256,
                event=JournalEvent.APPLIED,
            )
        with self.path.open("ab", buffering=0) as stream:
            stream.write(_encoded_record(second))
            os.fsync(stream.fileno())
        with DurableJournal(
            self.path,
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
        ) as repaired:
            self.assertEqual(
                repaired.anchor_result,
                "anchor_advanced_one_entry",
            )
            self.assertEqual(repaired.journal_records(), (first, second))

    def test_missing_anchor_with_exactly_one_complete_entry_repairs(self) -> None:
        with DurableJournal(
            self.path,
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
            create=True,
        ) as journal:
            first = self._record(journal, sequence=1, prior="0" * 64)
            journal.append_journal(first)
        with sqlite3.connect(self.fixture.state.path) as connection:
            connection.execute(
                "DELETE FROM journal_anchor_v1 WHERE binding_sha256 = ?",
                (
                    _claimed_execution_binding_evidence(
                        self.binding
                    ).journal_binding_sha256,
                ),
            )
        with DurableJournal(
            self.path,
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
        ) as repaired:
            self.assertEqual(repaired.anchor_result, "anchor_advanced_one_entry")
            self.assertEqual(repaired.journal_records(), (first,))

    def test_process_death_partial_tail_after_exact_anchor_is_truncated(self) -> None:
        with DurableJournal(
            self.path,
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
            create=True,
        ) as journal:
            first = self._record(journal, sequence=1, prior="0" * 64)
            journal.append_journal(first)
            second = self._record(
                journal,
                sequence=2,
                prior=first.record_sha256,
                event=JournalEvent.APPLIED,
            )
        anchored = self.path.read_bytes()
        torn = _encoded_record(second)[:73]
        with self.path.open("ab", buffering=0) as stream:
            stream.write(torn)
            os.fsync(stream.fileno())

        with DurableJournal(
            self.path,
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
        ) as recovered:
            self.assertEqual(recovered.anchor_result, "anchor_exact_resume")
            self.assertEqual(recovered.journal_records(), (first,))
        self.assertEqual(self.path.read_bytes(), anchored)

    def test_partial_tail_after_complete_unanchored_suffix_is_refused(self) -> None:
        with DurableJournal(
            self.path,
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
            create=True,
        ) as journal:
            first = self._record(journal, sequence=1, prior="0" * 64)
            journal.append_journal(first)
            second = self._record(
                journal,
                sequence=2,
                prior=first.record_sha256,
                event=JournalEvent.APPLIED,
            )
            third = self._record(
                journal,
                sequence=3,
                prior=second.record_sha256,
                step_index=1,
            )
        unauthenticated = _encoded_record(second) + _encoded_record(third)[:71]
        with self.path.open("ab", buffering=0) as stream:
            stream.write(unauthenticated)
            os.fsync(stream.fileno())
        before = self.path.read_bytes()

        with self.assertRaisesRegex(
            DurableJournalIntegrityError,
            "truncated_tail_not_exact_anchor",
        ):
            DurableJournal(
                self.path,
                claimed_execution_binding=self.binding,
                authority_state=self.fixture.state,
                held_lock=self.fixture.execution_lock.held_capability(),
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_anchor_ahead_and_multi_entry_gap_are_refused(self) -> None:
        with DurableJournal(
            self.path,
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
            create=True,
        ) as journal:
            first = self._record(journal, sequence=1, prior="0" * 64)
            journal.append_journal(first)
        self.path.write_bytes(b"")
        os.chmod(self.path, 0o600)
        with self.assertRaisesRegex(
            DurableJournalAnchorError,
            "anchor_mismatch",
        ):
            DurableJournal(
                self.path,
                claimed_execution_binding=self.binding,
                authority_state=self.fixture.state,
                held_lock=self.fixture.execution_lock.held_capability(),
            )

        alternate = _Fixture(Path(self.temporary.name) / "multi-gap")
        try:
            second_binding = alternate.claimed_binding()
            second_path = Path(
                _claimed_execution_binding_evidence(
                    second_binding
                ).execution_journal_path
            )
            second_path.parent.mkdir(parents=True, mode=0o700)
            os.chmod(second_path.parent, 0o700)
            with DurableJournal(
                second_path,
                claimed_execution_binding=second_binding,
                authority_state=alternate.state,
                held_lock=alternate.execution_lock.held_capability(),
                create=True,
            ) as journal:
                one = self._record(journal, sequence=1, prior="0" * 64)
                two = self._record(
                    journal,
                    sequence=2,
                    prior=one.record_sha256,
                    event=JournalEvent.APPLIED,
                )
            second_path.write_bytes(_encoded_record(one) + _encoded_record(two))
            os.chmod(second_path, 0o600)
            with self.assertRaisesRegex(
                DurableJournalAnchorError,
                "anchor_mismatch",
            ):
                DurableJournal(
                    second_path,
                    claimed_execution_binding=second_binding,
                    authority_state=alternate.state,
                    held_lock=alternate.execution_lock.held_capability(),
                )
        finally:
            alternate.close()

    def test_truncation_mutation_reorder_and_replacement_are_refused(self) -> None:
        with DurableJournal(
            self.path,
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
            create=True,
        ) as journal:
            first = self._record(journal, sequence=1, prior="0" * 64)
            journal.append_journal(first)
            second = self._record(
                journal,
                sequence=2,
                prior=first.record_sha256,
                event=JournalEvent.APPLIED,
            )
            journal.append_journal(second)
        original = self.path.read_bytes()

        self.path.write_bytes(original[:-1])
        os.chmod(self.path, 0o600)
        with self.assertRaisesRegex(DurableJournalIntegrityError, "truncated"):
            DurableJournal(
                self.path,
                claimed_execution_binding=self.binding,
                authority_state=self.fixture.state,
                held_lock=self.fixture.execution_lock.held_capability(),
            )

        mutated = original.replace(first.step_id.encode("ascii"), b"I01_MUTATED", 1)
        self.path.write_bytes(mutated)
        os.chmod(self.path, 0o600)
        with self.assertRaises(DurableJournalIntegrityError):
            DurableJournal(
                self.path,
                claimed_execution_binding=self.binding,
                authority_state=self.fixture.state,
                held_lock=self.fixture.execution_lock.held_capability(),
            )

        lines = original.splitlines(keepends=True)
        self.path.write_bytes(lines[1] + lines[0])
        os.chmod(self.path, 0o600)
        with self.assertRaises(DurableJournalIntegrityError):
            DurableJournal(
                self.path,
                claimed_execution_binding=self.binding,
                authority_state=self.fixture.state,
                held_lock=self.fixture.execution_lock.held_capability(),
            )

        self.path.write_bytes(original)
        os.chmod(self.path, 0o600)
        with DurableJournal(
            self.path,
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
        ) as journal:
            displaced = self.path.with_suffix(".old")
            self.path.rename(displaced)
            self.path.write_bytes(original)
            os.chmod(self.path, 0o600)
            with self.assertRaisesRegex(
                DurableJournalSecurityError,
                "file_invalid|file_replaced",
            ):
                journal.journal_records()

    def test_parent_mode_file_mode_symlink_and_hardlink_are_refused(self) -> None:
        os.chmod(self.path.parent, 0o755)
        try:
            with self.assertRaisesRegex(
                DurableJournalSecurityError,
                "directory_invalid",
            ):
                DurableJournal(
                    self.path,
                    claimed_execution_binding=self.binding,
                    authority_state=self.fixture.state,
                    held_lock=self.fixture.execution_lock.held_capability(),
                    create=True,
                )
        finally:
            os.chmod(self.path.parent, 0o700)

        with DurableJournal(
            self.path,
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
            create=True,
        ):
            pass
        os.chmod(self.path, 0o644)
        with self.assertRaisesRegex(DurableJournalSecurityError, "file_invalid"):
            DurableJournal(
                self.path,
                claimed_execution_binding=self.binding,
                authority_state=self.fixture.state,
                held_lock=self.fixture.execution_lock.held_capability(),
            )
        os.chmod(self.path, 0o600)
        hardlink = self.path.parent / "hardlink"
        os.link(self.path, hardlink)
        with self.assertRaisesRegex(DurableJournalSecurityError, "file_invalid"):
            DurableJournal(
                self.path,
                claimed_execution_binding=self.binding,
                authority_state=self.fixture.state,
                held_lock=self.fixture.execution_lock.held_capability(),
            )
        hardlink.unlink()
        original = self.path.with_suffix(".real")
        self.path.rename(original)
        self.path.symlink_to(original.name)
        with self.assertRaisesRegex(DurableJournalSecurityError, "file_invalid"):
            DurableJournal(
                self.path,
                claimed_execution_binding=self.binding,
                authority_state=self.fixture.state,
                held_lock=self.fixture.execution_lock.held_capability(),
            )


class _RecoveryBackend:
    execution_mode = EXECUTION_MODE

    def __init__(self) -> None:
        self.records: list[JournalRecord] = []
        self.states = {
            step.step_id: (
                StepState.AFTER if step.invariant_only else StepState.BEFORE
            )
            for step in STORES_ONLY_PLAN
        }

    def journal_records(self) -> tuple[JournalRecord, ...]:
        return tuple(self.records)

    def append_journal(self, record: JournalRecord) -> None:
        self.records.append(record)

    def probe(self, step: object) -> StepState:
        return self.states[step.step_id]  # type: ignore[attr-defined]

    def apply(self, step: object) -> None:
        self.states[step.step_id] = StepState.AFTER  # type: ignore[attr-defined]

    def compensate(self, step: object) -> None:
        self.states[step.step_id] = StepState.BEFORE  # type: ignore[attr-defined]

    def append(
        self,
        *,
        attempt_id: str,
        step_id: str,
        event: JournalEvent,
    ) -> None:
        prior = self.records[-1].record_sha256 if self.records else "0" * 64
        self.records.append(
            JournalRecord.create(
                plan_sha256=validate_plan(STORES_ONLY_PLAN),
                attempt_id=attempt_id,
                sequence=len(self.records) + 1,
                step_id=step_id,
                event=event,
                prior_record_sha256=prior,
            )
        )


class DormantStoreInstallControllerRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name) / "lock"
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
        self.lock = GlobalExecutionLock(directory / "execution.lock")

    def tearDown(self) -> None:
        self.lock.close()
        self.temporary.cleanup()

    def _controller(self, backend: _RecoveryBackend) -> DormantStoreInstallController:
        return DormantStoreInstallController(
            plan=STORES_ONLY_PLAN,
            backend=backend,
            held_lock=self.lock.held_capability(),
        )

    def test_crash_after_compensation_effect_before_record_resumes(self) -> None:
        backend = _RecoveryBackend()
        attempt = "compensation-effect-crash"
        for step in STORES_ONLY_PLAN[:4]:
            backend.append(attempt_id=attempt, step_id=step.step_id, event=JournalEvent.INTENT)
            backend.append(attempt_id=attempt, step_id=step.step_id, event=JournalEvent.APPLIED)
            backend.states[step.step_id] = StepState.AFTER
        backend.append(
            attempt_id=attempt,
            step_id="I00_ATTEMPT",
            event=JournalEvent.COMPENSATION_STARTED,
        )
        backend.append(
            attempt_id=attempt,
            step_id=STORES_ONLY_PLAN[3].step_id,
            event=JournalEvent.COMPENSATION_INTENT,
        )
        backend.states[STORES_ONLY_PLAN[3].step_id] = StepState.BEFORE
        receipt = self._controller(backend).run(attempt_id=attempt)
        self.assertEqual(receipt.outcome, "same_attempt_compensation_complete")
        self.assertEqual(
            receipt.compensated_step_ids,
            (STORES_ONLY_PLAN[3].step_id,),
        )

    def test_unowned_effect_during_compensation_is_refused(self) -> None:
        backend = _RecoveryBackend()
        attempt = "compensation-unowned-effect"
        for step in STORES_ONLY_PLAN[:4]:
            backend.append(
                attempt_id=attempt,
                step_id=step.step_id,
                event=JournalEvent.INTENT,
            )
            backend.append(
                attempt_id=attempt,
                step_id=step.step_id,
                event=JournalEvent.APPLIED,
            )
            backend.states[step.step_id] = StepState.AFTER
        backend.append(
            attempt_id=attempt,
            step_id="I00_ATTEMPT",
            event=JournalEvent.COMPENSATION_STARTED,
        )
        backend.states[STORES_ONLY_PLAN[7].step_id] = StepState.AFTER
        with self.assertRaisesRegex(
            StateDriftError,
            "unowned_effect_during_compensation",
        ):
            self._controller(backend).run(attempt_id=attempt)

    def test_recoverable_state_requires_exact_current_intent(self) -> None:
        backend = _RecoveryBackend()
        attempt = "exact-recoverable-intent"
        for step in STORES_ONLY_PLAN[:3]:
            backend.append(attempt_id=attempt, step_id=step.step_id, event=JournalEvent.INTENT)
            backend.append(attempt_id=attempt, step_id=step.step_id, event=JournalEvent.APPLIED)
            backend.states[step.step_id] = StepState.AFTER
        backend.append(
            attempt_id=attempt,
            step_id=STORES_ONLY_PLAN[3].step_id,
            event=JournalEvent.INTENT,
        )
        backend.states[STORES_ONLY_PLAN[3].step_id] = StepState.RECOVERABLE
        receipt = self._controller(backend).run(attempt_id=attempt)
        self.assertEqual(receipt.outcome, "inactive_stores_installation_complete")

        unowned = _RecoveryBackend()
        unowned.states[STORES_ONLY_PLAN[4].step_id] = StepState.RECOVERABLE
        with self.assertRaisesRegex(
            StateDriftError,
            "recoverable_without_exact_current_intent",
        ):
            self._controller(unowned).run(attempt_id="unowned-recoverable")


if __name__ == "__main__":
    unittest.main()

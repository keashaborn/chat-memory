from __future__ import annotations

from collections import Counter
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.governed_memory_install.authority_state import (
    AuthorityState,
    AuthorityStateSecurityError,
    JournalAnchorError,
    ResourceLedgerAnchorError,
)
from tools.governed_memory_install.controller import (
    EXECUTION_MODE,
    JournalEvent,
    JournalRecord,
    DormantStoreInstallController,
    PlanStep,
    StepState,
    STORES_ONLY_PLAN,
)
from tools.governed_memory_install.execution_capability import (
    _claimed_execution_binding_evidence,
)
from tools.governed_memory_install.journal import (
    DurableJournal,
    DurableJournalAnchorError,
    DurableJournalIntegrityError,
    DurableJournalSecurityError,
    MAX_EFFECT_RECOVERY_RECORDS,
    MAX_RECORD_BYTES,
)
from tools.governed_memory_install import journal as journal_module
from tools.governed_memory_install.resource_identity import (
    ResourceIdentityError,
    ResourceIdentityLedger,
)
from tests.memory.test_installation_durable_journal import (
    HASH_C,
    _Fixture,
    _encoded_record,
)

RESOURCE_LABELS_SHA256 = "d" * 64


class _DurableControllerBackend:
    execution_mode = EXECUTION_MODE

    def __init__(self, journal: DurableJournal) -> None:
        self.journal = journal
        self.states = {
            step.step_id: StepState.BEFORE for step in STORES_ONLY_PLAN
        }
        self.apply_calls: Counter[str] = Counter()

    def journal_records(self) -> tuple[JournalRecord, ...]:
        return self.journal.journal_records()

    def append_journal(self, record: JournalRecord) -> None:
        self.journal.append_journal(record)

    def probe(self, step: PlanStep) -> StepState:
        return self.states[step.step_id]

    def apply(self, step: PlanStep) -> None:
        self.apply_calls[step.step_id] += 1
        self.states[step.step_id] = StepState.AFTER

    def compensate(self, step: PlanStep) -> None:
        self.states[step.step_id] = StepState.BEFORE


class InstallationDurabilityAnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = _Fixture(Path(self.temporary.name))
        self.binding = self.fixture.claimed_binding()
        self.evidence = _claimed_execution_binding_evidence(self.binding)
        self.execution_directory = Path(
            self.evidence.execution_journal_path
        ).parent
        self.execution_directory.mkdir(parents=True, mode=0o700)
        os.chmod(self.execution_directory, 0o700)

    def tearDown(self) -> None:
        self.fixture.close()
        self.temporary.cleanup()

    def _journal(self, *, create: bool) -> DurableJournal:
        return DurableJournal(
            Path(self.evidence.execution_journal_path),
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
            create=create,
        )

    def _ledger(self, *, create: bool) -> ResourceIdentityLedger:
        return ResourceIdentityLedger(
            Path(self.evidence.resource_identity_ledger_path),
            claimed_execution_binding=self.binding,
            authority_state=self.fixture.state,
            held_lock=self.fixture.execution_lock.held_capability(),
            create=create,
        )

    def _append_network_identity(
        self,
        ledger: ResourceIdentityLedger,
        *,
        event: str = "created",
    ):
        return ledger.append(
            event=event,
            resource_kind="network",
            resource_name="governed-memory-test-net",
            resource_id="network-id-001",
            ownership_sha256=HASH_C,
            resource_labels_sha256=RESOURCE_LABELS_SHA256,
        )

    @staticmethod
    def _append_fsynced(path: Path, value: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
        try:
            offset = 0
            while offset < len(value):
                written = os.write(descriptor, value[offset:])
                if written <= 0:
                    raise OSError("short test write")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _replace_same_content_with_distinct_inode(self, path: Path) -> None:
        raw = path.read_bytes()
        before = os.stat(path, follow_symlinks=False)
        replacement = path.with_name(path.name + ".same-content-replacement")
        replacement.write_bytes(raw)
        os.chmod(replacement, 0o600)
        staged = os.stat(replacement, follow_symlinks=False)
        self.assertNotEqual(
            (before.st_dev, before.st_ino),
            (staged.st_dev, staged.st_ino),
        )
        os.replace(replacement, path)
        after = os.stat(path, follow_symlinks=False)
        self.assertEqual(
            (after.st_dev, after.st_ino),
            (staged.st_dev, staged.st_ino),
        )

    def test_resource_ledger_is_per_execution_lock_gated_and_anchored(self) -> None:
        ledger = self._ledger(create=True)
        self.assertEqual(ledger.anchor_result, "resource_anchor_exact_resume")
        created = ledger.append(
            event="created",
            resource_kind="network",
            resource_name="governed-memory-test-net",
            resource_id="network-id-001",
            ownership_sha256=HASH_C,
            resource_labels_sha256=RESOURCE_LABELS_SHA256,
        )
        self.assertEqual(created.sequence, 1)
        self.assertEqual(
            ledger.anchor_result,
            "resource_anchor_advanced_one_entry",
        )
        anchor = self.fixture.state.read_resource_ledger_anchor(
            ledger.binding_sha256,
            held_lock=self.fixture.execution_lock.held_capability(),
        )
        self.assertEqual((anchor.sequence, anchor.head_sha256), (1, created.entry_sha256))

        reopened = self._ledger(create=False)
        self.assertEqual(reopened.anchor_result, "resource_anchor_exact_resume")
        self.assertEqual(reopened.records(), (created,))

        self.fixture.execution_lock.close()
        with self.assertRaisesRegex(
            ResourceIdentityError,
            "global_lock_not_held",
        ):
            reopened.records()

    def test_resource_ledger_fsynced_entry_repairs_exactly_one_anchor_gap(self) -> None:
        ledger = self._ledger(create=True)
        real_advance = self.fixture.state.advance_resource_ledger_anchor

        def fail_after_file_commit(binding: str, **arguments: object):
            if arguments["ledger_sequence"] == 1:
                raise ResourceLedgerAnchorError("injected_anchor_failure")
            return real_advance(binding, **arguments)

        with mock.patch.object(
            self.fixture.state,
            "advance_resource_ledger_anchor",
            side_effect=fail_after_file_commit,
        ):
            with self.assertRaisesRegex(
                ResourceIdentityError,
                "anchor_mismatch",
            ):
                ledger.append(
                    event="created",
                    resource_kind="network",
                    resource_name="governed-memory-test-net",
                    resource_id="network-id-001",
                    ownership_sha256=HASH_C,
                    resource_labels_sha256=RESOURCE_LABELS_SHA256,
                )

        repaired = self._ledger(create=False)
        self.assertEqual(
            repaired.anchor_result,
            "resource_anchor_advanced_one_entry",
        )
        self.assertEqual(len(repaired.records()), 1)

    def test_resource_ledger_recovers_one_final_partial_entry_at_exact_anchor(
        self,
    ) -> None:
        ledger = self._ledger(create=True)
        created = self._append_network_identity(ledger)
        path = Path(self.evidence.resource_identity_ledger_path)
        anchored = path.read_bytes()
        partial = (
            b'{"binding_sha256":"'
            + ledger.binding_sha256.encode("ascii")
            + b'","entry_sha256":"'
        )
        self._append_fsynced(path, partial)

        recovered = self._ledger(create=False)

        self.assertEqual(recovered.records(), (created,))
        self.assertEqual(path.read_bytes(), anchored)
        self.assertEqual(
            recovered.anchor_result,
            "resource_anchor_exact_resume",
        )

    def test_resource_ledger_partial_recovery_refuses_complete_unanchored_entry(
        self,
    ) -> None:
        ledger = self._ledger(create=True)
        self._append_network_identity(ledger)
        path = Path(self.evidence.resource_identity_ledger_path)
        compatibility_writer = ResourceIdentityLedger(
            path,
            binding_sha256=ledger.binding_sha256,
        )
        compatibility_writer.append(
            event="observed",
            resource_kind="network",
            resource_name="governed-memory-test-net",
            resource_id="network-id-001",
            ownership_sha256=HASH_C,
            resource_labels_sha256=RESOURCE_LABELS_SHA256,
        )
        partial = b'{"binding_sha256":"' + ledger.binding_sha256.encode("ascii")
        self._append_fsynced(path, partial)
        damaged = path.read_bytes()

        with self.assertRaisesRegex(
            ResourceIdentityError,
            "truncated_tail_not_exact_anchor",
        ):
            self._ledger(create=False)
        self.assertEqual(path.read_bytes(), damaged)

    def test_resource_ledger_partial_recovery_refuses_middle_and_multiple_damage(
        self,
    ) -> None:
        ledger = self._ledger(create=True)
        self._append_network_identity(ledger)
        path = Path(self.evidence.resource_identity_ledger_path)
        prefix = b'{"binding_sha256":"' + ledger.binding_sha256.encode("ascii")
        self._append_fsynced(path, prefix + b'"}\n' + prefix)
        damaged = path.read_bytes()

        with self.assertRaises(ResourceIdentityError):
            self._ledger(create=False)
        self.assertEqual(path.read_bytes(), damaged)

    def test_resource_ledger_partial_recovery_refuses_anchor_ahead(self) -> None:
        ledger = self._ledger(create=True)
        self._append_network_identity(ledger)
        self._append_network_identity(ledger, event="observed")
        path = Path(self.evidence.resource_identity_ledger_path)
        first_entry = path.read_bytes().splitlines(keepends=True)[0]
        partial = b'{"binding_sha256":"' + ledger.binding_sha256.encode("ascii")
        path.write_bytes(first_entry + partial)
        os.chmod(path, 0o600)
        damaged = path.read_bytes()

        with self.assertRaisesRegex(
            ResourceIdentityError,
            "truncated_tail_not_exact_anchor",
        ):
            self._ledger(create=False)
        self.assertEqual(path.read_bytes(), damaged)

    def test_resource_ledger_partial_recovery_refuses_file_replacement(self) -> None:
        ledger = self._ledger(create=True)
        self._append_network_identity(ledger)
        path = Path(self.evidence.resource_identity_ledger_path)
        partial = b'{"binding_sha256":"' + ledger.binding_sha256.encode("ascii")
        self._append_fsynced(path, partial)
        real_read = self.fixture.state.read_resource_ledger_anchor

        def replace_file(*arguments: object, **keywords: object):
            anchor = real_read(*arguments, **keywords)
            raw = path.read_bytes()
            path.unlink()
            path.write_bytes(raw)
            os.chmod(path, 0o600)
            return anchor

        with mock.patch.object(
            self.fixture.state,
            "read_resource_ledger_anchor",
            side_effect=replace_file,
        ):
            with self.assertRaisesRegex(
                ResourceIdentityError,
                "file_invalid|cross_process_identity_mismatch",
            ):
                self._ledger(create=False)

    def test_resource_ledger_partial_recovery_refuses_directory_replacement(
        self,
    ) -> None:
        ledger = self._ledger(create=True)
        self._append_network_identity(ledger)
        path = Path(self.evidence.resource_identity_ledger_path)
        partial = b'{"binding_sha256":"' + ledger.binding_sha256.encode("ascii")
        self._append_fsynced(path, partial)
        real_read = self.fixture.state.read_resource_ledger_anchor

        def replace_directory(*arguments: object, **keywords: object):
            anchor = real_read(*arguments, **keywords)
            raw = path.read_bytes()
            replaced = self.execution_directory.with_name(
                self.execution_directory.name + "-tail-replaced"
            )
            self.execution_directory.rename(replaced)
            self.execution_directory.mkdir(mode=0o700)
            os.chmod(self.execution_directory, 0o700)
            path.write_bytes(raw)
            os.chmod(path, 0o600)
            return anchor

        with mock.patch.object(
            self.fixture.state,
            "read_resource_ledger_anchor",
            side_effect=replace_directory,
        ):
            with self.assertRaisesRegex(
                ResourceIdentityError,
                "cross_process_identity_mismatch",
            ):
                self._ledger(create=False)

    def test_resource_ledger_multi_entry_anchor_gap_refuses(self) -> None:
        secure = self._ledger(create=True)
        compatibility_writer = ResourceIdentityLedger(
            Path(self.evidence.resource_identity_ledger_path),
            binding_sha256=secure.binding_sha256,
        )
        compatibility_writer.append(
            event="created",
            resource_kind="network",
            resource_name="governed-memory-test-net",
            resource_id="network-id-001",
            ownership_sha256=HASH_C,
            resource_labels_sha256=RESOURCE_LABELS_SHA256,
        )
        compatibility_writer.append(
            event="observed",
            resource_kind="network",
            resource_name="governed-memory-test-net",
            resource_id="network-id-001",
            ownership_sha256=HASH_C,
            resource_labels_sha256=RESOURCE_LABELS_SHA256,
        )
        with self.assertRaisesRegex(
            ResourceIdentityError,
            "anchor_mismatch",
        ):
            self._ledger(create=False)

    def test_journal_and_ledger_same_content_inode_replacement_refuse(self) -> None:
        journal = self._journal(create=True)
        journal.close()
        journal_path = Path(self.evidence.execution_journal_path)
        self._replace_same_content_with_distinct_inode(journal_path)
        with self.assertRaisesRegex(
            DurableJournalSecurityError,
            "cross_process_identity_mismatch",
        ):
            self._journal(create=False)

        ledger = self._ledger(create=True)
        ledger_path = Path(self.evidence.resource_identity_ledger_path)
        self._replace_same_content_with_distinct_inode(ledger_path)
        with self.assertRaisesRegex(
            ResourceIdentityError,
            "cross_process_identity_mismatch",
        ):
            self._ledger(create=False)

    def test_same_content_directory_replacement_refuses_across_process_open(self) -> None:
        ledger = self._ledger(create=True)
        ledger.append(
            event="created",
            resource_kind="network",
            resource_name="governed-memory-test-net",
            resource_id="network-id-001",
            ownership_sha256=HASH_C,
            resource_labels_sha256=RESOURCE_LABELS_SHA256,
        )
        ledger_path = Path(self.evidence.resource_identity_ledger_path)
        ledger_bytes = ledger_path.read_bytes()
        replaced = self.execution_directory.with_name(
            self.execution_directory.name + "-replaced"
        )
        self.execution_directory.rename(replaced)
        self.execution_directory.mkdir(mode=0o700)
        os.chmod(self.execution_directory, 0o700)
        ledger_path.write_bytes(ledger_bytes)
        os.chmod(ledger_path, 0o600)
        with self.assertRaisesRegex(
            ResourceIdentityError,
            "cross_process_identity_mismatch",
        ):
            self._ledger(create=False)

    def test_authority_state_same_content_inode_replacement_refuses(self) -> None:
        path = self.fixture.state.path
        self._replace_same_content_with_distinct_inode(path)
        with self.assertRaisesRegex(
            AuthorityStateSecurityError,
            "cross_process_identity_mismatch",
        ):
            AuthorityState(path)

    def test_intent_refuses_before_effect_without_bounded_recovery_tail(self) -> None:
        with self._journal(create=True) as journal:
            record = JournalRecord.create(
                plan_sha256=journal.plan_sha256,
                attempt_id=journal.attempt_id,
                sequence=1,
                step_id=STORES_ONLY_PLAN[0].step_id,
                event=JournalEvent.INTENT,
                prior_record_sha256="0" * 64,
            )
            exact_limit_minus_one = (
                len(_encoded_record(record))
                + (MAX_EFFECT_RECOVERY_RECORDS * MAX_RECORD_BYTES)
                - 1
            )
            with mock.patch.object(
                journal_module,
                "MAX_JOURNAL_BYTES",
                exact_limit_minus_one,
            ):
                with self.assertRaisesRegex(
                    DurableJournalIntegrityError,
                    "effect_recovery_capacity_not_reserved",
                ):
                    journal.append_journal(record)
            self.assertEqual(journal.journal_records(), ())
            self.assertEqual(Path(journal.path).read_bytes(), b"")

    def test_fsynced_applied_anchor_failure_does_not_duplicate_effect_on_resume(
        self,
    ) -> None:
        journal = self._journal(create=True)
        first = STORES_ONLY_PLAN[0]
        intent = JournalRecord.create(
            plan_sha256=journal.plan_sha256,
            attempt_id=journal.attempt_id,
            sequence=1,
            step_id=first.step_id,
            event=JournalEvent.INTENT,
            prior_record_sha256="0" * 64,
        )
        journal.append_journal(intent)
        applied = JournalRecord.create(
            plan_sha256=journal.plan_sha256,
            attempt_id=journal.attempt_id,
            sequence=2,
            step_id=first.step_id,
            event=JournalEvent.APPLIED,
            prior_record_sha256=intent.record_sha256,
        )
        real_advance = self.fixture.state.advance_anchor

        def fail_after_fsync(binding: str, **arguments: object):
            if arguments["journal_sequence"] == 2:
                raise JournalAnchorError("injected_anchor_failure")
            return real_advance(binding, **arguments)

        with mock.patch.object(
            self.fixture.state,
            "advance_anchor",
            side_effect=fail_after_fsync,
        ):
            with self.assertRaises(DurableJournalAnchorError):
                journal.append_journal(applied)
        journal.close()

        repaired = self._journal(create=False)
        self.assertEqual(repaired.anchor_result, "anchor_advanced_one_entry")
        backend = _DurableControllerBackend(repaired)
        backend.states[first.step_id] = StepState.AFTER
        controller = DormantStoreInstallController(
            plan=STORES_ONLY_PLAN,
            backend=backend,
            held_lock=self.fixture.execution_lock.held_capability(),
        )
        receipt = controller.run(attempt_id=self.evidence.attempt_id)
        self.assertEqual(receipt.outcome, "inactive_stores_installation_complete")
        self.assertEqual(backend.apply_calls[first.step_id], 0)
        for step in STORES_ONLY_PLAN[1:]:
            self.assertEqual(backend.apply_calls[step.step_id], 1)
        repaired.close()


if __name__ == "__main__":
    unittest.main()

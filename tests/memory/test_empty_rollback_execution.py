from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import copy
import hashlib
import inspect
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.governed_memory_install.authority_state import AuthorityState
from tools.governed_memory_install.execution_lock import GlobalExecutionLock
from tools.governed_memory_install.durable_receipts import (
    DurableReceiptStore,
    ReceiptArtifact,
)
from tools.governed_memory_install.live_rollback_marker import (
    ACQUISITION_FILENAME,
    SEMANTIC_EMPTY_FILENAME,
    DurableRootLiveRollbackMarkerTransport,
    LiveRollbackMarkerTransportError,
    RootControllerAuthorityMarkerFileObservation,
)
from tools.governed_memory_install.rollback import (
    RETAINED_AUDIT_KEYS,
    ROLLBACK_RESOURCE_KEYS,
    build_empty_rollback_eligibility_receipt,
    derive_exact_rollback_resources_from_ledger,
    eligibility_receipt_sha256,
    empty_rollback_plan_sha256,
    exact_rollback_targets_sha256,
    expected_rollback_resource_names,
    verified_rollback_resource_parts,
)
from tools.governed_memory_install.rollback_authority import (
    EmptyRollbackExpectedBindings,
)
from tools.governed_memory_install.package_capability import (
    verified_package_evidence,
)
from tools.governed_memory_install.resource_identity import load_ledger
from tests.memory.test_empty_rollback_authority import (
    build_verified_controller_runtime,
    build_verified_install_package,
    build_verified_rollback_capability,
)
from tests.memory.resource_identity_test_support import append_resource_identity
from tools.governed_memory_install.controller_runtime import (
    _RUNTIME_TOKEN,
    _VerifiedControllerRuntimeCapability,
    verified_controller_runtime_evidence,
)
from tools.governed_memory_install import rollback_entrypoint
from tools.governed_memory_install import rollback_live_adapter
from tools.governed_memory_install import rollback_journal
from tools.governed_memory_install.rollback_journal import (
    DurableRollbackJournal,
    DurableRollbackJournalError,
    parse_rollback_journal_bytes,
)
from tools.governed_memory_install.rollback_entrypoint import (
    ClaimedEmptyRollbackEvidence,
    EmptyRollbackExecutionError,
    EmptyRollbackControllerAuthorityMarkerAcquisition,
    RollbackEvent,
    RollbackJournalRecord,
    RollbackObservation,
    _run_authorized_empty_store_rollback_durable_synthetic,
    claimed_empty_rollback_evidence,
    run_authorized_empty_store_rollback,
    validate_rollback_records,
)
from tools.governed_memory_install.receipts import (
    build_install_receipt,
    receipt_sha256,
    verify_empty_rollback_receipt,
)


class _Clock:
    def read_utc(self) -> datetime:
        return datetime(2026, 8, 12, 19, 5, tzinfo=timezone.utc)


class _ExpiredClock:
    def read_utc(self) -> datetime:
        return datetime(2026, 8, 12, 19, 11, tzinfo=timezone.utc)


class _NotYetValidClock:
    def read_utc(self) -> datetime:
        return datetime(2026, 8, 12, 18, 59, tzinfo=timezone.utc)


class _Journal:
    def __init__(self, claimed: object) -> None:
        evidence = claimed_empty_rollback_evidence(claimed)
        self.plan_sha256 = evidence.plan_sha256
        self.attempt_id = evidence.attempt_id
        self.execution_id = evidence.execution_id
        self.binding_sha256 = evidence.journal_binding_sha256
        self.items: list[RollbackJournalRecord] = []
        self.fail_before_applied_step: str | None = None
        self.fail_after_durable_step: str | None = None
        self.reserve_calls: list[int] = []

    def records(self) -> tuple[RollbackJournalRecord, ...]:
        return tuple(self.items)

    def reserve_effect_capacity(self, remaining_record_count: int) -> None:
        if type(remaining_record_count) is not int or remaining_record_count < 1:
            raise RuntimeError("bad synthetic reservation")
        self.reserve_calls.append(remaining_record_count)

    def append(self, record: RollbackJournalRecord) -> None:
        if (
            record.event == RollbackEvent.APPLIED.value
            and record.step_id == self.fail_before_applied_step
        ):
            self.fail_before_applied_step = None
            raise RuntimeError("synthetic crash before durable append")
        self.items.append(record)
        if (
            record.event == RollbackEvent.APPLIED.value
            and record.step_id == self.fail_after_durable_step
        ):
            self.fail_after_durable_step = None
            raise RuntimeError("synthetic anchor failure after durable append")

    def close(self) -> None:
        return None


class _JournalFactory:
    synthetic_receipt_root: Path | None = None

    def __init__(self, receipt_root: Path | None = None) -> None:
        self.journal: _Journal | None = None
        self.receipt_root = (
            receipt_root
            if receipt_root is not None
            else self.synthetic_receipt_root
        )

    def __call__(self, claimed: object) -> _Journal:
        if self.receipt_root is not None:
            execution_id = claimed_empty_rollback_evidence(claimed).execution_id
            execution_root = self.receipt_root / execution_id
            execution_root.mkdir(mode=0o700, exist_ok=True)
            os.chmod(execution_root, 0o700)
        if self.journal is None:
            self.journal = _Journal(claimed)
        return self.journal


class _Operations:
    def __init__(
        self,
        eligibility: dict[str, object],
        canonical_install_receipt: bytes,
        verified_resources: object,
    ) -> None:
        self.eligibility = eligibility
        self.canonical_install_receipt = canonical_install_receipt
        self.verified_resources = verified_resources
        self.states: dict[str, str] = {}
        self.apply_count: dict[str, int] = {}
        self.install_binding_checks = 0
        self.eligibility_checks = 0
        self.tamper_ownership_step: str | None = None
        self.tamper_runtime_tree_step: str | None = None
        self.observed_resource_names: dict[str, str] = {}
        self.inject_writer_after_store_stop = False
        self.live_qdrant_points = 0
        self.return_unverified_sequence = False
        self.active_controller_authority_marker_sha256: str | None = None
        self.marker_bound_postgres_identity_sha256 = hashlib.sha256(
            b"marker-bound:postgres"
        ).hexdigest()
        self.marker_bound_qdrant_identity_sha256 = hashlib.sha256(
            b"marker-bound:qdrant"
        ).hexdigest()
        self.marker_acquisitions = 0
        self.marker_releases = 0
        self.tampered_install_receipt_after_r03: bytes | None = None
        self.remove_install_receipt_after_r03 = False
        self.remove_retained_audit_key_after_r03: str | None = None
        self.retained_audit_hashes = {
            key: hashlib.sha256(("retained:" + key).encode("ascii")).hexdigest()
            for key in RETAINED_AUDIT_KEYS
        }
        self.retained_audit_hashes["installation_receipt"] = hashlib.sha256(
            canonical_install_receipt
        ).hexdigest()
        self.retained_audit_hashes["eligibility_receipt"] = hashlib.sha256(
            rollback_entrypoint.canonical_json_bytes(eligibility)
        ).hexdigest()
    def verify_install_receipt_and_ledger(
        self,
        request,
    ) -> object:
        if (
            request.installation_receipt_sha256
            != self.eligibility["installation_receipt_sha256"]
            or request.resource_ledger_head_sha256
            != self.eligibility["resource_ledger_head_sha256"]
            or request.exact_targets_sha256
            != self.eligibility["exact_targets_sha256"]
        ):
            raise RuntimeError("binding mismatch")
        if self.install_binding_checks >= 1:
            if self.remove_install_receipt_after_r03:
                canonical_install_receipt = b""
            elif self.tampered_install_receipt_after_r03 is not None:
                canonical_install_receipt = self.tampered_install_receipt_after_r03
            else:
                canonical_install_receipt = self.canonical_install_receipt
        else:
            canonical_install_receipt = self.canonical_install_receipt
        self.install_binding_checks += 1
        if self.return_unverified_sequence:
            return len(ROLLBACK_RESOURCE_KEYS)
        return rollback_entrypoint.verify_retained_install_receipt_and_ledger(
            canonical_install_receipt,
            self.verified_resources,
        )

    def _validate_marker(self, request, capability: object) -> None:
        evidence = (
            rollback_entrypoint.validate_empty_rollback_controller_authority_marker_capability(
                capability,
                expected_execution_id=request.execution_id,
                expected_attempt_id=request.attempt_id,
                expected_plan_sha256=request.plan_sha256,
                expected_eligibility_receipt_sha256=(
                    request.eligibility_receipt_sha256
                ),
                expected_exact_targets_sha256=request.exact_targets_sha256,
                expected_controller_runtime_tree_sha256=(
                    request.controller_runtime_tree_sha256
                ),
                expected_controller_release_tree_sha256=(
                    request.controller_release_tree_sha256
                ),
                expected_marker_bound_postgres_identity_sha256=(
                    self.marker_bound_postgres_identity_sha256
                ),
                expected_marker_bound_qdrant_identity_sha256=(
                    self.marker_bound_qdrant_identity_sha256
                ),
                expected_controller_authority_marker_sha256=self.active_controller_authority_marker_sha256,
            )
        )
        if evidence.controller_authority_marker_sha256 != self.active_controller_authority_marker_sha256:
            raise RuntimeError("controller-authority marker is not held")

    def acquire_empty_rollback_controller_authority_marker(
        self,
        request,
    ) -> EmptyRollbackControllerAuthorityMarkerAcquisition:
        if self.active_controller_authority_marker_sha256 is None:
            self.marker_acquisitions += 1
            self.active_controller_authority_marker_sha256 = hashlib.sha256(
                (request.execution_id + request.attempt_id).encode("ascii")
            ).hexdigest()
        return EmptyRollbackControllerAuthorityMarkerAcquisition(
            execution_id=request.execution_id,
            attempt_id=request.attempt_id,
            plan_sha256=request.plan_sha256,
            exact_targets_sha256=request.exact_targets_sha256,
            controller_runtime_tree_sha256=request.controller_runtime_tree_sha256,
            controller_release_tree_sha256=request.controller_release_tree_sha256,
            marker_bound_postgres_identity_sha256=(
                self.marker_bound_postgres_identity_sha256
            ),
            marker_bound_qdrant_identity_sha256=(
                self.marker_bound_qdrant_identity_sha256
            ),
            controller_authority_marker_sha256=self.active_controller_authority_marker_sha256,
        )

    def release_empty_rollback_controller_authority_marker(self, request, capability: object) -> None:
        self._validate_marker(request, capability)
        self.active_controller_authority_marker_sha256 = None
        self.marker_releases += 1

    def observe_empty_eligibility(
        self,
        request,
        controller_authority_marker_capability: object | None,
    ) -> dict[str, object]:
        if request.step.step_id != (
            "R06_ESTABLISH_ADMINISTRATIVE_WRITER_FENCE_AND_RECHECK_SEMANTIC_EMPTY"
        ):
            raise RuntimeError("unexpected semantic-empty step")
        self._validate_marker(request, controller_authority_marker_capability)
        self.eligibility_checks += 1
        observed = copy.deepcopy(self.eligibility)
        observed["observation_set_sha256"] = hashlib.sha256(
            request.step.step_id.encode("ascii")
        ).hexdigest()
        observed["qdrant_points"] = self.live_qdrant_points
        observed["receipt_sha256"] = eligibility_receipt_sha256(observed)
        if (
            request.step.step_id
            == "R06_ESTABLISH_ADMINISTRATIVE_WRITER_FENCE_AND_RECHECK_SEMANTIC_EMPTY"
            and self.inject_writer_after_store_stop
        ):
            self.live_qdrant_points = 1
        return observed

    def observe(self, request) -> RollbackObservation:
        if request.resource is not None:
            self.observed_resource_names[request.step.step_id] = (
                request.resource.resource_name
            )
        state = self.states.get(
            request.step.step_id,
            "after" if request.step.invariant_only else "before",
        )
        revision = hashlib.sha256(
            (request.step.step_id + ":" + state).encode("ascii")
        ).hexdigest()
        ownership = rollback_entrypoint._EmptyRollbackController._expected_ownership(
            request, revision
        )
        if request.step.step_id == self.tamper_ownership_step:
            ownership = "0" * 64
        runtime_tree = request.controller_runtime_tree_sha256
        if request.step.step_id == self.tamper_runtime_tree_step:
            runtime_tree = "0" * 64
        return RollbackObservation(
            state,
            revision,
            ownership,
            runtime_tree,
            request.controller_release_tree_sha256,
        )

    def apply_if_still_empty(
        self,
        request,
        expected: RollbackObservation,
        controller_authority_marker_capability: object,
        signed_eligibility,
    ) -> None:
        self._validate_marker(request, controller_authority_marker_capability)
        if self.observe(request) != expected:
            raise RuntimeError("resource changed inside controller-authority marker")
        observed_empty = copy.deepcopy(self.eligibility)
        observed_empty["observation_set_sha256"] = hashlib.sha256(
            ("atomic:" + request.step.step_id).encode("ascii")
        ).hexdigest()
        observed_empty["qdrant_points"] = self.live_qdrant_points
        observed_empty["receipt_sha256"] = eligibility_receipt_sha256(
            observed_empty
        )
        rollback_entrypoint._equivalent_empty_state(
            signed_eligibility,
            observed_empty,
        )
        if expected.state == "after":
            return
        self.states[request.step.step_id] = "after"
        self.apply_count[request.step.step_id] = (
            self.apply_count.get(request.step.step_id, 0) + 1
        )

    def exact_resources_absent(
        self,
        request,
        resources,
        controller_authority_marker_capability: object,
    ) -> bool:
        self._validate_marker(request, controller_authority_marker_capability)
        return all(
            self.states.get(
                step.step_id,
                "after" if step.invariant_only else "before",
            )
            == "after"
            for step in rollback_entrypoint.EMPTY_ROLLBACK_STEPS
            if step.resource_key is not None
        )

    def observe_retained_audit_set(
        self,
        request,
        retained_audit_keys,
        controller_authority_marker_capability: object,
    ) -> dict[str, str]:
        self._validate_marker(request, controller_authority_marker_capability)
        result = {
            key: self.retained_audit_hashes[key]
            for key in retained_audit_keys
        }
        if self.remove_retained_audit_key_after_r03 is not None:
            result.pop(self.remove_retained_audit_key_after_r03, None)
        return result


class _MemoryControllerAuthorityMarkerStore:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str], bytes] = {}

    @staticmethod
    def _observation(
        content: bytes,
    ) -> RootControllerAuthorityMarkerFileObservation:
        return RootControllerAuthorityMarkerFileObservation(
            content=content,
            regular_no_follow=True,
            mode=0o400,
            uid=0,
            gid=0,
            file_fsynced=True,
            parent_fsynced=True,
        )

    def create_or_read_exact(self, execution_id, filename, expected):
        key = (execution_id, filename)
        existing = self.files.setdefault(key, expected)
        if existing != expected:
            raise LiveRollbackMarkerTransportError("synthetic_marker_drift")
        return self._observation(existing)

    def read_optional_exact(self, execution_id, filename, expected):
        existing = self.files.get((execution_id, filename))
        if existing is None:
            return None
        if existing != expected:
            raise LiveRollbackMarkerTransportError("synthetic_marker_drift")
        return self._observation(existing)

    def remove_exact_execution(
        self,
        execution_id,
        acquisition,
        semantic_empty,
    ) -> None:
        expected = {
            ACQUISITION_FILENAME: acquisition,
            SEMANTIC_EMPTY_FILENAME: semantic_empty,
        }
        for filename, raw in expected.items():
            if self.files.get((execution_id, filename)) != raw:
                raise LiveRollbackMarkerTransportError(
                    "synthetic_marker_release_drift"
                )
        for filename in expected:
            del self.files[(execution_id, filename)]


class _RealMarkerOperations(_Operations):
    """Use the real create-once marker transport with synthetic effects."""

    def __init__(
        self,
        eligibility: dict[str, object],
        canonical_install_receipt: bytes,
        verified_resources: object,
    ) -> None:
        super().__init__(
            eligibility,
            canonical_install_receipt,
            verified_resources,
        )
        self.marker_store = _MemoryControllerAuthorityMarkerStore()
        self.marker_transport = DurableRootLiveRollbackMarkerTransport(
            self.marker_store
        )
        self.held_marker = None
        self.semantic_empty_state_sha256 = hashlib.sha256(
            b"synthetic-semantic-empty-state"
        ).hexdigest()

    def acquire_empty_rollback_controller_authority_marker(
        self,
        request,
    ) -> EmptyRollbackControllerAuthorityMarkerAcquisition:
        self.held_marker = self.marker_transport.acquire_or_recover(
            request,
            marker_bound_postgres_identity_sha256=(
                self.marker_bound_postgres_identity_sha256
            ),
            marker_bound_qdrant_identity_sha256=(
                self.marker_bound_qdrant_identity_sha256
            ),
            expected_semantic_empty_state_sha256=(
                self.semantic_empty_state_sha256
            ),
        )
        self.active_controller_authority_marker_sha256 = (
            self.held_marker.controller_authority_marker_sha256
        )
        self.marker_acquisitions += 1
        return EmptyRollbackControllerAuthorityMarkerAcquisition(
            execution_id=request.execution_id,
            attempt_id=request.attempt_id,
            plan_sha256=request.plan_sha256,
            exact_targets_sha256=request.exact_targets_sha256,
            controller_runtime_tree_sha256=(
                request.controller_runtime_tree_sha256
            ),
            controller_release_tree_sha256=(
                request.controller_release_tree_sha256
            ),
            marker_bound_postgres_identity_sha256=(
                self.marker_bound_postgres_identity_sha256
            ),
            marker_bound_qdrant_identity_sha256=(
                self.marker_bound_qdrant_identity_sha256
            ),
            controller_authority_marker_sha256=(
                self.active_controller_authority_marker_sha256
            ),
        )

    def observe_empty_eligibility(
        self,
        request,
        controller_authority_marker_capability,
    ) -> dict[str, object]:
        observed = super().observe_empty_eligibility(
            request,
            controller_authority_marker_capability,
        )
        if self.held_marker is None:
            raise RuntimeError("controller-authority marker is not held")
        self.held_marker = (
            self.marker_transport.persist_semantic_empty_observation(
                request,
                self.held_marker,
                self.semantic_empty_state_sha256,
            )
        )
        return observed

    def release_empty_rollback_controller_authority_marker(
        self,
        request,
        capability: object,
    ) -> None:
        self._validate_marker(request, capability)
        if self.held_marker is None:
            raise RuntimeError("controller-authority marker is not held")
        self.marker_transport.release_after_receipt(
            request,
            self.held_marker,
        )
        self.held_marker = None
        self.active_controller_authority_marker_sha256 = None
        self.marker_releases += 1



class DurableRollbackJournalTailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
        state_dir = root / "authority"
        lock_dir = root / "lock"
        journal_dir = root / "journal"
        for directory in (state_dir, lock_dir, journal_dir):
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
        self.state = AuthorityState(
            state_dir / "authority.sqlite3",
            create=True,
        )
        self.lock = GlobalExecutionLock(lock_dir / "execution.lock")
        self.path = journal_dir / "rollback.jsonl"
        self.plan_sha256 = "a" * 64
        self.attempt_id = "rollback-" + "b" * 40
        evidence = ClaimedEmptyRollbackEvidence(
            schema_version=rollback_entrypoint.ROLLBACK_CLAIM_SCHEMA,
            operation="empty_store_rollback",
            claim_result="exact_execution_resumed",
            execution_id="c" * 64,
            attempt_id=self.attempt_id,
            claim_sha256="d" * 64,
            authorization_sha256="e" * 64,
            scope_sha256="f" * 64,
            plan_sha256=self.plan_sha256,
            package_manifest_sha256="2" * 64,
            controller_runtime_receipt_sha256="3" * 64,
            controller_runtime_root=(
                "/opt/governed-memory-controller/runtimes/" + "3" * 64
            ),
            controller_runtime_tree_sha256="4" * 64,
            controller_release_root=(
                "/opt/governed-memory-controller/releases/" + "2" * 64
            ),
            controller_release_tree_sha256="5" * 64,
            controller_release_package_manifest_path=(
                "/opt/governed-memory-controller/releases/"
                + "2" * 64
                + "/ops/governed_memory/installation/current/package_manifest.json"
            ),
            controller_runtime_interpreter_path=(
                "/opt/governed-memory-controller/runtimes/"
                + "3" * 64
                + "/bin/python"
            ),
            controller_runtime_interpreter_sha256="6" * 64,
            controller_runtime_inventory_path=(
                "/opt/governed-memory-controller/runtimes/"
                + "3" * 64
                + "/controller-distributions.json"
            ),
            controller_runtime_inventory_sha256="7" * 64,
            controller_requirements_lock_sha256="8" * 64,
            supervisor_launcher_path=(
                "/opt/governed-memory-controller/releases/"
                + "2" * 64
                + "/tools/governed_memory_install/store_supervisor_launcher.py"
            ),
            supervisor_launcher_sha256="9" * 64,
            installation_execution_id="a" * 64,
            journal_binding_sha256="1" * 64,
            journal_path=str(self.path),
            authority_state_path_sha256=hashlib.sha256(
                str(self.state.path).encode("utf-8")
            ).hexdigest(),
            global_lock_path_sha256=hashlib.sha256(
                str(self.lock.path).encode("utf-8")
            ).hexdigest(),
        )
        self.claimed = rollback_entrypoint._ClaimedEmptyRollback(
            evidence,
            rollback_entrypoint._CLAIM_TOKEN,
        )

    def tearDown(self) -> None:
        self.lock.close()
        self.temporary.cleanup()

    def _record(
        self,
        sequence: int,
        step_id: str,
        event: RollbackEvent,
        prior: str,
    ) -> RollbackJournalRecord:
        return RollbackJournalRecord.create(
            plan_sha256=self.plan_sha256,
            attempt_id=self.attempt_id,
            sequence=sequence,
            step_id=step_id,
            event=event,
            prior_record_sha256=prior,
        )

    @staticmethod
    def _encoded(record: RollbackJournalRecord) -> bytes:
        return (
            rollback_journal._canonical(rollback_journal._record_document(record))
            + b"\n"
        )

    def test_process_death_partial_tail_is_truncated_from_exact_anchor(self) -> None:
        with DurableRollbackJournal(
            self.path,
            claimed_rollback=self.claimed,
            authority_state=self.state,
            held_lock=self.lock.held_capability(),
            create=True,
        ) as journal:
            journal.reserve_effect_capacity(3)
            first = self._record(
                1,
                "R01_REVERIFY_GLOBAL_LOCK",
                RollbackEvent.INTENT,
                "0" * 64,
            )
            journal.append(first)
        anchored = self.path.read_bytes()
        second = self._record(
            2,
            "R01_REVERIFY_GLOBAL_LOCK",
            RollbackEvent.APPLIED,
            first.record_sha256,
        )
        with self.path.open("ab", buffering=0) as stream:
            stream.write(self._encoded(second)[:67])
            os.fsync(stream.fileno())

        with DurableRollbackJournal(
            self.path,
            claimed_rollback=self.claimed,
            authority_state=self.state,
            held_lock=self.lock.held_capability(),
        ) as recovered:
            self.assertEqual(recovered.records(), (first,))
        self.assertEqual(self.path.read_bytes(), anchored)

    def test_complete_suffix_plus_partial_tail_is_never_truncated_or_accepted(
        self,
    ) -> None:
        with DurableRollbackJournal(
            self.path,
            claimed_rollback=self.claimed,
            authority_state=self.state,
            held_lock=self.lock.held_capability(),
            create=True,
        ) as journal:
            journal.reserve_effect_capacity(3)
            first = self._record(
                1,
                "R01_REVERIFY_GLOBAL_LOCK",
                RollbackEvent.INTENT,
                "0" * 64,
            )
            journal.append(first)
        second = self._record(
            2,
            "R01_REVERIFY_GLOBAL_LOCK",
            RollbackEvent.APPLIED,
            first.record_sha256,
        )
        third = self._record(
            3,
            "R02_VERIFY_CLAIMED_ROLLBACK_AUTHORITY",
            RollbackEvent.INTENT,
            second.record_sha256,
        )
        with self.path.open("ab", buffering=0) as stream:
            stream.write(self._encoded(second))
            stream.write(self._encoded(third)[:69])
            os.fsync(stream.fileno())
        before = self.path.read_bytes()

        with self.assertRaisesRegex(
            DurableRollbackJournalError,
            "truncated_tail_not_exact_anchor",
        ):
            DurableRollbackJournal(
                self.path,
                claimed_rollback=self.claimed,
                authority_state=self.state,
                held_lock=self.lock.held_capability(),
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_two_complete_unauthenticated_suffix_records_are_refused(self) -> None:
        with DurableRollbackJournal(
            self.path,
            claimed_rollback=self.claimed,
            authority_state=self.state,
            held_lock=self.lock.held_capability(),
            create=True,
        ) as journal:
            journal.reserve_effect_capacity(3)
            first = self._record(
                1,
                "R01_REVERIFY_GLOBAL_LOCK",
                RollbackEvent.INTENT,
                "0" * 64,
            )
            journal.append(first)
        second = self._record(
            2,
            "R01_REVERIFY_GLOBAL_LOCK",
            RollbackEvent.APPLIED,
            first.record_sha256,
        )
        third = self._record(
            3,
            "R02_VERIFY_CLAIMED_ROLLBACK_AUTHORITY",
            RollbackEvent.INTENT,
            second.record_sha256,
        )
        with self.path.open("ab", buffering=0) as stream:
            stream.write(self._encoded(second))
            stream.write(self._encoded(third))
            os.fsync(stream.fileno())
        before = self.path.read_bytes()

        with self.assertRaisesRegex(
            DurableRollbackJournalError,
            "anchor_mismatch",
        ):
            DurableRollbackJournal(
                self.path,
                claimed_rollback=self.claimed,
                authority_state=self.state,
                held_lock=self.lock.held_capability(),
            )
        self.assertEqual(self.path.read_bytes(), before)


class EmptyRollbackExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
        state_dir = root / "authority"
        lock_dir = root / "lock"
        state_dir.mkdir(mode=0o700)
        lock_dir.mkdir(mode=0o700)
        os.chmod(state_dir, 0o700)
        os.chmod(lock_dir, 0o700)
        self.state_path = state_dir / "authority.sqlite3"
        self.lock_path = lock_dir / "execution.lock"
        self.receipt_root = root / "executions"
        self.receipt_root.mkdir(mode=0o700)
        os.chmod(self.receipt_root, 0o700)
        self.installation_execution_id = "3" * 64
        installation_receipt_root = (
            self.receipt_root / self.installation_execution_id
        )
        installation_receipt_root.mkdir(mode=0o700)
        os.chmod(installation_receipt_root, 0o700)
        self.receipt_store = DurableReceiptStore.synthetic(self.receipt_root)
        _JournalFactory.synthetic_receipt_root = self.receipt_root
        self.state = AuthorityState(self.state_path, create=True)
        self.lock = GlobalExecutionLock(self.lock_path)
        self.package_capability = build_verified_install_package()
        self.runtime_capability = build_verified_controller_runtime(
            self.package_capability
        )
        runtime = verified_controller_runtime_evidence(self.runtime_capability)
        package = verified_package_evidence(self.package_capability)
        self.commit = package.candidate_git_commit
        self.tree = package.candidate_git_tree
        self.package_sha = package.package_manifest_sha256
        expected = expected_rollback_resource_names(self.package_sha)
        ledger_path = root / "resource-identity.jsonl"
        for key in ROLLBACK_RESOURCE_KEYS:
            kind, name = expected[key]
            is_container = kind == "container"
            append_resource_identity(
                ledger_path,
                binding_sha256="9" * 64,
                event="created",
                resource_kind=kind,
                resource_name=name,
                resource_id=(
                    hashlib.sha256(("container:" + key).encode("ascii")).hexdigest()
                    if is_container
                    else "resource-" + key
                ),
                image_id=("sha256:" + "1" * 64) if is_container else None,
                image_repo_digest=(
                    "governed/test@sha256:" + "2" * 64
                    if is_container
                    else None
                ),
                ownership_sha256=hashlib.sha256(
                    ("ownership:" + key).encode("ascii")
                ).hexdigest(),
                resource_labels_sha256=(
                    hashlib.sha256(
                        ("resource-labels:" + key).encode("ascii")
                    ).hexdigest()
                    if kind in {"container", "volume", "network"}
                    else None
                ),
            )
        records = load_ledger(
            ledger_path, expected_binding_sha256="9" * 64
        )
        ledger_head = records[-1].entry_sha256
        self.resources = derive_exact_rollback_resources_from_ledger(
            records,
            package_manifest_sha256=self.package_sha,
            expected_ledger_head_sha256=ledger_head,
        )
        targets_sha = exact_rollback_targets_sha256(
            self.resources, package_manifest_sha256=self.package_sha
        )
        runtime_receipt_sha256 = package.controller_runtime_receipt_sha256
        runtime_root = (
            "/opt/governed-memory-controller/runtimes/"
            + runtime_receipt_sha256
        )
        release_root = (
            "/opt/governed-memory-controller/releases/" + self.package_sha
        )
        self.install_receipt = build_install_receipt(
            execution_id=self.installation_execution_id,
            attempt_id="install-" + "4" * 40,
            candidate_git_commit=self.commit,
            candidate_git_tree=self.tree,
            package_manifest_sha256=self.package_sha,
            controller_runtime_receipt_sha256=runtime_receipt_sha256,
            controller_runtime_root=runtime_root,
            controller_runtime_tree_sha256=runtime.runtime_tree_sha256,
            controller_release_root=release_root,
            controller_release_tree_sha256=runtime.release_tree_sha256,
            controller_release_package_manifest_path=(
                release_root
                + "/ops/governed_memory/installation/current/package_manifest.json"
            ),
            controller_runtime_interpreter_path=runtime_root + "/bin/python",
            controller_runtime_interpreter_sha256=runtime.interpreter_sha256,
            controller_runtime_inventory_path=(
                runtime_root + "/controller-distributions.json"
            ),
            controller_runtime_inventory_sha256=(
                runtime.installed_distribution_inventory_sha256
            ),
            controller_requirements_lock_sha256=(
                runtime.controller_requirements_lock_sha256
            ),
            supervisor_launcher_path=(
                release_root
                + "/tools/governed_memory_install/store_supervisor_launcher.py"
            ),
            supervisor_launcher_sha256=runtime.supervisor_launcher_sha256,
            authorization_sha256="6" * 64,
            plan_sha256="7" * 64,
            journal_head_sha256="8" * 64,
            journal_sequence=41,
            resource_ledger_head_sha256=ledger_head,
            resource_ledger_sequence=len(records),
            image_identity_set_sha256="a" * 64,
            postflight_receipt_sha256="b" * 64,
            terminal_store_readiness_sha256="c" * 64,
        )
        self.install_receipt_bytes = rollback_entrypoint.canonical_json_bytes(
            self.install_receipt
        )
        self.receipt_store.write_once(
            ReceiptArtifact.INSTALL,
            self.installation_execution_id,
            self.install_receipt,
        )
        self.eligibility = build_empty_rollback_eligibility_receipt(
            candidate_git_commit=self.commit,
            candidate_git_tree=self.tree,
            package_manifest_sha256=self.package_sha,
            installation_execution_id=self.installation_execution_id,
            installation_receipt_sha256=str(
                self.install_receipt["receipt_sha256"]
            ),
            exact_targets_sha256=targets_sha,
            resource_ledger_head_sha256=ledger_head,
            observation_set_sha256="f" * 64,
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
        self.capability = self._capability(nonce="N" * 32)

    def tearDown(self) -> None:
        _JournalFactory.synthetic_receipt_root = None
        self.lock.close()
        self.temporary.cleanup()

    def _capability(self, *, nonce: str) -> object:
        plan_sha = empty_rollback_plan_sha256(
            self.eligibility, self.resources
        )
        bindings = EmptyRollbackExpectedBindings(
            candidate_git_commit=self.commit,
            candidate_git_tree=self.tree,
            package_manifest_sha256=self.package_sha,
            controller_runtime_receipt_sha256=(
                verified_package_evidence(
                    self.package_capability
                ).controller_runtime_receipt_sha256
            ),
            installation_execution_id=str(
                self.eligibility["installation_execution_id"]
            ),
            installation_receipt_sha256=str(
                self.eligibility["installation_receipt_sha256"]
            ),
            rollback_plan_sha256=plan_sha,
            exact_targets_sha256=str(self.eligibility["exact_targets_sha256"]),
            eligibility_receipt_sha256=str(self.eligibility["receipt_sha256"]),
            resource_ledger_head_sha256=str(
                self.eligibility["resource_ledger_head_sha256"]
            ),
        )
        return build_verified_rollback_capability(
            package_capability=self.package_capability,
            controller_runtime_capability=self.runtime_capability,
            bindings=bindings,
            nonce=nonce,
        )

    def _run(self, factory: _JournalFactory, operations: _Operations):
        with (
            patch.object(rollback_entrypoint, "AUTHORITY_STATE_PATH", self.state_path),
            patch.object(rollback_entrypoint, "GLOBAL_LOCK_PATH", self.lock_path),
        ):
            return rollback_entrypoint._run_authorized_empty_store_rollback_synthetic(
                verified_rollback_capability=self.capability,
                verified_package_capability=self.package_capability,
                verified_controller_runtime_capability=self.runtime_capability,
                eligibility_receipt=self.eligibility,
                resources=self.resources,
                authority_state=self.state,
                clock=_Clock(),
                held_lock=self.lock.held_capability(),
                journal_factory=factory,
                operations=operations,
                receipt_store=self.receipt_store,
            )

    def _operations(self) -> _Operations:
        return _Operations(
            self.eligibility,
            self.install_receipt_bytes,
            self.resources,
        )

    def test_exact_rollback_runs_once_and_exact_resume_is_idempotent(self) -> None:
        factory = _JournalFactory()
        operations = self._operations()
        receipt = self._run(factory, operations)
        self.assertEqual(receipt.outcome, "empty_store_rollback_complete")
        self.assertEqual(len(receipt.applied_step_ids), 22)
        self.assertEqual(operations.install_binding_checks, 2)
        self.assertEqual(operations.eligibility_checks, 1)
        self.assertEqual(operations.marker_acquisitions, 1)
        self.assertEqual(operations.marker_releases, 1)
        self.assertEqual(sum(operations.apply_count.values()), 10)
        self.assertGreater(len(factory.journal.reserve_calls), 0)  # type: ignore[union-attr]
        canonical = verify_empty_rollback_receipt(receipt.canonical_receipt)
        self.assertEqual(
            canonical["exact_targets_absent_count"], len(ROLLBACK_RESOURCE_KEYS)
        )
        self.assertEqual(canonical["journal_head_sha256"], receipt.journal_head_sha256)
        self.assertEqual(canonical["journal_sequence"], receipt.journal_sequence)
        self.assertEqual(canonical["plan_sha256"], receipt.plan_sha256)
        self.assertEqual(
            canonical["eligibility_receipt_sha256"],
            self.eligibility["receipt_sha256"],
        )
        expected_retained_sha = rollback_entrypoint._hash_domain(
            rollback_entrypoint._RETAINED_AUDIT_SET_DOMAIN,
            {
                "retained_audit_evidence": {
                    key: operations.retained_audit_hashes[key]
                    for key in sorted(operations.retained_audit_hashes)
                },
            },
        )
        self.assertEqual(
            canonical["retained_audit_set_sha256"],
            expected_retained_sha,
        )

        resumed = self._run(factory, operations)
        self.assertEqual(
            resumed.outcome, "empty_store_rollback_already_complete"
        )
        self.assertEqual(dict(resumed.canonical_receipt), canonical)
        self.assertEqual(sum(operations.apply_count.values()), 10)
        self.assertEqual(operations.install_binding_checks, 3)
        self.assertEqual(operations.eligibility_checks, 2)
        self.assertEqual(operations.marker_acquisitions, 2)
        self.assertEqual(operations.marker_releases, 2)

    def test_completed_replay_reestablishes_real_marker_semantic_proof(
        self,
    ) -> None:
        factory = _JournalFactory()
        operations = _RealMarkerOperations(
            self.eligibility,
            self.install_receipt_bytes,
            self.resources,
        )

        first = self._run(factory, operations)
        self.assertEqual(first.outcome, "empty_store_rollback_complete")
        self.assertEqual(operations.marker_store.files, {})

        replay = self._run(factory, operations)
        self.assertEqual(
            replay.outcome,
            "empty_store_rollback_already_complete",
        )
        self.assertEqual(operations.eligibility_checks, 2)
        self.assertEqual(operations.marker_acquisitions, 2)
        self.assertEqual(operations.marker_releases, 2)
        self.assertEqual(operations.marker_store.files, {})

    def test_expired_authority_cannot_persist_eligibility_receipt(self) -> None:
        eligibility_path = (
            self.receipt_root
            / self.installation_execution_id
            / "empty-rollback-eligibility-receipt.json"
        )
        self.assertFalse(eligibility_path.exists())
        factory = _JournalFactory()
        operations = self._operations()
        with (
            patch.object(rollback_entrypoint, "AUTHORITY_STATE_PATH", self.state_path),
            patch.object(rollback_entrypoint, "GLOBAL_LOCK_PATH", self.lock_path),
            self.assertRaisesRegex(
                EmptyRollbackExecutionError,
                "empty_rollback_authority_expired",
            ),
        ):
            rollback_entrypoint._run_authorized_empty_store_rollback_synthetic(
                verified_rollback_capability=self.capability,
                verified_package_capability=self.package_capability,
                verified_controller_runtime_capability=self.runtime_capability,
                eligibility_receipt=self.eligibility,
                resources=self.resources,
                authority_state=self.state,
                clock=_ExpiredClock(),
                held_lock=self.lock.held_capability(),
                journal_factory=factory,
                operations=operations,
                receipt_store=self.receipt_store,
            )
        self.assertFalse(eligibility_path.exists())
        self.assertIsNone(factory.journal)
        self.assertEqual(operations.apply_count, {})

    def test_premature_authority_cannot_persist_eligibility_receipt(self) -> None:
        eligibility_path = (
            self.receipt_root
            / self.installation_execution_id
            / "empty-rollback-eligibility-receipt.json"
        )
        factory = _JournalFactory()
        operations = self._operations()
        with (
            patch.object(rollback_entrypoint, "AUTHORITY_STATE_PATH", self.state_path),
            patch.object(rollback_entrypoint, "GLOBAL_LOCK_PATH", self.lock_path),
            self.assertRaisesRegex(
                EmptyRollbackExecutionError,
                "empty_rollback_authority_not_yet_valid",
            ),
        ):
            rollback_entrypoint._run_authorized_empty_store_rollback_synthetic(
                verified_rollback_capability=self.capability,
                verified_package_capability=self.package_capability,
                verified_controller_runtime_capability=self.runtime_capability,
                eligibility_receipt=self.eligibility,
                resources=self.resources,
                authority_state=self.state,
                clock=_NotYetValidClock(),
                held_lock=self.lock.held_capability(),
                journal_factory=factory,
                operations=operations,
                receipt_store=self.receipt_store,
            )
        self.assertFalse(eligibility_path.exists())
        self.assertIsNone(factory.journal)
        self.assertEqual(operations.apply_count, {})

    def test_wrong_lock_cannot_persist_eligibility_receipt(self) -> None:
        eligibility_path = (
            self.receipt_root
            / self.installation_execution_id
            / "empty-rollback-eligibility-receipt.json"
        )
        wrong_lock = GlobalExecutionLock(self.lock_path.with_name("wrong.lock"))
        try:
            factory = _JournalFactory()
            operations = self._operations()
            with (
                patch.object(
                    rollback_entrypoint,
                    "AUTHORITY_STATE_PATH",
                    self.state_path,
                ),
                patch.object(
                    rollback_entrypoint,
                    "GLOBAL_LOCK_PATH",
                    self.lock_path,
                ),
                self.assertRaisesRegex(
                    EmptyRollbackExecutionError,
                    "empty_rollback_execution_authority_invalid",
                ),
            ):
                rollback_entrypoint._run_authorized_empty_store_rollback_synthetic(
                    verified_rollback_capability=self.capability,
                    verified_package_capability=self.package_capability,
                    verified_controller_runtime_capability=self.runtime_capability,
                    eligibility_receipt=self.eligibility,
                    resources=self.resources,
                    authority_state=self.state,
                    clock=_Clock(),
                    held_lock=wrong_lock.held_capability(),
                    journal_factory=factory,
                    operations=operations,
                    receipt_store=self.receipt_store,
                )
        finally:
            wrong_lock.close()
        self.assertFalse(eligibility_path.exists())
        self.assertIsNone(factory.journal)
        self.assertEqual(operations.apply_count, {})

    def test_caller_controlled_ledger_sequence_is_not_verification(self) -> None:
        factory = _JournalFactory()
        operations = self._operations()
        operations.return_unverified_sequence = True

        with self.assertRaisesRegex(
            EmptyRollbackExecutionError,
            "install_ledger_capability_invalid",
        ):
            self._run(factory, operations)

        self.assertEqual(operations.apply_count, {})

    def test_runtime_capability_and_observed_tree_must_match_authorized_release(self) -> None:
        runtime = verified_controller_runtime_evidence(self.runtime_capability)
        mismatched = _VerifiedControllerRuntimeCapability(
            replace(runtime, release_tree_sha256="0" * 64),
            _RUNTIME_TOKEN,
        )
        factory = _JournalFactory()
        operations = self._operations()
        with (
            patch.object(rollback_entrypoint, "AUTHORITY_STATE_PATH", self.state_path),
            patch.object(rollback_entrypoint, "GLOBAL_LOCK_PATH", self.lock_path),
            self.assertRaisesRegex(
                EmptyRollbackExecutionError,
                "controller_runtime_binding_mismatch",
            ),
        ):
            rollback_entrypoint._run_authorized_empty_store_rollback_synthetic(
                verified_rollback_capability=self.capability,
                verified_package_capability=self.package_capability,
                verified_controller_runtime_capability=mismatched,
                eligibility_receipt=self.eligibility,
                resources=self.resources,
                authority_state=self.state,
                clock=_Clock(),
                held_lock=self.lock.held_capability(),
                journal_factory=factory,
                operations=operations,
                receipt_store=self.receipt_store,
            )
        self.assertIsNone(factory.journal)
        self.assertEqual(operations.apply_count, {})

        factory = _JournalFactory()
        operations = self._operations()
        operations.tamper_runtime_tree_step = (
            "R05_DISABLE_AND_REMOVE_STORES_SUPERVISOR"
        )
        with self.assertRaisesRegex(
            EmptyRollbackExecutionError,
            "resource_drift",
        ):
            self._run(factory, operations)
        self.assertEqual(operations.apply_count, {})

    def test_retained_install_receipt_must_be_exact_canonical_bytes(self) -> None:
        with self.assertRaisesRegex(
            EmptyRollbackExecutionError,
            "not_canonical",
        ):
            rollback_entrypoint.verify_retained_install_receipt_and_ledger(
                self.install_receipt_bytes + b"\n",
                self.resources,
            )

        alternate = build_install_receipt(
            execution_id="3" * 64,
            attempt_id="install-" + "4" * 40,
            candidate_git_commit=self.commit,
            candidate_git_tree=self.tree,
            package_manifest_sha256=self.package_sha,
            controller_runtime_receipt_sha256=str(
                self.install_receipt["controller_runtime_receipt_sha256"]
            ),
            controller_runtime_root=str(
                self.install_receipt["controller_runtime_root"]
            ),
            controller_runtime_tree_sha256=str(
                self.install_receipt["controller_runtime_tree_sha256"]
            ),
            controller_release_root=str(
                self.install_receipt["controller_release_root"]
            ),
            controller_release_tree_sha256=str(
                self.install_receipt["controller_release_tree_sha256"]
            ),
            controller_release_package_manifest_path=str(
                self.install_receipt[
                    "controller_release_package_manifest_path"
                ]
            ),
            controller_runtime_interpreter_path=str(
                self.install_receipt["controller_runtime_interpreter_path"]
            ),
            controller_runtime_interpreter_sha256=str(
                self.install_receipt[
                    "controller_runtime_interpreter_sha256"
                ]
            ),
            controller_runtime_inventory_path=str(
                self.install_receipt["controller_runtime_inventory_path"]
            ),
            controller_runtime_inventory_sha256=str(
                self.install_receipt[
                    "controller_runtime_inventory_sha256"
                ]
            ),
            controller_requirements_lock_sha256=str(
                self.install_receipt["controller_requirements_lock_sha256"]
            ),
            supervisor_launcher_path=str(
                self.install_receipt["supervisor_launcher_path"]
            ),
            supervisor_launcher_sha256=str(
                self.install_receipt["supervisor_launcher_sha256"]
            ),
            authorization_sha256="6" * 64,
            plan_sha256="7" * 64,
            journal_head_sha256="8" * 64,
            journal_sequence=41,
            resource_ledger_head_sha256=str(
                self.eligibility["resource_ledger_head_sha256"]
            ),
            resource_ledger_sequence=len(ROLLBACK_RESOURCE_KEYS),
            image_identity_set_sha256="a" * 64,
            postflight_receipt_sha256="d" * 64,
            terminal_store_readiness_sha256="c" * 64,
        )
        operations = self._operations()
        operations.canonical_install_receipt = (
            rollback_entrypoint.canonical_json_bytes(alternate)
        )
        with self.assertRaisesRegex(
            EmptyRollbackExecutionError,
            "install_ledger_capability_mismatch",
        ):
            self._run(_JournalFactory(), operations)
        self.assertEqual(operations.apply_count, {})

    def test_completed_replay_revalidates_retained_evidence_and_absence(
        self,
    ) -> None:
        factory = _JournalFactory()
        operations = self._operations()
        self._run(factory, operations)

        alternate = dict(self.install_receipt)
        alternate["postflight_receipt_sha256"] = "d" * 64
        alternate["receipt_sha256"] = receipt_sha256(alternate)
        operations.canonical_install_receipt = (
            rollback_entrypoint.canonical_json_bytes(alternate)
        )
        with self.assertRaisesRegex(
            EmptyRollbackExecutionError,
            "install_ledger_capability_mismatch",
        ):
            self._run(factory, operations)

        operations.canonical_install_receipt = self.install_receipt_bytes
        operations.states["R08_REMOVE_EXACT_QDRANT_CONTAINER"] = "before"
        with self.assertRaisesRegex(
            EmptyRollbackExecutionError,
            "completed_state_drift",
        ):
            self._run(factory, operations)

    def test_first_completion_rejects_install_receipt_tampered_after_r03(
        self,
    ) -> None:
        factory = _JournalFactory()
        operations = self._operations()
        alternate = dict(self.install_receipt)
        alternate["postflight_receipt_sha256"] = "d" * 64
        alternate["receipt_sha256"] = receipt_sha256(alternate)
        operations.tampered_install_receipt_after_r03 = (
            rollback_entrypoint.canonical_json_bytes(alternate)
        )

        with self.assertRaisesRegex(
            EmptyRollbackExecutionError,
            "install_ledger_capability_mismatch",
        ):
            self._run(factory, operations)

        self.assertEqual(
            factory.journal.records()[-1].event,  # type: ignore[union-attr]
            RollbackEvent.COMPLETE.value,
        )
        self.assertEqual(operations.marker_releases, 0)
        self.assertIsNotNone(operations.active_controller_authority_marker_sha256)

    def test_first_completion_rejects_retained_artifact_removed_after_r03(
        self,
    ) -> None:
        factory = _JournalFactory()
        operations = self._operations()
        operations.remove_retained_audit_key_after_r03 = "authority_anchor"

        with self.assertRaisesRegex(
            EmptyRollbackExecutionError,
            "retained_audit_evidence_invalid",
        ):
            self._run(factory, operations)

        self.assertEqual(
            factory.journal.records()[-1].event,  # type: ignore[union-attr]
            RollbackEvent.COMPLETE.value,
        )
        self.assertEqual(operations.marker_releases, 0)

    def test_effect_before_applied_record_recovers_without_repeating(self) -> None:
        factory = _JournalFactory()
        operations = self._operations()

        original_factory = factory.__call__

        def configure(claimed: object) -> _Journal:
            journal = original_factory(claimed)
            if not journal.items:
                journal.fail_before_applied_step = (
                    "R08_REMOVE_EXACT_QDRANT_CONTAINER"
                )
            return journal

        with self.assertRaises(EmptyRollbackExecutionError):
            with (
                patch.object(rollback_entrypoint, "AUTHORITY_STATE_PATH", self.state_path),
                patch.object(rollback_entrypoint, "GLOBAL_LOCK_PATH", self.lock_path),
            ):
                rollback_entrypoint._run_authorized_empty_store_rollback_synthetic(
                    verified_rollback_capability=self.capability,
                    verified_package_capability=self.package_capability,
                    verified_controller_runtime_capability=self.runtime_capability,
                    eligibility_receipt=self.eligibility,
                    resources=self.resources,
                    authority_state=self.state,
                    clock=_Clock(),
                    held_lock=self.lock.held_capability(),
                    journal_factory=configure,
                    operations=operations,
                    receipt_store=self.receipt_store,
                )
        before = operations.apply_count["R08_REMOVE_EXACT_QDRANT_CONTAINER"]
        receipt = self._run(factory, operations)
        self.assertEqual(receipt.outcome, "empty_store_rollback_complete")
        self.assertEqual(
            operations.apply_count["R08_REMOVE_EXACT_QDRANT_CONTAINER"], before
        )

    def test_durable_append_then_anchor_error_does_not_duplicate_record(self) -> None:
        factory = _JournalFactory()
        operations = self._operations()
        original_factory = factory.__call__

        def configure(claimed: object) -> _Journal:
            journal = original_factory(claimed)
            if not journal.items:
                journal.fail_after_durable_step = (
                    "R09_REMOVE_EXACT_POSTGRES_CONTAINER"
                )
            return journal

        with (
            patch.object(rollback_entrypoint, "AUTHORITY_STATE_PATH", self.state_path),
            patch.object(rollback_entrypoint, "GLOBAL_LOCK_PATH", self.lock_path),
        ):
            receipt = rollback_entrypoint._run_authorized_empty_store_rollback_synthetic(
                verified_rollback_capability=self.capability,
                verified_package_capability=self.package_capability,
                verified_controller_runtime_capability=self.runtime_capability,
                eligibility_receipt=self.eligibility,
                resources=self.resources,
                authority_state=self.state,
                clock=_Clock(),
                held_lock=self.lock.held_capability(),
                journal_factory=configure,
                operations=operations,
                receipt_store=self.receipt_store,
            )
        self.assertEqual(receipt.outcome, "empty_store_rollback_complete")
        records = factory.journal.records()  # type: ignore[union-attr]
        self.assertEqual(
            sum(
                item.step_id == "R09_REMOVE_EXACT_POSTGRES_CONTAINER"
                and item.event == RollbackEvent.APPLIED.value
                for item in records
            ),
            1,
        )

    def test_nonempty_under_live_marker_stops_before_any_destructive_step(
        self,
    ) -> None:
        factory = _JournalFactory()

        class _NonemptyUnderLiveStoreMarker(_Operations):
            def observe_empty_eligibility(
                self,
                request,
                controller_authority_marker_capability,
            ):
                receipt = super().observe_empty_eligibility(
                    request,
                    controller_authority_marker_capability,
                )
                if request.step.step_id == (
                    "R06_ESTABLISH_ADMINISTRATIVE_WRITER_FENCE_AND_RECHECK_SEMANTIC_EMPTY"
                ):
                    receipt["qdrant_points"] = 1
                    receipt["receipt_sha256"] = eligibility_receipt_sha256(receipt)
                return receipt

        operations = _NonemptyUnderLiveStoreMarker(
            self.eligibility,
            self.install_receipt_bytes,
            self.resources,
        )
        with self.assertRaises(Exception):
            self._run(factory, operations)
        self.assertEqual(
            operations.apply_count,
            {"R05_DISABLE_AND_REMOVE_STORES_SUPERVISOR": 1},
        )

    def test_new_writer_after_store_stop_is_refused_inside_delete_boundary(
        self,
    ) -> None:
        factory = _JournalFactory()
        operations = self._operations()
        operations.inject_writer_after_store_stop = True
        with self.assertRaisesRegex(
            EmptyRollbackExecutionError, "empty_rollback_effect_failed"
        ):
            self._run(factory, operations)
        self.assertEqual(
            operations.apply_count,
            {"R05_DISABLE_AND_REMOVE_STORES_SUPERVISOR": 1},
        )
        self.assertNotIn(
            "R08_REMOVE_EXACT_QDRANT_CONTAINER", operations.states
        )
        self.assertEqual(operations.marker_releases, 0)
        self.assertIsNotNone(operations.active_controller_authority_marker_sha256)

    def test_unowned_effect_and_observation_tamper_fail_closed(self) -> None:
        factory = _JournalFactory()
        operations = self._operations()
        operations.states["R08_REMOVE_EXACT_QDRANT_CONTAINER"] = "after"
        with self.assertRaisesRegex(
            EmptyRollbackExecutionError, "unowned_prior_effect"
        ):
            self._run(factory, operations)

        self.state = AuthorityState(self.state_path)
        self.capability = self._capability(nonce="O" * 32)
        factory = _JournalFactory()
        operations = self._operations()
        operations.tamper_ownership_step = "R08_REMOVE_EXACT_QDRANT_CONTAINER"
        with self.assertRaisesRegex(
            EmptyRollbackExecutionError, "resource_drift"
        ):
            self._run(factory, operations)

    def test_journal_validator_rejects_applied_without_intent(self) -> None:
        record = RollbackJournalRecord.create(
            plan_sha256="a" * 64,
            attempt_id="rollback-" + "b" * 40,
            sequence=1,
            step_id="R01_REVERIFY_GLOBAL_LOCK",
            event=RollbackEvent.APPLIED,
            prior_record_sha256="0" * 64,
        )
        with self.assertRaisesRegex(
            EmptyRollbackExecutionError, "applied_without_intent"
        ):
            validate_rollback_records(
                (record,),
                plan_sha256="a" * 64,
                attempt_id="rollback-" + "b" * 40,
            )

    def test_unsealed_caller_target_list_is_rejected_before_factory(self) -> None:
        _evidence, exact = verified_rollback_resource_parts(self.resources)
        caller_resources = list(exact)
        operations = self._operations()
        index = ROLLBACK_RESOURCE_KEYS.index(
            "canonical_database_and_roles"
        )
        caller_resources[index] = caller_resources[-1]
        with (
            patch.object(rollback_entrypoint, "AUTHORITY_STATE_PATH", self.state_path),
            patch.object(rollback_entrypoint, "GLOBAL_LOCK_PATH", self.lock_path),
        ):
            with self.assertRaisesRegex(
                EmptyRollbackExecutionError, "empty_rollback_plan_refused"
            ):
                run_authorized_empty_store_rollback(
                    verified_rollback_capability=self.capability,
                    verified_package_capability=self.package_capability,
                    verified_controller_runtime_capability=self.runtime_capability,
                    eligibility_receipt=self.eligibility,
                    resources=caller_resources,
                    authority_state=self.state,
                    clock=_Clock(),
                    held_lock=self.lock.held_capability(),
                    resolved_store_spec={},
                    receipt_store=self.receipt_store,
                )
        self.assertEqual(operations.apply_count, {})

    def test_concrete_durable_journal_completes_and_reopens_exactly(self) -> None:
        operations = self._operations()
        journal_template = str(
            Path(self.temporary.name)
            / "executions"
            / "{execution_id}"
            / "rollback.jsonl"
        )

        def durable_factory(claimed: object) -> DurableRollbackJournal:
            evidence = claimed_empty_rollback_evidence(claimed)
            path = Path(evidence.journal_path)
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(path.parent, 0o700)
            return DurableRollbackJournal(
                path,
                claimed_rollback=claimed,
                authority_state=self.state,
                held_lock=self.lock.held_capability(),
                create=not path.exists(),
            )

        with (
            patch.object(rollback_entrypoint, "AUTHORITY_STATE_PATH", self.state_path),
            patch.object(rollback_entrypoint, "GLOBAL_LOCK_PATH", self.lock_path),
            patch.object(
                rollback_entrypoint,
                "ROLLBACK_JOURNAL_TEMPLATE",
                journal_template,
            ),
        ):
            receipt = _run_authorized_empty_store_rollback_durable_synthetic(
                verified_rollback_capability=self.capability,
                verified_package_capability=self.package_capability,
                verified_controller_runtime_capability=self.runtime_capability,
                eligibility_receipt=self.eligibility,
                resources=self.resources,
                authority_state=self.state,
                clock=_Clock(),
                held_lock=self.lock.held_capability(),
                journal_factory=durable_factory,
                operations=operations,
                receipt_store=self.receipt_store,
            )
            resumed = _run_authorized_empty_store_rollback_durable_synthetic(
                verified_rollback_capability=self.capability,
                verified_package_capability=self.package_capability,
                verified_controller_runtime_capability=self.runtime_capability,
                eligibility_receipt=self.eligibility,
                resources=self.resources,
                authority_state=self.state,
                clock=_Clock(),
                held_lock=self.lock.held_capability(),
                journal_factory=durable_factory,
                operations=operations,
                receipt_store=self.receipt_store,
            )
        self.assertEqual(receipt.outcome, "empty_store_rollback_complete")
        self.assertEqual(
            resumed.outcome, "empty_store_rollback_already_complete"
        )
        path = Path(journal_template.replace("{execution_id}", receipt.execution_id))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        records = parse_rollback_journal_bytes(
            path.read_bytes(),
            plan_sha256=receipt.plan_sha256,
            attempt_id=receipt.attempt_id,
        )
        self.assertEqual(len(records), receipt.journal_sequence)
        self.assertEqual(records[-1].record_sha256, receipt.journal_head_sha256)

    def test_public_entrypoint_rejects_synthetic_receipt_store(self) -> None:
        with (
            patch.object(rollback_entrypoint, "AUTHORITY_STATE_PATH", self.state_path),
            patch.object(rollback_entrypoint, "GLOBAL_LOCK_PATH", self.lock_path),
        ):
            with self.assertRaisesRegex(
                EmptyRollbackExecutionError,
                "receipt_store_invalid",
            ):
                run_authorized_empty_store_rollback(
                    verified_rollback_capability=self.capability,
                    verified_package_capability=self.package_capability,
                    verified_controller_runtime_capability=self.runtime_capability,
                    eligibility_receipt=self.eligibility,
                    resources=self.resources,
                    authority_state=self.state,
                    clock=_Clock(),
                    held_lock=self.lock.held_capability(),
                    resolved_store_spec={},
                    receipt_store=self.receipt_store,
                )

    def test_public_entrypoint_exposes_no_factory_or_operation_injection_surface(
        self,
    ) -> None:
        parameters = set(
            inspect.signature(
                run_authorized_empty_store_rollback
            ).parameters
        )
        self.assertEqual(
            parameters,
            {
                "verified_rollback_capability",
                "verified_package_capability",
                "verified_controller_runtime_capability",
                "eligibility_receipt",
                "resources",
                "authority_state",
                "clock",
                "held_lock",
                "resolved_store_spec",
                "receipt_store",
            },
        )
        self.assertTrue(
            {
                "journal_factory",
                "operations",
                "transport_factory",
                "receipt_sink",
                "audit_source",
                "deletion_driver",
            }.isdisjoint(parameters)
        )

    def test_public_entrypoint_assembles_bound_operations_after_durable_journal(
        self,
    ) -> None:
        operations = self._operations()
        captured: dict[str, dict[str, object]] = {}
        selected_journals: list[DurableRollbackJournal] = []
        journal_template = str(
            Path(self.temporary.name)
            / "executions"
            / "{execution_id}"
            / "rollback.jsonl"
        )

        def durable_factory(claimed: object) -> DurableRollbackJournal:
            evidence = claimed_empty_rollback_evidence(claimed)
            path = Path(evidence.journal_path)
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(path.parent, 0o700)
            return DurableRollbackJournal(
                path,
                claimed_rollback=claimed,
                authority_state=self.state,
                held_lock=self.lock.held_capability(),
                create=True,
            )

        class _BoundProductionFactory:
            def __init__(inner_self, **kwargs: object) -> None:
                captured["init"] = dict(kwargs)

            def __call__(inner_self, **kwargs: object) -> object:
                captured["call"] = dict(kwargs)
                self.assertIsInstance(
                    kwargs.get("rollback_journal"), DurableRollbackJournal
                )
                return operations

        resolved_store_spec = {"verified": "ledger-bound"}
        held_lock = self.lock.held_capability()

        def production_journal(
            claimed: object,
            *,
            authority_state: AuthorityState,
            held_lock: object,
        ) -> DurableRollbackJournal:
            self.assertIs(authority_state, self.state)
            self.assertIs(held_lock, selected_held_lock)
            journal = durable_factory(claimed)
            selected_journals.append(journal)
            return journal

        selected_held_lock = held_lock
        with (
            patch.object(rollback_entrypoint, "AUTHORITY_STATE_PATH", self.state_path),
            patch.object(rollback_entrypoint, "GLOBAL_LOCK_PATH", self.lock_path),
            patch.object(
                rollback_entrypoint,
                "ROLLBACK_JOURNAL_TEMPLATE",
                journal_template,
            ),
            patch.object(
                DurableReceiptStore,
                "require_production_binding",
                return_value=None,
            ),
            patch.object(
                rollback_entrypoint,
                "_production_rollback_journal",
                side_effect=production_journal,
            ) as selected_journal_factory,
            patch.object(
                rollback_live_adapter,
                "ProductionLinuxEmptyRollbackOperationsFactory",
                _BoundProductionFactory,
            ),
        ):
            receipt = run_authorized_empty_store_rollback(
                verified_rollback_capability=self.capability,
                verified_package_capability=self.package_capability,
                verified_controller_runtime_capability=self.runtime_capability,
                eligibility_receipt=self.eligibility,
                resources=self.resources,
                authority_state=self.state,
                clock=_Clock(),
                held_lock=selected_held_lock,
                resolved_store_spec=resolved_store_spec,
                receipt_store=self.receipt_store,
            )
        self.assertEqual(receipt.outcome, "empty_store_rollback_complete")
        self.assertEqual(
            set(captured["init"]),
            {
                "held_lock",
                "verified_controller_runtime_capability",
                "authority_state",
                "receipt_store",
            },
        )
        self.assertEqual(
            set(captured["call"]),
            {
                "claimed_rollback",
                "verified_rollback_capability",
                "signed_eligibility",
                "resolved_store_spec",
                "verified_artifacts",
                "verified_resources",
                "installation_receipt",
                "rollback_journal",
            },
        )
        self.assertIs(
            captured["call"]["resolved_store_spec"], resolved_store_spec
        )
        self.assertEqual(selected_journal_factory.call_count, 1)
        self.assertIs(
            captured["call"]["rollback_journal"],
            selected_journals[0],
        )

    def test_durable_synthetic_path_rejects_volatile_duck_typed_journal(
        self,
    ) -> None:
        factory = _JournalFactory()
        operations = self._operations()
        with (
            patch.object(rollback_entrypoint, "AUTHORITY_STATE_PATH", self.state_path),
            patch.object(rollback_entrypoint, "GLOBAL_LOCK_PATH", self.lock_path),
        ):
            with self.assertRaisesRegex(
                EmptyRollbackExecutionError,
                "durable_journal_required",
            ):
                _run_authorized_empty_store_rollback_durable_synthetic(
                    verified_rollback_capability=self.capability,
                    verified_package_capability=self.package_capability,
                    verified_controller_runtime_capability=self.runtime_capability,
                    eligibility_receipt=self.eligibility,
                    resources=self.resources,
                    authority_state=self.state,
                    clock=_Clock(),
                    held_lock=self.lock.held_capability(),
                    journal_factory=factory,
                    operations=operations,
                    receipt_store=self.receipt_store,
                )
        self.assertEqual(operations.apply_count, {})

if __name__ == "__main__":
    unittest.main()

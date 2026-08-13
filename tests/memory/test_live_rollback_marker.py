from __future__ import annotations

import hashlib
import unittest

from tools.governed_memory_install.live_rollback_marker import (
    ACQUISITION_FILENAME,
    ClosedLinuxRootControllerAuthorityMarkerFileStore,
    DurableRootLiveRollbackMarkerTransport,
    LiveRollbackMarkerTransportError,
    RootControllerAuthorityMarkerFileObservation,
    SEMANTIC_EMPTY_FILENAME,
)
from tools.governed_memory_install.linux_live_adapters import (
    ExactRootRecordObservation,
)
from tools.governed_memory_install.linux_live_transports import (
    ObservationState,
    RootFileSlot,
    RootRemovalIdentity,
    TypedObservation,
)
from tools.governed_memory_install.rollback import EMPTY_ROLLBACK_STEPS
from tools.governed_memory_install.rollback_entrypoint import (
    RollbackOperationRequest,
)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _request() -> RollbackOperationRequest:
    package_sha256 = _hash("package")
    runtime_receipt_sha256 = _hash("runtime-receipt")
    return RollbackOperationRequest(
        step=EMPTY_ROLLBACK_STEPS[3],
        resource=None,
        execution_id=_hash("rollback-execution"),
        attempt_id="rollback-" + "a" * 40,
        journal_binding_sha256=_hash("journal-binding"),
        plan_sha256=_hash("rollback-plan"),
        package_manifest_sha256=package_sha256,
        controller_runtime_receipt_sha256=runtime_receipt_sha256,
        controller_runtime_root=(
            "/opt/governed-memory-controller/runtimes/"
            + runtime_receipt_sha256
        ),
        controller_runtime_tree_sha256=_hash("runtime-tree"),
        controller_release_root=(
            "/opt/governed-memory-controller/releases/" + package_sha256
        ),
        controller_release_tree_sha256=_hash("release-tree"),
        controller_release_package_manifest_path=(
            "/opt/governed-memory-controller/releases/"
            + package_sha256
            + "/ops/governed_memory/installation/current/"
            "package_manifest.json"
        ),
        controller_runtime_interpreter_path=(
            "/opt/governed-memory-controller/runtimes/"
            + runtime_receipt_sha256
            + "/bin/python"
        ),
        controller_runtime_interpreter_sha256=_hash("interpreter"),
        controller_runtime_inventory_path=(
            "/opt/governed-memory-controller/runtimes/"
            + runtime_receipt_sha256
            + "/controller-distributions.json"
        ),
        controller_runtime_inventory_sha256=_hash("inventory"),
        controller_requirements_lock_sha256=_hash("requirements-lock"),
        supervisor_launcher_path=(
            "/opt/governed-memory-controller/releases/"
            + package_sha256
            + "/tools/governed_memory_install/"
            "store_supervisor_launcher.py"
        ),
        supervisor_launcher_sha256=_hash("supervisor-launcher"),
        installation_execution_id=_hash("installation-execution"),
        installation_receipt_sha256=_hash("installation-receipt"),
        recovery_reservation_claim_sha256="0" * 64,
        retained_evidence_sha256=_hash("retained-evidence"),
        eligibility_receipt_sha256=_hash("eligibility-receipt"),
        resource_ledger_binding_sha256=_hash("ledger-binding"),
        resource_ledger_head_sha256=_hash("ledger-head"),
        resource_ledger_sequence=22,
        exact_targets_sha256=_hash("exact-targets"),
    )


class _MemoryStore:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str], bytes] = {}
        self.removed = False

    @staticmethod
    def _observation(content: bytes) -> RootControllerAuthorityMarkerFileObservation:
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
        if key in self.files and self.files[key] != expected:
            raise LiveRollbackMarkerTransportError("synthetic_mismatch")
        self.files.setdefault(key, expected)
        return self._observation(self.files[key])

    def read_optional_exact(self, execution_id, filename, expected):
        raw = self.files.get((execution_id, filename))
        if raw is None:
            return None
        if expected is not None and raw != expected:
            raise LiveRollbackMarkerTransportError("synthetic_mismatch")
        return self._observation(raw)

    def remove_exact_execution(
        self,
        execution_id,
        acquisition,
        semantic_empty,
    ):
        expected = {
            ACQUISITION_FILENAME: acquisition,
            SEMANTIC_EMPTY_FILENAME: semantic_empty,
        }
        for filename, raw in expected.items():
            if self.files.get((execution_id, filename)) != raw:
                raise LiveRollbackMarkerTransportError("synthetic_mismatch")
        for filename in expected:
            del self.files[(execution_id, filename)]
        self.removed = True


class _CrashableRemovalEffects:
    """Minimal exact-record effects used to exercise release-prefix replay."""

    def __init__(self, execution_id: str) -> None:
        self.execution_id = execution_id
        self.files: dict[RootFileSlot, bytes] = {}
        self.removals: list[RootFileSlot] = []
        self.crash_after_first_removal = False

    def _identity(self, slot: RootFileSlot, raw: bytes) -> RootRemovalIdentity:
        ordinal = 1 if slot is RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER else 2
        return RootRemovalIdentity(
            slot=slot,
            device=1,
            inode=ordinal,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            execution_id=self.execution_id,
        )

    def observe_execution_record(self, slot, expected_content_sha256):
        raw = self.files.get(slot)
        if raw is None:
            return ExactRootRecordObservation(
                TypedObservation(
                    ObservationState.ABSENT,
                    "verified_absent",
                    _hash("absent-" + slot.value),
                ),
                None,
            )
        identity = self._identity(slot, raw)
        state = (
            ObservationState.EXACT
            if identity.content_sha256 == expected_content_sha256
            else ObservationState.DRIFT
        )
        return ExactRootRecordObservation(
            TypedObservation(
                state,
                "verified_exact" if state is ObservationState.EXACT else "verified_drift",
                _hash("evidence-" + slot.value),
                _hash("identity-" + slot.value),
            ),
            identity if state is ObservationState.EXACT else None,
        )

    def create_execution_record(self, slot, canonical_document):
        if slot in self.files:
            raise RuntimeError("synthetic_create_collision")
        self.files[slot] = canonical_document
        return self._identity(slot, canonical_document)

    def read_execution_record(self, slot, expected_content_sha256):
        observed = self.observe_execution_record(slot, expected_content_sha256)
        if observed.observation.state is ObservationState.ABSENT:
            return None
        if observed.observation.state is not ObservationState.EXACT:
            raise RuntimeError("synthetic_read_drift")
        return self.files[slot]

    def remove_execution_record(self, identity):
        raw = self.files.get(identity.slot)
        if raw is None or self._identity(identity.slot, raw) != identity:
            raise RuntimeError("synthetic_remove_drift")
        del self.files[identity.slot]
        self.removals.append(identity.slot)
        if self.crash_after_first_removal and len(self.removals) == 1:
            raise RuntimeError("synthetic_release_crash")


class LiveControllerAuthorityMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = _request()
        self.store = _MemoryStore()
        self.transport = DurableRootLiveRollbackMarkerTransport(self.store)
        self.postgres_identity = _hash("postgres-marker-bound")
        self.qdrant_identity = _hash("qdrant-marker-bound")
        self.semantic_state_sha256 = _hash("semantic-empty-state")

    def acquire(self):
        return self.transport.acquire_or_recover(
            self.request,
            marker_bound_postgres_identity_sha256=(
                self.postgres_identity
            ),
            marker_bound_qdrant_identity_sha256=self.qdrant_identity,
            expected_semantic_empty_state_sha256=(
                self.semantic_state_sha256
            ),
        )

    def test_create_recover_seal_assert_and_release(self) -> None:
        acquired = self.acquire()
        self.assertIsNone(acquired.semantic_empty_state_sha256)
        self.assertEqual(self.acquire(), acquired)
        semantic_sha256 = self.semantic_state_sha256
        held = self.transport.persist_semantic_empty_observation(
            self.request,
            acquired,
            semantic_sha256,
        )
        self.assertEqual(
            held.semantic_empty_state_sha256,
            semantic_sha256,
        )
        self.assertEqual(self.transport.assert_held(self.request, held), held)
        self.assertEqual(self.acquire(), held)
        self.transport.release_after_receipt(self.request, held)
        self.assertTrue(self.store.removed)
        self.assertEqual(self.store.files, {})

    def test_release_before_semantic_proof_refused(self) -> None:
        with self.assertRaisesRegex(
            LiveRollbackMarkerTransportError,
            "release_without_semantic_proof",
        ):
            self.transport.release_after_receipt(
                self.request,
                self.acquire(),
            )

    def test_existing_acquisition_drift_refused(self) -> None:
        self.acquire()
        key = (self.request.execution_id, ACQUISITION_FILENAME)
        self.store.files[key] = self.store.files[key][:-1] + b"X"
        with self.assertRaises(LiveRollbackMarkerTransportError):
            self.acquire()

    def test_semantic_rewrite_refused(self) -> None:
        acquired = self.acquire()
        self.transport.persist_semantic_empty_observation(
            self.request,
            acquired,
            self.semantic_state_sha256,
        )
        with self.assertRaises(LiveRollbackMarkerTransportError):
            self.transport.persist_semantic_empty_observation(
                self.request,
                acquired,
                _hash("different-empty-observation"),
            )

    def test_linux_store_release_prefix_replays_after_first_unlink_crash(self) -> None:
        effects = _CrashableRemovalEffects(self.request.execution_id)
        store = object.__new__(ClosedLinuxRootControllerAuthorityMarkerFileStore)
        store._effects = effects
        store._execution_id = self.request.execution_id
        acquisition = b"acquisition\n"
        semantic = b"semantic\n"
        effects.files = {
            RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER: acquisition,
            RootFileSlot.ROLLBACK_SEMANTIC_EMPTY_PROOF: semantic,
        }
        effects.crash_after_first_removal = True

        with self.assertRaisesRegex(RuntimeError, "synthetic_release_crash"):
            store.remove_exact_execution(
                self.request.execution_id,
                acquisition,
                semantic,
            )
        self.assertEqual(
            effects.removals,
            [RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER],
        )
        self.assertNotIn(RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER, effects.files)
        self.assertIn(
            RootFileSlot.ROLLBACK_SEMANTIC_EMPTY_PROOF,
            effects.files,
        )

        effects.crash_after_first_removal = False
        store.create_or_read_exact(
            self.request.execution_id,
            ACQUISITION_FILENAME,
            acquisition,
        )
        store.remove_exact_execution(
            self.request.execution_id,
            acquisition,
            semantic,
        )
        self.assertEqual(effects.files, {})
        self.assertEqual(
            effects.removals,
            [
                RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER,
                RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER,
                RootFileSlot.ROLLBACK_SEMANTIC_EMPTY_PROOF,
            ],
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from dataclasses import replace
import hashlib
import inspect
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.governed_memory_install.durable_receipts import (
    DurableReceiptStore,
    ReceiptArtifact,
)
from tools.governed_memory_install.linux_plan import canonical_labels_sha256
from tools.governed_memory_install.linux_store_readiness import (
    CanonicalPostgreSQLRole,
    PostgreSQLCatalogIdentity,
    PostgreSQLRoleMembership,
    QdrantCollectionConfiguration,
    TerminalPostgreSQLSnapshot,
    TerminalQdrantSnapshot,
)
from tools.governed_memory_install.postgres_native_stages import _receipt
from tools.governed_memory_install.receipts import build_install_receipt
from tools.governed_memory_install.resource_identity import load_ledger
from tools.governed_memory_install.rollback import (
    EMPTY_ROLLBACK_STEPS,
    RETAINED_AUDIT_KEYS,
    ROLLBACK_RESOURCE_KEYS,
    build_empty_rollback_eligibility_receipt,
    derive_exact_rollback_resources_from_ledger,
    expected_rollback_resource_names,
    verified_rollback_resource_parts,
)
from tools.governed_memory_install import rollback_entrypoint
from tools.governed_memory_install.rollback_entrypoint import (
    EmptyRollbackControllerAuthorityMarkerEvidence,
    RollbackOperationRequest,
)
from tools.governed_memory_install.rollback_live_adapter import (
    ClosedLinuxLiveEmptyEligibilityProbe,
    DurableLiveRollbackMarkerObservation,
    ExactPhysicalEmptyRollbackOperations,
    LedgerBoundPhysicalTarget,
    PhysicalResourceObservation,
    PhysicalRollbackAdapterError,
    ProductionLinuxEmptyRollbackOperationsFactory,
    StoreBackedPostgreSQLStageReceiptSink,
    _ProductionControllerAuthorityMarkerState,
    physical_rollback_bindings_from_verified_ledger,
)
from tools.governed_memory_install.store_readiness import (
    COLLECTION,
    POSTGRES_BIND,
    POSTGRES_SERVER_VERSION,
    QDRANT_BIND,
    QDRANT_SERVER_VERSION,
    REQUIRED_ROLE_NAMES,
    TERMINAL_MIGRATION_IDS,
)
from tests.memory.resource_identity_test_support import append_resource_identity


EXECUTION_ID = "3" * 64
PACKAGE_SHA = "d" * 64
COMMIT = "b" * 40
TREE = "c" * 40


def _terminal_postgres() -> TerminalPostgreSQLSnapshot:
    return TerminalPostgreSQLSnapshot(
        bind=POSTGRES_BIND,
        server_major=16,
        server_version=POSTGRES_SERVER_VERSION,
        database="governed_memory",
        applied_migration_ids=TERMINAL_MIGRATION_IDS,
        roles=tuple(
            CanonicalPostgreSQLRole(
                role_name=name,
                can_login=False,
                inherit=False,
                superuser=False,
                create_database=False,
                create_role=False,
                replication=False,
                bypass_rls=False,
            )
            for name in REQUIRED_ROLE_NAMES
        ),
        memberships=(
            PostgreSQLRoleMembership(
                "governed_memory_owner", "governed_memory_bootstrap"
            ),
        ),
        catalog_identities=(
            PostgreSQLCatalogIdentity(
                "schema", "memory", "memory", "governed_memory_owner", "a" * 64
            ),
        ),
        governed_user_row_count=0,
        active_client_count=0,
        source_connection_count=0,
    )


def _terminal_qdrant() -> TerminalQdrantSnapshot:
    return TerminalQdrantSnapshot(
        bind=QDRANT_BIND,
        server_version=QDRANT_SERVER_VERSION,
        collection_exists=True,
        alias_target=COLLECTION,
        collection_config=QdrantCollectionConfiguration(
            vector_size=3072,
            distance="Dot",
            on_disk_payload=True,
            replication_factor=1,
        ),
        point_count=0,
        unexpected_candidate_collection_count=0,
        source_endpoint_count=0,
    )


class _TerminalStore:
    def __init__(self, bind: str, snapshot: object) -> None:
        self.bind = bind
        self.snapshot = snapshot

    def inspect_terminal(self) -> object:
        return self.snapshot


class _Driver:
    def __init__(self, targets: tuple[LedgerBoundPhysicalTarget, ...]) -> None:
        self.targets = {item.resource_key: item for item in targets}
        self.states = {
            item.resource_key: (
                "running" if item.resource_kind == "container" else "present"
            )
            for item in targets
        }
        self.labels = {
            item.resource_key: (
                (("governed-memory.resource", item.resource_key),)
                if item.resource_labels_sha256 is not None
                else ()
            )
            for item in targets
        }
        self.calls: list[tuple[str, str]] = []

    def observe(self, target: LedgerBoundPhysicalTarget) -> PhysicalResourceObservation:
        state = self.states[target.resource_key]
        if state == "absent":
            return PhysicalResourceObservation(
                resource_key=target.resource_key,
                resource_kind=target.resource_kind,
                resource_name=target.resource_name,
                state="absent",
                resource_id=None,
            )
        return PhysicalResourceObservation(
            resource_key=target.resource_key,
            resource_kind=target.resource_kind,
            resource_name=target.resource_name,
            state=state,
            resource_id=target.resource_id,
            labels=self.labels[target.resource_key],
            image_id=target.image_id,
            image_repo_digest=target.image_repo_digest,
        )

    def disable_and_remove_stores_supervisor(self, target, expected_revision_sha256):
        self.calls.append(("remove_supervisor", target.resource_key))
        self.states[target.resource_key] = "absent"

    def stop_container(self, target, expected_revision_sha256):
        self.calls.append(("stop_container", target.resource_key))
        self.states[target.resource_key] = "stopped"

    def remove_container(self, target, expected_revision_sha256):
        if self.states[target.resource_key] != "stopped":
            raise RuntimeError("container not stopped")
        self.calls.append(("remove_container", target.resource_key))
        self.states[target.resource_key] = "absent"

    def remove_volume(self, target, expected_revision_sha256):
        self.calls.append(("remove_volume", target.resource_key))
        self.states[target.resource_key] = "absent"

    def remove_network(self, target, expected_revision_sha256):
        self.calls.append(("remove_network", target.resource_key))
        self.states[target.resource_key] = "absent"

    def remove_regular_file(self, target, expected_revision_sha256):
        self.calls.append(("remove_regular_file", target.resource_key))
        self.states[target.resource_key] = "absent"

class _Probe:
    def __init__(self, eligibility: dict[str, object]) -> None:
        self.eligibility = eligibility
        self.calls = 0
        self.qdrant_points = 0

    def observe(self, request: RollbackOperationRequest):
        self.calls += 1
        return build_empty_rollback_eligibility_receipt(
            candidate_git_commit=str(self.eligibility["candidate_git_commit"]),
            candidate_git_tree=str(self.eligibility["candidate_git_tree"]),
            package_manifest_sha256=str(
                self.eligibility["package_manifest_sha256"]
            ),
            installation_execution_id=str(
                self.eligibility["installation_execution_id"]
            ),
            installation_receipt_sha256=str(
                self.eligibility["installation_receipt_sha256"]
            ),
            exact_targets_sha256=str(self.eligibility["exact_targets_sha256"]),
            resource_ledger_head_sha256=str(
                self.eligibility["resource_ledger_head_sha256"]
            ),
            observation_set_sha256=hashlib.sha256(
                f"observation:{self.calls}".encode("ascii")
            ).hexdigest(),
            pilot_ever_started=False,
            postgresql_user_rows=0,
            projection_queue_rows=0,
            qdrant_points=self.qdrant_points,
            active_clients=0,
            legacy_imports=0,
            application_services_installed=False,
            source_postgres_read_count=0,
            production_data_read=False,
            provider_calls=0,
        )


class _MarkerTransport:
    def __init__(self) -> None:
        self.held: DurableLiveRollbackMarkerObservation | None = None
        self.assertion_valid = True
        self.releases = 0

    def acquire_or_recover(
        self,
        request,
        *,
        marker_bound_postgres_identity_sha256,
        marker_bound_qdrant_identity_sha256,
        expected_semantic_empty_state_sha256,
    ):
        controller_authority_marker_sha256 = (
            rollback_entrypoint.canonical_live_rollback_controller_authority_marker_sha256(
                request,
                marker_bound_postgres_identity_sha256=(
                    marker_bound_postgres_identity_sha256
                ),
                marker_bound_qdrant_identity_sha256=(
                    marker_bound_qdrant_identity_sha256
                ),
            )
        )
        if self.held is None:
            self.held = DurableLiveRollbackMarkerObservation(
                controller_authority_marker_sha256=controller_authority_marker_sha256,
                marker_bound_postgres_identity_sha256=(
                    marker_bound_postgres_identity_sha256
                ),
                marker_bound_qdrant_identity_sha256=(
                    marker_bound_qdrant_identity_sha256
                ),
                semantic_empty_state_sha256=None,
                durable_root_file_regular_no_follow=True,
                durable_root_file_mode=0o400,
                durable_root_file_uid=0,
                durable_root_file_gid=0,
                durable_root_file_fsynced=True,
                durable_parent_fsynced=True,
            )
        return self.held

    def persist_semantic_empty_observation(
        self,
        request,
        held,
        semantic_empty_state_sha256,
    ):
        if held != self.held:
            raise RuntimeError("marker mismatch")
        self.held = replace(
            held,
            semantic_empty_state_sha256=(
                semantic_empty_state_sha256
            ),
        )
        return self.held

    def assert_held(self, request, held):
        if not self.assertion_valid or held != self.held:
            raise RuntimeError("marker is not held")
        return self.held

    def release_after_receipt(self, request, held):
        if held != self.held:
            raise RuntimeError("marker mismatch")
        self.held = None
        self.releases += 1


class _Audit:
    def __init__(self, terminal_postflight_sha256: str = "5" * 64) -> None:
        self.terminal_postflight_sha256 = terminal_postflight_sha256

    @staticmethod
    def _value(name: str) -> str:
        return hashlib.sha256(name.encode("ascii")).hexdigest()

    def authorization_nonce_sha256(self, request):
        return self._value("authorization_nonce")

    def installation_journal_sha256(self, request):
        return self._value("installation_journal")

    def rollback_journal_sha256(self, request):
        return self._value("rollback_journal")

    def authority_anchor_sha256(self, request):
        return self._value("authority_anchor")

    def resource_identity_ledger_sha256(self, request):
        return self._value("resource_identity_ledger")

    def terminal_postflight_receipt_sha256(self, request):
        return self.terminal_postflight_sha256


class ExactPhysicalRollbackOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
        ledger_path = root / "resources.jsonl"
        expected = expected_rollback_resource_names(PACKAGE_SHA)
        for key in ROLLBACK_RESOURCE_KEYS:
            kind, name = expected[key]
            is_container = kind == "container"
            labels = (
                {"governed-memory.resource": key}
                if kind in {"container", "volume", "network"}
                else None
            )
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
                    canonical_labels_sha256(labels) if labels is not None else None
                ),
            )
        self.records = load_ledger(
            ledger_path, expected_binding_sha256="9" * 64
        )
        self.verified_resources = derive_exact_rollback_resources_from_ledger(
            self.records,
            package_manifest_sha256=PACKAGE_SHA,
            expected_ledger_head_sha256=self.records[-1].entry_sha256,
        )
        self.bindings = physical_rollback_bindings_from_verified_ledger(
            self.records,
            self.verified_resources,
        )
        self.driver = _Driver(self.bindings.targets)

        runtime_receipt = "e" * 64
        runtime_root = (
            "/opt/governed-memory-controller/runtimes/" + runtime_receipt
        )
        release_root = "/opt/governed-memory-controller/releases/" + PACKAGE_SHA
        self.install_receipt = build_install_receipt(
            execution_id=EXECUTION_ID,
            attempt_id="install-" + "4" * 40,
            candidate_git_commit=COMMIT,
            candidate_git_tree=TREE,
            package_manifest_sha256=PACKAGE_SHA,
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
            authorization_sha256="f" * 64,
            plan_sha256="1" * 64,
            journal_head_sha256="2" * 64,
            journal_sequence=41,
            resource_ledger_head_sha256=self.records[-1].entry_sha256,
            resource_ledger_sequence=len(self.records),
            image_identity_set_sha256="4" * 64,
            postflight_receipt_sha256="5" * 64,
            terminal_store_readiness_sha256="6" * 64,
        )
        self.eligibility = build_empty_rollback_eligibility_receipt(
            candidate_git_commit=COMMIT,
            candidate_git_tree=TREE,
            package_manifest_sha256=PACKAGE_SHA,
            installation_execution_id=EXECUTION_ID,
            installation_receipt_sha256=str(
                self.install_receipt["receipt_sha256"]
            ),
            exact_targets_sha256=self.bindings.exact_targets_sha256,
            resource_ledger_head_sha256=self.bindings.resource_ledger_head_sha256,
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
        executions = root / "executions"
        execution_root = executions / EXECUTION_ID
        execution_root.mkdir(parents=True, mode=0o700)
        os.chmod(executions, 0o700)
        os.chmod(execution_root, 0o700)
        self.receipts = DurableReceiptStore.synthetic(executions)
        self.receipts.write_once(
            ReceiptArtifact.INSTALL, EXECUTION_ID, self.install_receipt
        )
        self.receipts.write_once(
            ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
            EXECUTION_ID,
            self.eligibility,
        )
        self.probe = _Probe(self.eligibility)
        self.marker_transport = _MarkerTransport()
        self.adapter = ExactPhysicalEmptyRollbackOperations(
            bindings=self.bindings,
            verified_resources=self.verified_resources,
            driver=self.driver,
            eligibility_probe=self.probe,
            controller_authority_marker_transport=self.marker_transport,
            retained_audit_source=_Audit(),
            receipt_store=self.receipts,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_production_factory_exposes_no_effect_injection_surface(self) -> None:
        parameters = set(
            inspect.signature(
                ProductionLinuxEmptyRollbackOperationsFactory.__init__
            ).parameters
        )
        self.assertEqual(
            parameters,
            {
                "self",
                "held_lock",
                "verified_controller_runtime_capability",
                "authority_state",
                "receipt_store",
            },
        )
        self.assertTrue(
            {
                "transport_factory",
                "eligibility_probe",
                "retained_audit_source",
                "postgres_receipt_sink",
                "driver",
            }.isdisjoint(parameters)
        )

    def test_store_backed_native_rollback_receipt_requires_held_marker(self) -> None:
        marker_state = _ProductionControllerAuthorityMarkerState(EXECUTION_ID)
        sink = StoreBackedPostgreSQLStageReceiptSink(
            receipt_store=self.receipts,
            execution_id=EXECUTION_ID,
            marker_state=marker_state,
        )
        receipt = _receipt(
            mode="rollback",
            final_state="installed_0001_0003",
            runtime_receipt_sha256="e" * 64,
            driver_runtime_identity_sha256="f" * 64,
            terminal_catalog_sha256=None,
            rollback_empty_proof_sha256="1" * 64,
            operations=("r01_rollback_pilot_marker_0004",),
        )
        with self.assertRaisesRegex(
            PhysicalRollbackAdapterError, "stage_receipt_sink_refused"
        ):
            sink.persist_postgres_stage_receipt(receipt)

        request = self._request(3)
        marker = DurableLiveRollbackMarkerObservation(
            controller_authority_marker_sha256="2" * 64,
            marker_bound_postgres_identity_sha256="3" * 64,
            marker_bound_qdrant_identity_sha256="4" * 64,
            semantic_empty_state_sha256=None,
            durable_root_file_regular_no_follow=True,
            durable_root_file_mode=0o400,
            durable_root_file_uid=0,
            durable_root_file_gid=0,
            durable_root_file_fsynced=True,
            durable_parent_fsynced=True,
        )
        marker_state.mark_held(request, marker)
        sink.persist_postgres_stage_receipt(receipt)
        durable = self.receipts.read(
            ReceiptArtifact.POSTGRES_ROLLBACK_I14,
            EXECUTION_ID,
        )
        self.assertEqual(durable.receipt_sha256, receipt.receipt_sha256)
        self.assertEqual(
            durable.canonical_receipt["native_receipt"]["final_state"],
            "installed_0001_0003",
        )
        with self.assertRaisesRegex(
            PhysicalRollbackAdapterError, "stage_receipt_sink_refused"
        ):
            sink.persist_postgres_stage_receipt(
                replace(receipt, receipt_sha256="0" * 64)
            )

    def test_live_empty_probe_rechecks_fixed_stores_and_writer_paths(self) -> None:
        probe = ClosedLinuxLiveEmptyEligibilityProbe(
            postgres=_TerminalStore(POSTGRES_BIND, _terminal_postgres()),
            qdrant=_TerminalStore(QDRANT_BIND, _terminal_qdrant()),
            signed_eligibility=self.eligibility,
        )
        request = self._request(5)
        with patch.object(
            ClosedLinuxLiveEmptyEligibilityProbe,
            "_application_writers_absent",
            return_value=True,
        ):
            observed = probe.observe(request)
        ignored = {"observation_set_sha256", "receipt_sha256"}
        self.assertEqual(
            {key: value for key, value in observed.items() if key not in ignored},
            {
                key: value
                for key, value in self.eligibility.items()
                if key not in ignored
            },
        )
        self.assertNotEqual(
            observed["observation_set_sha256"],
            self.eligibility["observation_set_sha256"],
        )
        with patch.object(
            ClosedLinuxLiveEmptyEligibilityProbe,
            "_application_writers_absent",
            return_value=False,
        ):
            with self.assertRaisesRegex(
                PhysicalRollbackAdapterError,
                "writer_fence_not_established",
            ):
                probe.observe(request)

    def _resource(self, key: str):
        unused, resources = verified_rollback_resource_parts(
            self.verified_resources
        )
        return next(item for item in resources if item.resource_key == key)

    def _request(self, step_index: int) -> RollbackOperationRequest:
        step = EMPTY_ROLLBACK_STEPS[step_index]
        runtime_receipt = "e" * 64
        runtime_root = (
            "/opt/governed-memory-controller/runtimes/" + runtime_receipt
        )
        release_root = "/opt/governed-memory-controller/releases/" + PACKAGE_SHA
        evidence, unused = verified_rollback_resource_parts(
            self.verified_resources
        )
        return RollbackOperationRequest(
            step=step,
            resource=(
                None if step.resource_key is None else self._resource(step.resource_key)
            ),
            execution_id=EXECUTION_ID,
            attempt_id="rollback-" + "5" * 40,
            journal_binding_sha256="6" * 64,
            plan_sha256="7" * 64,
            package_manifest_sha256=PACKAGE_SHA,
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
            installation_execution_id=EXECUTION_ID,
            installation_receipt_sha256=str(
                self.install_receipt["receipt_sha256"]
            ),
            recovery_reservation_claim_sha256="0" * 64,
            retained_evidence_sha256="b" * 64,
            eligibility_receipt_sha256=str(self.eligibility["receipt_sha256"]),
            resource_ledger_binding_sha256=(
                evidence.resource_ledger_binding_sha256
            ),
            resource_ledger_head_sha256=evidence.resource_ledger_head_sha256,
            resource_ledger_sequence=evidence.resource_ledger_sequence,
            exact_targets_sha256=evidence.exact_targets_sha256,
        )

    @staticmethod
    def _capability(request, acquisition):
        return rollback_entrypoint._EmptyRollbackControllerAuthorityMarkerCapability(
            EmptyRollbackControllerAuthorityMarkerEvidence(
                execution_id=request.execution_id,
                attempt_id=request.attempt_id,
                plan_sha256=request.plan_sha256,
                signed_eligibility_receipt_sha256=(
                    request.eligibility_receipt_sha256
                ),
                exact_targets_sha256=request.exact_targets_sha256,
                controller_runtime_tree_sha256=(
                    request.controller_runtime_tree_sha256
                ),
                controller_release_tree_sha256=(
                    request.controller_release_tree_sha256
                ),
                marker_bound_postgres_identity_sha256=(
                    acquisition.marker_bound_postgres_identity_sha256
                ),
                marker_bound_qdrant_identity_sha256=(
                    acquisition.marker_bound_qdrant_identity_sha256
                ),
                controller_authority_marker_sha256=acquisition.controller_authority_marker_sha256,
            ),
            rollback_entrypoint._CONTROLLER_AUTHORITY_MARKER_TOKEN,
        )

    def _remove_supervisor_and_acquire(self):
        marker_request = self._request(3)
        acquisition = self.adapter.acquire_empty_rollback_controller_authority_marker(marker_request)
        capability = self._capability(marker_request, acquisition)
        supervisor_request = self._request(4)
        self.adapter.apply_if_still_empty(
            supervisor_request,
            self.adapter.observe(supervisor_request),
            capability,
            self.eligibility,
        )
        semantic_request = self._request(5)
        self.adapter.observe_empty_eligibility(semantic_request, capability)
        stop_request = self._request(6)
        self.adapter.apply_if_still_empty(
            stop_request,
            self.adapter.observe(stop_request),
            capability,
            self.eligibility,
        )
        return (
            marker_request,
            capability,
            acquisition,
        )

    def test_marker_then_supervisor_removal_precedes_writer_fence_and_stop(self) -> None:
        marker_request = self._request(3)
        acquisition = self.adapter.acquire_empty_rollback_controller_authority_marker(marker_request)
        capability = self._capability(marker_request, acquisition)
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(self.driver.states["qdrant_container"], "running")
        self.assertEqual(self.driver.states["postgres_container"], "running")
        supervisor_request = self._request(4)
        self.adapter.apply_if_still_empty(
            supervisor_request,
            self.adapter.observe(supervisor_request),
            capability,
            self.eligibility,
        )
        observed = self.adapter.observe_empty_eligibility(
            self._request(5), capability
        )
        self.assertEqual(observed["postgresql_user_rows"], 0)
        self.assertEqual(self.probe.calls, 1)
        stop_request = self._request(6)
        self.adapter.apply_if_still_empty(
            stop_request,
            self.adapter.observe(stop_request),
            capability,
            self.eligibility,
        )
        self.assertEqual(
            self.driver.calls[:3],
            [
                ("remove_supervisor", "stores_supervisor"),
                ("stop_container", "qdrant_container"),
                ("stop_container", "postgres_container"),
            ],
        )
        self.assertEqual(self.driver.states["qdrant_container"], "stopped")
        self.assertEqual(self.driver.states["postgres_container"], "stopped")
        self.assertRegex(
            acquisition.marker_bound_postgres_identity_sha256,
            r"^[0-9a-f]{64}$",
        )
        self.assertRegex(
            acquisition.marker_bound_qdrant_identity_sha256,
            r"^[0-9a-f]{64}$",
        )
        self.assertNotEqual(
            acquisition.marker_bound_postgres_identity_sha256,
            acquisition.marker_bound_qdrant_identity_sha256,
        )

    def test_live_nonempty_recheck_refuses_before_any_destructive_effect(
        self,
    ) -> None:
        request = self._request(3)
        acquisition = self.adapter.acquire_empty_rollback_controller_authority_marker(request)
        capability = self._capability(request, acquisition)
        supervisor_request = self._request(4)
        self.adapter.apply_if_still_empty(
            supervisor_request,
            self.adapter.observe(supervisor_request),
            capability,
            self.eligibility,
        )
        self.probe.qdrant_points = 1
        with self.assertRaisesRegex(
            PhysicalRollbackAdapterError,
            "live_empty_observation_failed",
        ):
            self.adapter.observe_empty_eligibility(
                self._request(5),
                capability,
            )
        self.assertEqual(
            self.driver.calls,
            [("remove_supervisor", "stores_supervisor")],
        )

    def test_durable_marker_loss_is_refused_before_deletion(self) -> None:
        unused, capability, acquisition = self._remove_supervisor_and_acquire()
        self.marker_transport.assertion_valid = False
        request = self._request(7)
        with self.assertRaisesRegex(
            PhysicalRollbackAdapterError,
            "live_rollback_controller_authority_marker_not_held",
        ):
            self.adapter.apply_if_still_empty(
                request,
                self.adapter.observe(request),
                capability,
                self.eligibility,
            )
        self.assertNotIn(
            "remove_container", {operation for operation, unused in self.driver.calls}
        )

    def test_physical_teardown_order_and_terminal_absence(self) -> None:
        marker_request, capability, unused = self._remove_supervisor_and_acquire()

        volume_request = self._request(9)
        with self.assertRaisesRegex(
            PhysicalRollbackAdapterError, "volume_has_container"
        ):
            self.adapter.apply_if_still_empty(
                volume_request,
                self.adapter.observe(volume_request),
                capability,
                self.eligibility,
            )

        for step_index in (7, 8, 9, 10, 11, 12, 13, 14):
            request = self._request(step_index)
            self.adapter.apply_if_still_empty(
                request,
                self.adapter.observe(request),
                capability,
                self.eligibility,
            )
        final_request = self._request(21)
        unused, resources = verified_rollback_resource_parts(
            self.verified_resources
        )
        self.assertTrue(
            self.adapter.exact_resources_absent(
                final_request, resources, capability
            )
        )
        for logical_step in range(15, 21):
            self.assertEqual(
                self.adapter.observe(self._request(logical_step)).state,
                "after",
            )
        retained = self.adapter.observe_retained_audit_set(
            final_request, RETAINED_AUDIT_KEYS, capability
        )
        self.assertEqual(set(retained), set(RETAINED_AUDIT_KEYS))
        self.assertEqual(
            retained["installation_receipt"],
            self.receipts.read(
                ReceiptArtifact.INSTALL, EXECUTION_ID
            ).canonical_file_sha256,
        )
        self.adapter.release_empty_rollback_controller_authority_marker(marker_request, capability)
        self.assertNotIn(
            "remove_exact_qdrant_alias", {operation for operation, key in self.driver.calls}
        )

    def test_completed_replay_with_absent_stores_reuses_durable_empty_state(
        self,
    ) -> None:
        for resource_key in self.driver.states:
            self.driver.states[resource_key] = "absent"

        marker_request = self._request(3)
        acquisition = (
            self.adapter.acquire_empty_rollback_controller_authority_marker(
                marker_request
            )
        )
        capability = self._capability(marker_request, acquisition)
        observed = self.adapter.observe_empty_eligibility(
            self._request(5), capability
        )

        self.assertEqual(observed, self.eligibility)
        self.assertEqual(self.probe.calls, 0)
        self.assertIsNotNone(self.marker_transport.held)
        self.assertIsNotNone(
            self.marker_transport.held.semantic_empty_state_sha256
        )

        final_request = self._request(21)
        unused, resources = verified_rollback_resource_parts(
            self.verified_resources
        )
        self.assertTrue(
            self.adapter.exact_resources_absent(
                final_request, resources, capability
            )
        )
        self.adapter.verify_install_receipt_and_ledger(final_request)
        retained = self.adapter.observe_retained_audit_set(
            final_request, RETAINED_AUDIT_KEYS, capability
        )
        self.assertEqual(set(retained), set(RETAINED_AUDIT_KEYS))
        self.assertEqual(
            retained["installation_receipt"],
            self.receipts.read(
                ReceiptArtifact.INSTALL, EXECUTION_ID
            ).canonical_file_sha256,
        )
        self.assertEqual(
            retained["eligibility_receipt"],
            self.receipts.read(
                ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY,
                EXECUTION_ID,
            ).canonical_file_sha256,
        )

        self.adapter.release_empty_rollback_controller_authority_marker(
            marker_request, capability
        )
        self.assertIsNone(self.marker_transport.held)
        self.assertEqual(self.marker_transport.releases, 1)
        self.assertEqual(self.driver.calls, [])

    def test_completed_replay_recovers_persisted_semantic_marker_then_releases(
        self,
    ) -> None:
        for resource_key in self.driver.states:
            self.driver.states[resource_key] = "absent"

        marker_request = self._request(3)
        first_acquisition = (
            self.adapter.acquire_empty_rollback_controller_authority_marker(
                marker_request
            )
        )
        first_capability = self._capability(
            marker_request, first_acquisition
        )
        self.adapter.observe_empty_eligibility(
            self._request(5), first_capability
        )
        persisted = self.marker_transport.held
        self.assertIsNotNone(persisted)
        self.assertIsNotNone(persisted.semantic_empty_state_sha256)

        replay_adapter = ExactPhysicalEmptyRollbackOperations(
            bindings=self.bindings,
            verified_resources=self.verified_resources,
            driver=self.driver,
            eligibility_probe=self.probe,
            controller_authority_marker_transport=self.marker_transport,
            retained_audit_source=_Audit(),
            receipt_store=self.receipts,
        )
        replay_acquisition = (
            replay_adapter.acquire_empty_rollback_controller_authority_marker(
                marker_request
            )
        )
        replay_capability = self._capability(
            marker_request, replay_acquisition
        )
        replay_adapter.observe_empty_eligibility(
            self._request(5), replay_capability
        )

        self.assertEqual(self.probe.calls, 0)
        self.assertEqual(self.marker_transport.held, persisted)
        final_request = self._request(21)
        unused, resources = verified_rollback_resource_parts(
            self.verified_resources
        )
        self.assertTrue(
            replay_adapter.exact_resources_absent(
                final_request, resources, replay_capability
            )
        )
        replay_adapter.verify_install_receipt_and_ledger(final_request)
        retained = replay_adapter.observe_retained_audit_set(
            final_request, RETAINED_AUDIT_KEYS, replay_capability
        )
        self.assertEqual(set(retained), set(RETAINED_AUDIT_KEYS))

        replay_adapter.release_empty_rollback_controller_authority_marker(
            marker_request, replay_capability
        )
        self.assertIsNone(self.marker_transport.held)
        self.assertEqual(self.marker_transport.releases, 1)
        self.assertEqual(self.driver.calls, [])

    def test_logical_child_effects_fail_closed_until_plan_marks_verification(self) -> None:
        unused, capability, acquisition = self._remove_supervisor_and_acquire()
        request = self._request(15)
        with self.assertRaisesRegex(
            PhysicalRollbackAdapterError,
            "logical_absence_unproved",
        ):
            self.adapter.apply_if_still_empty(
                request,
                self.adapter.observe(request),
                capability,
                self.eligibility,
            )
        self.assertFalse(
            any("alias" in operation for operation, key in self.driver.calls)
        )

    def test_driver_label_drift_is_rejected_before_container_stop(self) -> None:
        marker_request = self._request(3)
        acquisition = self.adapter.acquire_empty_rollback_controller_authority_marker(marker_request)
        capability = self._capability(marker_request, acquisition)
        supervisor_request = self._request(4)
        self.adapter.apply_if_still_empty(
            supervisor_request,
            self.adapter.observe(supervisor_request),
            capability,
            self.eligibility,
        )
        self.adapter.observe_empty_eligibility(self._request(5), capability)
        self.driver.labels["qdrant_container"] = (("unexpected", "label"),)
        with self.assertRaisesRegex(
            PhysicalRollbackAdapterError, "identity_drift"
        ):
            self.adapter.observe(self._request(6))
        self.assertNotIn(
            ("stop_container", "qdrant_container"), self.driver.calls
        )

    def test_partial_supervisor_removal_is_exactly_recoverable(self) -> None:
        self.driver.states["stores_supervisor"] = "recoverable"
        marker_request = self._request(3)
        acquisition = self.adapter.acquire_empty_rollback_controller_authority_marker(marker_request)
        capability = self._capability(marker_request, acquisition)
        request = self._request(4)
        observed = self.adapter.observe(request)
        self.assertEqual(observed.state, "recoverable")
        self.adapter.apply_if_still_empty(
            request,
            observed,
            capability,
            self.eligibility,
        )
        self.assertEqual(self.driver.states["stores_supervisor"], "absent")

    def test_observation_ownership_matches_controller_contract(self) -> None:
        request = self._request(4)
        observed = self.adapter.observe(request)
        self.assertEqual(
            observed.ownership_sha256,
            rollback_entrypoint._EmptyRollbackController._expected_ownership(
                request, observed.revision_sha256
            ),
        )

    def test_retained_postflight_mismatch_is_refused(self) -> None:
        unused, capability, acquisition = self._remove_supervisor_and_acquire()
        self.adapter._audit = _Audit("4" * 64)
        with self.assertRaisesRegex(
            PhysicalRollbackAdapterError,
            "terminal_postflight_receipt_mismatch",
        ):
            self.adapter.observe_retained_audit_set(
                self._request(21), RETAINED_AUDIT_KEYS, capability
            )

    def test_install_receipt_and_ledger_are_reverified_from_durable_path(self) -> None:
        capability = self.adapter.verify_install_receipt_and_ledger(
            self._request(2)
        )
        self.assertIn("Capability", repr(capability))

    def test_binding_derivation_rejects_changed_physical_identity(self) -> None:
        changed = list(self.records)
        index = next(
            index
            for index, record in enumerate(changed)
            if record.resource_name
            == expected_rollback_resource_names(PACKAGE_SHA)[
                "qdrant_container"
            ][1]
        )
        changed[index] = replace(changed[index], resource_id="f" * 64)
        with self.assertRaisesRegex(
            PhysicalRollbackAdapterError, "ledger_binding_invalid|target_drift"
        ):
            physical_rollback_bindings_from_verified_ledger(
                tuple(changed), self.verified_resources
            )

    def test_exact_after_state_recovery_is_observation_only(self) -> None:
        unused, capability, acquisition = self._remove_supervisor_and_acquire()
        request = self._request(7)
        before = self.adapter.observe(request)
        self.adapter.apply_if_still_empty(
            request, before, capability, self.eligibility
        )
        calls_after_first = tuple(self.driver.calls)
        after = self.adapter.observe(request)
        self.assertEqual(after.state, "after")
        self.adapter.apply_if_still_empty(
            request, after, capability, self.eligibility
        )
        self.assertEqual(tuple(self.driver.calls), calls_after_first)


if __name__ == "__main__":
    unittest.main()

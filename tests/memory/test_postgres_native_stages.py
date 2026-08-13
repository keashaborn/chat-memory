from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
import inspect
import json
from pathlib import Path
import re
from types import MappingProxyType
import unittest

import tools.governed_memory_install.postgres_native_stages as subject
from tools.governed_memory_install.postgres_native_stages import (
    APPROVED_TERMINAL_CATALOG_SHA256,
    CATALOG_QUERIES,
    FORWARD_TRANSITIONS,
    NATIVE_STAGES,
    ROLLBACK_TRANSITIONS,
    SOURCE_SHA256,
    CatalogQueryId,
    ClosedPostgreSQLStageMachine,
    DriverRuntimeObservation,
    EndpointObservation,
    ExternalClientObservation,
    ForwardOperation,
    ForwardPrefix,
    InstallTarget,
    PostgreSQLNativeStageError,
    PrivacySettingsObservation,
    RollbackObservation,
    RollbackOperation,
    RollbackPrefix,
    RollbackTarget,
    StageId,
    construct_current_machine,
    contract_document,
    normalize_catalog_rows,
)


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_RECEIPT_SHA256 = "7" * 64
DRIVER_IDENTITY_SHA256 = "6" * 64
EMPTY_PROOF_SHA256 = "8" * 64


def _catalog_rows() -> dict[CatalogQueryId, tuple[tuple[object, ...], ...]]:
    rows = {query_id: () for query_id in CatalogQueryId}
    rows[CatalogQueryId.SESSION] = (
        (
            "governed_memory",
            "governed_memory_bootstrap",
            "governed_memory_bootstrap",
            160014,
            "UTF8",
            "none",
            "off",
            -1,
            0,
            "UTC",
        ),
    )
    rows[CatalogQueryId.ROLES] = (
        (
            "governed_memory_owner",
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            -1,
            "",
            "",
        ),
        (
            "governed_memory_api",
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            -1,
            "",
            "",
        ),
    )
    rows[CatalogQueryId.PRIVACY_SETTINGS] = (
        ("none", "off", -1, -1, "0", 0, 0, 0, False),
    )
    rows[CatalogQueryId.EXTERNAL_CLIENTS] = ((0, 0),)
    rows[CatalogQueryId.SEMANTIC_EMPTY] = ((0,),)
    return rows


class FakePrimitive:
    def __init__(
        self,
        *,
        forward_prefix: ForwardPrefix = ForwardPrefix.EMPTY,
        rollback_prefix: RollbackPrefix = RollbackPrefix.INSTALLED,
        rows: dict[CatalogQueryId, tuple[tuple[object, ...], ...]] | None = None,
    ) -> None:
        self.forward_prefix = forward_prefix
        self.rollback_prefix = rollback_prefix
        self.rows = _catalog_rows() if rows is None else rows
        self.calls: list[object] = []
        self.lock_held = False
        self.marker_held = True
        self.persisted = []
        self.forward_drift = False
        self.rollback_drift = False
        self.drop_marker_after_persist = False
        self.driver_override = None
        self.acquire_error = False
        self.endpoint_error = False

    def endpoint_identity(self) -> EndpointObservation:
        self.calls.append("endpoint_identity")
        if self.endpoint_error:
            raise RuntimeError("synthetic endpoint failure")
        return EndpointObservation(
            "127.0.0.1",
            55432,
            "postgres",
            "governed_memory",
            "governed_memory_bootstrap",
        )

    def driver_runtime_identity(self) -> DriverRuntimeObservation:
        self.calls.append("driver_runtime_identity")
        if self.driver_override is not None:
            return self.driver_override
        return DriverRuntimeObservation(
            "3.12.13",
            "3.3.4",
            "synchronous",
            "binary",
            180000,
            True,
            "5" * 64,
            RUNTIME_RECEIPT_SHA256,
            DRIVER_IDENTITY_SHA256,
        )

    def observe_privacy_settings(self) -> PrivacySettingsObservation:
        self.calls.append("observe_privacy_settings")
        return PrivacySettingsObservation(
            "none", "off", -1, -1, "0", 0, 0, 0, False
        )

    def observe_external_clients(self) -> ExternalClientObservation:
        self.calls.append("observe_external_clients")
        return ExternalClientObservation(0, 0)

    def acquire_fixed_session_lock(self) -> None:
        self.calls.append("acquire_lock")
        if self.acquire_error:
            raise RuntimeError("synthetic acquire failure")
        self.lock_held = True

    def fixed_session_lock_held(self) -> bool:
        self.calls.append("lock_held")
        return self.lock_held

    def release_fixed_session_lock(self) -> None:
        self.calls.append("release_lock")
        self.lock_held = False

    def observe_forward_prefix(self) -> ForwardPrefix:
        self.calls.append("observe_forward")
        return self.forward_prefix

    def apply_forward_transition(
        self, operation: ForwardOperation
    ) -> ForwardPrefix:
        self.calls.append(operation)
        expected_operation, expected_prefix = FORWARD_TRANSITIONS[
            self.forward_prefix
        ]
        if operation is not expected_operation:
            raise AssertionError("machine selected noncanonical forward operation")
        if self.forward_drift:
            return self.forward_prefix
        self.forward_prefix = expected_prefix
        return expected_prefix

    def terminal_catalog_rows(
        self, query_id: CatalogQueryId
    ) -> tuple[tuple[object, ...], ...]:
        self.calls.append(query_id)
        return self.rows[query_id]

    def controller_authority_marker_held(self) -> bool:
        self.calls.append("controller_authority_marker_held")
        return self.marker_held

    def observe_rollback_prefix(self) -> RollbackObservation:
        self.calls.append("observe_rollback")
        return RollbackObservation(self.rollback_prefix, EMPTY_PROOF_SHA256)

    def verify_semantic_empty(self) -> str:
        self.calls.append("verify_semantic_empty")
        return EMPTY_PROOF_SHA256

    def apply_rollback_transition(
        self, operation: RollbackOperation
    ) -> RollbackObservation:
        self.calls.append(operation)
        expected_operation, expected_prefix = ROLLBACK_TRANSITIONS[
            self.rollback_prefix
        ]
        if operation is not expected_operation:
            raise AssertionError("machine selected noncanonical rollback operation")
        if self.rollback_drift:
            return RollbackObservation(self.rollback_prefix, EMPTY_PROOF_SHA256)
        self.rollback_prefix = expected_prefix
        return RollbackObservation(expected_prefix, EMPTY_PROOF_SHA256)

    def persist_stage_receipt(self, receipt: object) -> None:
        self.calls.append("persist_receipt")
        if not self.lock_held:
            raise AssertionError("receipt was persisted without the session lock")
        if getattr(receipt, "mode", None) == "rollback" and not self.marker_held:
            raise AssertionError("rollback receipt was persisted without controller-authority marker")
        self.persisted.append(receipt)
        if self.drop_marker_after_persist:
            self.marker_held = False


def _install_machine(
    primitive: FakePrimitive,
) -> ClosedPostgreSQLStageMachine:
    _encoded, catalog_sha256 = normalize_catalog_rows(primitive.rows)
    authority = subject._mint_synthetic_authority(
        mode="install",
        runtime_receipt_sha256=RUNTIME_RECEIPT_SHA256,
        approved_driver_runtime_identity_sha256=DRIVER_IDENTITY_SHA256,
        approved_terminal_catalog_sha256=catalog_sha256,
    )
    return ClosedPostgreSQLStageMachine(primitive, authority)


def _rollback_machine(
    primitive: FakePrimitive,
) -> ClosedPostgreSQLStageMachine:
    authority = subject._mint_synthetic_authority(
        mode="rollback",
        runtime_receipt_sha256=RUNTIME_RECEIPT_SHA256,
        approved_driver_runtime_identity_sha256=DRIVER_IDENTITY_SHA256,
        rollback_empty_proof_sha256=EMPTY_PROOF_SHA256,
    )
    return ClosedPostgreSQLStageMachine(primitive, authority)


class PostgreSQLNativeStageTests(unittest.TestCase):
    def test_source_and_driver_identities_are_exact_and_current_gate_is_closed(
        self,
    ) -> None:
        self.assertEqual(len(SOURCE_SHA256), 9)
        for path, expected in SOURCE_SHA256.items():
            self.assertEqual(
                hashlib.sha256((ROOT / path).read_bytes()).hexdigest(),
                expected,
                path,
            )
        document = contract_document()
        driver = document["preferred_driver"]
        self.assertEqual(driver["python_version_target"], "3.12.13")
        self.assertEqual(driver["preferred_version"], "3.3.4")
        self.assertEqual(
            driver["required_distributions"],
            ["psycopg", "psycopg-binary"],
        )
        self.assertEqual(len(driver["selected_wheels"]), 2)
        self.assertTrue(driver["preference_contract_packaged"])
        for key in (
            "exact_driver_identity_contract_packaged",
            "exact_wheel_filenames_frozen",
            "wheel_bytes_staged",
            "wheel_bytes_verified",
            "binary_native_library_closure_inspected",
            "runtime_receipt_selected",
            "ready",
        ):
            self.assertIs(driver[key], True, key)
        gate = document["execution_gate"]
        self.assertTrue(gate["fixed_orchestration_machine_packaged"])
        self.assertTrue(gate["fixed_catalog_query_contract_packaged"])
        self.assertTrue(gate["operation_to_sql_translation_packaged"])
        self.assertTrue(gate["concrete_psycopg_adapter_packaged"])
        self.assertTrue(gate["current_constructor_refuses_before_primitive_call"])
        self.assertFalse(gate["live_execution_allowed"])
        self.assertFalse(
            gate[
                "caller_sql_dsn_endpoint_database_role_path_or_expected_catalog_allowed"
            ]
        )

    def test_four_stage_descriptors_and_transition_maps_are_immutable(self) -> None:
        self.assertEqual(tuple(NATIVE_STAGES), tuple(StageId))
        self.assertIsInstance(NATIVE_STAGES, MappingProxyType)
        self.assertIsInstance(FORWARD_TRANSITIONS, MappingProxyType)
        self.assertIsInstance(ROLLBACK_TRANSITIONS, MappingProxyType)
        self.assertEqual(len(FORWARD_TRANSITIONS), 15)
        self.assertEqual(
            {transition[0] for transition in FORWARD_TRANSITIONS.values()},
            set(ForwardOperation),
        )
        self.assertEqual(
            {transition[0] for transition in ROLLBACK_TRANSITIONS.values()},
            set(RollbackOperation),
        )
        with self.assertRaises(TypeError):
            NATIVE_STAGES[StageId.F01] = NATIVE_STAGES[StageId.F01]
        with self.assertRaises(FrozenInstanceError):
            NATIVE_STAGES[StageId.F01].stage_id = StageId.F02

    def test_public_execution_surface_has_no_sql_dsn_path_or_catalog_authority(
        self,
    ) -> None:
        self.assertEqual(
            tuple(inspect.signature(construct_current_machine).parameters),
            ("primitive",),
        )
        self.assertEqual(
            tuple(
                inspect.signature(
                    ClosedPostgreSQLStageMachine.run_install
                ).parameters
            ),
            ("self",),
        )
        self.assertEqual(
            tuple(
                inspect.signature(
                    ClosedPostgreSQLStageMachine.run_rollback
                ).parameters
            ),
            ("self",),
        )
        for forbidden in (
            "sql",
            "dsn",
            "host",
            "port",
            "database",
            "role",
            "path",
            "expected_catalog",
        ):
            self.assertNotIn(
                forbidden,
                inspect.signature(construct_current_machine).parameters,
            )
        module_text = Path(subject.__file__).read_text(encoding="ascii")
        self.assertNotRegex(
            module_text,
            r"(?m)^(?:from|import) (?:psycopg|socket|subprocess)\b",
        )

    def test_current_constructor_refuses_before_any_primitive_call(self) -> None:
        primitive = FakePrimitive()
        with self.assertRaisesRegex(
            PostgreSQLNativeStageError,
            "postgres_native_runtime_driver_catalog_not_ready",
        ):
            construct_current_machine(primitive)
        self.assertEqual(primitive.calls, [])

    def test_forward_machine_resumes_exact_prefix_and_compares_catalog(self) -> None:
        primitive = FakePrimitive(forward_prefix=ForwardPrefix.API_ROLE)
        receipt = _install_machine(primitive).run_install()
        expected_operations = []
        prefix = ForwardPrefix.API_ROLE
        while prefix is not ForwardPrefix.READY_FOR_CATALOG:
            operation, prefix = FORWARD_TRANSITIONS[prefix]
            expected_operations.append(operation.value)
        self.assertEqual(
            receipt.operations,
            tuple(expected_operations) + (StageId.T01.value,),
        )
        self.assertEqual(receipt.final_state, "ready_for_terminal_catalog")
        self.assertEqual(
            receipt.terminal_catalog_sha256,
            normalize_catalog_rows(primitive.rows)[1],
        )
        self.assertEqual(len(primitive.persisted), 1)
        self.assertFalse(primitive.lock_held)
        self.assertLess(
            primitive.calls.index("acquire_lock"),
            primitive.calls.index("endpoint_identity"),
        )
        self.assertLess(
            primitive.calls.index("endpoint_identity"),
            primitive.calls.index("driver_runtime_identity"),
        )
        self.assertLess(
            primitive.calls.index("acquire_lock"),
            primitive.calls.index("observe_forward"),
        )
        self.assertLess(
            primitive.calls.index("persist_receipt"),
            primitive.calls.index("release_lock"),
        )
        for query_id in CatalogQueryId:
            query_position = primitive.calls.index(query_id)
            self.assertGreater(query_position, 0)
            self.assertEqual(
                primitive.calls[query_position - 2 : query_position],
                ["lock_held", "observe_external_clients"],
            )
        self.assertTrue(
            all(
                type(value) is ForwardOperation
                for value in primitive.calls
                if type(value) is ForwardOperation
            )
        )

    def test_install_targets_stop_at_exact_controller_prefix(self) -> None:
        cases = (
            (InstallTarget.I11, ForwardPrefix.ROLES_PREFLIGHT),
            (InstallTarget.I12, ForwardPrefix.FOUNDATION),
            (InstallTarget.I13, ForwardPrefix.CLAIM_DETAIL),
        )
        for target, expected in cases:
            with self.subTest(target=target.value):
                primitive = FakePrimitive()
                receipt = _install_machine(primitive).advance_install(target)
                self.assertEqual(receipt.final_state, expected.value)
                self.assertEqual(primitive.forward_prefix, expected)
                self.assertNotIn(StageId.T01.value, receipt.operations)
                self.assertEqual(len(primitive.persisted), 1)

    def test_rollback_targets_stop_at_exact_controller_prefix(self) -> None:
        cases = (
            (
                RollbackTarget.I14,
                RollbackPrefix.INSTALLED,
                RollbackPrefix.WITHOUT_0004,
            ),
            (
                RollbackTarget.I13,
                RollbackPrefix.WITHOUT_0004,
                RollbackPrefix.WITHOUT_0003,
            ),
            (
                RollbackTarget.I12,
                RollbackPrefix.WITHOUT_0003,
                RollbackPrefix.DATABASE_PREFIX,
            ),
        )
        for target, start, expected in cases:
            with self.subTest(target=target.value):
                primitive = FakePrimitive(rollback_prefix=start)
                receipt = _rollback_machine(primitive).advance_rollback(target)
                self.assertEqual(receipt.final_state, expected.value)
                self.assertEqual(primitive.rollback_prefix, expected)
                self.assertEqual(len(receipt.operations), 1)

        invalid = FakePrimitive(rollback_prefix=RollbackPrefix.INSTALLED)
        with self.assertRaisesRegex(
            PostgreSQLNativeStageError,
            "postgres_rollback_prefix_not_before_target",
        ):
            _rollback_machine(invalid).advance_rollback(RollbackTarget.I13)
        self.assertEqual(invalid.persisted, [])

    def test_catalog_queries_and_normalization_are_exact_and_order_independent(
        self,
    ) -> None:
        self.assertEqual(tuple(CATALOG_QUERIES), tuple(CatalogQueryId))
        for query_id, query in CATALOG_QUERIES.items():
            self.assertEqual(query.query_id, query_id)
            self.assertEqual(
                query.sql_sha256,
                hashlib.sha256(query.sql.encode("ascii")).hexdigest(),
            )
            self.assertTrue(query.sql.startswith("SELECT "))
            self.assertIsNone(
                re.search(r"\bORDER\s+BY\s+[0-9]+(?:\s+COLLATE)?\b", query.sql)
            )
        security_queries = {
            CatalogQueryId.SCHEMAS: ("nspowner", "nspacl"),
            CatalogQueryId.DATABASE_ROLE_SETTINGS: ("pg_db_role_setting",),
            CatalogQueryId.COLUMNS: ("pg_attrdef", "attidentity", "attacl"),
            CatalogQueryId.SEQUENCES: ("pg_sequence", "seqincrement"),
            CatalogQueryId.INDEXES: ("pg_index", "indisvalid", "indpred"),
            CatalogQueryId.POLICIES: (
                "policy.permissive",
                "policy.cmd",
                "policy.roles",
            ),
            CatalogQueryId.TRIGGERS: ("trigger_entry.tgenabled",),
            CatalogQueryId.DEFAULT_ACLS: ("pg_default_acl", "defaclacl"),
        }
        for query_id, fragments in security_queries.items():
            for fragment in fragments:
                self.assertIn(fragment, CATALOG_QUERIES[query_id].sql)
        rows = _catalog_rows()
        reversed_rows = dict(reversed(tuple(rows.items())))
        reversed_rows[CatalogQueryId.ROLES] = tuple(
            reversed(rows[CatalogQueryId.ROLES])
        )
        self.assertEqual(
            normalize_catalog_rows(rows),
            normalize_catalog_rows(reversed_rows),
        )
        missing = dict(rows)
        missing.pop(CatalogQueryId.ROUTINES)
        with self.assertRaisesRegex(
            PostgreSQLNativeStageError, "postgres_catalog_result_set_invalid"
        ):
            normalize_catalog_rows(missing)
        invalid = dict(rows)
        invalid[CatalogQueryId.SESSION] = ((object(),) * 10,)
        with self.assertRaisesRegex(
            PostgreSQLNativeStageError, "postgres_catalog_scalar_invalid"
        ):
            normalize_catalog_rows(invalid)

    def test_catalog_drift_and_driver_drift_refuse_and_release_lock(self) -> None:
        primitive = FakePrimitive(forward_prefix=ForwardPrefix.READY_FOR_CATALOG)
        _encoded, approved = normalize_catalog_rows(primitive.rows)
        authority = subject._mint_synthetic_authority(
            mode="install",
            runtime_receipt_sha256=RUNTIME_RECEIPT_SHA256,
            approved_driver_runtime_identity_sha256=DRIVER_IDENTITY_SHA256,
            approved_terminal_catalog_sha256="9" * 64,
        )
        self.assertNotEqual(approved, "9" * 64)
        with self.assertRaisesRegex(
            PostgreSQLNativeStageError,
            "postgres_terminal_catalog_not_approved",
        ):
            ClosedPostgreSQLStageMachine(primitive, authority).run_install()
        self.assertFalse(primitive.lock_held)
        self.assertEqual(primitive.persisted, [])

        driver_drift = FakePrimitive()
        driver_drift.driver_override = replace(
            driver_drift.driver_runtime_identity(),
            driver_identity_sha256="e" * 64,
        )
        driver_drift.calls.clear()
        with self.assertRaisesRegex(
            PostgreSQLNativeStageError, "postgres_driver_identity_drift"
        ):
            _install_machine(driver_drift).run_install()
        self.assertLess(
            driver_drift.calls.index("acquire_lock"),
            driver_drift.calls.index("endpoint_identity"),
        )
        self.assertLess(
            driver_drift.calls.index("endpoint_identity"),
            driver_drift.calls.index("driver_runtime_identity"),
        )
        self.assertEqual(driver_drift.calls[-1], "release_lock")
        self.assertFalse(driver_drift.lock_held)

    def test_lock_acquisition_and_boundary_failure_release_are_safe(self) -> None:
        acquire_failure = FakePrimitive()
        acquire_failure.acquire_error = True
        with self.assertRaisesRegex(RuntimeError, "synthetic acquire failure"):
            _install_machine(acquire_failure).run_install()
        self.assertEqual(acquire_failure.calls, ["acquire_lock"])
        self.assertFalse(acquire_failure.lock_held)

        boundary_failure = FakePrimitive()
        boundary_failure.endpoint_error = True
        with self.assertRaisesRegex(RuntimeError, "synthetic endpoint failure"):
            _install_machine(boundary_failure).run_install()
        self.assertLess(
            boundary_failure.calls.index("acquire_lock"),
            boundary_failure.calls.index("endpoint_identity"),
        )
        self.assertEqual(boundary_failure.calls[-1], "release_lock")
        self.assertFalse(boundary_failure.lock_held)

    def test_rollback_checks_empty_before_mutation_and_persists_under_marker(
        self,
    ) -> None:
        primitive = FakePrimitive(rollback_prefix=RollbackPrefix.INSTALLED)
        receipt = _rollback_machine(primitive).run_rollback()
        expected = (
            RollbackOperation.R01_ROLLBACK_PILOT_MARKER_0004.value,
            RollbackOperation.R01_ROLLBACK_OWNER_CLAIM_DETAIL_0003.value,
            RollbackOperation.R01_ROLLBACK_FOUNDATION_0001.value,
            RollbackOperation.R01_DROP_EXACT_DATABASE_PREFIX.value,
            RollbackOperation.R01_DROP_EXACT_ROLE_PREFIX_TRANSACTION.value,
        )
        self.assertEqual(receipt.operations, expected)
        self.assertEqual(receipt.rollback_empty_proof_sha256, EMPTY_PROOF_SHA256)
        first_mutation = primitive.calls.index(
            RollbackOperation.R01_ROLLBACK_PILOT_MARKER_0004
        )
        self.assertLess(primitive.calls.index("verify_semantic_empty"), first_mutation)
        self.assertLess(
            primitive.calls.index("acquire_lock"),
            primitive.calls.index("endpoint_identity"),
        )
        self.assertLess(
            primitive.calls.index("endpoint_identity"),
            primitive.calls.index("observe_rollback"),
        )
        self.assertLess(
            primitive.calls.index("persist_receipt"),
            primitive.calls.index("release_lock"),
        )
        self.assertEqual(len(primitive.persisted), 1)
        self.assertFalse(primitive.lock_held)

    def test_rollback_resumes_role_only_prefix_without_reopening_absent_database(
        self,
    ) -> None:
        primitive = FakePrimitive(rollback_prefix=RollbackPrefix.ROLES_ONLY_3)
        receipt = _rollback_machine(primitive).run_rollback()
        self.assertEqual(
            receipt.operations,
            (RollbackOperation.R01_DROP_EXACT_ROLE_PREFIX_TRANSACTION.value,),
        )
        self.assertNotIn("verify_semantic_empty", primitive.calls)
        self.assertEqual(primitive.rollback_prefix, RollbackPrefix.EMPTY)

    def test_rollback_refuses_missing_or_lost_marker_and_transition_drift(self) -> None:
        missing_marker = FakePrimitive()
        missing_marker.marker_held = False
        with self.assertRaisesRegex(
            PostgreSQLNativeStageError,
            "postgres_controller_authority_marker_not_held",
        ):
            _rollback_machine(missing_marker).run_rollback()
        self.assertNotIn("endpoint_identity", missing_marker.calls)
        self.assertNotIn("acquire_lock", missing_marker.calls)

        drift = FakePrimitive()
        drift.rollback_drift = True
        with self.assertRaisesRegex(
            PostgreSQLNativeStageError, "postgres_rollback_transition_drift"
        ):
            _rollback_machine(drift).run_rollback()
        self.assertFalse(drift.lock_held)
        self.assertEqual(drift.persisted, [])

        lost_after_receipt = FakePrimitive(rollback_prefix=RollbackPrefix.EMPTY)
        lost_after_receipt.drop_marker_after_persist = True
        with self.assertRaisesRegex(
            PostgreSQLNativeStageError,
            "postgres_controller_authority_marker_not_held",
        ):
            _rollback_machine(lost_after_receipt).run_rollback()
        self.assertFalse(lost_after_receipt.lock_held)

    def test_checked_in_contract_is_deterministic_and_zero_effect(self) -> None:
        document = contract_document()
        checked_in = json.loads(
            (
                ROOT
                / "ops/governed_memory/installation/current/postgres/"
                "native_stage_contract.json"
            ).read_text(encoding="ascii")
        )
        self.assertEqual(checked_in, document)
        self.assertEqual(
            document["schema_version"],
            "governed-memory-postgres-native-stage-contract-v3",
        )
        coverage = document["terminal_catalog"]["security_sensitive_coverage"]
        self.assertTrue(all(coverage.values()))
        self.assertTrue(
            document["terminal_catalog"]["postgresql_parse_or_execution_proven"]
        )
        self.assertTrue(document["terminal_catalog"]["approved_manifest_selected"])
        self.assertEqual(
            document["terminal_catalog"]["approved_normalized_catalog_sha256"],
            APPROVED_TERMINAL_CATALOG_SHA256,
        )
        columns_sql = CATALOG_QUERIES[CatalogQueryId.COLUMNS].sql
        self.assertNotIn(" AS collation ", columns_sql)
        self.assertIn(" AS collation_entry ", columns_sql)
        self.assertEqual(set(document["repository_phase_effect_counts"].values()), {0})
        unsigned = dict(document)
        supplied = unsigned.pop("contract_sha256")
        self.assertEqual(
            supplied,
            hashlib.sha256(
                json.dumps(
                    unsigned,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            ).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()

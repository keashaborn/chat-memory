from __future__ import annotations

from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
import unittest

import tools.governed_memory_install.postgres_source_closure as subject
from tools.governed_memory_install.postgres_source_closure import (
    POSTGRES_BIND,
    POSTGRES_SERVER_VERSION,
    PREFERRED_DRIVER,
    PostgreSQLSourceClosureError,
    VerifiedSqlSource,
    contract_document,
)


ROOT = Path(__file__).resolve().parents[2]

SOURCE_PATHS = (
    "ops/governed_memory/installation/postgres/canonical_cluster.pgsql.in",
    "ops/governed_memory/installation/postgres/canonical_cluster_rollback.pgsql.in",
    "ops/governed_memory/installation/current/postgres/roles_preflight.pgsql",
    "governed-memory-migrations/0001_foundation/forward.pgsql",
    "governed-memory-migrations/0001_foundation/rollback.pgsql",
    "governed-memory-migrations/0003_owner_claim_detail/forward.pgsql",
    "governed-memory-migrations/0003_owner_claim_detail/rollback.pgsql",
    "governed-memory-migrations/0004_pilot_marker/forward.pgsql",
    "governed-memory-migrations/0004_pilot_marker/rollback.pgsql",
)


def _sources() -> dict[str, VerifiedSqlSource]:
    result = {}
    for relative in SOURCE_PATHS:
        raw = (ROOT / relative).read_bytes()
        result[relative] = VerifiedSqlSource(
            relative,
            raw,
            hashlib.sha256(raw).hexdigest(),
        )
    return result


class PostgreSQLSourceClosureTests(unittest.TestCase):
    def test_source_closure_is_exact_and_drift_refuses(self) -> None:
        document = contract_document(_sources())
        self.assertEqual(
            tuple(item["path"] for item in document["source_closure"]),
            tuple(sorted(SOURCE_PATHS)),
        )

        missing = _sources()
        missing.pop(next(iter(missing)))
        with self.assertRaisesRegex(
            PostgreSQLSourceClosureError, "postgres_source_closure_invalid"
        ):
            contract_document(missing)

        source = _sources()[SOURCE_PATHS[0]]
        with self.assertRaisesRegex(
            PostgreSQLSourceClosureError, "postgres_verified_source_invalid"
        ):
            replace(source, content=source.content + b"\n")

    def test_contract_has_roles_timeouts_lock_and_complete_semantic_requirements(
        self,
    ) -> None:
        document = contract_document(_sources())
        self.assertEqual(document["endpoint"]["bind"], POSTGRES_BIND)
        self.assertEqual(document["server_version"], POSTGRES_SERVER_VERSION)
        self.assertEqual(
            document["driver"]["preferred_distribution"], PREFERRED_DRIVER
        )
        session = document["session_contract"]
        self.assertEqual(session["session_user_fixed"], "governed_memory_bootstrap")
        self.assertEqual(session["connect_timeout_seconds"], 5)
        self.assertEqual(session["lock_timeout_milliseconds"], 1000)
        self.assertEqual(session["statement_timeout_milliseconds"], 15000)
        self.assertTrue(session["set_role_must_be_verified_per_phase"])
        self.assertTrue(session["advisory_lock_acquired_before_first_observation"])
        self.assertTrue(session["advisory_lock_held_through_terminal_or_rollback_receipt"])

        semantics = document["semantic_equivalence_requirements"]
        for key in (
            "auto_explain_privacy_predicate_required",
            "pgaudit_absence_predicate_required",
            "pgcrypto_exact_member_count_owner_and_dependency_closure_required",
            "canonical_rollback_all_identity_acl_dependency_and_prefix_predicates_required",
            "rollback_prefix_machine_required",
            "fixed_terminal_catalog_queries_required",
            "catalog_normalization_algorithm_required",
            "approved_catalog_manifest_is_independent_package_authority",
        ):
            self.assertTrue(semantics[key], key)
        for phase in document["native_phases"]:
            self.assertFalse(phase["translation_complete"])
            self.assertTrue(phase["required_current_roles"])

    def test_no_caller_sql_stage_tuple_dsn_or_catalog_surface_exists(self) -> None:
        exported = set(subject.__all__)
        for forbidden in (
            "DriverStage",
            "ClosedPostgreSQLStageMachine",
            "FixedSyncPostgreSQLPrimitive",
            "PostgreSQLPrefixSnapshot",
            "build_stage_plan",
        ):
            self.assertNotIn(forbidden, exported)
            self.assertFalse(hasattr(subject, forbidden))
        signature = inspect.signature(contract_document)
        self.assertEqual(tuple(signature.parameters), ("sources",))
        module_text = Path(subject.__file__).read_text(encoding="ascii")
        self.assertNotRegex(
            module_text,
            r"(?m)^(?:from|import) (?:psycopg|socket|subprocess)\b",
        )

    def test_execution_gate_is_unconditionally_closed(self) -> None:
        document = contract_document(_sources())
        gate = document["execution_gate"]
        self.assertEqual(gate["executable_stage_count"], 0)
        self.assertTrue(gate["live_execution_must_refuse"])
        self.assertFalse(gate["driver_native_translation_complete"])
        self.assertFalse(gate["rollback_prefix_machine_complete"])
        self.assertFalse(gate["fixed_catalog_queries_and_normalization_complete"])
        self.assertFalse(gate["approved_terminal_catalog_manifest_selected"])
        self.assertFalse(
            gate["caller_supplied_sql_stage_tuple_or_expected_catalog_allowed"]
        )
        self.assertFalse(hasattr(subject, "construct_executable_transport"))

    def test_checked_in_contract_is_deterministic_and_zero_effect(self) -> None:
        document = contract_document(_sources())
        checked_in = json.loads(
            (
                ROOT
                / "ops/governed_memory/installation/current/postgres/source_closure_contract.json"
            ).read_text(encoding="ascii")
        )
        self.assertEqual(checked_in, document)
        self.assertEqual(
            set(document["repository_phase_effect_counts"].values()), {0}
        )
        self.assertRegex(document["contract_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()

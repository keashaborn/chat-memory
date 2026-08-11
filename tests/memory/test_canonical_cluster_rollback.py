from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
POSTGRES = ROOT / "ops/governed_memory/installation/postgres"
FORWARD = POSTGRES / "canonical_cluster.pgsql.in"
ROLLBACK = POSTGRES / "canonical_cluster_rollback.pgsql.in"

TARGET_ROLES = (
    "governed_memory_owner",
    "governed_memory_api",
    "governed_memory_worker",
    "memory_ingest_writer",
    "memory_erasure_requester",
)


class CanonicalClusterRollbackContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.forward = FORWARD.read_text(encoding="utf-8")
        cls.rollback = ROLLBACK.read_text(encoding="utf-8")

    def test_only_exact_target_database_and_roles_are_dropped(self) -> None:
        upper = self.rollback.upper()
        self.assertNotIn("CASCADE", upper)
        self.assertNotIn("DROP OWNED", upper)
        self.assertNotIn("DROP EXTENSION", upper)
        self.assertEqual(upper.count("DROP DATABASE GOVERNED_MEMORY;"), 1)

        dropped_roles = re.findall(
            r"(?m)^DROP ROLE IF EXISTS ([a-z0-9_]+);$", self.rollback
        )
        self.assertEqual(set(dropped_roles), set(TARGET_ROLES))
        self.assertEqual(len(dropped_roles), len(TARGET_ROLES))
        self.assertNotRegex(
            self.rollback,
            re.compile(
                r"\bDROP\s+(?:DATABASE|ROLE)\b[^;\n]*(?:LIKE|SIMILAR|\*|%)",
                re.IGNORECASE,
            ),
        )
        self.assertEqual(
            set(re.findall(r"(?m)^\\connect ([a-z0-9_]+)$", self.rollback)),
            {"postgres", "governed_memory"},
        )

    def test_canonical_psql_refusals_exit_through_server_errors(self) -> None:
        for source in (self.forward, self.rollback):
            self.assertIn("\\set ON_ERROR_STOP on", source)
            self.assertNotRegex(
                source,
                re.compile(r"(?mi)^[ \t]*\\(?:q|quit)(?:[ \t]|$)"),
            )

        refusal_messages = (
            "fresh successor cluster preflight failed",
            "fresh successor cluster postflight failed",
            "required governed_memory_empty_cluster_rollback variable is absent",
            "governed_memory_empty_cluster_rollback must be exactly on",
            "canonical database gained an active or prepared client",
            "canonical cluster rollback postflight failed",
        )
        combined = self.forward + self.rollback
        self.assertEqual(combined.count("ERRCODE = 'P0001'"), 6)
        for message in refusal_messages:
            self.assertEqual(combined.count(f"MESSAGE = '{message}'"), 1)

    def test_every_forward_role_prefix_is_explicitly_modeled(self) -> None:
        created_roles = re.findall(
            r"(?m)^CREATE ROLE ([a-z0-9_]+)$", self.forward
        )
        self.assertEqual(tuple(created_roles), TARGET_ROLES)
        self.assertIn("expected_role_index IN 1..5", self.rollback)
        self.assertIn(
            "expected_role_index <= target_role_count", self.rollback
        )
        self.assertIn(
            "governed_memory_roles_only_recovery_preflight", self.rollback
        )
        self.assertIn("DROP ROLE IF EXISTS", self.rollback)

    def test_database_statement_boundaries_are_closed_finite_states(self) -> None:
        for state in ("'default'", "'public_revoked'", "'runtime_connect'"):
            self.assertIn(state, self.rollback)
        for boundary in (
            "canonical database shared dependencies differ",
            "canonical role membership graph differs",
            "canonical public schema owner or ACL differs",
            "pre-pgcrypto extension set differs",
            "pgcrypto extension member set differs",
            "pgcrypto trusted extension shared dependency differs",
            "canonical database gained an active or prepared client",
            "cluster has a replication slot",
        ):
            self.assertIn(boundary, self.rollback)

        self.assertEqual(
            self.rollback.count("DROP DATABASE governed_memory;"), 1
        )
        self.assertLess(
            self.rollback.index("DO $empty_database_preflight$"),
            self.rollback.index("DROP DATABASE governed_memory;"),
        )
        self.assertLess(
            self.rollback.index("DROP DATABASE governed_memory;"),
            self.rollback.index(
                "DO $governed_memory_roles_only_recovery_preflight$"
            ),
        )

    def test_pgcrypto_member_and_shared_dependency_sets_are_exact(self) -> None:
        expected_block = self.rollback.split(
            "WITH expected(proname, argument_types) AS (", 1
        )[1].split("),\n      actual AS (", 1)[0]
        self.assertEqual(expected_block.count("::name, ARRAY["), 36)
        self.assertNotIn("pg_catalog.count(*) = 37", self.rollback)
        self.assertIn("pg_catalog.count(*) = 1", self.rollback)
        self.assertIn("dependency.deptype = 'e'", self.rollback)
        self.assertIn("dependency.deptype = 'o'", self.rollback)
        self.assertIn(
            "procedure.proowner <> bootstrap_role_oid", self.rollback
        )
        self.assertNotIn(
            "procedure.proowner <> owner_role_oid", self.rollback
        )
        shared_dependency_match = re.search(
            r"IF NOT \(\n"
            r"      SELECT pg_catalog\.count\(\*\) = 1\n"
            r"(?P<body>.*?)"
            r"RAISE EXCEPTION "
            r"'pgcrypto trusted extension shared dependency differs';",
            self.rollback,
            re.DOTALL,
        )
        self.assertIsNotNone(shared_dependency_match)
        self.assertNotIn(
            "member_dependency", shared_dependency_match.group("body")
        )
        self.assertIn(
            "procedure.probin IS DISTINCT FROM '$libdir/pgcrypto'",
            self.rollback,
        )
        for exact_property in (
            "procedure.prosrc IS DISTINCT FROM CASE",
            "procedure.prorettype IS DISTINCT FROM CASE",
            "procedure.pronargs <>",
            "procedure.provolatile IS DISTINCT FROM CASE",
            "procedure.proisstrict IS DISTINCT FROM",
            "procedure.proparallel IS DISTINCT FROM",
            "procedure.pronargdefaults <> 0",
            "procedure.prosupport <> 0",
            "procedure.procost <> 1",
            "procedure.prorows <> CASE",
            "FROM pg_catalog.pg_description AS description",
            "standard public schema",
            "cryptographic functions",
            "canonical database-local comments or labels differ",
            "FROM pg_catalog.pg_seclabel",
        ):
            self.assertIn(exact_property, self.rollback)
        self.assertIn("extension.extversion = '1.3'", self.rollback)

    def test_forward_public_acl_postflight_uses_acl_grantee_zero(self) -> None:
        self.assertNotRegex(
            self.forward,
            re.compile(
                r"has_(?:database|schema)_privilege\(\s*'PUBLIC'",
                re.IGNORECASE,
            ),
        )
        self.assertEqual(self.forward.count("privilege.grantee = 0"), 2)
        self.assertIn("privilege.privilege_type = 'CONNECT'", self.forward)
        self.assertIn("privilege.privilege_type = 'CREATE'", self.forward)
        self.assertIn("pg_catalog.acldefault('d', database.datdba)", self.forward)
        self.assertIn(
            "pg_catalog.acldefault('n', namespace.nspowner)", self.forward
        )

    def test_forward_postflight_pins_trusted_extension_ownership(self) -> None:
        self.assertIn("extension.extversion = '1.3'", self.forward)
        self.assertIn(
            "extension_owner.rolname = 'governed_memory_owner'",
            self.forward,
        )
        self.assertIn("SELECT pg_catalog.count(*) = 36", self.forward)
        self.assertIn(
            "pg_catalog.pg_get_userbyid(procedure.proowner) =\n"
            "          'governed_memory_bootstrap'",
            self.forward,
        )

    def test_no_application_or_source_database_target_is_present(self) -> None:
        lowered = self.rollback.lower()
        for forbidden in (
            "conversation_threads",
            "chat_transcripts",
            "lifeswitch",
            "source_cluster_roles",
            "0002_conversation_bridge",
        ):
            self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()

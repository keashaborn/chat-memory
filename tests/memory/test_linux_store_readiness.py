from __future__ import annotations

from dataclasses import replace
import unittest

from tools.governed_memory_install.linux_store_readiness import (
    CanonicalPostgreSQLRole,
    ClosedStoreReadinessProbe,
    LinuxStoreReadinessError,
    PostgreSQLCatalogIdentity,
    PostgreSQLRoleMembership,
    PrebootstrapPostgreSQLSnapshot,
    PrebootstrapQdrantSnapshot,
    QdrantCollectionConfiguration,
    TerminalPostgreSQLSnapshot,
    TerminalQdrantSnapshot,
)
from tools.governed_memory_install.store_readiness import (
    COLLECTION,
    EXPECTED_QDRANT_COLLECTION_CONFIG_SHA256,
    POSTGRES_BIND,
    QDRANT_BIND,
    REQUIRED_ROLE_NAMES,
    TERMINAL_MIGRATION_IDS,
)


def _roles() -> tuple[CanonicalPostgreSQLRole, ...]:
    return tuple(
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
    )


def _terminal_postgres() -> TerminalPostgreSQLSnapshot:
    return TerminalPostgreSQLSnapshot(
        bind=POSTGRES_BIND,
        server_major=16,
        database="governed_memory",
        applied_migration_ids=TERMINAL_MIGRATION_IDS,
        roles=_roles(),
        memberships=(
            PostgreSQLRoleMembership(
                "governed_memory_owner", "governed_memory_bootstrap"
            ),
        ),
        catalog_identities=(
            PostgreSQLCatalogIdentity(
                "schema",
                "memory",
                "memory",
                "governed_memory_owner",
                "a" * 64,
            ),
        ),
        governed_user_row_count=0,
        active_client_count=0,
        source_connection_count=0,
    )


def _terminal_qdrant() -> TerminalQdrantSnapshot:
    return TerminalQdrantSnapshot(
        bind=QDRANT_BIND,
        server_version="1.19.0",
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


class _Postgres:
    bind = POSTGRES_BIND

    def __init__(self) -> None:
        self.prebootstrap = PrebootstrapPostgreSQLSnapshot(
            bind=POSTGRES_BIND,
            server_major=16,
            connected_database="postgres",
            target_database_exists=False,
            present_target_role_names=(),
            source_connection_count=0,
        )
        self.terminal = _terminal_postgres()

    def inspect_prebootstrap(self) -> PrebootstrapPostgreSQLSnapshot:
        return self.prebootstrap

    def inspect_terminal(self) -> TerminalPostgreSQLSnapshot:
        return self.terminal


class _Qdrant:
    bind = QDRANT_BIND

    def __init__(self) -> None:
        self.prebootstrap = PrebootstrapQdrantSnapshot(
            bind=QDRANT_BIND,
            server_version="1.19.0",
            collection_exists=False,
            point_count=0,
            source_endpoint_count=0,
        )
        self.terminal = _terminal_qdrant()

    def inspect_prebootstrap(self) -> PrebootstrapQdrantSnapshot:
        return self.prebootstrap

    def inspect_terminal(self) -> TerminalQdrantSnapshot:
        return self.terminal


class LinuxStoreReadinessTests(unittest.TestCase):
    def test_fixed_prebootstrap_probe_proves_database_and_all_five_roles_absent(
        self,
    ) -> None:
        postgres = _Postgres()
        qdrant = _Qdrant()
        receipt = ClosedStoreReadinessProbe(
            postgres=postgres, qdrant=qdrant
        ).verify_fresh_empty_stores()
        self.assertEqual(receipt.postgres.connected_database, "postgres")
        self.assertEqual(receipt.postgres.target_database, "governed_memory")
        self.assertFalse(receipt.postgres.target_database_exists)
        self.assertEqual(receipt.postgres.required_role_names, REQUIRED_ROLE_NAMES)
        self.assertEqual(receipt.postgres.present_target_role_names, ())
        self.assertEqual(receipt.postgres.source_connection_count, 0)
        self.assertEqual(receipt.qdrant.point_count, 0)
        self.assertEqual(receipt.production_read_count, 0)
        self.assertEqual(receipt.provider_call_count, 0)

    def test_prebootstrap_probe_refuses_target_database_or_role_presence(self) -> None:
        for mutation in (
            {"target_database_exists": True},
            {"present_target_role_names": ("governed_memory_owner",)},
        ):
            with self.subTest(mutation=mutation):
                postgres = _Postgres()
                postgres.prebootstrap = replace(postgres.prebootstrap, **mutation)
                with self.assertRaises(LinuxStoreReadinessError):
                    ClosedStoreReadinessProbe(
                        postgres=postgres, qdrant=_Qdrant()
                    ).verify_fresh_empty_stores()

    def test_terminal_receipt_hashes_normalized_catalog_role_graph_and_config(
        self,
    ) -> None:
        receipt = ClosedStoreReadinessProbe(
            postgres=_Postgres(), qdrant=_Qdrant()
        ).verify_terminal_canonical_stores()
        self.assertEqual(receipt.postgres.applied_migration_ids, TERMINAL_MIGRATION_IDS)
        self.assertRegex(receipt.postgres.exact_role_graph_sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(receipt.postgres.canonical_catalog_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(receipt.postgres.governed_user_row_count, 0)
        self.assertEqual(receipt.postgres.active_client_count, 0)
        self.assertEqual(
            receipt.qdrant.collection_config_sha256,
            EXPECTED_QDRANT_COLLECTION_CONFIG_SHA256,
        )
        self.assertEqual(receipt.qdrant.unexpected_candidate_collection_count, 0)
        self.assertEqual(receipt.qdrant.point_count, 0)

    def test_transports_are_fixed_loopback_and_exceptions_are_content_free(self) -> None:
        postgres = _Postgres()
        postgres.bind = "10.0.0.4:5432"
        with self.assertRaisesRegex(
            LinuxStoreReadinessError, "readiness_transport_invalid"
        ):
            ClosedStoreReadinessProbe(postgres=postgres, qdrant=_Qdrant())

        class FailingPostgres(_Postgres):
            def inspect_prebootstrap(self) -> PrebootstrapPostgreSQLSnapshot:
                raise RuntimeError("never-persist-this-secret")

        error: LinuxStoreReadinessError | None = None
        try:
            ClosedStoreReadinessProbe(
                postgres=FailingPostgres(), qdrant=_Qdrant()
            ).verify_fresh_empty_stores()
        except LinuxStoreReadinessError as caught:
            error = caught
        self.assertIsNotNone(error)
        self.assertEqual(str(error), "fresh_store_probe_failed")
        self.assertIsNone(error.__cause__)
        self.assertNotIn("secret", repr(error).lower())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

"""Closed, content-free readiness probes for the two dormant stores.

The adapter accepts no endpoint, SQL, HTTP path, credential, or arbitrary
command from its caller.  A separately pinned transport implementation must
be constructed for the two fixed loopback endpoints and may return only the
bounded normalized snapshots below.  Receipt hashes are computed here rather
than accepted from the transport.
"""

from dataclasses import dataclass
import hashlib
import json
import re
from time import monotonic as _monotonic, sleep as _sleep
from typing import Final, Mapping, Protocol

from .store_readiness import (
    ALIAS,
    BOOTSTRAP_DATABASE,
    COLLECTION,
    DATABASE,
    EXPECTED_QDRANT_COLLECTION_CONFIG_SHA256,
    POSTGRES_BIND,
    POSTGRES_SERVER_VERSION,
    QDRANT_BIND,
    QDRANT_SERVER_VERSION,
    REQUIRED_ROLE_NAMES,
    TERMINAL_MIGRATION_IDS,
    EmptyStoreReadiness,
    PrebootstrapPostgreSQLReadiness,
    QdrantReadiness,
    TerminalCanonicalStoreReadiness,
    TerminalPostgreSQLReadiness,
    TerminalQdrantReadiness,
)


MAX_CATALOG_IDENTITIES: Final = 4096
MAX_IDENTITY_TEXT_BYTES: Final = 512
READINESS_TRANSPORT_TIMEOUT_SECONDS: Final = 60.0
READINESS_TRANSPORT_RETRY_INTERVAL_SECONDS: Final = 0.1
_RETRYABLE_TRANSPORT_FAILURE_CODES: Final = frozenset(
    {
        "postgres_fixed_connect_failed",
        "qdrant_exchange_failed",
    }
)
_SAFE_IDENTIFIER_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_.:()\[\], -]{0,511}\Z", re.ASCII
)
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class LinuxStoreReadinessError(RuntimeError):
    """Content-free refusal from a fixed dormant-store readiness probe."""


def _inspect_transport_pair(
    postgres_inspect: object,
    qdrant_inspect: object,
    *,
    failure_code: str,
) -> tuple[object, object]:
    """Retry only transport-call failures within one fixed startup window."""

    if (
        not callable(postgres_inspect)
        or not callable(qdrant_inspect)
        or failure_code not in {
            "fresh_store_probe_failed",
            "terminal_store_probe_failed",
        }
    ):
        raise LinuxStoreReadinessError("readiness_transport_invalid")
    deadline = _monotonic() + READINESS_TRANSPORT_TIMEOUT_SECONDS
    while True:
        try:
            return postgres_inspect(), qdrant_inspect()
        except Exception as error:
            if str(error) not in _RETRYABLE_TRANSPORT_FAILURE_CODES:
                raise LinuxStoreReadinessError(failure_code) from None
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise LinuxStoreReadinessError(failure_code) from None
            _sleep(
                min(READINESS_TRANSPORT_RETRY_INTERVAL_SECONDS, remaining)
            )


def _canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise LinuxStoreReadinessError("readiness_evidence_not_canonical") from None
    return hashlib.sha256(encoded).hexdigest()


def _safe_identity_text(value: object) -> bool:
    return (
        type(value) is str
        and len(value.encode("ascii", errors="ignore")) <= MAX_IDENTITY_TEXT_BYTES
        and _SAFE_IDENTIFIER_RE.fullmatch(value) is not None
    )


@dataclass(frozen=True, slots=True)
class PrebootstrapPostgreSQLSnapshot:
    """Normalized observation made through the fixed bootstrap database."""

    bind: str
    server_major: int
    server_version: str
    connected_database: str
    target_database_exists: bool
    present_target_role_names: tuple[str, ...]
    source_connection_count: int

    def __post_init__(self) -> None:
        if (
            self.bind != POSTGRES_BIND
            or type(self.server_major) is not int
            or self.server_major != 16
            or self.server_version != POSTGRES_SERVER_VERSION
            or self.connected_database != BOOTSTRAP_DATABASE
            or type(self.target_database_exists) is not bool
            or type(self.present_target_role_names) is not tuple
            or tuple(sorted(set(self.present_target_role_names)))
            != self.present_target_role_names
            or any(name not in REQUIRED_ROLE_NAMES for name in self.present_target_role_names)
            or type(self.source_connection_count) is not int
            or self.source_connection_count < 0
        ):
            raise LinuxStoreReadinessError(
                "prebootstrap_postgres_snapshot_invalid"
            )


@dataclass(frozen=True, slots=True)
class PrebootstrapQdrantSnapshot:
    bind: str
    server_version: str
    collection_exists: bool
    point_count: int
    source_endpoint_count: int

    def __post_init__(self) -> None:
        if (
            self.bind != QDRANT_BIND
            or type(self.server_version) is not str
            or self.server_version != QDRANT_SERVER_VERSION
            or self.collection_exists is not False
            or type(self.point_count) is not int
            or self.point_count != 0
            or type(self.source_endpoint_count) is not int
            or self.source_endpoint_count != 0
        ):
            raise LinuxStoreReadinessError("prebootstrap_qdrant_snapshot_invalid")


@dataclass(frozen=True, slots=True)
class CanonicalPostgreSQLRole:
    role_name: str
    can_login: bool
    inherit: bool
    superuser: bool
    create_database: bool
    create_role: bool
    replication: bool
    bypass_rls: bool

    def __post_init__(self) -> None:
        if (
            self.role_name not in REQUIRED_ROLE_NAMES
            or any(
                type(value) is not bool
                for value in (
                    self.can_login,
                    self.inherit,
                    self.superuser,
                    self.create_database,
                    self.create_role,
                    self.replication,
                    self.bypass_rls,
                )
            )
        ):
            raise LinuxStoreReadinessError("terminal_postgres_role_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "bypass_rls": self.bypass_rls,
            "can_login": self.can_login,
            "create_database": self.create_database,
            "create_role": self.create_role,
            "inherit": self.inherit,
            "replication": self.replication,
            "role_name": self.role_name,
            "superuser": self.superuser,
        }


@dataclass(frozen=True, slots=True)
class PostgreSQLRoleMembership:
    granted_role: str
    member_role: str

    def __post_init__(self) -> None:
        if (self.granted_role, self.member_role) != (
            "governed_memory_owner",
            "governed_memory_bootstrap",
        ):
            raise LinuxStoreReadinessError(
                "terminal_postgres_role_membership_invalid"
            )

    def as_dict(self) -> dict[str, str]:
        return {
            "granted_role": self.granted_role,
            "member_role": self.member_role,
        }


@dataclass(frozen=True, slots=True)
class PostgreSQLCatalogIdentity:
    """One normalized, content-free catalog identity.

    ``definition_sha256`` binds definitions without retaining SQL, row values,
    credentials, or application content.
    """

    object_kind: str
    schema_name: str
    object_name: str
    owner_name: str
    definition_sha256: str

    def __post_init__(self) -> None:
        if (
            not _safe_identity_text(self.object_kind)
            or not _safe_identity_text(self.schema_name)
            or not _safe_identity_text(self.object_name)
            or not _safe_identity_text(self.owner_name)
            or _HASH_RE.fullmatch(self.definition_sha256) is None
        ):
            raise LinuxStoreReadinessError(
                "terminal_postgres_catalog_identity_invalid"
            )

    def as_dict(self) -> dict[str, str]:
        return {
            "definition_sha256": self.definition_sha256,
            "object_kind": self.object_kind,
            "object_name": self.object_name,
            "owner_name": self.owner_name,
            "schema_name": self.schema_name,
        }


@dataclass(frozen=True, slots=True)
class TerminalPostgreSQLSnapshot:
    bind: str
    server_major: int
    server_version: str
    database: str
    applied_migration_ids: tuple[str, ...]
    roles: tuple[CanonicalPostgreSQLRole, ...]
    memberships: tuple[PostgreSQLRoleMembership, ...]
    catalog_identities: tuple[PostgreSQLCatalogIdentity, ...]
    governed_user_row_count: int
    active_client_count: int
    source_connection_count: int

    def __post_init__(self) -> None:
        expected_roles = tuple(
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
        if (
            self.bind != POSTGRES_BIND
            or type(self.server_major) is not int
            or self.server_major != 16
            or self.server_version != POSTGRES_SERVER_VERSION
            or self.database != DATABASE
            or self.applied_migration_ids != TERMINAL_MIGRATION_IDS
            or self.roles != expected_roles
            or self.memberships
            != (
                PostgreSQLRoleMembership(
                    "governed_memory_owner", "governed_memory_bootstrap"
                ),
            )
            or type(self.catalog_identities) is not tuple
            or not 1 <= len(self.catalog_identities) <= MAX_CATALOG_IDENTITIES
            or any(
                type(item) is not PostgreSQLCatalogIdentity
                for item in self.catalog_identities
            )
            or tuple(sorted(self.catalog_identities, key=lambda item: (
                item.schema_name,
                item.object_kind,
                item.object_name,
                item.owner_name,
                item.definition_sha256,
            )))
            != self.catalog_identities
            or len(set(self.catalog_identities)) != len(self.catalog_identities)
            or type(self.governed_user_row_count) is not int
            or self.governed_user_row_count != 0
            or type(self.active_client_count) is not int
            or self.active_client_count != 0
            or type(self.source_connection_count) is not int
            or self.source_connection_count != 0
        ):
            raise LinuxStoreReadinessError("terminal_postgres_snapshot_invalid")


@dataclass(frozen=True, slots=True)
class QdrantCollectionConfiguration:
    vector_size: int
    distance: str
    on_disk_payload: bool
    replication_factor: int

    def __post_init__(self) -> None:
        if (
            type(self.vector_size) is not int
            or self.vector_size != 3072
            or type(self.distance) is not str
            or self.distance != "Dot"
            or type(self.on_disk_payload) is not bool
            or self.on_disk_payload is not True
            or type(self.replication_factor) is not int
            or self.replication_factor != 1
        ):
            raise LinuxStoreReadinessError(
                "terminal_qdrant_collection_config_invalid"
            )

    def canonical_document(self) -> dict[str, object]:
        return {
            "on_disk_payload": self.on_disk_payload,
            "replication_factor": self.replication_factor,
            "vectors": {
                "distance": self.distance,
                "size": self.vector_size,
            },
        }


@dataclass(frozen=True, slots=True)
class TerminalQdrantSnapshot:
    bind: str
    server_version: str
    collection_exists: bool
    alias_target: str
    collection_config: QdrantCollectionConfiguration
    point_count: int
    unexpected_candidate_collection_count: int
    source_endpoint_count: int

    def __post_init__(self) -> None:
        if (
            self.bind != QDRANT_BIND
            or type(self.server_version) is not str
            or self.server_version != QDRANT_SERVER_VERSION
            or self.collection_exists is not True
            or self.alias_target != COLLECTION
            or type(self.collection_config) is not QdrantCollectionConfiguration
            or type(self.point_count) is not int
            or self.point_count != 0
            or type(self.unexpected_candidate_collection_count) is not int
            or self.unexpected_candidate_collection_count != 0
            or type(self.source_endpoint_count) is not int
            or self.source_endpoint_count != 0
        ):
            raise LinuxStoreReadinessError("terminal_qdrant_snapshot_invalid")


class FixedPostgreSQLReadinessTransport(Protocol):
    """Pinned driver at 127.0.0.1:55434; no caller-selected query surface."""

    bind: str

    def inspect_prebootstrap(self) -> PrebootstrapPostgreSQLSnapshot: ...

    def inspect_terminal(self) -> TerminalPostgreSQLSnapshot: ...


class FixedQdrantReadinessTransport(Protocol):
    """Pinned HTTP client at 127.0.0.1:6345; no caller-selected path."""

    bind: str

    def inspect_prebootstrap(self) -> PrebootstrapQdrantSnapshot: ...

    def inspect_terminal(self) -> TerminalQdrantSnapshot: ...


class ClosedStoreReadinessProbe:
    """Concrete readiness composition over two closed, fixed transports."""

    def __init__(
        self,
        *,
        postgres: FixedPostgreSQLReadinessTransport,
        qdrant: FixedQdrantReadinessTransport,
        expected_postgres_catalog_sha256: str,
    ) -> None:
        if (
            getattr(postgres, "bind", None) != POSTGRES_BIND
            or getattr(qdrant, "bind", None) != QDRANT_BIND
            or not callable(getattr(postgres, "inspect_prebootstrap", None))
            or not callable(getattr(postgres, "inspect_terminal", None))
            or not callable(getattr(qdrant, "inspect_prebootstrap", None))
            or not callable(getattr(qdrant, "inspect_terminal", None))
            or _HASH_RE.fullmatch(expected_postgres_catalog_sha256) is None
        ):
            raise LinuxStoreReadinessError("readiness_transport_invalid")
        self._postgres = postgres
        self._qdrant = qdrant
        self._expected_postgres_catalog_sha256 = (
            expected_postgres_catalog_sha256
        )

    @staticmethod
    def _prebootstrap_postgres_receipt(
        snapshot: PrebootstrapPostgreSQLSnapshot,
    ) -> str:
        return _canonical_sha256(
            {
                "schema_version": (
                    "governed-memory-prebootstrap-postgres-readiness-v1"
                ),
                "bind": snapshot.bind,
                "connected_database": snapshot.connected_database,
                "present_target_role_names": list(
                    snapshot.present_target_role_names
                ),
                "required_role_names": list(REQUIRED_ROLE_NAMES),
                "server_major": snapshot.server_major,
                "server_version": snapshot.server_version,
                "source_connection_count": snapshot.source_connection_count,
                "target_database": DATABASE,
                "target_database_exists": snapshot.target_database_exists,
            }
        )

    @staticmethod
    def _prebootstrap_qdrant_receipt(
        snapshot: PrebootstrapQdrantSnapshot,
    ) -> str:
        return _canonical_sha256(
            {
                "schema_version": (
                    "governed-memory-prebootstrap-qdrant-readiness-v1"
                ),
                "alias": ALIAS,
                "bind": snapshot.bind,
                "collection": COLLECTION,
                "collection_exists": snapshot.collection_exists,
                "point_count": snapshot.point_count,
                "server_version": snapshot.server_version,
                "source_endpoint_count": snapshot.source_endpoint_count,
            }
        )

    def verify_fresh_empty_stores(self) -> EmptyStoreReadiness:
        postgres, qdrant = _inspect_transport_pair(
            self._postgres.inspect_prebootstrap,
            self._qdrant.inspect_prebootstrap,
            failure_code="fresh_store_probe_failed",
        )
        if (
            type(postgres) is not PrebootstrapPostgreSQLSnapshot
            or postgres.target_database_exists is not False
            or postgres.present_target_role_names != ()
            or postgres.source_connection_count != 0
            or type(qdrant) is not PrebootstrapQdrantSnapshot
        ):
            raise LinuxStoreReadinessError("fresh_store_probe_not_empty")
        postgres_receipt = PrebootstrapPostgreSQLReadiness(
            bind=postgres.bind,
            server_major=postgres.server_major,
            server_version=postgres.server_version,
            connected_database=postgres.connected_database,
            target_database=DATABASE,
            target_database_exists=postgres.target_database_exists,
            required_role_names=REQUIRED_ROLE_NAMES,
            present_target_role_names=postgres.present_target_role_names,
            source_connection_count=postgres.source_connection_count,
            receipt_sha256=self._prebootstrap_postgres_receipt(postgres),
        )
        qdrant_receipt = QdrantReadiness(
            bind=qdrant.bind,
            server_version=qdrant.server_version,
            collection=COLLECTION,
            alias=ALIAS,
            collection_exists=qdrant.collection_exists,
            point_count=qdrant.point_count,
            source_endpoint_count=qdrant.source_endpoint_count,
            receipt_sha256=self._prebootstrap_qdrant_receipt(qdrant),
        )
        return EmptyStoreReadiness.create(postgres_receipt, qdrant_receipt)

    @staticmethod
    def _terminal_postgres_receipt(
        snapshot: TerminalPostgreSQLSnapshot,
        *,
        role_graph_sha256: str,
        catalog_sha256: str,
    ) -> str:
        return _canonical_sha256(
            {
                "schema_version": (
                    "governed-memory-terminal-postgres-readiness-v1"
                ),
                "active_client_count": snapshot.active_client_count,
                "applied_migration_ids": list(snapshot.applied_migration_ids),
                "bind": snapshot.bind,
                "canonical_catalog_sha256": catalog_sha256,
                "database": snapshot.database,
                "exact_role_graph_sha256": role_graph_sha256,
                "governed_user_row_count": snapshot.governed_user_row_count,
                "server_major": snapshot.server_major,
                "server_version": snapshot.server_version,
                "source_connection_count": snapshot.source_connection_count,
            }
        )

    @staticmethod
    def _terminal_qdrant_receipt(
        snapshot: TerminalQdrantSnapshot,
        *,
        config_sha256: str,
    ) -> str:
        return _canonical_sha256(
            {
                "schema_version": (
                    "governed-memory-terminal-qdrant-readiness-v1"
                ),
                "alias": ALIAS,
                "alias_target": snapshot.alias_target,
                "bind": snapshot.bind,
                "collection": COLLECTION,
                "collection_config_sha256": config_sha256,
                "collection_exists": snapshot.collection_exists,
                "point_count": snapshot.point_count,
                "server_version": snapshot.server_version,
                "source_endpoint_count": snapshot.source_endpoint_count,
                "unexpected_candidate_collection_count": (
                    snapshot.unexpected_candidate_collection_count
                ),
            }
        )

    def verify_terminal_canonical_stores(
        self,
    ) -> TerminalCanonicalStoreReadiness:
        postgres, qdrant = _inspect_transport_pair(
            self._postgres.inspect_terminal,
            self._qdrant.inspect_terminal,
            failure_code="terminal_store_probe_failed",
        )
        if (
            type(postgres) is not TerminalPostgreSQLSnapshot
            or type(qdrant) is not TerminalQdrantSnapshot
        ):
            raise LinuxStoreReadinessError("terminal_store_probe_invalid")

        role_graph = {
            "memberships": [item.as_dict() for item in postgres.memberships],
            "roles": [item.as_dict() for item in postgres.roles],
            "schema_version": "governed-memory-postgres-role-graph-v1",
        }
        catalog = {
            "identities": [
                item.as_dict() for item in postgres.catalog_identities
            ],
            "schema_version": "governed-memory-postgres-catalog-v1",
        }
        role_graph_sha256 = _canonical_sha256(role_graph)
        catalog_sha256 = _canonical_sha256(catalog)
        if catalog_sha256 != self._expected_postgres_catalog_sha256:
            raise LinuxStoreReadinessError(
                "terminal_postgres_catalog_mismatch"
            )
        config_sha256 = _canonical_sha256(
            qdrant.collection_config.canonical_document()
        )
        if config_sha256 != EXPECTED_QDRANT_COLLECTION_CONFIG_SHA256:
            raise LinuxStoreReadinessError(
                "terminal_qdrant_collection_config_mismatch"
            )

        postgres_receipt = TerminalPostgreSQLReadiness(
            bind=postgres.bind,
            server_major=postgres.server_major,
            server_version=postgres.server_version,
            database=postgres.database,
            applied_migration_ids=postgres.applied_migration_ids,
            exact_role_graph_sha256=role_graph_sha256,
            canonical_catalog_sha256=catalog_sha256,
            governed_user_row_count=postgres.governed_user_row_count,
            active_client_count=postgres.active_client_count,
            source_connection_count=postgres.source_connection_count,
            receipt_sha256=self._terminal_postgres_receipt(
                postgres,
                role_graph_sha256=role_graph_sha256,
                catalog_sha256=catalog_sha256,
            ),
        )
        qdrant_receipt = TerminalQdrantReadiness(
            bind=qdrant.bind,
            server_version=qdrant.server_version,
            collection=COLLECTION,
            alias=ALIAS,
            collection_exists=qdrant.collection_exists,
            alias_target=qdrant.alias_target,
            collection_config_sha256=config_sha256,
            point_count=qdrant.point_count,
            unexpected_candidate_collection_count=(
                qdrant.unexpected_candidate_collection_count
            ),
            source_endpoint_count=qdrant.source_endpoint_count,
            receipt_sha256=self._terminal_qdrant_receipt(
                qdrant, config_sha256=config_sha256
            ),
        )
        return TerminalCanonicalStoreReadiness.create(
            postgres_receipt, qdrant_receipt
        )


__all__ = [
    "CanonicalPostgreSQLRole",
    "ClosedStoreReadinessProbe",
    "FixedPostgreSQLReadinessTransport",
    "FixedQdrantReadinessTransport",
    "LinuxStoreReadinessError",
    "PostgreSQLCatalogIdentity",
    "PostgreSQLRoleMembership",
    "PrebootstrapPostgreSQLSnapshot",
    "PrebootstrapQdrantSnapshot",
    "QdrantCollectionConfiguration",
    "TerminalPostgreSQLSnapshot",
    "TerminalQdrantSnapshot",
]

from __future__ import annotations

"""Content-free readiness evidence for dormant-store installation.

Fresh readiness is valid only before schema and Qdrant bootstrap.  Terminal
readiness is a separate proof over the fully migrated canonical stores.
"""

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Final, Protocol


_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
POSTGRES_BIND: Final = "127.0.0.1:55432"
QDRANT_BIND: Final = "127.0.0.1:6343"
DATABASE: Final = "governed_memory"
COLLECTION: Final = "governed_memory_9a54cf123493_000001"
ALIAS: Final = "governed_memory_active"
TERMINAL_MIGRATION_IDS: Final = (
    "governed_memory_foundation_0001",
    "governed_memory_owner_claim_detail_0003",
    "governed_memory_pilot_marker_0004",
)


class StoreReadinessError(RuntimeError):
    """Content-free refusal for incomplete or nonempty store evidence."""


@dataclass(frozen=True, slots=True)
class PostgreSQLReadiness:
    bind: str
    server_major: int
    database: str
    user_schema_count: int
    user_row_count: int
    source_connection_count: int
    receipt_sha256: str

    def __post_init__(self) -> None:
        if (
            self.bind != POSTGRES_BIND
            or type(self.server_major) is not int
            or self.server_major != 16
            or self.database != DATABASE
            or type(self.user_schema_count) is not int
            or self.user_schema_count != 0
            or type(self.user_row_count) is not int
            or self.user_row_count != 0
            or type(self.source_connection_count) is not int
            or self.source_connection_count != 0
            or _HASH_RE.fullmatch(self.receipt_sha256) is None
        ):
            raise StoreReadinessError("postgres_readiness_invalid")


@dataclass(frozen=True, slots=True)
class QdrantReadiness:
    bind: str
    server_version: str
    collection: str
    alias: str
    collection_exists: bool
    point_count: int
    source_endpoint_count: int
    receipt_sha256: str

    def __post_init__(self) -> None:
        if (
            self.bind != QDRANT_BIND
            or not isinstance(self.server_version, str)
            or not self.server_version.startswith("1.19.")
            or self.collection != COLLECTION
            or self.alias != ALIAS
            or self.collection_exists is not False
            or type(self.point_count) is not int
            or self.point_count != 0
            or type(self.source_endpoint_count) is not int
            or self.source_endpoint_count != 0
            or _HASH_RE.fullmatch(self.receipt_sha256) is None
        ):
            raise StoreReadinessError("qdrant_readiness_invalid")


@dataclass(frozen=True, slots=True)
class EmptyStoreReadiness:
    postgres: PostgreSQLReadiness
    qdrant: QdrantReadiness
    provider_call_count: int
    production_read_count: int
    receipt_sha256: str

    @classmethod
    def create(
        cls,
        postgres: PostgreSQLReadiness,
        qdrant: QdrantReadiness,
        *,
        provider_call_count: int = 0,
        production_read_count: int = 0,
    ) -> EmptyStoreReadiness:
        if (
            type(postgres) is not PostgreSQLReadiness
            or type(qdrant) is not QdrantReadiness
            or type(provider_call_count) is not int
            or provider_call_count != 0
            or type(production_read_count) is not int
            or production_read_count != 0
        ):
            raise StoreReadinessError("empty_store_readiness_invalid")
        document = {
            "schema_version": "governed-memory-empty-store-readiness-v1",
            "postgres_receipt_sha256": postgres.receipt_sha256,
            "qdrant_receipt_sha256": qdrant.receipt_sha256,
            "provider_call_count": 0,
            "production_read_count": 0,
        }
        digest = hashlib.sha256(
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).hexdigest()
        return cls(postgres, qdrant, 0, 0, digest)


@dataclass(frozen=True, slots=True)
class TerminalPostgreSQLReadiness:
    """Evidence that the canonical database is migrated but has no user data."""

    bind: str
    server_major: int
    database: str
    applied_migration_ids: tuple[str, ...]
    governed_user_row_count: int
    source_connection_count: int
    receipt_sha256: str

    def __post_init__(self) -> None:
        if (
            self.bind != POSTGRES_BIND
            or type(self.server_major) is not int
            or self.server_major != 16
            or self.database != DATABASE
            or type(self.applied_migration_ids) is not tuple
            or self.applied_migration_ids != TERMINAL_MIGRATION_IDS
            or type(self.governed_user_row_count) is not int
            or self.governed_user_row_count != 0
            or type(self.source_connection_count) is not int
            or self.source_connection_count != 0
            or _HASH_RE.fullmatch(self.receipt_sha256) is None
        ):
            raise StoreReadinessError("terminal_postgres_readiness_invalid")


@dataclass(frozen=True, slots=True)
class TerminalQdrantReadiness:
    """Evidence that the exact canonical collection and alias are empty."""

    bind: str
    server_version: str
    collection: str
    alias: str
    collection_exists: bool
    alias_target: str
    point_count: int
    source_endpoint_count: int
    receipt_sha256: str

    def __post_init__(self) -> None:
        if (
            self.bind != QDRANT_BIND
            or not isinstance(self.server_version, str)
            or not self.server_version.startswith("1.19.")
            or self.collection != COLLECTION
            or self.alias != ALIAS
            or self.collection_exists is not True
            or self.alias_target != COLLECTION
            or type(self.point_count) is not int
            or self.point_count != 0
            or type(self.source_endpoint_count) is not int
            or self.source_endpoint_count != 0
            or _HASH_RE.fullmatch(self.receipt_sha256) is None
        ):
            raise StoreReadinessError("terminal_qdrant_readiness_invalid")


@dataclass(frozen=True, slots=True)
class TerminalCanonicalStoreReadiness:
    """Aggregate terminal proof used by the canonical install receipt."""

    postgres: TerminalPostgreSQLReadiness
    qdrant: TerminalQdrantReadiness
    provider_call_count: int
    production_read_count: int
    receipt_sha256: str

    @staticmethod
    def _digest(
        postgres: TerminalPostgreSQLReadiness,
        qdrant: TerminalQdrantReadiness,
    ) -> str:
        document = {
            "schema_version": (
                "governed-memory-terminal-canonical-store-readiness-v1"
            ),
            "postgres_receipt_sha256": postgres.receipt_sha256,
            "qdrant_receipt_sha256": qdrant.receipt_sha256,
            "provider_call_count": 0,
            "production_read_count": 0,
        }
        return hashlib.sha256(
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).hexdigest()

    def __post_init__(self) -> None:
        if (
            type(self.postgres) is not TerminalPostgreSQLReadiness
            or type(self.qdrant) is not TerminalQdrantReadiness
            or type(self.provider_call_count) is not int
            or self.provider_call_count != 0
            or type(self.production_read_count) is not int
            or self.production_read_count != 0
            or self.receipt_sha256 != self._digest(self.postgres, self.qdrant)
        ):
            raise StoreReadinessError("terminal_store_readiness_invalid")

    @classmethod
    def create(
        cls,
        postgres: TerminalPostgreSQLReadiness,
        qdrant: TerminalQdrantReadiness,
        *,
        provider_call_count: int = 0,
        production_read_count: int = 0,
    ) -> TerminalCanonicalStoreReadiness:
        if (
            type(postgres) is not TerminalPostgreSQLReadiness
            or type(qdrant) is not TerminalQdrantReadiness
            or type(provider_call_count) is not int
            or provider_call_count != 0
            or type(production_read_count) is not int
            or production_read_count != 0
        ):
            raise StoreReadinessError("terminal_store_readiness_invalid")
        return cls(postgres, qdrant, 0, 0, cls._digest(postgres, qdrant))


class StoreReadinessProbe(Protocol):
    """Injected probe. Implementations are outside this pure contract."""

    def verify_fresh_empty_stores(self) -> EmptyStoreReadiness: ...

    def verify_terminal_canonical_stores(
        self,
    ) -> TerminalCanonicalStoreReadiness: ...


__all__ = [
    "ALIAS",
    "COLLECTION",
    "DATABASE",
    "EmptyStoreReadiness",
    "POSTGRES_BIND",
    "PostgreSQLReadiness",
    "QDRANT_BIND",
    "QdrantReadiness",
    "StoreReadinessError",
    "StoreReadinessProbe",
    "TERMINAL_MIGRATION_IDS",
    "TerminalCanonicalStoreReadiness",
    "TerminalPostgreSQLReadiness",
    "TerminalQdrantReadiness",
]

from __future__ import annotations

"""Fail-closed PostgreSQL driver-native source-closure contract.

Phase 9F deliberately packages no executable PostgreSQL transport.  The
checked-in SQL remains the sole semantic authority, but it contains psql
metacommands and therefore cannot be passed to Psycopg.  This module closes
the exact source set and the requirements for a later native translation.  It
has no socket, driver import, subprocess, filesystem-discovery, SQL execution,
DSN, password, or caller-supplied catalog surface.
"""

from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Final, Mapping


POSTGRES_BIND: Final = "127.0.0.1:55432"
POSTGRES_SERVER_VERSION: Final = "16.14"
BOOTSTRAP_DATABASE: Final = "postgres"
TARGET_DATABASE: Final = "governed_memory"
BOOTSTRAP_ROLE: Final = "governed_memory_bootstrap"
OWNER_ROLE: Final = "governed_memory_owner"
ADVISORY_LOCK_KEY: Final = 9_054_123_493_000_001
CONNECT_TIMEOUT_SECONDS: Final = 5
LOCK_TIMEOUT_MILLISECONDS: Final = 1_000
STATEMENT_TIMEOUT_MILLISECONDS: Final = 15_000
MAX_SQL_BYTES: Final = 16 * 1024 * 1024

PREFERRED_DRIVER: Final = "psycopg[binary]==3.3.4"
DRIVER_SELECTION_STATE: Final = (
    "preferred_sync_target_not_locked_not_staged_native_closure_not_inspected"
)
PROVED_FALLBACK: Final = "asyncpg==0.30.0"

CANONICAL_BOOTSTRAP_REFERENCE_PATH: Final = (
    "ops/governed_memory/installation/postgres/canonical_cluster.pgsql.in"
)
CANONICAL_ROLLBACK_REFERENCE_PATH: Final = (
    "ops/governed_memory/installation/postgres/"
    "canonical_cluster_rollback.pgsql.in"
)
ROLES_PREFLIGHT_REFERENCE_PATH: Final = (
    "ops/governed_memory/installation/current/postgres/roles_preflight.pgsql"
)

_SOURCE_SHA256: Final = MappingProxyType(
    {
        CANONICAL_BOOTSTRAP_REFERENCE_PATH: (
            "5e3238856349ca9928d9a20d7658959effdaf6472a795d959ad0707fa9bbbae0"
        ),
        CANONICAL_ROLLBACK_REFERENCE_PATH: (
            "5adec472f5b3277b5a2137fc5e7e8f1d50e587a54302246f6b87cef28442d277"
        ),
        ROLES_PREFLIGHT_REFERENCE_PATH: (
            "21994b66b3d1612180ab458ea6f83258598e963138f604b88ada57ad4f23229e"
        ),
        "governed-memory-migrations/0001_foundation/forward.pgsql": (
            "d67c94be3c4fa026334b87a4683a942cee930eeb1654e66f7b3f73b031927d91"
        ),
        "governed-memory-migrations/0001_foundation/rollback.pgsql": (
            "7d0a1473357e737518bd7de4d6f2599dac8d48ca55254925398b8c5f81b0816e"
        ),
        "governed-memory-migrations/0003_owner_claim_detail/forward.pgsql": (
            "a8f79613f766da18131f52d5f0c0008d3b20f120c76bc1cb3ff64d81712954d5"
        ),
        "governed-memory-migrations/0003_owner_claim_detail/rollback.pgsql": (
            "6f50f6f70ac35ede0a7ae98fa98593f4404457717ca242c8f9fe0bfd57789b68"
        ),
        "governed-memory-migrations/0004_pilot_marker/forward.pgsql": (
            "90b4d1aac35a538a24444d33273e516f484252a9e4a5ea67f495a04499f6ff58"
        ),
        "governed-memory-migrations/0004_pilot_marker/rollback.pgsql": (
            "01da1e6bab14009fe05ce0f644af27bc8cae48988c629f0c0308654b87e31775"
        ),
    }
)

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class PostgreSQLSourceClosureError(RuntimeError):
    """Content-free refusal from the PostgreSQL contract boundary."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise PostgreSQLSourceClosureError(
            "postgres_source_closure_document_invalid"
        ) from None


@dataclass(frozen=True, slots=True)
class VerifiedSqlSource:
    path: str
    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.path) is not str
            or self.path not in _SOURCE_SHA256
            or type(self.content) is not bytes
            or not 1 <= len(self.content) <= MAX_SQL_BYTES
            or b"\x00" in self.content
            or _HASH_RE.fullmatch(self.sha256) is None
            or self.sha256 != _SOURCE_SHA256[self.path]
            or _sha256(self.content) != self.sha256
        ):
            raise PostgreSQLSourceClosureError("postgres_verified_source_invalid")


def _verified_source_closure(
    sources: Mapping[str, VerifiedSqlSource],
) -> tuple[VerifiedSqlSource, ...]:
    if not isinstance(sources, Mapping) or set(sources) != set(_SOURCE_SHA256):
        raise PostgreSQLSourceClosureError("postgres_source_closure_invalid")
    ordered: list[VerifiedSqlSource] = []
    for path in sorted(_SOURCE_SHA256):
        source = sources[path]
        if type(source) is not VerifiedSqlSource:
            raise PostgreSQLSourceClosureError("postgres_source_closure_invalid")
        # Recheck bytes at the boundary; no mutable mapping or stale object can
        # substitute a different source after construction.
        if source.path != path or _sha256(source.content) != _SOURCE_SHA256[path]:
            raise PostgreSQLSourceClosureError("postgres_source_closure_invalid")
        ordered.append(source)
    return tuple(ordered)


def contract_document(
    sources: Mapping[str, VerifiedSqlSource],
) -> dict[str, object]:
    closure = _verified_source_closure(sources)
    phases = (
        {
            "phase_id": "F01_CANONICAL_CLUSTER_BOOTSTRAP_TRANSLATION",
            "connection_databases": [BOOTSTRAP_DATABASE, TARGET_DATABASE],
            "session_user": BOOTSTRAP_ROLE,
            "required_current_roles": [BOOTSTRAP_ROLE, OWNER_ROLE],
            "source_paths": [CANONICAL_BOOTSTRAP_REFERENCE_PATH],
            "translation_complete": False,
        },
        {
            "phase_id": "F02_OWNER_ROLE_PREFLIGHT_AND_MIGRATIONS",
            "connection_databases": [TARGET_DATABASE],
            "session_user": BOOTSTRAP_ROLE,
            "required_current_roles": [OWNER_ROLE],
            "source_paths": [
                ROLES_PREFLIGHT_REFERENCE_PATH,
                "governed-memory-migrations/0001_foundation/forward.pgsql",
                "governed-memory-migrations/0003_owner_claim_detail/forward.pgsql",
                "governed-memory-migrations/0004_pilot_marker/forward.pgsql",
            ],
            "translation_complete": False,
        },
        {
            "phase_id": "T01_INDEPENDENT_TERMINAL_CATALOG",
            "connection_databases": [TARGET_DATABASE],
            "session_user": BOOTSTRAP_ROLE,
            "required_current_roles": [OWNER_ROLE],
            "source_paths": [],
            "translation_complete": False,
        },
        {
            "phase_id": "R01_EXACT_EMPTY_ROLLBACK_PREFIX_MACHINE",
            "connection_databases": [TARGET_DATABASE, BOOTSTRAP_DATABASE],
            "session_user": BOOTSTRAP_ROLE,
            "required_current_roles": [OWNER_ROLE, BOOTSTRAP_ROLE],
            "source_paths": [
                "governed-memory-migrations/0004_pilot_marker/rollback.pgsql",
                "governed-memory-migrations/0003_owner_claim_detail/rollback.pgsql",
                "governed-memory-migrations/0001_foundation/rollback.pgsql",
                CANONICAL_ROLLBACK_REFERENCE_PATH,
            ],
            "translation_complete": False,
        },
    )
    document: dict[str, object] = {
        "schema_version": "governed-memory-postgres-source-closure-contract-v1",
        "state": "repository-only-source-closed-native-execution-blocked",
        "endpoint": {
            "bind": POSTGRES_BIND,
            "bootstrap_database": BOOTSTRAP_DATABASE,
            "target_database": TARGET_DATABASE,
            "caller_selected_dsn_endpoint_database_or_path_allowed": False,
        },
        "server_version": POSTGRES_SERVER_VERSION,
        "driver": {
            "preferred_distribution": PREFERRED_DRIVER,
            "selection_state": DRIVER_SELECTION_STATE,
            "proved_fallback_distribution": PROVED_FALLBACK,
            "preferred_driver_locked": False,
            "preferred_wheels_staged": False,
            "preferred_native_closure_inspected": False,
        },
        "session_contract": {
            "advisory_lock_key": ADVISORY_LOCK_KEY,
            "advisory_lock_acquired_before_first_observation": True,
            "advisory_lock_held_through_terminal_or_rollback_receipt": True,
            "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
            "lock_timeout_milliseconds": LOCK_TIMEOUT_MILLISECONDS,
            "statement_timeout_milliseconds": STATEMENT_TIMEOUT_MILLISECONDS,
            "session_user_fixed": BOOTSTRAP_ROLE,
            "set_role_must_be_verified_per_phase": True,
            "session_and_role_observations_receipted": True,
        },
        "semantic_equivalence_requirements": {
            "canonical_bootstrap_all_predicates_translated_byte_for_byte": True,
            "auto_explain_privacy_predicate_required": True,
            "pgaudit_absence_predicate_required": True,
            "pgcrypto_exact_member_count_owner_and_dependency_closure_required": True,
            "roles_preflight_all_predicates_translated_byte_for_byte": True,
            "migration_transactions_explicit_and_source_hash_bound": True,
            "canonical_rollback_all_identity_acl_dependency_and_prefix_predicates_required": True,
            "rollback_prefix_machine_required": True,
            "fixed_terminal_catalog_queries_required": True,
            "catalog_normalization_algorithm_required": True,
            "approved_catalog_manifest_is_independent_package_authority": True,
        },
        "source_closure": [
            {"path": source.path, "sha256": source.sha256}
            for source in closure
        ],
        "native_phases": list(phases),
        "execution_gate": {
            "executable_stage_count": 0,
            "driver_native_translation_complete": False,
            "rollback_prefix_machine_complete": False,
            "fixed_catalog_queries_and_normalization_complete": False,
            "approved_terminal_catalog_manifest_selected": False,
            "live_execution_must_refuse": True,
            "caller_supplied_sql_stage_tuple_or_expected_catalog_allowed": False,
        },
        "repository_phase_effect_counts": {
            "postgresql_calls": 0,
            "provider_calls": 0,
            "production_reads": 0,
            "secret_reads_or_writes": 0,
        },
    }
    document["contract_sha256"] = _sha256(_canonical_bytes(document))
    return document


def construct_executable_transport() -> None:
    """The only executable-construction surface; always closed in Phase 9F."""

    raise PostgreSQLSourceClosureError(
        "postgres_source_closure_execution_not_ready"
    )


__all__ = [
    "ADVISORY_LOCK_KEY",
    "BOOTSTRAP_DATABASE",
    "BOOTSTRAP_ROLE",
    "CONNECT_TIMEOUT_SECONDS",
    "LOCK_TIMEOUT_MILLISECONDS",
    "OWNER_ROLE",
    "POSTGRES_BIND",
    "POSTGRES_SERVER_VERSION",
    "PREFERRED_DRIVER",
    "PostgreSQLSourceClosureError",
    "STATEMENT_TIMEOUT_MILLISECONDS",
    "TARGET_DATABASE",
    "VerifiedSqlSource",
    "construct_executable_transport",
    "contract_document",
]

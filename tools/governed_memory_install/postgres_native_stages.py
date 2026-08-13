from __future__ import annotations

"""Closed, Psycopg-shaped PostgreSQL stage machine.

The module fixes the PostgreSQL endpoint, roles, source identities, stage
order, catalog observations, and rollback prefixes.  A concrete adapter may
implement :class:`FixedSyncPostgreSQLPrimitive` with Psycopg 3 synchronous
connections, but this repository phase imports no driver and performs no I/O.

The primitive surface accepts only enums.  It has no caller-selected SQL,
DSN, host, port, database, role, source path, or expected catalog surface.
The current constructor is fail-closed until the selected runtime, both
Psycopg wheels, native-library closure, and independent catalog authority are
packaged and verified by a later phase.  The executable machine also exposes
the four controller-owned target prefixes (I11--I14), so a journaled install
step cannot silently advance through a later migration.
"""

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Final, Mapping, Protocol, TypeAlias


POSTGRES_HOST: Final = "127.0.0.1"
POSTGRES_PORT: Final = 55433
POSTGRES_SERVER_VERSION: Final = "16.14"
BOOTSTRAP_DATABASE: Final = "postgres"
TARGET_DATABASE: Final = "governed_memory"
BOOTSTRAP_ROLE: Final = "governed_memory_bootstrap"
OWNER_ROLE: Final = "governed_memory_owner"
ADVISORY_LOCK_KEY: Final = 9_054_123_493_000_002
CONNECT_TIMEOUT_SECONDS: Final = 5
LOCK_TIMEOUT_MILLISECONDS: Final = 1_000
STATEMENT_TIMEOUT_MILLISECONDS: Final = 15_000
PYTHON_VERSION: Final = "3.12.13"
PREFERRED_PSYCOPG_VERSION: Final = "3.3.4"
PREFERRED_LIBPQ_VERSION: Final = 180000
APPROVED_TERMINAL_CATALOG_SHA256: Final = (
    "c37620ecb2d1f9a771ea67ce4a71f1d15700f26dba01d4386d70e801a692e1cc"
)
INDEPENDENT_NATIVE_AUDIT_IDENTITY_SHA256: Final = (
    "bb6714cb1f3cead78935ae10f9e2ba630f4e9266c208395c6292ff0667f5f7ee"
)

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ASCII_VALUE_RE = re.compile(r"[\x20-\x7e]{0,4096}\Z", re.ASCII)
_MAX_CATALOG_ROWS: Final = 1_000_000


class PostgreSQLNativeStageError(RuntimeError):
    """Content-free refusal from the closed native-stage boundary."""


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
        raise PostgreSQLNativeStageError(
            "postgres_native_document_invalid"
        ) from None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_DRIVER_RUNTIME_NATIVE_FILES: Final = (
    (
        "psycopg_binary.libs/libcom_err-2abe824b.so.2.1",
        17_497,
        "5426dcb54dd01c9eedda05e2179f0e47114e8b48a534ffe9e94916e79471b257",
    ),
    (
        "psycopg_binary.libs/libcrypt-13f4f5d0.so.1",
        221_657,
        "a74d1e8438d224e0fff14da0001f5e50129b0d666a103bf981b976770158d106",
    ),
    (
        "psycopg_binary.libs/libcrypto-e7530dde.so.3",
        6_502_241,
        "3761d43a07bc3a119cfce159a0ae50b5fd526bd9db8bf9e87626487d12fc89cd",
    ),
    (
        "psycopg_binary.libs/libgssapi_krb5-497db0c6.so.2.2",
        345_209,
        "2a74b0330ee973281b26f8ebe4acef0ebf9ee99c6b68497cf9151fff6c4c34e1",
    ),
    (
        "psycopg_binary.libs/libk5crypto-b1f99d5c.so.3.1",
        219_953,
        "9844e5009e70a6ad2fb22b587306810fe2a7b1b9f6b9922daa1c78ba3466de27",
    ),
    (
        "psycopg_binary.libs/libkeyutils-dfe70bd6.so.1.5",
        17_913,
        "c29e41b03cf4b2dffbfb4960946e2b42f82c0ca5d53d231d3f0fc597ba274488",
    ),
    (
        "psycopg_binary.libs/libkrb5-fcafa220.so.3.3",
        1_018_953,
        "b2aab528ff4cab2144e5ce01b246ac09f574a072a53ff63ea81d6bb29b26b8f1",
    ),
    (
        "psycopg_binary.libs/libkrb5support-d0bcff84.so.0.1",
        76_873,
        "6a71f57d748fef79b4e736d5348875545d0a224fa8928b5d62a3cf2647fc109f",
    ),
    (
        "psycopg_binary.libs/liblber-9320a7df.so.2.0.200",
        60_977,
        "39df9a3a7d6fb2f3dec38d4892bae83ceb5f4c45fea50ce838f68f23cb0b7eaf",
    ),
    (
        "psycopg_binary.libs/libldap-fa0f4823.so.2.0.200",
        451_417,
        "09cd64a08d8ebb3cb3e3f2313f6d6e5ba737e7603c6bb8c3266cd4f4f69689cb",
    ),
    (
        "psycopg_binary.libs/libpcre-9513aab5.so.1.2.0",
        406_817,
        "02eda850e04931656d8af81f5171bff74d8bec1553d3d85c3d32d7fc5efe8864",
    ),
    (
        "psycopg_binary.libs/libpq-2be5f14a.so.5.18",
        416_049,
        "3918e961944c87346958a1fd8f6618f386af647c080bdda53cea03cb74327e88",
    ),
    (
        "psycopg_binary.libs/libsasl2-84219a89.so.3.0.0",
        134_753,
        "ae3f8967d5fa191dac7c6ae5f9130f659cf2f5cb66b2f7e5c5a1a5a47369fe5a",
    ),
    (
        "psycopg_binary.libs/libselinux-0922c95c.so.1",
        178_337,
        "d4fa8e7fb3add960a68325a59c9694244aea3213fd739a50815cb067c4965654",
    ),
    (
        "psycopg_binary.libs/libssl-4a840876.so.3",
        1_147_337,
        "461301beff504e1f2eb844dcb70de2d986de76cc89e7b6e15b3a68b97187e1ac",
    ),
    (
        "psycopg_binary/_psycopg.cpython-312-x86_64-linux-gnu.so",
        734_705,
        "cd784a19160d1dc7d7d355a702cd33db4d5978ed6553a708d687960eecf83d3a",
    ),
    (
        "psycopg_binary/pq.cpython-312-x86_64-linux-gnu.so",
        341_641,
        "21c93d0002a339c065961c853f168d8cbd68ab7146fe02e6fdd2819ab01337f3",
    ),
)


def runtime_driver_probe_document() -> dict[str, object]:
    """Return the exact selected-wheel runtime identity preimage."""

    return {
        "schema_version": "governed-memory-psycopg-runtime-probe-v1",
        "python_version": PYTHON_VERSION,
        "psycopg_version": PREFERRED_PSYCOPG_VERSION,
        "pq_impl": "binary",
        "libpq_version": PREFERRED_LIBPQ_VERSION,
        "native_files": [
            {"path": path, "size": size, "sha256": sha256}
            for path, size, sha256 in _DRIVER_RUNTIME_NATIVE_FILES
        ],
    }


EXPECTED_DRIVER_IDENTITY_SHA256: Final = _sha256(
    _canonical_bytes(runtime_driver_probe_document())
)


SOURCE_SHA256: Final[Mapping[str, str]] = MappingProxyType(
    {
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
        "ops/governed_memory/installation/current/postgres/roles_preflight.pgsql": (
            "21994b66b3d1612180ab458ea6f83258598e963138f604b88ada57ad4f23229e"
        ),
        "ops/governed_memory/installation/postgres/canonical_cluster.pgsql.in": (
            "5e3238856349ca9928d9a20d7658959effdaf6472a795d959ad0707fa9bbbae0"
        ),
        (
            "ops/governed_memory/installation/postgres/"
            "canonical_cluster_rollback.pgsql.in"
        ): (
            "5adec472f5b3277b5a2137fc5e7e8f1d50e587a54302246f6b87cef28442d277"
        ),
    }
)
SOURCE_CLOSURE_SHA256: Final = _sha256(
    _canonical_bytes(
        [
            {"path": path, "sha256": SOURCE_SHA256[path]}
            for path in sorted(SOURCE_SHA256)
        ]
    )
)


class StageId(str, Enum):
    F01 = "F01_PREBOOTSTRAP_AND_CREATE_DATABASE"
    F02 = "F02_ROLES_PRIVACY_AND_MIGRATIONS"
    T01 = "T01_TERMINAL_EXACT_CATALOG"
    R01 = "R01_EMPTY_ROLLBACK_PREFIX_RESUME"


class InstallTarget(str, Enum):
    I11 = "I11_BOOTSTRAP_CANONICAL_DATABASE"
    I12 = "I12_APPLY_FOUNDATION_0001"
    I13 = "I13_APPLY_OWNER_CLAIM_DETAIL_0003"
    I14 = "I14_APPLY_PILOT_MARKER_0004"


class ForwardPrefix(str, Enum):
    EMPTY = "empty"
    OWNER_ROLE = "owner_role"
    API_ROLE = "owner_api_roles"
    WORKER_ROLE = "owner_api_worker_roles"
    INGEST_ROLE = "owner_api_worker_ingest_roles"
    ALL_ROLES = "all_five_roles"
    DATABASE = "database_created"
    DATABASE_ACL = "database_acl_bound"
    OWNER_MEMBERSHIP = "owner_membership_granted"
    PGCRYPTO = "pgcrypto_created"
    F01_COMPLETE = "f01_complete"
    ROLES_PREFLIGHT = "roles_privacy_preflight_complete"
    FOUNDATION = "foundation_0001_applied"
    CLAIM_DETAIL = "owner_claim_detail_0003_applied"
    PILOT_MARKER = "pilot_marker_0004_applied"
    READY_FOR_CATALOG = "ready_for_terminal_catalog"


class ForwardOperation(str, Enum):
    F01_CREATE_OWNER_ROLE = "f01_create_owner_role"
    F01_CREATE_API_ROLE = "f01_create_api_role"
    F01_CREATE_WORKER_ROLE = "f01_create_worker_role"
    F01_CREATE_INGEST_ROLE = "f01_create_ingest_role"
    F01_CREATE_ERASURE_ROLE = "f01_create_erasure_role"
    F01_CREATE_DATABASE = "f01_create_database"
    F01_BIND_DATABASE_ACL = "f01_bind_database_acl"
    F01_GRANT_OWNER_MEMBERSHIP = "f01_grant_owner_membership"
    F01_CREATE_PGCRYPTO = "f01_create_pgcrypto_as_owner"
    F01_VERIFY = "f01_verify_exact_cluster_prefix"
    F02_ROLES_PRIVACY_PREFLIGHT = "f02_roles_privacy_preflight"
    F02_APPLY_FOUNDATION_0001 = "f02_apply_foundation_0001"
    F02_APPLY_OWNER_CLAIM_DETAIL_0003 = "f02_apply_owner_claim_detail_0003"
    F02_APPLY_PILOT_MARKER_0004 = "f02_apply_pilot_marker_0004"
    F02_VERIFY = "f02_verify_exact_migration_prefix"


FORWARD_TRANSITIONS: Final[
    Mapping[ForwardPrefix, tuple[ForwardOperation, ForwardPrefix]]
] = MappingProxyType(
    {
        ForwardPrefix.EMPTY: (
            ForwardOperation.F01_CREATE_OWNER_ROLE,
            ForwardPrefix.OWNER_ROLE,
        ),
        ForwardPrefix.OWNER_ROLE: (
            ForwardOperation.F01_CREATE_API_ROLE,
            ForwardPrefix.API_ROLE,
        ),
        ForwardPrefix.API_ROLE: (
            ForwardOperation.F01_CREATE_WORKER_ROLE,
            ForwardPrefix.WORKER_ROLE,
        ),
        ForwardPrefix.WORKER_ROLE: (
            ForwardOperation.F01_CREATE_INGEST_ROLE,
            ForwardPrefix.INGEST_ROLE,
        ),
        ForwardPrefix.INGEST_ROLE: (
            ForwardOperation.F01_CREATE_ERASURE_ROLE,
            ForwardPrefix.ALL_ROLES,
        ),
        ForwardPrefix.ALL_ROLES: (
            ForwardOperation.F01_CREATE_DATABASE,
            ForwardPrefix.DATABASE,
        ),
        ForwardPrefix.DATABASE: (
            ForwardOperation.F01_BIND_DATABASE_ACL,
            ForwardPrefix.DATABASE_ACL,
        ),
        ForwardPrefix.DATABASE_ACL: (
            ForwardOperation.F01_GRANT_OWNER_MEMBERSHIP,
            ForwardPrefix.OWNER_MEMBERSHIP,
        ),
        ForwardPrefix.OWNER_MEMBERSHIP: (
            ForwardOperation.F01_CREATE_PGCRYPTO,
            ForwardPrefix.PGCRYPTO,
        ),
        ForwardPrefix.PGCRYPTO: (
            ForwardOperation.F01_VERIFY,
            ForwardPrefix.F01_COMPLETE,
        ),
        ForwardPrefix.F01_COMPLETE: (
            ForwardOperation.F02_ROLES_PRIVACY_PREFLIGHT,
            ForwardPrefix.ROLES_PREFLIGHT,
        ),
        ForwardPrefix.ROLES_PREFLIGHT: (
            ForwardOperation.F02_APPLY_FOUNDATION_0001,
            ForwardPrefix.FOUNDATION,
        ),
        ForwardPrefix.FOUNDATION: (
            ForwardOperation.F02_APPLY_OWNER_CLAIM_DETAIL_0003,
            ForwardPrefix.CLAIM_DETAIL,
        ),
        ForwardPrefix.CLAIM_DETAIL: (
            ForwardOperation.F02_APPLY_PILOT_MARKER_0004,
            ForwardPrefix.PILOT_MARKER,
        ),
        ForwardPrefix.PILOT_MARKER: (
            ForwardOperation.F02_VERIFY,
            ForwardPrefix.READY_FOR_CATALOG,
        ),
    }
)


INSTALL_TARGET_PREFIXES: Final[Mapping[InstallTarget, ForwardPrefix]] = (
    MappingProxyType(
        {
            InstallTarget.I11: ForwardPrefix.ROLES_PREFLIGHT,
            InstallTarget.I12: ForwardPrefix.FOUNDATION,
            InstallTarget.I13: ForwardPrefix.CLAIM_DETAIL,
            InstallTarget.I14: ForwardPrefix.READY_FOR_CATALOG,
        }
    )
)


class RollbackPrefix(str, Enum):
    INSTALLED = "installed_0001_0003_0004"
    WITHOUT_0004 = "installed_0001_0003"
    WITHOUT_0003 = "installed_0001"
    DATABASE_PREFIX = "exact_empty_canonical_database_prefix"
    ROLES_ONLY_5 = "roles_only_creation_prefix_5"
    ROLES_ONLY_4 = "roles_only_creation_prefix_4"
    ROLES_ONLY_3 = "roles_only_creation_prefix_3"
    ROLES_ONLY_2 = "roles_only_creation_prefix_2"
    ROLES_ONLY_1 = "roles_only_creation_prefix_1"
    EMPTY = "empty"


class RollbackOperation(str, Enum):
    R01_ROLLBACK_PILOT_MARKER_0004 = "r01_rollback_pilot_marker_0004"
    R01_ROLLBACK_OWNER_CLAIM_DETAIL_0003 = (
        "r01_rollback_owner_claim_detail_0003"
    )
    R01_ROLLBACK_FOUNDATION_0001 = "r01_rollback_foundation_0001"
    R01_DROP_EXACT_DATABASE_PREFIX = "r01_drop_exact_database_prefix"
    R01_DROP_EXACT_ROLE_PREFIX_TRANSACTION = (
        "r01_drop_exact_role_prefix_transaction"
    )


class RollbackTarget(str, Enum):
    I14 = "I14_ROLLED_BACK_TO_0001_0003"
    I13 = "I13_ROLLED_BACK_TO_0001"
    I12 = "I12_ROLLED_BACK_TO_CANONICAL_DATABASE"
    I11 = "I11_ROLLED_BACK_TO_EMPTY_CLUSTER"


ROLLBACK_TRANSITIONS: Final[
    Mapping[RollbackPrefix, tuple[RollbackOperation, RollbackPrefix]]
] = MappingProxyType(
    {
        RollbackPrefix.INSTALLED: (
            RollbackOperation.R01_ROLLBACK_PILOT_MARKER_0004,
            RollbackPrefix.WITHOUT_0004,
        ),
        RollbackPrefix.WITHOUT_0004: (
            RollbackOperation.R01_ROLLBACK_OWNER_CLAIM_DETAIL_0003,
            RollbackPrefix.WITHOUT_0003,
        ),
        RollbackPrefix.WITHOUT_0003: (
            RollbackOperation.R01_ROLLBACK_FOUNDATION_0001,
            RollbackPrefix.DATABASE_PREFIX,
        ),
        RollbackPrefix.DATABASE_PREFIX: (
            RollbackOperation.R01_DROP_EXACT_DATABASE_PREFIX,
            RollbackPrefix.ROLES_ONLY_5,
        ),
        RollbackPrefix.ROLES_ONLY_5: (
            RollbackOperation.R01_DROP_EXACT_ROLE_PREFIX_TRANSACTION,
            RollbackPrefix.EMPTY,
        ),
        RollbackPrefix.ROLES_ONLY_4: (
            RollbackOperation.R01_DROP_EXACT_ROLE_PREFIX_TRANSACTION,
            RollbackPrefix.EMPTY,
        ),
        RollbackPrefix.ROLES_ONLY_3: (
            RollbackOperation.R01_DROP_EXACT_ROLE_PREFIX_TRANSACTION,
            RollbackPrefix.EMPTY,
        ),
        RollbackPrefix.ROLES_ONLY_2: (
            RollbackOperation.R01_DROP_EXACT_ROLE_PREFIX_TRANSACTION,
            RollbackPrefix.EMPTY,
        ),
        RollbackPrefix.ROLES_ONLY_1: (
            RollbackOperation.R01_DROP_EXACT_ROLE_PREFIX_TRANSACTION,
            RollbackPrefix.EMPTY,
        ),
    }
)


ROLLBACK_TARGET_PREFIXES: Final[Mapping[RollbackTarget, RollbackPrefix]] = (
    MappingProxyType(
        {
            RollbackTarget.I14: RollbackPrefix.WITHOUT_0004,
            RollbackTarget.I13: RollbackPrefix.WITHOUT_0003,
            RollbackTarget.I12: RollbackPrefix.DATABASE_PREFIX,
            RollbackTarget.I11: RollbackPrefix.EMPTY,
        }
    )
)


class CatalogQueryId(str, Enum):
    SESSION = "t01_session"
    ROLES = "t01_roles"
    MEMBERSHIPS = "t01_memberships"
    DATABASE = "t01_database"
    DATABASE_ROLE_SETTINGS = "t01_database_role_settings"
    EXTENSIONS = "t01_extensions"
    SCHEMAS = "t01_schemas"
    NAMESPACES_RELATIONS = "t01_namespaces_relations"
    COLUMNS = "t01_columns"
    SEQUENCES = "t01_sequences"
    INDEXES = "t01_indexes"
    ROUTINES = "t01_routines"
    CONSTRAINTS = "t01_constraints"
    POLICIES = "t01_policies"
    TRIGGERS = "t01_triggers"
    DEFAULT_ACLS = "t01_default_acls"
    PRIVACY_SETTINGS = "t01_privacy_settings"
    EXTERNAL_CLIENTS = "t01_external_clients"
    SEMANTIC_EMPTY = "t01_semantic_empty"


@dataclass(frozen=True, slots=True)
class FixedCatalogQuery:
    query_id: CatalogQueryId
    database: str
    role: str
    columns: tuple[str, ...]
    sql: str
    sql_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.query_id) is not CatalogQueryId
            or self.database not in {BOOTSTRAP_DATABASE, TARGET_DATABASE}
            or self.role not in {BOOTSTRAP_ROLE, OWNER_ROLE}
            or type(self.columns) is not tuple
            or not self.columns
            or len(set(self.columns)) != len(self.columns)
            or any(
                type(column) is not str
                or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", column) is None
                for column in self.columns
            )
            or type(self.sql) is not str
            or not self.sql.startswith("SELECT ")
            or "\x00" in self.sql
            or self.sql_sha256 != _sha256(self.sql.encode("ascii"))
        ):
            raise PostgreSQLNativeStageError("postgres_catalog_query_invalid")


def _query(
    query_id: CatalogQueryId,
    database: str,
    role: str,
    columns: tuple[str, ...],
    sql: str,
) -> FixedCatalogQuery:
    return FixedCatalogQuery(
        query_id,
        database,
        role,
        columns,
        sql,
        _sha256(sql.encode("ascii")),
    )


CATALOG_QUERIES: Final[Mapping[CatalogQueryId, FixedCatalogQuery]] = (
    MappingProxyType(
        {
            CatalogQueryId.SESSION: _query(
                CatalogQueryId.SESSION,
                TARGET_DATABASE,
                BOOTSTRAP_ROLE,
                (
                    "database",
                    "current_user",
                    "session_user",
                    "server_version_num",
                    "server_encoding",
                    "log_statement",
                    "log_duration",
                    "log_min_duration_statement",
                    "log_parameter_max_length",
                    "timezone",
                ),
                "SELECT current_database()::text, current_user::text, "
                "session_user::text, pg_catalog.current_setting("
                "'server_version_num')::integer, pg_catalog.current_setting("
                "'server_encoding')::text, pg_catalog.current_setting("
                "'log_statement')::text, pg_catalog.current_setting("
                "'log_duration')::text, pg_catalog.current_setting("
                "'log_min_duration_statement')::integer, "
                "pg_catalog.current_setting('log_parameter_max_length')::integer, "
                "pg_catalog.current_setting('TimeZone')::text",
            ),
            CatalogQueryId.ROLES: _query(
                CatalogQueryId.ROLES,
                BOOTSTRAP_DATABASE,
                BOOTSTRAP_ROLE,
                (
                    "role_name",
                    "can_login",
                    "inherit",
                    "superuser",
                    "create_database",
                    "create_role",
                    "replication",
                    "bypass_rls",
                    "connection_limit",
                    "valid_until",
                    "configuration_text",
                ),
                "SELECT rolname::text, rolcanlogin, rolinherit, rolsuper, "
                "rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, "
                "rolconnlimit::integer, COALESCE(rolvaliduntil::text,''), "
                "COALESCE(rolconfig::text,'') FROM pg_catalog.pg_roles WHERE rolname "
                "IN ('governed_memory_owner','governed_memory_api',"
                "'governed_memory_worker','memory_ingest_writer',"
                "'memory_erasure_requester') ORDER BY rolname COLLATE \"C\"",
            ),
            CatalogQueryId.MEMBERSHIPS: _query(
                CatalogQueryId.MEMBERSHIPS,
                BOOTSTRAP_DATABASE,
                BOOTSTRAP_ROLE,
                (
                    "granted_role",
                    "member_role",
                    "grantor_role",
                    "admin_option",
                    "inherit_option",
                    "set_option",
                ),
                "SELECT granted.rolname::text, member.rolname::text, "
                "grantor.rolname::text, membership.admin_option, "
                "membership.inherit_option, membership.set_option FROM "
                "pg_catalog.pg_auth_members AS membership JOIN "
                "pg_catalog.pg_roles AS granted ON granted.oid=membership.roleid "
                "JOIN pg_catalog.pg_roles AS member ON member.oid=membership.member "
                "JOIN pg_catalog.pg_roles AS grantor ON grantor.oid=membership.grantor "
                "WHERE granted.rolname LIKE 'governed_memory_%' OR "
                "member.rolname LIKE 'governed_memory_%' OR granted.rolname IN "
                "('memory_ingest_writer','memory_erasure_requester') OR "
                "member.rolname IN ('memory_ingest_writer',"
                "'memory_erasure_requester') ORDER BY granted.rolname COLLATE "
                "\"C\",member.rolname COLLATE \"C\",grantor.rolname COLLATE \"C\"",
            ),
            CatalogQueryId.DATABASE: _query(
                CatalogQueryId.DATABASE,
                BOOTSTRAP_DATABASE,
                BOOTSTRAP_ROLE,
                (
                    "database_name",
                    "owner",
                    "encoding",
                    "allow_connections",
                    "is_template",
                    "connection_limit",
                    "acl_text",
                ),
                "SELECT database.datname::text, pg_catalog.pg_get_userbyid("
                "database.datdba)::text, pg_catalog.pg_encoding_to_char("
                "database.encoding)::text, database.datallowconn, "
                "database.datistemplate, database.datconnlimit::integer, "
                "COALESCE(database.datacl::text,'') FROM pg_catalog.pg_database "
                "AS database WHERE database.datname='governed_memory' ORDER BY "
                "database.datname COLLATE \"C\"",
            ),
            CatalogQueryId.DATABASE_ROLE_SETTINGS: _query(
                CatalogQueryId.DATABASE_ROLE_SETTINGS,
                BOOTSTRAP_DATABASE,
                BOOTSTRAP_ROLE,
                (
                    "database_name",
                    "role_name",
                    "configuration_text",
                ),
                "SELECT COALESCE(database.datname::text,''), "
                "COALESCE(role.rolname::text,''), "
                "COALESCE(setting.setconfig::text,'') FROM "
                "pg_catalog.pg_db_role_setting AS setting LEFT JOIN "
                "pg_catalog.pg_database AS database ON database.oid="
                "NULLIF(setting.setdatabase,0::pg_catalog.oid) LEFT JOIN "
                "pg_catalog.pg_roles AS role ON role.oid="
                "NULLIF(setting.setrole,0::pg_catalog.oid) WHERE "
                "database.datname='governed_memory' OR role.rolname IN "
                "('governed_memory_owner','governed_memory_api',"
                "'governed_memory_worker','memory_ingest_writer',"
                "'memory_erasure_requester') ORDER BY COALESCE(database.datname,'') "
                "COLLATE \"C\",COALESCE(role.rolname,'') COLLATE \"C\"",
            ),
            CatalogQueryId.EXTENSIONS: _query(
                CatalogQueryId.EXTENSIONS,
                TARGET_DATABASE,
                OWNER_ROLE,
                (
                    "extension_name",
                    "extension_version",
                    "owner",
                    "schema_name",
                    "member_count",
                ),
                "SELECT extension.extname::text, extension.extversion::text, "
                "pg_catalog.pg_get_userbyid(extension.extowner)::text, "
                "namespace.nspname::text, pg_catalog.count(dependency.objid)::integer "
                "FROM pg_catalog.pg_extension AS extension JOIN "
                "pg_catalog.pg_namespace AS namespace ON namespace.oid="
                "extension.extnamespace LEFT JOIN pg_catalog.pg_depend AS dependency "
                "ON dependency.refclassid='pg_catalog.pg_extension'::"
                "pg_catalog.regclass "
                "AND dependency.refobjid=extension.oid AND dependency.deptype='e' "
                "WHERE extension.extname='pgcrypto' GROUP BY extension.extname,"
                "extension.extversion,extension.extowner,namespace.nspname ORDER BY "
                "extension.extname COLLATE \"C\"",
            ),
            CatalogQueryId.SCHEMAS: _query(
                CatalogQueryId.SCHEMAS,
                TARGET_DATABASE,
                OWNER_ROLE,
                (
                    "namespace",
                    "owner",
                    "acl_text",
                ),
                "SELECT namespace.nspname::text, "
                "pg_catalog.pg_get_userbyid(namespace.nspowner)::text, "
                "COALESCE(namespace.nspacl::text,'') FROM "
                "pg_catalog.pg_namespace AS namespace WHERE namespace.nspname "
                "IN ('public','memory','memory_private') ORDER BY namespace.nspname "
                "COLLATE \"C\"",
            ),
            CatalogQueryId.NAMESPACES_RELATIONS: _query(
                CatalogQueryId.NAMESPACES_RELATIONS,
                TARGET_DATABASE,
                OWNER_ROLE,
                (
                    "namespace",
                    "relation",
                    "relation_kind",
                    "owner",
                    "row_security",
                    "force_row_security",
                    "persistence",
                    "replica_identity",
                    "options_text",
                    "acl_text",
                ),
                "SELECT namespace.nspname::text, relation.relname::text, "
                "relation.relkind::text, pg_catalog.pg_get_userbyid("
                "relation.relowner)::text, relation.relrowsecurity, "
                "relation.relforcerowsecurity, relation.relpersistence::text, "
                "relation.relreplident::text, COALESCE(relation.reloptions::text,''), "
                "COALESCE(relation.relacl::text,'') FROM pg_catalog.pg_class AS "
                "relation JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid="
                "relation.relnamespace WHERE namespace.nspname IN ('memory',"
                "'memory_private') ORDER BY namespace.nspname COLLATE \"C\","
                "relation.relname COLLATE \"C\",relation.relkind",
            ),
            CatalogQueryId.COLUMNS: _query(
                CatalogQueryId.COLUMNS,
                TARGET_DATABASE,
                OWNER_ROLE,
                (
                    "namespace",
                    "relation",
                    "column_position",
                    "column_name",
                    "type_identity",
                    "not_null",
                    "has_default",
                    "default_expression",
                    "identity_kind",
                    "generated_kind",
                    "collation_identity",
                    "acl_text",
                ),
                "SELECT namespace.nspname::text, relation.relname::text, "
                "attribute.attnum::integer, attribute.attname::text, "
                "pg_catalog.format_type(attribute.atttypid,attribute.atttypmod)::text, "
                "attribute.attnotnull, (default_value.oid IS NOT NULL), "
                "COALESCE(pg_catalog.pg_get_expr(default_value.adbin,"
                "default_value.adrelid,true),''), attribute.attidentity::text, "
                "attribute.attgenerated::text, CASE WHEN collation_entry.oid IS NULL "
                "THEN '' ELSE collation_namespace.nspname::text||'.'||"
                "collation_entry.collname::text END, COALESCE(attribute.attacl::text,'') "
                "FROM pg_catalog.pg_attribute AS attribute JOIN "
                "pg_catalog.pg_class AS relation ON relation.oid=attribute.attrelid "
                "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid="
                "relation.relnamespace LEFT JOIN pg_catalog.pg_attrdef AS "
                "default_value ON default_value.adrelid=attribute.attrelid AND "
                "default_value.adnum=attribute.attnum LEFT JOIN "
                "pg_catalog.pg_collation AS collation_entry ON collation_entry.oid="
                "NULLIF(attribute.attcollation,0::pg_catalog.oid) LEFT JOIN "
                "pg_catalog.pg_namespace AS collation_namespace ON "
                "collation_namespace.oid=collation_entry.collnamespace WHERE "
                "attribute.attnum>0 AND NOT attribute.attisdropped AND "
                "namespace.nspname IN ('memory','memory_private') ORDER BY "
                "namespace.nspname COLLATE \"C\",relation.relname COLLATE \"C\","
                "attribute.attnum",
            ),
            CatalogQueryId.SEQUENCES: _query(
                CatalogQueryId.SEQUENCES,
                TARGET_DATABASE,
                OWNER_ROLE,
                (
                    "namespace",
                    "sequence_name",
                    "owner",
                    "data_type",
                    "start_value",
                    "increment_by",
                    "minimum_value",
                    "maximum_value",
                    "cache_size",
                    "cycle",
                ),
                "SELECT namespace.nspname::text, relation.relname::text, "
                "pg_catalog.pg_get_userbyid(relation.relowner)::text, "
                "pg_catalog.format_type(sequence_entry.seqtypid,NULL)::text, "
                "sequence_entry.seqstart, sequence_entry.seqincrement, "
                "sequence_entry.seqmin, sequence_entry.seqmax, "
                "sequence_entry.seqcache, sequence_entry.seqcycle FROM "
                "pg_catalog.pg_sequence AS sequence_entry JOIN "
                "pg_catalog.pg_class AS relation ON relation.oid="
                "sequence_entry.seqrelid JOIN pg_catalog.pg_namespace AS "
                "namespace ON namespace.oid=relation.relnamespace WHERE "
                "namespace.nspname IN ('memory','memory_private') ORDER BY "
                "namespace.nspname COLLATE \"C\",relation.relname COLLATE \"C\"",
            ),
            CatalogQueryId.INDEXES: _query(
                CatalogQueryId.INDEXES,
                TARGET_DATABASE,
                OWNER_ROLE,
                (
                    "namespace",
                    "relation",
                    "index_name",
                    "owner",
                    "unique",
                    "primary",
                    "exclusion",
                    "immediate",
                    "clustered",
                    "replica_identity",
                    "valid",
                    "ready",
                    "live",
                    "predicate",
                    "definition",
                ),
                "SELECT namespace.nspname::text, relation.relname::text, "
                "index_relation.relname::text, pg_catalog.pg_get_userbyid("
                "index_relation.relowner)::text, index_entry.indisunique, "
                "index_entry.indisprimary, index_entry.indisexclusion, "
                "index_entry.indimmediate, index_entry.indisclustered, "
                "index_entry.indisreplident, index_entry.indisvalid, "
                "index_entry.indisready, index_entry.indislive, COALESCE("
                "pg_catalog.pg_get_expr(index_entry.indpred,index_entry.indrelid,"
                "true),''), pg_catalog.pg_get_indexdef(index_entry.indexrelid,0,"
                "true)::text FROM pg_catalog.pg_index AS index_entry JOIN "
                "pg_catalog.pg_class AS index_relation ON index_relation.oid="
                "index_entry.indexrelid JOIN pg_catalog.pg_class AS relation ON "
                "relation.oid=index_entry.indrelid "
                "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid="
                "relation.relnamespace WHERE namespace.nspname IN ('memory',"
                "'memory_private') ORDER BY namespace.nspname COLLATE \"C\","
                "relation.relname COLLATE \"C\",index_relation.relname COLLATE \"C\"",
            ),
            CatalogQueryId.ROUTINES: _query(
                CatalogQueryId.ROUTINES,
                TARGET_DATABASE,
                OWNER_ROLE,
                (
                    "namespace",
                    "routine",
                    "identity_arguments",
                    "owner",
                    "language",
                    "volatility",
                    "security_definer",
                    "configuration_text",
                    "acl_text",
                    "definition_sha256",
                ),
                "SELECT namespace.nspname::text, procedure.proname::text, "
                "pg_catalog.pg_get_function_identity_arguments(procedure.oid)::text, "
                "pg_catalog.pg_get_userbyid(procedure.proowner)::text, "
                "language.lanname::text, procedure.provolatile::text, "
                "procedure.prosecdef, COALESCE(procedure.proconfig::text,''), "
                "COALESCE(procedure.proacl::text,''), pg_catalog.encode("
                "public.digest(pg_catalog.convert_to("
                "pg_catalog.pg_get_functiondef(procedure.oid),'UTF8'),'sha256'),"
                "'hex')::text "
                "FROM pg_catalog.pg_proc AS procedure JOIN pg_catalog.pg_namespace "
                "AS namespace ON namespace.oid=procedure.pronamespace JOIN "
                "pg_catalog.pg_language AS language ON language.oid=procedure.prolang "
                "WHERE namespace.nspname IN ('memory','memory_private') ORDER BY "
                "namespace.nspname COLLATE \"C\",procedure.proname COLLATE \"C\","
                "pg_catalog.pg_get_function_identity_arguments(procedure.oid) "
                "COLLATE \"C\"",
            ),
            CatalogQueryId.CONSTRAINTS: _query(
                CatalogQueryId.CONSTRAINTS,
                TARGET_DATABASE,
                OWNER_ROLE,
                (
                    "namespace",
                    "relation",
                    "constraint_name",
                    "constraint_type",
                    "validated",
                    "deferrable",
                    "initially_deferred",
                    "definition",
                ),
                "SELECT namespace.nspname::text, relation.relname::text, "
                "constraint_entry.conname::text, constraint_entry.contype::text, "
                "constraint_entry.convalidated, constraint_entry.condeferrable, "
                "constraint_entry.condeferred, pg_catalog.pg_get_constraintdef("
                "constraint_entry.oid,true)::text FROM pg_catalog.pg_constraint AS "
                "constraint_entry JOIN pg_catalog.pg_class AS relation ON "
                "relation.oid=constraint_entry.conrelid "
                "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid="
                "relation.relnamespace WHERE namespace.nspname IN ('memory',"
                "'memory_private') ORDER BY namespace.nspname COLLATE \"C\","
                "relation.relname COLLATE \"C\",constraint_entry.conname COLLATE "
                "\"C\"",
            ),
            CatalogQueryId.POLICIES: _query(
                CatalogQueryId.POLICIES,
                TARGET_DATABASE,
                OWNER_ROLE,
                (
                    "namespace",
                    "relation",
                    "policy_name",
                    "permissive",
                    "command",
                    "roles_text",
                    "using_expression",
                    "check_expression",
                ),
                "SELECT policy.schemaname::text, policy.tablename::text, "
                "policy.policyname::text, policy.permissive::text, "
                "policy.cmd::text, COALESCE((SELECT pg_catalog.string_agg("
                "role_item.role_name::text,',' ORDER BY "
                "role_item.role_name::text COLLATE \"C\") FROM "
                "pg_catalog.unnest(policy.roles) AS role_item(role_name)),''), "
                "COALESCE(policy.qual,''), COALESCE(policy.with_check,'') FROM "
                "pg_catalog.pg_policies AS policy WHERE policy.schemaname IN "
                "('memory','memory_private') ORDER BY policy.schemaname COLLATE "
                "\"C\",policy.tablename COLLATE \"C\",policy.policyname COLLATE \"C\"",
            ),
            CatalogQueryId.TRIGGERS: _query(
                CatalogQueryId.TRIGGERS,
                TARGET_DATABASE,
                OWNER_ROLE,
                (
                    "namespace",
                    "relation",
                    "trigger_name",
                    "enabled_state",
                    "definition",
                ),
                "SELECT namespace.nspname::text, relation.relname::text, "
                "trigger_entry.tgname::text, trigger_entry.tgenabled::text, "
                "pg_catalog.pg_get_triggerdef(trigger_entry.oid,true)::text FROM "
                "pg_catalog.pg_trigger AS trigger_entry JOIN pg_catalog.pg_class AS "
                "relation ON relation.oid=trigger_entry.tgrelid JOIN "
                "pg_catalog.pg_namespace AS "
                "namespace ON namespace.oid=relation.relnamespace WHERE NOT "
                "trigger_entry.tgisinternal AND namespace.nspname IN ('memory',"
                "'memory_private') ORDER BY namespace.nspname COLLATE \"C\","
                "relation.relname COLLATE \"C\",trigger_entry.tgname COLLATE \"C\"",
            ),
            CatalogQueryId.DEFAULT_ACLS: _query(
                CatalogQueryId.DEFAULT_ACLS,
                TARGET_DATABASE,
                OWNER_ROLE,
                (
                    "grantor_role",
                    "namespace",
                    "object_type",
                    "acl_text",
                ),
                "SELECT owner.rolname::text, COALESCE(namespace.nspname::text,''), "
                "default_acl.defaclobjtype::text, "
                "COALESCE(default_acl.defaclacl::text,'') FROM "
                "pg_catalog.pg_default_acl AS default_acl JOIN "
                "pg_catalog.pg_roles AS owner ON owner.oid=default_acl.defaclrole "
                "LEFT JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid="
                "NULLIF(default_acl.defaclnamespace,0::pg_catalog.oid) WHERE "
                "owner.rolname IN "
                "('governed_memory_owner','governed_memory_api',"
                "'governed_memory_worker','memory_ingest_writer',"
                "'memory_erasure_requester') OR namespace.nspname IN ('memory',"
                "'memory_private') ORDER BY owner.rolname COLLATE \"C\","
                "COALESCE(namespace.nspname,'') COLLATE \"C\","
                "default_acl.defaclobjtype",
            ),
            CatalogQueryId.PRIVACY_SETTINGS: _query(
                CatalogQueryId.PRIVACY_SETTINGS,
                BOOTSTRAP_DATABASE,
                BOOTSTRAP_ROLE,
                (
                    "log_statement",
                    "log_duration",
                    "log_min_duration_statement",
                    "log_min_duration_sample",
                    "log_transaction_sample_rate",
                    "log_parameter_max_length",
                    "log_parameter_max_length_on_error",
                    "auto_explain_log_parameter_max_length",
                    "pgaudit_preloaded",
                ),
                "SELECT pg_catalog.current_setting('log_statement')::text, "
                "pg_catalog.current_setting('log_duration')::text, "
                "pg_catalog.current_setting('log_min_duration_statement')::integer, "
                "pg_catalog.current_setting('log_min_duration_sample')::integer, "
                "pg_catalog.current_setting('log_transaction_sample_rate')::text, "
                "pg_catalog.current_setting('log_parameter_max_length')::integer, "
                "pg_catalog.current_setting('log_parameter_max_length_on_error')::integer, "
                "COALESCE(pg_catalog.current_setting("
                "'auto_explain.log_parameter_max_length',true),'0')::integer, "
                "EXISTS (SELECT 1 FROM pg_catalog.unnest(pg_catalog.string_to_array("
                "pg_catalog.current_setting('shared_preload_libraries'),',')) AS "
                "configured(library_name) WHERE pg_catalog.lower(pg_catalog.btrim("
                "configured.library_name))='pgaudit')",
            ),
            CatalogQueryId.EXTERNAL_CLIENTS: _query(
                CatalogQueryId.EXTERNAL_CLIENTS,
                BOOTSTRAP_DATABASE,
                BOOTSTRAP_ROLE,
                ("external_client_count", "source_endpoint_count"),
                "SELECT pg_catalog.count(*) FILTER (WHERE pid<>pg_catalog.pg_backend_pid() "
                "AND application_name<>'governed-memory-controller')::integer, "
                "pg_catalog.count(*) FILTER (WHERE datname<>'governed_memory')::integer "
                "FROM pg_catalog.pg_stat_activity WHERE backend_type='client backend' "
                "AND (datname='governed_memory' OR usename IN ("
                "'governed_memory_owner','governed_memory_api',"
                "'governed_memory_worker','memory_ingest_writer',"
                "'memory_erasure_requester'))",
            ),
            CatalogQueryId.SEMANTIC_EMPTY: _query(
                CatalogQueryId.SEMANTIC_EMPTY,
                TARGET_DATABASE,
                OWNER_ROLE,
                ("governed_user_row_count",),
                "SELECT ((SELECT pg_catalog.count(*) FROM memory.source_erasure_operation)+"
                "(SELECT pg_catalog.count(*) FROM memory.source_erasure_target)+"
                "(SELECT pg_catalog.count(*) FROM memory.erased_chat_message_tombstone)+"
                "(SELECT pg_catalog.count(*) FROM memory.source_erasure_claim)+"
                "(SELECT pg_catalog.count(*) FROM memory.source_erasure_receipt)+"
                "(SELECT pg_catalog.count(*) FROM memory.evidence)+"
                "(SELECT pg_catalog.count(*) FROM memory.extraction_job)+"
                "(SELECT pg_catalog.count(*) FROM memory.provider_call)+"
                "(SELECT pg_catalog.count(*) FROM memory.proposal)+"
                "(SELECT pg_catalog.count(*) FROM memory.entity)+"
                "(SELECT pg_catalog.count(*) FROM memory.claim)+"
                "(SELECT pg_catalog.count(*) FROM memory.claim_revision)+"
                "(SELECT pg_catalog.count(*) FROM memory.claim_evidence)+"
                "(SELECT pg_catalog.count(*) FROM memory.projection_outbox)+"
                "(SELECT pg_catalog.count(*) FROM memory.answer_binding)+"
                "(SELECT pg_catalog.count(*) FROM memory.audit_event)+"
                "(SELECT pg_catalog.count(*) FROM memory.claim_deletion_receipt)+"
                "(SELECT pg_catalog.count(*) FROM memory.pilot_marker))::bigint",
            ),
        }
    )
)


@dataclass(frozen=True, slots=True)
class NativeStageDescriptor:
    stage_id: StageId
    source_paths: tuple[str, ...]
    forward_operations: tuple[ForwardOperation, ...]
    rollback_operations: tuple[RollbackOperation, ...]
    catalog_queries: tuple[CatalogQueryId, ...]

    def __post_init__(self) -> None:
        if (
            type(self.stage_id) is not StageId
            or type(self.source_paths) is not tuple
            or any(path not in SOURCE_SHA256 for path in self.source_paths)
            or type(self.forward_operations) is not tuple
            or any(
                type(operation) is not ForwardOperation
                for operation in self.forward_operations
            )
            or type(self.rollback_operations) is not tuple
            or any(
                type(operation) is not RollbackOperation
                for operation in self.rollback_operations
            )
            or type(self.catalog_queries) is not tuple
            or any(
                type(query_id) is not CatalogQueryId
                for query_id in self.catalog_queries
            )
        ):
            raise PostgreSQLNativeStageError("postgres_native_stage_invalid")


NATIVE_STAGES: Final[Mapping[StageId, NativeStageDescriptor]] = MappingProxyType(
    {
        StageId.F01: NativeStageDescriptor(
            StageId.F01,
            (
                "ops/governed_memory/installation/postgres/"
                "canonical_cluster.pgsql.in",
            ),
            tuple(
                operation
                for operation in ForwardOperation
                if operation.value.startswith("f01_")
            ),
            (),
            (),
        ),
        StageId.F02: NativeStageDescriptor(
            StageId.F02,
            (
                "ops/governed_memory/installation/current/postgres/"
                "roles_preflight.pgsql",
                "governed-memory-migrations/0001_foundation/forward.pgsql",
                "governed-memory-migrations/0003_owner_claim_detail/forward.pgsql",
                "governed-memory-migrations/0004_pilot_marker/forward.pgsql",
            ),
            tuple(
                operation
                for operation in ForwardOperation
                if operation.value.startswith("f02_")
            ),
            (),
            (),
        ),
        StageId.T01: NativeStageDescriptor(
            StageId.T01,
            (),
            (),
            (),
            tuple(CatalogQueryId),
        ),
        StageId.R01: NativeStageDescriptor(
            StageId.R01,
            (
                "governed-memory-migrations/0004_pilot_marker/rollback.pgsql",
                "governed-memory-migrations/0003_owner_claim_detail/rollback.pgsql",
                "governed-memory-migrations/0001_foundation/rollback.pgsql",
                "ops/governed_memory/installation/postgres/"
                "canonical_cluster_rollback.pgsql.in",
            ),
            (),
            tuple(RollbackOperation),
            (),
        ),
    }
)


CatalogScalar: TypeAlias = str | int | bool | None
CatalogRows: TypeAlias = tuple[tuple[CatalogScalar, ...], ...]


def normalize_catalog_rows(
    rows: Mapping[CatalogQueryId, CatalogRows],
) -> tuple[bytes, str]:
    """Normalize fixed query results without accepting expected values."""

    if not isinstance(rows, Mapping) or set(rows) != set(CATALOG_QUERIES):
        raise PostgreSQLNativeStageError("postgres_catalog_result_set_invalid")
    normalized_queries: list[dict[str, object]] = []
    total_rows = 0
    for query_id in CatalogQueryId:
        query = CATALOG_QUERIES[query_id]
        observed = rows[query_id]
        if type(observed) is not tuple:
            raise PostgreSQLNativeStageError("postgres_catalog_rows_invalid")
        total_rows += len(observed)
        if total_rows > _MAX_CATALOG_ROWS:
            raise PostgreSQLNativeStageError("postgres_catalog_rows_invalid")
        normalized_rows: list[list[CatalogScalar]] = []
        seen: set[bytes] = set()
        for row in observed:
            if type(row) is not tuple or len(row) != len(query.columns):
                raise PostgreSQLNativeStageError("postgres_catalog_row_invalid")
            normalized_row: list[CatalogScalar] = []
            for value in row:
                if value is None or type(value) is bool:
                    normalized_row.append(value)
                elif type(value) is int:
                    if not -(2**63) <= value < 2**63:
                        raise PostgreSQLNativeStageError(
                            "postgres_catalog_scalar_invalid"
                        )
                    normalized_row.append(value)
                elif type(value) is str and _ASCII_VALUE_RE.fullmatch(value):
                    normalized_row.append(value)
                else:
                    raise PostgreSQLNativeStageError(
                        "postgres_catalog_scalar_invalid"
                    )
            encoded = _canonical_bytes(normalized_row)
            if encoded in seen:
                raise PostgreSQLNativeStageError("postgres_catalog_duplicate_row")
            seen.add(encoded)
            normalized_rows.append(normalized_row)
        normalized_rows.sort(key=_canonical_bytes)
        normalized_queries.append(
            {
                "query_id": query_id.value,
                "columns": list(query.columns),
                "rows": normalized_rows,
                "sql_sha256": query.sql_sha256,
            }
        )
    encoded_catalog = _canonical_bytes(
        {
            "schema_version": "governed-memory-postgres-terminal-catalog-v1",
            "queries": normalized_queries,
        }
    )
    return encoded_catalog, _sha256(encoded_catalog)


@dataclass(frozen=True, slots=True)
class EndpointObservation:
    host: str
    port: int
    bootstrap_database: str
    target_database: str
    session_user: str


@dataclass(frozen=True, slots=True)
class DriverRuntimeObservation:
    python_version: str
    psycopg_version: str
    api_style: str
    pq_impl: str
    libpq_version: int
    binary_native_library_closure_inspected: bool
    native_closure_receipt_sha256: str
    runtime_receipt_sha256: str
    driver_identity_sha256: str

    def __post_init__(self) -> None:
        if (
            self.python_version != PYTHON_VERSION
            or self.psycopg_version != PREFERRED_PSYCOPG_VERSION
            or self.api_style != "synchronous"
            or self.pq_impl != "binary"
            or type(self.libpq_version) is not int
            or self.libpq_version != PREFERRED_LIBPQ_VERSION
            or self.binary_native_library_closure_inspected is not True
            or _HASH_RE.fullmatch(self.native_closure_receipt_sha256) is None
            or _HASH_RE.fullmatch(self.runtime_receipt_sha256) is None
            or _HASH_RE.fullmatch(self.driver_identity_sha256) is None
        ):
            raise PostgreSQLNativeStageError(
                "postgres_driver_runtime_observation_invalid"
            )


@dataclass(frozen=True, slots=True)
class PrivacySettingsObservation:
    log_statement: str
    log_duration: str
    log_min_duration_statement: int
    log_min_duration_sample: int
    log_transaction_sample_rate: str
    log_parameter_max_length: int
    log_parameter_max_length_on_error: int
    auto_explain_log_parameter_max_length: int
    pgaudit_preloaded: bool

    def __post_init__(self) -> None:
        if (
            self.log_statement,
            self.log_duration,
            self.log_min_duration_statement,
            self.log_min_duration_sample,
            self.log_transaction_sample_rate,
            self.log_parameter_max_length,
            self.log_parameter_max_length_on_error,
            self.auto_explain_log_parameter_max_length,
            self.pgaudit_preloaded,
        ) != ("none", "off", -1, -1, "0", 0, 0, 0, False):
            raise PostgreSQLNativeStageError("postgres_privacy_settings_invalid")


@dataclass(frozen=True, slots=True)
class ExternalClientObservation:
    external_client_count: int
    source_endpoint_count: int

    def __post_init__(self) -> None:
        if (
            type(self.external_client_count) is not int
            or self.external_client_count != 0
            or type(self.source_endpoint_count) is not int
            or self.source_endpoint_count != 0
        ):
            raise PostgreSQLNativeStageError("postgres_external_client_observation_invalid")


@dataclass(frozen=True, slots=True)
class RollbackObservation:
    prefix: RollbackPrefix
    empty_proof_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.prefix) is not RollbackPrefix
            or type(self.empty_proof_sha256) is not str
            or _HASH_RE.fullmatch(self.empty_proof_sha256) is None
        ):
            raise PostgreSQLNativeStageError(
                "postgres_rollback_observation_invalid"
            )


@dataclass(frozen=True, slots=True)
class NativeStageReceipt:
    mode: str
    final_state: str
    source_closure_sha256: str
    runtime_receipt_sha256: str
    driver_runtime_identity_sha256: str
    terminal_catalog_sha256: str | None
    rollback_empty_proof_sha256: str | None
    operations: tuple[str, ...]
    receipt_sha256: str


class FixedSyncPostgreSQLPrimitive(Protocol):
    """Closed primitive intended for one exact synchronous Psycopg adapter."""

    def endpoint_identity(self) -> EndpointObservation: ...

    def driver_runtime_identity(self) -> DriverRuntimeObservation: ...

    def observe_privacy_settings(self) -> PrivacySettingsObservation: ...

    def observe_external_clients(self) -> ExternalClientObservation: ...

    def acquire_fixed_session_lock(self) -> None: ...

    def fixed_session_lock_held(self) -> bool: ...

    def release_fixed_session_lock(self) -> None: ...

    def observe_forward_prefix(self) -> ForwardPrefix: ...

    def apply_forward_transition(
        self, operation: ForwardOperation
    ) -> ForwardPrefix: ...

    def terminal_catalog_rows(self, query_id: CatalogQueryId) -> CatalogRows: ...

    def controller_authority_marker_held(self) -> bool: ...

    def observe_rollback_prefix(self) -> RollbackObservation: ...

    def verify_semantic_empty(self) -> str: ...

    def apply_rollback_transition(
        self, operation: RollbackOperation
    ) -> RollbackObservation: ...

    def persist_stage_receipt(self, receipt: NativeStageReceipt) -> None: ...


_AUTHORITY_TOKEN = object()


@dataclass(frozen=True, slots=True)
class _ExecutionAuthority:
    token: object
    mode: str
    runtime_receipt_sha256: str
    approved_driver_runtime_identity_sha256: str
    approved_terminal_catalog_sha256: str | None
    rollback_empty_proof_sha256: str | None

    def __post_init__(self) -> None:
        if (
            self.token is not _AUTHORITY_TOKEN
            or self.mode not in {"install", "rollback"}
            or _HASH_RE.fullmatch(self.runtime_receipt_sha256) is None
            or _HASH_RE.fullmatch(
                self.approved_driver_runtime_identity_sha256
            )
            is None
            or (
                self.mode == "install"
                and (
                    type(self.approved_terminal_catalog_sha256) is not str
                    or _HASH_RE.fullmatch(
                        self.approved_terminal_catalog_sha256
                    )
                    is None
                    or self.rollback_empty_proof_sha256 is not None
                )
            )
            or (
                self.mode == "rollback"
                and (
                    type(self.rollback_empty_proof_sha256) is not str
                    or _HASH_RE.fullmatch(self.rollback_empty_proof_sha256)
                    is None
                    or self.approved_terminal_catalog_sha256 is not None
                )
            )
        ):
            raise PostgreSQLNativeStageError(
                "postgres_native_execution_authority_invalid"
            )


def _mint_synthetic_authority(
    *,
    mode: str,
    runtime_receipt_sha256: str,
    approved_driver_runtime_identity_sha256: str,
    approved_terminal_catalog_sha256: str | None = None,
    rollback_empty_proof_sha256: str | None = None,
) -> _ExecutionAuthority:
    """Private in-process proof seam; never a production authority source."""

    return _ExecutionAuthority(
        _AUTHORITY_TOKEN,
        mode,
        runtime_receipt_sha256,
        approved_driver_runtime_identity_sha256,
        approved_terminal_catalog_sha256,
        rollback_empty_proof_sha256,
    )


def _receipt(
    *,
    mode: str,
    final_state: str,
    runtime_receipt_sha256: str,
    driver_runtime_identity_sha256: str,
    terminal_catalog_sha256: str | None,
    rollback_empty_proof_sha256: str | None,
    operations: tuple[str, ...],
) -> NativeStageReceipt:
    document = {
        "schema_version": "governed-memory-postgres-native-stage-receipt-v1",
        "mode": mode,
        "final_state": final_state,
        "source_closure_sha256": SOURCE_CLOSURE_SHA256,
        "runtime_receipt_sha256": runtime_receipt_sha256,
        "driver_runtime_identity_sha256": driver_runtime_identity_sha256,
        "terminal_catalog_sha256": terminal_catalog_sha256,
        "rollback_empty_proof_sha256": rollback_empty_proof_sha256,
        "operations": list(operations),
    }
    return NativeStageReceipt(
        mode,
        final_state,
        SOURCE_CLOSURE_SHA256,
        runtime_receipt_sha256,
        driver_runtime_identity_sha256,
        terminal_catalog_sha256,
        rollback_empty_proof_sha256,
        operations,
        _sha256(_canonical_bytes(document)),
    )


class ClosedPostgreSQLStageMachine:
    """Execute only the fixed transition graph through a closed primitive."""

    __slots__ = ("_primitive", "_authority")

    def __init__(
        self,
        primitive: FixedSyncPostgreSQLPrimitive,
        authority: _ExecutionAuthority,
    ) -> None:
        if type(authority) is not _ExecutionAuthority:
            raise PostgreSQLNativeStageError(
                "postgres_native_execution_authority_invalid"
            )
        authority.__post_init__()
        self._primitive = primitive
        self._authority = authority

    def _verify_boundary(self) -> DriverRuntimeObservation:
        self._require_lock()
        endpoint = self._primitive.endpoint_identity()
        if endpoint != EndpointObservation(
            POSTGRES_HOST,
            POSTGRES_PORT,
            BOOTSTRAP_DATABASE,
            TARGET_DATABASE,
            BOOTSTRAP_ROLE,
        ):
            raise PostgreSQLNativeStageError("postgres_endpoint_identity_drift")
        self._require_lock()
        driver = self._primitive.driver_runtime_identity()
        if (
            type(driver) is not DriverRuntimeObservation
            or driver.runtime_receipt_sha256
            != self._authority.runtime_receipt_sha256
            or driver.driver_identity_sha256
            != self._authority.approved_driver_runtime_identity_sha256
        ):
            raise PostgreSQLNativeStageError("postgres_driver_identity_drift")
        driver.__post_init__()
        self._require_lock()
        privacy = self._primitive.observe_privacy_settings()
        if type(privacy) is not PrivacySettingsObservation:
            raise PostgreSQLNativeStageError("postgres_privacy_settings_invalid")
        privacy.__post_init__()
        self._require_no_external_clients()
        return driver

    def _require_no_external_clients(self) -> None:
        self._require_lock()
        observation = self._primitive.observe_external_clients()
        if type(observation) is not ExternalClientObservation:
            raise PostgreSQLNativeStageError(
                "postgres_external_client_observation_invalid"
            )
        observation.__post_init__()

    def _require_lock(self) -> None:
        if self._primitive.fixed_session_lock_held() is not True:
            raise PostgreSQLNativeStageError("postgres_session_lock_not_held")

    def _require_controller_authority_marker(self) -> None:
        if self._primitive.controller_authority_marker_held() is not True:
            raise PostgreSQLNativeStageError(
                "postgres_controller_authority_marker_not_held"
            )

    def advance_install(self, target: InstallTarget) -> NativeStageReceipt:
        if self._authority.mode != "install":
            raise PostgreSQLNativeStageError("postgres_install_authority_absent")
        if type(target) is not InstallTarget:
            raise PostgreSQLNativeStageError("postgres_install_target_invalid")
        target_prefix = INSTALL_TARGET_PREFIXES[target]
        operations: list[str] = []
        self._primitive.acquire_fixed_session_lock()
        try:
            self._require_lock()
            driver = self._verify_boundary()
            self._require_lock()
            prefix = self._primitive.observe_forward_prefix()
            if type(prefix) is not ForwardPrefix:
                raise PostgreSQLNativeStageError(
                    "postgres_forward_prefix_invalid"
                )
            while prefix is not target_prefix:
                transition = FORWARD_TRANSITIONS.get(prefix)
                if transition is None:
                    raise PostgreSQLNativeStageError(
                        "postgres_forward_prefix_not_before_target"
                    )
                operation, expected = transition
                self._require_lock()
                self._require_no_external_clients()
                observed = self._primitive.apply_forward_transition(operation)
                if observed is not expected:
                    raise PostgreSQLNativeStageError(
                        "postgres_forward_transition_drift"
                    )
                operations.append(operation.value)
                prefix = observed
            catalog_sha256: str | None = None
            if target is InstallTarget.I14:
                rows: dict[CatalogQueryId, CatalogRows] = {}
                for query_id in CatalogQueryId:
                    self._require_lock()
                    self._require_no_external_clients()
                    rows[query_id] = self._primitive.terminal_catalog_rows(query_id)
                self._require_lock()
                _encoded, catalog_sha256 = normalize_catalog_rows(rows)
                if (
                    catalog_sha256
                    != self._authority.approved_terminal_catalog_sha256
                ):
                    raise PostgreSQLNativeStageError(
                        "postgres_terminal_catalog_not_approved"
                    )
                operations.append(StageId.T01.value)
            receipt = _receipt(
                mode="install",
                final_state=target_prefix.value,
                runtime_receipt_sha256=self._authority.runtime_receipt_sha256,
                driver_runtime_identity_sha256=driver.driver_identity_sha256,
                terminal_catalog_sha256=catalog_sha256,
                rollback_empty_proof_sha256=None,
                operations=tuple(operations),
            )
            self._require_lock()
            self._primitive.persist_stage_receipt(receipt)
            self._require_lock()
            return receipt
        finally:
            self._primitive.release_fixed_session_lock()

    def run_install(self) -> NativeStageReceipt:
        """Monolithic compatibility wrapper over the exact I14 target."""

        return self.advance_install(InstallTarget.I14)

    def _advance_rollback_to(
        self,
        target_prefix: RollbackPrefix,
        allowed_starts: frozenset[RollbackPrefix] | None = None,
    ) -> NativeStageReceipt:
        if self._authority.mode != "rollback":
            raise PostgreSQLNativeStageError("postgres_rollback_authority_absent")
        self._require_controller_authority_marker()
        operations: list[str] = []
        self._primitive.acquire_fixed_session_lock()
        try:
            self._require_lock()
            driver = self._verify_boundary()
            self._require_lock()
            self._require_controller_authority_marker()
            observation = self._primitive.observe_rollback_prefix()
            if (
                type(observation) is not RollbackObservation
                or observation.empty_proof_sha256
                != self._authority.rollback_empty_proof_sha256
            ):
                raise PostgreSQLNativeStageError(
                    "postgres_rollback_prefix_or_empty_proof_invalid"
                )
            if (
                allowed_starts is not None
                and observation.prefix not in allowed_starts
            ):
                raise PostgreSQLNativeStageError(
                    "postgres_rollback_prefix_not_before_target"
                )
            self._require_no_external_clients()
            if observation.prefix in {
                RollbackPrefix.INSTALLED,
                RollbackPrefix.WITHOUT_0004,
                RollbackPrefix.WITHOUT_0003,
                RollbackPrefix.DATABASE_PREFIX,
            }:
                self._require_lock()
                self._require_controller_authority_marker()
                empty_proof = self._primitive.verify_semantic_empty()
                if empty_proof != self._authority.rollback_empty_proof_sha256:
                    raise PostgreSQLNativeStageError(
                        "postgres_semantic_empty_proof_invalid"
                    )
            while observation.prefix is not target_prefix:
                transition = ROLLBACK_TRANSITIONS.get(observation.prefix)
                if transition is None:
                    raise PostgreSQLNativeStageError(
                        "postgres_rollback_prefix_not_before_target"
                    )
                operation, expected = transition
                self._require_lock()
                self._require_controller_authority_marker()
                self._require_no_external_clients()
                observed = self._primitive.apply_rollback_transition(operation)
                if (
                    type(observed) is not RollbackObservation
                    or observed.prefix is not expected
                    or observed.empty_proof_sha256
                    != self._authority.rollback_empty_proof_sha256
                ):
                    raise PostgreSQLNativeStageError(
                        "postgres_rollback_transition_drift"
                    )
                operations.append(operation.value)
                observation = observed
            receipt = _receipt(
                mode="rollback",
                final_state=target_prefix.value,
                runtime_receipt_sha256=self._authority.runtime_receipt_sha256,
                driver_runtime_identity_sha256=driver.driver_identity_sha256,
                terminal_catalog_sha256=None,
                rollback_empty_proof_sha256=(
                    self._authority.rollback_empty_proof_sha256
                ),
                operations=tuple(operations),
            )
            self._require_lock()
            self._require_controller_authority_marker()
            self._primitive.persist_stage_receipt(receipt)
            self._require_lock()
            self._require_controller_authority_marker()
            return receipt
        finally:
            self._primitive.release_fixed_session_lock()

    def advance_rollback(self, target: RollbackTarget) -> NativeStageReceipt:
        """Advance exactly one controller rollback target and no later target."""

        if type(target) is not RollbackTarget:
            raise PostgreSQLNativeStageError("postgres_rollback_target_invalid")
        allowed_starts = frozenset({
            RollbackTarget.I14: {
                RollbackPrefix.INSTALLED,
                RollbackPrefix.WITHOUT_0004,
            },
            RollbackTarget.I13: {
                RollbackPrefix.WITHOUT_0004,
                RollbackPrefix.WITHOUT_0003,
            },
            RollbackTarget.I12: {
                RollbackPrefix.WITHOUT_0003,
                RollbackPrefix.DATABASE_PREFIX,
            },
            RollbackTarget.I11: {
                RollbackPrefix.DATABASE_PREFIX,
                RollbackPrefix.ROLES_ONLY_5,
                RollbackPrefix.ROLES_ONLY_4,
                RollbackPrefix.ROLES_ONLY_3,
                RollbackPrefix.ROLES_ONLY_2,
                RollbackPrefix.ROLES_ONLY_1,
                RollbackPrefix.EMPTY,
            },
        }[target])
        # Observe under the execution lock inside _advance_rollback_to.  The
        # primitive separately enforces this allowlist before mutation; this
        # mapping is exported in the contract and tested as the controller
        # target boundary.
        target_prefix = ROLLBACK_TARGET_PREFIXES[target]
        receipt = self._advance_rollback_to(target_prefix, allowed_starts)
        if receipt.final_state != target_prefix.value:
            raise PostgreSQLNativeStageError("postgres_rollback_target_drift")
        return receipt

    def run_rollback(self) -> NativeStageReceipt:
        """Monolithic compatibility wrapper over the exact empty target."""

        return self._advance_rollback_to(RollbackPrefix.EMPTY)


def construct_current_machine(
    primitive: FixedSyncPostgreSQLPrimitive,
) -> ClosedPostgreSQLStageMachine:
    """Fail closed before touching the primitive while inputs are unready."""

    del primitive
    raise PostgreSQLNativeStageError(
        "postgres_native_runtime_driver_catalog_not_ready"
    )


def contract_document() -> dict[str, object]:
    stages: list[dict[str, object]] = []
    for stage_id in StageId:
        stage = NATIVE_STAGES[stage_id]
        translation_projection = {
            "stage_id": stage_id.value,
            "sources": [
                {"path": path, "sha256": SOURCE_SHA256[path]}
                for path in stage.source_paths
            ],
            "forward_operations": [value.value for value in stage.forward_operations],
            "rollback_operations": [
                value.value for value in stage.rollback_operations
            ],
            "catalog_query_sha256": [
                CATALOG_QUERIES[value].sql_sha256
                for value in stage.catalog_queries
            ],
        }
        stages.append(
            {
                **translation_projection,
                "orchestration_plan_sha256": _sha256(
                    _canonical_bytes(translation_projection)
                ),
            }
        )
    document: dict[str, object] = {
        "schema_version": "governed-memory-postgres-native-stage-contract-v4",
        "state": (
            "source-closed-concrete-psycopg-transport-exact-prefix-machine-"
            "runtime-bound-terminal-catalog-disposable-selected-inactive"
        ),
        "endpoint": {
            "host": POSTGRES_HOST,
            "port": POSTGRES_PORT,
            "bootstrap_database": BOOTSTRAP_DATABASE,
            "target_database": TARGET_DATABASE,
            "bootstrap_role": BOOTSTRAP_ROLE,
            "owner_role": OWNER_ROLE,
            "caller_dsn_host_port_database_role_or_path_allowed": False,
        },
        "server_version": POSTGRES_SERVER_VERSION,
        "session": {
            "advisory_lock_key": ADVISORY_LOCK_KEY,
            "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
            "lock_timeout_milliseconds": LOCK_TIMEOUT_MILLISECONDS,
            "statement_timeout_milliseconds": STATEMENT_TIMEOUT_MILLISECONDS,
            (
                "session_lock_precedes_postgresql_endpoint_boundary_prefix_"
                "and_catalog_observations"
            ): True,
            "session_lock_held_through_receipt_persistence": True,
            "migration_transactions_required_by_adapter_contract": True,
            "migration_transactions_implemented_by_concrete_adapter": True,
            "set_role_owner_and_current_user_verification_required": True,
            "set_role_owner_and_current_user_verification_implemented": True,
            "privacy_settings_verified_before_mutation": True,
            "external_client_count_zero_verified_before_mutation": True,
        },
        "preferred_driver": {
            "selection_state": "exact-runtime-probed-capability-bound",
            "python_version_target": PYTHON_VERSION,
            "api_style": "synchronous",
            "preferred_package": "psycopg",
            "preferred_extra": "binary",
            "preferred_version": PREFERRED_PSYCOPG_VERSION,
            "required_distributions": ["psycopg", "psycopg-binary"],
            "selected_wheels": [
                "psycopg-3.3.4-py3-none-any.whl",
                (
                    "psycopg_binary-3.3.4-cp312-cp312-"
                    "manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
                ),
            ],
            "preference_contract_packaged": True,
            "exact_driver_identity_contract_packaged": True,
            "runtime_driver_identity_sha256": EXPECTED_DRIVER_IDENTITY_SHA256,
            "runtime_driver_probe": runtime_driver_probe_document(),
            "independent_native_audit_identity_sha256": (
                INDEPENDENT_NATIVE_AUDIT_IDENTITY_SHA256
            ),
            "pq_impl": "binary",
            "libpq_version": PREFERRED_LIBPQ_VERSION,
            "native_file_count": len(_DRIVER_RUNTIME_NATIVE_FILES),
            "exact_wheel_filenames_frozen": True,
            "wheel_bytes_staged": True,
            "wheel_bytes_verified": True,
            "binary_native_library_closure_inspected": True,
            "runtime_receipt_selected": True,
            "ready": True,
        },
        "source_closure": [
            {"path": path, "sha256": SOURCE_SHA256[path]}
            for path in sorted(SOURCE_SHA256)
        ],
        "source_closure_sha256": SOURCE_CLOSURE_SHA256,
        "stages": stages,
        "forward_resume_machine": [
            {
                "from": prefix.value,
                "operation": transition[0].value,
                "to": transition[1].value,
            }
            for prefix, transition in FORWARD_TRANSITIONS.items()
        ],
        "rollback_resume_machine": [
            {
                "from": prefix.value,
                "operation": transition[0].value,
                "to": transition[1].value,
            }
            for prefix, transition in ROLLBACK_TRANSITIONS.items()
        ],
        "controller_install_targets": [
            {"step": target.value, "target_prefix": prefix.value}
            for target, prefix in INSTALL_TARGET_PREFIXES.items()
        ],
        "controller_rollback_targets": [
            {"step": target.value, "target_prefix": prefix.value}
            for target, prefix in ROLLBACK_TARGET_PREFIXES.items()
        ],
        "terminal_catalog": {
            "queries": [
                {
                    "query_id": query_id.value,
                    "database": CATALOG_QUERIES[query_id].database,
                    "role": CATALOG_QUERIES[query_id].role,
                    "columns": list(CATALOG_QUERIES[query_id].columns),
                    "sql_sha256": CATALOG_QUERIES[query_id].sql_sha256,
                }
                for query_id in CatalogQueryId
            ],
            "normalizer": (
                "ascii-scalars-strict-width-deduplicate-sort-rows-by-"
                "canonical-json-sort-queries-by-fixed-enum-canonical-json-v1"
            ),
            "security_sensitive_coverage": {
                "target_and_public_schema_owner_and_acl": True,
                "database_and_role_configuration": True,
                "relation_owner_acl_and_row_security": True,
                "column_type_default_identity_generation_collation_and_acl": True,
                "sequence_definition": True,
                "index_definition_predicate_and_state": True,
                "constraint_definition_and_validation_state": True,
                "policy_permissive_command_roles_using_and_check": True,
                "trigger_enabled_state_and_definition": True,
                "default_acl_grantor_namespace_object_type_and_acl": True,
                "privacy_settings_and_pgaudit_absence": True,
                "external_client_and_source_endpoint_count": True,
                "semantic_empty_governed_row_count": True,
            },
            "caller_expected_catalog_allowed": False,
            "postgresql_parse_or_execution_proven": True,
            "approved_manifest_selected": True,
            "approved_normalized_catalog_sha256": (
                APPROVED_TERMINAL_CATALOG_SHA256
            ),
        },
        "rollback_controller_marker": {
            "controller_authority_marker_required_before_first_observation": True,
            "semantic_empty_proof_required_before_first_destructive_operation": True,
            "empty_proof_bound_across_database_absence_resume": True,
            "controller_authority_marker_held_through_final_receipt_persistence": True,
            "administrative_cooperative_writer_fence_implemented": True,
            "equivalent_privileged_root_bypass_excluded": False,
            "loopback_only_endpoint": True,
            "advisory_lock_and_zero_external_client_observation_required": True,
            "exact_prefix_only_resume": True,
        },
        "execution_gate": {
            "fixed_orchestration_machine_packaged": True,
            "fixed_catalog_query_contract_packaged": True,
            "operation_to_sql_translation_packaged": True,
            "concrete_psycopg_adapter_packaged": True,
            "runtime_ready": True,
            "driver_ready": True,
            "catalog_ready": True,
            "current_constructor_refuses_before_primitive_call": True,
            (
                "caller_sql_dsn_endpoint_database_role_path_or_"
                "expected_catalog_allowed"
            ): False,
            "live_execution_allowed": False,
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


__all__ = [
    "ADVISORY_LOCK_KEY",
    "APPROVED_TERMINAL_CATALOG_SHA256",
    "BOOTSTRAP_DATABASE",
    "BOOTSTRAP_ROLE",
    "CATALOG_QUERIES",
    "CatalogQueryId",
    "ClosedPostgreSQLStageMachine",
    "DriverRuntimeObservation",
    "EndpointObservation",
    "EXPECTED_DRIVER_IDENTITY_SHA256",
    "ExternalClientObservation",
    "FORWARD_TRANSITIONS",
    "FixedCatalogQuery",
    "FixedSyncPostgreSQLPrimitive",
    "ForwardOperation",
    "ForwardPrefix",
    "INSTALL_TARGET_PREFIXES",
    "INDEPENDENT_NATIVE_AUDIT_IDENTITY_SHA256",
    "InstallTarget",
    "NATIVE_STAGES",
    "NativeStageDescriptor",
    "NativeStageReceipt",
    "OWNER_ROLE",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_SERVER_VERSION",
    "PostgreSQLNativeStageError",
    "PrivacySettingsObservation",
    "ROLLBACK_TRANSITIONS",
    "ROLLBACK_TARGET_PREFIXES",
    "RollbackObservation",
    "RollbackOperation",
    "RollbackPrefix",
    "RollbackTarget",
    "SOURCE_CLOSURE_SHA256",
    "SOURCE_SHA256",
    "StageId",
    "TARGET_DATABASE",
    "construct_current_machine",
    "contract_document",
    "normalize_catalog_rows",
    "runtime_driver_probe_document",
]

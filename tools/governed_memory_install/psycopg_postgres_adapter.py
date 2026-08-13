from __future__ import annotations

"""Closed Psycopg 3 adapter for the dormant successor PostgreSQL store.

The public constructor accepts capabilities, not a DSN, endpoint, database,
role, SQL string, path, password, or expected catalog.  Every connection,
query, source identity, transaction setting, and role transition is fixed in
this module.  Import and construction perform no I/O; the selected driver and
the root-owned secret are opened only by an explicit effect or observation.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import importlib
import json
from pathlib import Path
import platform
import re
from typing import Final, Iterator, Mapping, Protocol

from .linux_store_effects import BoundResourceSnapshot, EffectPresence
from .linux_store_readiness import (
    CanonicalPostgreSQLRole,
    PostgreSQLCatalogIdentity,
    PostgreSQLRoleMembership,
    PrebootstrapPostgreSQLSnapshot,
    TerminalPostgreSQLSnapshot,
)
from .postgres_native_stages import (
    ADVISORY_LOCK_KEY,
    APPROVED_TERMINAL_CATALOG_SHA256,
    BOOTSTRAP_DATABASE,
    BOOTSTRAP_ROLE,
    CATALOG_QUERIES,
    CONNECT_TIMEOUT_SECONDS,
    EXPECTED_DRIVER_IDENTITY_SHA256,
    ExternalClientObservation,
    CatalogQueryId,
    CatalogRows,
    DriverRuntimeObservation,
    EndpointObservation,
    FORWARD_TRANSITIONS,
    ForwardOperation,
    ForwardPrefix,
    InstallTarget,
    LOCK_TIMEOUT_MILLISECONDS,
    NativeStageReceipt,
    OWNER_ROLE,
    POSTGRES_HOST,
    POSTGRES_PORT,
    POSTGRES_SERVER_VERSION,
    PREFERRED_LIBPQ_VERSION,
    PREFERRED_PSYCOPG_VERSION,
    PrivacySettingsObservation,
    PYTHON_VERSION,
    ROLLBACK_TRANSITIONS,
    RollbackObservation,
    RollbackOperation,
    RollbackPrefix,
    SOURCE_SHA256,
    STATEMENT_TIMEOUT_MILLISECONDS,
    TARGET_DATABASE,
    normalize_catalog_rows,
)
from .store_readiness import POSTGRES_BIND, REQUIRED_ROLE_NAMES, TERMINAL_MIGRATION_IDS


APPLICATION_NAME: Final = "governed-memory-controller"
NATIVE_FILE_COUNT: Final = 17
MAX_SECRET_BYTES: Final = 128
MIN_SECRET_BYTES: Final = 43
MAX_RECEIPT_BYTES: Final = 256 * 1024
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SECRET_RE = re.compile(rb"[A-Za-z0-9_-]{43,128}\Z", re.ASCII)
_SAFE_TEXT_RE = re.compile(r"[\x20-\x7e]{0,4096}\Z", re.ASCII)


class PsycopgPostgreSQLAdapterError(RuntimeError):
    """Content-free refusal from the fixed PostgreSQL transport."""


class FixedPostgreSQLSecretSource(Protocol):
    """Capability for the one fixed root-owned PostgreSQL secret file."""

    def read_fixed_postgres_password(self) -> bytes: ...


class PostgreSQLStageReceiptSink(Protocol):
    """Capability for fixed execution receipts and rollback-marker state."""

    def persist_postgres_stage_receipt(self, receipt: NativeStageReceipt) -> None: ...

    def postgres_controller_authority_marker_held(self) -> bool: ...


_RUNTIME_CAPABILITY_TOKEN = object()


@dataclass(frozen=True, slots=True)
class VerifiedPsycopgRuntimeCapability:
    """Opaque package-bound evidence for the selected controller runtime."""

    _token: object
    runtime_receipt_sha256: str
    native_closure_receipt_sha256: str
    driver_identity_sha256: str
    python_version: str
    psycopg_version: str
    pq_impl: str
    libpq_version: int
    native_file_count: int

    def __post_init__(self) -> None:
        if (
            self._token is not _RUNTIME_CAPABILITY_TOKEN
            or _HASH_RE.fullmatch(self.runtime_receipt_sha256) is None
            or _HASH_RE.fullmatch(self.native_closure_receipt_sha256) is None
            or self.driver_identity_sha256 != EXPECTED_DRIVER_IDENTITY_SHA256
            or self.python_version != PYTHON_VERSION
            or self.psycopg_version != PREFERRED_PSYCOPG_VERSION
            or self.pq_impl != "binary"
            or self.libpq_version != PREFERRED_LIBPQ_VERSION
            or self.native_file_count != NATIVE_FILE_COUNT
        ):
            raise PsycopgPostgreSQLAdapterError(
                "psycopg_runtime_capability_invalid"
            )


def _mint_synthetic_runtime_capability(
    *,
    runtime_receipt_sha256: str,
    native_closure_receipt_sha256: str,
) -> VerifiedPsycopgRuntimeCapability:
    """Private proof seam.  Production minting belongs to package verification."""

    return VerifiedPsycopgRuntimeCapability(
        _RUNTIME_CAPABILITY_TOKEN,
        runtime_receipt_sha256,
        native_closure_receipt_sha256,
        EXPECTED_DRIVER_IDENTITY_SHA256,
        PYTHON_VERSION,
        PREFERRED_PSYCOPG_VERSION,
        "binary",
        PREFERRED_LIBPQ_VERSION,
        NATIVE_FILE_COUNT,
    )


def verified_psycopg_runtime_capability(
    verified_controller_runtime_capability: object,
) -> VerifiedPsycopgRuntimeCapability:
    """Derive the driver capability only from verified runtime evidence."""

    try:
        from .controller_runtime import _controller_runtime_capability_evidence

        evidence = _controller_runtime_capability_evidence(
            verified_controller_runtime_capability
        )
    except Exception:
        raise PsycopgPostgreSQLAdapterError(
            "psycopg_controller_runtime_capability_invalid"
        ) from None
    if (
        evidence.python_implementation != "CPython"
        or evidence.python_version != PYTHON_VERSION
        or evidence.platform_os != "linux"
        or evidence.platform_architecture != "x86_64"
        or evidence.postgresql_driver_identity_sha256
        != EXPECTED_DRIVER_IDENTITY_SHA256
        or evidence.persistent_controller_substrate_created is not True
        or evidence.persistent_store_resources_created is not False
    ):
        raise PsycopgPostgreSQLAdapterError(
            "psycopg_controller_runtime_evidence_invalid"
        )
    return VerifiedPsycopgRuntimeCapability(
        _RUNTIME_CAPABILITY_TOKEN,
        evidence.controller_runtime_receipt_sha256,
        evidence.postgresql_driver_identity_sha256,
        evidence.postgresql_driver_identity_sha256,
        evidence.python_version,
        PREFERRED_PSYCOPG_VERSION,
        "binary",
        PREFERRED_LIBPQ_VERSION,
        NATIVE_FILE_COUNT,
    )


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
        raise PsycopgPostgreSQLAdapterError(
            "postgres_adapter_document_invalid"
        ) from None


def _split_fixed_sql(sql: str) -> tuple[str, ...]:
    """Split a verified PostgreSQL source without entering dollar bodies."""

    if type(sql) is not str or not sql or "\x00" in sql:
        raise PsycopgPostgreSQLAdapterError("postgres_fixed_sql_invalid")
    statements: list[str] = []
    start = 0
    index = 0
    state = "normal"
    dollar_tag = ""
    block_depth = 0
    while index < len(sql):
        if state == "single":
            if sql[index] == "'":
                if index + 1 < len(sql) and sql[index + 1] == "'":
                    index += 2
                    continue
                state = "normal"
            index += 1
            continue
        if state == "double":
            if sql[index] == '"':
                if index + 1 < len(sql) and sql[index + 1] == '"':
                    index += 2
                    continue
                state = "normal"
            index += 1
            continue
        if state == "line_comment":
            if sql[index] == "\n":
                state = "normal"
            index += 1
            continue
        if state == "block_comment":
            if sql.startswith("/*", index):
                block_depth += 1
                index += 2
            elif sql.startswith("*/", index):
                block_depth -= 1
                index += 2
                if block_depth == 0:
                    state = "normal"
            else:
                index += 1
            continue
        if state == "dollar":
            if sql.startswith(dollar_tag, index):
                index += len(dollar_tag)
                state = "normal"
            else:
                index += 1
            continue
        if sql.startswith("--", index):
            state = "line_comment"
            index += 2
        elif sql.startswith("/*", index):
            state = "block_comment"
            block_depth = 1
            index += 2
        elif sql[index] == "'":
            state = "single"
            index += 1
        elif sql[index] == '"':
            state = "double"
            index += 1
        elif sql[index] == "$":
            match = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", sql[index:])
            if match is None:
                index += 1
            else:
                dollar_tag = match.group(0)
                state = "dollar"
                index += len(dollar_tag)
        elif sql[index] == ";":
            statement = sql[start : index + 1].strip()
            if statement:
                statements.append(statement)
            start = index + 1
            index += 1
        else:
            index += 1
    if state not in {"normal", "line_comment"}:
        raise PsycopgPostgreSQLAdapterError("postgres_fixed_sql_unterminated")
    tail = sql[start:].strip()
    if tail:
        statements.append(tail)
    return tuple(statements)


@dataclass(frozen=True, slots=True)
class _VerifiedArtifact:
    path: str
    content: bytes
    sha256: str


def _verified_artifact(value: object, expected_path: str) -> _VerifiedArtifact:
    content = getattr(value, "content", None)
    path = getattr(value, "artifact_path", None)
    supplied = getattr(value, "sha256", None)
    expected = SOURCE_SHA256.get(expected_path)
    if (
        type(path) is not str
        or path != expected_path
        or type(content) is not bytes
        or not content
        or b"\x00" in content
        or type(supplied) is not str
        or supplied != expected
        or _sha256(content) != expected
    ):
        raise PsycopgPostgreSQLAdapterError("postgres_source_artifact_invalid")
    return _VerifiedArtifact(path, content, supplied)


_PRIVACY_QUERY = CATALOG_QUERIES[CatalogQueryId.PRIVACY_SETTINGS]
_EXTERNAL_CLIENT_QUERY = CATALOG_QUERIES[CatalogQueryId.EXTERNAL_CLIENTS]
_SEMANTIC_EMPTY_QUERY = CATALOG_QUERIES[CatalogQueryId.SEMANTIC_EMPTY]

_ROLE_PREFIX: Final = (
    "governed_memory_owner",
    "governed_memory_api",
    "governed_memory_worker",
    "memory_ingest_writer",
    "memory_erasure_requester",
)

_ROLE_SQL: Final[Mapping[ForwardOperation, str]] = {
    ForwardOperation.F01_CREATE_OWNER_ROLE: (
        "CREATE ROLE governed_memory_owner NOLOGIN NOINHERIT NOSUPERUSER "
        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    ),
    ForwardOperation.F01_CREATE_API_ROLE: (
        "CREATE ROLE governed_memory_api NOLOGIN NOINHERIT NOSUPERUSER "
        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    ),
    ForwardOperation.F01_CREATE_WORKER_ROLE: (
        "CREATE ROLE governed_memory_worker NOLOGIN NOINHERIT NOSUPERUSER "
        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    ),
    ForwardOperation.F01_CREATE_INGEST_ROLE: (
        "CREATE ROLE memory_ingest_writer NOLOGIN NOINHERIT NOSUPERUSER "
        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    ),
    ForwardOperation.F01_CREATE_ERASURE_ROLE: (
        "CREATE ROLE memory_erasure_requester NOLOGIN NOINHERIT NOSUPERUSER "
        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    ),
}

_BOOTSTRAP_OBSERVE_SQL: Final = (
    "SELECT role.rolname::text,role.rolcanlogin,role.rolinherit,role.rolsuper,"
    "role.rolcreatedb,role.rolcreaterole,role.rolreplication,role.rolbypassrls,"
    "role.rolconnlimit,role.rolpassword IS NULL,role.rolvaliduntil IS NULL "
    "FROM pg_catalog.pg_authid AS role WHERE role.rolname IN ("
    "'governed_memory_owner','governed_memory_api','governed_memory_worker',"
    "'memory_ingest_writer','memory_erasure_requester') "
    "ORDER BY role.rolname COLLATE \"C\""
)
_DATABASE_OBSERVE_SQL: Final = (
    "SELECT database.datname::text,pg_catalog.pg_get_userbyid(database.datdba)::text,"
    "database.encoding=pg_catalog.pg_char_to_encoding('UTF8'),database.datistemplate,"
    "database.datallowconn,COALESCE(database.datacl::text,'') "
    "FROM pg_catalog.pg_database AS database WHERE database.datname='governed_memory'"
)
_MEMBERSHIP_OBSERVE_SQL: Final = (
    "SELECT granted.rolname::text,member.rolname::text,grantor.rolname::text,"
    "membership.admin_option,membership.inherit_option,membership.set_option "
    "FROM pg_catalog.pg_auth_members AS membership "
    "JOIN pg_catalog.pg_roles AS granted ON granted.oid=membership.roleid "
    "JOIN pg_catalog.pg_roles AS member ON member.oid=membership.member "
    "JOIN pg_catalog.pg_roles AS grantor ON grantor.oid=membership.grantor "
    "WHERE granted.rolname IN ('governed_memory_owner','governed_memory_api',"
    "'governed_memory_worker','memory_ingest_writer','memory_erasure_requester') "
    "OR member.rolname IN ('governed_memory_owner','governed_memory_api',"
    "'governed_memory_worker','memory_ingest_writer','memory_erasure_requester')"
)
_TARGET_BOOTSTRAP_OBSERVE_SQL: Final = (
    "SELECT NOT EXISTS (SELECT 1 FROM pg_catalog.pg_database AS database "
    "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(database.datacl,"
    "pg_catalog.acldefault('d',database.datdba))) AS acl "
    "WHERE database.datname='governed_memory' AND acl.grantee=0 "
    "AND acl.privilege_type IN ('CONNECT','CREATE','TEMPORARY')) ,"
    "pg_catalog.has_database_privilege('governed_memory_api','governed_memory','CONNECT'),"
    "pg_catalog.has_database_privilege('governed_memory_worker','governed_memory','CONNECT'),"
    "NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace AS namespace "
    "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(namespace.nspacl,"
    "pg_catalog.acldefault('n',namespace.nspowner))) AS acl "
    "WHERE namespace.nspname='public' AND acl.grantee=0 "
    "AND acl.privilege_type='CREATE'),"
    "EXISTS (SELECT 1 FROM pg_catalog.pg_extension AS extension "
    "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=extension.extnamespace "
    "JOIN pg_catalog.pg_roles AS owner ON owner.oid=extension.extowner "
    "WHERE extension.extname='pgcrypto' AND extension.extversion='1.3' "
    "AND namespace.nspname='public' AND owner.rolname='governed_memory_owner')"
)

_FOUNDATION_OBSERVE_SQL: Final = (
    "SELECT pg_catalog.to_regnamespace('memory') IS NOT NULL,"
    "pg_catalog.to_regnamespace('memory_private') IS NOT NULL,"
    "pg_catalog.to_regclass('memory.predicate_catalog') IS NOT NULL,"
    "pg_catalog.to_regprocedure('memory_private.current_owner_id()') IS NOT NULL"
)
_CLAIM_DETAIL_OBSERVE_SQL: Final = (
    "SELECT pg_catalog.to_regprocedure('memory_private.read_claim(uuid)') IS NOT NULL"
)
_PILOT_MARKER_OBSERVE_SQL: Final = (
    "SELECT pg_catalog.to_regclass('memory.pilot_marker') IS NOT NULL,"
    "pg_catalog.to_regprocedure('memory_private.mark_pilot_started(text,uuid,text,text,timestamptz)') IS NOT NULL,"
    "pg_catalog.to_regprocedure('memory_private.read_pilot_marker()') IS NOT NULL"
)


class PsycopgPostgreSQLAdapter:
    """One exact synchronous adapter for effects, readiness, and stage proof."""

    bind = POSTGRES_BIND

    def __init__(
        self,
        *,
        secret_source: FixedPostgreSQLSecretSource,
        receipt_sink: PostgreSQLStageReceiptSink,
        runtime_capability: VerifiedPsycopgRuntimeCapability,
    ) -> None:
        if (
            not callable(getattr(secret_source, "read_fixed_postgres_password", None))
            or not callable(
                getattr(receipt_sink, "persist_postgres_stage_receipt", None)
            )
            or not callable(
                getattr(
                    receipt_sink,
                    "postgres_controller_authority_marker_held",
                    None,
                )
            )
            or type(runtime_capability) is not VerifiedPsycopgRuntimeCapability
        ):
            raise PsycopgPostgreSQLAdapterError(
                "postgres_adapter_capability_invalid"
            )
        runtime_capability.__post_init__()
        self._secret_source = secret_source
        self._receipt_sink = receipt_sink
        self._runtime = runtime_capability
        self._lock_connection: object | None = None
        self._last_empty_proof_sha256: str | None = None
        self._forward_session_prefix: ForwardPrefix | None = None

    def _driver(self) -> object:
        try:
            driver = importlib.import_module("psycopg")
            pq = getattr(driver, "pq")
            version = getattr(driver, "__version__")
            impl = getattr(pq, "__impl__")
            libpq_version = pq.version()
        except Exception:
            raise PsycopgPostgreSQLAdapterError("psycopg_runtime_import_failed") from None
        if (
            platform.python_version() != self._runtime.python_version
            or version != self._runtime.psycopg_version
            or impl != self._runtime.pq_impl
            or libpq_version != self._runtime.libpq_version
        ):
            raise PsycopgPostgreSQLAdapterError("psycopg_runtime_identity_drift")
        return driver

    def _password(self) -> str:
        try:
            raw = self._secret_source.read_fixed_postgres_password()
        except Exception:
            raise PsycopgPostgreSQLAdapterError("postgres_secret_read_failed") from None
        if type(raw) is not bytes or _SECRET_RE.fullmatch(raw) is None:
            raise PsycopgPostgreSQLAdapterError("postgres_secret_invalid")
        try:
            return raw.decode("ascii")
        except UnicodeDecodeError:
            raise PsycopgPostgreSQLAdapterError("postgres_secret_invalid") from None

    def _connect(self, database: str, *, autocommit: bool) -> object:
        if database not in {BOOTSTRAP_DATABASE, TARGET_DATABASE}:
            raise PsycopgPostgreSQLAdapterError("postgres_database_identity_invalid")
        driver = self._driver()
        try:
            connection = driver.connect(
                host=POSTGRES_HOST,
                port=POSTGRES_PORT,
                dbname=database,
                user=BOOTSTRAP_ROLE,
                password=self._password(),
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                application_name=APPLICATION_NAME,
                autocommit=autocommit,
            )
        except Exception:
            raise PsycopgPostgreSQLAdapterError("postgres_fixed_connect_failed") from None
        return connection

    @contextmanager
    def _connection(self, database: str, *, autocommit: bool = True) -> Iterator[object]:
        connection = self._connect(database, autocommit=autocommit)
        try:
            yield connection
        finally:
            try:
                connection.close()
            except Exception:
                raise PsycopgPostgreSQLAdapterError(
                    "postgres_fixed_connection_close_failed"
                ) from None

    @staticmethod
    def _execute(connection: object, sql: str, *, one: bool = False) -> object:
        if type(sql) is not str or not sql or "\x00" in sql:
            raise PsycopgPostgreSQLAdapterError("postgres_fixed_sql_invalid")
        try:
            cursor = connection.execute(sql)
            if one:
                return cursor.fetchone()
            return cursor
        except Exception:
            raise PsycopgPostgreSQLAdapterError("postgres_fixed_sql_failed") from None

    @staticmethod
    def _rows(connection: object, sql: str) -> tuple[tuple[object, ...], ...]:
        try:
            cursor = connection.execute(sql)
            rows = cursor.fetchall()
        except Exception:
            raise PsycopgPostgreSQLAdapterError("postgres_fixed_query_failed") from None
        if type(rows) not in {list, tuple}:
            raise PsycopgPostgreSQLAdapterError("postgres_fixed_query_result_invalid")
        normalized: list[tuple[object, ...]] = []
        for row in rows:
            if not isinstance(row, (list, tuple)):
                raise PsycopgPostgreSQLAdapterError(
                    "postgres_fixed_query_result_invalid"
                )
            normalized.append(tuple(row))
        return tuple(normalized)

    @staticmethod
    def _set_session_limits(connection: object, *, local: bool) -> None:
        scope = "LOCAL " if local else ""
        PsycopgPostgreSQLAdapter._execute(
            connection,
            f"SET {scope}lock_timeout='{LOCK_TIMEOUT_MILLISECONDS}ms'",
        )
        PsycopgPostgreSQLAdapter._execute(
            connection,
            f"SET {scope}statement_timeout='{STATEMENT_TIMEOUT_MILLISECONDS}ms'",
        )

    @staticmethod
    def _set_owner_role(connection: object) -> None:
        PsycopgPostgreSQLAdapter._execute(
            connection, "SET LOCAL ROLE governed_memory_owner"
        )
        row = PsycopgPostgreSQLAdapter._execute(
            connection,
            "SELECT current_user::text,session_user::text,current_database()::text",
            one=True,
        )
        if tuple(row) != (OWNER_ROLE, BOOTSTRAP_ROLE, TARGET_DATABASE):
            raise PsycopgPostgreSQLAdapterError("postgres_set_role_verification_failed")

    def endpoint_identity(self) -> EndpointObservation:
        connection = self._lock_connection
        owns = False
        if connection is None:
            connection = self._connect(BOOTSTRAP_DATABASE, autocommit=True)
            owns = True
        try:
            row = self._execute(
                connection,
                "SELECT current_database()::text,current_user::text,session_user::text,"
                "pg_catalog.current_setting('server_version')::text,"
                "pg_catalog.current_setting('server_encoding')::text",
                one=True,
            )
            if tuple(row) != (
                BOOTSTRAP_DATABASE,
                BOOTSTRAP_ROLE,
                BOOTSTRAP_ROLE,
                POSTGRES_SERVER_VERSION,
                "UTF8",
            ):
                raise PsycopgPostgreSQLAdapterError("postgres_endpoint_identity_drift")
            return EndpointObservation(
                POSTGRES_HOST,
                POSTGRES_PORT,
                BOOTSTRAP_DATABASE,
                TARGET_DATABASE,
                BOOTSTRAP_ROLE,
            )
        finally:
            if owns:
                connection.close()

    def driver_runtime_identity(self) -> DriverRuntimeObservation:
        self._driver()
        return DriverRuntimeObservation(
            self._runtime.python_version,
            self._runtime.psycopg_version,
            "synchronous",
            self._runtime.pq_impl,
            self._runtime.libpq_version,
            True,
            self._runtime.native_closure_receipt_sha256,
            self._runtime.runtime_receipt_sha256,
            self._runtime.driver_identity_sha256,
        )

    def acquire_fixed_session_lock(self) -> None:
        if self._lock_connection is not None:
            raise PsycopgPostgreSQLAdapterError("postgres_session_lock_already_held")
        connection = self._connect(BOOTSTRAP_DATABASE, autocommit=True)
        try:
            self._set_session_limits(connection, local=False)
            row = self._execute(
                connection,
                f"SELECT pg_catalog.pg_try_advisory_lock({ADVISORY_LOCK_KEY})",
                one=True,
            )
            if tuple(row) != (True,):
                raise PsycopgPostgreSQLAdapterError("postgres_session_lock_unavailable")
        except Exception:
            connection.close()
            raise
        self._lock_connection = connection
        self._forward_session_prefix = None

    def fixed_session_lock_held(self) -> bool:
        connection = self._lock_connection
        if connection is None:
            return False
        try:
            row = self._execute(connection, "SELECT 1", one=True)
            return tuple(row) == (1,)
        except Exception:
            return False

    def release_fixed_session_lock(self) -> None:
        connection = self._lock_connection
        self._lock_connection = None
        self._forward_session_prefix = None
        if connection is None:
            return
        try:
            row = self._execute(
                connection,
                f"SELECT pg_catalog.pg_advisory_unlock({ADVISORY_LOCK_KEY})",
                one=True,
            )
            if tuple(row) != (True,):
                raise PsycopgPostgreSQLAdapterError("postgres_session_lock_release_failed")
        finally:
            connection.close()

    @contextmanager
    def _exclusive_session(self) -> Iterator[None]:
        owns = self._lock_connection is None
        if owns:
            self.acquire_fixed_session_lock()
        try:
            if not self.fixed_session_lock_held():
                raise PsycopgPostgreSQLAdapterError(
                    "postgres_session_lock_not_held"
                )
            self.endpoint_identity()
            self.observe_privacy_settings()
            self.observe_external_clients()
            yield
        finally:
            if owns:
                self.release_fixed_session_lock()

    def _catalog_rows(self, query_id: CatalogQueryId) -> CatalogRows:
        if type(query_id) is not CatalogQueryId:
            raise PsycopgPostgreSQLAdapterError("postgres_catalog_query_id_invalid")
        query = CATALOG_QUERIES[query_id]
        with self._connection(query.database) as connection:
            if query.role == OWNER_ROLE:
                try:
                    with connection.transaction():
                        self._set_session_limits(connection, local=True)
                        self._set_owner_role(connection)
                        return self._rows(connection, query.sql)
                except PsycopgPostgreSQLAdapterError:
                    raise
                except Exception:
                    raise PsycopgPostgreSQLAdapterError(
                        "postgres_catalog_query_failed"
                    ) from None
            self._set_session_limits(connection, local=False)
            return self._rows(connection, query.sql)

    def terminal_catalog_rows(self, query_id: CatalogQueryId) -> CatalogRows:
        return self._catalog_rows(query_id)

    def observe_privacy_settings(self) -> PrivacySettingsObservation:
        rows = self._catalog_rows(CatalogQueryId.PRIVACY_SETTINGS)
        if len(rows) != 1 or len(rows[0]) != 9:
            raise PsycopgPostgreSQLAdapterError("postgres_privacy_settings_invalid")
        try:
            return PrivacySettingsObservation(*rows[0])
        except Exception:
            raise PsycopgPostgreSQLAdapterError("postgres_privacy_settings_invalid") from None

    def observe_external_clients(self) -> ExternalClientObservation:
        rows = self._catalog_rows(CatalogQueryId.EXTERNAL_CLIENTS)
        if len(rows) != 1 or len(rows[0]) != 2:
            raise PsycopgPostgreSQLAdapterError(
                "postgres_external_client_observation_invalid"
            )
        try:
            return ExternalClientObservation(*rows[0])
        except Exception:
            raise PsycopgPostgreSQLAdapterError(
                "postgres_external_client_observation_invalid"
            ) from None

    def controller_authority_marker_held(self) -> bool:
        try:
            return (
                self._receipt_sink.postgres_controller_authority_marker_held()
                is True
            )
        except Exception:
            return False

    def persist_stage_receipt(self, receipt: NativeStageReceipt) -> None:
        if type(receipt) is not NativeStageReceipt:
            raise PsycopgPostgreSQLAdapterError("postgres_stage_receipt_invalid")
        try:
            self._receipt_sink.persist_postgres_stage_receipt(receipt)
        except Exception:
            raise PsycopgPostgreSQLAdapterError("postgres_stage_receipt_failed") from None

    def _role_and_database_state(self) -> tuple[int, bool, bool, bool, bool]:
        with self._connection(BOOTSTRAP_DATABASE) as connection:
            roles = self._rows(connection, _BOOTSTRAP_OBSERVE_SQL)
            database = self._rows(connection, _DATABASE_OBSERVE_SQL)
            memberships = self._rows(connection, _MEMBERSHIP_OBSERVE_SQL)
        role_by_name = {str(row[0]): row for row in roles}
        present = tuple(name for name in _ROLE_PREFIX if name in role_by_name)
        if present != _ROLE_PREFIX[: len(present)] or len(role_by_name) != len(present):
            raise PsycopgPostgreSQLAdapterError("postgres_role_prefix_drift")
        for name in present:
            row = role_by_name[name]
            if tuple(row[1:]) != (
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                -1,
                True,
                True,
            ):
                raise PsycopgPostgreSQLAdapterError("postgres_role_prefix_drift")
        database_exists = bool(database)
        if len(database) > 1 or (database_exists and len(present) != 5):
            raise PsycopgPostgreSQLAdapterError("postgres_database_prefix_drift")
        database_acl = False
        if database_exists:
            if tuple(database[0][0:5]) != (
                TARGET_DATABASE,
                OWNER_ROLE,
                True,
                False,
                True,
            ):
                raise PsycopgPostgreSQLAdapterError("postgres_database_prefix_drift")
            with self._connection(TARGET_DATABASE) as target:
                target_state = self._rows(target, _TARGET_BOOTSTRAP_OBSERVE_SQL)
            if len(target_state) != 1 or len(target_state[0]) != 5:
                raise PsycopgPostgreSQLAdapterError("postgres_database_prefix_drift")
            no_public, api_connect, worker_connect, no_public_create, pgcrypto = (
                target_state[0]
            )
            database_acl = bool(
                no_public and api_connect and worker_connect and no_public_create
            )
        else:
            pgcrypto = False
        expected_membership = (
            OWNER_ROLE,
            BOOTSTRAP_ROLE,
            BOOTSTRAP_ROLE,
            False,
            True,
            True,
        )
        membership = len(memberships) == 1 and tuple(memberships[0]) == expected_membership
        if memberships and not membership:
            raise PsycopgPostgreSQLAdapterError("postgres_membership_prefix_drift")
        return len(present), database_exists, database_acl, membership, bool(pgcrypto)

    def _migration_presence(self) -> tuple[bool, bool, bool]:
        with self._connection(TARGET_DATABASE) as connection:
            with connection.transaction():
                self._set_owner_role(connection)
                foundation_row = self._execute(
                    connection, _FOUNDATION_OBSERVE_SQL, one=True
                )
                foundation_bits = tuple(foundation_row)
                if any(foundation_bits) and not all(foundation_bits):
                    raise PsycopgPostgreSQLAdapterError(
                        "postgres_foundation_prefix_drift"
                    )
                foundation = bool(all(foundation_bits))
                if not foundation:
                    return False, False, False
                claim = bool(
                    tuple(
                        self._execute(connection, _CLAIM_DETAIL_OBSERVE_SQL, one=True)
                    )[0]
                )
                pilot_bits = tuple(
                    self._execute(connection, _PILOT_MARKER_OBSERVE_SQL, one=True)
                )
                if any(pilot_bits) and not all(pilot_bits):
                    raise PsycopgPostgreSQLAdapterError(
                        "postgres_pilot_marker_prefix_drift"
                    )
                pilot = bool(all(pilot_bits))
                if pilot and not claim:
                    raise PsycopgPostgreSQLAdapterError("postgres_migration_prefix_drift")
                return foundation, claim, pilot

    def observe_forward_prefix(self) -> ForwardPrefix:
        if (
            self._lock_connection is not None
            and self._forward_session_prefix is not None
        ):
            return self._forward_session_prefix
        roles, database, acl, membership, pgcrypto = self._role_and_database_state()
        if roles == 0 and not database:
            return ForwardPrefix.EMPTY
        if roles < 5:
            return (
                ForwardPrefix.OWNER_ROLE,
                ForwardPrefix.API_ROLE,
                ForwardPrefix.WORKER_ROLE,
                ForwardPrefix.INGEST_ROLE,
            )[roles - 1]
        if not database:
            return ForwardPrefix.ALL_ROLES
        if not acl:
            return ForwardPrefix.DATABASE
        if not membership:
            return ForwardPrefix.DATABASE_ACL
        if not pgcrypto:
            return ForwardPrefix.OWNER_MEMBERSHIP
        foundation, claim, pilot = self._migration_presence()
        if pilot:
            return ForwardPrefix.READY_FOR_CATALOG
        if claim:
            return ForwardPrefix.CLAIM_DETAIL
        if foundation:
            return ForwardPrefix.FOUNDATION
        # I11 includes the fixed owner-role preflight.  It is validation-only;
        # exact bootstrap state is therefore its durable target identity.
        return ForwardPrefix.ROLES_PREFLIGHT

    @staticmethod
    def _load_fixed_source(expected_path: str) -> _VerifiedArtifact:
        if expected_path not in SOURCE_SHA256:
            raise PsycopgPostgreSQLAdapterError("postgres_source_identity_invalid")
        root = Path(__file__).resolve().parents[2]
        candidate = root.joinpath(*expected_path.split("/"))
        try:
            content = candidate.read_bytes()
        except OSError:
            raise PsycopgPostgreSQLAdapterError("postgres_fixed_source_read_failed") from None
        expected = SOURCE_SHA256[expected_path]
        if not content or b"\x00" in content or _sha256(content) != expected:
            raise PsycopgPostgreSQLAdapterError("postgres_fixed_source_identity_drift")
        return _VerifiedArtifact(expected_path, content, expected)

    def _apply_migration_source(self, source: _VerifiedArtifact) -> None:
        try:
            sql = source.content.decode("utf-8")
        except UnicodeDecodeError:
            raise PsycopgPostgreSQLAdapterError("postgres_source_artifact_invalid") from None
        # The artifacts are already bound by exact path and SHA-256.  Reject
        # only psql meta-command lines; SQL escape strings and regular
        # expressions legitimately contain backslashes.
        if any(line.lstrip().startswith("\\") for line in sql.splitlines()):
            raise PsycopgPostgreSQLAdapterError("postgres_migration_metacommand_refused")
        with self._connection(TARGET_DATABASE, autocommit=False) as connection:
            try:
                with connection.transaction():
                    self._set_session_limits(connection, local=True)
                    self._set_owner_role(connection)
                    if not self.fixed_session_lock_held():
                        raise PsycopgPostgreSQLAdapterError(
                            "postgres_session_lock_not_held"
                        )
                    for statement in _split_fixed_sql(sql):
                        self._execute(connection, statement)
            except PsycopgPostgreSQLAdapterError:
                raise
            except Exception:
                raise PsycopgPostgreSQLAdapterError(
                    "postgres_migration_transaction_failed"
                ) from None

    def _execute_roles_preflight(self, source: _VerifiedArtifact) -> None:
        try:
            sql = source.content.decode("utf-8")
            body = "DO $preflight$" + sql.split("DO $preflight$", 1)[1]
        except (UnicodeDecodeError, IndexError):
            raise PsycopgPostgreSQLAdapterError("postgres_source_artifact_invalid") from None
        if "\\" in body:
            raise PsycopgPostgreSQLAdapterError("postgres_preflight_translation_invalid")
        with self._connection(TARGET_DATABASE, autocommit=False) as connection:
            with connection.transaction():
                self._set_session_limits(connection, local=True)
                self._set_owner_role(connection)
                self._execute(
                    connection,
                    "SET LOCAL governed_memory.inactive_installation='on'",
                )
                self._execute(connection, body)

    def _execute_fixed_psql_script(
        self, source: _VerifiedArtifact, *, rollback: bool
    ) -> None:
        """Execute the two exact cluster scripts through a closed meta interpreter."""

        try:
            lines = source.content.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            raise PsycopgPostgreSQLAdapterError("postgres_source_artifact_invalid") from None
        variables: dict[str, object] = {}
        if rollback:
            variables["governed_memory_empty_cluster_rollback"] = "on"
        database = BOOTSTRAP_DATABASE
        connection = self._connect(database, autocommit=True)
        active_stack: list[tuple[bool, bool]] = []
        active = True
        buffer: list[str] = []

        def flush(*, capture: bool = False) -> None:
            nonlocal buffer
            sql = "\n".join(buffer).strip()
            buffer = []
            if not sql or not active:
                return
            sql = sql.replace(
                ":'governed_memory_empty_cluster_rollback'", "'on'"
            )
            try:
                statements = _split_fixed_sql(sql)
                # psql's \gset applies to the final query in the current
                # buffer after executing any preceding complete statements.
                # The fixed bootstrap intentionally switches databases and
                # executes four setup statements before its postflight SELECT.
                if capture and not statements:
                    raise PsycopgPostgreSQLAdapterError(
                        "postgres_psql_gset_statement_invalid"
                    )
                for statement in statements:
                    cursor = connection.execute(statement)
                if capture:
                    row = cursor.fetchone()
                    names = tuple(item.name for item in cursor.description)
                    if row is None or len(names) != len(row):
                        raise PsycopgPostgreSQLAdapterError(
                            "postgres_psql_gset_result_invalid"
                        )
                    variables.update(zip(names, tuple(row), strict=True))
            except PsycopgPostgreSQLAdapterError:
                raise
            except Exception:
                raise PsycopgPostgreSQLAdapterError(
                    "postgres_fixed_psql_script_failed"
                ) from None

        try:
            for raw_line in lines:
                stripped = raw_line.strip()
                if not stripped.startswith("\\"):
                    if active:
                        buffer.append(raw_line)
                    continue
                command, _, argument = stripped.partition(" ")
                if command == "\\gset":
                    flush(capture=True)
                else:
                    flush()
                if command == "\\set":
                    if argument != "ON_ERROR_STOP on":
                        raise PsycopgPostgreSQLAdapterError(
                            "postgres_psql_metacommand_refused"
                        )
                elif command == "\\unset":
                    if active:
                        variables.pop(argument, None)
                elif command == "\\if":
                    parent = active
                    token = argument.strip()
                    if token.startswith(":{?") and token.endswith("}"):
                        condition = token[3:-1] in variables
                    elif token.startswith(":"):
                        condition = variables.get(token[1:]) is True
                    else:
                        raise PsycopgPostgreSQLAdapterError(
                            "postgres_psql_condition_refused"
                        )
                    active_stack.append((parent, bool(condition)))
                    active = parent and bool(condition)
                elif command == "\\else":
                    if not active_stack:
                        raise PsycopgPostgreSQLAdapterError(
                            "postgres_psql_condition_invalid"
                        )
                    parent, condition = active_stack[-1]
                    active = parent and not condition
                elif command == "\\endif":
                    if not active_stack:
                        raise PsycopgPostgreSQLAdapterError(
                            "postgres_psql_condition_invalid"
                        )
                    parent, _condition = active_stack.pop()
                    active = parent
                elif command == "\\connect":
                    if active:
                        if argument not in {BOOTSTRAP_DATABASE, TARGET_DATABASE}:
                            raise PsycopgPostgreSQLAdapterError(
                                "postgres_psql_connect_refused"
                            )
                        connection.close()
                        database = argument
                        connection = self._connect(database, autocommit=True)
                elif command == "\\gset":
                    pass
                else:
                    raise PsycopgPostgreSQLAdapterError(
                        "postgres_psql_metacommand_refused"
                    )
            flush()
            if active_stack:
                raise PsycopgPostgreSQLAdapterError(
                    "postgres_psql_condition_invalid"
                )
        finally:
            connection.close()

    def apply_forward_transition(self, operation: ForwardOperation) -> ForwardPrefix:
        if type(operation) is not ForwardOperation:
            raise PsycopgPostgreSQLAdapterError("postgres_forward_operation_invalid")
        current = self.observe_forward_prefix()
        transition = FORWARD_TRANSITIONS.get(current)
        if transition is None or transition[0] is not operation:
            raise PsycopgPostgreSQLAdapterError("postgres_forward_operation_refused")
        if operation in _ROLE_SQL:
            with self._connection(BOOTSTRAP_DATABASE) as connection:
                self._execute(connection, _ROLE_SQL[operation])
        elif operation is ForwardOperation.F01_CREATE_DATABASE:
            with self._connection(BOOTSTRAP_DATABASE) as connection:
                self._execute(
                    connection,
                    "CREATE DATABASE governed_memory OWNER governed_memory_owner "
                    "ENCODING 'UTF8' TEMPLATE template0",
                )
        elif operation is ForwardOperation.F01_BIND_DATABASE_ACL:
            with self._connection(BOOTSTRAP_DATABASE) as connection:
                self._execute(connection, "REVOKE ALL ON DATABASE governed_memory FROM PUBLIC")
                self._execute(
                    connection,
                    "GRANT CONNECT ON DATABASE governed_memory TO "
                    "governed_memory_api,governed_memory_worker",
                )
            with self._connection(TARGET_DATABASE) as connection:
                self._execute(connection, "REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        elif operation is ForwardOperation.F01_GRANT_OWNER_MEMBERSHIP:
            with self._connection(BOOTSTRAP_DATABASE) as connection:
                self._execute(
                    connection,
                    "GRANT governed_memory_owner TO governed_memory_bootstrap",
                )
        elif operation is ForwardOperation.F01_CREATE_PGCRYPTO:
            with self._connection(TARGET_DATABASE, autocommit=False) as connection:
                with connection.transaction():
                    self._set_owner_role(connection)
                    self._execute(connection, "CREATE EXTENSION pgcrypto WITH SCHEMA public")
        elif operation in {ForwardOperation.F01_VERIFY, ForwardOperation.F02_VERIFY}:
            self.observe_privacy_settings()
        elif operation is ForwardOperation.F02_ROLES_PRIVACY_PREFLIGHT:
            self._execute_roles_preflight(
                self._load_fixed_source(
                    "ops/governed_memory/installation/current/postgres/roles_preflight.pgsql"
                )
            )
        elif operation is ForwardOperation.F02_APPLY_FOUNDATION_0001:
            self._execute_roles_preflight(
                self._load_fixed_source(
                    "ops/governed_memory/installation/current/postgres/roles_preflight.pgsql"
                )
            )
            self._apply_migration_source(
                self._load_fixed_source(
                    "governed-memory-migrations/0001_foundation/forward.pgsql"
                )
            )
        elif operation is ForwardOperation.F02_APPLY_OWNER_CLAIM_DETAIL_0003:
            self._apply_migration_source(
                self._load_fixed_source(
                    "governed-memory-migrations/0003_owner_claim_detail/forward.pgsql"
                )
            )
        elif operation is ForwardOperation.F02_APPLY_PILOT_MARKER_0004:
            self._apply_migration_source(
                self._load_fixed_source(
                    "governed-memory-migrations/0004_pilot_marker/forward.pgsql"
                )
            )
        else:
            raise PsycopgPostgreSQLAdapterError(
                "postgres_forward_operation_requires_exact_artifact"
            )
        expected = transition[1]
        self._forward_session_prefix = None
        durable = self.observe_forward_prefix()
        compatible = {
            ForwardPrefix.PGCRYPTO: {ForwardPrefix.ROLES_PREFLIGHT},
            ForwardPrefix.F01_COMPLETE: {ForwardPrefix.ROLES_PREFLIGHT},
            ForwardPrefix.ROLES_PREFLIGHT: {ForwardPrefix.ROLES_PREFLIGHT},
            ForwardPrefix.PILOT_MARKER: {ForwardPrefix.READY_FOR_CATALOG},
            ForwardPrefix.READY_FOR_CATALOG: {ForwardPrefix.READY_FOR_CATALOG},
        }.get(expected, {expected})
        if durable not in compatible:
            raise PsycopgPostgreSQLAdapterError("postgres_forward_transition_drift")
        self._forward_session_prefix = expected
        return expected

    def _snapshot(self, present: bool, identity: str) -> BoundResourceSnapshot:
        if present:
            return BoundResourceSnapshot(EffectPresence.EXACT, identity)
        return BoundResourceSnapshot(EffectPresence.ABSENT)

    def observe_canonical_bootstrap(self) -> BoundResourceSnapshot:
        prefix = self.observe_forward_prefix()
        if prefix is ForwardPrefix.EMPTY:
            return BoundResourceSnapshot(EffectPresence.ABSENT)
        if prefix in {
            ForwardPrefix.ROLES_PREFLIGHT,
            ForwardPrefix.FOUNDATION,
            ForwardPrefix.CLAIM_DETAIL,
            ForwardPrefix.READY_FOR_CATALOG,
        }:
            return self._snapshot(
                True,
                SOURCE_SHA256[
                    "ops/governed_memory/installation/postgres/canonical_cluster.pgsql.in"
                ],
            )
        return BoundResourceSnapshot(EffectPresence.PARTIAL)

    def observe_migration_0001(self) -> BoundResourceSnapshot:
        prefix = self.observe_forward_prefix()
        present = prefix in {
            ForwardPrefix.FOUNDATION,
            ForwardPrefix.CLAIM_DETAIL,
            ForwardPrefix.READY_FOR_CATALOG,
        }
        return self._snapshot(
            present,
            SOURCE_SHA256["governed-memory-migrations/0001_foundation/forward.pgsql"],
        )

    def observe_migration_0003(self) -> BoundResourceSnapshot:
        prefix = self.observe_forward_prefix()
        present = prefix in {ForwardPrefix.CLAIM_DETAIL, ForwardPrefix.READY_FOR_CATALOG}
        return self._snapshot(
            present,
            SOURCE_SHA256[
                "governed-memory-migrations/0003_owner_claim_detail/forward.pgsql"
            ],
        )

    def observe_migration_0004(self) -> BoundResourceSnapshot:
        return self._snapshot(
            self.observe_forward_prefix() is ForwardPrefix.READY_FOR_CATALOG,
            SOURCE_SHA256["governed-memory-migrations/0004_pilot_marker/forward.pgsql"],
        )

    def apply_canonical_bootstrap(self, bootstrap: object, roles_preflight: object) -> None:
        bootstrap_source = _verified_artifact(
            bootstrap,
            "ops/governed_memory/installation/postgres/canonical_cluster.pgsql.in",
        )
        preflight_source = _verified_artifact(
            roles_preflight,
            "ops/governed_memory/installation/current/postgres/roles_preflight.pgsql",
        )
        with self._exclusive_session():
            prefix = self.observe_forward_prefix()
            if prefix is ForwardPrefix.EMPTY:
                self._execute_fixed_psql_script(bootstrap_source, rollback=False)
            elif prefix is not ForwardPrefix.ROLES_PREFLIGHT:
                raise PsycopgPostgreSQLAdapterError("postgres_i11_prefix_refused")
            self._forward_session_prefix = None
            self._execute_roles_preflight(preflight_source)
            if self.observe_forward_prefix() is not ForwardPrefix.ROLES_PREFLIGHT:
                raise PsycopgPostgreSQLAdapterError("postgres_i11_target_drift")

    def apply_migration_0001(self, artifact: object) -> None:
        source = _verified_artifact(
            artifact, "governed-memory-migrations/0001_foundation/forward.pgsql"
        )
        with self._exclusive_session():
            if self.observe_forward_prefix() is not ForwardPrefix.ROLES_PREFLIGHT:
                raise PsycopgPostgreSQLAdapterError("postgres_i12_prefix_refused")
            self._execute_roles_preflight(
                self._load_fixed_source(
                    "ops/governed_memory/installation/current/postgres/roles_preflight.pgsql"
                )
            )
            self._apply_migration_source(source)
            self._forward_session_prefix = None
            if self.observe_forward_prefix() is not ForwardPrefix.FOUNDATION:
                raise PsycopgPostgreSQLAdapterError("postgres_i12_target_drift")

    def apply_migration_0003(self, artifact: object) -> None:
        source = _verified_artifact(
            artifact,
            "governed-memory-migrations/0003_owner_claim_detail/forward.pgsql",
        )
        with self._exclusive_session():
            if self.observe_forward_prefix() is not ForwardPrefix.FOUNDATION:
                raise PsycopgPostgreSQLAdapterError("postgres_i13_prefix_refused")
            self._apply_migration_source(source)
            self._forward_session_prefix = None
            if self.observe_forward_prefix() is not ForwardPrefix.CLAIM_DETAIL:
                raise PsycopgPostgreSQLAdapterError("postgres_i13_target_drift")

    def apply_migration_0004(self, artifact: object) -> None:
        source = _verified_artifact(
            artifact, "governed-memory-migrations/0004_pilot_marker/forward.pgsql"
        )
        with self._exclusive_session():
            if self.observe_forward_prefix() is not ForwardPrefix.CLAIM_DETAIL:
                raise PsycopgPostgreSQLAdapterError("postgres_i14_prefix_refused")
            self._apply_migration_source(source)
            self._forward_session_prefix = None
            if self.observe_forward_prefix() is not ForwardPrefix.READY_FOR_CATALOG:
                raise PsycopgPostgreSQLAdapterError("postgres_i14_target_drift")

    def verify_semantic_empty(self) -> str:
        rows = self._catalog_rows(CatalogQueryId.SEMANTIC_EMPTY)
        if rows != ((0,),):
            raise PsycopgPostgreSQLAdapterError("postgres_semantic_empty_refused")
        document = {
            "query_id": CatalogQueryId.SEMANTIC_EMPTY.value,
            "query_sha256": _SEMANTIC_EMPTY_QUERY.sql_sha256,
            "rows": [[0]],
            "schema_version": "governed-memory-postgres-semantic-empty-v1",
        }
        proof = _sha256(_canonical_bytes(document))
        self._last_empty_proof_sha256 = proof
        return proof

    def observe_rollback_prefix(self) -> RollbackObservation:
        prefix = self.observe_forward_prefix()
        rollback = {
            ForwardPrefix.READY_FOR_CATALOG: RollbackPrefix.INSTALLED,
            ForwardPrefix.CLAIM_DETAIL: RollbackPrefix.WITHOUT_0004,
            ForwardPrefix.FOUNDATION: RollbackPrefix.WITHOUT_0003,
            ForwardPrefix.ROLES_PREFLIGHT: RollbackPrefix.DATABASE_PREFIX,
            ForwardPrefix.ALL_ROLES: RollbackPrefix.ROLES_ONLY_5,
            ForwardPrefix.INGEST_ROLE: RollbackPrefix.ROLES_ONLY_4,
            ForwardPrefix.WORKER_ROLE: RollbackPrefix.ROLES_ONLY_3,
            ForwardPrefix.API_ROLE: RollbackPrefix.ROLES_ONLY_2,
            ForwardPrefix.OWNER_ROLE: RollbackPrefix.ROLES_ONLY_1,
            ForwardPrefix.EMPTY: RollbackPrefix.EMPTY,
        }.get(prefix)
        if rollback is None:
            raise PsycopgPostgreSQLAdapterError("postgres_rollback_prefix_drift")
        proof = self._last_empty_proof_sha256
        if proof is None and rollback in {
            RollbackPrefix.INSTALLED,
            RollbackPrefix.WITHOUT_0004,
            RollbackPrefix.WITHOUT_0003,
            RollbackPrefix.DATABASE_PREFIX,
        }:
            proof = self.verify_semantic_empty()
        if proof is None:
            raise PsycopgPostgreSQLAdapterError("postgres_semantic_empty_proof_absent")
        return RollbackObservation(rollback, proof)

    def _require_rollback_boundary(self) -> None:
        if not self.controller_authority_marker_held():
            raise PsycopgPostgreSQLAdapterError(
                "postgres_controller_authority_marker_not_held"
            )
        self.observe_external_clients()

    def rollback_migration_0004(self, artifact: object) -> None:
        source = _verified_artifact(
            artifact, "governed-memory-migrations/0004_pilot_marker/rollback.pgsql"
        )
        with self._exclusive_session():
            self._require_rollback_boundary()
            self.verify_semantic_empty()
            if self.observe_forward_prefix() is not ForwardPrefix.READY_FOR_CATALOG:
                raise PsycopgPostgreSQLAdapterError("postgres_i14_rollback_prefix_refused")
            self._apply_migration_source(source)
            self._forward_session_prefix = None
            if self.observe_forward_prefix() is not ForwardPrefix.CLAIM_DETAIL:
                raise PsycopgPostgreSQLAdapterError("postgres_i14_rollback_target_drift")

    def rollback_migration_0003(self, artifact: object) -> None:
        source = _verified_artifact(
            artifact,
            "governed-memory-migrations/0003_owner_claim_detail/rollback.pgsql",
        )
        with self._exclusive_session():
            self._require_rollback_boundary()
            self.verify_semantic_empty()
            if self.observe_forward_prefix() is not ForwardPrefix.CLAIM_DETAIL:
                raise PsycopgPostgreSQLAdapterError("postgres_i13_rollback_prefix_refused")
            self._apply_migration_source(source)
            self._forward_session_prefix = None
            if self.observe_forward_prefix() is not ForwardPrefix.FOUNDATION:
                raise PsycopgPostgreSQLAdapterError("postgres_i13_rollback_target_drift")

    def rollback_migration_0001(self, artifact: object) -> None:
        source = _verified_artifact(
            artifact, "governed-memory-migrations/0001_foundation/rollback.pgsql"
        )
        with self._exclusive_session():
            self._require_rollback_boundary()
            self.verify_semantic_empty()
            if self.observe_forward_prefix() is not ForwardPrefix.FOUNDATION:
                raise PsycopgPostgreSQLAdapterError("postgres_i12_rollback_prefix_refused")
            self._apply_migration_source(source)
            self._forward_session_prefix = None
            if self.observe_forward_prefix() is not ForwardPrefix.ROLES_PREFLIGHT:
                raise PsycopgPostgreSQLAdapterError("postgres_i12_rollback_target_drift")

    def rollback_canonical_bootstrap(self, artifact: object) -> None:
        source = _verified_artifact(
            artifact,
            "ops/governed_memory/installation/postgres/canonical_cluster_rollback.pgsql.in",
        )
        with self._exclusive_session():
            self._require_rollback_boundary()
            if self.observe_forward_prefix() is not ForwardPrefix.ROLES_PREFLIGHT:
                raise PsycopgPostgreSQLAdapterError("postgres_i11_rollback_prefix_refused")
            if self._last_empty_proof_sha256 is None:
                raise PsycopgPostgreSQLAdapterError("postgres_semantic_empty_proof_absent")
            self._execute_fixed_psql_script(source, rollback=True)
            self._forward_session_prefix = None
            if self.observe_forward_prefix() is not ForwardPrefix.EMPTY:
                raise PsycopgPostgreSQLAdapterError("postgres_i11_rollback_target_drift")

    def apply_rollback_transition(
        self, operation: RollbackOperation
    ) -> RollbackObservation:
        if type(operation) is not RollbackOperation:
            raise PsycopgPostgreSQLAdapterError("postgres_rollback_operation_invalid")
        current = self.observe_rollback_prefix()
        transition = ROLLBACK_TRANSITIONS.get(current.prefix)
        if transition is None or transition[0] is not operation:
            raise PsycopgPostgreSQLAdapterError("postgres_rollback_operation_refused")
        if operation is RollbackOperation.R01_ROLLBACK_PILOT_MARKER_0004:
            source = self._load_fixed_source(
                "governed-memory-migrations/0004_pilot_marker/rollback.pgsql"
            )
            self.rollback_migration_0004(source)
        elif operation is RollbackOperation.R01_ROLLBACK_OWNER_CLAIM_DETAIL_0003:
            source = self._load_fixed_source(
                "governed-memory-migrations/0003_owner_claim_detail/rollback.pgsql"
            )
            self.rollback_migration_0003(source)
        elif operation is RollbackOperation.R01_ROLLBACK_FOUNDATION_0001:
            source = self._load_fixed_source(
                "governed-memory-migrations/0001_foundation/rollback.pgsql"
            )
            self.rollback_migration_0001(source)
        elif operation is RollbackOperation.R01_DROP_EXACT_DATABASE_PREFIX:
            source = self._load_fixed_source(
                "ops/governed_memory/installation/postgres/"
                "canonical_cluster_rollback.pgsql.in"
            )
            self.rollback_canonical_bootstrap(source)
        elif operation is RollbackOperation.R01_DROP_EXACT_ROLE_PREFIX_TRANSACTION:
            source = self._load_fixed_source(
                "ops/governed_memory/installation/postgres/"
                "canonical_cluster_rollback.pgsql.in"
            )
            self._require_rollback_boundary()
            if self._last_empty_proof_sha256 is None:
                raise PsycopgPostgreSQLAdapterError(
                    "postgres_semantic_empty_proof_absent"
                )
            self._execute_fixed_psql_script(source, rollback=True)
        else:
            raise PsycopgPostgreSQLAdapterError("postgres_rollback_operation_refused")
        self._forward_session_prefix = None
        observed = self.observe_rollback_prefix()
        if observed.prefix is not transition[1]:
            raise PsycopgPostgreSQLAdapterError("postgres_rollback_transition_drift")
        return observed

    def inspect_prebootstrap(self) -> PrebootstrapPostgreSQLSnapshot:
        self.observe_privacy_settings()
        roles, database, _acl, _membership, _pgcrypto = self._role_and_database_state()
        external = self.observe_external_clients()
        present = _ROLE_PREFIX[:roles]
        return PrebootstrapPostgreSQLSnapshot(
            POSTGRES_BIND,
            16,
            POSTGRES_SERVER_VERSION,
            BOOTSTRAP_DATABASE,
            database,
            tuple(sorted(present)),
            external.source_endpoint_count,
        )

    def inspect_terminal(self) -> TerminalPostgreSQLSnapshot:
        with self._exclusive_session():
            if self.observe_forward_prefix() is not ForwardPrefix.READY_FOR_CATALOG:
                raise PsycopgPostgreSQLAdapterError("postgres_terminal_prefix_invalid")
            rows = {
                query_id: self._catalog_rows(query_id)
                for query_id in CatalogQueryId
            }
            _encoded, terminal_catalog_sha256 = normalize_catalog_rows(rows)
            if terminal_catalog_sha256 != APPROVED_TERMINAL_CATALOG_SHA256:
                raise PsycopgPostgreSQLAdapterError(
                    "postgres_terminal_catalog_not_approved"
                )
        external_rows = rows[CatalogQueryId.EXTERNAL_CLIENTS]
        if len(external_rows) != 1 or len(external_rows[0]) != 2:
            raise PsycopgPostgreSQLAdapterError(
                "postgres_external_client_observation_invalid"
            )
        external = ExternalClientObservation(*external_rows[0])
        semantic = rows[CatalogQueryId.SEMANTIC_EMPTY]
        if len(semantic) != 1 or len(semantic[0]) != 1:
            raise PsycopgPostgreSQLAdapterError("postgres_semantic_empty_result_invalid")
        identities: list[PostgreSQLCatalogIdentity] = []
        for query_id in CatalogQueryId:
            if query_id in {
                CatalogQueryId.SESSION,
                CatalogQueryId.PRIVACY_SETTINGS,
                CatalogQueryId.EXTERNAL_CLIENTS,
                CatalogQueryId.SEMANTIC_EMPTY,
            }:
                continue
            query_rows = rows[query_id]
            for index, row in enumerate(query_rows):
                identities.append(
                    PostgreSQLCatalogIdentity(
                        "catalog_row",
                        "memory",
                        f"{query_id.value}:{index:06d}",
                        OWNER_ROLE,
                        _sha256(_canonical_bytes(list(row))),
                    )
                )
        identities.sort(
            key=lambda item: (
                item.schema_name,
                item.object_kind,
                item.object_name,
                item.owner_name,
                item.definition_sha256,
            )
        )
        roles = tuple(
            CanonicalPostgreSQLRole(
                name, False, False, False, False, False, False, False
            )
            for name in REQUIRED_ROLE_NAMES
        )
        return TerminalPostgreSQLSnapshot(
            POSTGRES_BIND,
            16,
            POSTGRES_SERVER_VERSION,
            TARGET_DATABASE,
            TERMINAL_MIGRATION_IDS,
            roles,
            (PostgreSQLRoleMembership(OWNER_ROLE, BOOTSTRAP_ROLE),),
            tuple(identities),
            int(semantic[0][0]),
            external.external_client_count,
            external.source_endpoint_count,
        )


__all__ = [
    "APPLICATION_NAME",
    "FixedPostgreSQLSecretSource",
    "PostgreSQLStageReceiptSink",
    "PsycopgPostgreSQLAdapter",
    "PsycopgPostgreSQLAdapterError",
    "VerifiedPsycopgRuntimeCapability",
    "verified_psycopg_runtime_capability",
]

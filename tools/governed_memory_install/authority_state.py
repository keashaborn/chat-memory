from __future__ import annotations

"""Durable, content-free authority state for a future dormant-store installation executor.

Only SHA-256 bindings and journal sequence numbers are persisted.  Raw
nonces, authorization documents, scope documents, credentials, and journal
entries are never written to this database.  SQLite runs in rollback-journal
mode with FULL synchronization so claim-before-effect survives a completed
commit without relying on a WAL sidecar.
"""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Final

from .execution_lock import (
    ExecutionLockError,
    HeldExecutionLockCapability,
    validate_held_execution_lock,
)


STATE_SCHEMA_VERSION: Final = 2
STATE_APPLICATION_ID: Final = 0x474D4153
ZERO_HEAD: Final = "0" * 64
HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
OPERATION_RE: Final = re.compile(
    r"[a-z][a-z0-9_]{0,63}\Z",
    re.ASCII,
)
MIN_NONCE_CHARACTERS: Final = 32
MAX_NONCE_CHARACTERS: Final = 1024

_NONCE_DOMAIN: Final = b"governed-memory-authority-nonce-v1\x00"
_OPERATION_DOMAIN: Final = b"governed-memory-authority-operation-v1\x00"
_CLAIM_DOMAIN: Final = b"governed-memory-authority-claim-v1\x00"

_NONCE_COLUMNS: Final = (
    "nonce_sha256",
    "operation_sha256",
    "execution_sha256",
    "authorization_sha256",
    "scope_sha256",
    "trust_bundle_sha256",
    "claim_sha256",
)
_ANCHOR_COLUMNS: Final = (
    "binding_sha256",
    "sequence",
    "head_sha256",
)
_RESOURCE_ANCHOR_COLUMNS: Final = (
    "binding_sha256",
    "sequence",
    "head_sha256",
)
_FILESYSTEM_IDENTITY_COLUMNS: Final = (
    "binding_sha256",
    "artifact_kind",
    "path_sha256",
    "directory_device",
    "directory_inode",
    "file_device",
    "file_inode",
)
_STATE_IDENTITY_COLUMNS: Final = (
    "singleton",
    "path_sha256",
    "directory_device",
    "directory_inode",
    "database_device",
    "database_inode",
)
_NONCE_TABLE_SQL: Final = """
CREATE TABLE nonce_claim_v1 (
    nonce_sha256 TEXT PRIMARY KEY NOT NULL,
    operation_sha256 TEXT NOT NULL,
    execution_sha256 TEXT NOT NULL,
    authorization_sha256 TEXT NOT NULL,
    scope_sha256 TEXT NOT NULL,
    trust_bundle_sha256 TEXT NOT NULL,
    claim_sha256 TEXT NOT NULL UNIQUE
) WITHOUT ROWID
""".strip()
_ANCHOR_TABLE_SQL: Final = """
CREATE TABLE journal_anchor_v1 (
    binding_sha256 TEXT PRIMARY KEY NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    head_sha256 TEXT NOT NULL
) WITHOUT ROWID
""".strip()
_RESOURCE_ANCHOR_TABLE_SQL: Final = """
CREATE TABLE resource_ledger_anchor_v1 (
    binding_sha256 TEXT PRIMARY KEY NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    head_sha256 TEXT NOT NULL
) WITHOUT ROWID
""".strip()
_FILESYSTEM_IDENTITY_TABLE_SQL: Final = """
CREATE TABLE filesystem_identity_seal_v1 (
    binding_sha256 TEXT NOT NULL,
    artifact_kind TEXT NOT NULL CHECK (
        artifact_kind IN ('journal', 'resource_ledger')
    ),
    path_sha256 TEXT NOT NULL,
    directory_device INTEGER NOT NULL CHECK (directory_device >= 0),
    directory_inode INTEGER NOT NULL CHECK (directory_inode > 0),
    file_device INTEGER NOT NULL CHECK (file_device >= 0),
    file_inode INTEGER NOT NULL CHECK (file_inode > 0),
    PRIMARY KEY (binding_sha256, artifact_kind)
) WITHOUT ROWID
""".strip()
_STATE_IDENTITY_TABLE_SQL: Final = """
CREATE TABLE authority_state_identity_v1 (
    singleton INTEGER PRIMARY KEY NOT NULL CHECK (singleton = 1),
    path_sha256 TEXT NOT NULL,
    directory_device INTEGER NOT NULL CHECK (directory_device >= 0),
    directory_inode INTEGER NOT NULL CHECK (directory_inode > 0),
    database_device INTEGER NOT NULL CHECK (database_device >= 0),
    database_inode INTEGER NOT NULL CHECK (database_inode > 0)
) WITHOUT ROWID
""".strip()


class AuthorityStateError(RuntimeError):
    """Base class for content-free authority-state failures."""


class AuthorityStateSecurityError(AuthorityStateError):
    """The state directory, database, or SQLite sidecar is unsafe."""


class AuthorityStateIntegrityError(AuthorityStateError):
    """The state schema or a persisted binding is invalid."""


class AuthorityStateBusyError(AuthorityStateError):
    """SQLite could not obtain its bounded transaction lock."""


class AuthorityReplayError(AuthorityStateError):
    """A nonce is already bound to a different execution."""


class AuthorityClaimNotAllowedError(AuthorityStateError):
    """The caller prohibited creation of a previously unseen nonce claim."""


class JournalAnchorError(AuthorityStateError):
    """A journal cannot be reconciled with its durable anchor."""


class ResourceLedgerAnchorError(AuthorityStateError):
    """A resource ledger cannot be reconciled with its durable anchor."""


class FilesystemIdentitySealError(AuthorityStateError):
    """A durable file or its containing directory changed identity."""


@dataclass(frozen=True, slots=True)
class NonceClaim:
    result: str
    nonce_sha256: str
    operation_sha256: str
    execution_sha256: str
    authorization_sha256: str
    scope_sha256: str
    trust_bundle_sha256: str
    claim_sha256: str


@dataclass(frozen=True, slots=True)
class JournalAnchor:
    result: str
    binding_sha256: str
    sequence: int
    head_sha256: str


@dataclass(frozen=True, slots=True)
class ResourceLedgerAnchor:
    result: str
    binding_sha256: str
    sequence: int
    head_sha256: str


@dataclass(frozen=True, slots=True)
class FilesystemIdentitySeal:
    result: str
    binding_sha256: str
    artifact_kind: str
    path_sha256: str
    directory_device: int
    directory_inode: int
    file_device: int
    file_inode: int


def _require_hash(value: object, code: str) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise AuthorityStateIntegrityError(code)
    return value


def _require_held_lock(
    held_lock: HeldExecutionLockCapability,
    *,
    code: str,
) -> None:
    try:
        validate_held_execution_lock(held_lock)
    except ExecutionLockError as error:
        raise AuthorityStateSecurityError(code) from error


def _domain_hash(domain: bytes, value: str) -> str:
    return hashlib.sha256(domain + value.encode("utf-8")).hexdigest()


def nonce_sha256(nonce: str) -> str:
    """Return the domain-separated digest used as the sole nonce identity."""

    if (
        not isinstance(nonce, str)
        or not MIN_NONCE_CHARACTERS <= len(nonce) <= MAX_NONCE_CHARACTERS
        or "\x00" in nonce
    ):
        raise AuthorityStateIntegrityError("authority_nonce_invalid")
    try:
        nonce.encode("utf-8")
    except UnicodeError as error:
        raise AuthorityStateIntegrityError("authority_nonce_invalid") from error
    return _domain_hash(_NONCE_DOMAIN, nonce)


def operation_sha256(operation: str) -> str:
    if not isinstance(operation, str) or OPERATION_RE.fullmatch(operation) is None:
        raise AuthorityStateIntegrityError("authority_operation_invalid")
    return _domain_hash(_OPERATION_DOMAIN, operation)


def _claim_sha256(
    *,
    nonce_hash: str,
    operation_hash: str,
    execution_hash: str,
    authorization_hash: str,
    scope_hash: str,
    trust_bundle_hash: str,
) -> str:
    material = b"\x00".join(
        item.encode("ascii")
        for item in (
            nonce_hash,
            operation_hash,
            execution_hash,
            authorization_hash,
            scope_hash,
            trust_bundle_hash,
        )
    )
    return hashlib.sha256(_CLAIM_DOMAIN + material).hexdigest()


class AuthorityState:
    """SQLite-backed nonce ledger and hash-chain anchor.

    The parent directory must be a non-symlink directory owned by
    ``expected_uid`` with mode ``0700``.  The database must be a one-link,
    regular ``0600`` file owned by the same UID.  Passing the current effective
    UID makes these production-style invariants usable in unprivileged tests.
    """

    def __init__(
        self,
        path: Path,
        *,
        expected_uid: int | None = None,
        busy_timeout_seconds: float = 5.0,
        create: bool = False,
    ) -> None:
        self.path = Path(path)
        self.expected_uid = os.geteuid() if expected_uid is None else expected_uid
        if (
            not isinstance(self.expected_uid, int)
            or isinstance(self.expected_uid, bool)
            or self.expected_uid < 0
        ):
            raise AuthorityStateSecurityError("authority_state_uid_invalid")
        if (
            not isinstance(busy_timeout_seconds, (int, float))
            or isinstance(busy_timeout_seconds, bool)
            or not 0 < float(busy_timeout_seconds) <= 30
        ):
            raise AuthorityStateSecurityError("authority_state_timeout_invalid")
        if (
            not self.path.is_absolute()
            or not self.path.name
            or self.path.name in {".", ".."}
        ):
            raise AuthorityStateSecurityError("authority_state_path_invalid")
        if not isinstance(create, bool):
            raise AuthorityStateSecurityError("authority_state_create_invalid")
        self._busy_timeout_ms = int(float(busy_timeout_seconds) * 1000)
        self._directory_inode: tuple[int, int] | None = None
        self._database_inode: tuple[int, int] | None = None
        self._schema_ready = False
        self._created = self._prepare_path(create=create)
        self._initialize_schema()

    @staticmethod
    def _nofollow_flag() -> int:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow == 0:
            raise AuthorityStateSecurityError(
                "authority_state_nofollow_unavailable"
            )
        return nofollow

    def _open_validated_directory(self) -> int:
        flags = os.O_RDONLY | os.O_CLOEXEC | self._nofollow_flag()
        flags |= getattr(os, "O_DIRECTORY", 0)
        try:
            directory_fd = os.open(self.path.parent, flags)
        except OSError as error:
            raise AuthorityStateSecurityError(
                "authority_state_directory_invalid"
            ) from error
        try:
            opened = os.fstat(directory_fd)
            named = self.path.parent.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o700
                or opened.st_uid != self.expected_uid
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise AuthorityStateSecurityError(
                    "authority_state_directory_invalid"
                )
            inode = (opened.st_dev, opened.st_ino)
            if self._directory_inode is not None and inode != self._directory_inode:
                raise AuthorityStateSecurityError(
                    "authority_state_directory_replaced"
                )
            self._directory_inode = inode
            return directory_fd
        except BaseException:
            os.close(directory_fd)
            raise

    def _prepare_path(self, *, create: bool) -> bool:
        directory_fd = self._open_validated_directory()
        database_fd = -1
        flags = os.O_RDWR | os.O_CLOEXEC | self._nofollow_flag()
        try:
            if create:
                database_fd = os.open(
                    self.path.name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
                os.fchmod(database_fd, 0o600)
                os.fsync(database_fd)
                os.fsync(directory_fd)
            else:
                database_fd = os.open(
                    self.path.name,
                    flags,
                    dir_fd=directory_fd,
                )
            self._validate_database_fd(database_fd, directory_fd)
        except OSError as error:
            raise AuthorityStateSecurityError(
                "authority_state_file_invalid"
            ) from error
        finally:
            if database_fd >= 0:
                os.close(database_fd)
            os.close(directory_fd)
        return create

    def _validate_database_fd(self, database_fd: int, directory_fd: int) -> None:
        try:
            opened = os.fstat(database_fd)
            named = os.stat(
                self.path.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise AuthorityStateSecurityError(
                "authority_state_file_invalid"
            ) from error
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != self.expected_uid
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise AuthorityStateSecurityError("authority_state_file_invalid")
        inode = (opened.st_dev, opened.st_ino)
        if self._database_inode is not None and inode != self._database_inode:
            raise AuthorityStateSecurityError("authority_state_file_replaced")
        self._database_inode = inode

    def _validate_path(self) -> None:
        directory_fd = self._open_validated_directory()
        database_fd = -1
        try:
            database_fd = os.open(
                self.path.name,
                os.O_RDWR | os.O_CLOEXEC | self._nofollow_flag(),
                dir_fd=directory_fd,
            )
            self._validate_database_fd(database_fd, directory_fd)
            self._validate_sidecars(directory_fd)
        except OSError as error:
            raise AuthorityStateSecurityError(
                "authority_state_file_invalid"
            ) from error
        finally:
            if database_fd >= 0:
                os.close(database_fd)
            os.close(directory_fd)

    def _validate_sidecars(self, directory_fd: int) -> None:
        for suffix in ("-wal", "-shm"):
            try:
                os.stat(
                    self.path.name + suffix,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except OSError as error:
                raise AuthorityStateSecurityError(
                    "authority_state_sidecar_invalid"
                ) from error
            raise AuthorityStateSecurityError("authority_state_wal_forbidden")

        try:
            journal = os.stat(
                self.path.name + "-journal",
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        except OSError as error:
            raise AuthorityStateSecurityError(
                "authority_state_sidecar_invalid"
            ) from error
        if (
            not stat.S_ISREG(journal.st_mode)
            or stat.S_IMODE(journal.st_mode) != 0o600
            or journal.st_uid != self.expected_uid
            or journal.st_nlink != 1
        ):
            raise AuthorityStateSecurityError("authority_state_sidecar_invalid")

    def _connect(self) -> sqlite3.Connection:
        self._validate_path()
        try:
            connection = sqlite3.connect(
                str(self.path),
                timeout=self._busy_timeout_ms / 1000,
                isolation_level=None,
            )
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA fullfsync = ON")
            if mode is None or str(mode[0]).lower() != "delete":
                raise AuthorityStateIntegrityError(
                    "authority_state_journal_mode_invalid"
                )
            synchronous = connection.execute("PRAGMA synchronous").fetchone()
            if synchronous is None or synchronous[0] != 2:
                raise AuthorityStateIntegrityError(
                    "authority_state_sync_mode_invalid"
                )
            if self._schema_ready:
                application_id = connection.execute(
                    "PRAGMA application_id"
                ).fetchone()[0]
                user_version = connection.execute("PRAGMA user_version").fetchone()[0]
                if (
                    application_id != STATE_APPLICATION_ID
                    or user_version != STATE_SCHEMA_VERSION
                ):
                    raise AuthorityStateIntegrityError(
                        "authority_state_schema_identity_invalid"
                    )
                self._validate_schema(connection)
            return connection
        except AuthorityStateError:
            if "connection" in locals():
                connection.close()
            raise
        except sqlite3.OperationalError as error:
            if "connection" in locals():
                connection.close()
            raise AuthorityStateBusyError("authority_state_busy") from error
        except sqlite3.DatabaseError as error:
            if "connection" in locals():
                connection.close()
            raise AuthorityStateIntegrityError(
                "authority_state_database_invalid"
            ) from error

    @staticmethod
    def _begin_immediate(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            raise AuthorityStateBusyError("authority_state_busy") from error

    def _initialize_schema(self) -> None:
        connection = self._connect()
        try:
            self._begin_immediate(connection)
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            objects = connection.execute(
                """
                SELECT type, name
                FROM sqlite_schema
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall()
            if (
                self._created
                and application_id == 0
                and user_version == 0
                and not objects
            ):
                if self._directory_inode is None or self._database_inode is None:
                    raise AuthorityStateSecurityError(
                        "authority_state_identity_unavailable"
                    )
                identity_values = (
                    1,
                    hashlib.sha256(str(self.path).encode("utf-8")).hexdigest(),
                    self._directory_inode[0],
                    self._directory_inode[1],
                    self._database_inode[0],
                    self._database_inode[1],
                )
                connection.execute(_FILESYSTEM_IDENTITY_TABLE_SQL)
                connection.execute(_NONCE_TABLE_SQL)
                connection.execute(_ANCHOR_TABLE_SQL)
                connection.execute(_RESOURCE_ANCHOR_TABLE_SQL)
                connection.execute(_STATE_IDENTITY_TABLE_SQL)
                connection.execute(
                    """
                    INSERT INTO authority_state_identity_v1 (
                        singleton, path_sha256,
                        directory_device, directory_inode,
                        database_device, database_inode
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    identity_values,
                )
            elif (
                application_id != STATE_APPLICATION_ID
                or user_version != STATE_SCHEMA_VERSION
                or objects
                != [
                    ("table", "authority_state_identity_v1"),
                    ("table", "filesystem_identity_seal_v1"),
                    ("table", "journal_anchor_v1"),
                    ("table", "nonce_claim_v1"),
                    ("table", "resource_ledger_anchor_v1"),
                ]
            ):
                raise AuthorityStateIntegrityError(
                    "authority_state_schema_identity_invalid"
                )
            connection.execute(f"PRAGMA application_id = {STATE_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {STATE_SCHEMA_VERSION}")
            self._validate_schema(connection)
            connection.commit()
        except AuthorityStateError:
            connection.rollback()
            raise
        except sqlite3.OperationalError as error:
            connection.rollback()
            raise AuthorityStateBusyError("authority_state_busy") from error
        except sqlite3.DatabaseError as error:
            connection.rollback()
            raise AuthorityStateIntegrityError(
                "authority_state_database_invalid"
            ) from error
        finally:
            connection.close()
        self._schema_ready = True
        self._validate_path()

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT type, name, sql
            FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        expected_rows = [
            (
                "table",
                "authority_state_identity_v1",
                _STATE_IDENTITY_TABLE_SQL,
            ),
            (
                "table",
                "filesystem_identity_seal_v1",
                _FILESYSTEM_IDENTITY_TABLE_SQL,
            ),
            ("table", "journal_anchor_v1", _ANCHOR_TABLE_SQL),
            ("table", "nonce_claim_v1", _NONCE_TABLE_SQL),
            (
                "table",
                "resource_ledger_anchor_v1",
                _RESOURCE_ANCHOR_TABLE_SQL,
            ),
        ]
        if len(rows) != len(expected_rows) or any(
            actual_type != expected_type
            or actual_name != expected_name
            or not isinstance(actual_sql, str)
            or " ".join(actual_sql.split()) != " ".join(expected_sql.split())
            for (
                actual_type,
                actual_name,
                actual_sql,
            ), (
                expected_type,
                expected_name,
                expected_sql,
            ) in zip(rows, expected_rows, strict=True)
        ):
            raise AuthorityStateIntegrityError("authority_state_schema_invalid")
        for table, expected in (
            ("nonce_claim_v1", _NONCE_COLUMNS),
            ("journal_anchor_v1", _ANCHOR_COLUMNS),
            ("resource_ledger_anchor_v1", _RESOURCE_ANCHOR_COLUMNS),
            ("filesystem_identity_seal_v1", _FILESYSTEM_IDENTITY_COLUMNS),
            ("authority_state_identity_v1", _STATE_IDENTITY_COLUMNS),
        ):
            columns = tuple(
                row[1]
                for row in connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            )
            if columns != expected:
                raise AuthorityStateIntegrityError(
                    "authority_state_schema_invalid"
                )
        integrity = connection.execute("PRAGMA quick_check").fetchall()
        if integrity != [("ok",)]:
            raise AuthorityStateIntegrityError("authority_state_integrity_invalid")
        state_identity_rows = connection.execute(
            """
            SELECT singleton, path_sha256,
                   directory_device, directory_inode,
                   database_device, database_inode
            FROM authority_state_identity_v1
            """
        ).fetchall()
        if self._directory_inode is None or self._database_inode is None:
            raise AuthorityStateSecurityError(
                "authority_state_identity_unavailable"
            )
        expected_state_identity = (
            1,
            hashlib.sha256(str(self.path).encode("utf-8")).hexdigest(),
            self._directory_inode[0],
            self._directory_inode[1],
            self._database_inode[0],
            self._database_inode[1],
        )
        if state_identity_rows != [expected_state_identity]:
            raise AuthorityStateSecurityError(
                "authority_state_cross_process_identity_mismatch"
            )
        nonce_rows = connection.execute(
            """
            SELECT nonce_sha256, operation_sha256, execution_sha256,
                   authorization_sha256, scope_sha256,
                   trust_bundle_sha256, claim_sha256
            FROM nonce_claim_v1
            """
        ).fetchall()
        for row in nonce_rows:
            if (
                len(row) != len(_NONCE_COLUMNS)
                or any(
                    not isinstance(value, str)
                    or HASH_RE.fullmatch(value) is None
                    for value in row
                )
                or row[-1]
                != _claim_sha256(
                    nonce_hash=row[0],
                    operation_hash=row[1],
                    execution_hash=row[2],
                    authorization_hash=row[3],
                    scope_hash=row[4],
                    trust_bundle_hash=row[5],
                )
            ):
                raise AuthorityStateIntegrityError(
                    "authority_state_nonce_row_invalid"
                )
        anchor_rows = connection.execute(
            """
            SELECT binding_sha256, sequence, head_sha256
            FROM journal_anchor_v1
            """
        ).fetchall()
        for binding, sequence, head in anchor_rows:
            if (
                not isinstance(binding, str)
                or HASH_RE.fullmatch(binding) is None
                or not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence < 1
                or not isinstance(head, str)
                or HASH_RE.fullmatch(head) is None
            ):
                raise AuthorityStateIntegrityError(
                    "authority_state_anchor_row_invalid"
                )
        resource_anchor_rows = connection.execute(
            """
            SELECT binding_sha256, sequence, head_sha256
            FROM resource_ledger_anchor_v1
            """
        ).fetchall()
        for binding, sequence, head in resource_anchor_rows:
            if (
                not isinstance(binding, str)
                or HASH_RE.fullmatch(binding) is None
                or not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence < 1
                or not isinstance(head, str)
                or HASH_RE.fullmatch(head) is None
            ):
                raise AuthorityStateIntegrityError(
                    "authority_state_resource_anchor_row_invalid"
                )
        identity_rows = connection.execute(
            """
            SELECT binding_sha256, artifact_kind, path_sha256,
                   directory_device, directory_inode,
                   file_device, file_inode
            FROM filesystem_identity_seal_v1
            """
        ).fetchall()
        for row in identity_rows:
            if (
                len(row) != len(_FILESYSTEM_IDENTITY_COLUMNS)
                or not isinstance(row[0], str)
                or HASH_RE.fullmatch(row[0]) is None
                or row[1] not in {"journal", "resource_ledger"}
                or not isinstance(row[2], str)
                or HASH_RE.fullmatch(row[2]) is None
                or any(
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                    for value in (row[3], row[5])
                )
                or any(
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value <= 0
                    for value in (row[4], row[6])
                )
            ):
                raise AuthorityStateIntegrityError(
                    "authority_state_filesystem_identity_row_invalid"
                )

    @staticmethod
    def _finish(connection: sqlite3.Connection) -> None:
        try:
            connection.commit()
        except sqlite3.OperationalError as error:
            connection.rollback()
            raise AuthorityStateBusyError("authority_state_busy") from error
        except sqlite3.DatabaseError as error:
            connection.rollback()
            raise AuthorityStateIntegrityError(
                "authority_state_database_invalid"
            ) from error

    def claim_nonce(
        self,
        nonce: str,
        *,
        operation: str,
        execution_sha256: str,
        authorization_sha256: str,
        scope_sha256: str,
        trust_bundle_sha256: str,
        held_lock: HeldExecutionLockCapability,
        allow_new_claim: bool = True,
    ) -> NonceClaim:
        """Atomically claim a nonce or resume its exact execution binding."""

        _require_held_lock(
            held_lock,
            code="authority_nonce_claim_lock_not_held",
        )
        if not isinstance(allow_new_claim, bool):
            raise AuthorityStateIntegrityError(
                "authority_new_claim_policy_invalid"
            )

        nonce_hash = nonce_sha256(nonce)
        operation_hash = operation_sha256(operation)
        execution_hash = _require_hash(
            execution_sha256,
            "authority_execution_hash_invalid",
        )
        authorization_hash = _require_hash(
            authorization_sha256,
            "authority_authorization_hash_invalid",
        )
        scope_hash = _require_hash(scope_sha256, "authority_scope_hash_invalid")
        trust_bundle_hash = _require_hash(
            trust_bundle_sha256,
            "authority_trust_bundle_hash_invalid",
        )
        claim_hash = _claim_sha256(
            nonce_hash=nonce_hash,
            operation_hash=operation_hash,
            execution_hash=execution_hash,
            authorization_hash=authorization_hash,
            scope_hash=scope_hash,
            trust_bundle_hash=trust_bundle_hash,
        )
        expected = (
            nonce_hash,
            operation_hash,
            execution_hash,
            authorization_hash,
            scope_hash,
            trust_bundle_hash,
            claim_hash,
        )

        connection = self._connect()
        try:
            self._begin_immediate(connection)
            row = connection.execute(
                """
                SELECT nonce_sha256, operation_sha256, execution_sha256,
                       authorization_sha256, scope_sha256,
                       trust_bundle_sha256, claim_sha256
                FROM nonce_claim_v1
                WHERE nonce_sha256 = ?
                """,
                (nonce_hash,),
            ).fetchone()
            if row is None:
                if not allow_new_claim:
                    raise AuthorityClaimNotAllowedError(
                        "authority_new_claim_not_allowed"
                    )
                connection.execute(
                    """
                    INSERT INTO nonce_claim_v1 (
                        nonce_sha256, operation_sha256, execution_sha256,
                        authorization_sha256, scope_sha256,
                        trust_bundle_sha256, claim_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    expected,
                )
                result = "nonce_claimed"
            elif tuple(row) == expected:
                result = "exact_execution_resumed"
            else:
                raise AuthorityReplayError("authority_nonce_replayed")
            _require_held_lock(
                held_lock,
                code="authority_nonce_claim_lock_not_held",
            )
            self._finish(connection)
        except AuthorityStateError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise AuthorityReplayError("authority_nonce_replayed") from error
        except sqlite3.OperationalError as error:
            connection.rollback()
            raise AuthorityStateBusyError("authority_state_busy") from error
        except sqlite3.DatabaseError as error:
            connection.rollback()
            raise AuthorityStateIntegrityError(
                "authority_state_database_invalid"
            ) from error
        finally:
            connection.close()
        _require_held_lock(
            held_lock,
            code="authority_nonce_claim_lock_not_held",
        )
        self._validate_path()
        return NonceClaim(result=result, **dict(zip(_NONCE_COLUMNS, expected)))

    def read_anchor(self, binding_sha256: str) -> JournalAnchor:
        binding = _require_hash(binding_sha256, "journal_anchor_binding_invalid")
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT sequence, head_sha256
                FROM journal_anchor_v1
                WHERE binding_sha256 = ?
                """,
                (binding,),
            ).fetchone()
        except sqlite3.OperationalError as error:
            raise AuthorityStateBusyError("authority_state_busy") from error
        except sqlite3.DatabaseError as error:
            raise AuthorityStateIntegrityError(
                "authority_state_database_invalid"
            ) from error
        finally:
            connection.close()
        self._validate_path()
        if row is None:
            return JournalAnchor("anchor_empty", binding, 0, ZERO_HEAD)
        sequence, head = row
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            or not isinstance(head, str)
            or HASH_RE.fullmatch(head) is None
        ):
            raise AuthorityStateIntegrityError("journal_anchor_row_invalid")
        return JournalAnchor("anchor_current", binding, sequence, head)

    def seal_filesystem_identity(
        self,
        binding_sha256: str,
        *,
        artifact_kind: str,
        path: Path,
        directory_fd: int,
        file_fd: int,
        held_lock: HeldExecutionLockCapability,
    ) -> FilesystemIdentitySeal:
        """Persist and compare exact directory/file identity across processes.

        The caller must pass the already securely opened descriptors.  Their
        identities are compared with the named path before the content-free
        seal is committed.  Reopening a byte-identical replacement therefore
        refuses rather than silently establishing a new identity.
        """

        _require_held_lock(
            held_lock,
            code="filesystem_identity_lock_not_held",
        )
        binding = _require_hash(
            binding_sha256,
            "filesystem_identity_binding_invalid",
        )
        if artifact_kind not in {"journal", "resource_ledger"}:
            raise FilesystemIdentitySealError(
                "filesystem_identity_artifact_invalid"
            )
        target = Path(path)
        if (
            not target.is_absolute()
            or not target.name
            or target.name in {".", ".."}
            or type(directory_fd) is not int
            or type(file_fd) is not int
            or directory_fd < 0
            or file_fd < 0
        ):
            raise FilesystemIdentitySealError(
                "filesystem_identity_input_invalid"
            )
        try:
            opened_directory = os.fstat(directory_fd)
            named_directory = target.parent.stat(follow_symlinks=False)
            opened_file = os.fstat(file_fd)
            named_file = os.stat(
                target.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise FilesystemIdentitySealError(
                "filesystem_identity_path_invalid"
            ) from error
        if (
            not stat.S_ISDIR(opened_directory.st_mode)
            or not stat.S_ISDIR(named_directory.st_mode)
            or (opened_directory.st_dev, opened_directory.st_ino)
            != (named_directory.st_dev, named_directory.st_ino)
            or not stat.S_ISREG(opened_file.st_mode)
            or not stat.S_ISREG(named_file.st_mode)
            or opened_file.st_nlink != 1
            or (opened_file.st_dev, opened_file.st_ino)
            != (named_file.st_dev, named_file.st_ino)
        ):
            raise FilesystemIdentitySealError(
                "filesystem_identity_path_invalid"
            )
        identities = (
            opened_directory.st_dev,
            opened_directory.st_ino,
            opened_file.st_dev,
            opened_file.st_ino,
        )
        if any(
            type(value) is not int or value < 0 or value > 0x7FFF_FFFF_FFFF_FFFF
            for value in identities
        ) or opened_directory.st_ino == 0 or opened_file.st_ino == 0:
            raise FilesystemIdentitySealError(
                "filesystem_identity_value_invalid"
            )
        path_hash = hashlib.sha256(str(target).encode("utf-8")).hexdigest()
        expected = (
            binding,
            artifact_kind,
            path_hash,
            *identities,
        )
        _require_held_lock(
            held_lock,
            code="filesystem_identity_lock_not_held",
        )
        connection = self._connect()
        try:
            self._begin_immediate(connection)
            row = connection.execute(
                """
                SELECT binding_sha256, artifact_kind, path_sha256,
                       directory_device, directory_inode,
                       file_device, file_inode
                FROM filesystem_identity_seal_v1
                WHERE binding_sha256 = ? AND artifact_kind = ?
                """,
                (binding, artifact_kind),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO filesystem_identity_seal_v1 (
                        binding_sha256, artifact_kind, path_sha256,
                        directory_device, directory_inode,
                        file_device, file_inode
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    expected,
                )
                result = "filesystem_identity_sealed"
            elif tuple(row) == expected:
                result = "filesystem_identity_exact_resume"
            else:
                raise FilesystemIdentitySealError(
                    "filesystem_identity_replaced"
                )
            _require_held_lock(
                held_lock,
                code="filesystem_identity_lock_not_held",
            )
            self._finish(connection)
        except AuthorityStateError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise FilesystemIdentitySealError(
                "filesystem_identity_compare_failed"
            ) from error
        except sqlite3.OperationalError as error:
            connection.rollback()
            raise AuthorityStateBusyError("authority_state_busy") from error
        except sqlite3.DatabaseError as error:
            connection.rollback()
            raise AuthorityStateIntegrityError(
                "authority_state_database_invalid"
            ) from error
        finally:
            connection.close()
        _require_held_lock(
            held_lock,
            code="filesystem_identity_lock_not_held",
        )
        self._validate_path()
        return FilesystemIdentitySeal(result, *expected)

    def read_resource_ledger_anchor(
        self,
        binding_sha256: str,
        *,
        held_lock: HeldExecutionLockCapability,
    ) -> ResourceLedgerAnchor:
        _require_held_lock(
            held_lock,
            code="resource_ledger_anchor_lock_not_held",
        )
        binding = _require_hash(
            binding_sha256,
            "resource_ledger_anchor_binding_invalid",
        )
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT sequence, head_sha256
                FROM resource_ledger_anchor_v1
                WHERE binding_sha256 = ?
                """,
                (binding,),
            ).fetchone()
        except sqlite3.OperationalError as error:
            raise AuthorityStateBusyError("authority_state_busy") from error
        except sqlite3.DatabaseError as error:
            raise AuthorityStateIntegrityError(
                "authority_state_database_invalid"
            ) from error
        finally:
            connection.close()
        _require_held_lock(
            held_lock,
            code="resource_ledger_anchor_lock_not_held",
        )
        self._validate_path()
        if row is None:
            return ResourceLedgerAnchor(
                "resource_anchor_empty", binding, 0, ZERO_HEAD
            )
        sequence, head = row
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            or not isinstance(head, str)
            or HASH_RE.fullmatch(head) is None
        ):
            raise AuthorityStateIntegrityError(
                "resource_ledger_anchor_row_invalid"
            )
        return ResourceLedgerAnchor(
            "resource_anchor_current", binding, sequence, head
        )

    def advance_resource_ledger_anchor(
        self,
        binding_sha256: str,
        *,
        ledger_sequence: int,
        ledger_head_sha256: str,
        ledger_prior_head_sha256: str,
        held_lock: HeldExecutionLockCapability,
    ) -> ResourceLedgerAnchor:
        """CAS a resource-ledger head with one-entry crash repair only."""

        _require_held_lock(
            held_lock,
            code="resource_ledger_anchor_lock_not_held",
        )
        binding = _require_hash(
            binding_sha256,
            "resource_ledger_anchor_binding_invalid",
        )
        head = _require_hash(
            ledger_head_sha256,
            "resource_ledger_anchor_head_invalid",
        )
        prior = _require_hash(
            ledger_prior_head_sha256,
            "resource_ledger_anchor_prior_invalid",
        )
        if (
            not isinstance(ledger_sequence, int)
            or isinstance(ledger_sequence, bool)
            or ledger_sequence < 0
        ):
            raise ResourceLedgerAnchorError(
                "resource_ledger_anchor_sequence_invalid"
            )
        if ledger_sequence == 0 and (head != ZERO_HEAD or prior != ZERO_HEAD):
            raise ResourceLedgerAnchorError(
                "resource_ledger_anchor_zero_state_invalid"
            )
        connection = self._connect()
        try:
            self._begin_immediate(connection)
            row = connection.execute(
                """
                SELECT sequence, head_sha256
                FROM resource_ledger_anchor_v1
                WHERE binding_sha256 = ?
                """,
                (binding,),
            ).fetchone()
            current_sequence, current_head = (0, ZERO_HEAD) if row is None else row
            if (
                not isinstance(current_sequence, int)
                or isinstance(current_sequence, bool)
                or current_sequence < 0
                or not isinstance(current_head, str)
                or HASH_RE.fullmatch(current_head) is None
            ):
                raise AuthorityStateIntegrityError(
                    "resource_ledger_anchor_row_invalid"
                )
            if ledger_sequence < current_sequence:
                raise ResourceLedgerAnchorError(
                    "resource_ledger_anchor_ahead_of_file"
                )
            if ledger_sequence == current_sequence:
                if head != current_head:
                    raise ResourceLedgerAnchorError(
                        "resource_ledger_anchor_head_mismatch"
                    )
                result = "resource_anchor_exact_resume"
            elif ledger_sequence > current_sequence + 1:
                raise ResourceLedgerAnchorError(
                    "resource_ledger_anchor_suffix_too_long"
                )
            else:
                if prior != current_head:
                    raise ResourceLedgerAnchorError(
                        "resource_ledger_anchor_compare_failed"
                    )
                if head == current_head:
                    raise ResourceLedgerAnchorError(
                        "resource_ledger_anchor_head_not_advanced"
                    )
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO resource_ledger_anchor_v1 (
                            binding_sha256, sequence, head_sha256
                        ) VALUES (?, ?, ?)
                        """,
                        (binding, ledger_sequence, head),
                    )
                else:
                    changed = connection.execute(
                        """
                        UPDATE resource_ledger_anchor_v1
                        SET sequence = ?, head_sha256 = ?
                        WHERE binding_sha256 = ?
                          AND sequence = ?
                          AND head_sha256 = ?
                        """,
                        (
                            ledger_sequence,
                            head,
                            binding,
                            current_sequence,
                            current_head,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise ResourceLedgerAnchorError(
                            "resource_ledger_anchor_compare_failed"
                        )
                result = "resource_anchor_advanced_one_entry"
            _require_held_lock(
                held_lock,
                code="resource_ledger_anchor_lock_not_held",
            )
            self._finish(connection)
        except AuthorityStateError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise ResourceLedgerAnchorError(
                "resource_ledger_anchor_compare_failed"
            ) from error
        except sqlite3.OperationalError as error:
            connection.rollback()
            raise AuthorityStateBusyError("authority_state_busy") from error
        except sqlite3.DatabaseError as error:
            connection.rollback()
            raise AuthorityStateIntegrityError(
                "authority_state_database_invalid"
            ) from error
        finally:
            connection.close()
        _require_held_lock(
            held_lock,
            code="resource_ledger_anchor_lock_not_held",
        )
        self._validate_path()
        return ResourceLedgerAnchor(result, binding, ledger_sequence, head)

    def advance_anchor(
        self,
        binding_sha256: str,
        *,
        journal_sequence: int,
        journal_head_sha256: str,
        journal_prior_head_sha256: str,
        held_lock: HeldExecutionLockCapability,
    ) -> JournalAnchor:
        """Compare-and-set an anchor, allowing at most one journal entry.

        Equality is an idempotent resume.  A journal behind the anchor is
        refused, as is any unanchored suffix longer than one entry.  The one
        accepted recovery entry must name the current anchor as its exact
        predecessor.
        """

        _require_held_lock(
            held_lock,
            code="journal_anchor_lock_not_held",
        )
        binding = _require_hash(binding_sha256, "journal_anchor_binding_invalid")
        head = _require_hash(journal_head_sha256, "journal_anchor_head_invalid")
        prior = _require_hash(
            journal_prior_head_sha256,
            "journal_anchor_prior_invalid",
        )
        if (
            not isinstance(journal_sequence, int)
            or isinstance(journal_sequence, bool)
            or journal_sequence < 0
        ):
            raise JournalAnchorError("journal_anchor_sequence_invalid")
        if journal_sequence == 0 and (head != ZERO_HEAD or prior != ZERO_HEAD):
            raise JournalAnchorError("journal_anchor_zero_state_invalid")

        connection = self._connect()
        try:
            self._begin_immediate(connection)
            row = connection.execute(
                """
                SELECT sequence, head_sha256
                FROM journal_anchor_v1
                WHERE binding_sha256 = ?
                """,
                (binding,),
            ).fetchone()
            current_sequence, current_head = (0, ZERO_HEAD) if row is None else row
            if (
                not isinstance(current_sequence, int)
                or isinstance(current_sequence, bool)
                or current_sequence < 0
                or not isinstance(current_head, str)
                or HASH_RE.fullmatch(current_head) is None
            ):
                raise AuthorityStateIntegrityError("journal_anchor_row_invalid")

            if journal_sequence < current_sequence:
                raise JournalAnchorError("journal_anchor_ahead_of_journal")
            if journal_sequence == current_sequence:
                if head != current_head:
                    raise JournalAnchorError("journal_anchor_head_mismatch")
                result = "anchor_exact_resume"
            elif journal_sequence > current_sequence + 1:
                raise JournalAnchorError("journal_anchor_suffix_too_long")
            else:
                if prior != current_head:
                    raise JournalAnchorError("journal_anchor_compare_failed")
                if head == current_head:
                    raise JournalAnchorError("journal_anchor_head_not_advanced")
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO journal_anchor_v1 (
                            binding_sha256, sequence, head_sha256
                        ) VALUES (?, ?, ?)
                        """,
                        (binding, journal_sequence, head),
                    )
                else:
                    changed = connection.execute(
                        """
                        UPDATE journal_anchor_v1
                        SET sequence = ?, head_sha256 = ?
                        WHERE binding_sha256 = ?
                          AND sequence = ?
                          AND head_sha256 = ?
                        """,
                        (
                            journal_sequence,
                            head,
                            binding,
                            current_sequence,
                            current_head,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise JournalAnchorError("journal_anchor_compare_failed")
                result = "anchor_advanced_one_entry"
            _require_held_lock(
                held_lock,
                code="journal_anchor_lock_not_held",
            )
            self._finish(connection)
        except AuthorityStateError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise JournalAnchorError("journal_anchor_compare_failed") from error
        except sqlite3.OperationalError as error:
            connection.rollback()
            raise AuthorityStateBusyError("authority_state_busy") from error
        except sqlite3.DatabaseError as error:
            connection.rollback()
            raise AuthorityStateIntegrityError(
                "authority_state_database_invalid"
            ) from error
        finally:
            connection.close()
        _require_held_lock(
            held_lock,
            code="journal_anchor_lock_not_held",
        )
        self._validate_path()
        return JournalAnchor(result, binding, journal_sequence, head)

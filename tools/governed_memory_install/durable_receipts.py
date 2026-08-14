from __future__ import annotations

"""Create-once durable receipts at the controller's exact execution paths.

The production surface accepts only an execution identifier and one of the
closed receipt kinds.  It has no caller-selected filename, command,
endpoint, environment, or secret surface.  Tests may use ``synthetic`` to
exercise the same descriptor-level implementation below a temporary root.
"""

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from types import MappingProxyType
from typing import Final, Mapping, Protocol

from .receipts import (
    ReceiptError,
    canonical_json_bytes,
    verify_empty_rollback_receipt,
    verify_install_receipt,
)
from .rollback import (
    EmptyRollbackError,
    verify_empty_rollback_eligibility_receipt,
)


PRODUCTION_EXECUTIONS_ROOT: Final = Path(
    "/var/lib/governed-memory-controller/executions-v5"
)
MAX_DURABLE_RECEIPT_BYTES: Final = 64 * 1024
_EXECUTION_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class DurableReceiptError(RuntimeError):
    """Content-free refusal for receipt path, metadata, or bytes drift."""


class ReceiptArtifact(str, Enum):
    INSTALL = "install_receipt"
    EMPTY_ROLLBACK_ELIGIBILITY = "empty_rollback_eligibility_receipt"
    EMPTY_ROLLBACK = "empty_rollback_receipt"
    POSTGRES_INSTALL_I11 = "postgres_native_install_i11_receipt"
    POSTGRES_INSTALL_I12 = "postgres_native_install_i12_receipt"
    POSTGRES_INSTALL_I13 = "postgres_native_install_i13_receipt"
    POSTGRES_INSTALL_I14 = "postgres_native_install_i14_receipt"
    POSTGRES_ROLLBACK_I14 = "postgres_native_rollback_i14_receipt"
    POSTGRES_ROLLBACK_I13 = "postgres_native_rollback_i13_receipt"
    POSTGRES_ROLLBACK_I12 = "postgres_native_rollback_i12_receipt"
    POSTGRES_ROLLBACK_I11 = "postgres_native_rollback_i11_receipt"


_FILENAMES: Final = {
    ReceiptArtifact.INSTALL: "install-receipt.json",
    ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY: (
        "empty-rollback-eligibility-receipt.json"
    ),
    ReceiptArtifact.EMPTY_ROLLBACK: "empty-rollback-receipt.json",
    ReceiptArtifact.POSTGRES_INSTALL_I11: "postgres-native-install-i11-receipt.json",
    ReceiptArtifact.POSTGRES_INSTALL_I12: "postgres-native-install-i12-receipt.json",
    ReceiptArtifact.POSTGRES_INSTALL_I13: "postgres-native-install-i13-receipt.json",
    ReceiptArtifact.POSTGRES_INSTALL_I14: "postgres-native-install-i14-receipt.json",
    ReceiptArtifact.POSTGRES_ROLLBACK_I14: "postgres-native-rollback-i14-receipt.json",
    ReceiptArtifact.POSTGRES_ROLLBACK_I13: "postgres-native-rollback-i13-receipt.json",
    ReceiptArtifact.POSTGRES_ROLLBACK_I12: "postgres-native-rollback-i12-receipt.json",
    ReceiptArtifact.POSTGRES_ROLLBACK_I11: "postgres-native-rollback-i11-receipt.json",
}

_POSTGRES_NATIVE_EXPECTATIONS: Final = {
    ReceiptArtifact.POSTGRES_INSTALL_I11: (
        "install",
        "roles_privacy_preflight_complete",
    ),
    ReceiptArtifact.POSTGRES_INSTALL_I12: ("install", "foundation_0001_applied"),
    ReceiptArtifact.POSTGRES_INSTALL_I13: (
        "install",
        "owner_claim_detail_0003_applied",
    ),
    ReceiptArtifact.POSTGRES_INSTALL_I14: (
        "install",
        "ready_for_terminal_catalog",
    ),
    ReceiptArtifact.POSTGRES_ROLLBACK_I14: (
        "rollback",
        "installed_0001_0003",
    ),
    ReceiptArtifact.POSTGRES_ROLLBACK_I13: ("rollback", "installed_0001"),
    ReceiptArtifact.POSTGRES_ROLLBACK_I12: (
        "rollback",
        "exact_empty_canonical_database_prefix",
    ),
    ReceiptArtifact.POSTGRES_ROLLBACK_I11: ("rollback", "empty"),
}
_POSTGRES_NATIVE_WRAPPER_KEYS: Final = {
    "schema_version",
    "execution_id",
    "native_receipt",
    "receipt_sha256",
}
_POSTGRES_NATIVE_RECEIPT_KEYS: Final = {
    "schema_version",
    "mode",
    "final_state",
    "source_closure_sha256",
    "runtime_receipt_sha256",
    "driver_runtime_identity_sha256",
    "terminal_catalog_sha256",
    "rollback_empty_proof_sha256",
    "operations",
    "receipt_sha256",
}

_PRODUCTION_STORE_MODE: Final = object()
_SYNTHETIC_STORE_MODE: Final = object()


@dataclass(frozen=True, slots=True)
class DurableReceiptEvidence:
    artifact: ReceiptArtifact
    execution_id: str
    path: str
    path_sha256: str
    canonical_file_sha256: str
    receipt_sha256: str
    canonical_receipt: Mapping[str, object]

    def __post_init__(self) -> None:
        if (
            type(self.artifact) is not ReceiptArtifact
            or _EXECUTION_RE.fullmatch(self.execution_id) is None
            or not Path(self.path).is_absolute()
            or any(
                re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in (
                    self.path_sha256,
                    self.canonical_file_sha256,
                    self.receipt_sha256,
                )
            )
            or not isinstance(self.canonical_receipt, Mapping)
        ):
            raise DurableReceiptError("durable_receipt_evidence_invalid")


class EmptyRollbackReceiptSink(Protocol):
    """Controller hook that must run while the physical fence is held."""

    def persist(
        self,
        execution_id: str,
        receipt: Mapping[str, object],
    ) -> DurableReceiptEvidence: ...


def _nofollow() -> int:
    value = getattr(os, "O_NOFOLLOW", 0)
    if value == 0:
        raise DurableReceiptError("durable_receipt_nofollow_unavailable")
    return value


def _validate_execution_id(execution_id: str) -> None:
    if type(execution_id) is not str or _EXECUTION_RE.fullmatch(execution_id) is None:
        raise DurableReceiptError("durable_receipt_execution_id_invalid")


def _validate_receipt(
    artifact: ReceiptArtifact,
    value: Mapping[str, object],
) -> dict[str, object]:
    if type(artifact) is not ReceiptArtifact or not isinstance(value, Mapping):
        raise DurableReceiptError("durable_receipt_kind_invalid")
    try:
        if artifact is ReceiptArtifact.INSTALL:
            return verify_install_receipt(value)
        if artifact is ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY:
            return verify_empty_rollback_eligibility_receipt(value)
        if artifact is ReceiptArtifact.EMPTY_ROLLBACK:
            return verify_empty_rollback_receipt(value)
        expected_native = _POSTGRES_NATIVE_EXPECTATIONS.get(artifact)
        if expected_native is not None:
            wrapper = dict(value)
            native = wrapper.get("native_receipt")
            if (
                set(wrapper) != _POSTGRES_NATIVE_WRAPPER_KEYS
                or wrapper.get("schema_version")
                != "governed-memory-postgres-native-stage-durable-receipt-v1"
                or _EXECUTION_RE.fullmatch(str(wrapper.get("execution_id")))
                is None
                or type(native) is not dict
                or set(native) != _POSTGRES_NATIVE_RECEIPT_KEYS
                or native.get("schema_version")
                != "governed-memory-postgres-native-stage-receipt-v1"
                or (native.get("mode"), native.get("final_state"))
                != expected_native
                or any(
                    _EXECUTION_RE.fullmatch(str(native.get(key))) is None
                    for key in (
                        "source_closure_sha256",
                        "runtime_receipt_sha256",
                        "driver_runtime_identity_sha256",
                    )
                )
                or type(native.get("operations")) is not list
                or any(
                    type(operation) is not str
                    or not operation
                    or len(operation) > 128
                    or any(character in operation for character in "\x00\r\n")
                    for operation in native.get("operations", [])
                )
            ):
                raise DurableReceiptError(
                    "durable_postgres_native_receipt_invalid"
                )
            terminal = native.get("terminal_catalog_sha256")
            empty = native.get("rollback_empty_proof_sha256")
            if expected_native[0] == "install":
                terminal_valid = (
                    terminal is None
                    if expected_native[1] != "ready_for_terminal_catalog"
                    else _EXECUTION_RE.fullmatch(str(terminal)) is not None
                )
                proof_valid = empty is None
            else:
                terminal_valid = terminal is None
                proof_valid = _EXECUTION_RE.fullmatch(str(empty)) is not None
            unsigned_native = {
                key: native[key]
                for key in native
                if key != "receipt_sha256"
            }
            native_sha256 = hashlib.sha256(
                canonical_json_bytes(unsigned_native)
            ).hexdigest()
            if (
                terminal_valid is not True
                or proof_valid is not True
                or native.get("receipt_sha256") != native_sha256
                or wrapper.get("receipt_sha256") != native_sha256
            ):
                raise DurableReceiptError(
                    "durable_postgres_native_receipt_invalid"
                )
            return wrapper
    except (ReceiptError, EmptyRollbackError) as error:
        raise DurableReceiptError("durable_receipt_content_invalid") from error
    raise DurableReceiptError("durable_receipt_kind_invalid")


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise DurableReceiptError("durable_receipt_duplicate_key")
        value[key] = item
    return value


def _parse_canonical(
    artifact: ReceiptArtifact,
    raw: bytes,
) -> dict[str, object]:
    if not 1 <= len(raw) <= MAX_DURABLE_RECEIPT_BYTES:
        raise DurableReceiptError("durable_receipt_size_invalid")
    try:
        document = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_strict_json_object,
            parse_constant=lambda unused: (_ for _ in ()).throw(
                DurableReceiptError("durable_receipt_json_invalid")
            ),
        )
    except DurableReceiptError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DurableReceiptError("durable_receipt_json_invalid") from error
    if type(document) is not dict or canonical_json_bytes(document) != raw:
        raise DurableReceiptError("durable_receipt_not_canonical")
    return _validate_receipt(artifact, document)


def _open_directory_nofollow(path: Path, *, expected_uid: int) -> int:
    """Open every absolute path component without following symlinks."""

    if not path.is_absolute() or any(part in {"..", "."} for part in path.parts):
        raise DurableReceiptError("durable_receipt_directory_invalid")
    flags = os.O_RDONLY | os.O_CLOEXEC | _nofollow()
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = -1
    try:
        descriptor = os.open("/", flags)
        for part in path.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            metadata = os.fstat(next_descriptor)
            mode = stat.S_IMODE(metadata.st_mode)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid not in {0, expected_uid}
                or mode & 0o022
            ):
                os.close(next_descriptor)
                raise DurableReceiptError(
                    "durable_receipt_directory_ancestry_invalid"
                )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except DurableReceiptError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise DurableReceiptError("durable_receipt_directory_invalid") from error


def _validate_directory(
    descriptor: int,
    path: Path,
    *,
    expected_uid: int,
) -> tuple[int, int]:
    try:
        opened = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
    except OSError as error:
        raise DurableReceiptError("durable_receipt_directory_invalid") from error
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o700
        or opened.st_uid != expected_uid
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise DurableReceiptError("durable_receipt_directory_invalid")
    return opened.st_dev, opened.st_ino


def _open_receipt(
    directory_fd: int,
    name: str,
    *,
    expected_uid: int,
) -> int:
    descriptor = _open_receipt_if_present(
        directory_fd,
        name,
        expected_uid=expected_uid,
    )
    if descriptor is None:
        raise DurableReceiptError("durable_receipt_file_absent")
    return descriptor


def _open_receipt_if_present(
    directory_fd: int,
    name: str,
    *,
    expected_uid: int,
) -> int | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | _nofollow(),
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        try:
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise DurableReceiptError("durable_receipt_file_invalid") from error
        raise DurableReceiptError("durable_receipt_file_appeared_during_read")
    except OSError as error:
        raise DurableReceiptError("durable_receipt_file_invalid") from error
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise DurableReceiptError("durable_receipt_file_invalid") from error
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_uid != expected_uid
        or opened.st_nlink != 1
        or opened.st_size > MAX_DURABLE_RECEIPT_BYTES
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        os.close(descriptor)
        raise DurableReceiptError("durable_receipt_file_invalid")
    return descriptor


def _read_stable(
    descriptor: int,
    directory_fd: int,
    name: str,
) -> bytes:
    before = os.fstat(descriptor)
    chunks: list[bytes] = []
    total = 0
    while True:
        block = os.read(
            descriptor,
            min(64 * 1024, MAX_DURABLE_RECEIPT_BYTES + 1 - total),
        )
        if not block:
            break
        chunks.append(block)
        total += len(block)
        if total > MAX_DURABLE_RECEIPT_BYTES:
            raise DurableReceiptError("durable_receipt_size_invalid")
    try:
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise DurableReceiptError("durable_receipt_file_invalid") from error
    if (
        total != before.st_size
        or (after.st_dev, after.st_ino, after.st_size)
        != (before.st_dev, before.st_ino, before.st_size)
        or (named.st_dev, named.st_ino, named.st_size)
        != (before.st_dev, before.st_ino, before.st_size)
    ):
        raise DurableReceiptError("durable_receipt_changed_during_read")
    return b"".join(chunks)


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise DurableReceiptError("durable_receipt_write_failed")
        view = view[written:]


class DurableReceiptStore:
    """Secure reader/writer for the exact per-execution receipts."""

    __slots__ = ("_expected_uid", "_mode", "_root")

    def __init__(
        self,
        executions_root: Path,
        *,
        expected_uid: int,
        _mode: object | None = None,
    ) -> None:
        root = Path(executions_root)
        if (
            not root.is_absolute()
            or type(expected_uid) is not int
            or expected_uid < 0
            or (
                _mode is not _PRODUCTION_STORE_MODE
                and _mode is not _SYNTHETIC_STORE_MODE
            )
            or (
                _mode is _PRODUCTION_STORE_MODE
                and (
                    root != PRODUCTION_EXECUTIONS_ROOT
                    or expected_uid != 0
                )
            )
        ):
            raise DurableReceiptError("durable_receipt_store_invalid")
        self._root = root
        self._expected_uid = expected_uid
        self._mode = _mode

    @classmethod
    def production(cls) -> DurableReceiptStore:
        return cls(
            PRODUCTION_EXECUTIONS_ROOT,
            expected_uid=0,
            _mode=_PRODUCTION_STORE_MODE,
        )

    @classmethod
    def synthetic(
        cls,
        executions_root: Path,
        *,
        expected_uid: int | None = None,
    ) -> DurableReceiptStore:
        uid = os.geteuid() if expected_uid is None else expected_uid
        return cls(
            executions_root,
            expected_uid=uid,
            _mode=_SYNTHETIC_STORE_MODE,
        )

    def require_production_binding(self) -> None:
        """Refuse unless this is the canonical root-owned production store."""

        if (
            type(self) is not DurableReceiptStore
            or self._mode is not _PRODUCTION_STORE_MODE
            or self._root != PRODUCTION_EXECUTIONS_ROOT
            or self._expected_uid != 0
        ):
            raise DurableReceiptError(
                "durable_receipt_store_not_production"
            )

    def require_synthetic_binding(self) -> None:
        """Refuse production stores at private in-process test surfaces."""

        if (
            type(self) is not DurableReceiptStore
            or self._mode is not _SYNTHETIC_STORE_MODE
        ):
            raise DurableReceiptError("durable_receipt_store_not_synthetic")

    def path(self, artifact: ReceiptArtifact, execution_id: str) -> Path:
        _validate_execution_id(execution_id)
        if type(artifact) is not ReceiptArtifact:
            raise DurableReceiptError("durable_receipt_kind_invalid")
        return self._root / execution_id / _FILENAMES[artifact]

    def _evidence(
        self,
        artifact: ReceiptArtifact,
        execution_id: str,
        path: Path,
        raw: bytes,
    ) -> DurableReceiptEvidence:
        receipt = _parse_canonical(artifact, raw)
        if (
            artifact in {ReceiptArtifact.INSTALL, ReceiptArtifact.EMPTY_ROLLBACK}
            and receipt.get("execution_id") != execution_id
        ):
            raise DurableReceiptError("durable_receipt_path_binding_mismatch")
        if (
            artifact is ReceiptArtifact.EMPTY_ROLLBACK_ELIGIBILITY
            and receipt.get("installation_execution_id") != execution_id
        ):
            raise DurableReceiptError("durable_receipt_path_binding_mismatch")
        if (
            artifact in _POSTGRES_NATIVE_EXPECTATIONS
            and receipt.get("execution_id") != execution_id
        ):
            raise DurableReceiptError("durable_receipt_path_binding_mismatch")
        return DurableReceiptEvidence(
            artifact=artifact,
            execution_id=execution_id,
            path=str(path),
            path_sha256=hashlib.sha256(str(path).encode("utf-8")).hexdigest(),
            canonical_file_sha256=hashlib.sha256(raw).hexdigest(),
            receipt_sha256=str(receipt["receipt_sha256"]),
            canonical_receipt=MappingProxyType(receipt),
        )

    def read(
        self,
        artifact: ReceiptArtifact,
        execution_id: str,
    ) -> DurableReceiptEvidence:
        path = self.path(artifact, execution_id)
        directory_fd = file_fd = -1
        try:
            directory_fd = _open_directory_nofollow(
                path.parent, expected_uid=self._expected_uid
            )
            directory_identity = _validate_directory(
                directory_fd,
                path.parent,
                expected_uid=self._expected_uid,
            )
            file_fd = _open_receipt(
                directory_fd,
                path.name,
                expected_uid=self._expected_uid,
            )
            raw = _read_stable(file_fd, directory_fd, path.name)
            if _validate_directory(
                directory_fd,
                path.parent,
                expected_uid=self._expected_uid,
            ) != directory_identity:
                raise DurableReceiptError("durable_receipt_directory_replaced")
            return self._evidence(artifact, execution_id, path, raw)
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            if directory_fd >= 0:
                os.close(directory_fd)

    def read_if_present(
        self,
        artifact: ReceiptArtifact,
        execution_id: str,
    ) -> DurableReceiptEvidence | None:
        """Read one exact receipt, returning None only for stable ENOENT."""

        path = self.path(artifact, execution_id)
        root_fd = directory_fd = file_fd = -1
        try:
            root_fd = _open_directory_nofollow(
                self._root, expected_uid=self._expected_uid
            )
            root_identity = _validate_directory(
                root_fd,
                self._root,
                expected_uid=self._expected_uid,
            )
            flags = os.O_RDONLY | os.O_CLOEXEC | _nofollow()
            flags |= getattr(os, "O_DIRECTORY", 0)
            try:
                directory_fd = os.open(
                    execution_id,
                    flags,
                    dir_fd=root_fd,
                )
            except FileNotFoundError:
                try:
                    os.stat(
                        execution_id,
                        dir_fd=root_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise DurableReceiptError(
                        "durable_receipt_directory_invalid"
                    ) from error
                else:
                    raise DurableReceiptError(
                        "durable_receipt_directory_appeared"
                    )
                if _validate_directory(
                    root_fd,
                    self._root,
                    expected_uid=self._expected_uid,
                ) != root_identity:
                    raise DurableReceiptError(
                        "durable_receipt_root_replaced"
                    )
                return None
            except OSError as error:
                raise DurableReceiptError(
                    "durable_receipt_directory_invalid"
                ) from error
            directory_identity = _validate_directory(
                directory_fd,
                path.parent,
                expected_uid=self._expected_uid,
            )
            try:
                file_fd = os.open(
                    path.name,
                    os.O_RDONLY | os.O_CLOEXEC | _nofollow(),
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                try:
                    os.stat(
                        path.name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise DurableReceiptError(
                        "durable_receipt_file_invalid"
                    ) from error
                else:
                    raise DurableReceiptError(
                        "durable_receipt_file_appeared_during_read"
                    )
                if _validate_directory(
                    directory_fd,
                    path.parent,
                    expected_uid=self._expected_uid,
                ) != directory_identity:
                    raise DurableReceiptError(
                        "durable_receipt_directory_replaced"
                    )
                if _validate_directory(
                    root_fd,
                    self._root,
                    expected_uid=self._expected_uid,
                ) != root_identity:
                    raise DurableReceiptError("durable_receipt_root_replaced")
                return None
            except OSError as error:
                raise DurableReceiptError(
                    "durable_receipt_file_invalid"
                ) from error
            opened = os.fstat(file_fd)
            named = os.stat(
                path.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(named.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_uid != self._expected_uid
                or opened.st_nlink != 1
                or opened.st_size > MAX_DURABLE_RECEIPT_BYTES
                or (opened.st_dev, opened.st_ino)
                != (named.st_dev, named.st_ino)
            ):
                raise DurableReceiptError("durable_receipt_file_invalid")
            raw = _read_stable(file_fd, directory_fd, path.name)
            if _validate_directory(
                directory_fd,
                path.parent,
                expected_uid=self._expected_uid,
            ) != directory_identity:
                raise DurableReceiptError(
                    "durable_receipt_directory_replaced"
                )
            if _validate_directory(
                root_fd,
                self._root,
                expected_uid=self._expected_uid,
            ) != root_identity:
                raise DurableReceiptError("durable_receipt_root_replaced")
            return self._evidence(artifact, execution_id, path, raw)
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            if directory_fd >= 0:
                os.close(directory_fd)
            if root_fd >= 0:
                os.close(root_fd)

    def write_once(
        self,
        artifact: ReceiptArtifact,
        execution_id: str,
        receipt: Mapping[str, object],
    ) -> DurableReceiptEvidence:
        canonical = canonical_json_bytes(_validate_receipt(artifact, receipt))
        if len(canonical) > MAX_DURABLE_RECEIPT_BYTES:
            raise DurableReceiptError("durable_receipt_size_invalid")
        path = self.path(artifact, execution_id)
        directory_fd = temporary_fd = -1
        temporary_name = ""
        try:
            directory_fd = _open_directory_nofollow(
                path.parent, expected_uid=self._expected_uid
            )
            directory_identity = _validate_directory(
                directory_fd,
                path.parent,
                expected_uid=self._expected_uid,
            )
            existing_fd = _open_receipt_if_present(
                directory_fd,
                path.name,
                expected_uid=self._expected_uid,
            )
            try:
                if existing_fd is not None:
                    existing_raw = _read_stable(
                        existing_fd, directory_fd, path.name
                    )
                else:
                    existing_raw = None
                if existing_raw is not None:
                    existing = self._evidence(
                        artifact, execution_id, path, existing_raw
                    )
                    if existing.canonical_file_sha256 != hashlib.sha256(
                        canonical
                    ).hexdigest():
                        raise DurableReceiptError(
                            "durable_receipt_replace_refused"
                        )
                    if _validate_directory(
                        directory_fd,
                        path.parent,
                        expected_uid=self._expected_uid,
                    ) != directory_identity:
                        raise DurableReceiptError(
                            "durable_receipt_directory_replaced"
                        )
                    return existing
            finally:
                if existing_fd is not None:
                    os.close(existing_fd)

            temporary_name = (
                "." + path.name + "." + secrets.token_hex(16) + ".tmp"
            )
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | _nofollow(),
                0o600,
                dir_fd=directory_fd,
            )
            created = os.fstat(temporary_fd)
            if (
                not stat.S_ISREG(created.st_mode)
                or stat.S_IMODE(created.st_mode) != 0o600
                or created.st_uid != self._expected_uid
                or created.st_nlink != 1
            ):
                raise DurableReceiptError("durable_receipt_temporary_invalid")
            _write_all(temporary_fd, canonical)
            os.fsync(temporary_fd)
            written = os.fstat(temporary_fd)
            if written.st_size != len(canonical):
                raise DurableReceiptError("durable_receipt_write_failed")
            try:
                os.link(
                    temporary_name,
                    path.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                existing_fd = _open_receipt(
                    directory_fd,
                    path.name,
                    expected_uid=self._expected_uid,
                )
                try:
                    existing_raw = _read_stable(
                        existing_fd, directory_fd, path.name
                    )
                finally:
                    os.close(existing_fd)
                existing = self._evidence(
                    artifact, execution_id, path, existing_raw
                )
                if existing.canonical_file_sha256 != hashlib.sha256(
                    canonical
                ).hexdigest():
                    raise DurableReceiptError("durable_receipt_replace_refused")
                if _validate_directory(
                    directory_fd,
                    path.parent,
                    expected_uid=self._expected_uid,
                ) != directory_identity:
                    raise DurableReceiptError(
                        "durable_receipt_directory_replaced"
                    )
                return existing
            os.unlink(temporary_name, dir_fd=directory_fd)
            temporary_name = ""
            os.fsync(directory_fd)
            if _validate_directory(
                directory_fd,
                path.parent,
                expected_uid=self._expected_uid,
            ) != directory_identity:
                raise DurableReceiptError("durable_receipt_directory_replaced")
            final_fd = _open_receipt(
                directory_fd,
                path.name,
                expected_uid=self._expected_uid,
            )
            try:
                final_raw = _read_stable(final_fd, directory_fd, path.name)
            finally:
                os.close(final_fd)
            if _validate_directory(
                directory_fd,
                path.parent,
                expected_uid=self._expected_uid,
            ) != directory_identity:
                raise DurableReceiptError("durable_receipt_directory_replaced")
            evidence = self._evidence(
                artifact, execution_id, path, final_raw
            )
            if evidence.canonical_file_sha256 != hashlib.sha256(
                canonical
            ).hexdigest():
                raise DurableReceiptError("durable_receipt_postwrite_mismatch")
            return evidence
        except OSError as error:
            raise DurableReceiptError("durable_receipt_write_failed") from error
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            if directory_fd >= 0:
                if temporary_name:
                    try:
                        os.unlink(temporary_name, dir_fd=directory_fd)
                        os.fsync(directory_fd)
                    except OSError:
                        pass
                os.close(directory_fd)


class StoreBackedEmptyRollbackReceiptSink:
    """Closed final-receipt sink used by the rollback controller."""

    def __init__(self, store: DurableReceiptStore) -> None:
        if type(store) is not DurableReceiptStore:
            raise DurableReceiptError("durable_receipt_sink_invalid")
        self._store = store

    def persist(
        self,
        execution_id: str,
        receipt: Mapping[str, object],
    ) -> DurableReceiptEvidence:
        return self._store.write_once(
            ReceiptArtifact.EMPTY_ROLLBACK,
            execution_id,
            receipt,
        )


__all__ = [
    "DurableReceiptError",
    "DurableReceiptEvidence",
    "DurableReceiptStore",
    "EmptyRollbackReceiptSink",
    "MAX_DURABLE_RECEIPT_BYTES",
    "PRODUCTION_EXECUTIONS_ROOT",
    "ReceiptArtifact",
    "StoreBackedEmptyRollbackReceiptSink",
]

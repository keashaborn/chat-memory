from __future__ import annotations

"""Strict, secret-free, append-only identity ledger for exact resources."""

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Final, Iterable

from .authority_state import AuthorityState, AuthorityStateError
from .execution_capability import (
    ClaimedExecutionBindingError,
    _claimed_execution_binding_evidence,
)
from .execution_lock import (
    ExecutionLockError,
    HeldExecutionLockCapability,
    validate_held_execution_lock,
)


SCHEMA_VERSION: Final = "governed-memory-resource-identity-entry-v2"
ZERO_HEAD: Final = "0" * 64
MAX_LEDGER_BYTES: Final = 4 * 1024 * 1024
MAX_ENTRY_BYTES: Final = 8 * 1024
ENTRY_KEYS: Final = frozenset(
    {
        "schema_version",
        "sequence",
        "previous_entry_sha256",
        "binding_sha256",
        "event",
        "resource_kind",
        "resource_name",
        "resource_id",
        "image_id",
        "image_repo_digest",
        "ownership_sha256",
        "resource_labels_sha256",
        "entry_sha256",
    }
)
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_SAFE_ID_RE = re.compile(
    r"/?[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}\Z", re.ASCII
)
_FORBIDDEN_CONTENT = (
    "api_key",
    "bearer",
    "credential",
    "password",
    "private_key",
    "secret_value",
    "token",
)
_EVENTS = frozenset({"created", "observed", "started", "stopped", "removed"})
_KINDS = frozenset(
    {
        "container",
        "database",
        "image",
        "migration",
        "network",
        "qdrant_alias",
        "qdrant_collection",
        "resolved_store_spec",
        "secret_file",
        "systemd_unit",
        "volume",
    }
)
_TRANSITIONS: Final = {
    None: frozenset({"created", "observed"}),
    "created": frozenset({"observed", "started", "stopped", "removed"}),
    "observed": frozenset({"observed", "started", "stopped", "removed"}),
    "started": frozenset({"observed", "stopped"}),
    "stopped": frozenset({"observed", "started", "removed"}),
    "removed": frozenset(),
}
_RESOURCE_LEDGER_BINDING_DOMAIN: Final = (
    b"governed-memory-resource-ledger-binding-v1\x00"
)


class ResourceIdentityError(RuntimeError):
    """Closed refusal for ledger integrity or schema failure."""


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ResourceIdentityError("identity_entry_not_canonicalizable") from error


def resource_ledger_binding_sha256(journal_binding_sha256: str) -> str:
    """Derive the one ledger domain binding used by installer and supervisor."""

    if (
        type(journal_binding_sha256) is not str
        or _HASH_RE.fullmatch(journal_binding_sha256) is None
    ):
        raise ResourceIdentityError("identity_binding_invalid")
    return hashlib.sha256(
        _RESOURCE_LEDGER_BINDING_DOMAIN
        + journal_binding_sha256.encode("ascii")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ResourceIdentityRecord:
    sequence: int
    previous_entry_sha256: str
    binding_sha256: str
    event: str
    resource_kind: str
    resource_name: str
    resource_id: str
    image_id: str | None
    image_repo_digest: str | None
    ownership_sha256: str
    resource_labels_sha256: str | None
    entry_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "binding_sha256": self.binding_sha256,
            "entry_sha256": self.entry_sha256,
            "event": self.event,
            "image_id": self.image_id,
            "image_repo_digest": self.image_repo_digest,
            "ownership_sha256": self.ownership_sha256,
            "resource_labels_sha256": self.resource_labels_sha256,
            "previous_entry_sha256": self.previous_entry_sha256,
            "resource_id": self.resource_id,
            "resource_kind": self.resource_kind,
            "resource_name": self.resource_name,
            "schema_version": SCHEMA_VERSION,
            "sequence": self.sequence,
        }


def _validate_payload(payload: dict[str, object]) -> None:
    if set(payload) != ENTRY_KEYS - {"entry_sha256"}:
        raise ResourceIdentityError("identity_entry_keys_invalid")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ResourceIdentityError("identity_schema_invalid")
    if type(payload["sequence"]) is not int or payload["sequence"] < 1:
        raise ResourceIdentityError("identity_sequence_invalid")
    for key in ("previous_entry_sha256", "binding_sha256", "ownership_sha256"):
        if type(payload[key]) is not str or _HASH_RE.fullmatch(payload[key]) is None:
            raise ResourceIdentityError("identity_hash_invalid")
    resource_labels_sha256 = payload["resource_labels_sha256"]
    labeled_kinds = {"container", "network", "volume"}
    if payload["resource_kind"] in labeled_kinds:
        if (
            type(resource_labels_sha256) is not str
            or _HASH_RE.fullmatch(resource_labels_sha256) is None
        ):
            raise ResourceIdentityError("identity_resource_labels_invalid")
    elif resource_labels_sha256 is not None:
        raise ResourceIdentityError("identity_resource_labels_forbidden")
    if payload["event"] not in _EVENTS:
        raise ResourceIdentityError("identity_event_invalid")
    if payload["resource_kind"] not in _KINDS:
        raise ResourceIdentityError("identity_kind_invalid")
    for key in ("resource_name", "resource_id"):
        if (
            type(payload[key]) is not str
            or _SAFE_ID_RE.fullmatch(payload[key]) is None
            or "//" in payload[key]
            or any(part in {".", ".."} for part in payload[key].split("/"))
        ):
            raise ResourceIdentityError("identity_value_invalid")
    if (
        payload["resource_kind"] == "container"
        and _CONTAINER_ID_RE.fullmatch(payload["resource_id"]) is None
    ):
        raise ResourceIdentityError("container_id_invalid")
    image_id = payload["image_id"]
    if image_id is not None and (
        type(image_id) is not str or _IMAGE_ID_RE.fullmatch(image_id) is None
    ):
        raise ResourceIdentityError("identity_image_id_invalid")
    repo_digest = payload["image_repo_digest"]
    if repo_digest is not None and (
        type(repo_digest) is not str
        or re.fullmatch(
            r"[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}", repo_digest
        )
        is None
    ):
        raise ResourceIdentityError("identity_repo_digest_invalid")
    if payload["resource_kind"] == "container" and (
        image_id is None or repo_digest is None
    ):
        raise ResourceIdentityError("container_image_identity_required")
    if payload["resource_kind"] not in {"container", "image"} and (
        image_id is not None or repo_digest is not None
    ):
        raise ResourceIdentityError("identity_image_metadata_forbidden")
    rendered = _canonical_json(payload).decode("ascii").lower()
    if any(marker in rendered for marker in _FORBIDDEN_CONTENT):
        raise ResourceIdentityError("identity_entry_forbidden_content")


def _record_from_value(value: object) -> ResourceIdentityRecord:
    if type(value) is not dict or set(value) != ENTRY_KEYS:
        raise ResourceIdentityError("identity_entry_keys_invalid")
    payload = dict(value)
    entry_sha256 = payload.pop("entry_sha256")
    _validate_payload(payload)
    if type(entry_sha256) is not str or _HASH_RE.fullmatch(entry_sha256) is None:
        raise ResourceIdentityError("identity_entry_hash_invalid")
    calculated = hashlib.sha256(_canonical_json(payload)).hexdigest()
    if entry_sha256 != calculated:
        raise ResourceIdentityError("identity_entry_hash_mismatch")
    return ResourceIdentityRecord(
        sequence=payload["sequence"],  # type: ignore[arg-type]
        previous_entry_sha256=payload["previous_entry_sha256"],  # type: ignore[arg-type]
        binding_sha256=payload["binding_sha256"],  # type: ignore[arg-type]
        event=payload["event"],  # type: ignore[arg-type]
        resource_kind=payload["resource_kind"],  # type: ignore[arg-type]
        resource_name=payload["resource_name"],  # type: ignore[arg-type]
        resource_id=payload["resource_id"],  # type: ignore[arg-type]
        image_id=payload["image_id"],  # type: ignore[arg-type]
        image_repo_digest=payload["image_repo_digest"],  # type: ignore[arg-type]
        ownership_sha256=payload["ownership_sha256"],  # type: ignore[arg-type]
        resource_labels_sha256=payload["resource_labels_sha256"],  # type: ignore[arg-type]
        entry_sha256=entry_sha256,
    )


def parse_ledger_bytes(
    raw: bytes, *, expected_binding_sha256: str
) -> tuple[ResourceIdentityRecord, ...]:
    if _HASH_RE.fullmatch(expected_binding_sha256) is None:
        raise ResourceIdentityError("identity_binding_invalid")
    if len(raw) > MAX_LEDGER_BYTES:
        raise ResourceIdentityError("identity_ledger_too_large")
    if raw and not raw.endswith(b"\n"):
        raise ResourceIdentityError("identity_ledger_partial_entry")
    records: list[ResourceIdentityRecord] = []
    latest: dict[tuple[str, str], ResourceIdentityRecord] = {}
    previous = ZERO_HEAD
    for sequence, line in enumerate(raw.splitlines(), start=1):
        if not line or len(line) > MAX_ENTRY_BYTES:
            raise ResourceIdentityError("identity_entry_size_invalid")
        try:
            value = json.loads(line.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ResourceIdentityError("identity_entry_json_invalid") from error
        if _canonical_json(value) != line:
            raise ResourceIdentityError("identity_entry_not_canonical")
        record = _record_from_value(value)
        if record.sequence != sequence:
            raise ResourceIdentityError("identity_sequence_mismatch")
        if record.previous_entry_sha256 != previous:
            raise ResourceIdentityError("identity_chain_mismatch")
        if record.binding_sha256 != expected_binding_sha256:
            raise ResourceIdentityError("identity_binding_mismatch")
        identity_key = (record.resource_kind, record.resource_name)
        prior_identity = latest.get(identity_key)
        prior_event = prior_identity.event if prior_identity is not None else None
        if record.event not in _TRANSITIONS[prior_event]:
            raise ResourceIdentityError("identity_transition_invalid")
        if prior_identity is not None and (
            record.resource_id != prior_identity.resource_id
            or record.image_id != prior_identity.image_id
            or record.image_repo_digest != prior_identity.image_repo_digest
            or record.ownership_sha256 != prior_identity.ownership_sha256
            or record.resource_labels_sha256
            != prior_identity.resource_labels_sha256
        ):
            raise ResourceIdentityError("identity_resource_drift")
        previous = record.entry_sha256
        records.append(record)
        latest[identity_key] = record
    return tuple(records)


def _nofollow_flag() -> int:
    value = getattr(os, "O_NOFOLLOW", 0)
    if value == 0:
        raise ResourceIdentityError("identity_ledger_nofollow_unavailable")
    return value


def _open_secure_parent(path: Path, *, expected_uid: int) -> int:
    if not path.is_absolute() or not path.name or path.name in {".", ".."}:
        raise ResourceIdentityError("identity_ledger_path_invalid")
    flags = os.O_RDONLY | os.O_CLOEXEC | _nofollow_flag()
    flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path.parent, flags)
        opened = os.fstat(descriptor)
        named = path.parent.stat(follow_symlinks=False)
    except OSError as error:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise ResourceIdentityError("identity_ledger_directory_invalid") from error
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o700
        or opened.st_uid != expected_uid
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        os.close(descriptor)
        raise ResourceIdentityError("identity_ledger_directory_invalid")
    return descriptor


def _validate_open_file(
    descriptor: int,
    *,
    directory_descriptor: int,
    name: str,
    expected_uid: int,
) -> os.stat_result:
    try:
        opened = os.fstat(descriptor)
        named = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ResourceIdentityError("identity_ledger_file_invalid") from error
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_uid != expected_uid
        or opened.st_nlink != 1
        or opened.st_size > MAX_LEDGER_BYTES
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise ResourceIdentityError("identity_ledger_file_invalid")
    return opened


def _read_open_file(descriptor: int, *, expected_size: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        block = os.read(descriptor, min(64 * 1024, MAX_LEDGER_BYTES + 1 - total))
        if not block:
            break
        chunks.append(block)
        total += len(block)
        if total > MAX_LEDGER_BYTES:
            raise ResourceIdentityError("identity_ledger_too_large")
    if total != expected_size:
        raise ResourceIdentityError("identity_ledger_changed_during_read")
    return b"".join(chunks)


def load_ledger(
    path: Path,
    *,
    expected_binding_sha256: str,
    expected_uid: int | None = None,
) -> tuple[ResourceIdentityRecord, ...]:
    path = Path(path)
    uid = os.geteuid() if expected_uid is None else expected_uid
    directory_descriptor = _open_secure_parent(path, expected_uid=uid)
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_CLOEXEC | _nofollow_flag(),
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            return ()
        except OSError as error:
            raise ResourceIdentityError("identity_ledger_open_failed") from error
        info = _validate_open_file(
            descriptor,
            directory_descriptor=directory_descriptor,
            name=path.name,
            expected_uid=uid,
        )
        raw = _read_open_file(descriptor, expected_size=info.st_size)
        after = _validate_open_file(
            descriptor,
            directory_descriptor=directory_descriptor,
            name=path.name,
            expected_uid=uid,
        )
        if (after.st_dev, after.st_ino, after.st_size) != (
            info.st_dev,
            info.st_ino,
            info.st_size,
        ):
            raise ResourceIdentityError("identity_ledger_changed_during_read")
        return parse_ledger_bytes(
            raw, expected_binding_sha256=expected_binding_sha256
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_descriptor)


class ResourceIdentityLedger:
    def __init__(
        self,
        path: Path,
        *,
        binding_sha256: str | None = None,
        claimed_execution_binding: object | None = None,
        authority_state: AuthorityState | None = None,
        held_lock: HeldExecutionLockCapability | None = None,
        expected_uid: int | None = None,
        create: bool = False,
    ) -> None:
        if not isinstance(create, bool):
            raise ResourceIdentityError("identity_ledger_configuration_invalid")
        self.path = Path(path)
        self.expected_uid = os.geteuid() if expected_uid is None else expected_uid
        if type(self.expected_uid) is not int or self.expected_uid < 0:
            raise ResourceIdentityError("identity_ledger_configuration_invalid")
        self.authority_state: AuthorityState | None = None
        self.held_lock: HeldExecutionLockCapability | None = None
        self._secure_mode = claimed_execution_binding is not None
        self._last_anchor_result = "legacy_unanchored"
        if not self._secure_mode:
            if (
                not self.path.is_absolute()
                or not isinstance(binding_sha256, str)
                or _HASH_RE.fullmatch(binding_sha256) is None
                or authority_state is not None
                or held_lock is not None
                or create
            ):
                raise ResourceIdentityError(
                    "identity_ledger_configuration_invalid"
                )
            # Compatibility-only parser/test mode.  It is intentionally not
            # accepted as a current installation execution boundary.
            self.binding_sha256 = binding_sha256
            return
        if binding_sha256 is not None or type(authority_state) is not AuthorityState:
            raise ResourceIdentityError("identity_ledger_configuration_invalid")
        try:
            binding = _claimed_execution_binding_evidence(
                claimed_execution_binding
            )
            assert held_lock is not None
            validate_held_execution_lock(held_lock)
        except (AssertionError, ClaimedExecutionBindingError, ExecutionLockError) as error:
            raise ResourceIdentityError(
                "identity_ledger_execution_binding_invalid"
            ) from error
        lock_path_sha256 = hashlib.sha256(
            str(held_lock._owner.path).encode("utf-8")
        ).hexdigest()
        state_path_sha256 = hashlib.sha256(
            str(authority_state.path).encode("utf-8")
        ).hexdigest()
        if (
            str(self.path) != binding.resource_identity_ledger_path
            or hashlib.sha256(str(self.path).encode("utf-8")).hexdigest()
            != binding.resource_identity_ledger_path_sha256
            or lock_path_sha256 != binding.global_lock_path_sha256
            or state_path_sha256 != binding.authority_state_path_sha256
        ):
            raise ResourceIdentityError(
                "identity_ledger_execution_binding_mismatch"
            )
        self.binding_sha256 = resource_ledger_binding_sha256(
            binding.journal_binding_sha256
        )
        self.authority_state = authority_state
        self.held_lock = held_lock
        self._secure_reconcile(create=create)

    def _require_lock(self) -> None:
        if not self._secure_mode or self.held_lock is None:
            raise ResourceIdentityError("identity_ledger_secure_mode_required")
        try:
            validate_held_execution_lock(self.held_lock)
        except ExecutionLockError as error:
            raise ResourceIdentityError(
                "identity_ledger_global_lock_not_held"
            ) from error

    def _reconcile_anchor(
        self,
        records: tuple[ResourceIdentityRecord, ...],
    ) -> None:
        self._require_lock()
        assert self.authority_state is not None
        assert self.held_lock is not None
        sequence = len(records)
        head = records[-1].entry_sha256 if records else ZERO_HEAD
        prior = records[-1].previous_entry_sha256 if records else ZERO_HEAD
        try:
            result = self.authority_state.advance_resource_ledger_anchor(
                self.binding_sha256,
                ledger_sequence=sequence,
                ledger_head_sha256=head,
                ledger_prior_head_sha256=prior,
                held_lock=self.held_lock,
            )
        except AuthorityStateError as error:
            raise ResourceIdentityError(
                "identity_ledger_anchor_mismatch"
            ) from error
        self._last_anchor_result = result.result
        self._require_lock()

    def _recover_torn_tail(
        self,
        *,
        directory_descriptor: int,
        descriptor: int,
        raw: bytes,
    ) -> tuple[ResourceIdentityRecord, ...]:
        """Discard one final partial entry only after an exact anchor match."""

        self._require_lock()
        final_newline = raw.rfind(b"\n")
        complete_length = final_newline + 1
        complete = raw[:complete_length]
        partial = raw[complete_length:]
        expected_prefix = (
            b'{"binding_sha256":"'
            + self.binding_sha256.encode("ascii")
            + b'"'
        )
        shared = min(len(partial), len(expected_prefix))
        if (
            not partial
            or len(partial) > MAX_ENTRY_BYTES
            or partial[:shared] != expected_prefix[:shared]
        ):
            raise ResourceIdentityError("identity_ledger_truncated")
        records = parse_ledger_bytes(
            complete,
            expected_binding_sha256=self.binding_sha256,
        )
        head = records[-1].entry_sha256 if records else ZERO_HEAD
        assert self.authority_state is not None
        assert self.held_lock is not None
        try:
            anchor = self.authority_state.read_resource_ledger_anchor(
                self.binding_sha256,
                held_lock=self.held_lock,
            )
            self._require_lock()
        except AuthorityStateError as error:
            raise ResourceIdentityError(
                "identity_ledger_anchor_mismatch"
            ) from error
        if anchor.sequence != len(records) or anchor.head_sha256 != head:
            raise ResourceIdentityError(
                "identity_ledger_truncated_tail_not_exact_anchor"
            )

        try:
            opened = _validate_open_file(
                descriptor,
                directory_descriptor=directory_descriptor,
                name=self.path.name,
                expected_uid=self.expected_uid,
            )
            self.authority_state.seal_filesystem_identity(
                self.binding_sha256,
                artifact_kind="resource_ledger",
                path=self.path,
                directory_fd=directory_descriptor,
                file_fd=descriptor,
                held_lock=self.held_lock,
            )
            if opened.st_size != len(raw):
                raise ResourceIdentityError(
                    "identity_ledger_changed_during_tail_recovery"
                )
            observed = _read_open_file(descriptor, expected_size=len(raw))
            if observed != raw:
                raise ResourceIdentityError(
                    "identity_ledger_changed_during_tail_recovery"
                )
            self._require_lock()
            os.ftruncate(descriptor, complete_length)
            os.fsync(descriptor)
            os.fsync(directory_descriptor)
            self._require_lock()
            truncated = _validate_open_file(
                descriptor,
                directory_descriptor=directory_descriptor,
                name=self.path.name,
                expected_uid=self.expected_uid,
            )
            self.authority_state.seal_filesystem_identity(
                self.binding_sha256,
                artifact_kind="resource_ledger",
                path=self.path,
                directory_fd=directory_descriptor,
                file_fd=descriptor,
                held_lock=self.held_lock,
            )
            if truncated.st_size != complete_length:
                raise ResourceIdentityError(
                    "identity_ledger_tail_recovery_failed"
                )
        except AuthorityStateError as error:
            raise ResourceIdentityError(
                "identity_ledger_cross_process_identity_mismatch"
            ) from error
        except OSError as error:
            raise ResourceIdentityError(
                "identity_ledger_tail_recovery_failed"
            ) from error
        self._require_lock()
        return records

    @property
    def anchor_result(self) -> str:
        return self._last_anchor_result

    @property
    def secure_execution_mode(self) -> bool:
        return self._secure_mode

    def _open_secure(
        self,
        *,
        create: bool,
    ) -> tuple[int, int, tuple[ResourceIdentityRecord, ...]]:
        self._require_lock()
        directory_descriptor = _open_secure_parent(
            self.path,
            expected_uid=self.expected_uid,
        )
        descriptor = -1
        flags = os.O_APPEND | os.O_CLOEXEC | _nofollow_flag() | os.O_RDWR
        try:
            try:
                descriptor = os.open(
                    self.path.name,
                    flags | (os.O_CREAT | os.O_EXCL if create else 0),
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except OSError as error:
                raise ResourceIdentityError(
                    "identity_ledger_open_failed"
                ) from error
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as error:
                raise ResourceIdentityError("identity_ledger_locked") from error
            if create:
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
                os.fsync(directory_descriptor)
            info = _validate_open_file(
                descriptor,
                directory_descriptor=directory_descriptor,
                name=self.path.name,
                expected_uid=self.expected_uid,
            )
            assert self.authority_state is not None
            assert self.held_lock is not None
            try:
                self.authority_state.seal_filesystem_identity(
                    self.binding_sha256,
                    artifact_kind="resource_ledger",
                    path=self.path,
                    directory_fd=directory_descriptor,
                    file_fd=descriptor,
                    held_lock=self.held_lock,
                )
            except AuthorityStateError as error:
                raise ResourceIdentityError(
                    "identity_ledger_cross_process_identity_mismatch"
                ) from error
            self._require_lock()
            raw = _read_open_file(descriptor, expected_size=info.st_size)
            if raw and not raw.endswith(b"\n"):
                records = self._recover_torn_tail(
                    directory_descriptor=directory_descriptor,
                    descriptor=descriptor,
                    raw=raw,
                )
            else:
                records = parse_ledger_bytes(
                    raw,
                    expected_binding_sha256=self.binding_sha256,
                )
            after = _validate_open_file(
                descriptor,
                directory_descriptor=directory_descriptor,
                name=self.path.name,
                expected_uid=self.expected_uid,
            )
            expected_size = (
                raw.rfind(b"\n") + 1
                if raw and not raw.endswith(b"\n")
                else info.st_size
            )
            if (
                (after.st_dev, after.st_ino)
                != (info.st_dev, info.st_ino)
                or after.st_size != expected_size
            ):
                raise ResourceIdentityError(
                    "identity_ledger_changed_during_read"
                )
            self._reconcile_anchor(records)
            return directory_descriptor, descriptor, records
        except BaseException:
            if descriptor >= 0:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            os.close(directory_descriptor)
            raise

    @staticmethod
    def _close_secure(directory_descriptor: int, descriptor: int) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            os.close(directory_descriptor)

    def _secure_reconcile(self, *, create: bool) -> None:
        directory_descriptor, descriptor, _records = self._open_secure(
            create=create
        )
        self._close_secure(directory_descriptor, descriptor)

    def records(self) -> tuple[ResourceIdentityRecord, ...]:
        """Return an anchored snapshot in current execution mode only."""

        if not self._secure_mode:
            return load_ledger(
                self.path,
                expected_binding_sha256=self.binding_sha256,
                expected_uid=self.expected_uid,
            )
        directory_descriptor, descriptor, records = self._open_secure(
            create=False
        )
        self._close_secure(directory_descriptor, descriptor)
        return records

    def _secure_append(
        self,
        *,
        event: str,
        resource_kind: str,
        resource_name: str,
        resource_id: str,
        ownership_sha256: str,
        image_id: str | None,
        image_repo_digest: str | None,
        resource_labels_sha256: str | None = None,
    ) -> ResourceIdentityRecord:
        directory_descriptor, descriptor, records = self._open_secure(
            create=False
        )
        try:
            current = next(
                (
                    record
                    for record in reversed(records)
                    if record.resource_kind == resource_kind
                    and record.resource_name == resource_name
                ),
                None,
            )
            previous_event = current.event if current is not None else None
            if event not in _TRANSITIONS[previous_event]:
                raise ResourceIdentityError("identity_transition_invalid")
            if current is not None and (
                current.resource_id != resource_id
                or current.image_id != image_id
                or current.image_repo_digest != image_repo_digest
                or current.ownership_sha256 != ownership_sha256
                or current.resource_labels_sha256 != resource_labels_sha256
            ):
                raise ResourceIdentityError("identity_resource_drift")
            payload: dict[str, object] = {
                "binding_sha256": self.binding_sha256,
                "event": event,
                "image_id": image_id,
                "image_repo_digest": image_repo_digest,
                "ownership_sha256": ownership_sha256,
                "resource_labels_sha256": resource_labels_sha256,
                "previous_entry_sha256": (
                    records[-1].entry_sha256 if records else ZERO_HEAD
                ),
                "resource_id": resource_id,
                "resource_kind": resource_kind,
                "resource_name": resource_name,
                "schema_version": SCHEMA_VERSION,
                "sequence": len(records) + 1,
            }
            _validate_payload(payload)
            value = dict(payload)
            value["entry_sha256"] = hashlib.sha256(
                _canonical_json(payload)
            ).hexdigest()
            encoded = _canonical_json(value) + b"\n"
            if len(encoded) > MAX_ENTRY_BYTES:
                raise ResourceIdentityError("identity_entry_size_invalid")
            before = _validate_open_file(
                descriptor,
                directory_descriptor=directory_descriptor,
                name=self.path.name,
                expected_uid=self.expected_uid,
            )
            if before.st_size + len(encoded) > MAX_LEDGER_BYTES:
                raise ResourceIdentityError("identity_ledger_too_large")
            try:
                self._require_lock()
                written = 0
                while written < len(encoded):
                    count = os.write(descriptor, encoded[written:])
                    if count <= 0:
                        raise OSError("short resource-ledger write")
                    written += count
                os.fsync(descriptor)
                os.fsync(directory_descriptor)
                self._require_lock()
            except OSError as error:
                raise ResourceIdentityError(
                    "identity_ledger_write_failed"
                ) from error
            info = _validate_open_file(
                descriptor,
                directory_descriptor=directory_descriptor,
                name=self.path.name,
                expected_uid=self.expected_uid,
            )
            observed = parse_ledger_bytes(
                _read_open_file(descriptor, expected_size=info.st_size),
                expected_binding_sha256=self.binding_sha256,
            )
            record = _record_from_value(value)
            if observed != (*records, record):
                raise ResourceIdentityError(
                    "identity_ledger_post_append_mismatch"
                )
            self._reconcile_anchor(observed)
            return record
        finally:
            self._close_secure(directory_descriptor, descriptor)

    def append(
        self,
        *,
        event: str,
        resource_kind: str,
        resource_name: str,
        resource_id: str,
        ownership_sha256: str,
        resource_labels_sha256: str | None = None,
        image_id: str | None = None,
        image_repo_digest: str | None = None,
    ) -> ResourceIdentityRecord:
        if self._secure_mode:
            return self._secure_append(
                event=event,
                resource_kind=resource_kind,
                resource_name=resource_name,
                resource_id=resource_id,
                ownership_sha256=ownership_sha256,
                resource_labels_sha256=resource_labels_sha256,
                image_id=image_id,
                image_repo_digest=image_repo_digest,
            )
        directory_descriptor = _open_secure_parent(
            self.path, expected_uid=self.expected_uid
        )
        flags = os.O_APPEND | os.O_CLOEXEC | _nofollow_flag() | os.O_RDWR
        created = False
        try:
            try:
                descriptor = os.open(
                    self.path.name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_descriptor,
                )
                created = True
            except FileExistsError:
                descriptor = os.open(
                    self.path.name,
                    flags,
                    dir_fd=directory_descriptor,
                )
        except OSError as error:
            os.close(directory_descriptor)
            raise ResourceIdentityError("identity_ledger_open_failed") from error
        try:
            if created:
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
                os.fsync(directory_descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            info = _validate_open_file(
                descriptor,
                directory_descriptor=directory_descriptor,
                name=self.path.name,
                expected_uid=self.expected_uid,
            )
            raw = _read_open_file(descriptor, expected_size=info.st_size)
            records = parse_ledger_bytes(
                raw, expected_binding_sha256=self.binding_sha256
            )
            current = next(
                (
                    record
                    for record in reversed(records)
                    if record.resource_kind == resource_kind
                    and record.resource_name == resource_name
                ),
                None,
            )
            previous_event = current.event if current is not None else None
            if event not in _TRANSITIONS[previous_event]:
                raise ResourceIdentityError("identity_transition_invalid")
            if current is not None and (
                current.resource_id != resource_id
                or current.image_id != image_id
                or current.image_repo_digest != image_repo_digest
                or current.ownership_sha256 != ownership_sha256
                or current.resource_labels_sha256 != resource_labels_sha256
            ):
                raise ResourceIdentityError("identity_resource_drift")
            payload: dict[str, object] = {
                "binding_sha256": self.binding_sha256,
                "event": event,
                "image_id": image_id,
                "image_repo_digest": image_repo_digest,
                "ownership_sha256": ownership_sha256,
                "resource_labels_sha256": resource_labels_sha256,
                "previous_entry_sha256": (
                    records[-1].entry_sha256 if records else ZERO_HEAD
                ),
                "resource_id": resource_id,
                "resource_kind": resource_kind,
                "resource_name": resource_name,
                "schema_version": SCHEMA_VERSION,
                "sequence": len(records) + 1,
            }
            _validate_payload(payload)
            value = dict(payload)
            value["entry_sha256"] = hashlib.sha256(
                _canonical_json(payload)
            ).hexdigest()
            encoded = _canonical_json(value) + b"\n"
            if len(encoded) > MAX_ENTRY_BYTES:
                raise ResourceIdentityError("identity_entry_size_invalid")
            if info.st_size + len(encoded) > MAX_LEDGER_BYTES:
                raise ResourceIdentityError("identity_ledger_too_large")
            _validate_open_file(
                descriptor,
                directory_descriptor=directory_descriptor,
                name=self.path.name,
                expected_uid=self.expected_uid,
            )
            written = 0
            while written < len(encoded):
                count = os.write(descriptor, encoded[written:])
                if count <= 0:
                    raise ResourceIdentityError("identity_ledger_write_failed")
                written += count
            os.fsync(descriptor)
            os.fsync(directory_descriptor)
            _validate_open_file(
                descriptor,
                directory_descriptor=directory_descriptor,
                name=self.path.name,
                expected_uid=self.expected_uid,
            )
            return _record_from_value(value)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
                os.close(directory_descriptor)


def latest_exact_resources(
    records: Iterable[ResourceIdentityRecord],
) -> dict[tuple[str, str], ResourceIdentityRecord]:
    latest: dict[tuple[str, str], ResourceIdentityRecord] = {}
    for record in records:
        latest[(record.resource_kind, record.resource_name)] = record
    return latest

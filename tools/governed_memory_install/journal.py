from __future__ import annotations

"""Durable, content-free journal for the inactive installation controller."""

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Final


JOURNAL_SCHEMA_VERSION: Final = "governed-memory-installation-journal-entry-v1"
JOURNAL_KEYS: Final = frozenset(
    {
        "schema_version",
        "binding_sha256",
        "sequence",
        "operation",
        "attempt_id",
        "step_id",
        "event",
        "disposable_authorization_sha256",
        "prior_entry_sha256",
        "entry_sha256",
    }
)
OPERATIONS: Final = frozenset({"install", "rollback", "compensation"})
EVENTS: Final = frozenset(
    {"intent", "effect_complete", "recovered_after_effect"}
)
ZERO_HEAD: Final = "0" * 64
HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
ATTEMPT_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
STEP_RE: Final = re.compile(r"[IR][0-9]{2}_[A-Z0-9_]+\Z", re.ASCII)
MAX_JOURNAL_BYTES: Final = 16 * 1024 * 1024
MAX_ENTRY_BYTES: Final = 4096


class JournalError(RuntimeError):
    """Base class for fail-closed journal errors."""


class JournalSecurityError(JournalError):
    """The journal path or metadata is unsafe."""


class JournalIntegrityError(JournalError):
    """The journal bytes or hash chain are invalid."""


class JournalBusyError(JournalError):
    """Another controller owns the journal lock."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise JournalIntegrityError("journal_duplicate_key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class JournalEntry:
    binding_sha256: str
    sequence: int
    operation: str
    attempt_id: str
    step_id: str
    event: str
    disposable_authorization_sha256: str
    prior_entry_sha256: str
    entry_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "binding_sha256": self.binding_sha256,
            "sequence": self.sequence,
            "operation": self.operation,
            "attempt_id": self.attempt_id,
            "step_id": self.step_id,
            "event": self.event,
            "disposable_authorization_sha256": self.disposable_authorization_sha256,
            "prior_entry_sha256": self.prior_entry_sha256,
            "entry_sha256": self.entry_sha256,
        }


def _validate_scalar_fields(document: dict[str, object]) -> None:
    if set(document) != JOURNAL_KEYS:
        raise JournalIntegrityError("journal_entry_shape_invalid")
    if document.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        raise JournalIntegrityError("journal_schema_version_invalid")
    if not isinstance(document.get("sequence"), int) or isinstance(
        document.get("sequence"), bool
    ) or document["sequence"] < 1:
        raise JournalIntegrityError("journal_sequence_invalid")
    for key in (
        "binding_sha256",
        "disposable_authorization_sha256",
        "prior_entry_sha256",
        "entry_sha256",
    ):
        value = document.get(key)
        if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
            raise JournalIntegrityError("journal_hash_invalid")
    if document.get("operation") not in OPERATIONS:
        raise JournalIntegrityError("journal_operation_invalid")
    attempt_id = document.get("attempt_id")
    if not isinstance(attempt_id, str) or ATTEMPT_RE.fullmatch(attempt_id) is None:
        raise JournalIntegrityError("journal_attempt_id_invalid")
    step_id = document.get("step_id")
    if not isinstance(step_id, str) or STEP_RE.fullmatch(step_id) is None:
        raise JournalIntegrityError("journal_step_id_invalid")
    if document.get("event") not in EVENTS:
        raise JournalIntegrityError("journal_event_invalid")


def _entry_from_document(document: dict[str, object]) -> JournalEntry:
    _validate_scalar_fields(document)
    asserted_hash = document["entry_sha256"]
    hash_input = dict(document)
    del hash_input["entry_sha256"]
    if _sha256(_canonical_bytes(hash_input)) != asserted_hash:
        raise JournalIntegrityError("journal_entry_hash_mismatch")
    return JournalEntry(
        binding_sha256=str(document["binding_sha256"]),
        sequence=int(document["sequence"]),
        operation=str(document["operation"]),
        attempt_id=str(document["attempt_id"]),
        step_id=str(document["step_id"]),
        event=str(document["event"]),
        disposable_authorization_sha256=str(
            document["disposable_authorization_sha256"]
        ),
        prior_entry_sha256=str(document["prior_entry_sha256"]),
        entry_sha256=str(asserted_hash),
    )


def parse_journal_bytes(
    content: bytes,
    *,
    binding_sha256: str,
) -> tuple[JournalEntry, ...]:
    """Parse and validate the complete chain, rejecting partial final writes."""

    if HASH_RE.fullmatch(binding_sha256) is None:
        raise JournalIntegrityError("journal_binding_invalid")
    if len(content) > MAX_JOURNAL_BYTES:
        raise JournalIntegrityError("journal_too_large")
    if content and not content.endswith(b"\n"):
        raise JournalIntegrityError("journal_truncated")
    entries: list[JournalEntry] = []
    prior = ZERO_HEAD
    for sequence, raw_line in enumerate(content.splitlines(), start=1):
        if not raw_line or len(raw_line) > MAX_ENTRY_BYTES:
            raise JournalIntegrityError("journal_entry_size_invalid")
        try:
            document = json.loads(
                raw_line.decode("ascii"),
                object_pairs_hook=_strict_object,
            )
        except JournalIntegrityError:
            raise
        except (UnicodeError, json.JSONDecodeError) as error:
            raise JournalIntegrityError("journal_json_invalid") from error
        if not isinstance(document, dict):
            raise JournalIntegrityError("journal_entry_shape_invalid")
        if _canonical_bytes(document) != raw_line:
            raise JournalIntegrityError("journal_json_not_canonical")
        entry = _entry_from_document(document)
        if entry.binding_sha256 != binding_sha256:
            raise JournalIntegrityError("journal_binding_mismatch")
        if entry.sequence != sequence:
            raise JournalIntegrityError("journal_sequence_mismatch")
        if entry.prior_entry_sha256 != prior:
            raise JournalIntegrityError("journal_prior_hash_mismatch")
        prior = entry.entry_sha256
        entries.append(entry)
    return tuple(entries)


class FileJournal:
    """Locked append-only journal with strict filesystem invariants.

    A caller reopening an existing journal should pass the head and sequence
    from its last sealed receipt. That detects removal of a clean line suffix;
    the internal chain detects mutation, reordering, insertion, and partial
    truncation.
    """

    def __init__(
        self,
        path: Path,
        *,
        binding_sha256: str,
        expected_head_sha256: str | None = None,
        expected_sequence: int | None = None,
    ) -> None:
        if HASH_RE.fullmatch(binding_sha256) is None:
            raise JournalSecurityError("journal_binding_invalid")
        if (expected_head_sha256 is None) != (expected_sequence is None):
            raise JournalSecurityError("journal_expected_head_incomplete")
        if expected_head_sha256 is not None and (
            HASH_RE.fullmatch(expected_head_sha256) is None
            or not isinstance(expected_sequence, int)
            or isinstance(expected_sequence, bool)
            or expected_sequence < 0
        ):
            raise JournalSecurityError("journal_expected_head_invalid")
        self.path = Path(path)
        self.binding_sha256 = binding_sha256
        self._fd = -1
        self._inode: tuple[int, int] | None = None
        self._entries: tuple[JournalEntry, ...] = ()
        self._open(expected_head_sha256, expected_sequence)

    def _open(
        self,
        expected_head_sha256: str | None,
        expected_sequence: int | None,
    ) -> None:
        try:
            parent = self.path.parent.stat(follow_symlinks=False)
        except OSError as error:
            raise JournalSecurityError("journal_directory_invalid") from error
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != os.geteuid()
        ):
            raise JournalSecurityError("journal_directory_invalid")
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow == 0:
            raise JournalSecurityError("journal_nofollow_unavailable")
        flags = os.O_RDWR | os.O_APPEND | os.O_CLOEXEC | nofollow
        created = False
        try:
            self._fd = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            try:
                self._fd = os.open(self.path, flags)
            except OSError as error:
                raise JournalSecurityError("journal_file_invalid") from error
        except OSError as error:
            raise JournalSecurityError("journal_file_invalid") from error
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            os.close(self._fd)
            self._fd = -1
            raise JournalBusyError("journal_locked") from error
        try:
            if created:
                os.fchmod(self._fd, 0o600)
                os.fsync(self._fd)
                directory_fd = os.open(
                    self.path.parent,
                    os.O_RDONLY | os.O_CLOEXEC | nofollow,
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            self._validate_open_file()
            self._entries = self._read_validated()
            head = self.head_sha256
            if expected_sequence is not None and (
                len(self._entries) != expected_sequence
                or head != expected_head_sha256
            ):
                raise JournalIntegrityError("journal_sealed_head_mismatch")
        except BaseException:
            self.close()
            raise

    def _validate_open_file(self) -> None:
        try:
            opened = os.fstat(self._fd)
            named = self.path.stat(follow_symlinks=False)
        except OSError as error:
            raise JournalSecurityError("journal_file_invalid") from error
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or opened.st_uid != os.geteuid()
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise JournalSecurityError("journal_file_invalid")
        current = (opened.st_dev, opened.st_ino)
        if self._inode is not None and current != self._inode:
            raise JournalSecurityError("journal_path_replaced")
        self._inode = current

    def _read_validated(self) -> tuple[JournalEntry, ...]:
        self._validate_open_file()
        try:
            size = os.fstat(self._fd).st_size
            if size > MAX_JOURNAL_BYTES:
                raise JournalIntegrityError("journal_too_large")
            os.lseek(self._fd, 0, os.SEEK_SET)
            remaining = size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(self._fd, min(remaining, 65536))
                if not chunk:
                    raise JournalIntegrityError("journal_short_read")
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError as error:
            raise JournalIntegrityError("journal_read_failed") from error
        return parse_journal_bytes(
            b"".join(chunks), binding_sha256=self.binding_sha256
        )

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        observed = self._read_validated()
        if observed != self._entries:
            raise JournalIntegrityError("journal_changed_while_locked")
        return observed

    @property
    def sequence(self) -> int:
        return len(self._entries)

    @property
    def head_sha256(self) -> str:
        return self._entries[-1].entry_sha256 if self._entries else ZERO_HEAD

    def append(
        self,
        *,
        operation: str,
        attempt_id: str,
        step_id: str,
        event: str,
        disposable_authorization_sha256: str,
    ) -> JournalEntry:
        if self._read_validated() != self._entries:
            raise JournalIntegrityError("journal_changed_while_locked")
        sequence = len(self._entries) + 1
        document: dict[str, object] = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "binding_sha256": self.binding_sha256,
            "sequence": sequence,
            "operation": operation,
            "attempt_id": attempt_id,
            "step_id": step_id,
            "event": event,
            "disposable_authorization_sha256": disposable_authorization_sha256,
            "prior_entry_sha256": self.head_sha256,
        }
        document["entry_sha256"] = _sha256(_canonical_bytes(document))
        entry = _entry_from_document(document)
        encoded = _canonical_bytes(document) + b"\n"
        try:
            offset = 0
            while offset < len(encoded):
                written = os.write(self._fd, encoded[offset:])
                if written <= 0:
                    raise OSError("short journal write")
                offset += written
            os.fsync(self._fd)
        except OSError as error:
            raise JournalIntegrityError("journal_append_failed") from error
        self._entries = (*self._entries, entry)
        if self._read_validated() != self._entries:
            raise JournalIntegrityError("journal_post_append_mismatch")
        return entry

    def close(self) -> None:
        if self._fd >= 0:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = -1

    def __enter__(self) -> FileJournal:
        return self

    def __exit__(self, *unused: object) -> None:
        self.close()

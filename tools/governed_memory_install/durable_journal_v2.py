from __future__ import annotations

"""One-entry file/anchor recovery journal for the Phase 8B controller.

The file is append-and-fsync first; the content-free ``AuthorityState`` anchor
is advanced second.  Reopening accepts only equality or exactly one complete
file entry ahead of the anchor.  The latter is the sole repair path for a
crash between the two durable commits.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Final

from .authority_state import (
    AuthorityState,
    AuthorityStateError,
    HASH_RE,
    ZERO_HEAD,
)
from .controller_v2 import (
    ATTEMPT_STEP_ID,
    JournalEvent,
    JournalRecord,
)
from .execution_capability_v2 import (
    ClaimedExecutionBindingError,
    _claimed_execution_binding_evidence,
)
from .execution_lock import (
    ExecutionLockError,
    HeldExecutionLockCapability,
    validate_held_execution_lock,
)


JOURNAL_SCHEMA_VERSION: Final = "governed-memory-phase8b-journal-v1"
_JOURNAL_KEYS: Final = frozenset(
    {
        "schema_version",
        "plan_sha256",
        "attempt_id",
        "sequence",
        "step_id",
        "event",
        "prior_record_sha256",
        "record_sha256",
    }
)
MAX_JOURNAL_BYTES: Final = 16 * 1024 * 1024
MAX_RECORD_BYTES: Final = 4096


class DurableJournalV2Error(RuntimeError):
    """Base class for fail-closed durable journal failures."""


class DurableJournalV2SecurityError(DurableJournalV2Error):
    pass


class DurableJournalV2IntegrityError(DurableJournalV2Error):
    pass


class DurableJournalV2BusyError(DurableJournalV2Error):
    pass


class DurableJournalV2AnchorError(DurableJournalV2Error):
    pass


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise DurableJournalV2IntegrityError(
            "phase8b_durable_journal_json_invalid"
        ) from error


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_duplicate_key"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise DurableJournalV2IntegrityError(
        "phase8b_durable_journal_json_invalid"
    )


def _record_document(record: JournalRecord) -> dict[str, object]:
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "plan_sha256": record.plan_sha256,
        "attempt_id": record.attempt_id,
        "sequence": record.sequence,
        "step_id": record.step_id,
        "event": record.event,
        "prior_record_sha256": record.prior_record_sha256,
        "record_sha256": record.record_sha256,
    }


def _record_from_document(
    document: dict[str, object],
    *,
    expected_plan_sha256: str,
    expected_attempt_id: str,
) -> JournalRecord:
    if set(document) != _JOURNAL_KEYS:
        raise DurableJournalV2IntegrityError(
            "phase8b_durable_journal_record_shape_invalid"
        )
    if document.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        raise DurableJournalV2IntegrityError(
            "phase8b_durable_journal_schema_invalid"
        )
    if document.get("plan_sha256") != expected_plan_sha256:
        raise DurableJournalV2IntegrityError(
            "phase8b_durable_journal_plan_mismatch"
        )
    if document.get("attempt_id") != expected_attempt_id:
        raise DurableJournalV2IntegrityError(
            "phase8b_durable_journal_attempt_mismatch"
        )
    sequence = document.get("sequence")
    if type(sequence) is not int or sequence < 1:
        raise DurableJournalV2IntegrityError(
            "phase8b_durable_journal_sequence_invalid"
        )
    for key in ("prior_record_sha256", "record_sha256"):
        value = document.get(key)
        if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_hash_invalid"
            )
    step_id = document.get("step_id")
    if not isinstance(step_id, str) or not (
        step_id == ATTEMPT_STEP_ID
        or (
            len(step_id) >= 5
            and step_id[0] == "I"
            and step_id[1:3].isdigit()
            and step_id[3] == "_"
            and all(character.isupper() or character.isdigit() or character == "_"
                    for character in step_id[4:])
        )
    ):
        raise DurableJournalV2IntegrityError(
            "phase8b_durable_journal_step_invalid"
        )
    try:
        event = JournalEvent(document.get("event"))
    except (TypeError, ValueError) as error:
        raise DurableJournalV2IntegrityError(
            "phase8b_durable_journal_event_invalid"
        ) from error
    asserted = JournalRecord(
        plan_sha256=expected_plan_sha256,
        attempt_id=expected_attempt_id,
        sequence=sequence,
        step_id=step_id,
        event=event.value,
        prior_record_sha256=document["prior_record_sha256"],  # type: ignore[arg-type]
        record_sha256=document["record_sha256"],  # type: ignore[arg-type]
    )
    calculated = JournalRecord.create(
        plan_sha256=asserted.plan_sha256,
        attempt_id=asserted.attempt_id,
        sequence=asserted.sequence,
        step_id=asserted.step_id,
        event=event,
        prior_record_sha256=asserted.prior_record_sha256,
    )
    if asserted != calculated:
        raise DurableJournalV2IntegrityError(
            "phase8b_durable_journal_record_hash_invalid"
        )
    return asserted


def parse_durable_journal_bytes(
    content: bytes,
    *,
    expected_plan_sha256: str,
    expected_attempt_id: str,
) -> tuple[JournalRecord, ...]:
    if not isinstance(expected_plan_sha256, str) or HASH_RE.fullmatch(
        expected_plan_sha256
    ) is None:
        raise DurableJournalV2IntegrityError(
            "phase8b_durable_journal_plan_invalid"
        )
    if len(content) > MAX_JOURNAL_BYTES:
        raise DurableJournalV2IntegrityError(
            "phase8b_durable_journal_too_large"
        )
    if content and not content.endswith(b"\n"):
        raise DurableJournalV2IntegrityError(
            "phase8b_durable_journal_truncated"
        )
    records: list[JournalRecord] = []
    prior = ZERO_HEAD
    for sequence, raw_line in enumerate(content.splitlines(), start=1):
        if not raw_line or len(raw_line) > MAX_RECORD_BYTES:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_record_size_invalid"
            )
        try:
            document = json.loads(
                raw_line.decode("ascii"),
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except DurableJournalV2IntegrityError:
            raise
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_json_invalid"
            ) from error
        if type(document) is not dict or _canonical_bytes(document) != raw_line:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_json_not_canonical"
            )
        record = _record_from_document(
            document,
            expected_plan_sha256=expected_plan_sha256,
            expected_attempt_id=expected_attempt_id,
        )
        if record.sequence != sequence:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_sequence_mismatch"
            )
        if record.prior_record_sha256 != prior:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_chain_mismatch"
            )
        records.append(record)
        prior = record.record_sha256
    return tuple(records)


class DurableJournalV2:
    """Locked one-file controller journal anchored in ``AuthorityState``."""

    def __init__(
        self,
        path: Path,
        *,
        claimed_execution_binding: object,
        authority_state: AuthorityState,
        held_lock: HeldExecutionLockCapability,
        expected_uid: int | None = None,
        create: bool = False,
    ) -> None:
        # Token validation intentionally precedes every journal-path access.
        try:
            binding = _claimed_execution_binding_evidence(
                claimed_execution_binding
            )
        except ClaimedExecutionBindingError as error:
            raise DurableJournalV2SecurityError(
                "phase8b_durable_journal_binding_invalid"
            ) from error
        if type(authority_state) is not AuthorityState:
            raise DurableJournalV2SecurityError(
                "phase8b_durable_journal_authority_state_invalid"
            )
        try:
            validate_held_execution_lock(held_lock)
        except ExecutionLockError as error:
            raise DurableJournalV2SecurityError(
                "phase8b_durable_journal_lock_not_held"
            ) from error
        lock_path_sha256 = hashlib.sha256(
            str(held_lock._owner.path).encode("utf-8")
        ).hexdigest()
        if lock_path_sha256 != binding.global_lock_path_sha256:
            raise DurableJournalV2SecurityError(
                "phase8b_durable_journal_lock_binding_mismatch"
            )
        state_path_sha256 = hashlib.sha256(
            str(authority_state.path).encode("utf-8")
        ).hexdigest()
        if state_path_sha256 != binding.authority_state_path_sha256:
            raise DurableJournalV2SecurityError(
                "phase8b_durable_journal_authority_state_binding_mismatch"
            )
        uid = os.geteuid() if expected_uid is None else expected_uid
        if type(uid) is not int or uid < 0:
            raise DurableJournalV2SecurityError(
                "phase8b_durable_journal_uid_invalid"
            )
        if type(create) is not bool:
            raise DurableJournalV2SecurityError(
                "phase8b_durable_journal_create_invalid"
            )
        self.path = Path(path)
        if (
            not self.path.name
            or self.path.name in {".", ".."}
            or str(self.path) != binding.execution_journal_path
        ):
            raise DurableJournalV2SecurityError(
                "phase8b_durable_journal_path_invalid"
            )
        self.expected_uid = uid
        self.authority_state = authority_state
        self.held_lock = held_lock
        self.binding_sha256 = binding.journal_binding_sha256
        self.plan_sha256 = binding.controller_model_sha256
        self.attempt_id = binding.attempt_id
        self.execution_id = binding.execution_id
        self._fd = -1
        self._directory_inode: tuple[int, int] | None = None
        self._file_inode: tuple[int, int] | None = None
        self._records: tuple[JournalRecord, ...] = ()
        self._last_anchor_result = "unreconciled"
        self._open(create=create)

    def _require_lock(self) -> None:
        try:
            validate_held_execution_lock(self.held_lock)
        except ExecutionLockError as error:
            raise DurableJournalV2SecurityError(
                "phase8b_durable_journal_lock_not_held"
            ) from error

    @staticmethod
    def _nofollow() -> int:
        value = getattr(os, "O_NOFOLLOW", 0)
        if value == 0:
            raise DurableJournalV2SecurityError(
                "phase8b_durable_journal_nofollow_unavailable"
            )
        return value

    def _open_directory(self) -> int:
        self._require_lock()
        flags = os.O_RDONLY | os.O_CLOEXEC | self._nofollow()
        flags |= getattr(os, "O_DIRECTORY", 0)
        try:
            directory_fd = os.open(self.path.parent, flags)
        except OSError as error:
            raise DurableJournalV2SecurityError(
                "phase8b_durable_journal_directory_invalid"
            ) from error
        try:
            opened = os.fstat(directory_fd)
            named = self.path.parent.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o700
                or opened.st_uid != self.expected_uid
                or (opened.st_dev, opened.st_ino)
                != (named.st_dev, named.st_ino)
            ):
                raise DurableJournalV2SecurityError(
                    "phase8b_durable_journal_directory_invalid"
                )
            inode = (opened.st_dev, opened.st_ino)
            if self._directory_inode is not None and inode != self._directory_inode:
                raise DurableJournalV2SecurityError(
                    "phase8b_durable_journal_directory_replaced"
                )
            self._directory_inode = inode
            self._require_lock()
            return directory_fd
        except BaseException:
            os.close(directory_fd)
            raise

    def _validate_file(self, directory_fd: int) -> None:
        self._require_lock()
        try:
            opened = os.fstat(self._fd)
            named = os.stat(
                self.path.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise DurableJournalV2SecurityError(
                "phase8b_durable_journal_file_invalid"
            ) from error
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != self.expected_uid
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise DurableJournalV2SecurityError(
                "phase8b_durable_journal_file_invalid"
            )
        inode = (opened.st_dev, opened.st_ino)
        if self._file_inode is not None and inode != self._file_inode:
            raise DurableJournalV2SecurityError(
                "phase8b_durable_journal_file_replaced"
            )
        self._file_inode = inode
        self._require_lock()

    def _open(self, *, create: bool) -> None:
        self._require_lock()
        directory_fd = self._open_directory()
        flags = os.O_RDWR | os.O_APPEND | os.O_CLOEXEC | self._nofollow()
        try:
            try:
                if create:
                    self._fd = os.open(
                        self.path.name,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=directory_fd,
                    )
                else:
                    self._fd = os.open(
                        self.path.name,
                        flags,
                        dir_fd=directory_fd,
                    )
            except OSError as error:
                raise DurableJournalV2SecurityError(
                    "phase8b_durable_journal_file_invalid"
                ) from error
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as error:
                raise DurableJournalV2BusyError(
                    "phase8b_durable_journal_locked"
                ) from error
            if create:
                os.fchmod(self._fd, 0o600)
                os.fsync(self._fd)
                os.fsync(directory_fd)
            self._validate_file(directory_fd)
            self._require_lock()
        except BaseException:
            self.close()
            raise
        finally:
            os.close(directory_fd)
        try:
            self._records = self._read_validated()
            self._reconcile_anchor()
        except BaseException:
            self.close()
            raise

    def _read_validated(self) -> tuple[JournalRecord, ...]:
        self._require_lock()
        directory_fd = self._open_directory()
        try:
            self._validate_file(directory_fd)
            size = os.fstat(self._fd).st_size
            if size > MAX_JOURNAL_BYTES:
                raise DurableJournalV2IntegrityError(
                    "phase8b_durable_journal_too_large"
                )
            os.lseek(self._fd, 0, os.SEEK_SET)
            remaining = size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(self._fd, min(remaining, 65536))
                if not chunk:
                    raise DurableJournalV2IntegrityError(
                        "phase8b_durable_journal_short_read"
                    )
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError as error:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_read_failed"
            ) from error
        finally:
            os.close(directory_fd)
        records = parse_durable_journal_bytes(
            b"".join(chunks),
            expected_plan_sha256=self.plan_sha256,
            expected_attempt_id=self.attempt_id,
        )
        self._require_lock()
        return records

    def _reconcile_anchor(self) -> None:
        self._require_lock()
        sequence = len(self._records)
        head = self._records[-1].record_sha256 if self._records else ZERO_HEAD
        prior = (
            self._records[-1].prior_record_sha256
            if self._records
            else ZERO_HEAD
        )
        try:
            result = self.authority_state.advance_anchor(
                self.binding_sha256,
                journal_sequence=sequence,
                journal_head_sha256=head,
                journal_prior_head_sha256=prior,
            )
            self._require_lock()
        except AuthorityStateError as error:
            raise DurableJournalV2AnchorError(
                "phase8b_durable_journal_anchor_mismatch"
            ) from error
        self._last_anchor_result = result.result

    @property
    def anchor_result(self) -> str:
        return self._last_anchor_result

    def journal_records(self) -> tuple[JournalRecord, ...]:
        self._require_lock()
        observed = self._read_validated()
        if observed != self._records:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_changed_while_locked"
            )
        self._reconcile_anchor()
        return observed

    def append_journal(self, record: JournalRecord) -> None:
        self._require_lock()
        before = self.journal_records()
        if type(record) is not JournalRecord:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_record_type_invalid"
            )
        expected_prior = before[-1].record_sha256 if before else ZERO_HEAD
        try:
            event = JournalEvent(record.event)
        except (TypeError, ValueError) as error:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_event_invalid"
            ) from error
        expected = JournalRecord.create(
            plan_sha256=self.plan_sha256,
            attempt_id=self.attempt_id,
            sequence=len(before) + 1,
            step_id=record.step_id,
            event=event,
            prior_record_sha256=expected_prior,
        )
        if record != expected:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_append_binding_invalid"
            )
        encoded = _canonical_bytes(_record_document(record)) + b"\n"
        if len(encoded) > MAX_RECORD_BYTES:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_record_size_invalid"
            )
        current_size = os.fstat(self._fd).st_size
        if current_size + len(encoded) > MAX_JOURNAL_BYTES:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_too_large"
            )
        try:
            self._require_lock()
            offset = 0
            while offset < len(encoded):
                written = os.write(self._fd, encoded[offset:])
                if written <= 0:
                    raise OSError("short journal write")
                offset += written
            os.fsync(self._fd)
            self._require_lock()
        except OSError as error:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_append_failed"
            ) from error
        self._records = (*before, record)
        if self._read_validated() != self._records:
            raise DurableJournalV2IntegrityError(
                "phase8b_durable_journal_post_append_mismatch"
            )
        self._reconcile_anchor()

    def close(self) -> None:
        if self._fd >= 0:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = -1

    def __enter__(self) -> DurableJournalV2:
        return self

    def __exit__(self, *unused: object) -> None:
        self.close()


__all__ = [
    "DurableJournalV2",
    "DurableJournalV2AnchorError",
    "DurableJournalV2BusyError",
    "DurableJournalV2Error",
    "DurableJournalV2IntegrityError",
    "DurableJournalV2SecurityError",
    "parse_durable_journal_bytes",
]

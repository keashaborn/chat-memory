from __future__ import annotations

"""Fsynced, anchored journal for the empty-store rollback controller."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Final

from .authority_state import AuthorityState, AuthorityStateError, ZERO_HEAD
from .execution_lock import (
    ExecutionLockError,
    HeldExecutionLockCapability,
    validate_held_execution_lock,
)
from .rollback_entrypoint import (
    ROLLBACK_JOURNAL_SCHEMA,
    EmptyRollbackExecutionError,
    RollbackEvent,
    RollbackJournalRecord,
    claimed_empty_rollback_evidence,
    validate_rollback_records,
)


MAX_ROLLBACK_JOURNAL_BYTES: Final = 4 * 1024 * 1024
MAX_ROLLBACK_RECORD_BYTES: Final = 4096
MAX_ROLLBACK_PLAN_STEPS: Final = 22
_RECORD_KEYS: Final = {
    "schema_version",
    "plan_sha256",
    "attempt_id",
    "sequence",
    "step_id",
    "event",
    "prior_record_sha256",
    "record_sha256",
}


class DurableRollbackJournalError(RuntimeError):
    """Content-free refusal for durable rollback journal integrity."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise DurableRollbackJournalError(
            "empty_rollback_journal_json_invalid"
        ) from error


def _record_document(record: RollbackJournalRecord) -> dict[str, object]:
    return {
        "schema_version": ROLLBACK_JOURNAL_SCHEMA,
        "plan_sha256": record.plan_sha256,
        "attempt_id": record.attempt_id,
        "sequence": record.sequence,
        "step_id": record.step_id,
        "event": record.event,
        "prior_record_sha256": record.prior_record_sha256,
        "record_sha256": record.record_sha256,
    }


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_duplicate_key"
            )
        value[key] = item
    return value


def parse_rollback_journal_bytes(
    raw: bytes,
    *,
    plan_sha256: str,
    attempt_id: str,
) -> tuple[RollbackJournalRecord, ...]:
    if len(raw) > MAX_ROLLBACK_JOURNAL_BYTES or (
        raw and not raw.endswith(b"\n")
    ):
        raise DurableRollbackJournalError("empty_rollback_journal_size_invalid")
    records: list[RollbackJournalRecord] = []
    for line in raw.splitlines():
        if not line or len(line) > MAX_ROLLBACK_RECORD_BYTES:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_record_size_invalid"
            )
        try:
            value = json.loads(
                line.decode("ascii"),
                object_pairs_hook=_strict_object,
                parse_constant=lambda unused: (_ for _ in ()).throw(
                    DurableRollbackJournalError(
                        "empty_rollback_journal_json_invalid"
                    )
                ),
            )
        except DurableRollbackJournalError:
            raise
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_json_invalid"
            ) from error
        if type(value) is not dict or set(value) != _RECORD_KEYS or _canonical(
            value
        ) != line:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_record_invalid"
            )
        try:
            record = RollbackJournalRecord(
                plan_sha256=value["plan_sha256"],  # type: ignore[arg-type]
                attempt_id=value["attempt_id"],  # type: ignore[arg-type]
                sequence=value["sequence"],  # type: ignore[arg-type]
                step_id=value["step_id"],  # type: ignore[arg-type]
                event=value["event"],  # type: ignore[arg-type]
                prior_record_sha256=value["prior_record_sha256"],  # type: ignore[arg-type]
                record_sha256=value["record_sha256"],  # type: ignore[arg-type]
            )
        except TypeError as error:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_record_invalid"
            ) from error
        records.append(record)
    try:
        return validate_rollback_records(
            tuple(records), plan_sha256=plan_sha256, attempt_id=attempt_id
        )
    except EmptyRollbackExecutionError as error:
        raise DurableRollbackJournalError(
            "empty_rollback_journal_record_invalid"
        ) from error


class DurableRollbackJournal:
    """One secure file plus an AuthorityState compare-and-set anchor."""

    def __init__(
        self,
        path: Path,
        *,
        claimed_rollback: object,
        authority_state: AuthorityState,
        held_lock: HeldExecutionLockCapability,
        expected_uid: int | None = None,
        create: bool = False,
    ) -> None:
        try:
            claim = claimed_empty_rollback_evidence(claimed_rollback)
            validate_held_execution_lock(held_lock)
        except (EmptyRollbackExecutionError, ExecutionLockError) as error:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_claim_invalid"
            ) from error
        if (
            type(authority_state) is not AuthorityState
            or hashlib.sha256(str(authority_state.path).encode("utf-8")).hexdigest()
            != claim.authority_state_path_sha256
            or hashlib.sha256(str(held_lock._owner.path).encode("utf-8")).hexdigest()
            != claim.global_lock_path_sha256
            or str(path) != claim.journal_path
            or type(create) is not bool
        ):
            raise DurableRollbackJournalError(
                "empty_rollback_journal_binding_invalid"
            )
        uid = os.geteuid() if expected_uid is None else expected_uid
        if type(uid) is not int or uid < 0:
            raise DurableRollbackJournalError("empty_rollback_journal_uid_invalid")
        self.path = Path(path)
        self.plan_sha256 = claim.plan_sha256
        self.attempt_id = claim.attempt_id
        self.execution_id = claim.execution_id
        self.binding_sha256 = claim.journal_binding_sha256
        self.authority_state = authority_state
        self.held_lock = held_lock
        self.expected_uid = uid
        self._fd = -1
        self._directory_inode: tuple[int, int] | None = None
        self._file_inode: tuple[int, int] | None = None
        self._records: tuple[RollbackJournalRecord, ...] = ()
        self._reserved_record_count = 0
        self._open(create=create)

    @staticmethod
    def _nofollow() -> int:
        value = getattr(os, "O_NOFOLLOW", 0)
        if value == 0:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_nofollow_unavailable"
            )
        return value

    def _require_lock(self) -> None:
        try:
            validate_held_execution_lock(self.held_lock)
        except ExecutionLockError as error:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_lock_not_held"
            ) from error

    def _open_directory(self) -> int:
        self._require_lock()
        flags = os.O_RDONLY | os.O_CLOEXEC | self._nofollow()
        flags |= getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(self.path.parent, flags)
            opened = os.fstat(descriptor)
            named = self.path.parent.stat(follow_symlinks=False)
        except OSError as error:
            try:
                os.close(descriptor)
            except (OSError, UnboundLocalError):
                pass
            raise DurableRollbackJournalError(
                "empty_rollback_journal_directory_invalid"
            ) from error
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o700
            or opened.st_uid != self.expected_uid
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            os.close(descriptor)
            raise DurableRollbackJournalError(
                "empty_rollback_journal_directory_invalid"
            )
        inode = (opened.st_dev, opened.st_ino)
        if self._directory_inode is not None and inode != self._directory_inode:
            os.close(descriptor)
            raise DurableRollbackJournalError(
                "empty_rollback_journal_directory_replaced"
            )
        self._directory_inode = inode
        return descriptor

    def _validate_file(self, directory_fd: int) -> os.stat_result:
        self._require_lock()
        try:
            opened = os.fstat(self._fd)
            named = os.stat(
                self.path.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_file_invalid"
            ) from error
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != self.expected_uid
            or opened.st_nlink != 1
            or opened.st_size > MAX_ROLLBACK_JOURNAL_BYTES
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise DurableRollbackJournalError(
                "empty_rollback_journal_file_invalid"
            )
        inode = (opened.st_dev, opened.st_ino)
        if self._file_inode is not None and inode != self._file_inode:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_file_replaced"
            )
        self._file_inode = inode
        return opened

    def _open(self, *, create: bool) -> None:
        directory_fd = self._open_directory()
        flags = os.O_RDWR | os.O_APPEND | os.O_CLOEXEC | self._nofollow()
        try:
            try:
                self._fd = os.open(
                    self.path.name,
                    flags | (os.O_CREAT | os.O_EXCL if create else 0),
                    0o600,
                    dir_fd=directory_fd,
                )
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError) as error:
                raise DurableRollbackJournalError(
                    "empty_rollback_journal_open_failed"
                ) from error
            if create:
                os.fchmod(self._fd, 0o600)
                os.fsync(self._fd)
                os.fsync(directory_fd)
            self._validate_file(directory_fd)
            self.authority_state.seal_filesystem_identity(
                self.binding_sha256,
                artifact_kind="journal",
                path=self.path,
                directory_fd=directory_fd,
                file_fd=self._fd,
                held_lock=self.held_lock,
            )
        except BaseException:
            self.close()
            raise
        finally:
            os.close(directory_fd)
        try:
            self._records = self._read(recover_torn_tail=True)
            self._reconcile_anchor()
        except BaseException:
            self.close()
            raise

    def _read(
        self,
        *,
        recover_torn_tail: bool = False,
    ) -> tuple[RollbackJournalRecord, ...]:
        self._require_lock()
        directory_fd = self._open_directory()
        try:
            info = self._validate_file(directory_fd)
            os.lseek(self._fd, 0, os.SEEK_SET)
            remaining = info.st_size
            chunks: list[bytes] = []
            while remaining:
                block = os.read(self._fd, min(remaining, 65536))
                if not block:
                    raise DurableRollbackJournalError(
                        "empty_rollback_journal_short_read"
                    )
                chunks.append(block)
                remaining -= len(block)
        except OSError as error:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_read_failed"
            ) from error
        finally:
            os.close(directory_fd)
        raw = b"".join(chunks)
        if raw and not raw.endswith(b"\n") and recover_torn_tail:
            records = self._recover_torn_tail(raw)
        else:
            records = parse_rollback_journal_bytes(
                raw,
                plan_sha256=self.plan_sha256,
                attempt_id=self.attempt_id,
            )
        self._require_lock()
        return records

    def _recover_torn_tail(
        self,
        raw: bytes,
    ) -> tuple[RollbackJournalRecord, ...]:
        """Discard one final partial line only after an exact anchor match."""

        self._require_lock()
        final_newline = raw.rfind(b"\n")
        complete_length = final_newline + 1
        complete = raw[:complete_length]
        partial = raw[complete_length:]
        expected_prefix = (
            b'{"attempt_id":"' + self.attempt_id.encode("ascii") + b'"'
        )
        shared = min(len(partial), len(expected_prefix))
        if (
            not partial
            or len(partial) > MAX_ROLLBACK_RECORD_BYTES
            or partial[:shared] != expected_prefix[:shared]
        ):
            raise DurableRollbackJournalError(
                "empty_rollback_journal_truncated"
            )
        records = parse_rollback_journal_bytes(
            complete,
            plan_sha256=self.plan_sha256,
            attempt_id=self.attempt_id,
        )
        head = records[-1].record_sha256 if records else ZERO_HEAD
        try:
            anchor = self.authority_state.read_anchor(self.binding_sha256)
            self._require_lock()
        except AuthorityStateError as error:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_anchor_mismatch"
            ) from error
        if anchor.sequence != len(records) or anchor.head_sha256 != head:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_truncated_tail_not_exact_anchor"
            )

        directory_fd = self._open_directory()
        try:
            opened = self._validate_file(directory_fd)
            self.authority_state.seal_filesystem_identity(
                self.binding_sha256,
                artifact_kind="journal",
                path=self.path,
                directory_fd=directory_fd,
                file_fd=self._fd,
                held_lock=self.held_lock,
            )
            if opened.st_size != len(raw):
                raise DurableRollbackJournalError(
                    "empty_rollback_journal_changed_during_tail_recovery"
                )
            os.lseek(self._fd, 0, os.SEEK_SET)
            observed = bytearray()
            while len(observed) < len(raw):
                block = os.read(self._fd, len(raw) - len(observed))
                if not block:
                    break
                observed.extend(block)
            if bytes(observed) != raw:
                raise DurableRollbackJournalError(
                    "empty_rollback_journal_changed_during_tail_recovery"
                )
            self._require_lock()
            os.ftruncate(self._fd, complete_length)
            os.fsync(self._fd)
            self._require_lock()
            truncated = self._validate_file(directory_fd)
            self.authority_state.seal_filesystem_identity(
                self.binding_sha256,
                artifact_kind="journal",
                path=self.path,
                directory_fd=directory_fd,
                file_fd=self._fd,
                held_lock=self.held_lock,
            )
            if truncated.st_size != complete_length:
                raise DurableRollbackJournalError(
                    "empty_rollback_journal_tail_recovery_failed"
                )
        except AuthorityStateError as error:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_filesystem_identity_mismatch"
            ) from error
        except OSError as error:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_tail_recovery_failed"
            ) from error
        finally:
            os.close(directory_fd)
        self._require_lock()
        return records

    def _reconcile_anchor(self) -> None:
        self._require_lock()
        head = self._records[-1].record_sha256 if self._records else ZERO_HEAD
        prior = (
            self._records[-1].prior_record_sha256 if self._records else ZERO_HEAD
        )
        try:
            self.authority_state.advance_anchor(
                self.binding_sha256,
                journal_sequence=len(self._records),
                journal_head_sha256=head,
                journal_prior_head_sha256=prior,
                held_lock=self.held_lock,
            )
        except AuthorityStateError as error:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_anchor_mismatch"
            ) from error

    def records(self) -> tuple[RollbackJournalRecord, ...]:
        observed = self._read()
        if observed != self._records:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_changed_while_locked"
            )
        self._reconcile_anchor()
        return self._records

    def reserve_effect_capacity(self, remaining_record_count: int) -> None:
        self._require_lock()
        if (
            type(remaining_record_count) is not int
            or remaining_record_count < 1
            or remaining_record_count > 2 * MAX_ROLLBACK_PLAN_STEPS + 1
        ):
            raise DurableRollbackJournalError(
                "empty_rollback_journal_reservation_invalid"
            )
        directory_fd = self._open_directory()
        try:
            size = self._validate_file(directory_fd).st_size
        finally:
            os.close(directory_fd)
        if (
            size + remaining_record_count * MAX_ROLLBACK_RECORD_BYTES
            > MAX_ROLLBACK_JOURNAL_BYTES
        ):
            raise DurableRollbackJournalError(
                "empty_rollback_journal_recovery_capacity_unavailable"
            )
        self._reserved_record_count = remaining_record_count

    def append(self, record: RollbackJournalRecord) -> None:
        before = self.records()
        if self._reserved_record_count < 1:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_capacity_not_reserved"
            )
        if type(record) is not RollbackJournalRecord:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_append_binding_invalid"
            )
        try:
            event = RollbackEvent(record.event)
        except ValueError as error:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_append_binding_invalid"
            ) from error
        expected = RollbackJournalRecord.create(
            plan_sha256=self.plan_sha256,
            attempt_id=self.attempt_id,
            sequence=len(before) + 1,
            step_id=record.step_id,
            event=event,
            prior_record_sha256=(
                before[-1].record_sha256 if before else ZERO_HEAD
            ),
        )
        if record != expected:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_append_binding_invalid"
            )
        encoded = _canonical(_record_document(record)) + b"\n"
        directory_fd = self._open_directory()
        try:
            size = self._validate_file(directory_fd).st_size
        finally:
            os.close(directory_fd)
        if (
            len(encoded) > MAX_ROLLBACK_RECORD_BYTES
            or size + len(encoded)
            + (self._reserved_record_count - 1) * MAX_ROLLBACK_RECORD_BYTES
            > MAX_ROLLBACK_JOURNAL_BYTES
        ):
            raise DurableRollbackJournalError(
                "empty_rollback_journal_recovery_capacity_unavailable"
            )
        try:
            written = 0
            while written < len(encoded):
                count = os.write(self._fd, encoded[written:])
                if count <= 0:
                    raise OSError("short write")
                written += count
            os.fsync(self._fd)
        except OSError as error:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_append_failed"
            ) from error
        self._reserved_record_count -= 1
        self._records = (*before, record)
        if self._read() != self._records:
            raise DurableRollbackJournalError(
                "empty_rollback_journal_post_append_mismatch"
            )
        self._reconcile_anchor()

    def close(self) -> None:
        if self._fd >= 0:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = -1

    def __enter__(self) -> DurableRollbackJournal:
        return self

    def __exit__(self, *unused: object) -> None:
        self.close()


__all__ = [
    "DurableRollbackJournal",
    "DurableRollbackJournalError",
    "MAX_ROLLBACK_JOURNAL_BYTES",
    "MAX_ROLLBACK_RECORD_BYTES",
    "parse_rollback_journal_bytes",
]

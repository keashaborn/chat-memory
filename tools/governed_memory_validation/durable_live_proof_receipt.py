from __future__ import annotations

"""Create-once durable storage for the one promotable Phase 9 live proof.

The fixed production file is external retained evidence.  It is written by the
sealed runner only after the terminal live receipt has verified and before the
runner emits success.  Recovery-only receipts are structurally rejected.
"""

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Final, Mapping


PROMOTABLE_LIVE_PROOF_RECEIPT_PATH: Final = Path(
    "/var/lib/governed-memory-controller/"
    "phase9-disposable-live-proof-promotable-receipt-000005.json"
)
PROMOTABLE_LIVE_PROOF_RECEIPT_STAGING_SUFFIX: Final = ".publishing"
PROMOTABLE_LIVE_PROOF_RECEIPT_STAGING_PATH: Final = Path(
    str(PROMOTABLE_LIVE_PROOF_RECEIPT_PATH)
    + PROMOTABLE_LIVE_PROOF_RECEIPT_STAGING_SUFFIX
)
LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE: Final = (
    "ops/governed_memory/installation/current/live_proof_receipt.schema.json"
)
LIVE_PROOF_RECEIPT_SCHEMA: Final = "governed-memory-phase9-live-proof-receipt-v6"
MAX_PROMOTABLE_LIVE_PROOF_RECEIPT_BYTES: Final = 64 * 1024
ANONYMOUS_PUBLICATION_PREFLIGHT_BYTES: Final = (
    b"phase9-live-proof-anonymous-publication-preflight-v1\n"
)
AT_EMPTY_PATH: Final = 0x1000
EXPECTED_AUTHORIZATION_TEXT_SHA256: Final = hashlib.sha256(
    b"Authorized to move on and finish nine whatever it takes"
).hexdigest()

_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_RECEIPT_KEYS: Final = frozenset(
    {
        "schema_version",
        "result",
        "observation_scope",
        "candidate_git_commit",
        "candidate_git_tree",
        "package_manifest_sha256",
        "controller_runtime_receipt_sha256",
        "live_proof_receipt_schema_sha256",
        "recovery_capsule_sha256",
        "recovery_reservation_claim_sha256",
        "install_authority_claim_sha256",
        "start_authority_pair_claimed_atomically",
        "recovery_capsule_published_before_first_install_effect",
        "recovery_reservation_claimed_before_first_install_effect",
        "ephemeral_private_signer_retained_at_execution_start",
        "recovery_capsule_retained_at_terminal_observation",
        "authorization_text_sha256",
        "installation_execution_id",
        "installation_receipt_sha256",
        "image_identity_set_sha256",
        "empty_rollback_execution_id",
        "empty_rollback_receipt_sha256",
        "retained_audit_set_sha256",
        "install_process_death_boundary_sha256",
        "install_process_death_arm_receipt_sha256",
        "rollback_process_death_boundary_sha256",
        "rollback_process_death_arm_receipt_sha256",
        "install_process_death_observed",
        "install_exact_resume_completed",
        "cold_controller_process_restart_terminal_postflight_verified",
        "cold_restart_scope",
        "host_reboot_proven",
        "persistent_store_restart_supervision_and_boot_recovery_proven",
        "rollback_process_death_observed",
        "rollback_exact_resume_completed",
        "public_install_entrypoint_used",
        "public_empty_rollback_entrypoint_used",
        "fresh_r06_semantic_empty_recheck_required",
        "completed_public_rollback_replayed",
        "exact_rollback_resources_absent_count",
        "exact_resources_absent_at_terminal_observation",
        "terminal_absence_is_continuous_guarantee",
        "stores_installed_at_terminal_observation",
        "stores_supervisor_installed_at_terminal_observation",
        "controller_runtime_capability_reverified",
        "host_clock_synchronization_preflight_passed",
        "source_postgres_read_count",
        "source_postgres_write_count",
        "provider_calls",
        "production_data_read",
        "application_services_installed",
        "activation_performed",
        "issuer_or_host_death_durable_cleanup_proven",
        "receipt_sha256",
    }
)
_HASH_FIELDS: Final = (
    "package_manifest_sha256",
    "controller_runtime_receipt_sha256",
    "live_proof_receipt_schema_sha256",
    "recovery_capsule_sha256",
    "recovery_reservation_claim_sha256",
    "install_authority_claim_sha256",
    "authorization_text_sha256",
    "installation_execution_id",
    "installation_receipt_sha256",
    "image_identity_set_sha256",
    "empty_rollback_execution_id",
    "empty_rollback_receipt_sha256",
    "retained_audit_set_sha256",
    "install_process_death_boundary_sha256",
    "install_process_death_arm_receipt_sha256",
    "rollback_process_death_boundary_sha256",
    "rollback_process_death_arm_receipt_sha256",
    "receipt_sha256",
)
_REQUIRED_TRUE: Final = (
    "start_authority_pair_claimed_atomically",
    "install_process_death_observed",
    "install_exact_resume_completed",
    "cold_controller_process_restart_terminal_postflight_verified",
    "rollback_process_death_observed",
    "rollback_exact_resume_completed",
    "public_install_entrypoint_used",
    "public_empty_rollback_entrypoint_used",
    "fresh_r06_semantic_empty_recheck_required",
    "completed_public_rollback_replayed",
    "exact_resources_absent_at_terminal_observation",
    "controller_runtime_capability_reverified",
    "host_clock_synchronization_preflight_passed",
    "recovery_capsule_published_before_first_install_effect",
    "recovery_reservation_claimed_before_first_install_effect",
    "recovery_capsule_retained_at_terminal_observation",
)
_REQUIRED_FALSE: Final = (
    "ephemeral_private_signer_retained_at_execution_start",
    "host_reboot_proven",
    "persistent_store_restart_supervision_and_boot_recovery_proven",
    "terminal_absence_is_continuous_guarantee",
    "stores_installed_at_terminal_observation",
    "stores_supervisor_installed_at_terminal_observation",
    "production_data_read",
    "application_services_installed",
    "activation_performed",
    "issuer_or_host_death_durable_cleanup_proven",
)


class DurableLiveProofReceiptError(RuntimeError):
    """Content-free refusal for promotable receipt drift or I/O failure."""


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
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_invalid"
        ) from error


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_invalid"
            )
        result[key] = value
    return result


def _input_bindings(inputs: object) -> tuple[str, str, str, str]:
    try:
        values = (
            getattr(inputs, "candidate_git_commit"),
            getattr(inputs, "candidate_git_tree"),
            getattr(inputs, "package_manifest_sha256"),
            getattr(inputs, "controller_runtime_receipt_sha256"),
        )
    except (AttributeError, TypeError) as error:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_binding_invalid"
        ) from error
    if (
        any(type(value) is not str for value in values)
        or _COMMIT_RE.fullmatch(values[0]) is None
        or _COMMIT_RE.fullmatch(values[1]) is None
        or _HASH_RE.fullmatch(values[2]) is None
        or _HASH_RE.fullmatch(values[3]) is None
    ):
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_binding_invalid"
        )
    return values


def verify_promotable_live_proof_receipt(
    *,
    inputs: object,
    artifacts: Mapping[str, bytes],
    capsule_sha256: str,
    receipt: Mapping[str, object],
) -> Mapping[str, object]:
    """Independently bind the exact fresh-live receipt and reject recovery."""

    commit, tree, package, runtime = _input_bindings(inputs)
    schema_raw = artifacts.get(LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE)
    document = dict(receipt) if isinstance(receipt, Mapping) else {}
    if (
        not isinstance(artifacts, Mapping)
        or type(schema_raw) is not bytes
        or type(capsule_sha256) is not str
        or _HASH_RE.fullmatch(capsule_sha256) is None
        or not isinstance(receipt, Mapping)
        or set(document) != _RECEIPT_KEYS
        or document.get("schema_version") != LIVE_PROOF_RECEIPT_SCHEMA
        or document.get("result")
        != "disposable_install_crash_resume_and_empty_rollback_proved"
        or document.get("observation_scope")
        != "one_bounded_disposable_linux_execution_terminal_snapshot"
        or document.get("cold_restart_scope")
        != "controller_worker_process_only"
        or document.get("candidate_git_commit") != commit
        or document.get("candidate_git_tree") != tree
        or document.get("package_manifest_sha256") != package
        or document.get("controller_runtime_receipt_sha256") != runtime
        or document.get("live_proof_receipt_schema_sha256") != _sha(schema_raw)
        or document.get("recovery_capsule_sha256") != capsule_sha256
        or document.get("authorization_text_sha256")
        != EXPECTED_AUTHORIZATION_TEXT_SHA256
        or type(document.get("exact_rollback_resources_absent_count")) is not int
        or document.get("exact_rollback_resources_absent_count") != 15
        or any(
            type(document.get(key)) is not int or document.get(key) != 0
            for key in (
                "source_postgres_read_count",
                "source_postgres_write_count",
                "provider_calls",
            )
        )
        or any(document.get(key) is not True for key in _REQUIRED_TRUE)
        or any(document.get(key) is not False for key in _REQUIRED_FALSE)
        or any(
            type(document.get(key)) is not str
            or _HASH_RE.fullmatch(str(document[key])) is None
            for key in _HASH_FIELDS
        )
    ):
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_invalid"
        )
    unsigned = {
        key: value for key, value in document.items() if key != "receipt_sha256"
    }
    if document["receipt_sha256"] != _sha(_canonical(unsigned)):
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_invalid"
        )
    return MappingProxyType(document)


def _parse_and_verify(
    raw: bytes,
    *,
    inputs: object,
    artifacts: Mapping[str, bytes],
    capsule_sha256: str,
) -> Mapping[str, object]:
    if not 1 <= len(raw) <= MAX_PROMOTABLE_LIVE_PROOF_RECEIPT_BYTES:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_invalid"
        )
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda unused: (_ for _ in ()).throw(
                DurableLiveProofReceiptError(
                    "phase9_live_proof_durable_receipt_invalid"
                )
            ),
        )
    except DurableLiveProofReceiptError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_invalid"
        ) from error
    if type(value) is not dict or _canonical(value) != raw:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_invalid"
        )
    return verify_promotable_live_proof_receipt(
        inputs=inputs,
        artifacts=artifacts,
        capsule_sha256=capsule_sha256,
        receipt=value,
    )


def _nofollow() -> int:
    value = getattr(os, "O_NOFOLLOW", 0)
    if value == 0:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_nofollow_unavailable"
        )
    return value


def _open_production_parent() -> int:
    """Open the fixed root-owned ancestry component by component."""

    if os.geteuid() != 0:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_root_required"
        )
    parent = PROMOTABLE_LIVE_PROOF_RECEIPT_PATH.parent
    flags = os.O_RDONLY | os.O_CLOEXEC | _nofollow()
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = -1
    try:
        descriptor = os.open("/", flags)
        for index, part in enumerate(parent.parts[1:]):
            child = os.open(part, flags, dir_fd=descriptor)
            opened = os.fstat(child)
            final = index == len(parent.parts[1:]) - 1
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != 0
                or opened.st_gid != 0
                or (
                    stat.S_IMODE(opened.st_mode) != 0o700
                    if final
                    else stat.S_IMODE(opened.st_mode) & 0o022 != 0
                )
            ):
                os.close(child)
                raise DurableLiveProofReceiptError(
                    "phase9_live_proof_durable_receipt_parent_invalid"
                )
            os.close(descriptor)
            descriptor = child
        named = parent.stat(follow_symlinks=False)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_parent_invalid"
            )
        return descriptor
    except DurableLiveProofReceiptError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_parent_invalid"
        ) from error


def _read_all(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    extra = os.read(descriptor, 1)
    raw = b"".join(chunks)
    if remaining or extra:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_file_invalid"
        )
    return raw


def _read_member(
    parent_fd: int,
    parent_path: Path,
    name: str,
    *,
    expected_uid: int,
    expected_gid: int,
    allowed_links: frozenset[int],
) -> tuple[bytes, os.stat_result]:
    descriptor = -1
    try:
        parent_before = os.fstat(parent_fd)
        named_parent = parent_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent_before.st_mode)
            or stat.S_IMODE(parent_before.st_mode) != 0o700
            or parent_before.st_uid != expected_uid
            or parent_before.st_gid != expected_gid
            or (parent_before.st_dev, parent_before.st_ino)
            != (named_parent.st_dev, named_parent.st_ino)
        ):
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_parent_invalid"
            )
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | _nofollow(),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o400
            or opened.st_uid != expected_uid
            or opened.st_gid != expected_gid
            or opened.st_nlink not in allowed_links
            or not 1 <= opened.st_size <= MAX_PROMOTABLE_LIVE_PROOF_RECEIPT_BYTES
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_file_invalid"
            )
        raw = _read_all(descriptor, opened.st_size)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        parent_after = os.fstat(parent_fd)
        named_parent_after = parent_path.stat(follow_symlinks=False)
        stable = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_uid,
            item.st_gid,
            item.st_nlink,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if (
            stable(after) != stable(opened)
            or stable(named_after) != stable(opened)
            or (parent_after.st_dev, parent_after.st_ino)
            != (parent_before.st_dev, parent_before.st_ino)
            or (named_parent_after.st_dev, named_parent_after.st_ino)
            != (parent_before.st_dev, parent_before.st_ino)
        ):
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_file_invalid"
            )
        return raw, after
    except FileNotFoundError as error:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_absent"
        ) from error
    except DurableLiveProofReceiptError:
        raise
    except OSError as error:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_file_invalid"
        ) from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_optional_member(
    parent_fd: int,
    parent_path: Path,
    name: str,
    *,
    expected_uid: int,
    expected_gid: int,
    allowed_links: frozenset[int],
) -> tuple[bytes, os.stat_result] | None:
    try:
        return _read_member(
            parent_fd,
            parent_path,
            name,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_links=allowed_links,
        )
    except DurableLiveProofReceiptError as error:
        if str(error) != "phase9_live_proof_durable_receipt_absent":
            raise
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as observed:
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_file_invalid"
            ) from observed
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_appeared"
        )


def _open_complete_anonymous_inode(
    parent_fd: int,
    raw: bytes,
    *,
    expected_uid: int,
    expected_gid: int,
) -> tuple[int, tuple[int, int]]:
    """Write and verify an anonymous same-directory inode before naming it."""

    descriptor = -1
    completed = False
    try:
        tmpfile = getattr(os, "O_TMPFILE", 0)
        if type(tmpfile) is not int or tmpfile == 0:
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_anonymous_tmpfile_unavailable"
            )
        descriptor = os.open(
            ".",
            os.O_RDWR | os.O_CLOEXEC | tmpfile,
            0o400,
            dir_fd=parent_fd,
        )
        created = os.fstat(descriptor)
        inode = (created.st_dev, created.st_ino)
        if (
            not stat.S_ISREG(created.st_mode)
            or created.st_nlink != 0
            or created.st_size != 0
        ):
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_anonymous_inode_invalid"
            )
        os.fchown(descriptor, expected_uid, expected_gid)
        os.fchmod(descriptor, 0o400)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if type(written) is not int or written <= 0 or written > len(view):
                raise DurableLiveProofReceiptError(
                    "phase9_live_proof_durable_receipt_write_failed"
                )
            view = view[written:]
        os.fsync(descriptor)
        complete = os.fstat(descriptor)
        if (
            not stat.S_ISREG(complete.st_mode)
            or stat.S_IMODE(complete.st_mode) != 0o400
            or complete.st_uid != expected_uid
            or complete.st_gid != expected_gid
            or complete.st_nlink != 0
            or complete.st_size != len(raw)
            or (complete.st_dev, complete.st_ino) != inode
        ):
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_anonymous_inode_invalid"
            )
        if os.lseek(descriptor, 0, os.SEEK_SET) != 0:
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_anonymous_inode_invalid"
            )
        observed = _read_all(descriptor, len(raw))
        after = os.fstat(descriptor)
        if observed != raw or (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            complete.st_dev,
            complete.st_ino,
            complete.st_mode,
            complete.st_uid,
            complete.st_gid,
            complete.st_nlink,
            complete.st_size,
            complete.st_mtime_ns,
            complete.st_ctime_ns,
        ):
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_anonymous_inode_invalid"
            )
        completed = True
        return descriptor, inode
    except DurableLiveProofReceiptError:
        raise
    except OSError as error:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_write_failed"
        ) from error
    finally:
        if descriptor >= 0 and not completed:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _link_anonymous_inode(
    descriptor: int,
    parent_fd: int,
    name: str,
    *,
    inode: tuple[int, int],
    expected_uid: int,
    expected_gid: int,
) -> None:
    """Name one complete anonymous inode using Linux linkat AT_EMPTY_PATH."""

    if (
        type(descriptor) is not int
        or descriptor < 0
        or type(name) is not str
        or not name
        or "/" in name
        or "\x00" in name
    ):
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_publication_invalid"
        )
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        linkat = libc.linkat
        linkat.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        )
        linkat.restype = ctypes.c_int
        result = linkat(
            descriptor,
            ctypes.c_char_p(b""),
            parent_fd,
            ctypes.c_char_p(name.encode("ascii")),
            AT_EMPTY_PATH,
        )
        if result != 0:
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                raise DurableLiveProofReceiptError(
                    "phase9_live_proof_durable_receipt_staging_exists"
                )
            raise OSError(code, os.strerror(code))
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o400
            or opened.st_uid != expected_uid
            or opened.st_gid != expected_gid
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != inode
            or (named.st_dev, named.st_ino) != inode
        ):
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_publication_invalid"
            )
        os.fsync(parent_fd)
    except DurableLiveProofReceiptError:
        raise
    except (AttributeError, UnicodeError, OSError) as error:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_publication_failed"
        ) from error


def _write_complete_staging_from_anonymous(
    parent_fd: int,
    parent_path: Path,
    name: str,
    raw: bytes,
    *,
    expected_uid: int,
    expected_gid: int,
) -> tuple[int, int]:
    descriptor = -1
    try:
        descriptor, inode = _open_complete_anonymous_inode(
            parent_fd,
            raw,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        _link_anonymous_inode(
            descriptor,
            parent_fd,
            name,
            inode=inode,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        observed_raw, observed = _read_member(
            parent_fd,
            parent_path,
            name,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_links=frozenset({1}),
        )
        if observed_raw != raw or (observed.st_dev, observed.st_ino) != inode:
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_staging_invalid"
            )
        try:
            os.close(descriptor)
        except OSError as error:
            descriptor = -1
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_close_failed"
            ) from error
        descriptor = -1
        return inode
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _publish_staging(
    parent_fd: int,
    parent_path: Path,
    *,
    staging_name: str,
    final_name: str,
    inode: tuple[int, int],
    raw: bytes,
    expected_uid: int,
    expected_gid: int,
) -> None:
    try:
        os.link(
            staging_name,
            final_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        staging = os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
        final = os.stat(final_name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            (staging.st_dev, staging.st_ino) != inode
            or (final.st_dev, final.st_ino) != inode
            or staging.st_nlink != 2
            or final.st_nlink != 2
        ):
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_publication_invalid"
            )
        os.fsync(parent_fd)
        current = os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != inode:
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_publication_invalid"
            )
        os.unlink(staging_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        final_raw, final = _read_member(
            parent_fd,
            parent_path,
            final_name,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_links=frozenset({1}),
        )
        if final_raw != raw or (final.st_dev, final.st_ino) != inode:
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_publication_invalid"
            )
        os.fsync(parent_fd)
    except FileExistsError as error:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_final_exists"
        ) from error
    except DurableLiveProofReceiptError:
        raise
    except OSError as error:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_publication_failed"
        ) from error


def _reconcile_at_path(
    *,
    parent_fd: int,
    parent_path: Path,
    final_name: str,
    expected_uid: int,
    expected_gid: int,
    inputs: object,
    artifacts: Mapping[str, bytes],
    capsule_sha256: str,
    expected_raw: bytes | None,
) -> Mapping[str, object] | None:
    staging_name = final_name + PROMOTABLE_LIVE_PROOF_RECEIPT_STAGING_SUFFIX
    final_member = _read_optional_member(
        parent_fd,
        parent_path,
        final_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_links=frozenset({1, 2}),
    )
    staging_member = _read_optional_member(
        parent_fd,
        parent_path,
        staging_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_links=frozenset({1, 2}),
    )
    if final_member is None and staging_member is None:
        return None

    final_verified = staging_verified = None
    if final_member is not None:
        final_verified = _parse_and_verify(
            final_member[0],
            inputs=inputs,
            artifacts=artifacts,
            capsule_sha256=capsule_sha256,
        )
    if staging_member is not None:
        staging_verified = _parse_and_verify(
            staging_member[0],
            inputs=inputs,
            artifacts=artifacts,
            capsule_sha256=capsule_sha256,
        )
    for member in (final_member, staging_member):
        if member is not None and expected_raw is not None and member[0] != expected_raw:
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_conflict"
            )

    if final_member is not None and staging_member is not None:
        final_raw, final = final_member
        staging_raw, staging = staging_member
        if (
            final_raw != staging_raw
            or (final.st_dev, final.st_ino) != (staging.st_dev, staging.st_ino)
            or final.st_nlink != 2
            or staging.st_nlink != 2
        ):
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_conflict"
            )
        os.fsync(parent_fd)
        current = os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (staging.st_dev, staging.st_ino):
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_conflict"
            )
        os.unlink(staging_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        result = final_verified
    elif staging_member is not None:
        staging_raw, staging = staging_member
        if staging.st_nlink != 1:
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_conflict"
            )
        _publish_staging(
            parent_fd,
            parent_path,
            staging_name=staging_name,
            final_name=final_name,
            inode=(staging.st_dev, staging.st_ino),
            raw=staging_raw,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        result = staging_verified
    else:
        assert final_member is not None
        if final_member[1].st_nlink != 1:
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_conflict"
            )
        os.fsync(parent_fd)
        result = final_verified
    if result is None:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_invalid"
        )
    final_raw, final = _read_member(
        parent_fd,
        parent_path,
        final_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_links=frozenset({1}),
    )
    if expected_raw is not None and final_raw != expected_raw:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_conflict"
        )
    verified = _parse_and_verify(
        final_raw,
        inputs=inputs,
        artifacts=artifacts,
        capsule_sha256=capsule_sha256,
    )
    if dict(verified) != dict(result) or final.st_nlink != 1:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_publication_invalid"
        )
    os.fsync(parent_fd)
    return verified


def _persist_at_open_parent(
    *,
    parent_fd: int,
    parent_path: Path,
    final_name: str,
    expected_uid: int,
    expected_gid: int,
    inputs: object,
    artifacts: Mapping[str, bytes],
    capsule_sha256: str,
    receipt: Mapping[str, object],
) -> Mapping[str, object]:
    verified = verify_promotable_live_proof_receipt(
        inputs=inputs,
        artifacts=artifacts,
        capsule_sha256=capsule_sha256,
        receipt=receipt,
    )
    raw = _canonical(dict(verified))
    existing = _reconcile_at_path(
        parent_fd=parent_fd,
        parent_path=parent_path,
        final_name=final_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        inputs=inputs,
        artifacts=artifacts,
        capsule_sha256=capsule_sha256,
        expected_raw=raw,
    )
    if existing is not None:
        return existing
    staging_name = final_name + PROMOTABLE_LIVE_PROOF_RECEIPT_STAGING_SUFFIX
    inode = _write_complete_staging_from_anonymous(
        parent_fd,
        parent_path,
        staging_name,
        raw,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    _publish_staging(
        parent_fd,
        parent_path,
        staging_name=staging_name,
        final_name=final_name,
        inode=inode,
        raw=raw,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    result = _reconcile_at_path(
        parent_fd=parent_fd,
        parent_path=parent_path,
        final_name=final_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        inputs=inputs,
        artifacts=artifacts,
        capsule_sha256=capsule_sha256,
        expected_raw=raw,
    )
    if result is None:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_publication_invalid"
        )
    return result


def _remove_exact_preflight_staging_if_present(
    *,
    parent_fd: int,
    parent_path: Path,
    final_name: str,
    expected_uid: int,
    expected_gid: int,
) -> None:
    """Reconcile only the exact complete marker left by a killed preflight."""

    staging_name = final_name + PROMOTABLE_LIVE_PROOF_RECEIPT_STAGING_SUFFIX
    staging = _read_optional_member(
        parent_fd,
        parent_path,
        staging_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_links=frozenset({1, 2}),
    )
    if staging is None or staging[0] != ANONYMOUS_PUBLICATION_PREFLIGHT_BYTES:
        return
    final = _read_optional_member(
        parent_fd,
        parent_path,
        final_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_links=frozenset({1, 2}),
    )
    if final is not None:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_conflict"
        )
    inode = (staging[1].st_dev, staging[1].st_ino)
    try:
        current = os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != inode or current.st_nlink != 1:
            raise DurableLiveProofReceiptError(
                "phase9_live_proof_durable_receipt_preflight_invalid"
            )
        os.unlink(staging_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except DurableLiveProofReceiptError:
        raise
    except OSError as error:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_preflight_invalid"
        ) from error


def preflight_anonymous_publication_capability(
    *,
    inputs: object,
    artifacts: Mapping[str, bytes],
    capsule_sha256: str,
) -> Mapping[str, object] | None:
    """Prove anonymous publication before effects, leaving no probe name."""

    parent_fd = _open_production_parent()
    try:
        return _preflight_at_open_parent(
            parent_fd=parent_fd,
            parent_path=PROMOTABLE_LIVE_PROOF_RECEIPT_PATH.parent,
            final_name=PROMOTABLE_LIVE_PROOF_RECEIPT_PATH.name,
            expected_uid=0,
            expected_gid=0,
            inputs=inputs,
            artifacts=artifacts,
            capsule_sha256=capsule_sha256,
        )
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def _preflight_at_open_parent(
    *,
    parent_fd: int,
    parent_path: Path,
    final_name: str,
    expected_uid: int,
    expected_gid: int,
    inputs: object,
    artifacts: Mapping[str, bytes],
    capsule_sha256: str,
) -> Mapping[str, object] | None:
    staging_name = final_name + PROMOTABLE_LIVE_PROOF_RECEIPT_STAGING_SUFFIX
    _remove_exact_preflight_staging_if_present(
        parent_fd=parent_fd,
        parent_path=parent_path,
        final_name=final_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    existing = _reconcile_at_path(
        parent_fd=parent_fd,
        parent_path=parent_path,
        final_name=final_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        inputs=inputs,
        artifacts=artifacts,
        capsule_sha256=capsule_sha256,
        expected_raw=None,
    )
    if existing is not None:
        return existing
    inode = _write_complete_staging_from_anonymous(
        parent_fd,
        parent_path,
        staging_name,
        ANONYMOUS_PUBLICATION_PREFLIGHT_BYTES,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    observed = _read_member(
        parent_fd,
        parent_path,
        staging_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_links=frozenset({1}),
    )
    if observed[0] != ANONYMOUS_PUBLICATION_PREFLIGHT_BYTES or (
        observed[1].st_dev,
        observed[1].st_ino,
    ) != inode:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_preflight_invalid"
        )
    _remove_exact_preflight_staging_if_present(
        parent_fd=parent_fd,
        parent_path=parent_path,
        final_name=final_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    if _read_optional_member(
        parent_fd,
        parent_path,
        staging_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_links=frozenset({1}),
    ) is not None:
        raise DurableLiveProofReceiptError(
            "phase9_live_proof_durable_receipt_preflight_invalid"
        )
    os.fsync(parent_fd)
    return None


def persist_verified_promotable_live_receipt(
    *,
    inputs: object,
    artifacts: Mapping[str, bytes],
    capsule_sha256: str,
    receipt: Mapping[str, object],
) -> Mapping[str, object]:
    """Persist and re-read the one fixed production promotable receipt."""

    parent_fd = _open_production_parent()
    try:
        return _persist_at_open_parent(
            parent_fd=parent_fd,
            parent_path=PROMOTABLE_LIVE_PROOF_RECEIPT_PATH.parent,
            final_name=PROMOTABLE_LIVE_PROOF_RECEIPT_PATH.name,
            expected_uid=0,
            expected_gid=0,
            inputs=inputs,
            artifacts=artifacts,
            capsule_sha256=capsule_sha256,
            receipt=receipt,
        )
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def read_verified_promotable_live_receipt_if_present(
    *,
    inputs: object,
    artifacts: Mapping[str, bytes],
    capsule_sha256: str,
) -> Mapping[str, object] | None:
    """Read or safely finish an exact prior promotable publication."""

    parent_fd = _open_production_parent()
    try:
        return _read_at_open_parent(
            parent_fd=parent_fd,
            parent_path=PROMOTABLE_LIVE_PROOF_RECEIPT_PATH.parent,
            final_name=PROMOTABLE_LIVE_PROOF_RECEIPT_PATH.name,
            expected_uid=0,
            expected_gid=0,
            inputs=inputs,
            artifacts=artifacts,
            capsule_sha256=capsule_sha256,
        )
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def _read_at_open_parent(
    *,
    parent_fd: int,
    parent_path: Path,
    final_name: str,
    expected_uid: int,
    expected_gid: int,
    inputs: object,
    artifacts: Mapping[str, bytes],
    capsule_sha256: str,
) -> Mapping[str, object] | None:
    """Reconcile a killed preflight marker before reading durable success."""

    _remove_exact_preflight_staging_if_present(
        parent_fd=parent_fd,
        parent_path=parent_path,
        final_name=final_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    return _reconcile_at_path(
        parent_fd=parent_fd,
        parent_path=parent_path,
        final_name=final_name,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        inputs=inputs,
        artifacts=artifacts,
        capsule_sha256=capsule_sha256,
        expected_raw=None,
    )

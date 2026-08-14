#!/usr/bin/env python3
from __future__ import annotations

"""Run the signed-capsule Phase 9 disposable store proof.

The outer process accepts only four immutable identities. Start authority is
bound to the exact manager-to-issuer process lineage, live lease, inherited
read-only manager-control descriptor, fixed guard, and sealed release. The
root-owned recovery capsule contains pre-signed install documents and a narrow
public rollback-recovery delegation; RECOVER_ONLY remains independently
sealed and cannot initiate a pristine install. The issuer discards its private
key before a start-capable runner begins.

This program never imports provider clients, reads production data or provider
credentials, accepts a path/command/SQL/URL/resource name, or performs a name-based
cleanup.  A root-owned public recovery capsule and content-free authority-state
reservation are committed before the first install worker.  The capsule holds
only signed public documents; no private signing key is persisted.  Recovery
can therefore derive and resume only the exact ledger-bound empty rollback.
"""

import errno
import fcntl
import os
import select
import stat
import subprocess
import sys

_DONT_WRITE_BYTECODE_AT_START = sys.dont_write_bytecode
if __name__ == "__main__" and not _DONT_WRITE_BYTECODE_AT_START:
    raise SystemExit("phase9_live_proof_bytecode_writes_not_disabled")

_PREIMPORT_RELEASE_ROOT = "/opt/governed-memory-controller/releases"
_PREIMPORT_RUNTIME_ROOT = "/opt/governed-memory-controller/runtimes"
_PREIMPORT_RUNNER_RELATIVE = (
    "tools/governed_memory_validation/"
    "run_disposable_installation_live_proof.py"
)
_PREIMPORT_ISSUER_RELATIVE = (
    "tools/governed_memory_validation/"
    "issue_disposable_installation_live_proof.py"
)
_PREIMPORT_MANAGER_RELATIVE = (
    "tools/governed_memory_validation/"
    "execute_phase9_disposable_live_proof_controller.py"
)
_PREIMPORT_AUTHORITY_MODES = frozenset(
    {
        "preclaim-staged-start",
        "verify-published-start-pair",
        "start-or-recover",
    }
)
_PREIMPORT_RECOVERY_MODE = "recover-only"
_PREIMPORT_WORKER_MODES = frozenset(
    {
        "worker-start-or-recover",
        "worker-recover-only",
        "install",
        "resume-install",
        "rollback",
        "resume-rollback",
        "verify-absence",
    }
)
_PREIMPORT_MANAGER_CONTROL_FD = 198
_PREIMPORT_ISSUER_PID_KEY = "GOVERNED_MEMORY_PHASE9_ISSUER_PID"
_PREIMPORT_MANAGER_PID_KEY = "GOVERNED_MEMORY_PHASE9_MANAGER_PID"
_PREIMPORT_REPOSITORY_ROOT_KEY = (
    "GOVERNED_MEMORY_PHASE9_ISSUER_REPOSITORY_ROOT"
)
_PREIMPORT_LEASE_KEYS = (
    "CHAT_MEMORY_LEASE_ID",
    "CODEX_TASK_ID",
    "CODEX_THREAD_ID",
)
_PREIMPORT_LEASE_GUARD_PYTHON = "/usr/bin/python3.12"
_PREIMPORT_LEASE_GUARD = (
    "/var/lib/chat-memory-change-leases-v1/control/bin/"
    "chat_memory_lease_guard.py"
)
_PREIMPORT_LEASE_ID_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:@-"
)


def _preimport_exact_lower_hex(value: object, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _preimport_exact_positive_pid(value: object) -> int:
    if (
        type(value) is not str
        or not value.isascii()
        or not value.isdecimal()
        or value.startswith("0")
    ):
        raise RuntimeError("pid")
    result = int(value)
    if result <= 1 or str(result) != value:
        raise RuntimeError("pid")
    return result


def _preimport_exact_lease_identity(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or value[0] not in _PREIMPORT_LEASE_ID_CHARACTERS
        or any(
            character not in _PREIMPORT_LEASE_ID_CHARACTERS
            for character in value
        )
    ):
        raise RuntimeError("lease")
    return value


def _preimport_cli_identities() -> tuple[str, str, str, str, str]:
    arguments = tuple(sys.argv[1:])
    if (
        len(arguments) != 9
        or arguments[1] != "--candidate-git-commit"
        or arguments[3] != "--candidate-git-tree"
        or arguments[5] != "--package-manifest-sha256"
        or arguments[7] != "--controller-runtime-receipt-sha256"
        or arguments[0]
        not in (
            _PREIMPORT_AUTHORITY_MODES
            | {_PREIMPORT_RECOVERY_MODE}
            | _PREIMPORT_WORKER_MODES
        )
        or not _preimport_exact_lower_hex(arguments[2], 40)
        or not _preimport_exact_lower_hex(arguments[4], 40)
        or not _preimport_exact_lower_hex(arguments[6], 64)
        or not _preimport_exact_lower_hex(arguments[8], 64)
    ):
        raise RuntimeError("arguments")
    return (
        arguments[0],
        arguments[2],
        arguments[4],
        arguments[6],
        arguments[8],
    )


def _preimport_proc_cmdline(pid: int) -> tuple[str, ...]:
    with open(f"/proc/{pid}/cmdline", "rb", buffering=0) as source:
        raw = source.read(16 * 1024 + 1)
    if not raw or len(raw) > 16 * 1024 or not raw.endswith(b"\x00"):
        raise RuntimeError("cmdline")
    try:
        values = tuple(item.decode("utf-8") for item in raw[:-1].split(b"\x00"))
    except UnicodeError as error:
        raise RuntimeError("cmdline") from error
    if not values or any(not value for value in values):
        raise RuntimeError("cmdline")
    return values


def _preimport_proc_parent_pid(pid: int) -> int:
    with open(f"/proc/{pid}/status", "rb", buffering=0) as source:
        raw = source.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise RuntimeError("status")
    matches = [line for line in raw.splitlines() if line.startswith(b"PPid:\t")]
    if len(matches) != 1:
        raise RuntimeError("status")
    try:
        value = matches[0][len(b"PPid:\t") :].decode("ascii")
    except UnicodeError as error:
        raise RuntimeError("status") from error
    return _preimport_exact_positive_pid(value)


def _preimport_control_descriptor_identity() -> tuple[int, int]:
    opened = os.fstat(_PREIMPORT_MANAGER_CONTROL_FD)
    flags = fcntl.fcntl(_PREIMPORT_MANAGER_CONTROL_FD, fcntl.F_GETFL)
    ready, unused_write, unused_exception = select.select(
        [_PREIMPORT_MANAGER_CONTROL_FD], [], [], 0
    )
    if (
        not stat.S_ISFIFO(opened.st_mode)
        or flags & os.O_ACCMODE != os.O_RDONLY
        or not os.get_inheritable(_PREIMPORT_MANAGER_CONTROL_FD)
        or ready
        or unused_write
        or unused_exception
    ):
        raise RuntimeError("control")
    return opened.st_dev, opened.st_ino


def _preimport_require_descriptor_absent(descriptor: int) -> None:
    try:
        os.fstat(descriptor)
    except OSError as error:
        if error.errno == errno.EBADF:
            return
        raise RuntimeError("descriptor") from error
    raise RuntimeError("descriptor")


def _preimport_manager_holds_control_writer(
    manager_pid: int,
    control_identity: tuple[int, int],
) -> bool:
    for entry in os.listdir(f"/proc/{manager_pid}/fd"):
        if not entry.isascii() or not entry.isdecimal():
            continue
        descriptor = int(entry)
        try:
            opened = os.stat(f"/proc/{manager_pid}/fd/{descriptor}")
            if (
                not stat.S_ISFIFO(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != control_identity
            ):
                continue
            with open(
                f"/proc/{manager_pid}/fdinfo/{descriptor}",
                "rb",
                buffering=0,
            ) as source:
                raw = source.read(4097)
            if len(raw) > 4096:
                raise RuntimeError("fdinfo")
            flag_lines = [
                line for line in raw.splitlines() if line.startswith(b"flags:\t")
            ]
            if len(flag_lines) != 1:
                continue
            flags = int(flag_lines[0][len(b"flags:\t") :], 8)
            if flags & os.O_ACCMODE == os.O_WRONLY:
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
    return False


def _preimport_validate_sealed_invocation(
    mode: str,
    package_sha256: str,
    runtime_receipt_sha256: str,
) -> tuple[str, str]:
    if not sys.flags.isolated or sys.platform != "linux":
        raise RuntimeError("runtime")
    release_root = os.path.join(_PREIMPORT_RELEASE_ROOT, package_sha256)
    runner_path = os.path.join(release_root, _PREIMPORT_RUNNER_RELATIVE)
    runtime_python = os.path.join(
        _PREIMPORT_RUNTIME_ROOT,
        runtime_receipt_sha256,
        "bin/python",
    )
    if (
        not os.path.isabs(__file__)
        or __file__ != os.path.realpath(__file__)
        or sys.argv[0] != __file__
        or __file__ != runner_path
        or sys.executable != runtime_python
        or os.path.realpath(sys.executable) != sys.executable
    ):
        raise RuntimeError("invocation")
    for path in (runner_path, runtime_python):
        opened = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != 0
            or opened.st_gid != 0
            or stat.S_IMODE(opened.st_mode) & 0o022
        ):
            raise RuntimeError("ownership")
    if mode == _PREIMPORT_RECOVERY_MODE or mode in _PREIMPORT_WORKER_MODES:
        _preimport_require_descriptor_absent(_PREIMPORT_MANAGER_CONTROL_FD)
    return release_root, runtime_python


def _preimport_validate_issuer_authority(
    *,
    runtime_python: str,
) -> tuple[int, int, int, int, str, str, str, str]:
    issuer_pid = _preimport_exact_positive_pid(
        os.environ.get(_PREIMPORT_ISSUER_PID_KEY)
    )
    manager_pid = _preimport_exact_positive_pid(
        os.environ.get(_PREIMPORT_MANAGER_PID_KEY)
    )
    if os.getppid() != issuer_pid:
        raise RuntimeError("issuer")
    repository_root = os.environ.get(_PREIMPORT_REPOSITORY_ROOT_KEY)
    if (
        type(repository_root) is not str
        or not os.path.isabs(repository_root)
        or os.path.realpath(repository_root) != repository_root
    ):
        raise RuntimeError("repository")
    issuer_path = os.path.join(repository_root, _PREIMPORT_ISSUER_RELATIVE)
    manager_path = os.path.join(repository_root, _PREIMPORT_MANAGER_RELATIVE)
    if (
        os.path.realpath(issuer_path) != issuer_path
        or os.path.realpath(manager_path) != manager_path
        or os.readlink(f"/proc/{issuer_pid}/exe") != runtime_python
        or os.readlink(f"/proc/{manager_pid}/exe") != runtime_python
        or _preimport_proc_cmdline(issuer_pid)
        != (runtime_python, "-I", "-B", issuer_path)
        or _preimport_proc_cmdline(manager_pid)
        != (runtime_python, "-I", "-B", manager_path)
        or _preimport_proc_parent_pid(issuer_pid) != manager_pid
    ):
        raise RuntimeError("lineage")
    control_device, control_inode = _preimport_control_descriptor_identity()
    issuer_control = os.stat(
        f"/proc/{issuer_pid}/fd/{_PREIMPORT_MANAGER_CONTROL_FD}"
    )
    if (
        not stat.S_ISFIFO(issuer_control.st_mode)
        or (issuer_control.st_dev, issuer_control.st_ino)
        != (control_device, control_inode)
        or not _preimport_manager_holds_control_writer(
            manager_pid,
            (control_device, control_inode),
        )
    ):
        raise RuntimeError("control")
    lease_values = tuple(
        _preimport_exact_lease_identity(os.environ.get(key))
        for key in _PREIMPORT_LEASE_KEYS
    )
    return (
        issuer_pid,
        manager_pid,
        control_device,
        control_inode,
        repository_root,
        lease_values[0],
        lease_values[1],
        lease_values[2],
    )


_PREIMPORT_RUNNER_AUTHORITY: tuple[object, ...] | None = None
if __name__ == "__main__":
    try:
        (
            _preimport_mode,
            _preimport_commit,
            _preimport_tree,
            _preimport_package,
            _preimport_runtime,
        ) = _preimport_cli_identities()
        _preimport_release_root, _preimport_runtime_python = (
            _preimport_validate_sealed_invocation(
                _preimport_mode,
                _preimport_package,
                _preimport_runtime,
            )
        )
        if _preimport_mode in _PREIMPORT_AUTHORITY_MODES:
            _preimport_lineage = _preimport_validate_issuer_authority(
                runtime_python=_preimport_runtime_python,
            )
        else:
            _preimport_lineage = (0, 0, 0, 0, "", "", "", "")
        _PREIMPORT_RUNNER_AUTHORITY = (
            _preimport_mode,
            _preimport_commit,
            _preimport_tree,
            _preimport_package,
            _preimport_runtime,
            _preimport_release_root,
            _preimport_runtime_python,
            *_preimport_lineage,
        )
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        sys.stderr.write("phase9_live_proof_sealed_authority_required\n")
        raise SystemExit(1) from None

import argparse
import base64
import ctypes
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import signal
import time
from types import MappingProxyType
from typing import Final, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_INVOKED_RELEASE_PACKAGE_SHA256: str | None = None
if __name__ == "__main__":
    assert _PREIMPORT_RUNNER_AUTHORITY is not None
    _INVOKED_RELEASE_PACKAGE_SHA256 = str(_PREIMPORT_RUNNER_AUTHORITY[3])
    sys.path.insert(0, str(_PREIMPORT_RUNNER_AUTHORITY[5]))

from tools.governed_memory_install import authority
from tools.governed_memory_install.authority_state import (
    AuthorityClaimNotAllowedError,
    AuthorityState,
    derive_nonce_claim_identity,
    operation_sha256 as authority_operation_sha256,
)
from tools.governed_memory_install.controller_runtime import (
    verified_controller_runtime_evidence,
    verify_controller_runtime_capability,
)
from tools.governed_memory_install.durable_receipts import (
    DurableReceiptStore,
    ReceiptArtifact,
)
from tools.governed_memory_validation.durable_live_proof_receipt import (
    DurableLiveProofReceiptError,
    persist_verified_promotable_live_receipt,
    preflight_anonymous_publication_capability,
    read_verified_promotable_live_receipt_if_present,
)
from tools.governed_memory_validation import process_death_arm_receipt
from tools.governed_memory_install.execution_capability import (
    reconstruct_resolved_store_spec_from_claimed_binding,
    resume_dormant_store_install_execution_binding,
    verified_dormant_install_authority_identity,
)
from tools.governed_memory_install.execution_authority import (
    KernelUtcClock,
    read_trusted_utc,
)
from tools.governed_memory_install.execution_lock import (
    ExecutionLockBusyError,
    GlobalExecutionLock,
)
from tools.governed_memory_install.host_boundary import (
    CommandRunner,
    FIXED_ENVIRONMENT,
)
from tools.governed_memory_install.image_preflight import (
    expectations_from_store_spec,
    inspect_exact_store_image_set,
)
from tools.governed_memory_install.install_backend import InstallPrerequisites
from tools.governed_memory_install.install_entrypoint import (
    resume_authorized_dormant_store_install,
    run_authorized_dormant_store_install,
)
from tools.governed_memory_install.journal import (
    DurableJournal,
    DurableJournalError,
    parse_durable_journal_bytes,
)
from tools.governed_memory_install.linux_plan import validate_store_spec
from tools.governed_memory_install.package_capability import (
    verified_package_evidence,
    verify_install_package_capability,
)
from tools.governed_memory_install.receipts import (
    verify_empty_rollback_receipt,
    verify_install_receipt,
)
from tools.governed_memory_install.resource_identity import (
    load_ledger,
    resource_ledger_binding_sha256,
)
from tools.governed_memory_install.rollback import (
    build_empty_rollback_eligibility_receipt,
    derive_exact_rollback_resources_from_ledger,
    empty_rollback_plan_sha256,
    verified_rollback_resource_parts,
)
from tools.governed_memory_install.rollback_authority import (
    EmptyRollbackExpectedBindings,
    RECOVERY_DERIVATION_POLICY,
    RECOVERY_RESERVATION_OPERATION,
    ROLLBACK_OPERATION,
    SCOPE_SCHEMA_VERSION as ROLLBACK_SCOPE_SCHEMA,
    TRUST_BUNDLE_SCHEMA_VERSION as ROLLBACK_TRUST_SCHEMA,
    derive_empty_rollback_execution_capability_from_recovery_delegation,
    recovery_reservation_binding,
    rollback_capability_evidence,
    verify_empty_rollback_recovery_delegation,
)
from tools.governed_memory_install.rollback_entrypoint import (
    AUTHORITY_STATE_PATH,
    GLOBAL_LOCK_PATH,
    resume_authorized_empty_store_rollback,
    start_reserved_authorized_empty_store_rollback,
)
from tools.governed_memory_install.rollback_journal import (
    DurableRollbackJournal,
    DurableRollbackJournalError,
    parse_rollback_journal_bytes,
)


THREAD_ID: Final = "019fe927-8367-7f52-86f2-e2b5b43a2390"
AUTHORIZED_TEXT: Final = "Authorized to move on and finish nine whatever it takes"
AUTHORIZED_TEXT_SHA256: Final = hashlib.sha256(
    AUTHORIZED_TEXT.encode("utf-8")
).hexdigest()
RECOVERY_CAPSULE_PATH: Final = Path(
    "/var/lib/governed-memory-controller/phase9-disposable-proof-recovery-capsule-v6.json"
)
RECOVERY_CAPSULE_STAGING_PATH: Final = RECOVERY_CAPSULE_PATH.with_name(
    RECOVERY_CAPSULE_PATH.name + ".publishing"
)
RECOVERY_CAPSULE_SCHEMA: Final = "governed-memory-phase9-disposable-proof-recovery-capsule-v6"
AUTHORIZATION_NAMESPACE: Final = "governed-memory-phase9-live-proof-v5"
INSTALL_SCOPE_ID: Final = "phase9-disposable-live-install-000005"
ROLLBACK_SCOPE_ID: Final = "phase9-disposable-live-rollback-000005"
LIVE_PROOF_RECEIPT_SCHEMA: Final = "governed-memory-phase9-live-proof-receipt-v6"
RECOVERY_RECEIPT_SCHEMA: Final = (
    "governed-memory-phase9-disposable-proof-recovery-receipt-v1"
)
PAIR_ONLY_RECEIPT_SCHEMA: Final = (
    "governed-memory-phase9-start-pair-no-execution-receipt-v1"
)
LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE: Final = (
    "ops/governed_memory/installation/current/live_proof_receipt.schema.json"
)
INSTALL_NONCE_DOMAIN: Final = b"governed-memory-phase9-install-nonce-v1\x00"
ROLLBACK_NONCE_DOMAIN: Final = b"governed-memory-phase9-rollback-nonce-v1\x00"
RECOVERY_RESERVATION_NONCE_DOMAIN: Final = (
    b"governed-memory-phase9-rollback-recovery-reservation-nonce-v1\x00"
)

EXECUTIONS_ROOT: Final = Path("/var/lib/governed-memory-controller/executions-v5")
CONTROLLER_STATE_ROOT: Final = Path("/var/lib/governed-memory-controller")
LOCK_ROOT: Final = Path("/run/lock/governed-memory-controller")
LIVE_PROOF_GUARD_PATH: Final = (
    LOCK_ROOT / "phase9-disposable-live-proof.lock"
)
LIVE_PROOF_GUARD_FD: Final = 9
CONTROLLER_CONFIG_ROOT: Final = Path("/etc/governed-memory-controller")
STORE_SECRET_PARENT: Final = Path("/etc/governed-memory-stores")
STORE_SECRET_ROOT: Final = Path(
    "/etc/governed-memory-stores/9a54cf123493-000005"
)
RESOLVED_STORE_SPEC_PATH: Final = Path(
    "/etc/governed-memory-controller/store_spec-v5.json"
)
RUNTIME_RECEIPT_ROOT: Final = Path(
    "/var/lib/governed-memory-controller/runtime-receipts"
)
RELEASE_ROOT_PREFIX: Final = Path("/opt/governed-memory-controller/releases")

INSTALL_KILL_STEP: Final = "I04_WRITE_RESOLVED_STORE_SPEC_AND_GENERATE_FRESH_STORE_SECRETS"
ROLLBACK_KILL_STEP: Final = "R05_DISABLE_AND_REMOVE_STORES_SUPERVISOR"
JOURNAL_APPLIED_EVENT: Final = "applied"
BOUNDARY_TIMEOUT_SECONDS: Final = 180.0
WORKER_COMPLETION_TIMEOUT_SECONDS: Final = 240.0
WORKER_CLEANUP_TIMEOUT_SECONDS: Final = 5.0

# The signed public capsule plus an exact AuthorityState reservation make
# rollback derivation independently resumable without retaining a signer.
DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED: Final = True
POLL_INTERVAL_SECONDS: Final = 0.002
MAX_WORKER_RESULT_BYTES: Final = 1024 * 1024
MAX_ARTIFACT_BYTES: Final = 8 * 1024 * 1024
PR_SET_PDEATHSIG: Final = 1
TIMEDATECTL_BINARY: Final = Path("/usr/bin/timedatectl")
TIMEDATECTL_SYNCHRONIZATION_ARGV: Final = (
    "/usr/bin/timedatectl",
    "show",
    "--property=NTPSynchronized",
    "--value",
)
TIMEDATECTL_TIMEOUT_SECONDS: Final = 5

_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_EXECUTION_DIRECTORY_RE: Final = _HASH_RE
_INSTALL_ATTEMPT_RE: Final = re.compile(r"install-[0-9a-f]{40}\Z", re.ASCII)
_ROLLBACK_ATTEMPT_RE: Final = re.compile(r"rollback-[0-9a-f]{40}\Z", re.ASCII)

_LIVE_PROOF_RECEIPT_KEYS: Final = frozenset(
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
_RECOVERY_RECEIPT_KEYS: Final = frozenset(
    {
        "schema_version",
        "result",
        "candidate_git_commit",
        "candidate_git_tree",
        "package_manifest_sha256",
        "controller_runtime_receipt_sha256",
        "recovery_capsule_sha256",
        "recovery_reservation_claim_sha256",
        "install_authority_claim_sha256",
        "start_authority_pair_claimed_atomically",
        "installation_execution_id",
        "installation_receipt_sha256",
        "empty_rollback_execution_id",
        "empty_rollback_receipt_sha256",
        "exact_resources_absent",
        "stores_installed",
        "stores_supervisor_installed",
        "install_initiated_by_recovery_mode",
        "provider_calls",
        "production_data_read",
        "activation_performed",
        "receipt_sha256",
    }
)
_PAIR_ONLY_RECEIPT_KEYS: Final = frozenset(
    {
        "schema_version",
        "result",
        "candidate_git_commit",
        "candidate_git_tree",
        "package_manifest_sha256",
        "controller_runtime_receipt_sha256",
        "recovery_capsule_sha256",
        "recovery_reservation_claim_sha256",
        "install_authority_claim_sha256",
        "start_authority_pair_claimed_atomically",
        "expected_installation_execution_id",
        "install_execution_directory_present",
        "installation_receipt_present",
        "rollback_authority_claim_present",
        "install_initiated_by_recovery_mode",
        "provider_calls",
        "production_data_read",
        "activation_performed",
        "receipt_sha256",
    }
)


class LiveProofError(RuntimeError):
    """Content-free refusal from the disposable live proof runner."""


_INTERNAL_INSTALL_FAILURE_CODE_RE: Final = re.compile(
    r"[a-z][a-z0-9_]{1,95}\Z", re.ASCII
)


def _sanitized_internal_install_failure_code(
    error: BaseException,
) -> str | None:
    """Return only the deepest closed install-package error code.

    Host exception text can contain paths, command output, or other untrusted
    material.  Only content-free codes emitted by the sealed installation
    package are eligible for projection into the runner's public error token.
    """

    current: BaseException | None = error
    selected: str | None = None
    seen: set[int] = set()
    for _ in range(16):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        module = type(current).__module__
        value = str(current)
        if (
            module.startswith("tools.governed_memory_install.")
            and _INTERNAL_INSTALL_FAILURE_CODE_RE.fullmatch(value) is not None
        ):
            selected = value
        cause = current.__cause__
        current = cause if isinstance(cause, BaseException) else None
    return selected


class _CooperativeBoundaryStopAbort(BaseException):
    """Bypass controller recovery after an exact committed stop boundary."""


class WorkerMode(str, Enum):
    START_OR_RECOVER = "worker-start-or-recover"
    RECOVER_ONLY = "worker-recover-only"
    INSTALL = "install"
    RESUME_INSTALL = "resume-install"
    ROLLBACK = "rollback"
    RESUME_ROLLBACK = "resume-rollback"
    VERIFY_ABSENCE = "verify-absence"


class RunnerMode(str, Enum):
    PRECLAIM_STAGED_START = "preclaim-staged-start"
    VERIFY_PUBLISHED_START_PAIR = "verify-published-start-pair"
    START_OR_RECOVER = "start-or-recover"
    RECOVER_ONLY = "recover-only"


def _require_active_runner_lease(
    authority_state: tuple[object, ...],
) -> None:
    repository_root = authority_state[11]
    lease_values = authority_state[12:15]
    if (
        type(repository_root) is not str
        or not repository_root
        or len(lease_values) != len(_PREIMPORT_LEASE_KEYS)
        or any(type(value) is not str for value in lease_values)
    ):
        raise LiveProofError("phase9_live_proof_runner_authority_invalid")
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        **dict(zip(_PREIMPORT_LEASE_KEYS, lease_values, strict=True)),
    }
    try:
        completed = subprocess.run(
            (
                _PREIMPORT_LEASE_GUARD_PYTHON,
                _PREIMPORT_LEASE_GUARD,
                "--operation",
                "production-write",
                "--worktree",
                repository_root,
            ),
            cwd="/",
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
            check=False,
            user=1000,
            group=1000,
            extra_groups=(),
        )
    except (OSError, subprocess.SubprocessError, TypeError) as error:
        raise LiveProofError(
            "phase9_live_proof_runner_authority_invalid"
        ) from error
    if (
        completed.returncode != 0
        or completed.stdout != b""
        or completed.stderr != b""
    ):
        raise LiveProofError("phase9_live_proof_runner_authority_invalid")


def _require_runner_process_authority(expected_mode: RunnerMode) -> None:
    """Reprove the sealed CLI and its mode-specific authority before effects."""

    state = _PREIMPORT_RUNNER_AUTHORITY
    if type(expected_mode) is not RunnerMode or state is None or len(state) != 15:
        raise LiveProofError("phase9_live_proof_runner_authority_invalid")
    (
        mode,
        commit,
        tree,
        package_sha256,
        runtime_sha256,
        release_root,
        runtime_python,
        issuer_pid,
        manager_pid,
        control_device,
        control_inode,
        repository_root,
        lease_id,
        task_id,
        thread_id,
    ) = state
    if (
        mode != expected_mode.value
        or not _preimport_exact_lower_hex(commit, 40)
        or not _preimport_exact_lower_hex(tree, 40)
        or not _preimport_exact_lower_hex(package_sha256, 64)
        or not _preimport_exact_lower_hex(runtime_sha256, 64)
    ):
        raise LiveProofError("phase9_live_proof_runner_authority_invalid")
    try:
        observed_release_root, observed_runtime_python = (
            _preimport_validate_sealed_invocation(
                str(mode),
                str(package_sha256),
                str(runtime_sha256),
            )
        )
        if (
            observed_release_root != release_root
            or observed_runtime_python != runtime_python
        ):
            raise RuntimeError("identity")
        if expected_mode.value in _PREIMPORT_AUTHORITY_MODES:
            observed_lineage = _preimport_validate_issuer_authority(
                runtime_python=str(runtime_python),
            )
            if observed_lineage != (
                issuer_pid,
                manager_pid,
                control_device,
                control_inode,
                repository_root,
                lease_id,
                task_id,
                thread_id,
            ):
                raise RuntimeError("lineage")
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as error:
        raise LiveProofError(
            "phase9_live_proof_runner_authority_invalid"
        ) from error
    if expected_mode.value in _PREIMPORT_AUTHORITY_MODES:
        _require_active_runner_lease(state)


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
        raise LiveProofError("phase9_live_proof_json_invalid") from error


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _document_sha(value: object) -> str:
    return _sha(_canonical(value))


def _require_hash(value: str, *, commit: bool = False) -> str:
    pattern = _COMMIT_RE if commit else _HASH_RE
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise LiveProofError("phase9_live_proof_identity_invalid")
    return value


def _install_authority_material(
    verified_scope_capability: object,
) -> Mapping[str, str]:
    """Project the one canonical verified install-authority identity."""

    try:
        identity = verified_dormant_install_authority_identity(
            verified_scope_capability
        )
    except Exception as error:
        raise LiveProofError(
            "phase9_live_proof_install_authorization_invalid"
        ) from error
    return MappingProxyType(
        {
            "nonce": identity.authorization_nonce,
            "operation": identity.operation,
            "execution_sha256": identity.execution_sha256,
            "authorization_sha256": identity.authorization_sha256,
            "scope_sha256": identity.scope_sha256,
            "trust_bundle_sha256": identity.trust_bundle_sha256,
            "claim_sha256": identity.claim_sha256,
            "execution_id": identity.execution_id,
        }
    )


@dataclass(frozen=True, slots=True)
class ProofInputs:
    candidate_git_commit: str
    candidate_git_tree: str
    package_manifest_sha256: str
    controller_runtime_receipt_sha256: str

    def __post_init__(self) -> None:
        _require_hash(self.candidate_git_commit, commit=True)
        _require_hash(self.candidate_git_tree, commit=True)
        _require_hash(self.package_manifest_sha256)
        _require_hash(self.controller_runtime_receipt_sha256)
        if (
            _INVOKED_RELEASE_PACKAGE_SHA256 is not None
            and self.package_manifest_sha256
            != _INVOKED_RELEASE_PACKAGE_SHA256
        ):
            raise LiveProofError("phase9_live_proof_release_identity_mismatch")


@dataclass(frozen=True, slots=True)
class BoundaryJournalEvidence:
    boundary_kind: str
    execution_id: str
    journal_name: str
    plan_sha256: str
    attempt_id: str
    boundary_step_id: str
    boundary_record_sequence: int
    boundary_record_sha256: str
    journal_sequence_at_arm: int
    journal_head_sha256_at_arm: str
    journal_record_sha256s: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerifiedRecoveryCapsule:
    raw: bytes
    document: Mapping[str, object]
    install_documents: InstallDocuments
    rollback_delegation: Mapping[str, object]
    key_id: str
    rollback_nonce: str


@dataclass(frozen=True, slots=True)
class InstallDocuments:
    scope: bytes
    authorization: bytes
    trust_bundle: bytes
    key_id: str


@dataclass(frozen=True, slots=True)
class RollbackDocuments:
    scope: bytes
    authorization: bytes
    trust_bundle: bytes
    key_id: str
    eligibility: Mapping[str, object]
    resources: object
    resolved_store_spec: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ProofContext:
    inputs: ProofInputs
    package_manifest: bytes
    artifacts: Mapping[str, bytes]
    runtime_receipt: bytes
    install_documents: InstallDocuments
    verified_scope_capability: object
    verified_package_capability: object
    verified_runtime_capability: object
    verified_recovery_delegation_capability: object
    recovery_capsule: VerifiedRecoveryCapsule
    rollback_documents: RollbackDocuments | None = None


def _read_regular_no_follow(path: Path, *, maximum: int) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            or before.st_uid != 0
            or before.st_nlink != 1
            or not 1 <= before.st_size <= maximum
        ):
            raise LiveProofError("phase9_live_proof_file_identity_invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(65536, maximum + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > maximum:
                raise LiveProofError("phase9_live_proof_file_too_large")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise LiveProofError("phase9_live_proof_file_changed")
        return b"".join(chunks)
    except LiveProofError:
        raise
    except OSError as error:
        raise LiveProofError("phase9_live_proof_file_unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _optional_root_file_size_allowed(
    size: int,
    maximum: int,
    *,
    allow_empty: bool,
) -> bool:
    if (
        type(size) is not int
        or type(maximum) is not int
        or maximum < 1
        or type(allow_empty) is not bool
    ):
        return False
    return 0 <= size <= maximum and (allow_empty or size >= 1)


def _read_optional_root_regular_no_follow(
    path: Path,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> bytes | None:
    """Read a fixed root file; None means stable leaf absence only."""

    if type(allow_empty) is not bool:
        raise LiveProofError("phase9_live_proof_optional_file_policy_invalid")

    parent_fd = descriptor = -1
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(
            path.parent,
            flags | getattr(os, "O_DIRECTORY", 0),
        )
        parent = os.fstat(parent_fd)
        named_parent = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != 0
            or parent.st_gid != 0
            or (parent.st_dev, parent.st_ino)
            != (named_parent.st_dev, named_parent.st_ino)
        ):
            raise LiveProofError(
                "phase9_live_proof_optional_file_parent_invalid"
            )
        parent_identity = (parent.st_dev, parent.st_ino)
        try:
            descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            try:
                os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise LiveProofError(
                    "phase9_live_proof_optional_file_invalid"
                ) from error
            else:
                raise LiveProofError(
                    "phase9_live_proof_optional_file_appeared"
                )
            after_parent = os.fstat(parent_fd)
            after_named_parent = path.parent.stat(follow_symlinks=False)
            if (
                (after_parent.st_dev, after_parent.st_ino) != parent_identity
                or (after_named_parent.st_dev, after_named_parent.st_ino)
                != parent_identity
            ):
                raise LiveProofError(
                    "phase9_live_proof_optional_file_parent_changed"
                )
            return None
        opened = os.fstat(descriptor)
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or opened.st_uid != 0
            or opened.st_gid != 0
            or opened.st_nlink != 1
            or not _optional_root_file_size_allowed(
                opened.st_size,
                maximum,
                allow_empty=allow_empty,
            )
            or (opened.st_dev, opened.st_ino)
            != (named.st_dev, named.st_ino)
        ):
            raise LiveProofError("phase9_live_proof_optional_file_invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(65536, maximum + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > maximum:
                raise LiveProofError(
                    "phase9_live_proof_optional_file_invalid"
                )
        after = os.fstat(descriptor)
        named_after = os.stat(
            path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        after_parent = os.fstat(parent_fd)
        after_named_parent = path.parent.stat(follow_symlinks=False)
        if (
            (after.st_dev, after.st_ino, after.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
            or (named_after.st_dev, named_after.st_ino, named_after.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
            or (after_parent.st_dev, after_parent.st_ino) != parent_identity
            or (after_named_parent.st_dev, after_named_parent.st_ino)
            != parent_identity
        ):
            raise LiveProofError("phase9_live_proof_optional_file_changed")
        return b"".join(chunks)
    except LiveProofError:
        raise
    except OSError as error:
        raise LiveProofError("phase9_live_proof_optional_file_invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)


def _parse_canonical_object(raw: bytes, code: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LiveProofError(code) from error
    if type(value) is not dict or _canonical(value) != raw:
        raise LiveProofError(code)
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LiveProofError("phase9_live_proof_json_duplicate_key")
        result[key] = value
    return result


def _reject_nonfinite(unused: str) -> None:
    raise LiveProofError("phase9_live_proof_json_nonfinite")


def _parse_exact_hashed_json_object(raw: bytes, code: str) -> dict[str, object]:
    """Parse exact hash-bound release bytes without imposing whitespace form."""

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except LiveProofError as error:
        raise LiveProofError(code) from error
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LiveProofError(code) from error
    if type(value) is not dict:
        raise LiveProofError(code)
    return value


_RECOVERY_CAPSULE_KEYS: Final = frozenset(
    {
        "schema_version",
        "thread_id",
        "authorization_text_sha256",
        "authorization_namespace",
        "install_scope_id",
        "rollback_scope_id",
        "candidate_git_commit",
        "candidate_git_tree",
        "package_manifest_sha256",
        "controller_runtime_receipt_sha256",
        "controller_contract_sha256",
        "execution_plan_sha256",
        "exact_targets_sha256",
        "public_key_base64",
        "key_id",
        "recovery_capsule_nonce",
        "issued_at",
        "not_before",
        "expires_at",
        "single_use",
        "install_scope",
        "install_authorization",
        "trust_bundle",
        "rollback_delegation",
    }
)


def _parse_utc(value: object) -> datetime:
    if type(value) is not str or re.fullmatch(
        r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
        r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z",
        value,
        re.ASCII,
    ) is None:
        raise LiveProofError("phase9_live_proof_recovery_capsule_time_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise LiveProofError("phase9_live_proof_recovery_capsule_time_invalid") from error


def _decode_public_key(document: Mapping[str, object]) -> tuple[Ed25519PublicKey, str]:
    try:
        encoded = document.get("public_key_base64")
        if type(encoded) is not str:
            raise ValueError
        public = base64.b64decode(encoded, validate=True)
        if len(public) != 32 or base64.b64encode(public).decode("ascii") != encoded:
            raise ValueError
        key_id = _sha(public)
        if document.get("key_id") != key_id:
            raise ValueError
        return Ed25519PublicKey.from_public_bytes(public), key_id
    except (ValueError, TypeError) as error:
        raise LiveProofError("phase9_live_proof_recovery_capsule_public_key_invalid") from error


def _verify_rollback_delegation(
    document: Mapping[str, object], public_key: Ed25519PublicKey, key_id: str
) -> Mapping[str, object]:
    envelope = document.get("rollback_delegation")
    if type(envelope) is not dict or set(envelope) != {
        "schema_version", "payload", "signature"
    }:
        raise LiveProofError("phase9_live_proof_rollback_delegation_invalid")
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if (
        envelope.get("schema_version")
        != "governed-memory-empty-store-recovery-delegation-envelope-v1"
        or type(payload) is not dict
        or type(signature) is not dict
        or set(signature) != {"algorithm", "key_id", "value_base64"}
        or signature.get("algorithm") != "Ed25519"
        or signature.get("key_id") != key_id
    ):
        raise LiveProofError("phase9_live_proof_rollback_delegation_invalid")
    installation_execution_id = payload.get("installation_execution_id")
    if (
        type(installation_execution_id) is not str
        or _HASH_RE.fullmatch(installation_execution_id) is None
    ):
        raise LiveProofError("phase9_live_proof_rollback_delegation_invalid")
    recovery_capsule_nonce = str(document["recovery_capsule_nonce"])
    expected_payload = {
        "schema_version": (
            "governed-memory-empty-store-recovery-delegation-v1"
        ),
        "authorization_namespace": AUTHORIZATION_NAMESPACE,
        "thread_id": THREAD_ID,
        "install_scope_id": INSTALL_SCOPE_ID,
        "rollback_scope_id": ROLLBACK_SCOPE_ID,
        "authorization_text_sha256": AUTHORIZED_TEXT_SHA256,
        "candidate_git_commit": document["candidate_git_commit"],
        "candidate_git_tree": document["candidate_git_tree"],
        "package_manifest_sha256": document["package_manifest_sha256"],
        "controller_runtime_receipt_sha256": document[
            "controller_runtime_receipt_sha256"
        ],
        "controller_contract_sha256": document["controller_contract_sha256"],
        "execution_plan_sha256": document["execution_plan_sha256"],
        "exact_target_contract_sha256": document["exact_targets_sha256"],
        "installation_execution_id": installation_execution_id,
        "rollback_nonce": _sha(
            ROLLBACK_NONCE_DOMAIN + recovery_capsule_nonce.encode("ascii")
        ),
        "recovery_reservation_nonce": _sha(
            RECOVERY_RESERVATION_NONCE_DOMAIN + recovery_capsule_nonce.encode("ascii")
        ),
        "key_id": key_id,
        "issued_at": document["issued_at"],
        "not_before": document["not_before"],
        "expires_at": document["expires_at"],
        "single_use": True,
        "empty_only": True,
        "derivation_policy": dict(RECOVERY_DERIVATION_POLICY),
        "source_postgres_read_count": 0,
        "production_data_read": False,
        "provider_calls": 0,
        "activation_allowed": False,
    }
    if payload != expected_payload:
        raise LiveProofError("phase9_live_proof_rollback_delegation_invalid")
    try:
        raw_signature = base64.b64decode(str(signature["value_base64"]), validate=True)
        if len(raw_signature) != 64:
            raise ValueError
        public_key.verify(raw_signature, _canonical(payload))
    except (InvalidSignature, ValueError, TypeError) as error:
        raise LiveProofError("phase9_live_proof_rollback_delegation_invalid") from error
    return MappingProxyType(dict(envelope))


def _stable_file_identity(value: os.stat_result) -> tuple[int, ...]:
    """Exclude access time, which may change solely because the file was read."""

    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        stat.S_IMODE(value.st_mode),
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _load_verified_recovery_capsule_member(
    inputs: ProofInputs,
    artifacts: Mapping[str, bytes],
    *,
    path: Path,
    allowed_link_counts: frozenset[int],
    require_current: bool = True,
) -> tuple[VerifiedRecoveryCapsule, tuple[int, int]]:
    if (
        type(require_current) is not bool
        or type(path) is not type(RECOVERY_CAPSULE_PATH)
        or path not in {RECOVERY_CAPSULE_PATH, RECOVERY_CAPSULE_STAGING_PATH}
        or not allowed_link_counts
        or not allowed_link_counts.issubset({1, 2})
    ):
        raise LiveProofError("phase9_live_proof_recovery_capsule_policy_invalid")
    parent_fd = descriptor = -1
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(
            path.parent,
            flags | getattr(os, "O_DIRECTORY", 0),
        )
        parent = os.fstat(parent_fd)
        named_parent = path.parent.stat(
            follow_symlinks=False
        )
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != 0
            or parent.st_gid != 0
            or (parent.st_dev, parent.st_ino)
            != (named_parent.st_dev, named_parent.st_ino)
        ):
            raise LiveProofError(
                "phase9_live_proof_recovery_capsule_parent_invalid"
            )
        descriptor = os.open(
            path.name,
            flags,
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        named = os.stat(
            path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o400
            or opened.st_uid != 0
            or opened.st_gid != 0
            or opened.st_nlink not in allowed_link_counts
            or opened.st_size < 1
            or opened.st_size > 64 * 1024
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise LiveProofError("phase9_live_proof_recovery_capsule_identity_invalid")
        raw = os.read(descriptor, opened.st_size + 1)
        after = os.fstat(descriptor)
        named_after = os.stat(
            path.name,
            dir_fd=parent_fd,
            follow_symlinks=False
        )
        parent_after = os.fstat(parent_fd)
        named_parent_after = path.parent.stat(
            follow_symlinks=False
        )
        if (
            len(raw) != opened.st_size
            or _stable_file_identity(after) != _stable_file_identity(opened)
            or _stable_file_identity(named_after)
            != _stable_file_identity(opened)
            or _stable_file_identity(parent_after)
            != _stable_file_identity(parent)
            or _stable_file_identity(named_parent_after)
            != _stable_file_identity(parent)
        ):
            raise LiveProofError("phase9_live_proof_recovery_capsule_changed")
    except LiveProofError:
        raise
    except OSError as error:
        raise LiveProofError("phase9_live_proof_recovery_capsule_unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)
    return (
        _verify_recovery_capsule_raw(
            inputs,
            artifacts,
            raw,
            require_current=require_current,
        ),
        (opened.st_dev, opened.st_ino),
    )


def _load_verified_recovery_capsule(
    inputs: ProofInputs,
    artifacts: Mapping[str, bytes],
    *,
    require_current: bool = True,
) -> VerifiedRecoveryCapsule:
    capsule, unused_inode = _load_verified_recovery_capsule_member(
        inputs,
        artifacts,
        path=RECOVERY_CAPSULE_PATH,
        allowed_link_counts=frozenset({1}),
        require_current=require_current,
    )
    return capsule


def _verify_recovery_capsule_raw(
    inputs: ProofInputs,
    artifacts: Mapping[str, bytes],
    raw: bytes,
    *,
    require_current: bool = True,
) -> VerifiedRecoveryCapsule:
    """Verify capsule bytes after a caller has proved their file identity."""

    if type(require_current) is not bool:
        raise LiveProofError("phase9_live_proof_recovery_capsule_policy_invalid")
    document = _parse_canonical_object(raw, "phase9_live_proof_recovery_capsule_invalid")
    contract_raw = artifacts.get(
        "ops/governed_memory/installation/current/contract.json"
    )
    plan_raw = artifacts.get(
        "ops/governed_memory/installation/current/controller_plan.json"
    )
    if type(contract_raw) is not bytes or type(plan_raw) is not bytes:
        raise LiveProofError("phase9_live_proof_package_artifact_missing")
    contract = _parse_exact_hashed_json_object(
        contract_raw, "phase9_live_proof_contract_invalid"
    )
    exact_targets = contract.get("exact_targets")
    expected = {
        "schema_version": RECOVERY_CAPSULE_SCHEMA,
        "thread_id": THREAD_ID,
        "authorization_text_sha256": AUTHORIZED_TEXT_SHA256,
        "authorization_namespace": AUTHORIZATION_NAMESPACE,
        "install_scope_id": INSTALL_SCOPE_ID,
        "rollback_scope_id": ROLLBACK_SCOPE_ID,
        "candidate_git_commit": inputs.candidate_git_commit,
        "candidate_git_tree": inputs.candidate_git_tree,
        "package_manifest_sha256": inputs.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            inputs.controller_runtime_receipt_sha256
        ),
        "controller_contract_sha256": _sha(contract_raw),
        "execution_plan_sha256": _sha(plan_raw),
        "exact_targets_sha256": _document_sha(exact_targets),
    }
    if (
        set(document) != _RECOVERY_CAPSULE_KEYS
        or type(exact_targets) is not dict
        or any(document.get(key) != value for key, value in expected.items())
        or document.get("single_use") is not True
        or type(document.get("recovery_capsule_nonce")) is not str
        or _HASH_RE.fullmatch(str(document["recovery_capsule_nonce"])) is None
    ):
        raise LiveProofError("phase9_live_proof_recovery_capsule_binding_invalid")
    issued = _parse_utc(document["issued_at"])
    not_before = _parse_utc(document["not_before"])
    expires = _parse_utc(document["expires_at"])
    now = datetime.now(timezone.utc)
    if (
        not_before < issued - timedelta(seconds=10)
        or expires <= not_before
        or (expires - not_before).total_seconds() > 15 * 60
        or (require_current and (now < not_before or now >= expires))
    ):
        raise LiveProofError("phase9_live_proof_recovery_capsule_expired")
    public_key, key_id = _decode_public_key(document)
    recovery_capsule_nonce = str(document["recovery_capsule_nonce"])
    install_nonce = _sha(INSTALL_NONCE_DOMAIN + recovery_capsule_nonce.encode("ascii"))
    rollback_nonce = _sha(ROLLBACK_NONCE_DOMAIN + recovery_capsule_nonce.encode("ascii"))
    install_scope = document.get("install_scope")
    install_authorization = document.get("install_authorization")
    trust_bundle = document.get("trust_bundle")
    if (
        install_scope != _install_scope(inputs, artifacts)
        or type(install_authorization) is not dict
        or type(trust_bundle) is not dict
        or trust_bundle
        != {
            "schema_version": authority.TRUST_BUNDLE_SCHEMA_VERSION,
            "authorization_namespace": AUTHORIZATION_NAMESPACE,
            "keys": [
                {
                    "key_id": key_id,
                    "algorithm": "Ed25519",
                    "public_key_base64": document["public_key_base64"],
                }
            ],
        }
    ):
        raise LiveProofError("phase9_live_proof_install_delegation_invalid")
    scope_raw = _canonical(install_scope)
    auth_payload = install_authorization.get("payload")
    if (
        set(install_authorization)
        != {"schema_version", "payload", "signature"}
        or install_authorization.get("schema_version")
        != authority.AUTHORIZATION_SCHEMA_VERSION
        or type(auth_payload) is not dict
        or auth_payload
        != {
            "schema_version": authority.AUTHORIZATION_PAYLOAD_SCHEMA_VERSION,
            "authorization_id": "phase9-disposable-live-install-auth-000005",
            "authorization_namespace": AUTHORIZATION_NAMESPACE,
            "thread_id": THREAD_ID,
            "scope_id": INSTALL_SCOPE_ID,
            "scope_sha256": _sha(scope_raw),
            "key_id": key_id,
            "approval_phrase": (
                "APPROVE GOVERNED MEMORY DORMANT STORE INSTALL " + _sha(scope_raw)
            ),
            "nonce": install_nonce,
            "issued_at": document["issued_at"],
            "not_before": document["not_before"],
            "expires_at": document["expires_at"],
            "single_use": True,
        }
    ):
        raise LiveProofError("phase9_live_proof_install_delegation_invalid")
    delegation = _verify_rollback_delegation(document, public_key, key_id)
    delegation_payload = delegation.get("payload")
    if (
        type(delegation_payload) is not dict
        or delegation_payload.get("rollback_nonce") != rollback_nonce
    ):
        raise LiveProofError("phase9_live_proof_rollback_delegation_invalid")
    return VerifiedRecoveryCapsule(
        raw=raw,
        document=MappingProxyType(document),
        install_documents=InstallDocuments(
            scope=scope_raw,
            authorization=_canonical(install_authorization),
            trust_bundle=_canonical(trust_bundle),
            key_id=key_id,
        ),
        rollback_delegation=delegation,
        key_id=key_id,
        rollback_nonce=rollback_nonce,
    )


def _safe_release_member(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if (
        type(relative) is not str
        or not relative
        or pure.is_absolute()
        or str(pure) != relative
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise LiveProofError("phase9_live_proof_manifest_path_invalid")
    return root.joinpath(*pure.parts)


def _load_release(inputs: ProofInputs) -> tuple[bytes, Mapping[str, bytes]]:
    release_root = RELEASE_ROOT_PREFIX / inputs.package_manifest_sha256
    manifest_path = release_root / (
        "ops/governed_memory/installation/current/package_manifest.json"
    )
    manifest_raw = _read_regular_no_follow(
        manifest_path, maximum=MAX_ARTIFACT_BYTES
    )
    if _sha(manifest_raw) != inputs.package_manifest_sha256:
        raise LiveProofError("phase9_live_proof_package_identity_mismatch")
    manifest = _parse_exact_hashed_json_object(
        manifest_raw, "phase9_live_proof_package_manifest_invalid"
    )
    index = manifest.get("artifacts")
    if type(index) is not dict or not index:
        raise LiveProofError("phase9_live_proof_package_manifest_invalid")
    artifacts: dict[str, bytes] = {}
    for relative in sorted(index):
        expected = index[relative]
        if (
            type(relative) is not str
            or type(expected) is not str
            or _HASH_RE.fullmatch(expected) is None
        ):
            raise LiveProofError("phase9_live_proof_package_manifest_invalid")
        raw = _read_regular_no_follow(
            _safe_release_member(release_root, relative),
            maximum=MAX_ARTIFACT_BYTES,
        )
        if _sha(raw) != expected:
            raise LiveProofError("phase9_live_proof_package_artifact_mismatch")
        artifacts[relative] = raw
    return manifest_raw, MappingProxyType(artifacts)


def _load_runtime_receipt(inputs: ProofInputs) -> bytes:
    raw = _read_regular_no_follow(
        RUNTIME_RECEIPT_ROOT
        / (inputs.controller_runtime_receipt_sha256 + ".json"),
        maximum=128 * 1024,
    )
    if _sha(raw) != inputs.controller_runtime_receipt_sha256:
        raise LiveProofError("phase9_live_proof_runtime_receipt_mismatch")
    return raw


def _utc_seconds(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _install_scope(
    inputs: ProofInputs,
    artifacts: Mapping[str, bytes],
) -> dict[str, object]:
    contract_raw = artifacts.get(
        "ops/governed_memory/installation/current/contract.json"
    )
    plan_raw = artifacts.get(
        "ops/governed_memory/installation/current/controller_plan.json"
    )
    if type(contract_raw) is not bytes or type(plan_raw) is not bytes:
        raise LiveProofError("phase9_live_proof_package_artifact_missing")
    contract = _parse_exact_hashed_json_object(
        contract_raw, "phase9_live_proof_contract_invalid"
    )
    exact_targets = contract.get("exact_targets")
    if type(exact_targets) is not dict:
        raise LiveProofError("phase9_live_proof_contract_invalid")
    return {
        "schema_version": authority.SCOPE_SCHEMA_VERSION,
        "operation": authority.AUTHORIZATION_OPERATION,
        "authorization_namespace": AUTHORIZATION_NAMESPACE,
        "thread_id": THREAD_ID,
        "scope_id": INSTALL_SCOPE_ID,
        "candidate_git_commit": inputs.candidate_git_commit,
        "candidate_git_tree": inputs.candidate_git_tree,
        "package_manifest_sha256": inputs.package_manifest_sha256,
        "controller_contract_sha256": _sha(contract_raw),
        "execution_plan_sha256": _sha(plan_raw),
        "exact_targets_sha256": _document_sha(exact_targets),
        "controller_runtime_receipt_sha256": (
            inputs.controller_runtime_receipt_sha256
        ),
        "source_boundary": {
            "source_postgres_connection_count": 0,
            "source_postgres_read_count": 0,
            "source_postgres_write_count": 0,
            "source_preparation_phase": (
                "separate_source_preparation_authorization_required"
            ),
        },
        "store_policy": {
            "postgresql": "fresh_isolated_empty",
            "qdrant": "fresh_isolated_empty",
            "legacy_imports_allowed": False,
            "snapshot_restore_allowed": False,
            "unprocessed_prefill_allowed": False,
            "initial_postgresql_user_row_count": 0,
            "initial_qdrant_point_count": 0,
        },
        "secret_policy": {
            "allowed_secret_names": list(authority.ALLOWED_SECRET_NAMES),
            "forbidden_secret_classes": list(
                authority.FORBIDDEN_SECRET_CLASSES
            ),
            "runtime_secret_count": 0,
            "provider_secret_count": 0,
            "supabase_secret_count": 0,
            "service_secret_count": 0,
            "pilot_secret_count": 0,
            "secret_values_present": False,
        },
    }


def _verified_install_context_from_capsule(
    inputs: ProofInputs,
    *,
    package_manifest: bytes,
    artifacts: Mapping[str, bytes],
    runtime_receipt: bytes,
    capsule: VerifiedRecoveryCapsule,
) -> tuple[ProofContext, VerifiedRecoveryCapsule]:
    documents = capsule.install_documents
    scope_document = _parse_canonical_object(
        documents.scope, "phase9_live_proof_scope_invalid"
    )
    bindings = authority.DormantInstallExpectedBindings(
        candidate_git_commit=inputs.candidate_git_commit,
        candidate_git_tree=inputs.candidate_git_tree,
        package_manifest_sha256=inputs.package_manifest_sha256,
        controller_contract_sha256=str(
            scope_document["controller_contract_sha256"]
        ),
        execution_plan_sha256=str(scope_document["execution_plan_sha256"]),
        exact_targets_sha256=str(scope_document["exact_targets_sha256"]),
        controller_runtime_receipt_sha256=(
            inputs.controller_runtime_receipt_sha256
        ),
    )
    scope_capability = authority.verify_dormant_install_execution_capability(
        documents.scope,
        documents.authorization,
        documents.trust_bundle,
        expected_namespace=AUTHORIZATION_NAMESPACE,
        expected_thread_id=THREAD_ID,
        expected_scope_id=INSTALL_SCOPE_ID,
        expected_key_id=documents.key_id,
        expected_trust_bundle_sha256=_sha(documents.trust_bundle),
        expected_bindings=bindings,
    )
    package_capability = verify_install_package_capability(
        scope_capability,
        signed_scope_json=documents.scope,
        package_manifest_json=package_manifest,
        artifact_bytes=artifacts,
    )
    runtime_capability = verify_controller_runtime_capability(
        package_capability,
        runtime_build_receipt_json=runtime_receipt,
    )
    install_material = _install_authority_material(scope_capability)
    delegation_payload = capsule.rollback_delegation.get("payload")
    if (
        type(delegation_payload) is not dict
        or delegation_payload.get("installation_execution_id")
        != install_material["execution_id"]
    ):
        raise LiveProofError("phase9_live_proof_rollback_delegation_invalid")
    recovery_delegation_capability = verify_empty_rollback_recovery_delegation(
        _canonical(dict(capsule.rollback_delegation)),
        documents.trust_bundle,
        expected_namespace=AUTHORIZATION_NAMESPACE,
        expected_thread_id=THREAD_ID,
        expected_install_scope_id=INSTALL_SCOPE_ID,
        expected_rollback_scope_id=ROLLBACK_SCOPE_ID,
        expected_authorization_text_sha256=AUTHORIZED_TEXT_SHA256,
        expected_key_id=documents.key_id,
        expected_trust_bundle_sha256=_sha(documents.trust_bundle),
        expected_installation_execution_id=install_material["execution_id"],
        verified_install_scope_capability=scope_capability,
        verified_package_capability=package_capability,
        verified_controller_runtime_capability=runtime_capability,
    )
    return (
        ProofContext(
            inputs=inputs,
            package_manifest=package_manifest,
            artifacts=artifacts,
            runtime_receipt=runtime_receipt,
            install_documents=documents,
            verified_scope_capability=scope_capability,
            verified_package_capability=package_capability,
            verified_runtime_capability=runtime_capability,
            verified_recovery_delegation_capability=(
                recovery_delegation_capability
            ),
            recovery_capsule=capsule,
        ),
        capsule,
    )


def _verified_install_context(
    inputs: ProofInputs,
    *,
    require_current: bool = True,
) -> tuple[ProofContext, VerifiedRecoveryCapsule]:
    package_manifest, artifacts = _load_release(inputs)
    runtime_receipt = _load_runtime_receipt(inputs)
    capsule = _load_verified_recovery_capsule(
        inputs,
        artifacts,
        require_current=require_current,
    )
    return _verified_install_context_from_capsule(
        inputs,
        package_manifest=package_manifest,
        artifacts=artifacts,
        runtime_receipt=runtime_receipt,
        capsule=capsule,
    )


def _verified_staged_install_context(
    inputs: ProofInputs,
) -> tuple[ProofContext, VerifiedRecoveryCapsule, tuple[int, int]]:
    package_manifest, artifacts = _load_release(inputs)
    runtime_receipt = _load_runtime_receipt(inputs)
    capsule, inode = _load_verified_recovery_capsule_member(
        inputs,
        artifacts,
        path=RECOVERY_CAPSULE_STAGING_PATH,
        allowed_link_counts=frozenset({1, 2}),
        require_current=False,
    )
    context, verified = _verified_install_context_from_capsule(
        inputs,
        package_manifest=package_manifest,
        artifacts=artifacts,
        runtime_receipt=runtime_receipt,
        capsule=capsule,
    )
    return context, verified, inode


def _verified_published_install_context(
    inputs: ProofInputs,
) -> tuple[ProofContext, VerifiedRecoveryCapsule, tuple[int, int]]:
    package_manifest, artifacts = _load_release(inputs)
    runtime_receipt = _load_runtime_receipt(inputs)
    capsule, inode = _load_verified_recovery_capsule_member(
        inputs,
        artifacts,
        path=RECOVERY_CAPSULE_PATH,
        allowed_link_counts=frozenset({1, 2}),
        require_current=False,
    )
    context, verified = _verified_install_context_from_capsule(
        inputs,
        package_manifest=package_manifest,
        artifacts=artifacts,
        runtime_receipt=runtime_receipt,
        capsule=capsule,
    )
    return context, verified, inode


def _verify_preexisting_root_directory(path: Path, mode: int) -> None:
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as error:
        raise LiveProofError("phase9_live_proof_substrate_invalid") from error
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != 0
        or observed.st_gid != 0
        or stat.S_IMODE(observed.st_mode) != mode
    ):
        raise LiveProofError("phase9_live_proof_substrate_invalid")


def _prepare_fixed_substrate() -> None:
    """Validate the separately installed substrate without mutating it."""

    if os.geteuid() != 0:
        raise LiveProofError("phase9_live_proof_root_required")
    for path, mode in (
        (LOCK_ROOT, 0o700),
        (CONTROLLER_STATE_ROOT, 0o700),
        (EXECUTIONS_ROOT, 0o700),
        (CONTROLLER_CONFIG_ROOT, 0o700),
        (STORE_SECRET_PARENT, 0o755),
        (STORE_SECRET_ROOT, 0o700),
    ):
        _verify_preexisting_root_directory(path, mode)


def _verify_host_clock_synchronized() -> bool:
    """Fail closed unless the fixed host synchronization probe says yes."""

    try:
        metadata = TIMEDATECTL_BINARY.stat(follow_symlinks=False)
    except OSError as error:
        raise LiveProofError(
            "phase9_live_proof_clock_preflight_binary_invalid"
        ) from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o755
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
    ):
        raise LiveProofError("phase9_live_proof_clock_preflight_binary_invalid")
    try:
        completed = subprocess.run(
            TIMEDATECTL_SYNCHRONIZATION_ARGV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            env=dict(FIXED_ENVIRONMENT),
            check=False,
            timeout=TIMEDATECTL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LiveProofError("phase9_live_proof_clock_preflight_failed") from error
    if (
        completed.returncode != 0
        or completed.stdout != b"yes\n"
        or completed.stderr != b""
    ):
        raise LiveProofError("phase9_live_proof_clock_not_synchronized")
    try:
        after = TIMEDATECTL_BINARY.stat(follow_symlinks=False)
    except OSError as error:
        raise LiveProofError(
            "phase9_live_proof_clock_preflight_binary_changed"
        ) from error
    if _stable_file_identity(after) != _stable_file_identity(metadata):
        raise LiveProofError("phase9_live_proof_clock_preflight_binary_changed")
    return True


def _authority_state(*, allow_create: bool = True) -> AuthorityState:
    if type(allow_create) is not bool:
        raise LiveProofError("phase9_live_proof_authority_state_invalid")
    try:
        metadata = AUTHORITY_STATE_PATH.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise LiveProofError("phase9_live_proof_authority_state_invalid")
        create = allow_create
    except FileNotFoundError:
        if not allow_create:
            raise LiveProofError(
                "phase9_live_proof_authority_state_absent"
            ) from None
        create = True
    return AuthorityState(
        AUTHORITY_STATE_PATH,
        expected_uid=0,
        create=create,
    )


def _claim_or_verify_recovery_reservation(
    context: ProofContext,
    *,
    state: AuthorityState,
    held_lock: object,
    allow_new_claim: bool,
    require_current: bool,
) -> Mapping[str, object]:
    """Claim or reverify the exact signed public recovery reservation."""

    if type(allow_new_claim) is not bool or type(require_current) is not bool:
        raise LiveProofError("phase9_live_proof_recovery_reservation_invalid")
    try:
        binding = recovery_reservation_binding(
            context.verified_recovery_delegation_capability
        )
        if binding.installation_execution_id != _expected_install_execution_id(
            context
        ):
            raise LiveProofError(
                "phase9_live_proof_recovery_reservation_install_mismatch"
            )
        reading = read_trusted_utc(KernelUtcClock())
        not_before = _parse_utc(binding.not_before)
        expires_at = _parse_utc(binding.expires_at)
        if reading.observed_at < not_before:
            raise LiveProofError(
                "phase9_live_proof_recovery_reservation_not_yet_valid"
            )
        if require_current and reading.observed_at >= expires_at:
            raise LiveProofError(
                "phase9_live_proof_recovery_reservation_expired"
            )
        claim = state.claim_nonce(
            binding.nonce,
            operation=binding.operation,
            execution_sha256=binding.execution_sha256,
            authorization_sha256=binding.authorization_sha256,
            scope_sha256=binding.scope_sha256,
            trust_bundle_sha256=binding.trust_bundle_sha256,
            held_lock=held_lock,
            allow_new_claim=(
                allow_new_claim and reading.observed_at < expires_at
            ),
        )
    except AuthorityClaimNotAllowedError as error:
        raise LiveProofError(
            "phase9_live_proof_recovery_reservation_absent"
        ) from error
    except LiveProofError:
        raise
    except Exception as error:
        raise LiveProofError(
            "phase9_live_proof_recovery_reservation_invalid"
        ) from error
    if (
        claim.operation_sha256
        != authority_operation_sha256(RECOVERY_RESERVATION_OPERATION)
        or claim.execution_sha256 != binding.execution_sha256
        or (
            allow_new_claim
            and claim.result not in {"nonce_claimed", "exact_execution_resumed"}
        )
        or (
            not allow_new_claim
            and claim.result != "exact_execution_resumed"
        )
    ):
        raise LiveProofError(
            "phase9_live_proof_recovery_reservation_invalid"
        )
    return MappingProxyType(
        {
            "result": claim.result,
            "claim_sha256": claim.claim_sha256,
            "execution_sha256": claim.execution_sha256,
        }
    )


def _start_authority_pair_identities(context: ProofContext) -> tuple[object, object]:
    """Derive the exact reservation/install claim pair from one verified capsule."""

    try:
        reservation = recovery_reservation_binding(
            context.verified_recovery_delegation_capability
        )
        install = _install_authority_material(context.verified_scope_capability)
        document = context.recovery_capsule.document
        if (
            reservation.installation_execution_id != install["execution_id"]
            or reservation.not_before != document.get("not_before")
            or reservation.expires_at != document.get("expires_at")
            or reservation.nonce == install["nonce"]
            or reservation.nonce == context.recovery_capsule.rollback_nonce
            or install["nonce"] == context.recovery_capsule.rollback_nonce
        ):
            raise LiveProofError(
                "phase9_live_proof_start_authority_pair_binding_mismatch"
            )
        reservation_identity = derive_nonce_claim_identity(
            reservation.nonce,
            operation=reservation.operation,
            execution_sha256=reservation.execution_sha256,
            authorization_sha256=reservation.authorization_sha256,
            scope_sha256=reservation.scope_sha256,
            trust_bundle_sha256=reservation.trust_bundle_sha256,
        )
        install_identity = derive_nonce_claim_identity(
            install["nonce"],
            operation=install["operation"],
            execution_sha256=install["execution_sha256"],
            authorization_sha256=install["authorization_sha256"],
            scope_sha256=install["scope_sha256"],
            trust_bundle_sha256=install["trust_bundle_sha256"],
        )
    except LiveProofError:
        raise
    except Exception as error:
        raise LiveProofError(
            "phase9_live_proof_start_authority_pair_invalid"
        ) from error
    if (
        reservation_identity.nonce_sha256 == install_identity.nonce_sha256
        or reservation_identity.claim_sha256 == install_identity.claim_sha256
    ):
        raise LiveProofError("phase9_live_proof_start_authority_pair_not_distinct")
    return reservation_identity, install_identity


def _claim_or_verify_start_authority_pair(
    context: ProofContext,
    *,
    state: AuthorityState,
    held_lock: object,
    allow_new_pair: bool,
) -> Mapping[str, object]:
    """Create both start claims atomically, or reverify the exact durable pair."""

    if type(allow_new_pair) is not bool:
        raise LiveProofError("phase9_live_proof_start_authority_pair_invalid")
    reservation_identity, install_identity = _start_authority_pair_identities(context)
    document = context.recovery_capsule.document
    try:
        not_before = _parse_utc(document.get("not_before"))
        expires_at = _parse_utc(document.get("expires_at"))
        reading = read_trusted_utc(KernelUtcClock())
        if reading.observed_at < not_before:
            raise LiveProofError(
                "phase9_live_proof_start_authority_pair_not_yet_valid"
            )
        current = reading.observed_at < expires_at
        pair = state.claim_exact_nonce_pair(
            reservation_identity,
            install_identity,
            held_lock=held_lock,
            allow_new_pair=allow_new_pair and current,
        )
    except AuthorityClaimNotAllowedError as error:
        code = (
            "phase9_live_proof_start_authority_pair_expired"
            if allow_new_pair and not current
            else "phase9_live_proof_start_authority_pair_absent"
        )
        raise LiveProofError(code) from error
    except LiveProofError:
        raise
    except Exception as error:
        raise LiveProofError(
            "phase9_live_proof_start_authority_pair_invalid"
        ) from error
    allowed_results = (
        {"nonce_claimed", "exact_execution_resumed"}
        if allow_new_pair
        else {"exact_execution_resumed"}
    )
    if (
        pair.result not in allowed_results
        or pair.first.claim_sha256 != reservation_identity.claim_sha256
        or pair.second.claim_sha256 != install_identity.claim_sha256
        or pair.first.operation_sha256
        != authority_operation_sha256(RECOVERY_RESERVATION_OPERATION)
        or pair.second.operation_sha256
        != authority_operation_sha256(authority.AUTHORIZATION_OPERATION)
    ):
        raise LiveProofError("phase9_live_proof_start_authority_pair_invalid")
    return MappingProxyType(
        {
            "result": pair.result,
            "recovery_reservation_claim_sha256": pair.first.claim_sha256,
            "install_authority_claim_sha256": pair.second.claim_sha256,
            "start_authority_pair_claimed_atomically": True,
        }
    )


def _start_authority_pair_receipt(
    *,
    pair: Mapping[str, object],
    capsule: VerifiedRecoveryCapsule,
    inode: tuple[int, int],
) -> Mapping[str, object]:
    receipt = {
        "result": pair["result"],
        "recovery_capsule_sha256": _sha(capsule.raw),
        "recovery_capsule_device": inode[0],
        "recovery_capsule_inode": inode[1],
        "recovery_reservation_claim_sha256": pair[
            "recovery_reservation_claim_sha256"
        ],
        "install_authority_claim_sha256": pair[
            "install_authority_claim_sha256"
        ],
        "start_authority_pair_claimed_atomically": True,
    }
    if (
        receipt["result"] not in {"nonce_claimed", "exact_execution_resumed"}
        or any(
            type(receipt[key]) is not str
            or _HASH_RE.fullmatch(str(receipt[key])) is None
            for key in (
                "recovery_capsule_sha256",
                "recovery_reservation_claim_sha256",
                "install_authority_claim_sha256",
            )
        )
        or type(receipt["recovery_capsule_device"]) is not int
        or receipt["recovery_capsule_device"] < 0
        or type(receipt["recovery_capsule_inode"]) is not int
        or receipt["recovery_capsule_inode"] <= 0
    ):
        raise LiveProofError("phase9_live_proof_start_authority_pair_invalid")
    return MappingProxyType(receipt)


def preclaim_staged_start_authority_pair(
    inputs: ProofInputs,
) -> Mapping[str, object]:
    """Atomically claim the pair from the one complete unpublished capsule."""

    _require_runner_process_authority(RunnerMode.PRECLAIM_STAGED_START)
    _verify_host_clock_synchronized()
    _prepare_fixed_substrate()
    context, capsule, inode = _verified_staged_install_context(inputs)
    with GlobalExecutionLock(GLOBAL_LOCK_PATH, expected_uid=0) as lock:
        held = lock.held_capability()
        state = _authority_state(allow_create=True)
        pair = _claim_or_verify_start_authority_pair(
            context,
            state=state,
            held_lock=held,
            allow_new_pair=True,
        )
        observed, observed_inode = _load_verified_recovery_capsule_member(
            inputs,
            context.artifacts,
            path=RECOVERY_CAPSULE_STAGING_PATH,
            allowed_link_counts=frozenset({1, 2}),
            require_current=False,
        )
        if observed.raw != capsule.raw or observed_inode != inode:
            raise LiveProofError(
                "phase9_live_proof_staged_recovery_capsule_changed"
            )
    return _start_authority_pair_receipt(
        pair=pair,
        capsule=capsule,
        inode=inode,
    )


def verify_published_start_authority_pair(
    inputs: ProofInputs,
) -> Mapping[str, object]:
    """Reverify, without creating, the pair behind a published capsule."""

    _require_runner_process_authority(
        RunnerMode.VERIFY_PUBLISHED_START_PAIR
    )
    _verify_host_clock_synchronized()
    _prepare_fixed_substrate()
    context, capsule, inode = _verified_published_install_context(inputs)
    with GlobalExecutionLock(GLOBAL_LOCK_PATH, expected_uid=0) as lock:
        held = lock.held_capability()
        state = _authority_state(allow_create=False)
        pair = _claim_or_verify_start_authority_pair(
            context,
            state=state,
            held_lock=held,
            allow_new_pair=False,
        )
    return _start_authority_pair_receipt(
        pair=pair,
        capsule=capsule,
        inode=inode,
    )


def _verify_start_authority_pair_before_install(
    context: ProofContext,
) -> Mapping[str, object]:
    with GlobalExecutionLock(GLOBAL_LOCK_PATH, expected_uid=0) as lock:
        held = lock.held_capability()
        _verify_host_clock_synchronized()
        state = _authority_state(allow_create=False)
        return _claim_or_verify_start_authority_pair(
            context,
            state=state,
            held_lock=held,
            allow_new_pair=False,
        )


def _install_prerequisites(context: ProofContext) -> InstallPrerequisites:
    store_raw = context.artifacts.get(
        "ops/governed_memory/installation/store_spec-v5.json"
    )
    if type(store_raw) is not bytes:
        raise LiveProofError("phase9_live_proof_store_spec_missing")
    store_spec = validate_store_spec(
        _parse_exact_hashed_json_object(
            store_raw, "phase9_live_proof_store_spec_invalid"
        ),
        allow_placeholders=True,
    )
    expectations = expectations_from_store_spec(store_spec)
    local_images = inspect_exact_store_image_set(
        CommandRunner(command_profile="image_inspect"), expectations
    )
    runtime = verified_controller_runtime_evidence(
        context.verified_runtime_capability
    )
    package = verified_package_evidence(context.verified_package_capability)
    return InstallPrerequisites(
        store_spec_sha256=package.store_spec_sha256,
        controller_runtime_receipt_sha256=(
            runtime.controller_runtime_receipt_sha256
        ),
        controller_runtime_root=runtime.runtime_root,
        controller_runtime_tree_sha256=runtime.runtime_tree_sha256,
        controller_release_root=runtime.release_root,
        controller_release_tree_sha256=runtime.release_tree_sha256,
        controller_release_package_manifest_path=runtime.package_manifest_path,
        controller_runtime_interpreter_path=runtime.interpreter_path,
        controller_runtime_interpreter_sha256=runtime.interpreter_sha256,
        controller_runtime_inventory_path=runtime.inventory_path,
        controller_runtime_inventory_sha256=(
            runtime.installed_distribution_inventory_sha256
        ),
        controller_requirements_lock_sha256=(
            runtime.controller_requirements_lock_sha256
        ),
        supervisor_launcher_path=runtime.supervisor_launcher_path,
        supervisor_launcher_sha256=runtime.supervisor_launcher_sha256,
        local_images=local_images,
        expected_images=expectations,
    )


def _run_install_worker(
    context: ProofContext,
    *,
    resume_only: bool,
) -> Mapping[str, object]:
    if type(resume_only) is not bool:
        raise LiveProofError("phase9_live_proof_install_mode_invalid")
    with GlobalExecutionLock(GLOBAL_LOCK_PATH, expected_uid=0) as lock:
        held = lock.held_capability()
        _verify_host_clock_synchronized()
        state = _authority_state(allow_create=False)
        return _execute_install_locked(
            context,
            state=state,
            held_lock=held,
            resume_only=resume_only,
        )


def _execute_install_locked(
    context: ProofContext,
    *,
    state: AuthorityState,
    held_lock: object,
    resume_only: bool,
) -> Mapping[str, object]:
    """Execute one install mode under the caller's continuously held lock."""

    if type(resume_only) is not bool:
        raise LiveProofError("phase9_live_proof_install_mode_invalid")
    try:
        _claim_or_verify_recovery_reservation(
            context,
            state=state,
            held_lock=held_lock,
            allow_new_claim=False,
            require_current=False,
        )
        receipt_store = DurableReceiptStore.production()
        prerequisites = _install_prerequisites(context)
        install_function = (
            resume_authorized_dormant_store_install
            if resume_only
            else run_authorized_dormant_store_install
        )
        return install_function(
            verified_scope_capability=context.verified_scope_capability,
            verified_package_capability=context.verified_package_capability,
            verified_controller_runtime_capability=(
                context.verified_runtime_capability
            ),
            authority_state=state,
            clock=KernelUtcClock(),
            held_lock=held_lock,
            prerequisites=prerequisites,
            receipt_store=receipt_store,
        )
    except LiveProofError:
        raise
    except Exception as error:
        internal_code = _sanitized_internal_install_failure_code(error)
        public_code = "phase9_live_proof_install_execution_failed"
        if internal_code is not None:
            public_code += "_" + internal_code
        raise LiveProofError(public_code) from error


def _expected_install_execution_id(context: ProofContext) -> str:
    """Derive the exact install execution ID before the first worker fork."""
    return _install_authority_material(context.verified_scope_capability)[
        "execution_id"
    ]


def _resolved_store_spec(
    context: ProofContext,
    *,
    state: AuthorityState,
    held_lock: object,
) -> dict[str, object]:
    """Reconstruct exact spec from sealed bytes and compare live copy if present."""

    try:
        claimed = resume_dormant_store_install_execution_binding(
            context.verified_scope_capability,
            verified_package_capability=context.verified_package_capability,
            verified_controller_runtime_capability=(
                context.verified_runtime_capability
            ),
            state=state,
            clock=KernelUtcClock(),
            held_lock=held_lock,
        )
        reconstructed = reconstruct_resolved_store_spec_from_claimed_binding(
            claimed,
            verified_package_capability=context.verified_package_capability,
        )
    except Exception as error:
        raise LiveProofError(
            "phase9_live_proof_resolved_store_spec_reconstruction_failed"
        ) from error
    expected = _canonical(reconstructed) + b"\n"
    raw = _read_optional_root_regular_no_follow(
        RESOLVED_STORE_SPEC_PATH,
        maximum=16 * 1024 * 1024,
    )
    if raw is not None and raw != expected:
        raise LiveProofError("phase9_live_proof_resolved_store_spec_invalid")
    return validate_store_spec(reconstructed, allow_placeholders=False)


def _load_install_resources(
    *,
    install_receipt: Mapping[str, object],
    resolved_store_spec: Mapping[str, object],
) -> object:
    verified = verify_install_receipt(install_receipt)
    execution_id = str(verified["execution_id"])
    binding = resolved_store_spec.get("execution_binding")
    if type(binding) is not dict or binding.get("execution_id") != execution_id:
        raise LiveProofError("phase9_live_proof_install_binding_mismatch")
    journal_binding = binding.get("binding_sha256")
    if type(journal_binding) is not str or _HASH_RE.fullmatch(journal_binding) is None:
        raise LiveProofError("phase9_live_proof_install_binding_mismatch")
    ledger_binding = resource_ledger_binding_sha256(journal_binding)
    records = load_ledger(
        EXECUTIONS_ROOT / execution_id / "resources.jsonl",
        expected_binding_sha256=ledger_binding,
        expected_uid=0,
    )
    if (
        not records
        or records[-1].entry_sha256
        != verified["resource_ledger_head_sha256"]
        or len(records) != verified["resource_ledger_sequence"]
    ):
        raise LiveProofError("phase9_live_proof_install_ledger_mismatch")
    return derive_exact_rollback_resources_from_ledger(
        records,
        package_manifest_sha256=str(verified["package_manifest_sha256"]),
        expected_ledger_head_sha256=str(
            verified["resource_ledger_head_sha256"]
        ),
    )


def _rollback_eligibility_seed(
    *,
    install_receipt: Mapping[str, object],
    resources: object,
) -> Mapping[str, object]:
    install = verify_install_receipt(install_receipt)
    evidence, unused = verified_rollback_resource_parts(resources)
    expectation_sha256 = _document_sha(
        {
            "schema_version": (
                "governed-memory-phase9-zero-state-expectation-v1"
            ),
            "installation_receipt_sha256": install["receipt_sha256"],
            "pilot_ever_started": False,
            "postgresql_user_rows": 0,
            "projection_queue_rows": 0,
            "qdrant_points": 0,
            "active_clients": 0,
            "legacy_imports": 0,
            "source_postgres_read_count": 0,
            "production_data_read": False,
            "provider_calls": 0,
        }
    )
    return MappingProxyType(
        build_empty_rollback_eligibility_receipt(
            candidate_git_commit=str(install["candidate_git_commit"]),
            candidate_git_tree=str(install["candidate_git_tree"]),
            package_manifest_sha256=str(install["package_manifest_sha256"]),
            installation_execution_id=str(install["execution_id"]),
            installation_receipt_sha256=str(install["receipt_sha256"]),
            exact_targets_sha256=evidence.exact_targets_sha256,
            resource_ledger_head_sha256=(
                evidence.resource_ledger_head_sha256
            ),
            observation_set_sha256=expectation_sha256,
            pilot_ever_started=False,
            postgresql_user_rows=0,
            projection_queue_rows=0,
            qdrant_points=0,
            active_clients=0,
            legacy_imports=0,
            application_services_installed=False,
            source_postgres_read_count=0,
            production_data_read=False,
            provider_calls=0,
        )
    )


def _build_rollback_documents(
    *,
    context: ProofContext,
    install_receipt: Mapping[str, object],
    state: AuthorityState | None = None,
    held_lock: object | None = None,
) -> RollbackDocuments:
    install = verify_install_receipt(install_receipt)
    if state is None or held_lock is None:
        with GlobalExecutionLock(GLOBAL_LOCK_PATH, expected_uid=0) as lock:
            return _build_rollback_documents(
                context=context,
                install_receipt=install,
                state=_authority_state(allow_create=False),
                held_lock=lock.held_capability(),
            )
    resolved = _resolved_store_spec(
        context,
        state=state,
        held_lock=held_lock,
    )
    resources = _load_install_resources(
        install_receipt=install, resolved_store_spec=resolved
    )
    eligibility = _rollback_eligibility_seed(
        install_receipt=install, resources=resources
    )
    plan_sha256 = empty_rollback_plan_sha256(eligibility, resources)
    evidence, unused = verified_rollback_resource_parts(resources)
    scope = {
        "schema_version": ROLLBACK_SCOPE_SCHEMA,
        "operation": ROLLBACK_OPERATION,
        "authorization_namespace": AUTHORIZATION_NAMESPACE,
        "thread_id": THREAD_ID,
        "scope_id": ROLLBACK_SCOPE_ID,
        "candidate_git_commit": context.inputs.candidate_git_commit,
        "candidate_git_tree": context.inputs.candidate_git_tree,
        "package_manifest_sha256": context.inputs.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            context.inputs.controller_runtime_receipt_sha256
        ),
        "installation_execution_id": install["execution_id"],
        "installation_receipt_sha256": install["receipt_sha256"],
        "rollback_plan_sha256": plan_sha256,
        "exact_targets_sha256": evidence.exact_targets_sha256,
        "eligibility_receipt_sha256": eligibility["receipt_sha256"],
        "resource_ledger_head_sha256": evidence.resource_ledger_head_sha256,
        "empty_only_policy": {
            "pilot_ever_started": False,
            "postgresql_user_rows": 0,
            "projection_queue_rows": 0,
            "qdrant_points": 0,
            "active_clients": 0,
            "legacy_imports": 0,
        },
        "retention_policy": {
            "authorization_nonce_retained": True,
            "execution_journal_retained": True,
            "authority_anchor_retained": True,
            "resource_identity_ledger_retained": True,
            "install_and_rollback_receipts_retained": True,
        },
    }
    scope_raw = _canonical(scope)
    capsule = context.recovery_capsule
    key_id = capsule.key_id
    return RollbackDocuments(
        scope=scope_raw,
        authorization=_canonical(dict(capsule.rollback_delegation)),
        trust_bundle=capsule.install_documents.trust_bundle,
        key_id=key_id,
        eligibility=eligibility,
        resources=resources,
        resolved_store_spec=resolved,
    )


def _verified_rollback_capability(context: ProofContext) -> object:
    documents = context.rollback_documents
    if documents is None:
        raise LiveProofError("phase9_live_proof_rollback_context_absent")
    eligibility = documents.eligibility
    resource_evidence, unused = verified_rollback_resource_parts(
        documents.resources
    )
    bindings = EmptyRollbackExpectedBindings(
        candidate_git_commit=context.inputs.candidate_git_commit,
        candidate_git_tree=context.inputs.candidate_git_tree,
        package_manifest_sha256=context.inputs.package_manifest_sha256,
        controller_runtime_receipt_sha256=(
            context.inputs.controller_runtime_receipt_sha256
        ),
        installation_execution_id=str(
            eligibility["installation_execution_id"]
        ),
        installation_nonce_sha256=(
            verified_dormant_install_authority_identity(
                context.verified_scope_capability
            ).authorization_nonce_sha256
        ),
        installation_receipt_sha256=str(
            eligibility["installation_receipt_sha256"]
        ),
        rollback_plan_sha256=empty_rollback_plan_sha256(
            eligibility, documents.resources
        ),
        exact_targets_sha256=resource_evidence.exact_targets_sha256,
        eligibility_receipt_sha256=str(eligibility["receipt_sha256"]),
        resource_ledger_head_sha256=(
            resource_evidence.resource_ledger_head_sha256
        ),
    )
    return derive_empty_rollback_execution_capability_from_recovery_delegation(
        context.verified_recovery_delegation_capability,
        documents.scope,
        expected_bindings=bindings,
    )


def _run_rollback_worker(
    context: ProofContext,
    *,
    start_from_reservation: bool,
) -> Mapping[str, object]:
    if type(start_from_reservation) is not bool:
        raise LiveProofError("phase9_live_proof_rollback_mode_invalid")
    documents = context.rollback_documents
    if documents is None:
        raise LiveProofError("phase9_live_proof_rollback_context_absent")
    with GlobalExecutionLock(GLOBAL_LOCK_PATH, expected_uid=0) as lock:
        held = lock.held_capability()
        _verify_host_clock_synchronized()
        state = _authority_state(allow_create=False)
        return _execute_rollback_locked(
            context,
            state=state,
            held_lock=held,
            start_from_reservation=start_from_reservation,
        )


def _execute_rollback_locked(
    context: ProofContext,
    *,
    state: AuthorityState,
    held_lock: object,
    start_from_reservation: bool,
) -> Mapping[str, object]:
    """Execute one rollback mode under the caller's continuously held lock."""

    if type(start_from_reservation) is not bool:
        raise LiveProofError("phase9_live_proof_rollback_mode_invalid")
    documents = context.rollback_documents
    if documents is None:
        raise LiveProofError("phase9_live_proof_rollback_context_absent")
    try:
        _claim_or_verify_recovery_reservation(
            context,
            state=state,
            held_lock=held_lock,
            allow_new_claim=False,
            require_current=False,
        )
        capability = _verified_rollback_capability(context)
        receipt_store = DurableReceiptStore.production()

        rollback_function = (
            start_reserved_authorized_empty_store_rollback
            if start_from_reservation
            else resume_authorized_empty_store_rollback
        )
        receipt = rollback_function(
            verified_rollback_capability=capability,
            verified_package_capability=context.verified_package_capability,
            verified_controller_runtime_capability=(
                context.verified_runtime_capability
            ),
            eligibility_receipt=documents.eligibility,
            resources=documents.resources,
            authority_state=state,
            clock=KernelUtcClock(),
            held_lock=held_lock,
            resolved_store_spec=documents.resolved_store_spec,
            receipt_store=receipt_store,
        )
        return dict(receipt.canonical_receipt)
    except LiveProofError:
        raise
    except Exception as error:
        raise LiveProofError("phase9_live_proof_rollback_execution_failed") from error


def _expected_rollback_execution_id(context: ProofContext) -> str:
    capability = _verified_rollback_capability(context)
    evidence = rollback_capability_evidence(capability)
    runtime_binding = {
        "package_manifest_sha256": evidence.package_manifest_sha256,
        "installation_execution_id": evidence.installation_execution_id,
        "controller_runtime_receipt_sha256": (
            evidence.controller_runtime_receipt_sha256
        ),
        "controller_runtime_root": evidence.controller_runtime_root,
        "controller_runtime_tree_sha256": (
            evidence.controller_runtime_tree_sha256
        ),
        "controller_release_root": evidence.controller_release_root,
        "controller_release_tree_sha256": (
            evidence.controller_release_tree_sha256
        ),
        "controller_release_package_manifest_path": (
            evidence.controller_release_package_manifest_path
        ),
        "controller_runtime_interpreter_path": (
            evidence.controller_runtime_interpreter_path
        ),
        "controller_runtime_interpreter_sha256": (
            evidence.controller_runtime_interpreter_sha256
        ),
        "controller_runtime_inventory_path": (
            evidence.controller_runtime_inventory_path
        ),
        "controller_runtime_inventory_sha256": (
            evidence.controller_runtime_inventory_sha256
        ),
        "controller_requirements_lock_sha256": (
            evidence.controller_requirements_lock_sha256
        ),
        "supervisor_launcher_path": evidence.supervisor_launcher_path,
        "supervisor_launcher_sha256": evidence.supervisor_launcher_sha256,
    }
    execution_sha = _sha(
        b"governed-memory-empty-rollback-execution-v3\x00"
        + _canonical(
            {
                "operation": evidence.operation,
                "scope_sha256": evidence.scope_sha256,
                "authorization_sha256": evidence.authorization_sha256,
                "trust_bundle_sha256": evidence.trust_bundle_sha256,
                "plan_sha256": evidence.rollback_plan_sha256,
                **runtime_binding,
            }
        )
    )
    return execution_sha


def _run_absence_worker(context: ProofContext) -> Mapping[str, object]:
    # A completed public rollback replay does not trust the earlier receipt.
    # It reacquires the exact ledger-bound composition and re-proves terminal
    # resource absence before returning the same durable receipt.
    replay_receipt = _run_rollback_worker(
        context,
        start_from_reservation=False,
    )
    return _terminal_absence_from_replay(context, replay_receipt)


def _terminal_absence_from_replay(
    context: ProofContext,
    replay_receipt: Mapping[str, object],
) -> Mapping[str, object]:
    """Bind terminal absence to one just-completed public rollback replay."""

    replay = verify_empty_rollback_receipt(replay_receipt)
    retained_runtime = verify_controller_runtime_capability(
        context.verified_package_capability,
        runtime_build_receipt_json=context.runtime_receipt,
    )
    result = {
        "completed_public_rollback_replayed": True,
        "exact_resources_absent": replay["exact_resources_absent"],
        "stores_installed": replay["stores_installed"],
        "stores_supervisor_installed": replay[
            "stores_supervisor_installed"
        ],
        "controller_runtime_capability_reverified": (
            retained_runtime is not None
        ),
    }
    if (
        result["exact_resources_absent"] is not True
        or result["stores_installed"] is not False
        or result["stores_supervisor_installed"] is not False
        or result["controller_runtime_capability_reverified"] is not True
    ):
        raise LiveProofError("phase9_live_proof_terminal_absence_failed")
    return MappingProxyType(result)


def _dispatch_worker(
    mode: WorkerMode, context: ProofContext
) -> Mapping[str, object]:
    if mode is WorkerMode.START_OR_RECOVER:
        return _run_start_or_recovery_selection_worker(context)
    if mode is WorkerMode.RECOVER_ONLY:
        return _run_recovery_selection_worker(context)
    if mode is WorkerMode.INSTALL:
        return _run_install_worker(context, resume_only=False)
    if mode is WorkerMode.RESUME_INSTALL:
        return _run_install_worker(context, resume_only=True)
    if mode is WorkerMode.ROLLBACK:
        return _run_rollback_worker(
            context,
            start_from_reservation=True,
        )
    if mode is WorkerMode.RESUME_ROLLBACK:
        return _run_rollback_worker(
            context,
            start_from_reservation=False,
        )
    if mode is WorkerMode.VERIFY_ABSENCE:
        return _run_absence_worker(context)
    raise LiveProofError("phase9_live_proof_worker_mode_refused")


def _cooperative_boundary_target(
    mode: WorkerMode,
    boundary_kind: str | None,
) -> tuple[type[object], str, str] | None:
    """Select the only two proof-only post-fsync stop boundaries."""

    if boundary_kind is None:
        return None
    if (
        boundary_kind == process_death_arm_receipt.INSTALL_BOUNDARY_KIND
        and mode is WorkerMode.RESUME_INSTALL
    ):
        return DurableJournal, "append_journal", INSTALL_KILL_STEP
    if (
        boundary_kind
        == process_death_arm_receipt.EMPTY_ROLLBACK_BOUNDARY_KIND
        and mode is WorkerMode.ROLLBACK
    ):
        return DurableRollbackJournal, "append", ROLLBACK_KILL_STEP
    raise LiveProofError(
        "phase9_live_proof_cooperative_stop_boundary_invalid"
    )


def _dispatch_worker_with_cooperative_boundary_stop(
    mode: WorkerMode,
    context: ProofContext,
    *,
    boundary_kind: str | None,
) -> Mapping[str, object]:
    """Self-stop immediately after the exact durable APPLIED append returns.

    The patch exists only inside one forked sealed proof worker.  The public
    install and rollback entrypoints and their dependency surfaces remain
    unchanged.  If the worker is ever continued instead of killed, it refuses
    before the controller can dispatch the next effect.
    """

    target = _cooperative_boundary_target(mode, boundary_kind)
    if target is None:
        return _dispatch_worker(mode, context)
    journal_type, method_name, step_id = target
    original = getattr(journal_type, method_name, None)
    if not callable(original):
        raise LiveProofError(
            "phase9_live_proof_cooperative_stop_boundary_invalid"
        )
    stopped = False

    def append_then_stop(instance: object, record: object) -> None:
        nonlocal stopped
        original(instance, record)
        if (
            getattr(record, "step_id", None) == step_id
            and getattr(record, "event", None) == JOURNAL_APPLIED_EVENT
        ):
            if stopped:
                raise _CooperativeBoundaryStopAbort(
                    "phase9_live_proof_cooperative_stop_reentered"
                )
            stopped = True
            try:
                os.kill(os.getpid(), signal.SIGSTOP)
            except OSError as error:
                raise _CooperativeBoundaryStopAbort(
                    "phase9_live_proof_cooperative_stop_failed"
                ) from error
            raise _CooperativeBoundaryStopAbort(
                "phase9_live_proof_cooperative_stop_unexpectedly_resumed"
            )

    setattr(journal_type, method_name, append_then_stop)
    try:
        result = _dispatch_worker(mode, context)
    finally:
        setattr(journal_type, method_name, original)
    if not stopped:
        raise LiveProofError(
            "phase9_live_proof_cooperative_stop_boundary_not_reached"
        )
    return result


def _child_write(descriptor: int, value: Mapping[str, object]) -> None:
    raw = _canonical(value)
    if len(raw) > MAX_WORKER_RESULT_BYTES:
        raise LiveProofError("phase9_live_proof_worker_result_too_large")
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise LiveProofError("phase9_live_proof_worker_pipe_failed")
        view = view[written:]


def _arm_worker_parent_death(expected_parent_pid: int) -> None:
    """Kill an orphaned Linux worker before it can dispatch any effect."""

    if (
        sys.platform != "linux"
        or type(expected_parent_pid) is not int
        or expected_parent_pid <= 1
    ):
        raise LiveProofError("phase9_live_proof_worker_parent_fence_invalid")
    try:
        library = ctypes.CDLL(None, use_errno=True)
        prctl = library.prctl
        prctl.argtypes = (
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        )
        prctl.restype = ctypes.c_int
        result = prctl(
            PR_SET_PDEATHSIG,
            int(signal.SIGKILL),
            0,
            0,
            0,
        )
    except (AttributeError, OSError, TypeError, ValueError) as error:
        raise LiveProofError(
            "phase9_live_proof_worker_parent_fence_invalid"
        ) from error
    if result != 0:
        raise LiveProofError("phase9_live_proof_worker_parent_fence_invalid")
    # PR_SET_PDEATHSIG does not retroactively signal when the parent died
    # between fork and prctl.  Refuse that race before dispatch.
    if os.getppid() != expected_parent_pid:
        try:
            os.kill(os.getpid(), signal.SIGKILL)
        finally:
            os._exit(1)


def _adopt_inherited_live_proof_guard() -> GlobalExecutionLock:
    """Revalidate the issuer-owned operation guard inside each worker."""

    try:
        return GlobalExecutionLock.from_inherited_descriptor(
            LIVE_PROOF_GUARD_PATH,
            LIVE_PROOF_GUARD_FD,
            expected_uid=0,
            expected_gid=0,
        )
    except Exception as error:
        raise LiveProofError("phase9_live_proof_worker_guard_invalid") from error


def _fork_worker(
    mode: WorkerMode,
    context: ProofContext,
    *,
    cooperative_boundary_kind: str | None = None,
) -> tuple[int, int]:
    _cooperative_boundary_target(mode, cooperative_boundary_kind)
    if not hasattr(os, "fork"):
        raise LiveProofError("phase9_live_proof_linux_fork_required")
    try:
        read_fd, write_fd = os.pipe()
    except OSError as error:
        raise LiveProofError("phase9_live_proof_worker_pipe_failed") from error
    expected_parent_pid = os.getpid()
    try:
        pid = os.fork()
    except OSError as error:
        for descriptor in (read_fd, write_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise LiveProofError("phase9_live_proof_worker_fork_failed") from error
    if pid == 0:
        os.close(read_fd)
        exit_code = 1
        try:
            _arm_worker_parent_death(expected_parent_pid)
            with _adopt_inherited_live_proof_guard():
                result = _dispatch_worker_with_cooperative_boundary_stop(
                    mode,
                    context,
                    boundary_kind=cooperative_boundary_kind,
                )
                _child_write(write_fd, {"ok": True, "result": dict(result)})
                exit_code = 0
        except BaseException as error:
            code = str(error)
            if not code or any(character in code for character in "\r\n\x00"):
                code = "phase9_live_proof_worker_failed"
            try:
                _child_write(write_fd, {"ok": False, "error_code": code})
            except BaseException:
                pass
        finally:
            os.close(write_fd)
            os._exit(exit_code)
    try:
        os.close(write_fd)
    except OSError as error:
        try:
            _terminate_and_reap_worker(pid, read_fd)
        except BaseException as cleanup_error:
            raise LiveProofError(
                "phase9_live_proof_worker_cleanup_unproved"
            ) from cleanup_error
        raise LiveProofError(
            "phase9_live_proof_worker_parent_setup_failed"
        ) from error
    return pid, read_fd


def _read_pipe(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            block = os.read(
                descriptor, min(65536, MAX_WORKER_RESULT_BYTES + 1 - total)
            )
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > MAX_WORKER_RESULT_BYTES:
                raise LiveProofError("phase9_live_proof_worker_result_too_large")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _wait_worker(pid: int, read_fd: int) -> Mapping[str, object]:
    """Bound, own and reap one exact worker while draining its result pipe."""

    deadline = time.monotonic() + WORKER_COMPLETION_TIMEOUT_SECONDS
    data = bytearray()
    descriptor_owned = True
    child_owned = True
    status: int | None = None
    try:
        while child_owned or descriptor_owned:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LiveProofError("phase9_live_proof_worker_timeout")
            if child_owned:
                try:
                    waited, observed_status = os.waitpid(pid, os.WNOHANG)
                except InterruptedError:
                    continue
                except OSError as error:
                    raise LiveProofError(
                        "phase9_live_proof_worker_wait_failed"
                    ) from error
                if waited == pid:
                    child_owned = False
                    status = observed_status
                elif waited != 0:
                    raise LiveProofError(
                        "phase9_live_proof_worker_wait_failed"
                    )
            if descriptor_owned:
                try:
                    ready, unused_write, unused_exception = select.select(
                        [read_fd], [], [], min(remaining, POLL_INTERVAL_SECONDS)
                    )
                except (OSError, ValueError) as error:
                    raise LiveProofError(
                        "phase9_live_proof_worker_pipe_failed"
                    ) from error
                if ready:
                    try:
                        block = os.read(
                            read_fd,
                            min(
                                65536,
                                MAX_WORKER_RESULT_BYTES + 1 - len(data),
                            ),
                        )
                    except OSError as error:
                        raise LiveProofError(
                            "phase9_live_proof_worker_pipe_failed"
                        ) from error
                    if not block:
                        os.close(read_fd)
                        descriptor_owned = False
                    else:
                        data.extend(block)
                        if len(data) > MAX_WORKER_RESULT_BYTES:
                            raise LiveProofError(
                                "phase9_live_proof_worker_result_too_large"
                            )
            elif child_owned:
                time.sleep(min(remaining, POLL_INTERVAL_SECONDS))
    except BaseException as error:
        cleanup_error: BaseException | None = None
        if child_owned:
            try:
                _terminate_and_reap_worker(
                    pid, read_fd if descriptor_owned else None
                )
                child_owned = False
                descriptor_owned = False
            except BaseException as observed:
                cleanup_error = observed
        elif descriptor_owned:
            try:
                os.close(read_fd)
                descriptor_owned = False
            except OSError as observed:
                cleanup_error = observed
        if cleanup_error is not None:
            raise LiveProofError(
                "phase9_live_proof_worker_cleanup_unproved"
            ) from cleanup_error
        raise error
    if status is None:
        raise LiveProofError("phase9_live_proof_worker_wait_failed")
    raw = bytes(data)
    try:
        document = _parse_canonical_object(
            raw, "phase9_live_proof_worker_result_invalid"
        )
    except LiveProofError:
        raise LiveProofError("phase9_live_proof_worker_failed") from None
    if (
        not os.WIFEXITED(status)
        or os.WEXITSTATUS(status) != 0
        or document.get("ok") is not True
        or type(document.get("result")) is not dict
    ):
        code = document.get("error_code")
        raise LiveProofError(
            code if type(code) is str else "phase9_live_proof_worker_failed"
        )
    return MappingProxyType(dict(document["result"]))


def _terminate_and_reap_worker(
    pid: int, read_fd: int | None
) -> int | None:
    """Boundedly terminate and reap one exact owned child process."""

    if read_fd is not None:
        try:
            os.close(read_fd)
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        # Reaping below is still authoritative for whether the child remains.
        pass
    deadline = time.monotonic() + WORKER_CLEANUP_TIMEOUT_SECONDS
    while True:
        try:
            waited, status = os.waitpid(pid, os.WNOHANG)
        except InterruptedError:
            continue
        except ChildProcessError:
            return None
        if waited == pid:
            return status
        if waited != 0:
            raise LiveProofError("phase9_live_proof_worker_cleanup_unproved")
        if time.monotonic() >= deadline:
            raise LiveProofError("phase9_live_proof_worker_cleanup_unproved")
        time.sleep(POLL_INTERVAL_SECONDS)


def _verify_stopped_worker_holds_global_execution_lock() -> bool:
    """Prove the stopped effect worker still excludes another executor."""

    try:
        unexpected = GlobalExecutionLock(
            GLOBAL_LOCK_PATH,
            expected_uid=0,
            expected_gid=0,
        )
    except ExecutionLockBusyError:
        return True
    except Exception as error:
        raise LiveProofError(
            "phase9_live_proof_global_lock_exclusion_unproved"
        ) from error
    else:
        unexpected.close()
        raise LiveProofError(
            "phase9_live_proof_global_lock_exclusion_unproved"
        )


def _verify_stopped_worker_has_no_children(pid: int) -> bool:
    """Require one stopped task and an empty direct child-process set."""

    if (
        sys.platform != "linux"
        or type(pid) is not int
        or pid <= 1
        or getattr(os, "O_NOFOLLOW", 0) == 0
    ):
        raise LiveProofError(
            "phase9_live_proof_worker_child_exclusion_unproved"
        )
    task_root = Path(f"/proc/{pid}/task")
    try:
        with os.scandir(task_root) as entries:
            task_ids = tuple(
                sorted(
                    entry.name
                    for entry in entries
                    if entry.is_dir(follow_symlinks=False)
                )
            )
    except OSError as error:
        raise LiveProofError(
            "phase9_live_proof_worker_child_exclusion_unproved"
        ) from error
    if task_ids != (str(pid),):
        raise LiveProofError(
            "phase9_live_proof_worker_child_exclusion_unproved"
        )
    path = task_root / str(pid) / "children"
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        raw = os.read(descriptor, 4097)
    except OSError as error:
        raise LiveProofError(
            "phase9_live_proof_worker_child_exclusion_unproved"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > 4096 or raw.strip():
        raise LiveProofError(
            "phase9_live_proof_worker_child_exclusion_unproved"
        )
    return True


def _execution_directories() -> frozenset[str]:
    names: set[str] = set()
    try:
        with os.scandir(EXECUTIONS_ROOT) as entries:
            for entry in entries:
                if (
                    _EXECUTION_DIRECTORY_RE.fullmatch(entry.name) is not None
                    and entry.is_dir(follow_symlinks=False)
                ):
                    names.add(entry.name)
    except OSError as error:
        raise LiveProofError("phase9_live_proof_execution_root_unavailable") from error
    return frozenset(names)


def _journal_has_boundary(path: Path, step_id: str) -> bool:
    try:
        raw = _read_regular_no_follow(path, maximum=16 * 1024 * 1024)
    except LiveProofError:
        return False
    if raw and not raw.endswith(b"\n"):
        return False
    for line in raw.splitlines():
        try:
            record = json.loads(line.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError):
            return False
        if type(record) is not dict or _canonical(record) != line:
            return False
        if (
            record.get("step_id") == step_id
            and record.get("event") == JOURNAL_APPLIED_EVENT
        ):
            return True
    return False


def _boundary_journal_evidence(
    *,
    context: ProofContext,
    boundary_kind: str,
    execution_id: str,
    journal_name: str,
    step_id: str,
) -> BoundaryJournalEvidence:
    """Read one stopped worker's complete, hash-chained boundary state."""

    _require_hash(execution_id)
    if boundary_kind == "install":
        if journal_name != "journal.jsonl" or step_id != INSTALL_KILL_STEP:
            raise LiveProofError("phase9_live_proof_boundary_identity_invalid")
        expected_plan = verified_package_evidence(
            context.verified_package_capability
        ).controller_model_sha256
        attempt_pattern = _INSTALL_ATTEMPT_RE
    elif boundary_kind == "empty_rollback":
        if journal_name != "rollback.jsonl" or step_id != ROLLBACK_KILL_STEP:
            raise LiveProofError("phase9_live_proof_boundary_identity_invalid")
        expected_plan = rollback_capability_evidence(
            _verified_rollback_capability(context)
        ).rollback_plan_sha256
        attempt_pattern = _ROLLBACK_ATTEMPT_RE
    else:
        raise LiveProofError("phase9_live_proof_boundary_identity_invalid")
    path = EXECUTIONS_ROOT / execution_id / journal_name
    raw = _read_regular_no_follow(path, maximum=16 * 1024 * 1024)
    lines = raw.splitlines()
    if not lines or not raw.endswith(b"\n"):
        raise LiveProofError("phase9_live_proof_boundary_journal_invalid")
    first = _parse_canonical_object(
        lines[0],
        "phase9_live_proof_boundary_journal_invalid",
    )
    attempt_id = first.get("attempt_id")
    if (
        first.get("plan_sha256") != expected_plan
        or type(attempt_id) is not str
        or attempt_pattern.fullmatch(attempt_id) is None
    ):
        raise LiveProofError("phase9_live_proof_boundary_journal_invalid")
    try:
        if boundary_kind == "install":
            records = parse_durable_journal_bytes(
                raw,
                expected_plan_sha256=expected_plan,
                expected_attempt_id=attempt_id,
            )
        else:
            records = parse_rollback_journal_bytes(
                raw,
                plan_sha256=expected_plan,
                attempt_id=attempt_id,
            )
    except (DurableJournalError, DurableRollbackJournalError) as error:
        raise LiveProofError(
            "phase9_live_proof_boundary_journal_invalid"
        ) from error
    matches = tuple(
        record
        for record in records
        if record.step_id == step_id
        and record.event == JOURNAL_APPLIED_EVENT
    )
    if len(matches) != 1 or not records:
        raise LiveProofError("phase9_live_proof_boundary_journal_invalid")
    boundary = matches[0]
    head = records[-1]
    return BoundaryJournalEvidence(
        boundary_kind=boundary_kind,
        execution_id=execution_id,
        journal_name=journal_name,
        plan_sha256=expected_plan,
        attempt_id=attempt_id,
        boundary_step_id=step_id,
        boundary_record_sequence=boundary.sequence,
        boundary_record_sha256=boundary.record_sha256,
        journal_sequence_at_arm=len(records),
        journal_head_sha256_at_arm=head.record_sha256,
        journal_record_sha256s=tuple(
            record.record_sha256 for record in records
        ),
    )


def _install_journal_compensation_complete(path: Path) -> bool:
    """Read one strict install journal terminal state; malformed bytes refuse."""

    raw = _read_optional_root_regular_no_follow(
        path,
        maximum=16 * 1024 * 1024,
        allow_empty=True,
    )
    if raw is None:
        return False
    if raw == b"":
        return False
    if not raw.endswith(b"\n"):
        raise LiveProofError("phase9_live_proof_install_journal_invalid")
    records: list[dict[str, object]] = []
    for line in raw.splitlines():
        try:
            record = json.loads(line.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise LiveProofError(
                "phase9_live_proof_install_journal_invalid"
            ) from error
        if type(record) is not dict or _canonical(record) != line:
            raise LiveProofError("phase9_live_proof_install_journal_invalid")
        records.append(record)
    terminal = records[-1]
    return (
        terminal.get("step_id") == "I00_ATTEMPT"
        and terminal.get("event") == "compensation_complete"
    )


def _build_process_death_arm_receipt(
    *,
    context: ProofContext,
    evidence: BoundaryJournalEvidence,
    worker_pid: int,
    recovery_reservation_claim_sha256: str,
    install_authority_claim_sha256: str,
) -> Mapping[str, object]:
    """Bind a stopped owned worker to its exact durable journal boundary."""

    if type(worker_pid) is not int or worker_pid <= 1:
        raise LiveProofError("phase9_live_proof_worker_identity_invalid")
    _require_hash(recovery_reservation_claim_sha256)
    _require_hash(install_authority_claim_sha256)
    worker_identity_sha256 = _document_sha(
        {
            "schema_version": "governed-memory-phase9-owned-worker-identity-v1",
            "runner_pid": os.getpid(),
            "worker_pid": worker_pid,
        }
    )
    receipt: dict[str, object] = {
        "schema_version": (
            process_death_arm_receipt.PROCESS_DEATH_ARM_RECEIPT_SCHEMA
        ),
        "result": (
            process_death_arm_receipt.PROCESS_DEATH_ARM_RECEIPT_RESULT
        ),
        "boundary_kind": evidence.boundary_kind,
        "candidate_git_commit": context.inputs.candidate_git_commit,
        "candidate_git_tree": context.inputs.candidate_git_tree,
        "package_manifest_sha256": context.inputs.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            context.inputs.controller_runtime_receipt_sha256
        ),
        "recovery_capsule_sha256": _sha(context.recovery_capsule.raw),
        "recovery_reservation_claim_sha256": (
            recovery_reservation_claim_sha256
        ),
        "install_authority_claim_sha256": install_authority_claim_sha256,
        "execution_id": evidence.execution_id,
        "journal_name": evidence.journal_name,
        "journal_plan_sha256": evidence.plan_sha256,
        "journal_attempt_id": evidence.attempt_id,
        "boundary_step_id": evidence.boundary_step_id,
        "boundary_event": JOURNAL_APPLIED_EVENT,
        "boundary_record_sequence": evidence.boundary_record_sequence,
        "boundary_record_sha256": evidence.boundary_record_sha256,
        "journal_sequence_at_arm": evidence.journal_sequence_at_arm,
        "journal_head_sha256_at_arm": evidence.journal_head_sha256_at_arm,
        "worker_identity_sha256": worker_identity_sha256,
        "worker_parent_death_sigkill_armed": True,
        "worker_cooperative_post_fsync_sigstop_observed": True,
        "worker_sigstop_observed": True,
        "worker_child_process_set_empty_at_arm": True,
        "global_execution_lock_exclusion_observed": True,
        "sigkill_required_before_resume": True,
    }
    receipt["receipt_sha256"] = _document_sha(receipt)
    try:
        return process_death_arm_receipt.verify_process_death_arm_receipt(
            receipt,
            expected_execution_id=evidence.execution_id,
            expected_boundary_kind=evidence.boundary_kind,
        )
    except process_death_arm_receipt.ProcessDeathArmReceiptError as error:
        raise LiveProofError(
            "phase9_live_proof_process_death_arm_invalid"
        ) from error


def _kill_after_new_durable_boundary(
    *,
    context: ProofContext,
    boundary_kind: str,
    pid: int,
    read_fd: int,
    expected_execution_id: str,
    allowed_existing_directories: frozenset[str],
    journal_name: str,
    step_id: str,
    recovery_reservation_claim_sha256: str,
    install_authority_claim_sha256: str,
) -> Mapping[str, object]:
    _require_hash(expected_execution_id)
    if (
        type(allowed_existing_directories) is not frozenset
        or any(
            _EXECUTION_DIRECTORY_RE.fullmatch(item) is None
            for item in allowed_existing_directories
        )
        or expected_execution_id in allowed_existing_directories
    ):
        raise LiveProofError("phase9_live_proof_boundary_identity_invalid")
    deadline = time.monotonic() + BOUNDARY_TIMEOUT_SECONDS
    child_owned = True
    descriptor_owned = True
    try:
        while time.monotonic() < deadline:
            waited, status = os.waitpid(
                pid,
                os.WNOHANG | os.WUNTRACED,
            )
            if waited == pid and not os.WIFSTOPPED(status):
                child_owned = False
                raw = _read_pipe(read_fd)
                descriptor_owned = False
                try:
                    detail = _parse_canonical_object(
                        raw, "phase9_live_proof_worker_result_invalid"
                    )
                    code = detail.get("error_code")
                except LiveProofError:
                    code = None
                raise LiveProofError(
                    code
                    if type(code) is str
                    else "phase9_live_proof_worker_exited_before_boundary"
                )
            if waited not in (0, pid):
                raise LiveProofError("phase9_live_proof_worker_wait_failed")
            observed_directories = _execution_directories()
            unexpected = observed_directories - (
                allowed_existing_directories | {expected_execution_id}
            )
            missing_prior = allowed_existing_directories - observed_directories
            if unexpected or missing_prior:
                raise LiveProofError(
                    "phase9_live_proof_parallel_execution_detected"
                )
            if waited == pid:
                if (
                    os.WSTOPSIG(status) != signal.SIGSTOP
                    or expected_execution_id not in observed_directories
                ):
                    raise LiveProofError(
                        "phase9_live_proof_worker_stopped_state_invalid"
                    )
                evidence = _boundary_journal_evidence(
                    context=context,
                    boundary_kind=boundary_kind,
                    execution_id=expected_execution_id,
                    journal_name=journal_name,
                    step_id=step_id,
                )
                if (
                    evidence.boundary_record_sequence
                    != evidence.journal_sequence_at_arm
                    or evidence.boundary_record_sha256
                    != evidence.journal_head_sha256_at_arm
                ):
                    raise LiveProofError(
                        "phase9_live_proof_cooperative_stop_boundary_invalid"
                    )
                if not _verify_stopped_worker_has_no_children(pid):
                    raise LiveProofError(
                        "phase9_live_proof_worker_child_exclusion_unproved"
                    )
                if not _verify_stopped_worker_holds_global_execution_lock():
                    raise LiveProofError(
                        "phase9_live_proof_global_lock_exclusion_unproved"
                    )
                arm = _build_process_death_arm_receipt(
                    context=context,
                    evidence=evidence,
                    worker_pid=pid,
                    recovery_reservation_claim_sha256=(
                        recovery_reservation_claim_sha256
                    ),
                    install_authority_claim_sha256=(
                        install_authority_claim_sha256
                    ),
                )
                try:
                    durable_arm = (
                        process_death_arm_receipt.persist_process_death_arm_receipt(
                            arm
                        )
                    )
                except (
                    process_death_arm_receipt.ProcessDeathArmReceiptError
                ) as error:
                    raise LiveProofError(
                        "phase9_live_proof_process_death_arm_persistence_failed"
                    ) from error
                status = _terminate_and_reap_worker(pid, read_fd)
                child_owned = False
                descriptor_owned = False
                if (
                    status is None
                    or not os.WIFSIGNALED(status)
                    or os.WTERMSIG(status) != signal.SIGKILL
                ):
                    raise LiveProofError(
                        "phase9_live_proof_process_death_unproved"
                    )
                return MappingProxyType(dict(durable_arm))
            time.sleep(POLL_INTERVAL_SECONDS)
        raise LiveProofError("phase9_live_proof_boundary_timeout")
    except BaseException as original_error:
        if child_owned:
            try:
                _terminate_and_reap_worker(
                    pid, read_fd if descriptor_owned else None
                )
                child_owned = False
                descriptor_owned = False
            except BaseException as cleanup_error:
                raise LiveProofError(
                    "phase9_live_proof_worker_cleanup_unproved"
                ) from cleanup_error
        elif descriptor_owned:
            try:
                os.close(read_fd)
            except OSError:
                pass
        raise original_error


def verify_live_proof_receipt(
    receipt: Mapping[str, object],
) -> dict[str, object]:
    """Strictly verify the content-free live proof receipt shape and digest."""

    if type(receipt) is not dict or set(receipt) != _LIVE_PROOF_RECEIPT_KEYS:
        raise LiveProofError("phase9_live_proof_receipt_invalid")
    if (
        receipt.get("schema_version") != LIVE_PROOF_RECEIPT_SCHEMA
        or receipt.get("result")
        != "disposable_install_crash_resume_and_empty_rollback_proved"
        or receipt.get("observation_scope")
        != "one_bounded_disposable_linux_execution_terminal_snapshot"
        or receipt.get("cold_restart_scope")
        != "controller_worker_process_only"
        or type(receipt.get("exact_rollback_resources_absent_count")) is not int
        or receipt.get("exact_rollback_resources_absent_count") != 15
        or type(receipt.get("source_postgres_read_count")) is not int
        or receipt.get("source_postgres_read_count") != 0
        or type(receipt.get("source_postgres_write_count")) is not int
        or receipt.get("source_postgres_write_count") != 0
        or type(receipt.get("provider_calls")) is not int
        or receipt.get("provider_calls") != 0
        or receipt.get("production_data_read") is not False
        or receipt.get("application_services_installed") is not False
        or receipt.get("activation_performed") is not False
        or receipt.get("host_reboot_proven") is not False
        or receipt.get(
            "persistent_store_restart_supervision_and_boot_recovery_proven"
        )
        is not False
        or receipt.get("issuer_or_host_death_durable_cleanup_proven")
        is not False
        or receipt.get("terminal_absence_is_continuous_guarantee")
        is not False
        or receipt.get("ephemeral_private_signer_retained_at_execution_start")
        is not False
    ):
        raise LiveProofError("phase9_live_proof_receipt_invalid")
    required_true = (
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
        "start_authority_pair_claimed_atomically",
        "recovery_capsule_retained_at_terminal_observation",
    )
    required_false = (
        "stores_installed_at_terminal_observation",
        "stores_supervisor_installed_at_terminal_observation",
    )
    if any(receipt.get(key) is not True for key in required_true) or any(
        receipt.get(key) is not False for key in required_false
    ):
        raise LiveProofError("phase9_live_proof_receipt_invalid")
    for key in (
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
    ):
        if type(receipt.get(key)) is not str or _HASH_RE.fullmatch(
            str(receipt[key])
        ) is None:
            raise LiveProofError("phase9_live_proof_receipt_invalid")
    for key in ("candidate_git_commit", "candidate_git_tree"):
        if type(receipt.get(key)) is not str or _COMMIT_RE.fullmatch(
            str(receipt[key])
        ) is None:
            raise LiveProofError("phase9_live_proof_receipt_invalid")
    expected_digest = _document_sha(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    if receipt.get("receipt_sha256") != expected_digest:
        raise LiveProofError("phase9_live_proof_receipt_invalid")
    return dict(receipt)


def verify_recovery_receipt(
    value: Mapping[str, object],
) -> dict[str, object]:
    receipt = dict(value)
    if (
        set(receipt) != _RECOVERY_RECEIPT_KEYS
        or receipt.get("schema_version") != RECOVERY_RECEIPT_SCHEMA
        or receipt.get("result")
        != "exact_reserved_install_recovered_and_rolled_back_empty"
        or receipt.get("exact_resources_absent") is not True
        or receipt.get("stores_installed") is not False
        or receipt.get("stores_supervisor_installed") is not False
        or receipt.get("install_initiated_by_recovery_mode") is not False
        or receipt.get("start_authority_pair_claimed_atomically") is not True
        or type(receipt.get("provider_calls")) is not int
        or receipt.get("provider_calls") != 0
        or receipt.get("production_data_read") is not False
        or receipt.get("activation_performed") is not False
    ):
        raise LiveProofError("phase9_live_proof_recovery_receipt_invalid")
    for key in (
        "package_manifest_sha256",
        "controller_runtime_receipt_sha256",
        "recovery_capsule_sha256",
        "recovery_reservation_claim_sha256",
        "install_authority_claim_sha256",
        "installation_execution_id",
        "installation_receipt_sha256",
        "empty_rollback_execution_id",
        "empty_rollback_receipt_sha256",
        "receipt_sha256",
    ):
        if type(receipt.get(key)) is not str or _HASH_RE.fullmatch(
            str(receipt[key])
        ) is None:
            raise LiveProofError(
                "phase9_live_proof_recovery_receipt_invalid"
            )
    for key in ("candidate_git_commit", "candidate_git_tree"):
        if type(receipt.get(key)) is not str or _COMMIT_RE.fullmatch(
            str(receipt[key])
        ) is None:
            raise LiveProofError(
                "phase9_live_proof_recovery_receipt_invalid"
            )
    if receipt["receipt_sha256"] != _document_sha(
        {key: item for key, item in receipt.items() if key != "receipt_sha256"}
    ):
        raise LiveProofError("phase9_live_proof_recovery_receipt_invalid")
    return receipt


def verify_pair_only_receipt(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Verify the safe, non-promotable pair-without-execution state."""

    receipt = dict(value)
    if (
        set(receipt) != _PAIR_ONLY_RECEIPT_KEYS
        or receipt.get("schema_version") != PAIR_ONLY_RECEIPT_SCHEMA
        or receipt.get("result")
        != "exact_start_pair_present_install_execution_absent"
        or receipt.get("start_authority_pair_claimed_atomically") is not True
        or receipt.get("install_execution_directory_present") is not False
        or receipt.get("installation_receipt_present") is not False
        or receipt.get("rollback_authority_claim_present") is not False
        or receipt.get("install_initiated_by_recovery_mode") is not False
        or type(receipt.get("provider_calls")) is not int
        or receipt.get("provider_calls") != 0
        or receipt.get("production_data_read") is not False
        or receipt.get("activation_performed") is not False
    ):
        raise LiveProofError("phase9_live_proof_pair_only_receipt_invalid")
    for key in (
        "package_manifest_sha256",
        "controller_runtime_receipt_sha256",
        "recovery_capsule_sha256",
        "recovery_reservation_claim_sha256",
        "install_authority_claim_sha256",
        "expected_installation_execution_id",
        "receipt_sha256",
    ):
        if type(receipt.get(key)) is not str or _HASH_RE.fullmatch(
            str(receipt[key])
        ) is None:
            raise LiveProofError(
                "phase9_live_proof_pair_only_receipt_invalid"
            )
    for key in ("candidate_git_commit", "candidate_git_tree"):
        if type(receipt.get(key)) is not str or _COMMIT_RE.fullmatch(
            str(receipt[key])
        ) is None:
            raise LiveProofError(
                "phase9_live_proof_pair_only_receipt_invalid"
            )
    if receipt["receipt_sha256"] != _document_sha(
        {key: item for key, item in receipt.items() if key != "receipt_sha256"}
    ):
        raise LiveProofError("phase9_live_proof_pair_only_receipt_invalid")
    return receipt


def _verify_death_arm_against_completed_journal(
    *,
    context: ProofContext,
    arm_receipt: Mapping[str, object],
    completed_receipt: Mapping[str, object],
    boundary_kind: str,
) -> Mapping[str, object]:
    """Bind one pre-kill arm to the later exact completed journal head."""

    completed = (
        verify_install_receipt(completed_receipt)
        if boundary_kind == "install"
        else verify_empty_rollback_receipt(completed_receipt)
    )
    execution_id = str(completed["execution_id"])
    try:
        arm = process_death_arm_receipt.verify_process_death_arm_receipt(
            arm_receipt,
            expected_execution_id=execution_id,
            expected_boundary_kind=boundary_kind,
        )
    except process_death_arm_receipt.ProcessDeathArmReceiptError as error:
        raise LiveProofError(
            "phase9_live_proof_process_death_arm_invalid"
        ) from error
    expected_bindings = {
        "candidate_git_commit": context.inputs.candidate_git_commit,
        "candidate_git_tree": context.inputs.candidate_git_tree,
        "package_manifest_sha256": context.inputs.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            context.inputs.controller_runtime_receipt_sha256
        ),
        "recovery_capsule_sha256": _sha(context.recovery_capsule.raw),
        "execution_id": execution_id,
        "journal_plan_sha256": completed["plan_sha256"],
        "journal_attempt_id": completed["attempt_id"],
    }
    if any(arm.get(key) != value for key, value in expected_bindings.items()):
        raise LiveProofError("phase9_live_proof_process_death_arm_mismatch")
    journal_name = str(arm["journal_name"])
    step_id = str(arm["boundary_step_id"])
    evidence = _boundary_journal_evidence(
        context=context,
        boundary_kind=boundary_kind,
        execution_id=execution_id,
        journal_name=journal_name,
        step_id=step_id,
    )
    arm_sequence = arm.get("journal_sequence_at_arm")
    boundary_sequence = arm.get("boundary_record_sequence")
    if (
        type(arm_sequence) is not int
        or type(boundary_sequence) is not int
        or arm_sequence > len(evidence.journal_record_sha256s)
        or boundary_sequence > len(evidence.journal_record_sha256s)
        or evidence.journal_record_sha256s[arm_sequence - 1]
        != arm.get("journal_head_sha256_at_arm")
        or evidence.journal_record_sha256s[boundary_sequence - 1]
        != arm.get("boundary_record_sha256")
        or evidence.boundary_record_sequence != boundary_sequence
        or evidence.boundary_record_sha256
        != arm.get("boundary_record_sha256")
        or evidence.plan_sha256 != completed["plan_sha256"]
        or evidence.attempt_id != completed["attempt_id"]
        or len(evidence.journal_record_sha256s)
        != completed["journal_sequence"]
        or evidence.journal_record_sha256s[-1]
        != completed["journal_head_sha256"]
    ):
        raise LiveProofError("phase9_live_proof_process_death_arm_mismatch")
    return MappingProxyType(dict(arm))


def _proof_receipt(
    *,
    context: ProofContext,
    install_receipt: Mapping[str, object],
    rollback_receipt: Mapping[str, object],
    absence: Mapping[str, object],
    host_clock_synchronization_preflight_passed: bool,
    recovery_reservation_claim_sha256: str,
    install_authority_claim_sha256: str,
    install_death_arm: Mapping[str, object],
    rollback_death_arm: Mapping[str, object],
) -> dict[str, object]:
    install = verify_install_receipt(install_receipt)
    rollback = verify_empty_rollback_receipt(rollback_receipt)
    verified_install_arm = _verify_death_arm_against_completed_journal(
        context=context,
        arm_receipt=install_death_arm,
        completed_receipt=install,
        boundary_kind="install",
    )
    verified_rollback_arm = _verify_death_arm_against_completed_journal(
        context=context,
        arm_receipt=rollback_death_arm,
        completed_receipt=rollback,
        boundary_kind="empty_rollback",
    )
    if any(
        arm.get("recovery_reservation_claim_sha256")
        != recovery_reservation_claim_sha256
        or arm.get("install_authority_claim_sha256")
        != install_authority_claim_sha256
        for arm in (verified_install_arm, verified_rollback_arm)
    ):
        raise LiveProofError("phase9_live_proof_process_death_arm_mismatch")
    schema_raw = context.artifacts.get(LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE)
    if type(schema_raw) is not bytes:
        raise LiveProofError("phase9_live_proof_receipt_schema_missing")
    if (
        install["source_postgres_read_count"] != 0
        or install["source_postgres_write_count"] != 0
        or install["provider_calls"] != 0
        or install["production_data_read"] is not False
        or install["application_services_installed"] is not False
        or install["activation_performed"] is not False
        or rollback["source_postgres_read_count"] != 0
        or rollback["source_postgres_write_count"] != 0
        or rollback["provider_calls"] != 0
        or rollback["production_data_read"] is not False
        or rollback["application_services_installed"] is not False
        or rollback["activation_performed"] is not False
        or rollback["exact_rollback_resources_absent_count"] != 15
        or host_clock_synchronization_preflight_passed is not True
        or _HASH_RE.fullmatch(recovery_reservation_claim_sha256) is None
        or _HASH_RE.fullmatch(install_authority_claim_sha256) is None
    ):
        raise LiveProofError("phase9_live_proof_receipt_effect_boundary_invalid")
    terminal_capsule = _load_verified_recovery_capsule(
        context.inputs,
        context.artifacts,
        require_current=False,
    )
    if terminal_capsule.raw != context.recovery_capsule.raw:
        raise LiveProofError("phase9_live_proof_recovery_capsule_changed")
    receipt: dict[str, object] = {
        "schema_version": LIVE_PROOF_RECEIPT_SCHEMA,
        "result": "disposable_install_crash_resume_and_empty_rollback_proved",
        "observation_scope": (
            "one_bounded_disposable_linux_execution_terminal_snapshot"
        ),
        "candidate_git_commit": context.inputs.candidate_git_commit,
        "candidate_git_tree": context.inputs.candidate_git_tree,
        "package_manifest_sha256": context.inputs.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            context.inputs.controller_runtime_receipt_sha256
        ),
        "live_proof_receipt_schema_sha256": _sha(schema_raw),
        "recovery_capsule_sha256": _sha(context.recovery_capsule.raw),
        "recovery_reservation_claim_sha256": (
            recovery_reservation_claim_sha256
        ),
        "install_authority_claim_sha256": install_authority_claim_sha256,
        "start_authority_pair_claimed_atomically": True,
        "recovery_capsule_published_before_first_install_effect": True,
        "recovery_reservation_claimed_before_first_install_effect": True,
        "ephemeral_private_signer_retained_at_execution_start": False,
        "recovery_capsule_retained_at_terminal_observation": True,
        "authorization_text_sha256": AUTHORIZED_TEXT_SHA256,
        "installation_execution_id": install["execution_id"],
        "installation_receipt_sha256": install["receipt_sha256"],
        "image_identity_set_sha256": install["image_identity_set_sha256"],
        "empty_rollback_execution_id": rollback["execution_id"],
        "empty_rollback_receipt_sha256": rollback["receipt_sha256"],
        "retained_audit_set_sha256": rollback[
            "retained_audit_set_sha256"
        ],
        "install_process_death_boundary_sha256": verified_install_arm[
            "boundary_record_sha256"
        ],
        "install_process_death_arm_receipt_sha256": verified_install_arm[
            "receipt_sha256"
        ],
        "rollback_process_death_boundary_sha256": verified_rollback_arm[
            "boundary_record_sha256"
        ],
        "rollback_process_death_arm_receipt_sha256": verified_rollback_arm[
            "receipt_sha256"
        ],
        "install_process_death_observed": True,
        "install_exact_resume_completed": True,
        "cold_controller_process_restart_terminal_postflight_verified": True,
        "cold_restart_scope": "controller_worker_process_only",
        "host_reboot_proven": False,
        "persistent_store_restart_supervision_and_boot_recovery_proven": False,
        "rollback_process_death_observed": True,
        "rollback_exact_resume_completed": True,
        "public_install_entrypoint_used": True,
        "public_empty_rollback_entrypoint_used": True,
        "fresh_r06_semantic_empty_recheck_required": True,
        "completed_public_rollback_replayed": absence[
            "completed_public_rollback_replayed"
        ],
        "exact_rollback_resources_absent_count": rollback[
            "exact_rollback_resources_absent_count"
        ],
        "exact_resources_absent_at_terminal_observation": absence[
            "exact_resources_absent"
        ],
        "terminal_absence_is_continuous_guarantee": False,
        "stores_installed_at_terminal_observation": absence[
            "stores_installed"
        ],
        "stores_supervisor_installed_at_terminal_observation": absence[
            "stores_supervisor_installed"
        ],
        "controller_runtime_capability_reverified": absence[
            "controller_runtime_capability_reverified"
        ],
        "host_clock_synchronization_preflight_passed": True,
        "source_postgres_read_count": rollback[
            "source_postgres_read_count"
        ],
        "source_postgres_write_count": rollback[
            "source_postgres_write_count"
        ],
        "provider_calls": rollback["provider_calls"],
        "production_data_read": rollback["production_data_read"],
        "application_services_installed": rollback[
            "application_services_installed"
        ],
        "activation_performed": rollback["activation_performed"],
        "issuer_or_host_death_durable_cleanup_proven": False,
    }
    receipt["receipt_sha256"] = _document_sha(receipt)
    return verify_live_proof_receipt(receipt)


def _reconstruct_promotable_live_receipt_from_retained_evidence(
    *,
    context: ProofContext,
    install_receipt: Mapping[str, object],
    rollback_receipt: Mapping[str, object],
    absence: Mapping[str, object],
) -> Mapping[str, object] | None:
    """Rebuild success only from both durable pre-kill arm receipts."""

    install = verify_install_receipt(install_receipt)
    rollback = verify_empty_rollback_receipt(rollback_receipt)
    try:
        install_arm = (
            process_death_arm_receipt.read_process_death_arm_receipt_if_present(
                execution_id=str(install["execution_id"]),
                boundary_kind="install",
            )
        )
        rollback_arm = (
            process_death_arm_receipt.read_process_death_arm_receipt_if_present(
                execution_id=str(rollback["execution_id"]),
                boundary_kind="empty_rollback",
            )
        )
    except process_death_arm_receipt.ProcessDeathArmReceiptError as error:
        raise LiveProofError(
            "phase9_live_proof_process_death_arm_invalid"
        ) from error
    if install_arm is None or rollback_arm is None:
        # Zero or one arm cannot prove both commanded process deaths.  Exact
        # cleanup remains valid but deliberately non-promotable.
        return None
    rollback_documents = _build_rollback_documents(
        context=context,
        install_receipt=install,
    )
    selected = replace(context, rollback_documents=rollback_documents)
    reservation_identity, install_identity = _start_authority_pair_identities(
        selected
    )
    verified = _proof_receipt(
        context=selected,
        install_receipt=install,
        rollback_receipt=rollback,
        absence=absence,
        host_clock_synchronization_preflight_passed=True,
        recovery_reservation_claim_sha256=(
            reservation_identity.claim_sha256
        ),
        install_authority_claim_sha256=install_identity.claim_sha256,
        install_death_arm=install_arm,
        rollback_death_arm=rollback_arm,
    )
    try:
        durable = persist_verified_promotable_live_receipt(
            inputs=selected.inputs,
            artifacts=selected.artifacts,
            capsule_sha256=_sha(selected.recovery_capsule.raw),
            receipt=verified,
        )
    except DurableLiveProofReceiptError as error:
        raise LiveProofError(
            "phase9_live_proof_durable_receipt_persistence_failed"
        ) from error
    return MappingProxyType(dict(durable))


def _complete_exact_rollback_recovery(
    *,
    context: ProofContext,
    install_receipt: Mapping[str, object],
    rollback_context: ProofContext | None,
    start_from_reservation: bool,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Recover only the exact installed ledger through public rollback."""

    selected = rollback_context
    if selected is None:
        documents = _build_rollback_documents(
            context=context,
            install_receipt=install_receipt,
        )
        selected = replace(context, rollback_documents=documents)
    mode = (
        WorkerMode.ROLLBACK
        if start_from_reservation
        else WorkerMode.RESUME_ROLLBACK
    )
    pid, descriptor = _fork_worker(mode, selected)
    recovered = verify_empty_rollback_receipt(
        _wait_worker(pid, descriptor)
    )
    if recovered["exact_resources_absent"] is not True:
        raise LiveProofError("phase9_live_proof_exact_rollback_recovery_failed")
    pid, descriptor = _fork_worker(WorkerMode.VERIFY_ABSENCE, selected)
    absence = _wait_worker(pid, descriptor)
    if absence.get("exact_resources_absent") is not True:
        raise LiveProofError("phase9_live_proof_exact_rollback_recovery_failed")
    return MappingProxyType(dict(recovered)), MappingProxyType(dict(absence))


def _recover_verified_install_receipt(
    *,
    context: ProofContext,
    execution_id: str,
) -> Mapping[str, object]:
    """Recover only an exact install whose durable authority claim exists."""

    _exact_install_execution_evidence_present(context, execution_id)
    # The same public install composition performs any durable-receipt replay
    # or journal resume only after it acquires the global execution lock.
    pid, descriptor = _fork_worker(WorkerMode.RESUME_INSTALL, context)
    receipt = verify_install_receipt(_wait_worker(pid, descriptor))
    if receipt["execution_id"] != execution_id:
        raise LiveProofError("phase9_live_proof_install_recovery_mismatch")
    return MappingProxyType(dict(receipt))


def _exact_install_execution_evidence_present(
    context: ProofContext,
    execution_id: str,
) -> None:
    """Require a preexisting exact claim without creating any install claim."""

    if execution_id != _expected_install_execution_id(context):
        raise LiveProofError("phase9_live_proof_install_recovery_mismatch")
    material = _install_authority_material(
        context.verified_scope_capability
    )
    with GlobalExecutionLock(GLOBAL_LOCK_PATH, expected_uid=0) as lock:
        held = lock.held_capability()
        _verify_host_clock_synchronized()
        state = _authority_state(allow_create=False)
        _claim_or_verify_recovery_reservation(
            context,
            state=state,
            held_lock=held,
            allow_new_claim=False,
            require_current=False,
        )
        try:
            claim = state.claim_nonce(
                material["nonce"],
                operation=material["operation"],
                execution_sha256=material["execution_sha256"],
                authorization_sha256=material["authorization_sha256"],
                scope_sha256=material["scope_sha256"],
                trust_bundle_sha256=material["trust_bundle_sha256"],
                held_lock=held,
                allow_new_claim=False,
            )
        except AuthorityClaimNotAllowedError as error:
            raise LiveProofError(
                "phase9_live_proof_install_recovery_evidence_absent"
            ) from error
        except Exception as error:
            raise LiveProofError(
                "phase9_live_proof_install_recovery_evidence_invalid"
            ) from error
        if claim.result != "exact_execution_resumed":
            raise LiveProofError(
                "phase9_live_proof_install_recovery_evidence_invalid"
            )


def _exact_install_claim_present_locked(
    context: ProofContext,
    *,
    state: AuthorityState,
    held_lock: object,
) -> bool:
    material = _install_authority_material(
        context.verified_scope_capability
    )
    try:
        claim = state.claim_nonce(
            material["nonce"],
            operation=material["operation"],
            execution_sha256=material["execution_sha256"],
            authorization_sha256=material["authorization_sha256"],
            scope_sha256=material["scope_sha256"],
            trust_bundle_sha256=material["trust_bundle_sha256"],
            held_lock=held_lock,
            allow_new_claim=False,
        )
    except AuthorityClaimNotAllowedError:
        return False
    except Exception as error:
        raise LiveProofError(
            "phase9_live_proof_install_recovery_evidence_invalid"
        ) from error
    if claim.result != "exact_execution_resumed":
        raise LiveProofError(
            "phase9_live_proof_install_recovery_evidence_invalid"
        )
    return True


def _rollback_claim_present_locked(
    context: ProofContext,
    *,
    state: AuthorityState,
    held_lock: object,
    install_receipt: Mapping[str, object],
) -> bool:
    documents = _build_rollback_documents(
        context=context,
        install_receipt=install_receipt,
        state=state,
        held_lock=held_lock,
    )
    selected = replace(context, rollback_documents=documents)
    evidence = rollback_capability_evidence(
        _verified_rollback_capability(selected)
    )
    try:
        return state.inspect_nonce_claim(
            evidence.nonce,
            held_lock=held_lock,
        ).present
    except Exception as error:
        raise LiveProofError(
            "phase9_live_proof_rollback_state_invalid"
        ) from error


def _recovery_selection_worker(
    context: ProofContext,
    *,
    allow_start: bool,
) -> Mapping[str, object]:
    """Choose start/recovery from exact claims and receipt under one lock."""

    if type(allow_start) is not bool:
        raise LiveProofError("phase9_live_proof_recovery_policy_invalid")
    with GlobalExecutionLock(GLOBAL_LOCK_PATH, expected_uid=0) as lock:
        held = lock.held_capability()
        _verify_host_clock_synchronized()
        try:
            state = _authority_state(allow_create=allow_start)
        except LiveProofError:
            raise
        binding = recovery_reservation_binding(
            context.verified_recovery_delegation_capability
        )
        reservation_present = state.inspect_nonce_claim(
            binding.nonce,
            held_lock=held,
        ).present
        install_present = _exact_install_claim_present_locked(
            context,
            state=state,
            held_lock=held,
        )
        execution_id = _expected_install_execution_id(context)
        receipt_store = DurableReceiptStore.production()
        receipt_evidence = receipt_store.read_if_present(
            ReceiptArtifact.INSTALL,
            execution_id,
        )
        if not reservation_present:
            if install_present or receipt_evidence is not None:
                raise LiveProofError(
                    "phase9_live_proof_recovery_state_inconsistent"
                )
            raise LiveProofError("phase9_live_proof_start_authority_pair_absent")
        if not install_present:
            if receipt_evidence is not None:
                raise LiveProofError(
                    "phase9_live_proof_recovery_state_inconsistent"
                )
            raise LiveProofError(
                "phase9_live_proof_recovery_reserved_without_install_claim"
            )
        pair = _claim_or_verify_start_authority_pair(
            context,
            state=state,
            held_lock=held,
            allow_new_pair=False,
        )
        if receipt_evidence is None:
            execution_directories = _execution_directories()
            if execution_id not in execution_directories:
                if execution_directories:
                    raise LiveProofError(
                        "phase9_live_proof_parallel_execution_detected"
                    )
                if not allow_start:
                    rollback_present = state.inspect_nonce_claim(
                        str(context.recovery_capsule.rollback_nonce),
                        held_lock=held,
                    ).present
                    if rollback_present:
                        raise LiveProofError(
                            "phase9_live_proof_rollback_state_without_install_receipt"
                        )
                    return MappingProxyType(
                        {
                            "action": "pair_only",
                            "recovery_reservation_claim_sha256": pair[
                                "recovery_reservation_claim_sha256"
                            ],
                            "install_authority_claim_sha256": pair[
                                "install_authority_claim_sha256"
                            ],
                            "start_authority_pair_claimed_atomically": True,
                            "expected_installation_execution_id": execution_id,
                            "install_execution_directory_present": False,
                            "installation_receipt_present": False,
                            "rollback_authority_claim_present": False,
                        }
                    )
                return MappingProxyType(
                    {
                        "action": "start_preclaimed",
                        "recovery_reservation_claim_sha256": pair[
                            "recovery_reservation_claim_sha256"
                        ],
                        "install_authority_claim_sha256": pair[
                            "install_authority_claim_sha256"
                        ],
                        "start_authority_pair_claimed_atomically": True,
                    }
                )
            if execution_directories != frozenset({execution_id}):
                raise LiveProofError(
                    "phase9_live_proof_parallel_execution_detected"
                )
        rollback_present = False
        if receipt_evidence is not None:
            install = verify_install_receipt(
                receipt_evidence.canonical_receipt
            )
            rollback_present = _rollback_claim_present_locked(
                context,
                state=state,
                held_lock=held,
                install_receipt=install,
            )
        else:
            rollback_nonce = str(context.recovery_capsule.rollback_nonce)
            if state.inspect_nonce_claim(
                rollback_nonce,
                held_lock=held,
            ).present:
                raise LiveProofError(
                    "phase9_live_proof_rollback_state_without_install_receipt"
                )
            install_journal = EXECUTIONS_ROOT / execution_id / "journal.jsonl"
            if _install_journal_compensation_complete(install_journal):
                raise LiveProofError(
                    "phase9_live_proof_install_compensation_manual_recovery_required"
                )
            install = verify_install_receipt(
                _execute_install_locked(
                    context,
                    state=state,
                    held_lock=held,
                    resume_only=True,
                )
            )
            if install.get("execution_id") != execution_id:
                raise LiveProofError(
                    "phase9_live_proof_install_recovery_mismatch"
                )
        documents = _build_rollback_documents(
            context=context,
            install_receipt=install,
            state=state,
            held_lock=held,
        )
        selected = replace(context, rollback_documents=documents)
        rollback = verify_empty_rollback_receipt(
            _execute_rollback_locked(
                selected,
                state=state,
                held_lock=held,
                start_from_reservation=not rollback_present,
            )
        )
        if rollback.get("exact_resources_absent") is not True:
            raise LiveProofError(
                "phase9_live_proof_exact_rollback_recovery_failed"
            )
        replay = _execute_rollback_locked(
            selected,
            state=state,
            held_lock=held,
            start_from_reservation=False,
        )
        absence = _terminal_absence_from_replay(selected, replay)
        return MappingProxyType(
            {
                "action": "recovered",
                "install_receipt": dict(install),
                "rollback_receipt": dict(rollback),
                "absence": dict(absence),
            }
        )


def _run_recovery_selection_worker(
    context: ProofContext,
) -> Mapping[str, object]:
    return _recovery_selection_worker(context, allow_start=False)


def _run_start_or_recovery_selection_worker(
    context: ProofContext,
) -> Mapping[str, object]:
    return _recovery_selection_worker(context, allow_start=True)


def _recovery_receipt(
    *,
    context: ProofContext,
    install_receipt: Mapping[str, object],
    rollback_receipt: Mapping[str, object],
    absence: Mapping[str, object],
) -> Mapping[str, object]:
    install = verify_install_receipt(install_receipt)
    rollback = verify_empty_rollback_receipt(rollback_receipt)
    if (
        absence.get("exact_resources_absent") is not True
        or absence.get("stores_installed") is not False
        or absence.get("stores_supervisor_installed") is not False
    ):
        raise LiveProofError("phase9_live_proof_recovery_receipt_invalid")
    reservation_identity, install_identity = _start_authority_pair_identities(
        context
    )
    receipt: dict[str, object] = {
        "schema_version": RECOVERY_RECEIPT_SCHEMA,
        "result": "exact_reserved_install_recovered_and_rolled_back_empty",
        "candidate_git_commit": context.inputs.candidate_git_commit,
        "candidate_git_tree": context.inputs.candidate_git_tree,
        "package_manifest_sha256": context.inputs.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            context.inputs.controller_runtime_receipt_sha256
        ),
        "recovery_capsule_sha256": _sha(context.recovery_capsule.raw),
        "recovery_reservation_claim_sha256": (
            reservation_identity.claim_sha256
        ),
        "install_authority_claim_sha256": install_identity.claim_sha256,
        "start_authority_pair_claimed_atomically": True,
        "installation_execution_id": install["execution_id"],
        "installation_receipt_sha256": install["receipt_sha256"],
        "empty_rollback_execution_id": rollback["execution_id"],
        "empty_rollback_receipt_sha256": rollback["receipt_sha256"],
        "exact_resources_absent": True,
        "stores_installed": False,
        "stores_supervisor_installed": False,
        "install_initiated_by_recovery_mode": False,
        "provider_calls": 0,
        "production_data_read": False,
        "activation_performed": False,
    }
    receipt["receipt_sha256"] = _document_sha(receipt)
    return MappingProxyType(verify_recovery_receipt(receipt))


def _pair_only_receipt(
    *,
    context: ProofContext,
    selection: Mapping[str, object],
) -> Mapping[str, object]:
    """Attest pristine pair-only state without initiating an install."""

    if (
        selection.get("action") != "pair_only"
        or selection.get("start_authority_pair_claimed_atomically") is not True
        or selection.get("install_execution_directory_present") is not False
        or selection.get("installation_receipt_present") is not False
        or selection.get("rollback_authority_claim_present") is not False
    ):
        raise LiveProofError("phase9_live_proof_pair_only_selection_invalid")
    receipt: dict[str, object] = {
        "schema_version": PAIR_ONLY_RECEIPT_SCHEMA,
        "result": "exact_start_pair_present_install_execution_absent",
        "candidate_git_commit": context.inputs.candidate_git_commit,
        "candidate_git_tree": context.inputs.candidate_git_tree,
        "package_manifest_sha256": context.inputs.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            context.inputs.controller_runtime_receipt_sha256
        ),
        "recovery_capsule_sha256": _sha(context.recovery_capsule.raw),
        "recovery_reservation_claim_sha256": selection.get(
            "recovery_reservation_claim_sha256"
        ),
        "install_authority_claim_sha256": selection.get(
            "install_authority_claim_sha256"
        ),
        "start_authority_pair_claimed_atomically": True,
        "expected_installation_execution_id": selection.get(
            "expected_installation_execution_id"
        ),
        "install_execution_directory_present": False,
        "installation_receipt_present": False,
        "rollback_authority_claim_present": False,
        "install_initiated_by_recovery_mode": False,
        "provider_calls": 0,
        "production_data_read": False,
        "activation_performed": False,
    }
    receipt["receipt_sha256"] = _document_sha(receipt)
    return MappingProxyType(verify_pair_only_receipt(receipt))


def recover_live_proof(inputs: ProofInputs) -> Mapping[str, object]:
    """Resume only an exact preclaimed install and its derived rollback."""

    _require_runner_process_authority(RunnerMode.RECOVER_ONLY)
    _verify_host_clock_synchronized()
    context, unused_capsule = _verified_install_context(
        inputs,
        require_current=False,
    )
    _prepare_fixed_substrate()
    pid, descriptor = _fork_worker(WorkerMode.RECOVER_ONLY, context)
    selection = _wait_worker(pid, descriptor)
    if selection.get("action") == "pair_only":
        return _pair_only_receipt(context=context, selection=selection)
    if selection.get("action") != "recovered":
        raise LiveProofError("phase9_live_proof_recovery_selection_invalid")
    install_raw = selection.get("install_receipt")
    rollback_raw = selection.get("rollback_receipt")
    absence = selection.get("absence")
    if (
        type(install_raw) is not dict
        or type(rollback_raw) is not dict
        or type(absence) is not dict
    ):
        raise LiveProofError("phase9_live_proof_recovery_selection_invalid")
    install = verify_install_receipt(install_raw)
    rollback = verify_empty_rollback_receipt(rollback_raw)
    try:
        existing_promotable = (
            read_verified_promotable_live_receipt_if_present(
                inputs=context.inputs,
                artifacts=context.artifacts,
                capsule_sha256=_sha(context.recovery_capsule.raw),
            )
        )
    except DurableLiveProofReceiptError as error:
        raise LiveProofError(
            "phase9_live_proof_durable_receipt_preflight_failed"
        ) from error
    if existing_promotable is None:
        reconstructed = (
            _reconstruct_promotable_live_receipt_from_retained_evidence(
                context=context,
                install_receipt=install,
                rollback_receipt=rollback,
                absence=absence,
            )
        )
        if reconstructed is not None:
            return reconstructed
    return _recovery_receipt(
        context=context,
        install_receipt=install,
        rollback_receipt=rollback,
        absence=absence,
    )


def start_or_recover_live_proof(inputs: ProofInputs) -> Mapping[str, object]:
    """Start once only from untouched state, otherwise select closed recovery."""

    _require_runner_process_authority(RunnerMode.START_OR_RECOVER)
    context, unused_capsule = _verified_install_context(
        inputs,
        require_current=False,
    )
    _prepare_fixed_substrate()
    pid, descriptor = _fork_worker(WorkerMode.START_OR_RECOVER, context)
    selection = _wait_worker(pid, descriptor)
    if selection.get("action") == "start_preclaimed":
        reservation_claim_sha256 = selection.get(
            "recovery_reservation_claim_sha256"
        )
        install_claim_sha256 = selection.get("install_authority_claim_sha256")
        if (
            type(reservation_claim_sha256) is not str
            or _HASH_RE.fullmatch(reservation_claim_sha256) is None
            or type(install_claim_sha256) is not str
            or _HASH_RE.fullmatch(install_claim_sha256) is None
            or selection.get("start_authority_pair_claimed_atomically") is not True
        ):
            raise LiveProofError(
                "phase9_live_proof_recovery_selection_invalid"
            )
        return run_live_proof(
            inputs,
            expected_reservation_claim_sha256=reservation_claim_sha256,
            expected_install_claim_sha256=install_claim_sha256,
        )
    if selection.get("action") != "recovered":
        raise LiveProofError("phase9_live_proof_recovery_selection_invalid")
    install_raw = selection.get("install_receipt")
    rollback_raw = selection.get("rollback_receipt")
    absence = selection.get("absence")
    if (
        type(install_raw) is not dict
        or type(rollback_raw) is not dict
        or type(absence) is not dict
    ):
        raise LiveProofError("phase9_live_proof_recovery_selection_invalid")
    install = verify_install_receipt(install_raw)
    rollback = verify_empty_rollback_receipt(rollback_raw)
    try:
        existing_promotable = read_verified_promotable_live_receipt_if_present(
            inputs=context.inputs,
            artifacts=context.artifacts,
            capsule_sha256=_sha(context.recovery_capsule.raw),
        )
    except DurableLiveProofReceiptError as error:
        raise LiveProofError(
            "phase9_live_proof_durable_receipt_preflight_failed"
        ) from error
    if existing_promotable is not None:
        return MappingProxyType(dict(existing_promotable))
    reconstructed = _reconstruct_promotable_live_receipt_from_retained_evidence(
        context=context,
        install_receipt=install,
        rollback_receipt=rollback,
        absence=absence,
    )
    if reconstructed is not None:
        return reconstructed
    return _recovery_receipt(
        context=context,
        install_receipt=install,
        rollback_receipt=rollback,
        absence=absence,
    )


def run_live_proof(
    inputs: ProofInputs,
    *,
    expected_reservation_claim_sha256: str | None = None,
    expected_install_claim_sha256: str | None = None,
) -> Mapping[str, object]:
    """Run the one exact install/kill/resume/rollback/kill/resume proof."""

    _require_runner_process_authority(RunnerMode.START_OR_RECOVER)
    host_clock_synchronization_preflight_passed = (
        _verify_host_clock_synchronized()
    )
    context, unused_capsule = _verified_install_context(
        inputs, require_current=False
    )
    _prepare_fixed_substrate()
    try:
        prior_promotable = preflight_anonymous_publication_capability(
            inputs=inputs,
            artifacts=context.artifacts,
            capsule_sha256=_sha(context.recovery_capsule.raw),
        )
    except DurableLiveProofReceiptError as error:
        raise LiveProofError(
            "phase9_live_proof_durable_receipt_preflight_failed"
        ) from error
    if prior_promotable is not None:
        raise LiveProofError(
            "phase9_live_proof_existing_promotable_receipt_requires_recovery"
        )
    pair = _verify_start_authority_pair_before_install(context)
    reservation_claim_sha256 = str(
        pair["recovery_reservation_claim_sha256"]
    )
    install_claim_sha256 = str(pair["install_authority_claim_sha256"])
    if (
        expected_reservation_claim_sha256 is not None
        and reservation_claim_sha256
        != expected_reservation_claim_sha256
    ):
        raise LiveProofError(
            "phase9_live_proof_recovery_reservation_mismatch"
        )
    if (
        expected_install_claim_sha256 is not None
        and install_claim_sha256 != expected_install_claim_sha256
    ):
        raise LiveProofError("phase9_live_proof_install_authority_mismatch")
    rollback_context: ProofContext | None = None
    verified_install: Mapping[str, object] | None = None
    install_worker_started = False
    # The install execution identifier is a deterministic function of the
    # pre-signed install capability.  Compute it before the first worker so a
    # death immediately after durable authority claim can still resume the
    # exact install rather than falling back to a name-selected cleanup.
    installation_execution_id = _expected_install_execution_id(context)
    try:
        before_install = _execution_directories()
        if installation_execution_id in before_install:
            raise LiveProofError(
                "phase9_live_proof_install_execution_preexisting"
            )
        pid, descriptor = _fork_worker(
            WorkerMode.RESUME_INSTALL,
            context,
            cooperative_boundary_kind=(
                process_death_arm_receipt.INSTALL_BOUNDARY_KIND
            ),
        )
        install_worker_started = True
        install_death_arm = _kill_after_new_durable_boundary(
            context=context,
            boundary_kind="install",
            pid=pid,
            read_fd=descriptor,
            expected_execution_id=installation_execution_id,
            allowed_existing_directories=before_install,
            journal_name="journal.jsonl",
            step_id=INSTALL_KILL_STEP,
            recovery_reservation_claim_sha256=(
                reservation_claim_sha256
            ),
            install_authority_claim_sha256=install_claim_sha256,
        )
        if (
            install_death_arm.get("execution_id")
            != installation_execution_id
        ):
            raise LiveProofError("phase9_live_proof_install_execution_mismatch")
        pid, descriptor = _fork_worker(WorkerMode.RESUME_INSTALL, context)
        install_receipt = _wait_worker(pid, descriptor)
        install = verify_install_receipt(install_receipt)
        verified_install = MappingProxyType(dict(install))
        if install["execution_id"] != installation_execution_id:
            raise LiveProofError("phase9_live_proof_install_resume_mismatch")

        rollback_documents = _build_rollback_documents(
            context=context,
            install_receipt=install,
        )
        rollback_context = replace(
            context, rollback_documents=rollback_documents
        )
        before_rollback = _execution_directories()
        rollback_execution_id = _expected_rollback_execution_id(
            rollback_context
        )
        if rollback_execution_id in before_rollback:
            raise LiveProofError(
                "phase9_live_proof_rollback_execution_preexisting"
            )
        pid, descriptor = _fork_worker(
            WorkerMode.ROLLBACK,
            rollback_context,
            cooperative_boundary_kind=(
                process_death_arm_receipt.EMPTY_ROLLBACK_BOUNDARY_KIND
            ),
        )
        rollback_death_arm = _kill_after_new_durable_boundary(
            context=rollback_context,
            boundary_kind="empty_rollback",
            pid=pid,
            read_fd=descriptor,
            expected_execution_id=rollback_execution_id,
            allowed_existing_directories=before_rollback,
            journal_name="rollback.jsonl",
            step_id=ROLLBACK_KILL_STEP,
            recovery_reservation_claim_sha256=(
                reservation_claim_sha256
            ),
            install_authority_claim_sha256=install_claim_sha256,
        )
        pid, descriptor = _fork_worker(
            WorkerMode.RESUME_ROLLBACK, rollback_context
        )
        rollback_receipt = _wait_worker(pid, descriptor)
        rollback = verify_empty_rollback_receipt(rollback_receipt)
        if rollback["execution_id"] != rollback_execution_id:
            raise LiveProofError("phase9_live_proof_rollback_resume_mismatch")
        pid, descriptor = _fork_worker(
            WorkerMode.VERIFY_ABSENCE, rollback_context
        )
        absence = _wait_worker(pid, descriptor)
        verified_live_receipt = _proof_receipt(
                context=rollback_context,
                install_receipt=install,
                rollback_receipt=rollback,
                absence=absence,
                host_clock_synchronization_preflight_passed=(
                    host_clock_synchronization_preflight_passed
                ),
                recovery_reservation_claim_sha256=(
                    reservation_claim_sha256
                ),
                install_authority_claim_sha256=install_claim_sha256,
                install_death_arm=install_death_arm,
                rollback_death_arm=rollback_death_arm,
            )
        _require_runner_process_authority(RunnerMode.START_OR_RECOVER)
        try:
            durable_live_receipt = persist_verified_promotable_live_receipt(
                inputs=inputs,
                artifacts=rollback_context.artifacts,
                capsule_sha256=_sha(rollback_context.recovery_capsule.raw),
                receipt=verified_live_receipt,
            )
        except DurableLiveProofReceiptError as error:
            raise LiveProofError(
                "phase9_live_proof_durable_receipt_persistence_failed"
            ) from error
        return MappingProxyType(dict(durable_live_receipt))
    except BaseException as original_error:
        if (
            isinstance(original_error, LiveProofError)
            and str(original_error)
            == "phase9_live_proof_worker_cleanup_unproved"
        ):
            # A worker or its descendants may still be effectful and all
            # siblings inherit the same flock open description. Never launch
            # an in-process recovery sibling until exact cleanup is proven.
            raise
        # Never introduce a generic cleanup surface.  As soon as a durable
        # install receipt has verified, even a failure while constructing its
        # first rollback capability must retry that exact construction and
        # finish only through the public ledger-bound rollback.
        if verified_install is None and install_worker_started:
            try:
                verified_install = _recover_verified_install_receipt(
                    context=context,
                    execution_id=installation_execution_id,
                )
            except BaseException as recovery_error:
                raise LiveProofError(
                    "phase9_live_proof_install_recovery_failed"
                ) from recovery_error
        if verified_install is not None:
            try:
                _complete_exact_rollback_recovery(
                    context=context,
                    install_receipt=verified_install,
                    rollback_context=rollback_context,
                    start_from_reservation=(rollback_context is None),
                )
            except BaseException as recovery_error:
                raise LiveProofError(
                    "phase9_live_proof_exact_rollback_recovery_failed"
                ) from recovery_error
        raise original_error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_disposable_installation_live_proof.py",
        allow_abbrev=False,
    )
    parser.add_argument(
        "mode",
        choices=(
            *(item.value for item in RunnerMode),
            *(item.value for item in WorkerMode),
        ),
    )
    parser.add_argument("--candidate-git-commit", required=True)
    parser.add_argument("--candidate-git-tree", required=True)
    parser.add_argument("--package-manifest-sha256", required=True)
    parser.add_argument("--controller-runtime-receipt-sha256", required=True)
    return parser


def parse_inputs(argv: Sequence[str]) -> tuple[str, ProofInputs]:
    values = _parser().parse_args(tuple(argv))
    inputs = ProofInputs(
        candidate_git_commit=values.candidate_git_commit,
        candidate_git_tree=values.candidate_git_tree,
        package_manifest_sha256=values.package_manifest_sha256,
        controller_runtime_receipt_sha256=(
            values.controller_runtime_receipt_sha256
        ),
    )
    return values.mode, inputs


def main(argv: Sequence[str] | None = None) -> int:
    mode, inputs = parse_inputs(sys.argv[1:] if argv is None else argv)
    # Worker modes are real, closed in-process modes used only through forked
    # ProofContext objects.  They cannot be invoked externally with loose
    # filesystem or authority inputs.
    if mode in {item.value for item in WorkerMode}:
        raise LiveProofError("phase9_live_proof_worker_context_required")
    try:
        selected_mode = RunnerMode(mode)
    except ValueError as error:
        raise LiveProofError("phase9_live_proof_mode_invalid") from error
    _require_runner_process_authority(selected_mode)
    try:
        guard = GlobalExecutionLock.from_inherited_descriptor(
            LIVE_PROOF_GUARD_PATH,
            LIVE_PROOF_GUARD_FD,
            expected_uid=0,
            expected_gid=0,
        )
    except Exception as error:
        raise LiveProofError("phase9_live_proof_guard_invalid") from error
    with guard:
        if mode == RunnerMode.PRECLAIM_STAGED_START.value:
            receipt = preclaim_staged_start_authority_pair(inputs)
        elif mode == RunnerMode.VERIFY_PUBLISHED_START_PAIR.value:
            receipt = verify_published_start_authority_pair(inputs)
        elif mode == RunnerMode.START_OR_RECOVER.value:
            receipt = start_or_recover_live_proof(inputs)
        elif mode == RunnerMode.RECOVER_ONLY.value:
            receipt = recover_live_proof(inputs)
        else:
            raise LiveProofError("phase9_live_proof_mode_invalid")
    sys.stdout.buffer.write(_canonical(dict(receipt)) + b"\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LiveProofError as error:
        sys.stderr.write(str(error) + "\n")
        raise SystemExit(1)

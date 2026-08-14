#!/usr/bin/env python3
from __future__ import annotations

"""Issue and supervise the one exact Phase 9 disposable Linux proof.

This repository-only authority is intentionally excluded from the sealed
controller release.  It creates one fixed durable root-owned public recovery
capsule with pre-signed install documents and a transitive empty-rollback
delegation, then discards the private key before execution.  The exact sealed
runner can recover from only that public capsule and its durable reservation.
No operator-selectable path, command, SQL, URL, resource, signer, nonce,
approval text, or proof identity is accepted.  The fixed manager pipe and
lease identity environment are mandatory authority inputs, not proof inputs.
"""

import sys
import ctypes
import fcntl
import os
import select
import signal
import stat

_ISOLATED_RUNTIME_AT_START = bool(sys.flags.isolated)
_DONT_WRITE_BYTECODE_AT_START = bool(sys.dont_write_bytecode)
_PINNED_PHASE9J_RUNTIME_RECEIPT_SHA256 = (
    "9dda4d93a1bcfecf4305736feffafb578e4629cd32594cec6b2d6d72cb90a4d3"
)
_PINNED_PHASE9J_INTERPRETER = (
    "/opt/governed-memory-controller/runtimes/"
    + _PINNED_PHASE9J_RUNTIME_RECEIPT_SHA256
    + "/bin/python"
)
MANAGER_CONTROL_FD = 198
MANAGER_PID_ENVIRONMENT_KEY = "GOVERNED_MEMORY_PHASE9_MANAGER_PID"
PR_SET_PDEATHSIG = 1
_MANAGER_CONTROL_LOST = False
_DIRECT_MANAGER_PID: int | None = None
_DIRECT_MANAGER_PIPE_ID: tuple[int, int] | None = None
_PREIMPORT_ISSUER_RELATIVE = (
    "tools/governed_memory_validation/"
    "issue_disposable_installation_live_proof.py"
)
_PREIMPORT_MANAGER_RELATIVE = (
    "tools/governed_memory_validation/"
    "execute_phase9_disposable_live_proof_controller.py"
)


def _mark_manager_control_lost(
    unused_signum: int,
    unused_frame: object,
) -> None:
    """Record parent loss without performing unsafe work in a signal handler."""

    del unused_signum, unused_frame
    global _MANAGER_CONTROL_LOST
    _MANAGER_CONTROL_LOST = True


def _preimport_manager_pid() -> int:
    raw = os.environ.get(MANAGER_PID_ENVIRONMENT_KEY)
    if (
        type(raw) is not str
        or not raw.isascii()
        or not raw.isdecimal()
        or raw.startswith("0")
    ):
        raise RuntimeError("phase9_proof_issuer_manager_control_required")
    value = int(raw)
    if value <= 1 or str(value) != raw or value != os.getppid():
        raise RuntimeError("phase9_proof_issuer_manager_control_required")
    return value


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


def _preimport_validate_manager_lineage(
    manager_pid: int,
    control_identity: tuple[int, int],
) -> None:
    issuer_path = os.path.realpath(__file__)
    repository_root = os.path.dirname(
        os.path.dirname(os.path.dirname(issuer_path))
    )
    manager_path = os.path.join(repository_root, _PREIMPORT_MANAGER_RELATIVE)
    if (
        not os.path.isabs(__file__)
        or issuer_path != __file__
        or os.path.relpath(issuer_path, repository_root)
        != _PREIMPORT_ISSUER_RELATIVE
        or tuple(sys.argv) != (issuer_path,)
        or os.path.realpath(manager_path) != manager_path
        or os.readlink(f"/proc/{manager_pid}/exe")
        != _PINNED_PHASE9J_INTERPRETER
        or _preimport_proc_cmdline(manager_pid)
        != (
            _PINNED_PHASE9J_INTERPRETER,
            "-I",
            "-B",
            manager_path,
        )
        or not _preimport_manager_holds_control_writer(
            manager_pid,
            control_identity,
        )
    ):
        raise RuntimeError("manager")


def _preimport_require_manager_control() -> int:
    """Bind direct execution to one live manager and fixed read-only pipe."""

    try:
        opened = os.fstat(MANAGER_CONTROL_FD)
        flags = fcntl.fcntl(MANAGER_CONTROL_FD, fcntl.F_GETFL)
        if (
            not stat.S_ISFIFO(opened.st_mode)
            or flags & os.O_ACCMODE != os.O_RDONLY
        ):
            raise OSError("manager control descriptor")
        os.set_blocking(MANAGER_CONTROL_FD, False)
        ready, unused_write, unused_exception = select.select(
            [MANAGER_CONTROL_FD], [], [], 0
        )
        if ready or unused_write or unused_exception:
            raise OSError("manager control descriptor closed")
        manager_pid = _preimport_manager_pid()
        _preimport_validate_manager_lineage(
            manager_pid,
            (opened.st_dev, opened.st_ino),
        )
        signal.signal(signal.SIGTERM, _mark_manager_control_lost)
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
        if prctl(
            PR_SET_PDEATHSIG,
            int(signal.SIGCONT),
            0,
            0,
            0,
        ) != 0:
            raise OSError("manager parent-death signal")
        if os.getppid() != manager_pid or _MANAGER_CONTROL_LOST:
            raise OSError("manager parent changed")
        ready, unused_write, unused_exception = select.select(
            [MANAGER_CONTROL_FD], [], [], 0
        )
        if ready or unused_write or unused_exception:
            raise OSError("manager control descriptor closed")
        global _DIRECT_MANAGER_PIPE_ID
        _DIRECT_MANAGER_PIPE_ID = (opened.st_dev, opened.st_ino)
        return manager_pid
    except (OSError, RuntimeError, ValueError, AttributeError, TypeError):
        sys.stderr.write("phase9_proof_issuer_manager_control_required\n")
        raise SystemExit(1) from None


if __name__ == "__main__" and not (
    _ISOLATED_RUNTIME_AT_START and _DONT_WRITE_BYTECODE_AT_START
):
    sys.stderr.write("phase9_proof_issuer_runtime_isolation_required\n")
    raise SystemExit(1)
if __name__ == "__main__" and (
    sys.platform != "linux" or sys.executable != _PINNED_PHASE9J_INTERPRETER
):
    sys.stderr.write("phase9_proof_issuer_pinned_runtime_required\n")
    raise SystemExit(1)
if __name__ == "__main__":
    _DIRECT_MANAGER_PID = _preimport_require_manager_control()

import base64
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from types import MappingProxyType
from typing import Final, Mapping, Sequence

# Isolated direct-file execution intentionally has no working-directory import
# authority.  Admit only this issuer's own resolved repository root; the live
# procedure separately binds that clean tree to the supplied commit/tree, while
# the release runner itself imports exclusively from its sealed release root.
_ISSUER_PATH = Path(__file__).resolve(strict=True)
_ISSUER_REPOSITORY_ROOT = _ISSUER_PATH.parents[2]
if (
    _ISSUER_PATH.relative_to(_ISSUER_REPOSITORY_ROOT).as_posix()
    != "tools/governed_memory_validation/issue_disposable_installation_live_proof.py"
):
    raise SystemExit("phase9_proof_issuer_invocation_invalid")
sys.path.insert(0, str(_ISSUER_REPOSITORY_ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools.governed_memory_install import authority, authority_state as authority_state_module
from tools.governed_memory_install.authority_state import (
    STATE_APPLICATION_ID,
    STATE_SCHEMA_VERSION,
)
from tools.governed_memory_install.execution_lock import (
    ExecutionLockError,
    GlobalExecutionLock,
)
from tools.governed_memory_validation import (
    bootstrap_phase9_disposable_store_substrate as substrate_bootstrap,
)
from tools.governed_memory_validation import (
    durable_live_proof_receipt,
)
from tools.governed_memory_validation import (
    staged_prefix_disposition,
)
from tools.governed_memory_validation import phase9_permitted_candidate
from tools.governed_memory_validation import (
    run_disposable_installation_live_proof as runner,
)


RUNNER_RELATIVE: Final = (
    "tools/governed_memory_validation/run_disposable_installation_live_proof.py"
)
RECOVERY_CAPSULE_NONCE_DOMAIN: Final = b"governed-memory-phase9-recovery-capsule-nonce-v1\x00"
RUNNER_TIMEOUT_SECONDS: Final = 20 * 60
RUNNER_REAP_TIMEOUT_SECONDS: Final = 5.0
RUNNER_REAP_POLL_SECONDS: Final = 0.01
MAX_RUNNER_OUTPUT_BYTES: Final = 1024 * 1024
RECOVERY_CAPSULE_TEMP_SUFFIX: Final = ".publishing"
_SAFE_ENVIRONMENT: Final = MappingProxyType(
    {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
)
_ERROR_RE: Final = re.compile(
    r"phase9_live_proof_[a-z0-9_]{1,140}\Z", re.ASCII
)
_GIT: Final = "/usr/bin/git"
LEASE_GUARD_PYTHON: Final = "/usr/bin/python3.12"
LEASE_GUARD: Final = (
    "/var/lib/chat-memory-change-leases-v1/control/bin/"
    "chat_memory_lease_guard.py"
)
LEASE_GUARD_UID: Final = 1000
LEASE_GUARD_GID: Final = 1000
MANAGER_AUTHORITY_RECHECK_SECONDS: Final = 30.0
_LEASE_ID_RE: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z", re.ASCII
)
_MANAGER_LOSS_CODES: Final = frozenset(
    {
        "phase9_proof_issuer_manager_control_lost",
        "phase9_proof_issuer_production_write_lease_denied",
    }
)


class Phase9ProofIssuerError(RuntimeError):
    """Content-free refusal from the repository-only proof issuer."""


@dataclass(frozen=True, slots=True)
class CapsulePublicationObservation:
    """One verified crash state for the fixed capsule publication."""

    state: str
    capsule: runner.VerifiedRecoveryCapsule
    capsule_sha256: str
    inode: tuple[int, int]


def _require_closed_issuer_runtime() -> None:
    """Refuse authority issuance outside the exact isolated no-bytecode runtime."""

    if not (_ISOLATED_RUNTIME_AT_START and _DONT_WRITE_BYTECODE_AT_START):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_runtime_isolation_required"
        )


def _selected_lease_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if environment is None else environment
    selected: dict[str, str] = {}
    for key in ("CHAT_MEMORY_LEASE_ID", "CODEX_TASK_ID", "CODEX_THREAD_ID"):
        value = source.get(key)
        if type(value) is not str or _LEASE_ID_RE.fullmatch(value) is None:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_production_write_lease_denied"
            )
        selected[key] = value
    return selected


def _require_production_write_lease(
    environment: Mapping[str, str] | None = None,
    *,
    command_runner: object = subprocess,
) -> None:
    selected = _selected_lease_environment(environment)
    guard_environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        **selected,
    }
    try:
        completed = command_runner.run(
            (
                LEASE_GUARD_PYTHON,
                LEASE_GUARD,
                "--operation",
                "production-write",
                "--worktree",
                str(_ISSUER_REPOSITORY_ROOT),
            ),
            cwd="/",
            env=guard_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
            check=False,
            user=LEASE_GUARD_UID,
            group=LEASE_GUARD_GID,
            extra_groups=(),
        )
    except (OSError, subprocess.SubprocessError, AttributeError) as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_production_write_lease_denied"
        ) from error
    if (
        completed.returncode != 0
        or completed.stdout != b""
        or completed.stderr != b""
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_production_write_lease_denied"
        )


def _require_manager_control_health() -> None:
    """Require the original manager parent and its exact open control pipe."""

    try:
        manager_pid = _preimport_manager_pid()
        opened = os.fstat(MANAGER_CONTROL_FD)
        flags = fcntl.fcntl(MANAGER_CONTROL_FD, fcntl.F_GETFL)
        ready, unused_write, unused_exception = select.select(
            [MANAGER_CONTROL_FD], [], [], 0
        )
    except (OSError, RuntimeError, ValueError, AttributeError, TypeError) as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_manager_control_lost"
        ) from error
    if (
        _MANAGER_CONTROL_LOST
        or manager_pid != _DIRECT_MANAGER_PID
        or _DIRECT_MANAGER_PIPE_ID != (opened.st_dev, opened.st_ino)
        or not stat.S_ISFIFO(opened.st_mode)
        or flags & os.O_ACCMODE != os.O_RDONLY
        or ready
        or unused_write
        or unused_exception
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_manager_control_lost"
        )


def _require_active_manager_authority() -> None:
    """Require live manager lineage and the current fixed production lease."""

    _require_manager_control_health()
    _require_production_write_lease()


def verify_exact_clean_candidate(inputs: runner.ProofInputs) -> None:
    """Bind this repository-only authority source to the supplied clean tree."""

    issuer_relative = (
        "tools/governed_memory_validation/"
        "issue_disposable_installation_live_proof.py"
    )
    if (
        _ISSUER_REPOSITORY_ROOT.resolve(strict=True)
        != _ISSUER_REPOSITORY_ROOT
        or _ISSUER_PATH.resolve(strict=True) != _ISSUER_PATH
        or _ISSUER_PATH.relative_to(_ISSUER_REPOSITORY_ROOT).as_posix()
        != issuer_relative
        or not _ISSUER_PATH.is_file()
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_candidate_source_invalid"
        )
    fixed_commands = (
        ("rev-parse", "--show-toplevel"),
        ("rev-parse", "HEAD"),
        ("rev-parse", "HEAD^{tree}"),
        ("status", "--porcelain=v1", "--untracked-files=all"),
        ("ls-files", "--error-unmatch", "--stage", "--", issuer_relative),
        ("rev-parse", "HEAD:" + issuer_relative),
        ("hash-object", str(_ISSUER_PATH)),
    )
    outputs: list[str] = []
    try:
        for arguments in fixed_commands:
            completed = subprocess.run(
                (
                    _GIT,
                    "-c",
                    "safe.directory=" + str(_ISSUER_REPOSITORY_ROOT),
                    "-C",
                    str(_ISSUER_REPOSITORY_ROOT),
                    *arguments,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
                env=dict(_SAFE_ENVIRONMENT),
            )
            if (
                completed.returncode != 0
                or len(completed.stdout) > 1024 * 1024
                or completed.stderr is not None
            ):
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_candidate_git_invalid"
                )
            outputs.append(completed.stdout.decode("ascii").strip())
    except Phase9ProofIssuerError:
        raise
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_candidate_git_invalid"
        ) from error
    if (
        outputs[0] != str(_ISSUER_REPOSITORY_ROOT)
        or outputs[1] != inputs.candidate_git_commit
        or outputs[2] != inputs.candidate_git_tree
        or outputs[3] != ""
        or re.fullmatch(
            r"100644 [0-9a-f]{40} 0\t" + re.escape(issuer_relative),
            outputs[4],
            re.ASCII,
        )
        is None
        or outputs[5] != outputs[6]
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_candidate_git_mismatch"
        )
    _require_exact_permitted_candidate(inputs)


def _require_exact_permitted_candidate(
    inputs: runner.ProofInputs,
) -> phase9_permitted_candidate.PermittedCandidateAuthority:
    """Bind caller inputs to the root permit, fixed tag, and current sources."""

    try:
        return phase9_permitted_candidate.require_exact_permitted_candidate(
            expected_candidate_git_commit=inputs.candidate_git_commit,
            expected_candidate_git_tree=inputs.candidate_git_tree,
            expected_package_manifest_sha256=(
                inputs.package_manifest_sha256
            ),
            expected_controller_runtime_receipt_sha256=(
                inputs.controller_runtime_receipt_sha256
            ),
            expected_thread_id=runner.THREAD_ID,
            expected_authorization_text_sha256=(
                runner.AUTHORIZED_TEXT_SHA256
            ),
        )
    except phase9_permitted_candidate.Phase9PermittedCandidateError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_candidate_permit_required"
        ) from error


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
        raise Phase9ProofIssuerError("phase9_proof_issuer_document_invalid") from error


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _utc_seconds(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is not timezone.utc:
        raise Phase9ProofIssuerError("phase9_proof_issuer_time_invalid")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _public_key(private_key: Ed25519PrivateKey) -> tuple[str, str]:
    if not isinstance(private_key, Ed25519PrivateKey):
        raise Phase9ProofIssuerError("phase9_proof_issuer_key_invalid")
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii"), _sha(raw)


def _signed_envelope(
    *,
    schema_version: str,
    payload: Mapping[str, object],
    private_key: Ed25519PrivateKey,
    key_id: str,
) -> dict[str, object]:
    raw = _canonical(dict(payload))
    return {
        "schema_version": schema_version,
        "payload": dict(payload),
        "signature": {
            "algorithm": "Ed25519",
            "key_id": key_id,
            "value_base64": base64.b64encode(private_key.sign(raw)).decode("ascii"),
        },
    }


def build_exact_recovery_capsule(
    *,
    inputs: runner.ProofInputs,
    artifacts: Mapping[str, bytes],
    private_key: Ed25519PrivateKey,
    issued_at: datetime,
) -> Mapping[str, object]:
    """Build the exact fixed capsule; all nonces derive from immutable inputs."""

    if type(inputs) is not runner.ProofInputs or type(artifacts) is not dict:
        raise Phase9ProofIssuerError("phase9_proof_issuer_inputs_invalid")
    public_key_base64, key_id = _public_key(private_key)
    issued = issued_at.replace(microsecond=0)
    not_before = issued
    expires = issued + timedelta(minutes=15)
    issued_text = _utc_seconds(issued)
    not_before_text = _utc_seconds(not_before)
    expires_text = _utc_seconds(expires)
    contract_raw = artifacts.get(
        "ops/governed_memory/installation/current/contract.json"
    )
    plan_raw = artifacts.get(
        "ops/governed_memory/installation/current/controller_plan.json"
    )
    if type(contract_raw) is not bytes or type(plan_raw) is not bytes:
        raise Phase9ProofIssuerError("phase9_proof_issuer_artifact_missing")
    contract = runner._parse_exact_hashed_json_object(
        contract_raw, "phase9_proof_issuer_contract_invalid"
    )
    exact_targets = contract.get("exact_targets")
    if type(exact_targets) is not dict:
        raise Phase9ProofIssuerError("phase9_proof_issuer_contract_invalid")
    exact_targets_sha256 = runner._document_sha(exact_targets)
    nonce_material = b"\x00".join(
        value.encode("ascii")
        for value in (
            inputs.candidate_git_commit,
            inputs.candidate_git_tree,
            inputs.package_manifest_sha256,
            inputs.controller_runtime_receipt_sha256,
            _sha(contract_raw),
            _sha(plan_raw),
            exact_targets_sha256,
            key_id,
            issued_text,
        )
    )
    recovery_capsule_nonce = _sha(RECOVERY_CAPSULE_NONCE_DOMAIN + nonce_material)
    install_scope = runner._install_scope(inputs, artifacts)
    install_scope_raw = _canonical(install_scope)
    install_nonce = _sha(
        runner.INSTALL_NONCE_DOMAIN + recovery_capsule_nonce.encode("ascii")
    )
    install_payload = {
        "schema_version": authority.AUTHORIZATION_PAYLOAD_SCHEMA_VERSION,
        "authorization_id": "phase9-disposable-live-install-auth-000003",
        "authorization_namespace": runner.AUTHORIZATION_NAMESPACE,
        "thread_id": runner.THREAD_ID,
        "scope_id": runner.INSTALL_SCOPE_ID,
        "scope_sha256": _sha(install_scope_raw),
        "key_id": key_id,
        "approval_phrase": (
            "APPROVE GOVERNED MEMORY DORMANT STORE INSTALL "
            + _sha(install_scope_raw)
        ),
        "nonce": install_nonce,
        "issued_at": issued_text,
        "not_before": not_before_text,
        "expires_at": expires_text,
        "single_use": True,
    }
    install_authorization = _signed_envelope(
        schema_version=authority.AUTHORIZATION_SCHEMA_VERSION,
        payload=install_payload,
        private_key=private_key,
        key_id=key_id,
    )
    trust_bundle = {
        "schema_version": authority.TRUST_BUNDLE_SCHEMA_VERSION,
        "authorization_namespace": runner.AUTHORIZATION_NAMESPACE,
        "keys": [
            {
                "key_id": key_id,
                "algorithm": "Ed25519",
                "public_key_base64": public_key_base64,
            }
        ],
    }
    install_documents = runner.InstallDocuments(
        scope=install_scope_raw,
        authorization=_canonical(install_authorization),
        trust_bundle=_canonical(trust_bundle),
        key_id=key_id,
    )
    scope_capability = authority.verify_dormant_install_execution_capability(
        install_documents.scope,
        install_documents.authorization,
        install_documents.trust_bundle,
        expected_namespace=runner.AUTHORIZATION_NAMESPACE,
        expected_thread_id=runner.THREAD_ID,
        expected_scope_id=runner.INSTALL_SCOPE_ID,
        expected_key_id=key_id,
        expected_trust_bundle_sha256=_sha(install_documents.trust_bundle),
        expected_bindings=authority.DormantInstallExpectedBindings(
            candidate_git_commit=inputs.candidate_git_commit,
            candidate_git_tree=inputs.candidate_git_tree,
            package_manifest_sha256=inputs.package_manifest_sha256,
            controller_contract_sha256=_sha(contract_raw),
            execution_plan_sha256=_sha(plan_raw),
            exact_targets_sha256=exact_targets_sha256,
            controller_runtime_receipt_sha256=(
                inputs.controller_runtime_receipt_sha256
            ),
        ),
    )
    installation_execution_id = runner._install_authority_material(
        scope_capability
    )["execution_id"]
    rollback_nonce = _sha(
        runner.ROLLBACK_NONCE_DOMAIN + recovery_capsule_nonce.encode("ascii")
    )
    recovery_reservation_nonce = _sha(
        runner.RECOVERY_RESERVATION_NONCE_DOMAIN
        + recovery_capsule_nonce.encode("ascii")
    )
    rollback_delegation_payload = {
        "schema_version": (
            "governed-memory-empty-store-recovery-delegation-v1"
        ),
        "authorization_namespace": runner.AUTHORIZATION_NAMESPACE,
        "thread_id": runner.THREAD_ID,
        "install_scope_id": runner.INSTALL_SCOPE_ID,
        "rollback_scope_id": runner.ROLLBACK_SCOPE_ID,
        "authorization_text_sha256": runner.AUTHORIZED_TEXT_SHA256,
        "candidate_git_commit": inputs.candidate_git_commit,
        "candidate_git_tree": inputs.candidate_git_tree,
        "package_manifest_sha256": inputs.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            inputs.controller_runtime_receipt_sha256
        ),
        "controller_contract_sha256": _sha(contract_raw),
        "execution_plan_sha256": _sha(plan_raw),
        "exact_target_contract_sha256": exact_targets_sha256,
        "installation_execution_id": installation_execution_id,
        "rollback_nonce": rollback_nonce,
        "recovery_reservation_nonce": recovery_reservation_nonce,
        "key_id": key_id,
        "issued_at": issued_text,
        "not_before": not_before_text,
        "expires_at": expires_text,
        "single_use": True,
        "empty_only": True,
        "derivation_policy": dict(runner.RECOVERY_DERIVATION_POLICY),
        "source_postgres_read_count": 0,
        "production_data_read": False,
        "provider_calls": 0,
        "activation_allowed": False,
    }
    rollback_delegation = _signed_envelope(
        schema_version=(
            "governed-memory-empty-store-recovery-delegation-envelope-v1"
        ),
        payload=rollback_delegation_payload,
        private_key=private_key,
        key_id=key_id,
    )
    capsule = {
        "schema_version": runner.RECOVERY_CAPSULE_SCHEMA,
        "thread_id": runner.THREAD_ID,
        "authorization_text_sha256": runner.AUTHORIZED_TEXT_SHA256,
        "authorization_namespace": runner.AUTHORIZATION_NAMESPACE,
        "install_scope_id": runner.INSTALL_SCOPE_ID,
        "rollback_scope_id": runner.ROLLBACK_SCOPE_ID,
        "candidate_git_commit": inputs.candidate_git_commit,
        "candidate_git_tree": inputs.candidate_git_tree,
        "package_manifest_sha256": inputs.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            inputs.controller_runtime_receipt_sha256
        ),
        "controller_contract_sha256": _sha(contract_raw),
        "execution_plan_sha256": _sha(plan_raw),
        "exact_targets_sha256": exact_targets_sha256,
        "public_key_base64": public_key_base64,
        "key_id": key_id,
        "recovery_capsule_nonce": recovery_capsule_nonce,
        "issued_at": issued_text,
        "not_before": not_before_text,
        "expires_at": expires_text,
        "single_use": True,
        "install_scope": install_scope,
        "install_authorization": install_authorization,
        "trust_bundle": trust_bundle,
        "rollback_delegation": rollback_delegation,
    }
    if set(capsule) != runner._RECOVERY_CAPSULE_KEYS:
        raise Phase9ProofIssuerError("phase9_proof_issuer_recovery_capsule_invalid")
    return MappingProxyType(capsule)


def _ensure_recovery_capsule_parent() -> int:
    if os.geteuid() != 0:
        raise Phase9ProofIssuerError("phase9_proof_issuer_root_required")
    parent = runner.RECOVERY_CAPSULE_PATH.parent
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = -1
    try:
        descriptor = os.open(parent, flags)
        opened = os.fstat(descriptor)
        named = parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o700
            or opened.st_uid != 0
            or opened.st_gid != 0
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_parent_invalid"
            )
        return descriptor
    except Phase9ProofIssuerError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except OSError as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_recovery_capsule_parent_invalid"
        ) from error
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _recovery_capsule_temp_name() -> str:
    return runner.RECOVERY_CAPSULE_PATH.name + RECOVERY_CAPSULE_TEMP_SUFFIX


def _read_capsule_member(
    parent_fd: int,
    name: str,
    *,
    allowed_link_counts: frozenset[int],
    allow_empty: bool = False,
) -> tuple[bytes, os.stat_result]:
    """Read one protected capsule inode without following or racing its name."""

    descriptor = -1
    try:
        parent_before = os.fstat(parent_fd)
        named_parent_before = runner.RECOVERY_CAPSULE_PATH.parent.stat(
            follow_symlinks=False
        )
        if (
            not stat.S_ISDIR(parent_before.st_mode)
            or stat.S_IMODE(parent_before.st_mode) != 0o700
            or parent_before.st_uid != 0
            or parent_before.st_gid != 0
            or (parent_before.st_dev, parent_before.st_ino)
            != (named_parent_before.st_dev, named_parent_before.st_ino)
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_parent_invalid"
            )
        parent_identity = (parent_before.st_dev, parent_before.st_ino)
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        except FileNotFoundError as error:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_absent"
            ) from error
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o400
            or opened.st_uid != 0
            or opened.st_gid != 0
            or opened.st_nlink not in allowed_link_counts
            or opened.st_size < (0 if allow_empty else 1)
            or opened.st_size > 64 * 1024
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_invalid"
            )
        raw = os.read(descriptor, opened.st_size + 1)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        parent_after = os.fstat(parent_fd)
        named_parent_after = runner.RECOVERY_CAPSULE_PATH.parent.stat(
            follow_symlinks=False
        )
        if (
            len(raw) != opened.st_size
            or runner._stable_file_identity(after)
            != runner._stable_file_identity(opened)
            or runner._stable_file_identity(named_after)
            != runner._stable_file_identity(opened)
            or (parent_after.st_dev, parent_after.st_ino) != parent_identity
            or (named_parent_after.st_dev, named_parent_after.st_ino)
            != parent_identity
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_invalid"
            )
        return raw, after
    except Phase9ProofIssuerError:
        raise
    except OSError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_recovery_capsule_invalid"
        ) from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_optional_capsule_member(
    parent_fd: int,
    name: str,
    *,
    allowed_link_counts: frozenset[int],
    allow_empty: bool = False,
) -> tuple[bytes, os.stat_result] | None:
    """Return None only for a stable initial leaf absence."""

    try:
        return _read_capsule_member(
            parent_fd,
            name,
            allowed_link_counts=allowed_link_counts,
            allow_empty=allow_empty,
        )
    except Phase9ProofIssuerError as error:
        if str(error) != "phase9_proof_issuer_recovery_capsule_absent":
            raise
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as observed:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_invalid"
            ) from observed
        else:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_appeared"
            )
        parent = os.fstat(parent_fd)
        named_parent = runner.RECOVERY_CAPSULE_PATH.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != 0
            or parent.st_gid != 0
            or (parent.st_dev, parent.st_ino)
            != (named_parent.st_dev, named_parent.st_ino)
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_parent_invalid"
            )
        return None


def _unlink_exact_member(
    parent_fd: int,
    name: str,
    expected_inode: tuple[int, int],
) -> None:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != expected_inode:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_cleanup_failed"
            )
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileNotFoundError:
        return
    except Phase9ProofIssuerError:
        raise
    except OSError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_recovery_capsule_cleanup_failed"
        ) from error


def _publish_temp_link(
    parent_fd: int,
    *,
    temp_inode: tuple[int, int],
) -> None:
    """Publish a complete temp inode at the fixed final name without replace."""

    try:
        os.link(
            _recovery_capsule_temp_name(),
            runner.RECOVERY_CAPSULE_PATH.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        temp = os.stat(
            _recovery_capsule_temp_name(), dir_fd=parent_fd, follow_symlinks=False
        )
        final = os.stat(
            runner.RECOVERY_CAPSULE_PATH.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            (temp.st_dev, temp.st_ino) != temp_inode
            or (final.st_dev, final.st_ino) != temp_inode
            or temp.st_nlink != 2
            or final.st_nlink != 2
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_publication_invalid"
            )
        os.fsync(parent_fd)
    except FileExistsError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_existing_recovery_capsule_refused"
        ) from error
    except Phase9ProofIssuerError:
        raise
    except OSError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_recovery_capsule_publication_failed"
        ) from error


def _verify_exact_empty_directory(path: Path, mode: int) -> None:
    """Prove one fixed root-owned directory is empty and identity-stable."""

    descriptor = -1
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(before.st_mode)
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_uid != 0
            or before.st_gid != 0
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            or os.listdir(descriptor) != []
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_staged_cleanup_not_pristine"
            )
        after = os.fstat(descriptor)
        named_after = path.stat(follow_symlinks=False)
        if (
            runner._stable_file_identity(after)
            != runner._stable_file_identity(before)
            or (named_after.st_dev, named_after.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_staged_cleanup_not_pristine"
            )
    except Phase9ProofIssuerError:
        raise
    except OSError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_staged_cleanup_not_pristine"
        ) from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _verify_pristine_authority_state() -> None:
    """Allow absence or the exact initialized v2 database with zero effects."""

    path = runner.AUTHORITY_STATE_PATH
    sidecars = tuple(Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm"))
    for sidecar in sidecars:
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_staged_cleanup_not_pristine"
            ) from error
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_staged_cleanup_not_pristine"
        )
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_staged_cleanup_not_pristine"
        ) from error
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_uid != 0
        or before.st_gid != 0
        or before.st_nlink != 1
        or not 1 <= before.st_size <= 8 * 1024 * 1024
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_staged_cleanup_not_pristine"
        )
    try:
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro&immutable=1",
            uri=True,
            isolation_level=None,
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            application_id = connection.execute(
                "PRAGMA application_id"
            ).fetchone()[0]
            user_version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            quick_check = connection.execute("PRAGMA quick_check").fetchall()
            objects = connection.execute(
                "SELECT type, name, sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            expected_objects = [
                (
                    "table",
                    "authority_state_identity_v1",
                    authority_state_module._STATE_IDENTITY_TABLE_SQL,
                ),
                (
                    "table",
                    "filesystem_identity_seal_v1",
                    authority_state_module._FILESYSTEM_IDENTITY_TABLE_SQL,
                ),
                (
                    "table",
                    "journal_anchor_v1",
                    authority_state_module._ANCHOR_TABLE_SQL,
                ),
                (
                    "table",
                    "nonce_claim_v1",
                    authority_state_module._NONCE_TABLE_SQL,
                ),
                (
                    "table",
                    "resource_ledger_anchor_v1",
                    authority_state_module._RESOURCE_ANCHOR_TABLE_SQL,
                ),
            ]
            counts = {
                table: connection.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
                for unused_type, table, unused_sql in expected_objects
                if table != "authority_state_identity_v1"
            }
            identity = connection.execute(
                "SELECT singleton, path_sha256, directory_device, "
                "directory_inode, database_device, database_inode "
                "FROM authority_state_identity_v1"
            ).fetchall()
        finally:
            connection.close()
    except Exception as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_staged_cleanup_not_pristine"
        ) from error
    parent = path.parent.stat(follow_symlinks=False)
    after = path.stat(follow_symlinks=False)
    if (
        application_id != STATE_APPLICATION_ID
        or user_version != STATE_SCHEMA_VERSION
        or quick_check != [("ok",)]
        or objects != expected_objects
        or any(value != 0 for value in counts.values())
        or identity
        != [
            (
                1,
                _sha(str(path).encode("utf-8")),
                parent.st_dev,
                parent.st_ino,
                before.st_dev,
                before.st_ino,
            )
        ]
        or runner._stable_file_identity(after)
        != runner._stable_file_identity(before)
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_staged_cleanup_not_pristine"
        )
    for sidecar in sidecars:
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_staged_cleanup_not_pristine"
            ) from error
        else:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_staged_cleanup_not_pristine"
            )


def _production_staged_prefix_expectation(
    inputs: runner.ProofInputs,
) -> staged_prefix_disposition.ReviewedStagedPrefixExpectation:
    return staged_prefix_disposition.ReviewedStagedPrefixExpectation(
        contract_sha256=str(
            staged_prefix_disposition.PRODUCTION_CONTRACT_SHA256
        ),
        disposition_id=staged_prefix_disposition.PRODUCTION_DISPOSITION_ID,
        old_tag_ref=staged_prefix_disposition.PRODUCTION_OLD_TAG_REF,
        old_tag_commit=staged_prefix_disposition.PRODUCTION_OLD_TAG_COMMIT,
        old_tag_tree=staged_prefix_disposition.PRODUCTION_OLD_TAG_TREE,
        failed_package_manifest_sha256=(
            staged_prefix_disposition.PRODUCTION_FAILED_PACKAGE_MANIFEST_SHA256
        ),
        failed_controller_runtime_receipt_sha256=(
            staged_prefix_disposition.PRODUCTION_FAILED_RUNTIME_RECEIPT_SHA256
        ),
        corrected_generation=(
            staged_prefix_disposition.PRODUCTION_CORRECTED_GENERATION
        ),
        corrected_package_manifest_sha256=inputs.package_manifest_sha256,
        corrected_controller_runtime_receipt_sha256=(
            inputs.controller_runtime_receipt_sha256
        ),
    )


def _verified_production_disposition_contract(
    inputs: runner.ProofInputs,
) -> Mapping[str, object]:
    paths = staged_prefix_disposition.production_disposition_paths()
    expectation = _production_staged_prefix_expectation(inputs)
    try:
        tombstone = staged_prefix_disposition.verify_staged_prefix_tombstone(
            paths, expectation
        )
        contract, unused_stable = staged_prefix_disposition._load_contract(
            paths, expectation
        )
    except staged_prefix_disposition.StagedPrefixDispositionError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_staged_cleanup_not_pristine"
        ) from error
    if tombstone is None:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_staged_cleanup_not_pristine"
        )
    return MappingProxyType(contract)


def _require_pristine_staged_cleanup_state(
    *,
    inputs: runner.ProofInputs,
) -> None:
    """Prove no installation effect before removing one failed staging inode."""

    try:
        runner.RECOVERY_CAPSULE_PATH.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_staged_cleanup_not_pristine"
        ) from error
    else:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_staged_cleanup_not_pristine"
        )
    _verify_pristine_authority_state()
    _verify_exact_empty_directory(runner.EXECUTIONS_ROOT, 0o700)
    _verify_exact_empty_directory(runner.STORE_SECRET_ROOT, 0o700)
    contract = _verified_production_disposition_contract(inputs)
    resources = contract.get("absent_resources")
    if type(resources) is not list:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_staged_cleanup_not_pristine"
        )
    allowed_present_paths = {
        str(runner.AUTHORITY_STATE_PATH),
        str(runner.EXECUTIONS_ROOT),
        str(runner.STORE_SECRET_ROOT),
        str(runner.RECOVERY_CAPSULE_PATH),
        str(runner.RECOVERY_CAPSULE_STAGING_PATH),
    }
    checked = [
        item
        for item in resources
        if not (
            type(item) is dict
            and item.get("kind") == "path"
            and item.get("identity") in allowed_present_paths
        )
    ]
    try:
        disposition_paths = staged_prefix_disposition.production_disposition_paths()
        expectation = _production_staged_prefix_expectation(inputs)
        staged_prefix_disposition._verify_resource_subset_absence(
            checked,
            paths=disposition_paths,
            runner=staged_prefix_disposition._ClosedCommandRunner(
                staged_prefix_disposition._allowed_commands(
                    disposition_paths, expectation
                )
            ),
        )
    except staged_prefix_disposition.StagedPrefixDispositionError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_staged_cleanup_not_pristine"
        ) from error


def reconcile_capsule_publication(
    *,
    inputs: runner.ProofInputs,
    artifacts: Mapping[str, bytes],
) -> CapsulePublicationObservation | None:
    """Classify the exact crash state without creating authority or publishing."""

    parent_fd = _ensure_recovery_capsule_parent()
    temp_name = _recovery_capsule_temp_name()
    try:
        final_member = _read_optional_capsule_member(
            parent_fd,
            runner.RECOVERY_CAPSULE_PATH.name,
            allowed_link_counts=frozenset({1, 2}),
        )
        temp_member = _read_optional_capsule_member(
            parent_fd,
            temp_name,
            allowed_link_counts=frozenset({1, 2}),
            allow_empty=True,
        )
        if final_member is None and temp_member is None:
            for name in (runner.RECOVERY_CAPSULE_PATH.name, temp_name):
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise Phase9ProofIssuerError(
                        "phase9_proof_issuer_recovery_capsule_invalid"
                    ) from error
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_recovery_capsule_appeared"
                )
            opened_parent = os.fstat(parent_fd)
            named_parent = runner.RECOVERY_CAPSULE_PATH.parent.stat(
                follow_symlinks=False
            )
            if (
                not stat.S_ISDIR(opened_parent.st_mode)
                or stat.S_IMODE(opened_parent.st_mode) != 0o700
                or opened_parent.st_uid != 0
                or opened_parent.st_gid != 0
                or (opened_parent.st_dev, opened_parent.st_ino)
                != (named_parent.st_dev, named_parent.st_ino)
            ):
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_recovery_capsule_parent_invalid"
                )
            return None
        verified_final: runner.VerifiedRecoveryCapsule | None = None
        verified_temp: runner.VerifiedRecoveryCapsule | None = None
        if final_member is not None:
            final_raw, final = final_member
            try:
                verified_final = runner._verify_recovery_capsule_raw(
                    inputs, artifacts, final_raw, require_current=False
                )
            except runner.LiveProofError as error:
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_recovery_capsule_invalid"
                ) from error
        if temp_member is not None:
            temp_raw, temp = temp_member
            try:
                verified_temp = runner._verify_recovery_capsule_raw(
                    inputs, artifacts, temp_raw, require_current=False
                )
            except runner.LiveProofError as error:
                if final_member is not None or temp.st_nlink != 1:
                    raise Phase9ProofIssuerError(
                        "phase9_proof_issuer_recovery_capsule_invalid"
                    ) from error
                _require_pristine_staged_cleanup_state(inputs=inputs)
                inode = (temp.st_dev, temp.st_ino)
                _require_active_manager_authority()
                _unlink_exact_member(parent_fd, temp_name, inode)
                if _read_optional_capsule_member(
                    parent_fd,
                    temp_name,
                    allowed_link_counts=frozenset({1}),
                    allow_empty=True,
                ) is not None:
                    raise Phase9ProofIssuerError(
                        "phase9_proof_issuer_recovery_capsule_cleanup_failed"
                    )
                return None
        if final_member is not None and temp_member is not None:
            final_raw, final = final_member
            temp_raw, temp = temp_member
            if (
                final_raw != temp_raw
                or (final.st_dev, final.st_ino)
                != (temp.st_dev, temp.st_ino)
                or final.st_nlink != 2
                or temp.st_nlink != 2
            ):
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_capsule_publication_drift"
                )
            inode = (final.st_dev, final.st_ino)
            state = "linked"
            verified = verified_final
            raw = final_raw
        elif temp_member is not None:
            temp_raw, temp = temp_member
            if temp.st_nlink != 1:
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_capsule_publication_drift"
            )
            inode = (temp.st_dev, temp.st_ino)
            state = "staged"
            verified = verified_temp
            raw = temp_raw
        else:
            assert final_member is not None
            final_raw, final = final_member
            if final.st_nlink != 1:
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_capsule_publication_drift"
            )
            inode = (final.st_dev, final.st_ino)
            state = "published"
            verified = verified_final
            raw = final_raw
        os.fsync(parent_fd)
        if verified is None:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_invalid"
            )
        return CapsulePublicationObservation(
            state=state,
            capsule=verified,
            capsule_sha256=_sha(raw),
            inode=inode,
        )
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def prepare_fixed_recovery_capsule(
    capsule: Mapping[str, object],
) -> tuple[str, tuple[int, int]]:
    """Durably prepare one complete unpublished fixed capsule."""

    raw = _canonical(dict(capsule))
    parent_fd = _ensure_recovery_capsule_parent()
    file_fd = -1
    temp_created = False
    temp_inode: tuple[int, int] | None = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            _require_active_manager_authority()
            file_fd = os.open(
                _recovery_capsule_temp_name(), flags, 0o400, dir_fd=parent_fd
            )
            temp_created = True
        except FileExistsError as error:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_existing_recovery_capsule_temp_refused"
            ) from error
        named_created = os.stat(
            _recovery_capsule_temp_name(), dir_fd=parent_fd, follow_symlinks=False
        )
        temp_inode = (named_created.st_dev, named_created.st_ino)
        created = os.fstat(file_fd)
        if (created.st_dev, created.st_ino) != temp_inode:
            raise Phase9ProofIssuerError("phase9_proof_issuer_recovery_capsule_invalid")
        os.fchown(file_fd, 0, 0)
        os.fchmod(file_fd, 0o400)
        view = memoryview(raw)
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.fsync(file_fd)
        opened = os.fstat(file_fd)
        named = os.stat(
            _recovery_capsule_temp_name(), dir_fd=parent_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o400
            or opened.st_uid != 0
            or opened.st_gid != 0
            or opened.st_nlink != 1
            or opened.st_size != len(raw)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or (opened.st_dev, opened.st_ino) != temp_inode
        ):
            raise Phase9ProofIssuerError("phase9_proof_issuer_recovery_capsule_invalid")
        try:
            os.close(file_fd)
        except OSError as error:
            file_fd = -1
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_write_failed"
            ) from error
        file_fd = -1
        os.fsync(parent_fd)
        staged_raw, staged = _read_capsule_member(
            parent_fd,
            _recovery_capsule_temp_name(),
            allowed_link_counts=frozenset({1}),
        )
        if staged_raw != raw or (staged.st_dev, staged.st_ino) != temp_inode:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_write_failed"
            )
        return _sha(raw), temp_inode
    except BaseException as error:
        if temp_created and temp_inode is not None:
            try:
                _unlink_exact_member(parent_fd, _recovery_capsule_temp_name(), temp_inode)
            except Phase9ProofIssuerError:
                raise
        if isinstance(error, Phase9ProofIssuerError):
            raise
        if isinstance(error, OSError):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_write_failed"
            ) from error
        raise
    finally:
        if file_fd >= 0:
            try:
                os.close(file_fd)
            except OSError:
                pass
        try:
            os.close(parent_fd)
        except OSError:
            pass


def complete_linked_capsule_publication(
    *,
    expected_capsule_sha256: str,
    expected_inode: tuple[int, int],
) -> None:
    """Finish only the exact already-linked publication after pair proof."""

    parent_fd = _ensure_recovery_capsule_parent()
    try:
        temp_raw, temp = _read_capsule_member(
            parent_fd,
            _recovery_capsule_temp_name(),
            allowed_link_counts=frozenset({2}),
        )
        final_raw, final = _read_capsule_member(
            parent_fd,
            runner.RECOVERY_CAPSULE_PATH.name,
            allowed_link_counts=frozenset({2}),
        )
        if (
            _sha(temp_raw) != expected_capsule_sha256
            or final_raw != temp_raw
            or (temp.st_dev, temp.st_ino) != expected_inode
            or (final.st_dev, final.st_ino) != expected_inode
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_capsule_publication_drift"
            )
        _require_active_manager_authority()
        _unlink_exact_member(
            parent_fd, _recovery_capsule_temp_name(), expected_inode
        )
        published_raw, published = _read_capsule_member(
            parent_fd,
            runner.RECOVERY_CAPSULE_PATH.name,
            allowed_link_counts=frozenset({1}),
        )
        if (
            _sha(published_raw) != expected_capsule_sha256
            or (published.st_dev, published.st_ino) != expected_inode
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_capsule_publication_drift"
            )
        os.fsync(parent_fd)
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def commit_preclaimed_recovery_capsule(
    *,
    expected_capsule_sha256: str,
    expected_inode: tuple[int, int],
) -> None:
    """Publish only the exact staged capsule whose start pair was preclaimed."""

    parent_fd = _ensure_recovery_capsule_parent()
    try:
        raw, staged = _read_capsule_member(
            parent_fd,
            _recovery_capsule_temp_name(),
            allowed_link_counts=frozenset({1}),
        )
        if (
            _sha(raw) != expected_capsule_sha256
            or (staged.st_dev, staged.st_ino) != expected_inode
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_capsule_publication_drift"
            )
        if _read_optional_capsule_member(
            parent_fd,
            runner.RECOVERY_CAPSULE_PATH.name,
            allowed_link_counts=frozenset({1, 2}),
        ) is not None:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_existing_recovery_capsule_refused"
            )
        _require_active_manager_authority()
        _publish_temp_link(parent_fd, temp_inode=expected_inode)
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass
    complete_linked_capsule_publication(
        expected_capsule_sha256=expected_capsule_sha256,
        expected_inode=expected_inode,
    )


def _runner_argv(
    inputs: runner.ProofInputs,
    mode: runner.RunnerMode = runner.RunnerMode.START_OR_RECOVER,
) -> tuple[str, ...]:
    if type(mode) is not runner.RunnerMode:
        raise Phase9ProofIssuerError("phase9_proof_issuer_mode_invalid")
    runtime_python = str(
        Path("/opt/governed-memory-controller/runtimes")
        / inputs.controller_runtime_receipt_sha256
        / "bin/python"
    )
    return (
        runtime_python,
        "-I",
        "-B",
        str(
            runner.RELEASE_ROOT_PREFIX
            / inputs.package_manifest_sha256
            / RUNNER_RELATIVE
        ),
        mode.value,
        "--candidate-git-commit",
        inputs.candidate_git_commit,
        "--candidate-git-tree",
        inputs.candidate_git_tree,
        "--package-manifest-sha256",
        inputs.package_manifest_sha256,
        "--controller-runtime-receipt-sha256",
        inputs.controller_runtime_receipt_sha256,
    )


def _close_unintended_runner_descriptors(
    *,
    retain_manager_control: bool,
) -> None:
    """Leave only inert I/O and the mode's exact inherited capabilities."""

    if type(retain_manager_control) is not bool:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_runner_descriptor_seal_failed"
        )
    retained = {0, 1, 2, runner.LIVE_PROOF_GUARD_FD}
    if retain_manager_control:
        retained.add(MANAGER_CONTROL_FD)
    try:
        entries = os.listdir("/proc/self/fd")
    except OSError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_runner_descriptor_seal_failed"
        ) from error
    for entry in entries:
        if not entry.isascii() or not entry.isdecimal():
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_runner_descriptor_seal_failed"
            )
        descriptor = int(entry)
        if descriptor in retained:
            continue
        try:
            os.close(descriptor)
        except OSError as error:
            # The descriptor used internally by listdir may be represented in
            # the completed snapshot but already closed when listdir returns.
            if error.errno != errno.EBADF:
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_runner_descriptor_seal_failed"
                ) from error


def _seal_exact_runner_process(
    *,
    guard_descriptor: int,
    stdout_descriptor: int,
    stderr_descriptor: int,
    mode: runner.RunnerMode,
) -> None:
    """Install the exact inherited process surface immediately before exec."""

    if (
        type(guard_descriptor) is not int
        or guard_descriptor < 3
        or type(stdout_descriptor) is not int
        or stdout_descriptor < 0
        or type(stderr_descriptor) is not int
        or stderr_descriptor < 0
        or type(mode) is not runner.RunnerMode
    ):
        raise Phase9ProofIssuerError("phase9_proof_issuer_guard_invalid")
    stdin_descriptor = os.open(
        "/dev/null",
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    os.dup2(stdin_descriptor, 0)
    os.dup2(stdout_descriptor, 1)
    os.dup2(stderr_descriptor, 2)
    os.dup2(guard_descriptor, runner.LIVE_PROOF_GUARD_FD)
    retained_descriptors = [0, 1, 2, runner.LIVE_PROOF_GUARD_FD]
    retain_manager_control = mode is not runner.RunnerMode.RECOVER_ONLY
    if retain_manager_control:
        try:
            opened = os.fstat(MANAGER_CONTROL_FD)
            flags = fcntl.fcntl(MANAGER_CONTROL_FD, fcntl.F_GETFL)
        except OSError as error:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_runner_descriptor_seal_failed"
            ) from error
        if (
            not stat.S_ISFIFO(opened.st_mode)
            or flags & os.O_ACCMODE != os.O_RDONLY
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_runner_descriptor_seal_failed"
            )
        retained_descriptors.append(MANAGER_CONTROL_FD)
    for descriptor in retained_descriptors:
        os.set_inheritable(descriptor, True)
    _close_unintended_runner_descriptors(
        retain_manager_control=retain_manager_control,
    )


def _runner_environment(
    mode: runner.RunnerMode,
    *,
    issuer_pid: int,
) -> dict[str, str]:
    if type(mode) is not runner.RunnerMode or issuer_pid <= 1:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_runner_environment_invalid"
        )
    environment = dict(_SAFE_ENVIRONMENT)
    if mode is runner.RunnerMode.RECOVER_ONLY:
        return environment
    manager_pid = _preimport_manager_pid()
    environment.update(_selected_lease_environment())
    environment.update(
        {
            "GOVERNED_MEMORY_PHASE9_ISSUER_PID": str(issuer_pid),
            MANAGER_PID_ENVIRONMENT_KEY: str(manager_pid),
            "GOVERNED_MEMORY_PHASE9_ISSUER_REPOSITORY_ROOT": str(
                _ISSUER_REPOSITORY_ROOT
            ),
        }
    )
    return environment


def _arm_runner_parent_death(expected_parent_pid: int) -> None:
    """Kill an orphaned Linux runner before it can create a new session."""

    if (
        sys.platform != "linux"
        or type(expected_parent_pid) is not int
        or expected_parent_pid <= 1
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_runner_parent_fence_invalid"
        )
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
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_runner_parent_fence_invalid"
        ) from error
    if result != 0:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_runner_parent_fence_invalid"
        )
    # PR_SET_PDEATHSIG is not retroactive when the parent exits between fork
    # and prctl. Refuse that race before setsid or exec can detach the runner.
    if os.getppid() != expected_parent_pid:
        try:
            os.kill(os.getpid(), signal.SIGKILL)
        finally:
            os._exit(1)


def _spawn_exact_runner(
    inputs: runner.ProofInputs,
    guard_descriptor: int,
    mode: runner.RunnerMode = runner.RunnerMode.START_OR_RECOVER,
) -> tuple[int, int, int]:
    if type(guard_descriptor) is not int or guard_descriptor < 3:
        raise Phase9ProofIssuerError("phase9_proof_issuer_guard_invalid")
    owned: set[int] = set()
    pid = -1
    try:
        stdout_read, stdout_write = os.pipe()
        owned.update((stdout_read, stdout_write))
        stderr_read, stderr_write = os.pipe()
        owned.update((stderr_read, stderr_write))
        expected_parent_pid = os.getpid()
        runner_environment = _runner_environment(
            mode,
            issuer_pid=expected_parent_pid,
        )
        pid = os.fork()
        if pid == 0:
            try:
                _arm_runner_parent_death(expected_parent_pid)
                os.setsid()
                _seal_exact_runner_process(
                    guard_descriptor=guard_descriptor,
                    stdout_descriptor=stdout_write,
                    stderr_descriptor=stderr_write,
                    mode=mode,
                )
                arguments = _runner_argv(inputs, mode)
                runtime_python = arguments[0]
                os.execve(
                    runtime_python,
                    arguments,
                    runner_environment,
                )
            except BaseException:
                os._exit(127)
        for descriptor in (stdout_write, stderr_write):
            os.close(descriptor)
            owned.discard(descriptor)
        result = (pid, stdout_read, stderr_read)
        for descriptor in result[1:]:
            owned.discard(descriptor)
        return result
    except BaseException as error:
        cleanup_error: BaseException | None = None
        if pid > 0:
            try:
                _terminate_and_reap_exact_runner(pid)
            except BaseException as observed:
                cleanup_error = observed
        for descriptor in tuple(owned):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if cleanup_error is not None:
            raise cleanup_error from error
        if isinstance(error, Phase9ProofIssuerError):
            raise
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_runner_spawn_failed"
        ) from error


def _require_process_group_absent_after_reap(
    pid: int,
    *,
    deadline: float,
) -> None:
    """Prove post-reap group absence without signaling a reusable PGID."""

    if type(pid) is not int or pid <= 0 or type(deadline) is not float:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_runner_cleanup_failed"
        )
    while True:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        except OSError as error:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_runner_cleanup_failed"
            ) from error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_runner_cleanup_failed"
            )
        select.select(
            [],
            [],
            [],
            min(remaining, RUNNER_REAP_POLL_SECONDS),
        )


def _terminate_and_reap_exact_runner(pid: int) -> None:
    """Race-safely terminate one forked runner and bound exact-child reaping."""

    if type(pid) is not int or pid <= 0:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_runner_cleanup_failed"
        )
    deadline = time.monotonic() + RUNNER_REAP_TIMEOUT_SECONDS

    group_signal_error: OSError | None = None
    while True:
        try:
            os.killpg(pid, signal.SIGKILL)
            group_signal_error = None
        except ProcessLookupError:
            pass
        except OSError as error:
            group_signal_error = error
        try:
            waited, unused_status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            if group_signal_error is not None:
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_runner_cleanup_failed"
                ) from group_signal_error
            _require_process_group_absent_after_reap(
                pid,
                deadline=deadline,
            )
            return
        except OSError as error:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_runner_cleanup_failed"
            ) from error
        if waited == pid:
            if group_signal_error is not None:
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_runner_cleanup_failed"
                ) from group_signal_error
            _require_process_group_absent_after_reap(
                pid,
                deadline=deadline,
            )
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            if group_signal_error is not None:
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_runner_cleanup_failed"
                ) from group_signal_error
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_runner_cleanup_failed"
            ) from error
        try:
            os.killpg(pid, signal.SIGKILL)
            group_signal_error = None
        except ProcessLookupError:
            pass
        except OSError as error:
            group_signal_error = error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_runner_cleanup_failed"
            ) from group_signal_error
        select.select([], [], [], min(remaining, RUNNER_REAP_POLL_SECONDS))


def _supervise_attempt(
    *,
    inputs: runner.ProofInputs,
    guard_descriptor: int,
    mode: runner.RunnerMode = runner.RunnerMode.START_OR_RECOVER,
) -> tuple[int, bytes, bytes]:
    requires_manager_authority = mode is not runner.RunnerMode.RECOVER_ONLY
    if requires_manager_authority:
        _require_active_manager_authority()
        next_authority_recheck = (
            time.monotonic() + MANAGER_AUTHORITY_RECHECK_SECONDS
        )
    else:
        next_authority_recheck = float("inf")
    pid, stdout_fd, stderr_fd = _spawn_exact_runner(
        inputs,
        guard_descriptor,
        mode,
    )
    open_reads: set[int] = set()
    reads_tracked = False
    status: int | None = None
    try:
        open_reads.update((stdout_fd, stderr_fd))
        reads_tracked = True
        deadline = time.monotonic() + RUNNER_TIMEOUT_SECONDS
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        while status is None or open_reads:
            now = time.monotonic()
            if requires_manager_authority:
                _require_manager_control_health()
                if now >= next_authority_recheck:
                    _require_production_write_lease()
                    next_authority_recheck = (
                        now + MANAGER_AUTHORITY_RECHECK_SECONDS
                    )
            if now >= deadline:
                raise Phase9ProofIssuerError("phase9_proof_issuer_runner_timeout")
            if status is None:
                waited, observed_status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    status = observed_status
            ready, unused_write, unused_exception = select.select(
                sorted(open_reads), [], [], 0.05
            )
            for descriptor in ready:
                block = os.read(descriptor, 65536)
                if not block:
                    os.close(descriptor)
                    open_reads.discard(descriptor)
                    continue
                selected = (
                    stdout_buffer if descriptor == stdout_fd else stderr_buffer
                )
                selected.extend(block)
                maximum = (
                    MAX_RUNNER_OUTPUT_BYTES
                    if descriptor == stdout_fd
                    else 16 * 1024
                )
                if len(selected) > maximum:
                    raise Phase9ProofIssuerError(
                        "phase9_proof_issuer_output_too_large"
                    )
            if status is not None and not ready and open_reads:
                # An exited process must eventually close all exact pipes.
                continue
    except BaseException as error:
        cleanup_error: BaseException | None = None
        try:
            if status is None:
                _terminate_and_reap_exact_runner(pid)
            else:
                _require_process_group_absent_after_reap(
                    pid,
                    deadline=time.monotonic() + RUNNER_REAP_TIMEOUT_SECONDS,
                )
        except BaseException as observed:
            cleanup_error = observed
        if cleanup_error is not None:
            raise cleanup_error from error
        if isinstance(error, Phase9ProofIssuerError):
            raise
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_supervision_failed"
        ) from error
    finally:
        remaining_reads = (
            tuple(open_reads)
            if reads_tracked
            else (stdout_fd, stderr_fd)
        )
        for descriptor in remaining_reads:
            try:
                os.close(descriptor)
            except OSError:
                pass
    assert status is not None
    # The direct child was already reaped above. Its numeric PID/PGID can now
    # be reused, so never signal it. Require bounded signal-0 absence for every
    # status before accepting success or allowing any later RECOVER_ONLY path.
    _require_process_group_absent_after_reap(
        pid,
        deadline=time.monotonic() + RUNNER_REAP_TIMEOUT_SECONDS,
    )
    if requires_manager_authority:
        # Close the race between the final loop observation and any caller
        # publishing or consuming an effectful runner result.
        _require_active_manager_authority()
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status), bytes(stdout_buffer), bytes(stderr_buffer)
    return 128 + os.WTERMSIG(status), bytes(stdout_buffer), bytes(stderr_buffer)


def _strict_runner_error(status: int, stdout: bytes, stderr: bytes) -> str:
    if (
        status != 1
        or stdout != b""
        or not stderr.endswith(b"\n")
        or stderr.count(b"\n") != 1
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_runner_output_invalid"
        )
    try:
        line = stderr[:-1].decode("ascii")
    except UnicodeError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_runner_output_invalid"
        ) from error
    if _ERROR_RE.fullmatch(line) is None:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_runner_output_invalid"
        )
    return line


def _strict_runner_receipt_payload(stdout: bytes, stderr: bytes) -> bytes:
    if (
        stderr != b""
        or not stdout.endswith(b"\n")
        or stdout.count(b"\n") != 1
        or len(stdout) == 1
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_runner_output_invalid"
        )
    return stdout[:-1]


_START_AUTHORITY_PAIR_RECEIPT_KEYS: Final = frozenset(
    {
        "result",
        "recovery_capsule_sha256",
        "recovery_capsule_device",
        "recovery_capsule_inode",
        "recovery_reservation_claim_sha256",
        "install_authority_claim_sha256",
        "start_authority_pair_claimed_atomically",
    }
)


def _run_start_authority_pair_mode(
    *,
    inputs: runner.ProofInputs,
    guard_descriptor: int,
    mode: runner.RunnerMode,
    expected_capsule_sha256: str,
    expected_inode: tuple[int, int],
) -> Mapping[str, object]:
    """Run and strictly bind one closed preclaim or reverify operation."""

    if mode not in {
        runner.RunnerMode.PRECLAIM_STAGED_START,
        runner.RunnerMode.VERIFY_PUBLISHED_START_PAIR,
    }:
        raise Phase9ProofIssuerError("phase9_proof_issuer_mode_invalid")
    status, stdout, stderr = _supervise_attempt(
        inputs=inputs,
        guard_descriptor=guard_descriptor,
        mode=mode,
    )
    if status != 0:
        raise Phase9ProofIssuerError(
            _strict_runner_error(status, stdout, stderr)
        )
    try:
        receipt = runner._parse_canonical_object(
            _strict_runner_receipt_payload(stdout, stderr),
            "phase9_proof_issuer_start_authority_receipt_invalid",
        )
    except runner.LiveProofError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_start_authority_receipt_invalid"
        ) from error
    allowed_results = (
        {"nonce_claimed", "exact_execution_resumed"}
        if mode is runner.RunnerMode.PRECLAIM_STAGED_START
        else {"exact_execution_resumed"}
    )
    if (
        set(receipt) != _START_AUTHORITY_PAIR_RECEIPT_KEYS
        or receipt.get("result") not in allowed_results
        or receipt.get("recovery_capsule_sha256")
        != expected_capsule_sha256
        or receipt.get("recovery_capsule_device") != expected_inode[0]
        or receipt.get("recovery_capsule_inode") != expected_inode[1]
        or receipt.get("start_authority_pair_claimed_atomically") is not True
        or any(
            type(receipt.get(key)) is not str
            or runner._HASH_RE.fullmatch(str(receipt[key])) is None
            for key in (
                "recovery_reservation_claim_sha256",
                "install_authority_claim_sha256",
            )
        )
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_start_authority_receipt_invalid"
        )
    return MappingProxyType(receipt)


def _ensure_live_proof_guard_parent() -> None:
    """Create or reopen the one fixed root-owned guard directory."""

    parent = runner.LIVE_PROOF_GUARD_PATH.parent.parent
    leaf = runner.LIVE_PROOF_GUARD_PATH.parent.name
    parent_fd = leaf_fd = -1
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        parent_fd = os.open(parent, flags)
        parent_opened = os.fstat(parent_fd)
        parent_named = parent.stat(follow_symlinks=False)
        parent_mode = stat.S_IMODE(parent_opened.st_mode)
        if (
            not stat.S_ISDIR(parent_opened.st_mode)
            or parent_opened.st_uid != 0
            or parent_opened.st_gid != 0
            or (parent_opened.st_dev, parent_opened.st_ino)
            != (parent_named.st_dev, parent_named.st_ino)
            or (
                parent_mode & 0o022
                and not parent_opened.st_mode & stat.S_ISVTX
            )
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_guard_parent_invalid"
            )
        try:
            _require_active_manager_authority()
            os.mkdir(leaf, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            created = False
        else:
            created = True
            os.chown(
                leaf,
                0,
                0,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.chmod(
                leaf,
                0o700,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.fsync(parent_fd)
        leaf_fd = os.open(leaf, flags, dir_fd=parent_fd)
        opened = os.fstat(leaf_fd)
        named = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        parent_after = os.fstat(parent_fd)
        parent_named_after = parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o700
            or opened.st_uid != 0
            or opened.st_gid != 0
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or (parent_after.st_dev, parent_after.st_ino)
            != (parent_opened.st_dev, parent_opened.st_ino)
            or (parent_named_after.st_dev, parent_named_after.st_ino)
            != (parent_opened.st_dev, parent_opened.st_ino)
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_guard_parent_invalid"
            )
        if created:
            os.fsync(leaf_fd)
    except Phase9ProofIssuerError:
        raise
    except OSError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_guard_parent_invalid"
        ) from error
    finally:
        if leaf_fd >= 0:
            os.close(leaf_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _verify_supervised_receipt(
    *,
    inputs: runner.ProofInputs,
    artifacts: Mapping[str, bytes],
    capsule_sha256: str,
    stdout: bytes,
    stderr: bytes,
) -> tuple[str, Mapping[str, object]]:
    """Verify one exact live or explicitly non-promotable state receipt."""

    try:
        receipt = runner._parse_canonical_object(
            _strict_runner_receipt_payload(stdout, stderr),
            "phase9_proof_issuer_receipt_invalid",
        )
        schema = receipt.get("schema_version")
        if schema == runner.LIVE_PROOF_RECEIPT_SCHEMA:
            kind = "live"
            verified = runner.verify_live_proof_receipt(receipt)
        elif schema == runner.RECOVERY_RECEIPT_SCHEMA:
            kind = "recovery"
            verified = runner.verify_recovery_receipt(receipt)
        elif schema == runner.PAIR_ONLY_RECEIPT_SCHEMA:
            kind = "pair_only"
            verified = runner.verify_pair_only_receipt(receipt)
        else:
            raise runner.LiveProofError(
                "phase9_live_proof_receipt_schema_invalid"
            )
    except runner.LiveProofError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_receipt_invalid"
        ) from error
    expected_bindings = {
        "candidate_git_commit": inputs.candidate_git_commit,
        "candidate_git_tree": inputs.candidate_git_tree,
        "package_manifest_sha256": inputs.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            inputs.controller_runtime_receipt_sha256
        ),
    }
    if (
        verified.get("recovery_capsule_sha256") != capsule_sha256
        or any(
            verified.get(key) != value
            for key, value in expected_bindings.items()
        )
        or (
            kind == "live"
            and (
                type(artifacts.get(runner.LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE))
                is not bytes
                or verified.get("live_proof_receipt_schema_sha256")
                != _sha(artifacts[runner.LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE])
                or verified.get("authorization_text_sha256")
                != runner.AUTHORIZED_TEXT_SHA256
            )
        )
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_receipt_binding_invalid"
        )
    return kind, MappingProxyType(dict(verified))


def _require_production_staged_prefix_disposition(
    *,
    inputs: runner.ProofInputs,
) -> Mapping[str, object]:
    """Require the exact failed-prefix fence before 000003 can mutate state."""

    try:
        paths = staged_prefix_disposition.production_disposition_paths()
        expectation = _production_staged_prefix_expectation(inputs)
        receipt = staged_prefix_disposition.verify_staged_prefix_tombstone(
            paths,
            expectation,
        )
        successor_identity = (
            staged_prefix_disposition.production_corrected_attempt_identity_sha256(
                package_manifest_sha256=inputs.package_manifest_sha256,
                controller_runtime_receipt_sha256=(
                    inputs.controller_runtime_receipt_sha256
                ),
            )
        )
    except staged_prefix_disposition.StagedPrefixDispositionError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_staged_prefix_disposition_required"
        ) from error
    if (
        not isinstance(receipt, MappingABC)
        or receipt.get("schema_version")
        != staged_prefix_disposition.TOMBSTONE_SCHEMA
        or receipt.get("result") != staged_prefix_disposition.RESULT
        or receipt.get("contract_sha256")
        != staged_prefix_disposition.PRODUCTION_CONTRACT_SHA256
        or receipt.get("failed_prefix_identity_sha256")
        != staged_prefix_disposition.production_failed_prefix_identity_sha256()
        or receipt.get("corrected_attempt_identity_sha256")
        != successor_identity
        or receipt.get("corrected_generation")
        != staged_prefix_disposition.PRODUCTION_CORRECTED_GENERATION
        or receipt.get("failed_evidence_preserved_in_place") is not True
        or receipt.get("no_store_or_service_effects_proven") is not True
        or receipt.get("deletion_performed") is not False
        or receipt.get("provider_calls") != 0
        or receipt.get("production_data_read") is not False
        or receipt.get("activation_performed") is not False
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_staged_prefix_disposition_required"
        )
    return receipt


def _remove_exact_staged_capsule_after_pristine_proof(
    *,
    inputs: runner.ProofInputs,
    expected_capsule_sha256: str,
    expected_inode: tuple[int, int],
) -> None:
    _require_pristine_staged_cleanup_state(inputs=inputs)
    parent_fd = _ensure_recovery_capsule_parent()
    try:
        raw, staged = _read_capsule_member(
            parent_fd,
            _recovery_capsule_temp_name(),
            allowed_link_counts=frozenset({1}),
            allow_empty=True,
        )
        if (
            _sha(raw) != expected_capsule_sha256
            or (staged.st_dev, staged.st_ino) != expected_inode
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_capsule_publication_drift"
            )
        _require_active_manager_authority()
        _unlink_exact_member(
            parent_fd,
            _recovery_capsule_temp_name(),
            expected_inode,
        )
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def _prepare_preclaim_publish_new_capsule(
    *,
    inputs: runner.ProofInputs,
    artifacts: Mapping[str, bytes],
    guard_descriptor: int,
) -> tuple[CapsulePublicationObservation, Mapping[str, object]]:
    private_key = Ed25519PrivateKey.generate()
    capsule = build_exact_recovery_capsule(
        inputs=inputs,
        artifacts=dict(artifacts),
        private_key=private_key,
        issued_at=datetime.now(timezone.utc),
    )
    verify_exact_clean_candidate(inputs)
    capsule_sha256, capsule_inode = prepare_fixed_recovery_capsule(capsule)
    # Nothing reachable by the sealed runner retains signing authority.
    del capsule
    del private_key
    try:
        prior_success = (
            durable_live_proof_receipt.preflight_anonymous_publication_capability(
                inputs=inputs,
                artifacts=artifacts,
                capsule_sha256=capsule_sha256,
            )
        )
    except durable_live_proof_receipt.DurableLiveProofReceiptError as error:
        _remove_exact_staged_capsule_after_pristine_proof(
            inputs=inputs,
            expected_capsule_sha256=capsule_sha256,
            expected_inode=capsule_inode,
        )
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_durable_receipt_preflight_failed"
        ) from error
    if prior_success is not None:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_durable_receipt_without_published_capsule"
        )
    pair = _run_start_authority_pair_mode(
        inputs=inputs,
        guard_descriptor=guard_descriptor,
        mode=runner.RunnerMode.PRECLAIM_STAGED_START,
        expected_capsule_sha256=capsule_sha256,
        expected_inode=capsule_inode,
    )
    commit_preclaimed_recovery_capsule(
        expected_capsule_sha256=capsule_sha256,
        expected_inode=capsule_inode,
    )
    verified_pair = _run_start_authority_pair_mode(
        inputs=inputs,
        guard_descriptor=guard_descriptor,
        mode=runner.RunnerMode.VERIFY_PUBLISHED_START_PAIR,
        expected_capsule_sha256=capsule_sha256,
        expected_inode=capsule_inode,
    )
    if any(
        verified_pair.get(key) != pair.get(key)
        for key in (
            "recovery_reservation_claim_sha256",
            "install_authority_claim_sha256",
        )
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_start_authority_receipt_invalid"
        )
    try:
        verified_capsule = runner._load_verified_recovery_capsule(
            inputs,
            artifacts,
            require_current=False,
        )
    except runner.LiveProofError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_recovery_capsule_invalid"
        ) from error
    if _sha(verified_capsule.raw) != capsule_sha256:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_capsule_publication_drift"
        )
    return (
        CapsulePublicationObservation(
            state="published",
            capsule=verified_capsule,
            capsule_sha256=capsule_sha256,
            inode=capsule_inode,
        ),
        verified_pair,
    )


def _adopt_durable_success_after_fresh_absence_recheck(
    *,
    inputs: runner.ProofInputs,
    artifacts: Mapping[str, bytes],
    capsule_sha256: str,
    durable_receipt: Mapping[str, object],
    guard_descriptor: int,
) -> Mapping[str, object]:
    status, stdout, stderr = _supervise_attempt(
        inputs=inputs,
        guard_descriptor=guard_descriptor,
        mode=runner.RunnerMode.RECOVER_ONLY,
    )
    if status != 0:
        raise Phase9ProofIssuerError(
            _strict_runner_error(status, stdout, stderr)
        )
    kind, recovery = _verify_supervised_receipt(
        inputs=inputs,
        artifacts=artifacts,
        capsule_sha256=capsule_sha256,
        stdout=stdout,
        stderr=stderr,
    )
    exact_bindings = (
        "candidate_git_commit",
        "candidate_git_tree",
        "package_manifest_sha256",
        "controller_runtime_receipt_sha256",
        "recovery_capsule_sha256",
        "recovery_reservation_claim_sha256",
        "install_authority_claim_sha256",
        "installation_execution_id",
        "installation_receipt_sha256",
        "empty_rollback_execution_id",
        "empty_rollback_receipt_sha256",
    )
    if (
        kind != "recovery"
        or recovery.get("exact_resources_absent") is not True
        or recovery.get("stores_installed") is not False
        or recovery.get("stores_supervisor_installed") is not False
        or recovery.get("start_authority_pair_claimed_atomically") is not True
        or any(
            recovery.get(key) != durable_receipt.get(key)
            for key in exact_bindings
        )
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_durable_receipt_adoption_invalid"
        )
    return MappingProxyType(dict(durable_receipt))


def issue_and_supervise(inputs: runner.ProofInputs) -> Mapping[str, object]:
    """Issue exact authority and supervise one bounded exact runner."""

    _require_closed_issuer_runtime()
    if os.geteuid() != 0:
        raise Phase9ProofIssuerError("phase9_proof_issuer_root_required")
    _require_active_manager_authority()
    verify_exact_clean_candidate(inputs)
    try:
        unused_manifest, artifacts = runner._load_release(inputs)
    except runner.LiveProofError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_release_invalid"
        ) from error
    if runner.DURABLE_PRE_EFFECT_ROLLBACK_AUTHORITY_PACKAGED is not True:
        # Refuse unless the sealed runner can claim and reverify a durable
        # public recovery reservation before any install worker.
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_durable_recovery_authority_required"
        )
    _require_active_manager_authority()
    _ensure_live_proof_guard_parent()
    _require_active_manager_authority()
    try:
        guard = GlobalExecutionLock(
            runner.LIVE_PROOF_GUARD_PATH,
            expected_uid=0,
            expected_gid=0,
        )
    except ExecutionLockError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_guard_unavailable"
        ) from error
    guard.retain_across_inherited_processes()
    with guard:
        _require_active_manager_authority()
        verify_exact_clean_candidate(inputs)
        _require_production_staged_prefix_disposition(inputs=inputs)
        verify_exact_clean_candidate(inputs)
        _require_active_manager_authority()
        try:
            substrate_bootstrap.bootstrap_phase9_disposable_store_substrate(
                held_lock=guard.held_capability()
            )
        except substrate_bootstrap.Phase9DisposableStoreSubstrateError as error:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_substrate_bootstrap_failed"
            ) from error
        _require_active_manager_authority()
        try:
            runner._prepare_fixed_substrate()
        except runner.LiveProofError as error:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_substrate_invalid"
            ) from error
        _require_active_manager_authority()
        try:
            observation = reconcile_capsule_publication(
                inputs=inputs,
                artifacts=artifacts,
            )
        except runner.LiveProofError as error:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_capsule_invalid"
            ) from error
        if observation is None:
            observation, start_pair = _prepare_preclaim_publish_new_capsule(
                inputs=inputs,
                artifacts=artifacts,
                guard_descriptor=guard.descriptor,
            )
        elif observation.state == "staged":
            try:
                prior_success = (
                    durable_live_proof_receipt.preflight_anonymous_publication_capability(
                        inputs=inputs,
                        artifacts=artifacts,
                        capsule_sha256=observation.capsule_sha256,
                    )
                )
            except durable_live_proof_receipt.DurableLiveProofReceiptError as error:
                _remove_exact_staged_capsule_after_pristine_proof(
                    inputs=inputs,
                    expected_capsule_sha256=(
                        observation.capsule_sha256
                    ),
                    expected_inode=observation.inode,
                )
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_durable_receipt_preflight_failed"
                ) from error
            if prior_success is not None:
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_durable_receipt_without_published_capsule"
                )
            try:
                start_pair = _run_start_authority_pair_mode(
                    inputs=inputs,
                    guard_descriptor=guard.descriptor,
                    mode=runner.RunnerMode.PRECLAIM_STAGED_START,
                    expected_capsule_sha256=(
                        observation.capsule_sha256
                    ),
                    expected_inode=observation.inode,
                )
            except Phase9ProofIssuerError as error:
                if str(error) != (
                    "phase9_live_proof_start_authority_pair_expired"
                ):
                    raise
                _remove_exact_staged_capsule_after_pristine_proof(
                    inputs=inputs,
                    expected_capsule_sha256=(
                        observation.capsule_sha256
                    ),
                    expected_inode=observation.inode,
                )
                observation, start_pair = (
                    _prepare_preclaim_publish_new_capsule(
                        inputs=inputs,
                        artifacts=artifacts,
                        guard_descriptor=guard.descriptor,
                    )
                )
            else:
                commit_preclaimed_recovery_capsule(
                    expected_capsule_sha256=(
                        observation.capsule_sha256
                    ),
                    expected_inode=observation.inode,
                )
                published_pair = _run_start_authority_pair_mode(
                    inputs=inputs,
                    guard_descriptor=guard.descriptor,
                    mode=runner.RunnerMode.VERIFY_PUBLISHED_START_PAIR,
                    expected_capsule_sha256=(
                        observation.capsule_sha256
                    ),
                    expected_inode=observation.inode,
                )
                if any(
                    published_pair.get(key) != start_pair.get(key)
                    for key in (
                        "recovery_reservation_claim_sha256",
                        "install_authority_claim_sha256",
                    )
                ):
                    raise Phase9ProofIssuerError(
                        "phase9_proof_issuer_start_authority_receipt_invalid"
                    )
                observation = CapsulePublicationObservation(
                    state="published",
                    capsule=observation.capsule,
                    capsule_sha256=observation.capsule_sha256,
                    inode=observation.inode,
                )
        elif observation.state in {"linked", "published"}:
            start_pair = _run_start_authority_pair_mode(
                inputs=inputs,
                guard_descriptor=guard.descriptor,
                mode=runner.RunnerMode.VERIFY_PUBLISHED_START_PAIR,
                expected_capsule_sha256=observation.capsule_sha256,
                expected_inode=observation.inode,
            )
            if observation.state == "linked":
                complete_linked_capsule_publication(
                    expected_capsule_sha256=(
                        observation.capsule_sha256
                    ),
                    expected_inode=observation.inode,
                )
                observation = CapsulePublicationObservation(
                    state="published",
                    capsule=observation.capsule,
                    capsule_sha256=observation.capsule_sha256,
                    inode=observation.inode,
                )
        else:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_capsule_publication_drift"
            )

        capsule_sha256 = observation.capsule_sha256
        if (
            observation.state != "published"
            or start_pair.get("start_authority_pair_claimed_atomically")
            is not True
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_start_authority_receipt_invalid"
            )
        try:
            durable_success = (
                durable_live_proof_receipt.read_verified_promotable_live_receipt_if_present(
                inputs=inputs,
                artifacts=artifacts,
                capsule_sha256=capsule_sha256,
            )
            )
        except durable_live_proof_receipt.DurableLiveProofReceiptError as error:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_durable_receipt_invalid"
            ) from error
        if durable_success is not None:
            return _adopt_durable_success_after_fresh_absence_recheck(
                inputs=inputs,
                artifacts=artifacts,
                capsule_sha256=capsule_sha256,
                durable_receipt=durable_success,
                guard_descriptor=guard.descriptor,
            )

        recovery_required = False
        manager_authority_lost = False
        attempt_error: Phase9ProofIssuerError | None = None
        try:
            status, stdout, stderr = _supervise_attempt(
                inputs=inputs,
                guard_descriptor=guard.descriptor,
                mode=runner.RunnerMode.START_OR_RECOVER,
            )
        except Phase9ProofIssuerError as error:
            if str(error) == "phase9_proof_issuer_runner_cleanup_failed":
                # The prior process group may still be live and shares this
                # inherited flock open description. Never overlap recovery.
                raise
            if str(error) in _MANAGER_LOSS_CODES:
                manager_authority_lost = True
            attempt_error = error
            recovery_required = True
        else:
            if status == 0:
                try:
                    kind, verified = _verify_supervised_receipt(
                        inputs=inputs,
                        artifacts=artifacts,
                        capsule_sha256=capsule_sha256,
                        stdout=stdout,
                        stderr=stderr,
                    )
                except Phase9ProofIssuerError as error:
                    attempt_error = error
                    recovery_required = True
                else:
                    if kind == "live":
                        return verified
                    if kind == "recovery":
                        attempt_error = Phase9ProofIssuerError(
                        "phase9_proof_issuer_recovery_completed_live_proof_not_proven"
                        )
                    else:
                        attempt_error = Phase9ProofIssuerError(
                        "phase9_proof_issuer_recovery_receipt_invalid"
                        )
                    recovery_required = True
            else:
                # _supervise_attempt already killed and proved absence of the
                # failed process group. Preserve the original one-retry policy
                # after RECOVER_ONLY proves pair-only pristine state.
                recovery_required = True

        if not recovery_required:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_state_invalid"
            )
        # A killed runner may have durably published verified success before
        # stdout was observed.  Re-read that create-once evidence and require a
        # fresh exact-absence recovery before returning the original receipt.
        durable_read_error: Phase9ProofIssuerError | None = None
        try:
            durable_after_failure = (
                durable_live_proof_receipt.read_verified_promotable_live_receipt_if_present(
                    inputs=inputs,
                    artifacts=artifacts,
                    capsule_sha256=capsule_sha256,
                )
            )
        except durable_live_proof_receipt.DurableLiveProofReceiptError as error:
            durable_after_failure = None
            durable_read_error = Phase9ProofIssuerError(
                "phase9_proof_issuer_durable_receipt_invalid"
            )
            durable_read_error.__cause__ = error
        if durable_after_failure is not None:
            return _adopt_durable_success_after_fresh_absence_recheck(
                inputs=inputs,
                artifacts=artifacts,
                capsule_sha256=capsule_sha256,
                durable_receipt=durable_after_failure,
                guard_descriptor=guard.descriptor,
            )
        # The first exact process group is already killed and reaped on a
        # timeout, or exited nonzero.  Invoke the closed recovery mode once.
        status, stdout, stderr = _supervise_attempt(
            inputs=inputs,
            guard_descriptor=guard.descriptor,
            mode=runner.RunnerMode.RECOVER_ONLY,
        )
        if status != 0:
            raise Phase9ProofIssuerError(
                _strict_runner_error(status, stdout, stderr)
            )
        kind, verified = _verify_supervised_receipt(
            inputs=inputs,
            artifacts=artifacts,
            capsule_sha256=capsule_sha256,
            stdout=stdout,
            stderr=stderr,
        )
        if kind == "live":
            return verified
        if kind == "recovery":
            # Cleanup evidence is deliberately non-promotable unless both
            # durable commanded-death arms allow exact reconstruction.
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_completed_live_proof_not_proven"
            )
        if kind != "pair_only":
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_receipt_invalid"
            )
        if manager_authority_lost:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_authority_lost_before_install_effect"
            )
        if durable_read_error is not None:
            raise durable_read_error
        if (
            attempt_error is not None
            and str(attempt_error) != "phase9_proof_issuer_runner_timeout"
        ):
            raise attempt_error

        # The first runner died after atomically claiming its exact pair but
        # before creating the expected execution directory.  Retry that same
        # pair once; never mint another claim and never loop.
        retry_error: Phase9ProofIssuerError | None = None
        try:
            retry_status, retry_stdout, retry_stderr = _supervise_attempt(
                inputs=inputs,
                guard_descriptor=guard.descriptor,
                mode=runner.RunnerMode.START_OR_RECOVER,
            )
        except Phase9ProofIssuerError as error:
            if str(error) == "phase9_proof_issuer_runner_cleanup_failed":
                raise
            if str(error) in _MANAGER_LOSS_CODES:
                manager_authority_lost = True
            retry_error = error
            retry_status = -1
            retry_stdout = b""
            retry_stderr = b""
        if retry_status == 0:
            try:
                retry_kind, retry_verified = _verify_supervised_receipt(
                    inputs=inputs,
                    artifacts=artifacts,
                    capsule_sha256=capsule_sha256,
                    stdout=retry_stdout,
                    stderr=retry_stderr,
                )
            except Phase9ProofIssuerError as error:
                retry_error = error
            else:
                if retry_kind == "live":
                    return retry_verified
                if retry_kind == "recovery":
                    retry_error = Phase9ProofIssuerError(
                    "phase9_proof_issuer_recovery_completed_live_proof_not_proven"
                    )
                else:
                    retry_error = Phase9ProofIssuerError(
                        "phase9_proof_issuer_recovery_receipt_invalid"
                    )
        # A retry may have published success immediately before losing stdout.
        retry_durable_read_error: Phase9ProofIssuerError | None = None
        try:
            durable_after_retry = (
                durable_live_proof_receipt.read_verified_promotable_live_receipt_if_present(
                    inputs=inputs,
                    artifacts=artifacts,
                    capsule_sha256=capsule_sha256,
                )
            )
        except durable_live_proof_receipt.DurableLiveProofReceiptError as error:
            durable_after_retry = None
            retry_durable_read_error = Phase9ProofIssuerError(
                "phase9_proof_issuer_durable_receipt_invalid"
            )
            retry_durable_read_error.__cause__ = error
        if durable_after_retry is not None:
            return _adopt_durable_success_after_fresh_absence_recheck(
                inputs=inputs,
                artifacts=artifacts,
                capsule_sha256=capsule_sha256,
                durable_receipt=durable_after_retry,
                guard_descriptor=guard.descriptor,
            )

        status, stdout, stderr = _supervise_attempt(
            inputs=inputs,
            guard_descriptor=guard.descriptor,
            mode=runner.RunnerMode.RECOVER_ONLY,
        )
        if status != 0:
            raise Phase9ProofIssuerError(
                _strict_runner_error(status, stdout, stderr)
            )
        final_kind, final_verified = _verify_supervised_receipt(
            inputs=inputs,
            artifacts=artifacts,
            capsule_sha256=capsule_sha256,
            stdout=stdout,
            stderr=stderr,
        )
        if final_kind == "live":
            return final_verified
        if final_kind == "recovery":
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_recovery_completed_live_proof_not_proven"
            )
        if final_kind == "pair_only":
            if manager_authority_lost:
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_authority_lost_before_install_effect"
                )
            if retry_durable_read_error is not None:
                raise retry_durable_read_error
            if retry_error is not None:
                raise retry_error
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_start_not_observed_pair_only_pristine"
            )
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_recovery_receipt_invalid"
        )


def _proof_inputs_from_verified_permit() -> runner.ProofInputs:
    """Derive the only permitted proof inputs; accept no operator identities."""

    try:
        candidate = phase9_permitted_candidate.require_exact_permitted_candidate(
            expected_thread_id=runner.THREAD_ID,
            expected_authorization_text_sha256=runner.AUTHORIZED_TEXT_SHA256,
        )
    except phase9_permitted_candidate.Phase9PermittedCandidateError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_candidate_permit_required"
        ) from error
    return runner.ProofInputs(
        candidate_git_commit=candidate.candidate_git_commit,
        candidate_git_tree=candidate.candidate_git_tree,
        package_manifest_sha256=candidate.package_manifest_sha256,
        controller_runtime_receipt_sha256=(
            candidate.controller_runtime_receipt_sha256
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    if tuple(sys.argv[1:] if argv is None else argv):
        raise Phase9ProofIssuerError("phase9_proof_issuer_arguments_refused")
    _require_active_manager_authority()
    inputs = _proof_inputs_from_verified_permit()
    receipt = issue_and_supervise(inputs)
    sys.stdout.buffer.write(_canonical(dict(receipt)) + b"\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Phase9ProofIssuerError as error:
        sys.stderr.write(str(error) + "\n")
        raise SystemExit(1)
    except runner.LiveProofError:
        sys.stderr.write("phase9_proof_issuer_runner_boundary_invalid\n")
        raise SystemExit(1)

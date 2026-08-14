#!/usr/bin/env python3
from __future__ import annotations

"""Execute the fixed lease-guarded Phase 9J disposable proof chain."""

import os
import subprocess
import sys


_PREIMPORT_CONTROLLER_RUNTIME_RECEIPT_SHA256 = (
    "9dda4d93a1bcfecf4305736feffafb578e4629cd32594cec6b2d6d72cb90a4d3"
)
_PREIMPORT_CONTROLLER_PYTHON = (
    "/opt/governed-memory-controller/runtimes/"
    f"{_PREIMPORT_CONTROLLER_RUNTIME_RECEIPT_SHA256}/bin/python"
)
_PREIMPORT_GIT = "/usr/bin/git"
_PREIMPORT_EXPECTED_CANDIDATE_REF = (
    "refs/tags/governed-memory-phase9j-pre-effect-disposition-000004"
)
_PREIMPORT_CONTROLLER_RELATIVE = (
    "tools/governed_memory_validation/"
    "execute_phase9_disposable_live_proof_controller.py"
)
_PREIMPORT_SOURCE_PATHS = (
    "tools/governed_memory_validation/publish_phase9_staged_prefix_permit.py",
    "tools/governed_memory_validation/execute_phase9_staged_prefix_disposition.py",
    "tools/governed_memory_validation/staged_prefix_disposition.py",
    "tools/governed_memory_validation/"
    "generate_staged_prefix_disposition_contract.py",
    "tools/governed_memory_validation/phase9_permitted_candidate.py",
    "tools/governed_memory_validation/issue_disposable_installation_live_proof.py",
    "tools/governed_memory_validation/bootstrap_phase9_disposable_store_substrate.py",
    _PREIMPORT_CONTROLLER_RELATIVE,
    "ops/governed_memory/staged_prefix_disposition_contract.json",
)
_PREIMPORT_GIT_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
}
_ISOLATED_RUNTIME_AT_START = bool(sys.flags.isolated)
_DONT_WRITE_BYTECODE_AT_START = bool(sys.dont_write_bytecode)
if __name__ == "__main__" and not (
    _ISOLATED_RUNTIME_AT_START
    and _DONT_WRITE_BYTECODE_AT_START
    and sys.platform == "linux"
    and sys.executable == _PREIMPORT_CONTROLLER_PYTHON
):
    sys.stderr.write("phase9_proof_controller_runtime_isolation_required\n")
    raise SystemExit(1)


def _preimport_git(repository_root: str, *arguments: str) -> bytes:
    completed = subprocess.run(
        (
            _PREIMPORT_GIT,
            "-c",
            "safe.directory=" + repository_root,
            "-C",
            repository_root,
            *arguments,
        ),
        cwd="/",
        env=dict(_PREIMPORT_GIT_ENVIRONMENT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    if (
        completed.returncode != 0
        or completed.stderr != b""
        or len(completed.stdout) > 1024 * 1024
    ):
        raise RuntimeError("candidate")
    return bytes(completed.stdout)


def _preimport_git_object(repository_root: str, *arguments: str) -> str:
    raw = _preimport_git(repository_root, *arguments)
    try:
        value = raw.decode("ascii").rstrip("\n")
    except UnicodeError as error:
        raise RuntimeError("candidate") from error
    if (
        raw != (value + "\n").encode("ascii")
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RuntimeError("candidate")
    return value


def _preimport_exact_candidate_valid() -> bool:
    """Reprove the clean fixed tag before any repository module can load.

    This is a cooperative Phase-9-only boundary: the repository and Git object
    store remain writable by the authorized UID 1000 operator.  Exact tag,
    HEAD, status, tracking, and worktree-blob checks narrow that trust window;
    later permit and source rechecks remain mandatory because this is not
    kernel confinement against a malicious same-UID process.
    """

    try:
        controller_path = os.path.realpath(__file__)
        repository_root = os.path.dirname(
            os.path.dirname(os.path.dirname(controller_path))
        )
        if (
            os.path.relpath(controller_path, repository_root)
            != _PREIMPORT_CONTROLLER_RELATIVE
            or _preimport_git(repository_root, "rev-parse", "--show-toplevel")
            != (repository_root + "\n").encode("utf-8")
            or _preimport_git(
                repository_root,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            )
            != b""
        ):
            return False
        _preimport_git(
            repository_root,
            "ls-files",
            "--error-unmatch",
            "--stage",
            "--",
            *_PREIMPORT_SOURCE_PATHS,
        )
        tag_commit = _preimport_git_object(
            repository_root,
            "rev-parse",
            "--verify",
            _PREIMPORT_EXPECTED_CANDIDATE_REF + "^{commit}",
        )
        tag_tree = _preimport_git_object(
            repository_root,
            "rev-parse",
            "--verify",
            _PREIMPORT_EXPECTED_CANDIDATE_REF + "^{tree}",
        )
        if (
            _preimport_git_object(
                repository_root, "rev-parse", "--verify", "HEAD^{commit}"
            )
            != tag_commit
            or _preimport_git_object(
                repository_root, "rev-parse", "--verify", "HEAD^{tree}"
            )
            != tag_tree
        ):
            return False
        for relative in _PREIMPORT_SOURCE_PATHS:
            committed = _preimport_git_object(
                repository_root, "rev-parse", "HEAD:" + relative
            )
            observed = _preimport_git_object(
                repository_root,
                "hash-object",
                os.path.join(repository_root, relative),
            )
            if committed != observed:
                return False
        return bool(
            _preimport_git_object(
                repository_root,
                "rev-parse",
                "--verify",
                _PREIMPORT_EXPECTED_CANDIDATE_REF + "^{commit}",
            )
            == tag_commit
            and _preimport_git_object(
                repository_root,
                "rev-parse",
                "--verify",
                _PREIMPORT_EXPECTED_CANDIDATE_REF + "^{tree}",
            )
            == tag_tree
            and _preimport_git_object(
                repository_root, "rev-parse", "--verify", "HEAD^{commit}"
            )
            == tag_commit
            and _preimport_git_object(
                repository_root, "rev-parse", "--verify", "HEAD^{tree}"
            )
            == tag_tree
            and _preimport_git(
                repository_root,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            )
            == b""
        )
    except (OSError, subprocess.SubprocessError, RuntimeError, UnicodeError):
        return False


if __name__ == "__main__" and not _preimport_exact_candidate_valid():
    sys.stderr.write("phase9_proof_controller_candidate_preflight_required\n")
    raise SystemExit(1)

from collections.abc import Mapping, Sequence
import errno
import hashlib
import json
from pathlib import Path
import re
import signal
import time
from typing import Final


_CONTROLLER_PATH: Final = Path(__file__).resolve(strict=True)
_REPOSITORY_ROOT: Final = _CONTROLLER_PATH.parents[2]
_CONTROLLER_RELATIVE: Final = _PREIMPORT_CONTROLLER_RELATIVE
if (
    _CONTROLLER_PATH.relative_to(_REPOSITORY_ROOT).as_posix()
    != _CONTROLLER_RELATIVE
):
    raise SystemExit("phase9_proof_controller_invocation_invalid")
sys.path.insert(0, str(_REPOSITORY_ROOT))

from tools.governed_memory_validation import (
    durable_live_proof_receipt,
)
from tools.governed_memory_validation import (
    execute_phase9_staged_prefix_disposition as disposition_entrypoint,
)
from tools.governed_memory_validation import phase9_permitted_candidate
from tools.governed_memory_validation import staged_prefix_disposition
from tools.governed_memory_validation import (
    publish_phase9_staged_prefix_permit as permit_publisher,
)
from tools.governed_memory_validation import (
    run_disposable_installation_live_proof as runner,
)


LEASE_GUARD_PYTHON: Final = "/usr/bin/python3.12"
CONTROLLER_RUNTIME_RECEIPT_SHA256: Final = (
    _PREIMPORT_CONTROLLER_RUNTIME_RECEIPT_SHA256
)
CONTROLLER_PYTHON: Final = _PREIMPORT_CONTROLLER_PYTHON
LEASE_GUARD: Final = (
    "/var/lib/chat-memory-change-leases-v1/control/bin/"
    "chat_memory_lease_guard.py"
)
LEASE_GUARD_UID: Final = 1000
LEASE_GUARD_GID: Final = 1000
PUBLISHER_RELATIVE: Final = (
    "tools/governed_memory_validation/publish_phase9_staged_prefix_permit.py"
)
DISPOSITION_RELATIVE: Final = (
    "tools/governed_memory_validation/execute_phase9_staged_prefix_disposition.py"
)
ISSUER_RELATIVE: Final = (
    "tools/governed_memory_validation/"
    "issue_disposable_installation_live_proof.py"
)
MAX_STEP_OUTPUT_BYTES: Final = 1024 * 1024
MAX_STEP_ERROR_BYTES: Final = 16 * 1024
LEASE_RECHECK_SECONDS: Final = 30.0
PROCESS_GROUP_REAP_SECONDS: Final = 10.0
ISSUER_CONTROLLED_RECOVERY_SECONDS: Final = 30 * 60
ISSUER_TIMEOUT_SECONDS: Final = 3 * 60 * 60
ISSUER_CONTROL_FD: Final = 198
MANAGER_PID_ENVIRONMENT_KEY: Final = "GOVERNED_MEMORY_PHASE9_MANAGER_PID"
ROOT_UID: Final = 0
ROOT_GID: Final = 0
RESULT_SCHEMA: Final = "governed-memory-phase9j-live-proof-controller-v1"
_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z", re.ASCII)
_STEP_RELATIVES: Final = frozenset(
    {PUBLISHER_RELATIVE, DISPOSITION_RELATIVE, ISSUER_RELATIVE}
)
_SAFE_ENVIRONMENT: Final = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
}


class Phase9DisposableLiveProofControllerError(RuntimeError):
    """Content-free refusal from the exact production controller."""


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
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_document_invalid"
        ) from error


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate")
        value[key] = item
    return value


def _strict_document(raw: bytes, code: str) -> dict[str, object]:
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise Phase9DisposableLiveProofControllerError(code)
    payload = raw[:-1]
    try:
        value = json.loads(
            payload.decode("ascii"), object_pairs_hook=_unique_object
        )
    except (ValueError, UnicodeError) as error:
        raise Phase9DisposableLiveProofControllerError(code) from error
    if type(value) is not dict or _canonical(value) != payload:
        raise Phase9DisposableLiveProofControllerError(code)
    return value


def _lease_environment(environment: Mapping[str, str]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for key in ("CHAT_MEMORY_LEASE_ID", "CODEX_TASK_ID", "CODEX_THREAD_ID"):
        value = environment.get(key)
        if type(value) is not str or _ID_RE.fullmatch(value) is None:
            raise Phase9DisposableLiveProofControllerError(
                "phase9_proof_controller_lease_identity_invalid"
            )
        selected[key] = value
    return selected


def _child_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        **_SAFE_ENVIRONMENT,
        **_lease_environment(environment),
        MANAGER_PID_ENVIRONMENT_KEY: str(os.getpid()),
    }


def _require_production_write_lease(
    environment: Mapping[str, str],
    *,
    command_runner: object = subprocess,
) -> None:
    selected = _lease_environment(environment)
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
                str(_REPOSITORY_ROOT),
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
    except (OSError, subprocess.SubprocessError) as error:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_lease_denied"
        ) from error
    if (
        completed.returncode != 0
        or completed.stdout != b""
        or completed.stderr != b""
    ):
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_lease_denied"
        )


def _kill_and_reap_process_group(process: object) -> None:
    """Kill the exact new session and reap its direct child."""

    pid = getattr(process, "pid", None)
    if type(pid) is not int or pid <= 1:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_step_cleanup_failed"
        )
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as error:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_step_cleanup_failed"
        ) from error
    try:
        process.communicate(timeout=PROCESS_GROUP_REAP_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_step_cleanup_failed"
        ) from error
    if getattr(process, "returncode", None) is None:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_step_cleanup_failed"
        )


def _close_descriptor(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError as error:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_step_cleanup_failed"
        ) from error


def _open_issuer_control_pipe() -> int:
    """Install one fresh read-only pipe endpoint at inherited FD 198."""

    try:
        os.fstat(ISSUER_CONTROL_FD)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise Phase9DisposableLiveProofControllerError(
                "phase9_proof_controller_control_pipe_failed"
            ) from error
    else:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_control_fd_busy"
        )

    read_descriptor = -1
    write_descriptor = -1
    control_descriptor_owned = False
    try:
        read_descriptor, write_descriptor = os.pipe2(os.O_CLOEXEC)
        control_descriptor_owned = (
            read_descriptor == ISSUER_CONTROL_FD
            or write_descriptor == ISSUER_CONTROL_FD
        )
        if write_descriptor == ISSUER_CONTROL_FD:
            replacement = os.dup(write_descriptor)
            os.close(write_descriptor)
            write_descriptor = replacement
            control_descriptor_owned = False
        if read_descriptor == ISSUER_CONTROL_FD:
            os.set_inheritable(ISSUER_CONTROL_FD, True)
        else:
            os.dup2(read_descriptor, ISSUER_CONTROL_FD, inheritable=True)
            control_descriptor_owned = True
            os.close(read_descriptor)
            read_descriptor = -1
        return write_descriptor
    except (OSError, AttributeError) as error:
        for descriptor in (read_descriptor, write_descriptor):
            if descriptor >= 0 and descriptor != ISSUER_CONTROL_FD:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if control_descriptor_owned:
            try:
                os.close(ISSUER_CONTROL_FD)
            except OSError:
                pass
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_control_pipe_failed"
        ) from error


def _wait_for_controlled_issuer_recovery(
    process: object,
    write_descriptor: int,
) -> None:
    """Withdraw authority, resume a stopped issuer, then await its recovery."""

    close_error: BaseException | None = None
    try:
        _close_descriptor(write_descriptor)
    except BaseException as error:
        close_error = error
    try:
        process.send_signal(signal.SIGCONT)
    except ProcessLookupError:
        pass
    except (OSError, subprocess.SubprocessError, AttributeError) as error:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_step_cleanup_failed"
        ) from error
    try:
        process.communicate(timeout=ISSUER_CONTROLLED_RECOVERY_SECONDS)
    except subprocess.TimeoutExpired:
        # The issuer owns RECOVER_ONLY and retains the global execution lock.
        # A manager wait bound is not authority to interrupt that recovery.
        if close_error is not None:
            raise Phase9DisposableLiveProofControllerError(
                "phase9_proof_controller_step_cleanup_failed"
            ) from close_error
        return
    except (OSError, subprocess.SubprocessError) as error:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_step_cleanup_failed"
        ) from error
    if getattr(process, "returncode", None) is None:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_step_cleanup_failed"
        )
    if close_error is not None:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_step_cleanup_failed"
        ) from close_error


def _partial_output_is_oversized(error: subprocess.TimeoutExpired) -> bool:
    stdout = error.output if type(error.output) is bytes else b""
    stderr = error.stderr if type(error.stderr) is bytes else b""
    return (
        len(stdout) > MAX_STEP_OUTPUT_BYTES
        or len(stderr) > MAX_STEP_ERROR_BYTES
    )


def _run_step(
    relative: str,
    arguments: tuple[str, ...],
    *,
    timeout: int,
    environment: Mapping[str, str],
    command_runner: object = subprocess,
) -> dict[str, object]:
    if (
        relative not in _STEP_RELATIVES
        or type(arguments) is not tuple
        or arguments != ()
        or type(timeout) is not int
        or timeout <= 0
        or timeout > ISSUER_TIMEOUT_SECONDS
    ):
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_step_invalid"
        )
    _require_production_write_lease(
        environment,
        command_runner=command_runner,
    )
    script = _REPOSITORY_ROOT / relative
    issuer_controlled = relative == ISSUER_RELATIVE
    control_write_descriptor = -1
    if issuer_controlled:
        control_write_descriptor = _open_issuer_control_pipe()
    options: dict[str, object] = {
        "cwd": "/",
        "env": _child_environment(environment),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "close_fds": True,
        "start_new_session": True,
    }
    if issuer_controlled:
        options["pass_fds"] = (ISSUER_CONTROL_FD,)
    try:
        process = command_runner.Popen(
            (CONTROLLER_PYTHON, "-I", "-B", str(script), *arguments),
            **options,
        )
    except BaseException as error:
        cleanup_error: BaseException | None = None
        for descriptor in (
            ISSUER_CONTROL_FD if issuer_controlled else -1,
            control_write_descriptor,
        ):
            if descriptor >= 0:
                try:
                    _close_descriptor(descriptor)
                except BaseException as observed:
                    cleanup_error = observed
        if cleanup_error is not None:
            raise cleanup_error from error
        if isinstance(
            error, (OSError, subprocess.SubprocessError, AttributeError)
        ):
            raise Phase9DisposableLiveProofControllerError(
                "phase9_proof_controller_step_failed"
            ) from error
        raise
    if issuer_controlled:
        try:
            _close_descriptor(ISSUER_CONTROL_FD)
        except BaseException as error:
            owned_write_descriptor = control_write_descriptor
            control_write_descriptor = -1
            _wait_for_controlled_issuer_recovery(
                process, owned_write_descriptor
            )
            raise error

    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Phase9DisposableLiveProofControllerError(
                    "phase9_proof_controller_step_timeout"
                )
            try:
                stdout, stderr = process.communicate(
                    timeout=min(LEASE_RECHECK_SECONDS, remaining)
                )
            except subprocess.TimeoutExpired as error:
                if _partial_output_is_oversized(error):
                    raise Phase9DisposableLiveProofControllerError(
                        "phase9_proof_controller_step_failed"
                    ) from error
                _require_production_write_lease(
                    environment,
                    command_runner=command_runner,
                )
                continue
            except (OSError, subprocess.SubprocessError) as error:
                raise Phase9DisposableLiveProofControllerError(
                    "phase9_proof_controller_step_failed"
                ) from error
            break
        if issuer_controlled:
            _close_descriptor(control_write_descriptor)
            control_write_descriptor = -1
    except BaseException as error:
        try:
            if issuer_controlled:
                owned_write_descriptor = control_write_descriptor
                control_write_descriptor = -1
                _wait_for_controlled_issuer_recovery(
                    process, owned_write_descriptor
                )
            elif getattr(process, "returncode", None) is None:
                _kill_and_reap_process_group(process)
        except BaseException as cleanup_error:
            raise cleanup_error from error
        raise
    _require_production_write_lease(
        environment,
        command_runner=command_runner,
    )
    if (
        process.returncode != 0
        or type(stdout) is not bytes
        or type(stderr) is not bytes
        or stderr != b""
        or len(stdout) > MAX_STEP_OUTPUT_BYTES
    ):
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_step_failed"
        )
    return _strict_document(
        stdout,
        "phase9_proof_controller_step_receipt_invalid",
    )


def _require_disposition(
    candidate: phase9_permitted_candidate.PermittedCandidateAuthority,
) -> Mapping[str, object]:
    try:
        expectation = disposition_entrypoint._expectation(
            staged_prefix_disposition
        )
        receipt = staged_prefix_disposition.verify_staged_prefix_tombstone(
            staged_prefix_disposition.production_disposition_paths(),
            expectation,
        )
    except (
        disposition_entrypoint.Phase9StagedPrefixDispositionEntrypointError,
        staged_prefix_disposition.StagedPrefixDispositionError,
    ) as error:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_disposition_invalid"
        ) from error
    if (
        receipt is None
        or receipt.get("contract_sha256") != candidate.contract_sha256
        or receipt.get("corrected_attempt_identity_sha256")
        != candidate.successor_attempt_identity_sha256
        or receipt.get("corrected_generation")
        != disposition_entrypoint.CORRECTED_GENERATION
        or receipt.get("corrected_package_manifest_sha256")
        != candidate.package_manifest_sha256
        or receipt.get("corrected_controller_runtime_receipt_sha256")
        != candidate.controller_runtime_receipt_sha256
        or receipt.get("failed_evidence_preserved_in_place") is not True
        or receipt.get("no_store_or_service_effects_proven") is not True
        or receipt.get("deletion_performed") is not False
        or receipt.get("provider_calls") != 0
        or receipt.get("production_data_read") is not False
        or receipt.get("activation_performed") is not False
    ):
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_disposition_invalid"
        )
    return receipt


def execute_phase9_disposable_live_proof_controller(
    *,
    environment: Mapping[str, str] | None = None,
    command_runner: object = subprocess,
) -> Mapping[str, object]:
    if (
        not _ISOLATED_RUNTIME_AT_START
        or not _DONT_WRITE_BYTECODE_AT_START
        or sys.platform != "linux"
        or sys.executable != CONTROLLER_PYTHON
    ):
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_runtime_isolation_required"
        )
    if os.geteuid() != ROOT_UID or os.getegid() != ROOT_GID:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_root_required"
        )
    selected_environment = dict(os.environ if environment is None else environment)
    _lease_environment(selected_environment)
    _require_production_write_lease(
        selected_environment,
        command_runner=command_runner,
    )
    permit = _run_step(
        PUBLISHER_RELATIVE,
        (),
        timeout=120,
        environment=selected_environment,
        command_runner=command_runner,
    )
    try:
        candidate = phase9_permitted_candidate.require_exact_permitted_candidate()
    except phase9_permitted_candidate.Phase9PermittedCandidateError as error:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_permit_invalid"
        ) from error
    if (
        permit.get("permit_sha256") is None
        or permit.get("candidate_git_commit") != candidate.candidate_git_commit
        or permit.get("candidate_git_tree") != candidate.candidate_git_tree
        or permit.get("package_manifest_sha256")
        != candidate.package_manifest_sha256
        or permit.get("controller_runtime_receipt_sha256")
        != candidate.controller_runtime_receipt_sha256
    ):
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_permit_invalid"
        )

    disposition_step = _run_step(
        DISPOSITION_RELATIVE,
        (),
        timeout=180,
        environment=selected_environment,
        command_runner=command_runner,
    )
    disposition = _require_disposition(candidate)
    if any(
        disposition_step.get(key) != disposition.get(key)
        for key in (
            "contract_sha256",
            "corrected_controller_runtime_receipt_sha256",
            "corrected_generation",
            "corrected_package_manifest_sha256",
            "result",
            "tombstone_sha256",
        )
    ):
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_disposition_invalid"
        )

    proof_inputs = runner.ProofInputs(
        candidate_git_commit=candidate.candidate_git_commit,
        candidate_git_tree=candidate.candidate_git_tree,
        package_manifest_sha256=candidate.package_manifest_sha256,
        controller_runtime_receipt_sha256=(
            candidate.controller_runtime_receipt_sha256
        ),
    )
    live = _run_step(
        ISSUER_RELATIVE,
        (),
        timeout=ISSUER_TIMEOUT_SECONDS,
        environment=selected_environment,
        command_runner=command_runner,
    )
    try:
        verified_live = runner.verify_live_proof_receipt(live)
        unused_manifest, artifacts = runner._load_release(proof_inputs)
        durable = (
            durable_live_proof_receipt.read_verified_promotable_live_receipt_if_present(
                inputs=proof_inputs,
                artifacts=artifacts,
                capsule_sha256=str(verified_live["recovery_capsule_sha256"]),
            )
        )
    except (
        runner.LiveProofError,
        durable_live_proof_receipt.DurableLiveProofReceiptError,
        KeyError,
    ) as error:
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_live_receipt_invalid"
        ) from error
    if (
        durable is None
        or dict(durable) != dict(verified_live)
        or verified_live.get("candidate_git_commit")
        != candidate.candidate_git_commit
        or verified_live.get("candidate_git_tree")
        != candidate.candidate_git_tree
        or verified_live.get("package_manifest_sha256")
        != candidate.package_manifest_sha256
        or verified_live.get("controller_runtime_receipt_sha256")
        != candidate.controller_runtime_receipt_sha256
        or verified_live.get("exact_resources_absent_at_terminal_observation")
        is not True
        or verified_live.get("stores_installed_at_terminal_observation")
        is not False
        or verified_live.get(
            "stores_supervisor_installed_at_terminal_observation"
        )
        is not False
    ):
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_live_receipt_invalid"
        )
    _require_production_write_lease(
        selected_environment,
        command_runner=command_runner,
    )
    return {
        "schema_version": RESULT_SCHEMA,
        "result": "exact_disposable_live_proof_completed",
        "candidate_git_commit": candidate.candidate_git_commit,
        "candidate_git_tree": candidate.candidate_git_tree,
        "package_manifest_sha256": candidate.package_manifest_sha256,
        "controller_runtime_receipt_sha256": (
            candidate.controller_runtime_receipt_sha256
        ),
        "permit_sha256": permit["permit_sha256"],
        "staged_prefix_tombstone_sha256": disposition["tombstone_sha256"],
        "live_proof_receipt_sha256": verified_live["receipt_sha256"],
        "exact_resources_absent": True,
        "activation_performed": False,
        "provider_calls": 0,
        "production_data_read": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    if tuple(sys.argv[1:] if argv is None else argv):
        raise Phase9DisposableLiveProofControllerError(
            "phase9_proof_controller_arguments_refused"
        )
    result = execute_phase9_disposable_live_proof_controller()
    sys.stdout.buffer.write(_canonical(dict(result)) + b"\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Phase9DisposableLiveProofControllerError as error:
        sys.stderr.write(str(error) + "\n")
        raise SystemExit(1) from None
    except (
        permit_publisher.Phase9StagedPrefixPermitPublicationError,
        disposition_entrypoint.Phase9StagedPrefixDispositionEntrypointError,
        staged_prefix_disposition.StagedPrefixDispositionError,
        phase9_permitted_candidate.Phase9PermittedCandidateError,
        runner.LiveProofError,
        durable_live_proof_receipt.DurableLiveProofReceiptError,
    ):
        sys.stderr.write("phase9_proof_controller_boundary_invalid\n")
        raise SystemExit(1) from None

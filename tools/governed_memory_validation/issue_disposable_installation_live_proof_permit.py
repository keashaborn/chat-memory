#!/usr/bin/env python3
from __future__ import annotations

"""Issue and supervise the one exact Phase 9 disposable Linux proof.

This repository-only authority is intentionally excluded from the sealed
controller release.  It creates one fixed root-owned permit with pre-signed
install documents, retains the private key, services only the fixed rollback
signature pipes, and execs only the exact sealed runner under the exact sealed
controller interpreter.  No path, command, SQL, URL, resource, signer, nonce,
approval text, or environment input is accepted.
"""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import select
import stat
import subprocess
import sys
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
    != "tools/governed_memory_validation/issue_disposable_installation_live_proof_permit.py"
):
    raise SystemExit("phase9_proof_issuer_invocation_invalid")
sys.path.insert(0, str(_ISSUER_REPOSITORY_ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools.governed_memory_install import authority
from tools.governed_memory_validation.phase9_disposable_proof_authority import (
    ExactRollbackSigningAuthority,
    Phase9DisposableProofAuthorityError,
)
from tools.governed_memory_validation import (
    run_disposable_installation_live_proof as runner,
)


RUNNER_RELATIVE: Final = (
    "tools/governed_memory_validation/run_disposable_installation_live_proof.py"
)
PERMIT_NONCE_DOMAIN: Final = b"governed-memory-phase9-proof-permit-nonce-v1\x00"
MAX_RUNNER_ATTEMPTS: Final = 2
RUNNER_TIMEOUT_SECONDS: Final = 6 * 60
TOTAL_SUPERVISION_TIMEOUT_SECONDS: Final = 12 * 60
MAX_RUNNER_OUTPUT_BYTES: Final = 1024 * 1024
_SAFE_ENVIRONMENT: Final = MappingProxyType(
    {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
)
_ERROR_RE: Final = re.compile(r"[a-z0-9_]{1,160}\Z", re.ASCII)
_GIT: Final = "/usr/bin/git"


class Phase9ProofIssuerError(RuntimeError):
    """Content-free refusal from the repository-only proof issuer."""


def verify_exact_clean_candidate(inputs: runner.ProofInputs) -> None:
    """Bind this repository-only authority source to the supplied clean tree."""

    fixed_commands = (
        ("rev-parse", "HEAD"),
        ("rev-parse", "HEAD^{tree}"),
        ("status", "--porcelain=v1", "--untracked-files=all"),
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
            if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
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
        outputs[0] != inputs.candidate_git_commit
        or outputs[1] != inputs.candidate_git_tree
        or outputs[2] != ""
    ):
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_candidate_git_mismatch"
        )


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


def build_exact_permit(
    *,
    inputs: runner.ProofInputs,
    artifacts: Mapping[str, bytes],
    private_key: Ed25519PrivateKey,
    issued_at: datetime,
) -> Mapping[str, object]:
    """Build the exact fixed permit; all nonces derive from immutable inputs."""

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
    permit_nonce = _sha(PERMIT_NONCE_DOMAIN + nonce_material)
    install_scope = runner._install_scope(inputs, artifacts)
    install_scope_raw = _canonical(install_scope)
    install_nonce = _sha(
        runner.INSTALL_NONCE_DOMAIN + permit_nonce.encode("ascii")
    )
    install_payload = {
        "schema_version": authority.AUTHORIZATION_PAYLOAD_SCHEMA_VERSION,
        "authorization_id": "phase9-disposable-live-install-auth-000001",
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
    rollback_delegation_payload = {
        "schema_version": "governed-memory-phase9-rollback-delegation-v1",
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
        "exact_targets_sha256": exact_targets_sha256,
        "permit_nonce": permit_nonce,
        "key_id": key_id,
        "issued_at": issued_text,
        "not_before": not_before_text,
        "expires_at": expires_text,
        "single_use": True,
        "empty_only": True,
        "source_postgres_read_count": 0,
        "production_data_read": False,
        "provider_calls": 0,
        "activation_allowed": False,
    }
    rollback_delegation = _signed_envelope(
        schema_version=(
            "governed-memory-phase9-rollback-delegation-envelope-v1"
        ),
        payload=rollback_delegation_payload,
        private_key=private_key,
        key_id=key_id,
    )
    permit = {
        "schema_version": runner.PERMIT_SCHEMA,
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
        "permit_nonce": permit_nonce,
        "issued_at": issued_text,
        "not_before": not_before_text,
        "expires_at": expires_text,
        "single_use": True,
        "install_scope": install_scope,
        "install_authorization": install_authorization,
        "trust_bundle": {
            "schema_version": authority.TRUST_BUNDLE_SCHEMA_VERSION,
            "authorization_namespace": runner.AUTHORIZATION_NAMESPACE,
            "keys": [
                {
                    "key_id": key_id,
                    "algorithm": "Ed25519",
                    "public_key_base64": public_key_base64,
                }
            ],
        },
        "rollback_delegation": rollback_delegation,
    }
    if set(permit) != runner._PERMIT_KEYS:
        raise Phase9ProofIssuerError("phase9_proof_issuer_permit_invalid")
    return MappingProxyType(permit)


def _ensure_permit_parent() -> int:
    if os.geteuid() != 0:
        raise Phase9ProofIssuerError("phase9_proof_issuer_root_required")
    parent = runner.PERMIT_PATH.parent
    try:
        os.mkdir(parent, 0o700)
        os.chown(parent, 0, 0, follow_symlinks=False)
        os.chmod(parent, 0o700, follow_symlinks=False)
    except FileExistsError:
        pass
    except OSError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_permit_parent_invalid"
        ) from error
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
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
                "phase9_proof_issuer_permit_parent_invalid"
            )
        return descriptor
    except Exception:
        if "descriptor" in locals():
            os.close(descriptor)
        raise


def publish_fixed_permit(
    permit: Mapping[str, object],
) -> tuple[str, tuple[int, int]]:
    """Create the fixed 0400 permit once and return its exact inode identity."""

    raw = _canonical(dict(permit))
    parent_fd = _ensure_permit_parent()
    file_fd = -1
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            file_fd = os.open(
                runner.PERMIT_PATH.name,
                flags,
                0o400,
                dir_fd=parent_fd,
            )
        except FileExistsError as error:
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_existing_permit_refused"
            ) from error
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
        named = runner.PERMIT_PATH.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o400
            or opened.st_uid != 0
            or opened.st_gid != 0
            or opened.st_nlink != 1
            or opened.st_size != len(raw)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise Phase9ProofIssuerError("phase9_proof_issuer_permit_invalid")
        os.fsync(parent_fd)
        return _sha(raw), (opened.st_dev, opened.st_ino)
    except Phase9ProofIssuerError:
        raise
    except OSError as error:
        raise Phase9ProofIssuerError("phase9_proof_issuer_permit_write_failed") from error
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(parent_fd)


def remove_exact_permit(inode: tuple[int, int]) -> None:
    """Unlink only the exact permit inode created by this issuer process."""

    if (
        type(inode) is not tuple
        or len(inode) != 2
        or any(type(value) is not int or value < 0 for value in inode)
    ):
        raise Phase9ProofIssuerError("phase9_proof_issuer_permit_inode_invalid")
    parent_fd = _ensure_permit_parent()
    try:
        metadata = runner.PERMIT_PATH.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != inode
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
        ):
            raise Phase9ProofIssuerError(
                "phase9_proof_issuer_permit_inode_mismatch"
            )
        os.unlink(runner.PERMIT_PATH.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except Phase9ProofIssuerError:
        raise
    except OSError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_permit_remove_failed"
        ) from error
    finally:
        os.close(parent_fd)


def _read_bounded(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            block = os.read(descriptor, min(65536, maximum + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > maximum:
                raise Phase9ProofIssuerError("phase9_proof_issuer_output_too_large")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _read_one_frame(descriptor: int, maximum: int, deadline: float) -> bytes:
    """Read one newline-terminated frame without waiting for pipe EOF."""

    data = bytearray()
    while len(data) <= maximum:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Phase9ProofIssuerError("phase9_proof_issuer_runner_timeout")
        ready, unused_write, unused_exception = select.select(
            [descriptor], [], [], min(remaining, 0.05)
        )
        if not ready:
            continue
        block = os.read(descriptor, min(65536, maximum + 1 - len(data)))
        if not block:
            break
        data.extend(block)
        newline = data.find(b"\n")
        if newline >= 0:
            if newline != len(data) - 1:
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_signing_request_invalid"
                )
            return bytes(data)
    if len(data) > maximum:
        raise Phase9ProofIssuerError("phase9_proof_issuer_output_too_large")
    return bytes(data)


def _write_response(descriptor: int, response: Mapping[str, object]) -> None:
    raw = _canonical(dict(response)) + b"\n"
    view = memoryview(raw)
    try:
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError
            view = view[written:]
    except OSError as error:
        raise Phase9ProofIssuerError(
            "phase9_proof_issuer_signature_pipe_failed"
        ) from error


def _runner_argv(inputs: runner.ProofInputs) -> tuple[str, ...]:
    return (
        "python",
        "-I",
        "-B",
        str(
            runner.RELEASE_ROOT_PREFIX
            / inputs.package_manifest_sha256
            / RUNNER_RELATIVE
        ),
        "run",
        "--candidate-git-commit",
        inputs.candidate_git_commit,
        "--candidate-git-tree",
        inputs.candidate_git_tree,
        "--package-manifest-sha256",
        inputs.package_manifest_sha256,
        "--controller-runtime-receipt-sha256",
        inputs.controller_runtime_receipt_sha256,
    )


def _spawn_exact_runner(
    inputs: runner.ProofInputs,
) -> tuple[int, int, int, int, int]:
    request_read, request_write = os.pipe()
    response_read, response_write = os.pipe()
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            os.dup2(request_write, runner.ROLLBACK_REQUEST_FD)
            os.dup2(response_read, runner.ROLLBACK_RESPONSE_FD)
            os.dup2(stdout_write, 1)
            os.dup2(stderr_write, 2)
            retained = {
                runner.ROLLBACK_REQUEST_FD,
                runner.ROLLBACK_RESPONSE_FD,
                1,
                2,
            }
            for descriptor in {
                request_read,
                request_write,
                response_read,
                response_write,
                stdout_read,
                stdout_write,
                stderr_read,
                stderr_write,
            } - retained:
                os.close(descriptor)
            runtime_python = str(
                Path("/opt/governed-memory-controller/runtimes")
                / inputs.controller_runtime_receipt_sha256
                / "bin/python"
            )
            os.execve(runtime_python, _runner_argv(inputs), dict(_SAFE_ENVIRONMENT))
        except BaseException:
            os._exit(127)
    os.close(request_write)
    os.close(response_read)
    os.close(stdout_write)
    os.close(stderr_write)
    return pid, request_read, response_write, stdout_read, stderr_read


def _kill_exact_runner_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _supervise_attempt(
    *,
    inputs: runner.ProofInputs,
    signing_authority: ExactRollbackSigningAuthority,
) -> tuple[int, bytes, bytes]:
    pid, request_fd, response_fd, stdout_fd, stderr_fd = _spawn_exact_runner(inputs)
    deadline = time.monotonic() + RUNNER_TIMEOUT_SECONDS
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    request_complete = False
    open_reads = {request_fd, stdout_fd, stderr_fd}
    status: int | None = None
    try:
        while status is None or open_reads:
            if time.monotonic() >= deadline:
                raise Phase9ProofIssuerError("phase9_proof_issuer_runner_timeout")
            waited, observed_status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = observed_status
            ready, unused_write, unused_exception = select.select(
                sorted(open_reads), [], [], 0.05
            )
            for descriptor in ready:
                if descriptor == request_fd:
                    frame = _read_one_frame(
                        request_fd, MAX_RUNNER_OUTPUT_BYTES, deadline
                    )
                    open_reads.discard(request_fd)
                    os.close(request_fd)
                    if frame:
                        if request_complete:
                            raise Phase9ProofIssuerError(
                                "phase9_proof_issuer_signing_request_invalid"
                            )
                        request_complete = True
                        response = signing_authority.authorize(frame[:-1])
                        _write_response(response_fd, response)
                    os.close(response_fd)
                    response_fd = -1
                else:
                    block = os.read(descriptor, 65536)
                    if not block:
                        open_reads.discard(descriptor)
                        os.close(descriptor)
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
    except (Phase9DisposableProofAuthorityError, Phase9ProofIssuerError):
        try:
            _kill_exact_runner_group(pid)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        raise
    finally:
        if response_fd >= 0:
            os.close(response_fd)
        for descriptor in tuple(open_reads):
            try:
                os.close(descriptor)
            except OSError:
                pass
    assert status is not None
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status), bytes(stdout_buffer), bytes(stderr_buffer)
    return 128 + os.WTERMSIG(status), bytes(stdout_buffer), bytes(stderr_buffer)


def issue_and_supervise(inputs: runner.ProofInputs) -> Mapping[str, object]:
    """Issue exact authority and supervise bounded exact runner replays."""

    if os.geteuid() != 0:
        raise Phase9ProofIssuerError("phase9_proof_issuer_root_required")
    verify_exact_clean_candidate(inputs)
    unused_manifest, artifacts = runner._load_release(inputs)
    private_key = Ed25519PrivateKey.generate()
    permit = build_exact_permit(
        inputs=inputs,
        artifacts=dict(artifacts),
        private_key=private_key,
        issued_at=datetime.now(timezone.utc),
    )
    verify_exact_clean_candidate(inputs)
    permit_sha256, permit_inode = publish_fixed_permit(permit)
    signing_authority = ExactRollbackSigningAuthority(permit, private_key)
    last_error = "phase9_proof_issuer_runner_failed"
    supervision_deadline = time.monotonic() + TOTAL_SUPERVISION_TIMEOUT_SECONDS
    for unused_attempt in range(MAX_RUNNER_ATTEMPTS):
        if time.monotonic() >= supervision_deadline:
            break
        try:
            status, stdout, stderr = _supervise_attempt(
                inputs=inputs,
                signing_authority=signing_authority,
            )
        except Phase9ProofIssuerError as error:
            if str(error) != "phase9_proof_issuer_runner_timeout":
                raise
            last_error = str(error)
            continue
        if status == 0:
            try:
                receipt = runner._parse_canonical_object(
                    stdout.rstrip(b"\n"),
                    "phase9_proof_issuer_receipt_invalid",
                )
                verified = runner.verify_live_proof_receipt(receipt)
            except runner.LiveProofError as error:
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_receipt_invalid"
                ) from error
            expected_schema = artifacts.get(runner.LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE)
            if (
                type(expected_schema) is not bytes
                or verified["live_proof_receipt_schema_sha256"]
                != _sha(expected_schema)
                or verified["external_proof_permit_sha256"] != permit_sha256
            ):
                raise Phase9ProofIssuerError(
                    "phase9_proof_issuer_receipt_binding_invalid"
                )
            remove_exact_permit(permit_inode)
            return MappingProxyType(verified)
        line = stderr.decode("ascii", errors="ignore").strip()
        last_error = line if _ERROR_RE.fullmatch(line) else last_error
    raise Phase9ProofIssuerError(last_error)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issue_disposable_installation_live_proof_permit.py",
        allow_abbrev=False,
    )
    parser.add_argument("--candidate-git-commit", required=True)
    parser.add_argument("--candidate-git-tree", required=True)
    parser.add_argument("--package-manifest-sha256", required=True)
    parser.add_argument("--controller-runtime-receipt-sha256", required=True)
    return parser


def parse_inputs(argv: Sequence[str]) -> runner.ProofInputs:
    values = _parser().parse_args(tuple(argv))
    return runner.ProofInputs(
        candidate_git_commit=values.candidate_git_commit,
        candidate_git_tree=values.candidate_git_tree,
        package_manifest_sha256=values.package_manifest_sha256,
        controller_runtime_receipt_sha256=(
            values.controller_runtime_receipt_sha256
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    inputs = parse_inputs(sys.argv[1:] if argv is None else argv)
    receipt = issue_and_supervise(inputs)
    sys.stdout.buffer.write(_canonical(dict(receipt)) + b"\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Phase9ProofIssuerError as error:
        sys.stderr.write(str(error) + "\n")
        raise SystemExit(1)

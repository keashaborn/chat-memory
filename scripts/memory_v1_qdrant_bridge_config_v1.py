#!/usr/bin/env python3
"""One-use, lease-bound bridge configuration controller.

The outer uid-1000 process and the sudo child both verify the spec, checkout,
tool digest, lease and file identities.  Those duplicate checks are drift and
operator-error controls.  They are deliberately not described as a security
boundary against a malicious uid 1000: on the target host that account already
has NOPASSWD root-equivalent sudo authority.

The controller never prints or records environment values.  It does not start,
stop or inspect services.  The privileged child only backs up and atomically
replaces /opt/chat-memory/.env under an exclusive lock.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
import pathlib
import pwd
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


SPEC_VERSION = "memory_v1_qdrant_bridge_config_spec_v1"
AUDIT_VERSION = "memory_v1_qdrant_bridge_config_audit_v1"
REPORT_VERSION = "memory_v1_qdrant_bridge_config_report_v1"
EXPECTED_UID = 1000
EXPECTED_GID = 1000
ROOT_UID = 0
ROOT_GID = 0
PRODUCTION_REPOSITORY = pathlib.Path("/opt/chat-memory")
PREINTEGRATION_OPERATOR_REPOSITORY = pathlib.Path(
    "/home/ubuntu/chat-memory-qdrant-shadow-rebuild-v1-final"
)
ALLOWED_OPERATOR_REPOSITORIES = frozenset(
    {PRODUCTION_REPOSITORY, PREINTEGRATION_OPERATOR_REPOSITORY}
)
TOOL_RELATIVE_PATH = pathlib.Path("scripts/memory_v1_qdrant_bridge_config_v1.py")
ENV_PATH = PRODUCTION_REPOSITORY / ".env"
SNAPSHOT_ROOT = pathlib.Path("/home/ubuntu/brains/snapshots")
LEASE_EVENTS_ROOT = pathlib.Path("/var/lib/chat-memory-change-leases-v1/events")
LEASE_GUARD = pathlib.Path(
    "/var/lib/chat-memory-change-leases-v1/control/bin/chat_memory_lease_guard.py"
)
LOCK_PATH = pathlib.Path("/run/lock/chat-memory-qdrant-bridge-config-v1.lock")
PYTHON = "/usr/bin/python3.12"
GIT = "/usr/bin/git"
SUDO = "/usr/bin/sudo"
SETTING_KEY = "MEMORY_V1_COLLECTION"
SETTING_LINE = b"MEMORY_V1_COLLECTION=memory_claim_v1_active\n"
ANCHOR_LINE = b"MEMORY_V1_V5_SHADOW=1\n"
MAX_SPEC_BYTES = 131_072
MAX_ENV_BYTES = 1_048_576
MAX_EVIDENCE_BYTES = 2_000_000
LEASE_FIELDS = frozenset(
    {
        "lease_id",
        "task_id",
        "thread_id",
        "acquire_event_sha256",
        "registry_revision",
    }
)
IDENTITY_FIELDS = frozenset(
    {"device", "inode", "uid", "gid", "mode", "nlink", "size"}
)
SPEC_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "operation",
        "operator_repository",
        "expected_operator_git",
        "expected_production_git",
        "expected_tool_path",
        "expected_tool_sha256",
        "lease",
        "env_path",
        "expected_env_identity",
        "expected_env_sha256",
        "expected_result_env_sha256",
        "backup_path",
        "expected_backup_identity",
        "expected_backup_sha256",
        "apply_spec_path",
        "apply_spec_sha256",
        "apply_report_path",
        "apply_report_sha256",
        "audit_path",
        "report_path",
    }
)
AUDIT_STATES = frozenset(
    {
        "prepared",
        "backup_durable",
        "completed",
        "failed_no_change",
        "rolled_back_verified",
        "indeterminate",
    }
)
REPORT_FIELDS = frozenset(
    {
        "contract_version",
        "run_id",
        "operation",
        "spec_sha256",
        "tool_path",
        "tool_sha256",
        "operator_repository",
        "operator_git_head",
        "operator_git_tree",
        "production_git_head",
        "production_git_tree",
        "lease_id",
        "lease_event_sha256",
        "lease_registry_revision",
        "env_path",
        "backup_path",
        "backup_sha256",
        "prior_env_sha256",
        "result_env_sha256",
        "raw_values_recorded",
        "service_action_performed",
        "state",
        "prior_env_identity",
        "result_env_identity",
        "backup_identity",
        "failure_stage",
    }
)


class BridgeConfigError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_git_identity(value: Any, *, label: str) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"head", "tree"}
        or any(
            not isinstance(item, str)
            or re.fullmatch(r"[0-9a-f]{40}", item) is None
            for item in value.values()
        )
    ):
        raise BridgeConfigError(f"{label} Git identity rejected")
    return dict(value)


def _operator_repository(value: Any) -> pathlib.Path:
    if not isinstance(value, str):
        raise BridgeConfigError("operator repository identity rejected")
    path = pathlib.Path(value)
    if value != str(path) or path not in ALLOWED_OPERATOR_REPOSITORIES:
        raise BridgeConfigError("operator repository identity rejected")
    return path


def _operator_tool_path(operator_repository: pathlib.Path) -> pathlib.Path:
    return operator_repository / TOOL_RELATIVE_PATH


def current_tool_path() -> pathlib.Path:
    path = pathlib.Path(__file__)
    if not path.is_absolute():
        path = pathlib.Path(os.path.abspath(path))
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _identity(info: os.stat_result) -> dict[str, int]:
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode),
        "nlink": info.st_nlink,
        "size": info.st_size,
    }


def _validate_identity(value: Any, *, root_owned: bool) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != IDENTITY_FIELDS:
        raise BridgeConfigError("file identity fields differ")
    result: dict[str, int] = {}
    for name in IDENTITY_FIELDS:
        item = value.get(name)
        if type(item) is not int or item < 0:
            raise BridgeConfigError("file identity value rejected")
        result[name] = item
    expected_uid = ROOT_UID if root_owned else EXPECTED_UID
    expected_gid = ROOT_GID if root_owned else EXPECTED_GID
    if (
        result["uid"] != expected_uid
        or result["gid"] != expected_gid
        or result["mode"] != 0o600
        or result["nlink"] != 1
        or not 1 <= result["size"] <= MAX_ENV_BYTES
    ):
        raise BridgeConfigError("file identity boundary rejected")
    return result


def validate_lease(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != LEASE_FIELDS:
        raise BridgeConfigError("production-write lease fields differ")
    lease = dict(value)
    for field in ("lease_id", "task_id", "thread_id"):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,200}", str(lease.get(field) or "")):
            raise BridgeConfigError("production-write lease identity rejected")
    if not _is_sha256(lease.get("acquire_event_sha256")):
        raise BridgeConfigError("production-write lease event rejected")
    if type(lease.get("registry_revision")) is not int or lease["registry_revision"] < 1:
        raise BridgeConfigError("production-write lease revision rejected")
    return lease


def _snapshot_path(value: Any, *, filename: str, run_id: str | None) -> pathlib.Path:
    path = pathlib.Path(str(value or ""))
    try:
        relative = path.relative_to(SNAPSHOT_ROOT)
    except ValueError as exc:
        raise BridgeConfigError("snapshot path escaped trusted root") from exc
    if (
        not path.is_absolute()
        or len(relative.parts) < 4
        or any(part in {"", ".", ".."} for part in relative.parts)
        or path.name != filename
        or (run_id is not None and path.parent.name != run_id)
        or path.parent.parent.name != "bridge-config"
    ):
        raise BridgeConfigError("snapshot path rejected")
    return path


def _spec_path(value: Any) -> pathlib.Path:
    path = pathlib.Path(str(value or ""))
    try:
        relative = path.relative_to(SNAPSHOT_ROOT)
    except ValueError as exc:
        raise BridgeConfigError("spec path escaped trusted root") from exc
    if (
        not path.is_absolute()
        or len(relative.parts) != 2
        or any(part in {"", ".", ".."} for part in relative.parts)
        or path.name != "bridge-config.spec.json"
    ):
        raise BridgeConfigError("spec path layout rejected")
    return path


def _read_bounded_descriptor(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise BridgeConfigError("file exceeds size bound")
    return b"".join(chunks)


def _stable_read(path: pathlib.Path, *, expected_uid: int, expected_gid: int, maximum: int,
                 expected_identity: Mapping[str, int] | None = None) -> tuple[bytes, dict[str, int]]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise BridgeConfigError("identity-bound file open failed") from exc
    try:
        opening = os.fstat(descriptor)
        identity = _identity(opening)
        if (
            not stat.S_ISREG(opening.st_mode)
            or opening.st_uid != expected_uid
            or opening.st_gid != expected_gid
            or stat.S_IMODE(opening.st_mode) != 0o600
            or opening.st_nlink != 1
            or not 1 <= opening.st_size <= maximum
            or (expected_identity is not None and identity != dict(expected_identity))
        ):
            raise BridgeConfigError("identity-bound file metadata rejected")
        raw = _read_bounded_descriptor(descriptor, maximum)
        closing = os.fstat(descriptor)
        stable = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if len(raw) != opening.st_size or any(
            getattr(opening, name) != getattr(closing, name) for name in stable
        ):
            raise BridgeConfigError("identity-bound file changed while reading")
        return raw, identity
    finally:
        os.close(descriptor)


def _read_spec_file(path: pathlib.Path) -> bytes:
    if not path.is_absolute():
        raise BridgeConfigError("spec path must be absolute")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise BridgeConfigError("spec open failed") from exc
    try:
        opening = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opening.st_mode)
            or opening.st_uid != EXPECTED_UID
            or opening.st_gid != EXPECTED_GID
            or stat.S_IMODE(opening.st_mode) != 0o600
            or opening.st_nlink != 1
            or not 1 <= opening.st_size <= MAX_SPEC_BYTES
        ):
            raise BridgeConfigError("spec identity rejected")
        raw = _read_bounded_descriptor(descriptor, MAX_SPEC_BYTES)
        closing = os.fstat(descriptor)
        if len(raw) != opening.st_size or _identity(opening) != _identity(closing):
            raise BridgeConfigError("spec changed while reading")
        return raw
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class Spec:
    value: dict[str, Any]
    raw: bytes

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.raw)

    @classmethod
    def load(cls, path: pathlib.Path, expected_sha256: str) -> "Spec":
        _spec_path(path)
        raw = _read_spec_file(path)
        if not _is_sha256(expected_sha256) or sha256_bytes(raw) != expected_sha256:
            raise BridgeConfigError("spec digest mismatch")
        try:
            value = json.loads(raw.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeConfigError("spec parse failed") from exc
        if not isinstance(value, dict) or set(value) != SPEC_FIELDS or raw != canonical_bytes(value):
            raise BridgeConfigError("spec contract differs")
        operation = value.get("operation")
        run_id = str(value.get("run_id") or "")
        if (
            value.get("schema_version") != SPEC_VERSION
            or operation not in {"apply", "restore"}
            or not re.fullmatch(
                r"memory-qdrant-bridge-config-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}",
                run_id,
            )
        ):
            raise BridgeConfigError("spec operation identity rejected")
        operator_repository = _operator_repository(value.get("operator_repository"))
        operator_git = _validate_git_identity(
            value.get("expected_operator_git"), label="operator"
        )
        production_git = _validate_git_identity(
            value.get("expected_production_git"), label="production"
        )
        if (
            pathlib.Path(str(value.get("expected_tool_path") or ""))
            != _operator_tool_path(operator_repository)
            or not _is_sha256(value.get("expected_tool_sha256"))
            or pathlib.Path(str(value.get("env_path") or "")) != ENV_PATH
            or not _is_sha256(value.get("expected_env_sha256"))
            or not _is_sha256(value.get("expected_result_env_sha256"))
            or value.get("expected_env_sha256") == value.get("expected_result_env_sha256")
        ):
            raise BridgeConfigError("spec immutable identity rejected")
        if operator_repository == PRODUCTION_REPOSITORY and operator_git != production_git:
            raise BridgeConfigError("installed operator and production Git identities differ")
        _validate_identity(value.get("expected_env_identity"), root_owned=True)
        validate_lease(value.get("lease"))
        backup = _snapshot_path(value.get("backup_path"), filename="bridge-config.env.backup", run_id=None)
        audit = _snapshot_path(value.get("audit_path"), filename="bridge-config.audit.jsonl", run_id=run_id)
        report = _snapshot_path(value.get("report_path"), filename="bridge-config.report.json", run_id=run_id)
        if len({backup, audit, report}) != 3:
            raise BridgeConfigError("spec evidence paths collide")
        if operation == "apply":
            if (
                backup.parent.name != run_id
                or value.get("expected_backup_identity") is not None
                or value.get("expected_backup_sha256") is not None
                or value.get("apply_spec_path") is not None
                or value.get("apply_spec_sha256") is not None
                or value.get("apply_report_path") is not None
                or value.get("apply_report_sha256") is not None
            ):
                raise BridgeConfigError("apply backup boundary rejected")
        else:
            _validate_identity(value.get("expected_backup_identity"), root_owned=True)
            if (
                not _is_sha256(value.get("expected_backup_sha256"))
                or value["expected_backup_sha256"] != value["expected_result_env_sha256"]
            ):
                raise BridgeConfigError("restore backup boundary rejected")
            apply_spec_path = _spec_path(value.get("apply_spec_path"))
            apply_report_path = _snapshot_path(
                value.get("apply_report_path"),
                filename="bridge-config.report.json",
                run_id=None,
            )
            if (
                not _is_sha256(value.get("apply_spec_sha256"))
                or not _is_sha256(value.get("apply_report_sha256"))
                or apply_spec_path == path
                or apply_report_path == report
            ):
                raise BridgeConfigError("restore lineage boundary rejected")
        return cls(value=value, raw=raw)


def _run(argv: Sequence[str], *, env: Mapping[str, str], timeout: int = 60) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            env=dict(env),
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeConfigError("bounded subprocess failed") from exc


def git_identity(repository: pathlib.Path) -> dict[str, str]:
    prefix = [
        GIT,
        "--no-optional-locks",
        "--no-pager",
        "-c",
        f"safe.directory={repository}",
        "-C",
        str(repository),
    ]
    env = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
    head = _run([*prefix, "rev-parse", "HEAD"], env=env)
    tree = _run([*prefix, "rev-parse", "HEAD^{tree}"], env=env)
    status = _run(
        [*prefix, "status", "--porcelain=v1", "--untracked-files=normal"], env=env
    )
    if any(item.returncode != 0 for item in (head, tree, status)) or status.stdout:
        raise BridgeConfigError("checkout identity rejected")
    try:
        result = {
            "head": head.stdout.strip().decode("ascii"),
            "tree": tree.stdout.strip().decode("ascii"),
        }
    except UnicodeDecodeError as exc:
        raise BridgeConfigError("checkout identity parse failed") from exc
    if any(not re.fullmatch(r"[0-9a-f]{40}", item) for item in result.values()):
        raise BridgeConfigError("checkout identity malformed")
    return result


def tool_sha256(path: pathlib.Path) -> str:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise BridgeConfigError("tool open failed") from exc
    try:
        opening = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opening.st_mode)
            or opening.st_uid != EXPECTED_UID
            or opening.st_gid != EXPECTED_GID
            or stat.S_IMODE(opening.st_mode) != 0o644
            or opening.st_nlink != 1
            or not 1 <= opening.st_size <= MAX_EVIDENCE_BYTES
        ):
            raise BridgeConfigError("tool identity rejected")
        raw = _read_bounded_descriptor(descriptor, MAX_EVIDENCE_BYTES)
        closing = os.fstat(descriptor)
        if len(raw) != opening.st_size or _identity(opening) != _identity(closing):
            raise BridgeConfigError("tool changed while reading")
        return sha256_bytes(raw)
    finally:
        os.close(descriptor)


def verify_execution_identity(spec: Spec) -> pathlib.Path:
    operator_repository = _operator_repository(spec.value["operator_repository"])
    expected_tool_path = _operator_tool_path(operator_repository)
    if (
        pathlib.Path(spec.value["expected_tool_path"]) != expected_tool_path
        or current_tool_path() != expected_tool_path
        or tool_sha256(expected_tool_path) != spec.value["expected_tool_sha256"]
    ):
        raise BridgeConfigError("executed operator tool identity changed")
    if git_identity(operator_repository) != spec.value["expected_operator_git"]:
        raise BridgeConfigError("operator checkout changed")
    if git_identity(PRODUCTION_REPOSITORY) != spec.value["expected_production_git"]:
        raise BridgeConfigError("production checkout changed")
    return expected_tool_path


def _load_lease_event(lease: Mapping[str, Any]) -> dict[str, Any]:
    prefix = f"{lease['registry_revision']:020d}-"
    try:
        root = os.open(
            LEASE_EVENTS_ROOT,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise BridgeConfigError("lease registry unavailable") from exc
    try:
        root_info = os.fstat(root)
        with os.scandir(root) as entries:
            candidates = [
                entry.name
                for entry in entries
                if entry.name.startswith(prefix) and entry.name.endswith(".json")
            ]
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_uid != EXPECTED_UID
            or stat.S_IMODE(root_info.st_mode) & 0o077
            or len(candidates) != 1
            or not re.fullmatch(
                rf"{lease['registry_revision']:020d}-[0-9a-f]{{32}}\.json", candidates[0]
            )
        ):
            raise BridgeConfigError("lease registry identity rejected")
        descriptor = os.open(
            candidates[0],
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root,
        )
        try:
            opening = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opening.st_mode)
                or opening.st_uid != EXPECTED_UID
                or stat.S_IMODE(opening.st_mode) != 0o600
                or opening.st_nlink != 1
                or not 1 <= opening.st_size <= 262_144
            ):
                raise BridgeConfigError("lease event identity rejected")
            raw = _read_bounded_descriptor(descriptor, 262_144)
            closing = os.fstat(descriptor)
            if len(raw) != opening.st_size or _identity(opening) != _identity(closing):
                raise BridgeConfigError("lease event changed while reading")
        finally:
            os.close(descriptor)
    finally:
        os.close(root)
    try:
        event = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeConfigError("lease event parse failed") from exc
    if (
        not isinstance(event, dict)
        or raw != canonical_bytes(event)
        or event.get("schema_version") != "chat-memory-change-lease-event-v1"
        or event.get("event_type") != "acquire"
        or event.get("sequence") != lease["registry_revision"]
        or event.get("lease_id") != lease["lease_id"]
        or event.get("event_sha256") != lease["acquire_event_sha256"]
    ):
        raise BridgeConfigError("lease acquisition event differs")
    body = dict(event)
    stored = body.pop("event_sha256", None)
    actor = event.get("actor")
    payload = event.get("payload")
    if (
        sha256_bytes(canonical_bytes(body)) != stored
        or not isinstance(actor, dict)
        or actor.get("uid") != EXPECTED_UID
        or actor.get("task_id") != lease["task_id"]
        or actor.get("thread_id") != lease["thread_id"]
        or not isinstance(payload, dict)
        or payload.get("worktree") != str(PRODUCTION_REPOSITORY)
        or "production-write" not in (payload.get("change_types") or [])
    ):
        raise BridgeConfigError("lease acquisition authority differs")
    return event


def require_guard(lease_value: Mapping[str, Any]) -> None:
    if os.geteuid() != EXPECTED_UID:
        raise BridgeConfigError("direct production-write guard requires uid 1000")
    lease = validate_lease(lease_value)
    _load_lease_event(lease)
    expected = {
        "CHAT_MEMORY_LEASE_ID": lease["lease_id"],
        "CODEX_TASK_ID": lease["task_id"],
        "CODEX_THREAD_ID": lease["thread_id"],
    }
    if any(os.environ.get(name) != value for name, value in expected.items()):
        raise BridgeConfigError("lease runtime identity differs")
    if not LEASE_GUARD.is_file():
        raise BridgeConfigError("production-write guard installation rejected")
    result = _run(
        [
            PYTHON,
            str(LEASE_GUARD),
            "--operation",
            "production-write",
            "--worktree",
            str(PRODUCTION_REPOSITORY),
        ],
        env={**expected, "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        timeout=30,
    )
    if result.returncode != 0:
        raise BridgeConfigError("production-write guard denied")


def require_guard_as_uid1000(lease_value: Mapping[str, Any]) -> None:
    if os.getuid() != ROOT_UID or os.geteuid() != ROOT_UID:
        raise BridgeConfigError("privileged guard broker requires root")
    lease = validate_lease(lease_value)
    _load_lease_event(lease)
    expected = {
        "CHAT_MEMORY_LEASE_ID": lease["lease_id"],
        "CODEX_TASK_ID": lease["task_id"],
        "CODEX_THREAD_ID": lease["thread_id"],
    }
    if any(os.environ.get(name) != value for name, value in expected.items()):
        raise BridgeConfigError("lease runtime identity differs")
    if not LEASE_GUARD.is_file():
        raise BridgeConfigError("production-write guard installation rejected")
    try:
        account = pwd.getpwuid(EXPECTED_UID)
    except KeyError as exc:
        raise BridgeConfigError("uid-1000 guard account missing") from exc
    if account.pw_uid != EXPECTED_UID or account.pw_gid != EXPECTED_GID:
        raise BridgeConfigError("uid-1000 guard account mapping rejected")
    result = _run(
        [
            SUDO,
            "--non-interactive",
            "--user",
            f"#{EXPECTED_UID}",
            "--preserve-env=CHAT_MEMORY_LEASE_ID,CODEX_TASK_ID,CODEX_THREAD_ID",
            PYTHON,
            str(LEASE_GUARD),
            "--operation",
            "production-write",
            "--worktree",
            str(PRODUCTION_REPOSITORY),
        ],
        env={**expected, "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        timeout=30,
    )
    if result.returncode != 0:
        raise BridgeConfigError("uid-1000 production-write guard denied")


def _open_private_parent(path: pathlib.Path) -> tuple[int, str]:
    try:
        relative = path.relative_to(SNAPSHOT_ROOT)
    except ValueError as exc:
        raise BridgeConfigError("evidence path escaped trusted root") from exc
    if len(relative.parts) < 2 or any(part in {"", ".", ".."} for part in relative.parts):
        raise BridgeConfigError("evidence path rejected")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(SNAPSHOT_ROOT, flags)
    except OSError as exc:
        raise BridgeConfigError("snapshot root open failed") from exc
    try:
        root_info = os.fstat(descriptor)
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_IMODE(root_info.st_mode) & 0o022:
            raise BridgeConfigError("snapshot root identity rejected")
        for index, component in enumerate(relative.parts[:-1]):
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            expected_uid = EXPECTED_UID if index == 0 else ROOT_UID
            expected_gid = EXPECTED_GID if index == 0 else ROOT_GID
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != expected_uid
                or info.st_gid != expected_gid
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise BridgeConfigError("private evidence directory rejected")
        return descriptor, relative.parts[-1]
    except Exception:
        os.close(descriptor)
        raise


def _write_all(descriptor: int, raw: bytes) -> None:
    offset = 0
    while offset < len(raw):
        written = os.write(descriptor, raw[offset:])
        if written <= 0:
            raise BridgeConfigError("durable write failed")
        offset += written


def _create_private_file(path: pathlib.Path, raw: bytes = b"") -> tuple[int, dict[str, int]]:
    parent, name = _open_private_parent(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent,
        )
        if raw:
            _write_all(descriptor, raw)
        os.fsync(descriptor)
        os.fsync(parent)
        info = os.fstat(descriptor)
        identity = _identity(info)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != ROOT_UID
            or info.st_gid != ROOT_GID
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise BridgeConfigError("private evidence identity rejected")
        return descriptor, identity
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise BridgeConfigError("private evidence creation failed") from exc
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        os.close(parent)


def _stable_read_private(
    path: pathlib.Path,
    *,
    maximum: int,
    expected_identity: Mapping[str, int] | None = None,
) -> tuple[bytes, dict[str, int]]:
    parent, name = _open_private_parent(path)
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
        except OSError as exc:
            raise BridgeConfigError("private evidence open failed") from exc
        try:
            opening = os.fstat(descriptor)
            identity = _identity(opening)
            if (
                not stat.S_ISREG(opening.st_mode)
                or opening.st_uid != ROOT_UID
                or opening.st_gid != ROOT_GID
                or stat.S_IMODE(opening.st_mode) != 0o600
                or opening.st_nlink != 1
                or not 1 <= opening.st_size <= maximum
                or (expected_identity is not None and identity != dict(expected_identity))
            ):
                raise BridgeConfigError("private evidence identity rejected")
            raw = _read_bounded_descriptor(descriptor, maximum)
            closing = os.fstat(descriptor)
            stable = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_gid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if len(raw) != opening.st_size or any(
                getattr(opening, item) != getattr(closing, item) for item in stable
            ):
                raise BridgeConfigError("private evidence changed while reading")
            return raw, identity
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def _rewrite_descriptor(descriptor: int, raw: bytes) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    _write_all(descriptor, raw)
    os.fsync(descriptor)


@dataclass
class Audit:
    descriptor: int
    prior_event_sha256: str | None = None
    sequence: int = 0

    def append(self, state: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        if state not in AUDIT_STATES:
            raise BridgeConfigError("audit state rejected")
        body = {
            "contract_version": AUDIT_VERSION,
            **dict(fields),
            "audit_sequence": self.sequence + 1,
            "occurred_at": _now(),
            "prior_event_sha256": self.prior_event_sha256,
            "state": state,
        }
        event_sha = sha256_bytes(canonical_bytes(body))
        event = {**body, "event_sha256": event_sha}
        opening_size = os.lseek(self.descriptor, 0, os.SEEK_END)
        try:
            _write_all(self.descriptor, canonical_bytes(event))
            os.fsync(self.descriptor)
        except Exception as exc:
            try:
                os.ftruncate(self.descriptor, opening_size)
                os.fsync(self.descriptor)
            except Exception as rollback_exc:
                raise BridgeConfigError("audit append indeterminate") from rollback_exc
            raise BridgeConfigError("audit append failed without durable change") from exc
        self.sequence += 1
        self.prior_event_sha256 = event_sha
        return event


def _assert_env_shape(raw: bytes, *, expect_setting: bool) -> None:
    if not raw or len(raw) > MAX_ENV_BYTES or b"\x00" in raw or b"\r" in raw or not raw.endswith(b"\n"):
        raise BridgeConfigError("environment text boundary rejected")
    try:
        raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise BridgeConfigError("environment text encoding rejected") from exc
    lines = raw.splitlines(keepends=True)
    setting_pattern = re.compile(
        br"^[ \t]*(?:export[ \t]+)?MEMORY_V1_COLLECTION[ \t]*="
    )
    anchor_pattern = re.compile(
        br"^[ \t]*(?:export[ \t]+)?MEMORY_V1_V5_SHADOW[ \t]*="
    )
    setting_lines = [line for line in lines if setting_pattern.match(line)]
    anchor_lines = [line for line in lines if anchor_pattern.match(line)]
    if (
        anchor_lines != [ANCHOR_LINE]
        or len(setting_lines) != (1 if expect_setting else 0)
        or (expect_setting and setting_lines != [SETTING_LINE])
    ):
        raise BridgeConfigError("environment bridge shape rejected")
    if expect_setting:
        index = lines.index(ANCHOR_LINE)
        if index == 0 or lines[index - 1] != SETTING_LINE:
            raise BridgeConfigError("environment bridge placement rejected")


def apply_transform(raw: bytes) -> bytes:
    _assert_env_shape(raw, expect_setting=False)
    lines = raw.splitlines(keepends=True)
    index = lines.index(ANCHOR_LINE)
    return b"".join([*lines[:index], SETTING_LINE, *lines[index:]])


def restore_transform(current: bytes, backup: bytes) -> bytes:
    _assert_env_shape(backup, expect_setting=False)
    _assert_env_shape(current, expect_setting=True)
    if apply_transform(backup) != current:
        raise BridgeConfigError("restore source is not exact applied bridge state")
    return backup


def _open_env_parent() -> int:
    if ENV_PATH.parent != PRODUCTION_REPOSITORY or ENV_PATH.name != ".env":
        raise BridgeConfigError("environment parent path rejected")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(PRODUCTION_REPOSITORY, flags)
    except OSError as exc:
        raise BridgeConfigError("environment parent open failed") from exc
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != EXPECTED_UID
        or info.st_gid != EXPECTED_GID
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        os.close(descriptor)
        raise BridgeConfigError("environment parent identity rejected")
    return descriptor


def _stable_read_at(
    parent_descriptor: int,
    name: str,
    *,
    expected_uid: int,
    expected_gid: int,
    maximum: int,
    expected_identity: Mapping[str, int] | None = None,
) -> tuple[bytes, dict[str, int]]:
    if not name or "/" in name or name in {".", ".."}:
        raise BridgeConfigError("directory-relative file name rejected")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise BridgeConfigError("directory-relative file open failed") from exc
    try:
        opening = os.fstat(descriptor)
        identity = _identity(opening)
        if (
            not stat.S_ISREG(opening.st_mode)
            or opening.st_uid != expected_uid
            or opening.st_gid != expected_gid
            or stat.S_IMODE(opening.st_mode) != 0o600
            or opening.st_nlink != 1
            or not 1 <= opening.st_size <= maximum
            or (expected_identity is not None and identity != dict(expected_identity))
        ):
            raise BridgeConfigError("directory-relative file identity rejected")
        raw = _read_bounded_descriptor(descriptor, maximum)
        closing = os.fstat(descriptor)
        stable = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if len(raw) != opening.st_size or any(
            getattr(opening, field) != getattr(closing, field) for field in stable
        ):
            raise BridgeConfigError("directory-relative file changed while reading")
        return raw, identity
    finally:
        os.close(descriptor)


def _rename_exchange(parent_descriptor: int, first: str, second: str) -> None:
    if sys.platform != "linux" or any(
        not name or "/" in name or name in {".", ".."} for name in (first, second)
    ):
        raise BridgeConfigError("atomic exchange platform or path rejected")
    library = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = library.renameat2
    except AttributeError as exc:
        raise BridgeConfigError("atomic exchange unavailable") from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            parent_descriptor,
            os.fsencode(first),
            parent_descriptor,
            os.fsencode(second),
            2,  # RENAME_EXCHANGE
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise BridgeConfigError("atomic exchange failed") from OSError(
            error, os.strerror(error)
        )


def _replace_env(raw: bytes, *, expected_identity: Mapping[str, int], expected_sha256: str,
                 run_id: str) -> dict[str, int]:
    parent = _open_env_parent()
    temporary_name = f".env.{run_id}.tmp"
    temporary: int | None = None
    temporary_exists = False
    exchanged = False
    try:
        current, identity = _stable_read_at(
            parent,
            ENV_PATH.name,
            expected_uid=ROOT_UID,
            expected_gid=ROOT_GID,
            maximum=MAX_ENV_BYTES,
            expected_identity=expected_identity,
        )
        if sha256_bytes(current) != expected_sha256:
            raise BridgeConfigError("environment changed before atomic replace")
        temporary = os.open(
            temporary_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent,
        )
        temporary_exists = True
        _write_all(temporary, raw)
        os.fsync(temporary)
        info = os.fstat(temporary)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != ROOT_UID
            or info.st_gid != ROOT_GID
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size != len(raw)
        ):
            raise BridgeConfigError("replacement file identity rejected")
        os.lseek(temporary, 0, os.SEEK_SET)
        if _read_bounded_descriptor(temporary, MAX_ENV_BYTES) != raw:
            raise BridgeConfigError("replacement file bytes rejected")
        os.close(temporary)
        temporary = None
        precommit, precommit_identity = _stable_read_at(
            parent,
            ENV_PATH.name,
            expected_uid=ROOT_UID,
            expected_gid=ROOT_GID,
            maximum=MAX_ENV_BYTES,
            expected_identity=identity,
        )
        if precommit != current or precommit_identity != identity:
            raise BridgeConfigError("environment changed before atomic exchange")
        _rename_exchange(parent, temporary_name, ENV_PATH.name)
        exchanged = True
        os.fsync(parent)
        displaced, displaced_identity = _stable_read_at(
            parent,
            temporary_name,
            expected_uid=ROOT_UID,
            expected_gid=ROOT_GID,
            maximum=MAX_ENV_BYTES,
        )
        if displaced != current or displaced_identity != identity:
            _rename_exchange(parent, temporary_name, ENV_PATH.name)
            exchanged = False
            os.fsync(parent)
            restored, restored_identity = _stable_read_at(
                parent,
                ENV_PATH.name,
                expected_uid=ROOT_UID,
                expected_gid=ROOT_GID,
                maximum=MAX_ENV_BYTES,
                expected_identity=displaced_identity,
            )
            if restored != displaced or restored_identity != displaced_identity:
                raise BridgeConfigError("concurrent environment restoration indeterminate")
            raise BridgeConfigError("environment changed at atomic exchange")
        closing_raw, closing_identity = _stable_read_at(
            parent,
            ENV_PATH.name,
            expected_uid=ROOT_UID,
            expected_gid=ROOT_GID,
            maximum=MAX_ENV_BYTES,
        )
        if closing_raw != raw:
            raise BridgeConfigError("atomic replacement verification failed")
        os.unlink(temporary_name, dir_fd=parent)
        os.fsync(parent)
        temporary_exists = False
        exchanged = False
        return closing_identity
    finally:
        if temporary is not None:
            os.close(temporary)
        if temporary_exists and not exchanged:
            try:
                os.unlink(temporary_name, dir_fd=parent)
                os.fsync(parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def _discard_retained_exchange_temporary(
    *,
    run_id: str,
    expected_raw: bytes,
    expected_identity: Mapping[str, int],
) -> None:
    """Remove only the verified original displaced by an interrupted exchange."""
    parent = _open_env_parent()
    temporary_name = f".env.{run_id}.tmp"
    try:
        try:
            os.stat(temporary_name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        retained, retained_identity = _stable_read_at(
            parent,
            temporary_name,
            expected_uid=ROOT_UID,
            expected_gid=ROOT_GID,
            maximum=MAX_ENV_BYTES,
            expected_identity=expected_identity,
        )
        if retained != expected_raw or retained_identity != dict(expected_identity):
            raise BridgeConfigError("retained exchange temporary rejected")
        os.unlink(temporary_name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def _lock() -> int:
    try:
        descriptor = os.open(
            LOCK_PATH,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise BridgeConfigError("configuration lock open failed") from exc
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != ROOT_UID
        or info.st_gid != ROOT_GID
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        os.close(descriptor)
        raise BridgeConfigError("configuration lock identity rejected")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise BridgeConfigError("configuration lock unavailable") from exc
    return descriptor


def _common_fields(spec: Spec, backup_sha: str) -> dict[str, Any]:
    lease = spec.value["lease"]
    return {
        "run_id": spec.value["run_id"],
        "operation": spec.value["operation"],
        "spec_sha256": spec.sha256,
        "tool_path": spec.value["expected_tool_path"],
        "tool_sha256": spec.value["expected_tool_sha256"],
        "operator_repository": spec.value["operator_repository"],
        "operator_git_head": spec.value["expected_operator_git"]["head"],
        "operator_git_tree": spec.value["expected_operator_git"]["tree"],
        "production_git_head": spec.value["expected_production_git"]["head"],
        "production_git_tree": spec.value["expected_production_git"]["tree"],
        "lease_id": lease["lease_id"],
        "lease_event_sha256": lease["acquire_event_sha256"],
        "lease_registry_revision": lease["registry_revision"],
        "env_path": str(ENV_PATH),
        "backup_path": spec.value["backup_path"],
        "backup_sha256": backup_sha,
        "prior_env_sha256": spec.value["expected_env_sha256"],
        "result_env_sha256": spec.value["expected_result_env_sha256"],
        "raw_values_recorded": False,
        "service_action_performed": False,
    }


def _report(spec: Spec, *, state: str, common: Mapping[str, Any],
            prior_identity: Mapping[str, int], result_identity: Mapping[str, int] | None,
            backup_identity: Mapping[str, int] | None,
            failure_stage: str | None = None) -> dict[str, Any]:
    return {
        "contract_version": REPORT_VERSION,
        **dict(common),
        "state": state,
        "prior_env_identity": dict(prior_identity),
        "result_env_identity": dict(result_identity) if result_identity is not None else None,
        "backup_identity": (
            dict(backup_identity) if backup_identity is not None else None
        ),
        "failure_stage": failure_stage,
    }


def _validate_restore_lineage(spec: Spec, backup_sha256: str) -> None:
    if spec.value["operation"] != "restore":
        raise BridgeConfigError("restore lineage invoked for non-restore spec")
    apply_spec_path = pathlib.Path(spec.value["apply_spec_path"])
    apply_spec = Spec.load(apply_spec_path, spec.value["apply_spec_sha256"])
    if (
        apply_spec.value["operation"] != "apply"
        or apply_spec.value["expected_tool_sha256"] != spec.value["expected_tool_sha256"]
        or apply_spec.value["backup_path"] != spec.value["backup_path"]
        or apply_spec.value["report_path"] != spec.value["apply_report_path"]
        or apply_spec.value["expected_env_sha256"] != backup_sha256
        or apply_spec.value["expected_result_env_sha256"]
        != spec.value["expected_env_sha256"]
    ):
        raise BridgeConfigError("apply spec lineage differs")
    report_raw, _ = _stable_read_private(
        pathlib.Path(spec.value["apply_report_path"]), maximum=MAX_EVIDENCE_BYTES
    )
    if sha256_bytes(report_raw) != spec.value["apply_report_sha256"]:
        raise BridgeConfigError("apply report lineage digest changed")
    try:
        report = json.loads(report_raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeConfigError("apply report lineage parse failed") from exc
    if (
        not isinstance(report, dict)
        or set(report) != REPORT_FIELDS
        or report_raw != canonical_bytes(report)
        or report.get("contract_version") != REPORT_VERSION
        or report.get("state") != "completed"
        or report.get("failure_stage") is not None
        or report.get("run_id") != apply_spec.value["run_id"]
        or report.get("operation") != "apply"
        or report.get("spec_sha256") != apply_spec.sha256
        or report.get("tool_path") != apply_spec.value["expected_tool_path"]
        or report.get("tool_sha256") != apply_spec.value["expected_tool_sha256"]
        or report.get("operator_repository")
        != apply_spec.value["operator_repository"]
        or report.get("operator_git_head")
        != apply_spec.value["expected_operator_git"]["head"]
        or report.get("operator_git_tree")
        != apply_spec.value["expected_operator_git"]["tree"]
        or report.get("production_git_head")
        != apply_spec.value["expected_production_git"]["head"]
        or report.get("production_git_tree")
        != apply_spec.value["expected_production_git"]["tree"]
        or report.get("lease_id") != apply_spec.value["lease"]["lease_id"]
        or report.get("lease_event_sha256")
        != apply_spec.value["lease"]["acquire_event_sha256"]
        or report.get("lease_registry_revision")
        != apply_spec.value["lease"]["registry_revision"]
        or report.get("env_path") != str(ENV_PATH)
        or report.get("backup_path") != spec.value["backup_path"]
        or report.get("backup_sha256") != backup_sha256
        or report.get("backup_identity") != spec.value["expected_backup_identity"]
        or report.get("prior_env_sha256") != spec.value["expected_result_env_sha256"]
        or report.get("result_env_sha256") != spec.value["expected_env_sha256"]
        or report.get("prior_env_identity")
        != apply_spec.value["expected_env_identity"]
        or report.get("result_env_identity") != spec.value["expected_env_identity"]
        or report.get("raw_values_recorded") is not False
        or report.get("service_action_performed") is not False
    ):
        raise BridgeConfigError("apply report lineage differs")


def _execute_child(spec: Spec) -> dict[str, Any]:
    if os.getuid() != ROOT_UID or os.geteuid() != ROOT_UID or os.environ.get("SUDO_UID") != str(EXPECTED_UID):
        raise BridgeConfigError("privileged child identity rejected")
    verify_execution_identity(spec)
    require_guard_as_uid1000(spec.value["lease"])
    lock_descriptor = _lock()
    audit_descriptor: int | None = None
    report_descriptor: int | None = None
    try:
        environment_parent = _open_env_parent()
        os.close(environment_parent)
        prior, prior_identity = _stable_read(
            ENV_PATH,
            expected_uid=ROOT_UID,
            expected_gid=ROOT_GID,
            maximum=MAX_ENV_BYTES,
            expected_identity=spec.value["expected_env_identity"],
        )
        if sha256_bytes(prior) != spec.value["expected_env_sha256"]:
            raise BridgeConfigError("environment digest changed")
        operation = spec.value["operation"]
        backup_path = pathlib.Path(spec.value["backup_path"])
        if operation == "apply":
            desired = apply_transform(prior)
            backup = prior
            backup_identity: dict[str, int] | None = None
            backup_sha = sha256_bytes(prior)
        else:
            backup, backup_identity = _stable_read_private(
                backup_path,
                maximum=MAX_ENV_BYTES,
                expected_identity=spec.value["expected_backup_identity"],
            )
            backup_sha = sha256_bytes(backup)
            if backup_sha != spec.value["expected_backup_sha256"]:
                raise BridgeConfigError("backup digest changed")
            _validate_restore_lineage(spec, backup_sha)
            desired = restore_transform(prior, backup)
        if sha256_bytes(desired) != spec.value["expected_result_env_sha256"]:
            raise BridgeConfigError("expected result digest differs")
        audit_descriptor, _ = _create_private_file(pathlib.Path(spec.value["audit_path"]))
        report_descriptor, _ = _create_private_file(pathlib.Path(spec.value["report_path"]))
        audit = Audit(audit_descriptor)
        common = _common_fields(spec, backup_sha)
        prepared = audit.append(
            "prepared",
            {
                **common,
                "prior_env_identity": prior_identity,
                "backup_identity": backup_identity,
            },
        )
        backup_event_sha256: str | None = None
        result_identity: dict[str, int] | None = None
        try:
            if operation == "apply":
                backup_descriptor, backup_identity = _create_private_file(
                    backup_path, prior
                )
                os.close(backup_descriptor)
                backup, verified_backup_identity = _stable_read_private(
                    backup_path,
                    maximum=MAX_ENV_BYTES,
                    expected_identity=backup_identity,
                )
                if backup != prior or verified_backup_identity != backup_identity:
                    raise BridgeConfigError("durable backup verification rejected")
                backup_event = audit.append(
                    "backup_durable",
                    {
                        **common,
                        "prepared_event_sha256": prepared["event_sha256"],
                        "backup_identity": backup_identity,
                    },
                )
                backup_event_sha256 = backup_event["event_sha256"]
            require_guard_as_uid1000(spec.value["lease"])
            current, current_identity = _stable_read(
                ENV_PATH,
                expected_uid=ROOT_UID,
                expected_gid=ROOT_GID,
                maximum=MAX_ENV_BYTES,
                expected_identity=prior_identity,
            )
            if current != prior:
                raise BridgeConfigError("environment drifted after prepared event")
            result_identity = _replace_env(
                desired,
                expected_identity=current_identity,
                expected_sha256=spec.value["expected_env_sha256"],
                run_id=spec.value["run_id"],
            )
            closing, closing_identity = _stable_read(
                ENV_PATH,
                expected_uid=ROOT_UID,
                expected_gid=ROOT_GID,
                maximum=MAX_ENV_BYTES,
                expected_identity=result_identity,
            )
            if closing != desired or closing_identity != result_identity:
                raise BridgeConfigError("environment closing proof rejected")
            report = _report(
                spec,
                state="completed",
                common=common,
                prior_identity=prior_identity,
                result_identity=result_identity,
                backup_identity=backup_identity,
            )
            _rewrite_descriptor(report_descriptor, canonical_bytes(report))
            audit.append(
                "completed",
                {
                    **common,
                    "prepared_event_sha256": prepared["event_sha256"],
                    "backup_event_sha256": backup_event_sha256,
                    "prior_env_identity": prior_identity,
                    "result_env_identity": result_identity,
                    "backup_identity": backup_identity,
                    "report_sha256": sha256_bytes(canonical_bytes(report)),
                },
            )
            return report
        except Exception as exc:
            failure_stage = "mutation_or_evidence"
            state = "indeterminate"
            reconciled_identity: dict[str, int] | None = None
            try:
                observed, observed_identity = _stable_read(
                    ENV_PATH,
                    expected_uid=ROOT_UID,
                    expected_gid=ROOT_GID,
                    maximum=MAX_ENV_BYTES,
                )
                if observed == prior:
                    state = "failed_no_change"
                    reconciled_identity = observed_identity
                elif observed == desired:
                    if operation == "restore":
                        _discard_retained_exchange_temporary(
                            run_id=spec.value["run_id"],
                            expected_raw=prior,
                            expected_identity=prior_identity,
                        )
                        state = "completed"
                        reconciled_identity = observed_identity
                    else:
                        require_guard_as_uid1000(spec.value["lease"])
                        reconciled_identity = _replace_env(
                            prior,
                            expected_identity=observed_identity,
                            expected_sha256=sha256_bytes(desired),
                            run_id=f'{spec.value["run_id"]}.rollback',
                        )
                        restored, verified_identity = _stable_read(
                            ENV_PATH,
                            expected_uid=ROOT_UID,
                            expected_gid=ROOT_GID,
                            maximum=MAX_ENV_BYTES,
                            expected_identity=reconciled_identity,
                        )
                        if restored != prior or verified_identity != reconciled_identity:
                            raise BridgeConfigError("failure rollback verification rejected")
                        _discard_retained_exchange_temporary(
                            run_id=spec.value["run_id"],
                            expected_raw=prior,
                            expected_identity=prior_identity,
                        )
                        state = "rolled_back_verified"
            except Exception:
                state = "indeterminate"
                reconciled_identity = None
            failure_report = _report(
                spec,
                state=state,
                common=common,
                prior_identity=prior_identity,
                result_identity=reconciled_identity,
                backup_identity=backup_identity,
                failure_stage=failure_stage,
            )
            try:
                _rewrite_descriptor(report_descriptor, canonical_bytes(failure_report))
                audit.append(
                    state,
                    {
                        **common,
                        "prepared_event_sha256": prepared["event_sha256"],
                        "backup_event_sha256": backup_event_sha256,
                        "prior_env_identity": prior_identity,
                        "reconciled_env_identity": reconciled_identity,
                        "backup_identity": backup_identity,
                        "failure_stage": failure_stage,
                        "report_sha256": sha256_bytes(canonical_bytes(failure_report)),
                    },
                )
            except Exception as audit_exc:
                raise BridgeConfigError(f"bridge config outcome:{state}; audit outcome:indeterminate") from audit_exc
            if state == "completed":
                return failure_report
            raise BridgeConfigError(f"bridge config outcome:{state}") from exc
    finally:
        if report_descriptor is not None:
            os.close(report_descriptor)
        if audit_descriptor is not None:
            os.close(audit_descriptor)
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def _parse_child_report(raw: bytes, spec: Spec) -> dict[str, Any]:
    if not raw or len(raw) > MAX_EVIDENCE_BYTES:
        raise BridgeConfigError("privileged child report boundary rejected")
    try:
        report = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeConfigError("privileged child report parse failed") from exc
    expected_backup_sha = (
        spec.value["expected_env_sha256"]
        if spec.value["operation"] == "apply"
        else spec.value["expected_backup_sha256"]
    )
    expected_common = _common_fields(spec, expected_backup_sha)
    if (
        not isinstance(report, dict)
        or set(report) != REPORT_FIELDS
        or raw != canonical_bytes(report)
        or report.get("contract_version") != REPORT_VERSION
        or report.get("state") != "completed"
        or report.get("failure_stage")
        not in ({None} if spec.value["operation"] == "apply" else {None, "mutation_or_evidence"})
        or any(report.get(name) != value for name, value in expected_common.items())
        or report.get("prior_env_identity") != spec.value["expected_env_identity"]
        or report.get("raw_values_recorded") is not False
        or report.get("service_action_performed") is not False
    ):
        raise BridgeConfigError("privileged child report rejected")
    try:
        result_identity = _validate_identity(
            report.get("result_env_identity"), root_owned=True
        )
        backup_identity = _validate_identity(
            report.get("backup_identity"), root_owned=True
        )
    except BridgeConfigError as exc:
        raise BridgeConfigError("privileged child report identity rejected") from exc
    expected_result_size = (
        spec.value["expected_env_identity"]["size"] + len(SETTING_LINE)
        if spec.value["operation"] == "apply"
        else spec.value["expected_backup_identity"]["size"]
    )
    expected_backup_size = (
        spec.value["expected_env_identity"]["size"]
        if spec.value["operation"] == "apply"
        else spec.value["expected_backup_identity"]["size"]
    )
    if (
        result_identity["size"] != expected_result_size
        or backup_identity["size"] != expected_backup_size
        or (
            spec.value["operation"] == "restore"
            and backup_identity != spec.value["expected_backup_identity"]
        )
    ):
        raise BridgeConfigError("privileged child report file identity differs")
    return report


def _execute_outer(
    spec_path: pathlib.Path, spec_sha256: str, operation: str
) -> dict[str, Any]:
    if os.getuid() != EXPECTED_UID or os.geteuid() != EXPECTED_UID:
        raise BridgeConfigError("outer uid identity rejected")
    spec = Spec.load(spec_path, spec_sha256)
    if operation not in {"apply", "restore"} or spec.value["operation"] != operation:
        raise BridgeConfigError("outer operation differs from spec")
    expected_tool_path = verify_execution_identity(spec)
    require_guard(spec.value["lease"])
    lease = spec.value["lease"]
    inherited = {
        "CHAT_MEMORY_LEASE_ID": lease["lease_id"],
        "CODEX_TASK_ID": lease["task_id"],
        "CODEX_THREAD_ID": lease["thread_id"],
    }
    argv = [
        SUDO,
        "--non-interactive",
        "--preserve-env=CHAT_MEMORY_LEASE_ID,CODEX_TASK_ID,CODEX_THREAD_ID",
        PYTHON,
        str(expected_tool_path),
        f"--execute-{operation}",
        "--privileged-child",
        "--spec",
        str(spec_path),
        "--spec-sha256",
        spec_sha256,
    ]
    result = _run(
        argv,
        env={**inherited, "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        timeout=120,
    )
    if result.returncode != 0 or result.stderr:
        raise BridgeConfigError("privileged child failed")
    return _parse_child_report(result.stdout, spec)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec")
    parser.add_argument("--spec-sha256")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--execute-apply", action="store_true")
    operation.add_argument("--execute-restore", action="store_true")
    parser.add_argument("--privileged-child", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = arguments()
    operation = "apply" if args.execute_apply else "restore" if args.execute_restore else None
    if operation is None or not args.spec or not args.spec_sha256:
        print("bridge configuration execution disabled", file=sys.stderr)
        return 64
    stage = "spec"
    try:
        spec_path = pathlib.Path(args.spec)
        if args.privileged_child:
            spec = Spec.load(spec_path, args.spec_sha256)
            if spec.value["operation"] != operation:
                raise BridgeConfigError("privileged child operation differs from spec")
            stage = "privileged-child"
            report = _execute_child(spec)
        else:
            stage = "outer"
            report = _execute_outer(spec_path, args.spec_sha256, operation)
        sys.stdout.buffer.write(canonical_bytes(report))
        return 0
    except BridgeConfigError:
        print(f"bridge configuration failed:{stage}", file=sys.stderr)
        return 2
    except Exception:
        print(f"bridge configuration failed:{stage}:internal", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

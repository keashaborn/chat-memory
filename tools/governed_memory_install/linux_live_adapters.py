from __future__ import annotations

"""Closed Linux adapters for the dormant governed-memory stores.

The public adapters expose fixed operations, not argv, paths, URLs, resource
names, SQL, or credentials.  Effects occur only when an adapter method is
called.  Construction and import are effect free.  Command, filesystem, and
HTTP primitives are injected so the complete lane can be proven with
synthetic transports before a separately authorized disposable Linux run.
"""

from dataclasses import dataclass
from enum import Enum
import errno
import hashlib
import http.client
import json
import os
import re
import selectors
import socket
import stat
import subprocess
import time
from types import MappingProxyType
from typing import Final, Mapping, Protocol, Sequence

from .execution_lock import (
    HeldExecutionLockCapability,
    validate_held_execution_lock,
)

from .linux_live_transports import (
    DOCKER_NETWORK,
    DOCKER_REQUESTS,
    POSTGRES_CONTAINER,
    POSTGRES_VOLUME,
    QDRANT_COLLECTION,
    QDRANT_CONTAINER,
    QDRANT_REQUESTS,
    QDRANT_VOLUME,
    ROOT_FILE_SLOTS,
    SYSTEMD_ENABLEMENT_PATH,
    SYSTEMD_ENABLEMENT_TARGET,
    SYSTEMD_UNIT_PATH,
    DockerRequestId,
    ObservationState,
    QdrantRequestBytes,
    QdrantRequestId,
    RootFileSlot,
    RootNodeKind,
    RootRemovalIdentity,
    TypedObservation,
)
from .linux_plan import (
    build_store_create_plan,
    canonical_labels_sha256,
    validate_store_spec,
)
from .linux_store_effects import (
    BoundLinuxStoreTransports,
    BoundResourceSnapshot,
    BoundRetainedRootDirectorySnapshot,
    BoundSystemdSupervisorSnapshot,
    EffectPresence,
    ExactInstallArtifacts,
    ExactSystemdSupervisor,
    FilesystemNodeKind,
    LinuxStoreEffectsError,
    LivePreflightSnapshot,
    POSTGRES_STORE_SECRET_PATH,
    QDRANT_STORE_SECRET_PATH,
    SecretEnvironmentDocument,
    secret_environment_public_id,
)
from .psycopg_postgres_adapter import (
    PostgreSQLStageReceiptSink,
    PsycopgPostgreSQLAdapter,
    verified_psycopg_runtime_capability,
)
from .linux_store_readiness import (
    PrebootstrapQdrantSnapshot,
    QdrantCollectionConfiguration,
    TerminalQdrantSnapshot,
)
from .store_readiness import QDRANT_BIND, QDRANT_SERVER_VERSION


SYSTEMCTL_BINARY: Final = "/usr/bin/systemctl"
SYSTEMD_DAEMON_RELOAD_ARGV: Final = (SYSTEMCTL_BINARY, "daemon-reload")
FIXED_COMMAND_ENVIRONMENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        "HOME": "/",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "TZ": "UTC",
    }
)
FIXED_COMMAND_TIMEOUT_SECONDS: Final = 20
MAX_COMMAND_OUTPUT_BYTES: Final = 256 * 1024
MAX_QDRANT_RESPONSE_BYTES: Final = 1024 * 1024
QDRANT_HTTP_HOST: Final = "127.0.0.1"
QDRANT_HTTP_PORT: Final = 6343
QDRANT_HTTP_TIMEOUT_SECONDS: Final = 5
ROLLBACK_CONTROLLER_AUTHORITY_MARKER_PATH_TEMPLATE: Final = ROOT_FILE_SLOTS[
    RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER
].path_template
ROLLBACK_SEMANTIC_EMPTY_PROOF_PATH_TEMPLATE: Final = ROOT_FILE_SLOTS[
    RootFileSlot.ROLLBACK_SEMANTIC_EMPTY_PROOF
].path_template
_ROLLBACK_RECORD_SLOTS: Final = frozenset(
    {
        RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER,
        RootFileSlot.ROLLBACK_SEMANTIC_EMPTY_PROOF,
    }
)

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_EXECUTION_RE = _HASH_RE
_ATTEMPT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_IMAGE_REFERENCE_RE = re.compile(
    r"[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}\Z",
    re.ASCII,
)
_REPO_DIGEST_RE = re.compile(
    r"[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}\Z", re.ASCII
)
_SECRET_RE = re.compile(rb"[A-Za-z0-9_-]{43,128}\Z", re.ASCII)


class LinuxLiveAdapterError(RuntimeError):
    """Content-free refusal from a closed Linux live adapter."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise LinuxLiveAdapterError("linux_live_document_invalid") from None


def _require_canonical_json_document(raw: bytes, *, max_bytes: int) -> None:
    if type(raw) is not bytes or not 1 <= len(raw) <= max_bytes or b"\x00" in raw:
        raise LinuxLiveAdapterError("root_document_invalid")
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LinuxLiveAdapterError("root_document_invalid") from None
    if type(value) is not dict or raw != _canonical_bytes(value) + b"\n":
        raise LinuxLiveAdapterError("root_document_not_canonical")


class CommandDisposition(str, Enum):
    SUCCESS = "success"
    VERIFIED_ABSENT = "verified_absent"


@dataclass(frozen=True, slots=True)
class FixedCommandResult:
    """Normalized command result with no raw stderr surface.

    ``VERIFIED_ABSENT`` is minted only by the injected, reviewed runner after
    it recognizes the exact Docker not-found exit for the exact inspect argv.
    Other non-zero exits and timeouts must raise instead.
    """

    argv: tuple[str, ...]
    disposition: CommandDisposition
    stdout: bytes

    def __post_init__(self) -> None:
        if (
            type(self.argv) is not tuple
            or not self.argv
            or any(type(item) is not str or not item or "\x00" in item for item in self.argv)
            or type(self.disposition) is not CommandDisposition
            or type(self.stdout) is not bytes
            or len(self.stdout) > MAX_COMMAND_OUTPUT_BYTES
            or (
                self.disposition is CommandDisposition.VERIFIED_ABSENT
                and self.stdout != b""
            )
        ):
            raise LinuxLiveAdapterError("fixed_command_result_invalid")


class FixedArgvRunner(Protocol):
    """Execute only the exact argv selected by a closed adapter."""

    def execute(self, argv: tuple[str, ...]) -> FixedCommandResult: ...


class BoundedSubprocessFixedArgvRunner:
    """Shell-free runner restricted to one validated store specification."""

    def __init__(self, resolved_store_spec: Mapping[str, object]) -> None:
        spec = validate_store_spec(resolved_store_spec, allow_placeholders=False)
        create = tuple(step.argv for step in build_store_create_plan(spec))
        immutable = tuple(request.argv for request in DOCKER_REQUESTS.values())
        self._allowed = frozenset(create + immutable + (SYSTEMD_DAEMON_RELOAD_ARGV,))
        self._observations = frozenset(
            request.argv
            for request in DOCKER_REQUESTS.values()
            if request.is_observation
        )
        self._create = frozenset(create)

    @staticmethod
    def _verified_absence(
        argv: tuple[str, ...], stdout: bytes, stderr: bytes
    ) -> bool:
        name = argv[-1]
        if argv[1:3] == ("network", "inspect"):
            expected = (
                "Error response from daemon: network " + name + " not found\n"
            ).encode("utf-8")
        elif argv[1:3] == ("volume", "inspect"):
            expected = (
                "Error response from daemon: get " + name + ": no such volume\n"
            ).encode("utf-8")
        elif argv[1:3] == ("container", "inspect"):
            expected = ("Error: No such container: " + name + "\n").encode(
                "utf-8"
            )
        else:
            return False
        return stderr == expected and stdout in {b"", b"[]\n"}

    @staticmethod
    def _success_output_valid(argv: tuple[str, ...], stdout: bytes) -> bool:
        if argv == SYSTEMD_DAEMON_RELOAD_ARGV:
            return stdout == b""
        if "inspect" in argv:
            return bool(stdout) and len(stdout) <= MAX_COMMAND_OUTPUT_BYTES
        if argv[1:3] == ("volume", "create"):
            return stdout == (argv[-1] + "\n").encode("utf-8")
        if argv[1:3] in {
            ("network", "create"),
            ("container", "rm"),
        } or argv[1] == "create":
            value = stdout[:-1] if stdout.endswith(b"\n") else b""
            if argv[1:3] == ("container", "rm"):
                return stdout == (argv[-1] + "\n").encode("utf-8")
            return re.fullmatch(rb"[0-9a-f]{64}", value) is not None
        if argv[1] in {"start", "stop"}:
            return stdout == (argv[-1] + "\n").encode("utf-8")
        if argv[1:3] in {("volume", "rm"), ("network", "rm")}:
            return stdout == (argv[-1] + "\n").encode("utf-8")
        return False

    @staticmethod
    def _collect(process: subprocess.Popen[bytes]) -> tuple[int, bytes, bytes]:
        if process.stdout is None or process.stderr is None:
            process.kill()
            process.wait()
            raise LinuxLiveAdapterError("fixed_command_pipe_unavailable")
        streams = {"stdout": bytearray(), "stderr": bytearray()}
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + FIXED_COMMAND_TIMEOUT_SECONDS
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LinuxLiveAdapterError("fixed_command_timeout")
                events = selector.select(remaining)
                if not events:
                    raise LinuxLiveAdapterError("fixed_command_timeout")
                for key, _ in events:
                    block = os.read(key.fileobj.fileno(), 8192)
                    if not block:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    streams[key.data].extend(block)
                    if sum(len(item) for item in streams.values()) > MAX_COMMAND_OUTPUT_BYTES:
                        raise LinuxLiveAdapterError(
                            "fixed_command_output_limit_exceeded"
                        )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LinuxLiveAdapterError("fixed_command_timeout")
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise LinuxLiveAdapterError("fixed_command_timeout") from None
            return returncode, bytes(streams["stdout"]), bytes(streams["stderr"])
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()

    def execute(self, argv: tuple[str, ...]) -> FixedCommandResult:
        if type(argv) is not tuple or argv not in self._allowed:
            raise LinuxLiveAdapterError("fixed_command_argv_refused")
        try:
            process = subprocess.Popen(
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env=dict(FIXED_COMMAND_ENVIRONMENT),
                close_fds=True,
            )
        except OSError:
            raise LinuxLiveAdapterError("fixed_command_launch_failed") from None
        returncode, stdout, stderr = self._collect(process)
        if returncode != 0:
            if argv in self._observations and self._verified_absence(
                argv, stdout, stderr
            ):
                return FixedCommandResult(
                    argv, CommandDisposition.VERIFIED_ABSENT, b""
                )
            raise LinuxLiveAdapterError("fixed_command_failed")
        if stderr != b"" or not self._success_output_valid(argv, stdout):
            raise LinuxLiveAdapterError("fixed_command_output_invalid")
        return FixedCommandResult(argv, CommandDisposition.SUCCESS, stdout)


class DescriptorNodeKind(str, Enum):
    ABSENT = "absent"
    REGULAR_FILE = "regular_file"
    SYMBOLIC_LINK = "symbolic_link"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ResolvedRootSlot:
    """One canonical slot resolved only by the bound execution identity."""

    slot: RootFileSlot
    execution_id: str

    def __post_init__(self) -> None:
        if type(self.slot) is not RootFileSlot or _EXECUTION_RE.fullmatch(
            self.execution_id
        ) is None:
            raise LinuxLiveAdapterError("resolved_root_slot_invalid")

    @property
    def path(self) -> str:
        return ROOT_FILE_SLOTS[self.slot].path_template.replace(
            "{execution_id}", self.execution_id
        )

    @property
    def ancestor_paths(self) -> tuple[str, ...]:
        return tuple(
            item.path_template.replace("{execution_id}", self.execution_id)
            for item in ROOT_FILE_SLOTS[self.slot].ancestors
        )


@dataclass(frozen=True, slots=True)
class DescriptorNodeObservation:
    kind: DescriptorNodeKind
    device: int | None = None
    inode: int | None = None
    uid: int | None = None
    gid: int | None = None
    mode: int | None = None
    link_count: int | None = None
    content: bytes | None = None
    symlink_target: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not DescriptorNodeKind:
            raise LinuxLiveAdapterError("descriptor_node_observation_invalid")
        metadata = (
            self.device,
            self.inode,
            self.uid,
            self.gid,
            self.mode,
            self.link_count,
        )
        if self.kind is DescriptorNodeKind.ABSENT:
            if any(value is not None for value in metadata) or any(
                value is not None for value in (self.content, self.symlink_target)
            ):
                raise LinuxLiveAdapterError("descriptor_absence_invalid")
            return
        if (
            any(type(value) is not int for value in metadata)
            or self.device is None
            or self.device <= 0
            or self.inode is None
            or self.inode <= 0
            or self.uid is None
            or self.uid < 0
            or self.gid is None
            or self.gid < 0
            or self.mode is None
            or not 0 <= self.mode <= 0o7777
            or self.link_count is None
            or self.link_count < 1
        ):
            raise LinuxLiveAdapterError("descriptor_metadata_invalid")
        if self.kind is DescriptorNodeKind.REGULAR_FILE:
            if type(self.content) is not bytes or self.symlink_target is not None:
                raise LinuxLiveAdapterError("descriptor_regular_file_invalid")
        elif self.kind is DescriptorNodeKind.SYMBOLIC_LINK:
            if (
                self.content is not None
                or type(self.symlink_target) is not str
                or not self.symlink_target
                or "\x00" in self.symlink_target
            ):
                raise LinuxLiveAdapterError("descriptor_symlink_invalid")
        elif self.content is not None or self.symlink_target is not None:
            raise LinuxLiveAdapterError("descriptor_other_node_invalid")

    @property
    def content_sha256(self) -> str | None:
        return _sha256(self.content) if self.content is not None else None


class RetainedRootDirectory(str, Enum):
    CONTROLLER_CONFIG = "controller_config"
    STORE_SECRET = "store_secret"


_RETAINED_DIRECTORY_PATHS: Final[Mapping[RetainedRootDirectory, str]] = MappingProxyType(
    {
        RetainedRootDirectory.CONTROLLER_CONFIG: "/etc/governed-memory-controller",
        RetainedRootDirectory.STORE_SECRET: (
            "/etc/governed-memory-stores/9a54cf123493-000001"
        ),
    }
)


@dataclass(frozen=True, slots=True)
class DescriptorDirectoryObservation:
    slot: RetainedRootDirectory
    path: str
    device: int
    inode: int
    uid: int
    gid: int
    mode: int

    def __post_init__(self) -> None:
        if (
            type(self.slot) is not RetainedRootDirectory
            or self.path != _RETAINED_DIRECTORY_PATHS[self.slot]
            or type(self.device) is not int
            or self.device <= 0
            or type(self.inode) is not int
            or self.inode <= 0
            or self.uid != 0
            or self.gid != 0
            or self.mode != 0o700
        ):
            raise LinuxLiveAdapterError("descriptor_directory_observation_invalid")


class DescriptorSafeRootFilesystem(Protocol):
    """No-follow, directory-descriptor filesystem primitive.

    Implementations must verify every canonical ancestor, use create-exclusive
    semantics, fsync a created regular file and its parent, compare device and
    inode immediately before unlink, and fsync the parent after create/unlink.
    No implementation may resolve a caller-supplied path.
    """

    def observe(self, slot: ResolvedRootSlot) -> DescriptorNodeObservation: ...

    def create_regular_exclusive(
        self, slot: ResolvedRootSlot, content: bytes
    ) -> DescriptorNodeObservation: ...

    def create_symlink_exclusive(
        self, slot: ResolvedRootSlot
    ) -> DescriptorNodeObservation: ...

    def remove_regular_exact(
        self, slot: ResolvedRootSlot, identity: RootRemovalIdentity
    ) -> None: ...

    def remove_symlink_exact(
        self,
        slot: ResolvedRootSlot,
        *,
        expected_device: int,
        expected_inode: int,
    ) -> None: ...

    def observe_retained_directory(
        self, slot: RetainedRootDirectory
    ) -> DescriptorDirectoryObservation: ...


class PosixDescriptorSafeRootFilesystem:
    """Linux no-follow implementation of ``DescriptorSafeRootFilesystem``."""

    _DIRECTORY_FLAGS: Final = (
        os.O_RDONLY
        | os.O_CLOEXEC
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    _FILE_READ_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)

    def __init__(self) -> None:
        if getattr(os, "O_NOFOLLOW", 0) == 0 or getattr(os, "O_DIRECTORY", 0) == 0:
            raise LinuxLiveAdapterError("root_descriptor_flags_unavailable")

    @staticmethod
    def _validate_directory(fd: int, *, allowed_modes: frozenset[int]) -> os.stat_result:
        observed = os.fstat(fd)
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != 0
            or observed.st_gid != 0
            or stat.S_IMODE(observed.st_mode) not in allowed_modes
        ):
            raise LinuxLiveAdapterError("root_ancestor_invalid")
        return observed

    def _open_parent(self, slot: ResolvedRootSlot) -> int:
        contract = ROOT_FILE_SLOTS[slot.slot]
        ancestors = tuple(
            (
                item.path_template.replace("{execution_id}", slot.execution_id),
                item.allowed_modes,
            )
            for item in contract.ancestors
        )
        if not ancestors or ancestors[0][0] != "/" or ancestors[-1][0] != os.path.dirname(slot.path):
            raise LinuxLiveAdapterError("root_ancestor_contract_invalid")
        current_fd = -1
        try:
            current_fd = os.open("/", self._DIRECTORY_FLAGS)
            self._validate_directory(current_fd, allowed_modes=ancestors[0][1])
            previous = "/"
            for expected_path, allowed_modes in ancestors[1:]:
                prefix = previous.rstrip("/") + "/"
                if not expected_path.startswith(prefix):
                    raise LinuxLiveAdapterError("root_ancestor_contract_invalid")
                relative = expected_path[len(prefix) :]
                if not relative or "/" in relative or relative in {".", ".."}:
                    raise LinuxLiveAdapterError("root_ancestor_contract_invalid")
                next_fd = os.open(relative, self._DIRECTORY_FLAGS, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
                self._validate_directory(current_fd, allowed_modes=allowed_modes)
                previous = expected_path
            return current_fd
        except OSError as error:
            if current_fd >= 0:
                os.close(current_fd)
            raise LinuxLiveAdapterError("root_ancestor_open_failed") from error
        except BaseException:
            if current_fd >= 0:
                os.close(current_fd)
            raise

    @staticmethod
    def _absent() -> DescriptorNodeObservation:
        return DescriptorNodeObservation(DescriptorNodeKind.ABSENT)

    def observe(self, slot: ResolvedRootSlot) -> DescriptorNodeObservation:
        if type(slot) is not ResolvedRootSlot:
            raise LinuxLiveAdapterError("resolved_root_slot_invalid")
        parent_fd = file_fd = -1
        name = os.path.basename(slot.path)
        contract = ROOT_FILE_SLOTS[slot.slot]
        try:
            parent_fd = self._open_parent(slot)
            try:
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return self._absent()
            mode = stat.S_IMODE(named.st_mode)
            metadata = {
                "device": named.st_dev,
                "inode": named.st_ino,
                "uid": named.st_uid,
                "gid": named.st_gid,
                "mode": mode,
                "link_count": named.st_nlink,
            }
            if stat.S_ISREG(named.st_mode):
                file_fd = os.open(name, self._FILE_READ_FLAGS, dir_fd=parent_fd)
                before = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
                    or before.st_size > contract.max_bytes
                ):
                    raise LinuxLiveAdapterError("root_regular_file_changed")
                chunks: list[bytes] = []
                total = 0
                while True:
                    block = os.read(file_fd, min(65536, contract.max_bytes + 1 - total))
                    if not block:
                        break
                    chunks.append(block)
                    total += len(block)
                    if total > contract.max_bytes:
                        raise LinuxLiveAdapterError("root_regular_file_too_large")
                after = os.fstat(file_fd)
                named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    total != before.st_size
                    or (after.st_dev, after.st_ino, after.st_size)
                    != (before.st_dev, before.st_ino, before.st_size)
                    or (named_after.st_dev, named_after.st_ino)
                    != (before.st_dev, before.st_ino)
                ):
                    raise LinuxLiveAdapterError("root_regular_file_changed")
                return DescriptorNodeObservation(
                    DescriptorNodeKind.REGULAR_FILE,
                    **metadata,
                    content=b"".join(chunks),
                )
            if stat.S_ISLNK(named.st_mode):
                target = os.readlink(name, dir_fd=parent_fd)
                after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (after.st_dev, after.st_ino) != (named.st_dev, named.st_ino):
                    raise LinuxLiveAdapterError("root_symlink_changed")
                return DescriptorNodeObservation(
                    DescriptorNodeKind.SYMBOLIC_LINK,
                    **metadata,
                    symlink_target=target,
                )
            return DescriptorNodeObservation(DescriptorNodeKind.OTHER, **metadata)
        except FileNotFoundError:
            return self._absent()
        except OSError as error:
            raise LinuxLiveAdapterError("root_observation_failed") from error
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            if parent_fd >= 0:
                os.close(parent_fd)

    def create_regular_exclusive(
        self, slot: ResolvedRootSlot, content: bytes
    ) -> DescriptorNodeObservation:
        contract = ROOT_FILE_SLOTS[slot.slot]
        if (
            contract.node_kind is not RootNodeKind.REGULAR_FILE
            or type(content) is not bytes
            or not 1 <= len(content) <= contract.max_bytes
        ):
            raise LinuxLiveAdapterError("root_create_request_invalid")
        parent_fd = file_fd = -1
        try:
            parent_fd = self._open_parent(slot)
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0)
            )
            file_fd = os.open(
                os.path.basename(slot.path),
                flags,
                contract.required_mode,
                dir_fd=parent_fd,
            )
            os.fchmod(file_fd, contract.required_mode)
            os.fchown(file_fd, 0, 0)
            view = memoryview(content)
            while view:
                written = os.write(file_fd, view)
                if written <= 0:
                    raise LinuxLiveAdapterError("root_write_failed")
                view = view[written:]
            os.fsync(file_fd)
            os.close(file_fd)
            file_fd = -1
            os.fsync(parent_fd)
        except OSError as error:
            raise LinuxLiveAdapterError("root_create_failed") from error
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            if parent_fd >= 0:
                os.close(parent_fd)
        return self.observe(slot)

    def create_symlink_exclusive(
        self, slot: ResolvedRootSlot
    ) -> DescriptorNodeObservation:
        contract = ROOT_FILE_SLOTS[slot.slot]
        if contract.node_kind is not RootNodeKind.SYMBOLIC_LINK:
            raise LinuxLiveAdapterError("root_symlink_create_request_invalid")
        parent_fd = -1
        try:
            parent_fd = self._open_parent(slot)
            os.symlink(
                contract.symlink_target,
                os.path.basename(slot.path),
                dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        except OSError as error:
            raise LinuxLiveAdapterError("root_symlink_create_failed") from error
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)
        return self.observe(slot)

    def remove_regular_exact(
        self, slot: ResolvedRootSlot, identity: RootRemovalIdentity
    ) -> None:
        if type(identity) is not RootRemovalIdentity or identity.slot is not slot.slot or identity.execution_id != slot.execution_id:
            raise LinuxLiveAdapterError("root_remove_identity_invalid")
        parent_fd = -1
        try:
            parent_fd = self._open_parent(slot)
            current = self.observe(slot)
            if (
                current.kind is not DescriptorNodeKind.REGULAR_FILE
                or (current.device, current.inode, current.content_sha256)
                != (identity.device, identity.inode, identity.content_sha256)
                or current.uid != 0
                or current.gid != 0
                or current.link_count != 1
            ):
                raise LinuxLiveAdapterError("root_remove_identity_changed")
            named = os.stat(
                os.path.basename(slot.path), dir_fd=parent_fd, follow_symlinks=False
            )
            if (named.st_dev, named.st_ino) != (identity.device, identity.inode):
                raise LinuxLiveAdapterError("root_remove_identity_changed")
            os.unlink(os.path.basename(slot.path), dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as error:
            raise LinuxLiveAdapterError("root_remove_failed") from error
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)

    def remove_symlink_exact(
        self,
        slot: ResolvedRootSlot,
        *,
        expected_device: int,
        expected_inode: int,
    ) -> None:
        parent_fd = -1
        try:
            parent_fd = self._open_parent(slot)
            current = self.observe(slot)
            if (
                current.kind is not DescriptorNodeKind.SYMBOLIC_LINK
                or (current.device, current.inode) != (expected_device, expected_inode)
                or current.symlink_target != ROOT_FILE_SLOTS[slot.slot].symlink_target
                or current.uid != 0
                or current.gid != 0
                or current.link_count != 1
            ):
                raise LinuxLiveAdapterError("root_symlink_remove_identity_changed")
            named = os.stat(
                os.path.basename(slot.path), dir_fd=parent_fd, follow_symlinks=False
            )
            if (named.st_dev, named.st_ino) != (expected_device, expected_inode):
                raise LinuxLiveAdapterError("root_symlink_remove_identity_changed")
            os.unlink(os.path.basename(slot.path), dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as error:
            raise LinuxLiveAdapterError("root_symlink_remove_failed") from error
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)

    def observe_retained_directory(
        self, slot: RetainedRootDirectory
    ) -> DescriptorDirectoryObservation:
        if type(slot) is not RetainedRootDirectory:
            raise LinuxLiveAdapterError("retained_directory_slot_invalid")
        representative = ResolvedRootSlot(
            RootFileSlot.RESOLVED_STORE_SPEC
            if slot is RetainedRootDirectory.CONTROLLER_CONFIG
            else RootFileSlot.POSTGRES_SECRET,
            "0" * 64,
        )
        fd = -1
        try:
            fd = self._open_parent(representative)
            observed = os.fstat(fd)
            return DescriptorDirectoryObservation(
                slot,
                _RETAINED_DIRECTORY_PATHS[slot],
                observed.st_dev,
                observed.st_ino,
                observed.st_uid,
                observed.st_gid,
                stat.S_IMODE(observed.st_mode),
            )
        except OSError as error:
            raise LinuxLiveAdapterError("retained_directory_observation_failed") from error
        finally:
            if fd >= 0:
                os.close(fd)


@dataclass(frozen=True, slots=True)
class ExactRootRecordObservation:
    observation: TypedObservation
    identity: RootRemovalIdentity | None

    def __post_init__(self) -> None:
        if type(self.observation) is not TypedObservation or (
            (self.observation.state is ObservationState.EXACT)
            != (type(self.identity) is RootRemovalIdentity)
        ):
            raise LinuxLiveAdapterError("rollback_controller_authority_marker_observation_invalid")


class ClosedRootFileEffects:
    """Exact install files plus an execution-bound rollback authority marker."""

    def __init__(
        self,
        *,
        filesystem: DescriptorSafeRootFilesystem,
        execution_id: str,
        resolved_store_spec_sha256: str,
        authority_execution_id: str | None = None,
    ) -> None:
        authority_id = (
            execution_id
            if authority_execution_id is None
            else authority_execution_id
        )
        if (
            not all(
                callable(getattr(filesystem, method, None))
                for method in (
                    "observe",
                    "create_regular_exclusive",
                    "create_symlink_exclusive",
                    "remove_regular_exact",
                    "remove_symlink_exact",
                    "observe_retained_directory",
                )
            )
            or _EXECUTION_RE.fullmatch(execution_id) is None
            or _EXECUTION_RE.fullmatch(authority_id) is None
            or _HASH_RE.fullmatch(resolved_store_spec_sha256) is None
        ):
            raise LinuxLiveAdapterError("closed_root_file_dependencies_invalid")
        self._filesystem = filesystem
        self._execution_id = execution_id
        self._authority_execution_id = authority_id
        self._resolved_store_spec_sha256 = resolved_store_spec_sha256

    def _slot(self, slot: RootFileSlot) -> ResolvedRootSlot:
        execution_id = (
            self._authority_execution_id
            if slot in _ROLLBACK_RECORD_SLOTS
            else self._execution_id
        )
        return ResolvedRootSlot(slot, execution_id)

    def for_rollback_authority(
        self, authority_execution_id: str
    ) -> ClosedRootFileEffects:
        """Rebind only rollback records, retaining install-file identity."""

        return ClosedRootFileEffects(
            filesystem=self._filesystem,
            execution_id=self._execution_id,
            resolved_store_spec_sha256=self._resolved_store_spec_sha256,
            authority_execution_id=authority_execution_id,
        )

    @staticmethod
    def _regular_exact(
        observed: DescriptorNodeObservation, *, mode: int, max_bytes: int
    ) -> bool:
        return (
            observed.kind is DescriptorNodeKind.REGULAR_FILE
            and observed.uid == 0
            and observed.gid == 0
            and observed.mode == mode
            and observed.link_count == 1
            and type(observed.content) is bytes
            and len(observed.content) <= max_bytes
        )

    def _observe_regular(self, slot: RootFileSlot) -> DescriptorNodeObservation:
        observed = self._filesystem.observe(self._slot(slot))
        if type(observed) is not DescriptorNodeObservation:
            raise LinuxLiveAdapterError("root_observation_invalid")
        return observed

    def _snapshot_for_hash(
        self, slot: RootFileSlot, expected_sha256: str
    ) -> BoundResourceSnapshot:
        observed = self._observe_regular(slot)
        if observed.kind is DescriptorNodeKind.ABSENT:
            return BoundResourceSnapshot(EffectPresence.ABSENT)
        contract = ROOT_FILE_SLOTS[slot]
        if (
            not self._regular_exact(
                observed,
                mode=contract.required_mode,
                max_bytes=contract.max_bytes,
            )
            or observed.content_sha256 != expected_sha256
        ):
            return BoundResourceSnapshot(EffectPresence.DRIFT)
        return BoundResourceSnapshot(EffectPresence.EXACT, expected_sha256)

    def _create_regular(self, slot: RootFileSlot, content: bytes) -> DescriptorNodeObservation:
        observed = self._filesystem.create_regular_exclusive(self._slot(slot), content)
        contract = ROOT_FILE_SLOTS[slot]
        if (
            type(observed) is not DescriptorNodeObservation
            or not self._regular_exact(
                observed,
                mode=contract.required_mode,
                max_bytes=contract.max_bytes,
            )
            or observed.content != content
        ):
            raise LinuxLiveAdapterError("root_create_not_observed")
        return observed

    def _identity(
        self, slot: RootFileSlot, observed: DescriptorNodeObservation
    ) -> RootRemovalIdentity:
        if (
            observed.device is None
            or observed.inode is None
            or observed.content_sha256 is None
        ):
            raise LinuxLiveAdapterError("root_identity_absent")
        return RootRemovalIdentity(
            slot,
            observed.device,
            observed.inode,
            observed.content_sha256,
            self._execution_id,
        )

    def _remove_regular(self, slot: RootFileSlot, expected_resource_id: str) -> None:
        observed = self._observe_regular(slot)
        if observed.kind is DescriptorNodeKind.ABSENT:
            return
        contract = ROOT_FILE_SLOTS[slot]
        if not self._regular_exact(
            observed, mode=contract.required_mode, max_bytes=contract.max_bytes
        ):
            raise LinuxLiveAdapterError("root_remove_drift_refused")
        identity = self._identity(slot, observed)
        if slot is RootFileSlot.RESOLVED_STORE_SPEC:
            if identity.content_sha256 != expected_resource_id:
                raise LinuxLiveAdapterError("root_remove_drift_refused")
        elif slot in {RootFileSlot.POSTGRES_SECRET, RootFileSlot.QDRANT_SECRET}:
            if self._secret_resource_id(slot, observed.content) != expected_resource_id:
                raise LinuxLiveAdapterError("root_remove_drift_refused")
        self._filesystem.remove_regular_exact(self._slot(slot), identity)
        if self._filesystem.observe(self._slot(slot)).kind is not DescriptorNodeKind.ABSENT:
            raise LinuxLiveAdapterError("root_remove_not_observed")

    def observe_retained_parent_directories(
        self,
    ) -> tuple[BoundRetainedRootDirectorySnapshot, ...]:
        result: list[BoundRetainedRootDirectorySnapshot] = []
        for slot in (
            RetainedRootDirectory.CONTROLLER_CONFIG,
            RetainedRootDirectory.STORE_SECRET,
        ):
            observed = self._filesystem.observe_retained_directory(slot)
            if type(observed) is not DescriptorDirectoryObservation:
                raise LinuxLiveAdapterError("retained_directory_observation_invalid")
            result.append(
                BoundRetainedRootDirectorySnapshot(
                    observed.path,
                    "root",
                    "root",
                    observed.mode,
                    True,
                    False,
                )
            )
        return result[0], result[1]

    def observe_resolved_store_spec(self) -> BoundResourceSnapshot:
        return self._snapshot_for_hash(
            RootFileSlot.RESOLVED_STORE_SPEC,
            self._resolved_store_spec_sha256,
        )

    def _secret_resource_id(
        self, slot: RootFileSlot, content: bytes | None
    ) -> str | None:
        if type(content) is not bytes:
            return None
        header = (
            b"# governed-memory-execution-id="
            + self._execution_id.encode("ascii")
            + b"\n"
        )
        if slot is RootFileSlot.POSTGRES_SECRET:
            prefix = header + b"POSTGRES_DB=postgres\nPOSTGRES_PASSWORD="
            suffix = b"\nPOSTGRES_USER=governed_memory_bootstrap\n"
            if not content.startswith(prefix) or not content.endswith(suffix):
                return None
            secret = content[len(prefix) : -len(suffix)]
            path = POSTGRES_STORE_SECRET_PATH
        elif slot is RootFileSlot.QDRANT_SECRET:
            prefix = header + b"QDRANT__SERVICE__API_KEY="
            if not content.startswith(prefix) or not content.endswith(b"\n"):
                return None
            secret = content[len(prefix) : -1]
            path = QDRANT_STORE_SECRET_PATH
        else:
            return None
        if _SECRET_RE.fullmatch(secret) is None:
            return None
        return secret_environment_public_id(path, self._execution_id)

    def _observe_secret(self, slot: RootFileSlot) -> BoundResourceSnapshot:
        observed = self._observe_regular(slot)
        if observed.kind is DescriptorNodeKind.ABSENT:
            return BoundResourceSnapshot(EffectPresence.ABSENT)
        contract = ROOT_FILE_SLOTS[slot]
        resource_id = self._secret_resource_id(slot, observed.content)
        if (
            not self._regular_exact(
                observed,
                mode=contract.required_mode,
                max_bytes=contract.max_bytes,
            )
            or resource_id is None
        ):
            return BoundResourceSnapshot(EffectPresence.DRIFT)
        return BoundResourceSnapshot(EffectPresence.EXACT, resource_id)

    def observe_postgres_secret(self) -> BoundResourceSnapshot:
        return self._observe_secret(RootFileSlot.POSTGRES_SECRET)

    def observe_qdrant_secret(self) -> BoundResourceSnapshot:
        return self._observe_secret(RootFileSlot.QDRANT_SECRET)

    def _read_fixed_secret(self, slot: RootFileSlot) -> bytes:
        """Read one execution-bound secret through the descriptor-safe root.

        This is deliberately narrower than an environment-file reader: the
        caller cannot select a path, variable name, or execution identity.
        The exact root-owned file shape is revalidated on every read and only
        the single secret value crosses the adapter boundary.
        """

        observed = self._observe_regular(slot)
        contract = ROOT_FILE_SLOTS[slot]
        if (
            not self._regular_exact(
                observed,
                mode=contract.required_mode,
                max_bytes=contract.max_bytes,
            )
            or self._secret_resource_id(slot, observed.content) is None
            or type(observed.content) is not bytes
        ):
            raise LinuxLiveAdapterError("store_secret_not_exact")
        header = (
            b"# governed-memory-execution-id="
            + self._execution_id.encode("ascii")
            + b"\n"
        )
        if slot is RootFileSlot.POSTGRES_SECRET:
            prefix = header + b"POSTGRES_DB=postgres\nPOSTGRES_PASSWORD="
            suffix = b"\nPOSTGRES_USER=governed_memory_bootstrap\n"
        elif slot is RootFileSlot.QDRANT_SECRET:
            prefix = header + b"QDRANT__SERVICE__API_KEY="
            suffix = b"\n"
        else:
            raise LinuxLiveAdapterError("store_secret_slot_invalid")
        value = observed.content[len(prefix) : -len(suffix)]
        if _SECRET_RE.fullmatch(value) is None:
            raise LinuxLiveAdapterError("store_secret_not_exact")
        return value

    def read_fixed_postgres_password(self) -> bytes:
        return self._read_fixed_secret(RootFileSlot.POSTGRES_SECRET)

    def read_qdrant_api_key(self) -> bytes:
        return self._read_fixed_secret(RootFileSlot.QDRANT_SECRET)

    def create_resolved_store_spec(self, canonical_document: bytes) -> None:
        _require_canonical_json_document(
            canonical_document,
            max_bytes=ROOT_FILE_SLOTS[RootFileSlot.RESOLVED_STORE_SPEC].max_bytes,
        )
        if _sha256(canonical_document[:-1]) != self._resolved_store_spec_sha256:
            raise LinuxLiveAdapterError("resolved_store_spec_identity_mismatch")
        self._create_regular(RootFileSlot.RESOLVED_STORE_SPEC, canonical_document)

    def _create_secret(
        self, slot: RootFileSlot, document: SecretEnvironmentDocument
    ) -> None:
        if type(document) is not SecretEnvironmentDocument:
            raise LinuxLiveAdapterError("secret_document_invalid")
        expected_logical = (
            "postgres" if slot is RootFileSlot.POSTGRES_SECRET else "qdrant"
        )
        if document.logical_name != expected_logical or document.execution_id != self._execution_id:
            raise LinuxLiveAdapterError("secret_document_binding_mismatch")
        content = document._render_for_root_writer_only()
        if self._secret_resource_id(slot, content) is None:
            raise LinuxLiveAdapterError("secret_document_invalid")
        self._create_regular(slot, content)

    def create_postgres_secret(self, document: SecretEnvironmentDocument) -> None:
        self._create_secret(RootFileSlot.POSTGRES_SECRET, document)

    def create_qdrant_secret(self, document: SecretEnvironmentDocument) -> None:
        self._create_secret(RootFileSlot.QDRANT_SECRET, document)

    def remove_resolved_store_spec(self) -> None:
        self._remove_regular(
            RootFileSlot.RESOLVED_STORE_SPEC,
            self._resolved_store_spec_sha256,
        )

    def remove_postgres_secret(self) -> None:
        self._remove_regular(
            RootFileSlot.POSTGRES_SECRET,
            secret_environment_public_id(
                POSTGRES_STORE_SECRET_PATH, self._execution_id
            ),
        )

    def remove_qdrant_secret(self) -> None:
        self._remove_regular(
            RootFileSlot.QDRANT_SECRET,
            secret_environment_public_id(QDRANT_STORE_SECRET_PATH, self._execution_id),
        )

    def read_terminal_postflight_receipt(self) -> bytes | None:
        observed = self._observe_regular(RootFileSlot.TERMINAL_POSTFLIGHT_RECEIPT)
        if observed.kind is DescriptorNodeKind.ABSENT:
            return None
        contract = ROOT_FILE_SLOTS[RootFileSlot.TERMINAL_POSTFLIGHT_RECEIPT]
        if not self._regular_exact(
            observed, mode=contract.required_mode, max_bytes=contract.max_bytes
        ):
            raise LinuxLiveAdapterError("postflight_receipt_drift")
        return observed.content

    def create_terminal_postflight_receipt(self, canonical_document: bytes) -> None:
        _require_canonical_json_document(
            canonical_document,
            max_bytes=ROOT_FILE_SLOTS[
                RootFileSlot.TERMINAL_POSTFLIGHT_RECEIPT
            ].max_bytes,
        )
        self._create_regular(
            RootFileSlot.TERMINAL_POSTFLIGHT_RECEIPT, canonical_document
        )

    @property
    def rollback_controller_authority_marker_path(self) -> str:
        return self._slot(RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER).path

    @property
    def rollback_semantic_empty_proof_path(self) -> str:
        return self._slot(RootFileSlot.ROLLBACK_SEMANTIC_EMPTY_PROOF).path

    def observe_execution_record(
        self,
        slot: RootFileSlot,
        expected_content_sha256: str,
    ) -> ExactRootRecordObservation:
        if slot not in _ROLLBACK_RECORD_SLOTS:
            raise LinuxLiveAdapterError("execution_record_slot_invalid")
        if _HASH_RE.fullmatch(expected_content_sha256) is None:
            raise LinuxLiveAdapterError("execution_record_hash_invalid")
        path = self._slot(slot).path
        observed = self._observe_regular(slot)
        evidence = _sha256(
            _canonical_bytes(
                {
                    "path": path,
                    "state": observed.kind.value,
                }
            )
        )
        if observed.kind is DescriptorNodeKind.ABSENT:
            return ExactRootRecordObservation(
                TypedObservation(
                    ObservationState.ABSENT, "verified_absent", evidence
                ),
                None,
            )
        contract = ROOT_FILE_SLOTS[slot]
        identity_sha256 = _sha256(
            _canonical_bytes(
                {
                    "content_sha256": observed.content_sha256,
                    "device": observed.device,
                    "inode": observed.inode,
                    "path": path,
                }
            )
        )
        exact = (
            self._regular_exact(
                observed,
                mode=contract.required_mode,
                max_bytes=contract.max_bytes,
            )
            and observed.content_sha256 == expected_content_sha256
        )
        if not exact:
            return ExactRootRecordObservation(
                TypedObservation(
                    ObservationState.DRIFT,
                    "verified_drift",
                    evidence,
                    identity_sha256,
                ),
                None,
            )
        identity = self._identity(slot, observed)
        return ExactRootRecordObservation(
            TypedObservation(
                ObservationState.EXACT,
                "verified_exact",
                evidence,
                identity_sha256,
            ),
            identity,
        )

    def create_execution_record(
        self, slot: RootFileSlot, canonical_document: bytes
    ) -> RootRemovalIdentity:
        if slot not in _ROLLBACK_RECORD_SLOTS:
            raise LinuxLiveAdapterError("execution_record_slot_invalid")
        _require_canonical_json_document(
            canonical_document,
            max_bytes=ROOT_FILE_SLOTS[slot].max_bytes,
        )
        try:
            document = json.loads(canonical_document.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LinuxLiveAdapterError("execution_record_document_invalid") from None
        if document.get("execution_id") != self._authority_execution_id:
            raise LinuxLiveAdapterError("execution_record_execution_mismatch")
        observed = self._create_regular(slot, canonical_document)
        return self._identity(slot, observed)

    def read_execution_record(
        self, slot: RootFileSlot, expected_content_sha256: str
    ) -> bytes | None:
        observation = self.observe_execution_record(
            slot, expected_content_sha256
        )
        if observation.observation.state is ObservationState.ABSENT:
            return None
        if observation.observation.state is not ObservationState.EXACT:
            raise LinuxLiveAdapterError("execution_record_drift_refused")
        observed = self._observe_regular(slot)
        if (
            observation.identity is None
            or observed.device != observation.identity.device
            or observed.inode != observation.identity.inode
            or observed.content_sha256
            != observation.identity.content_sha256
            or type(observed.content) is not bytes
        ):
            raise LinuxLiveAdapterError("execution_record_changed_during_read")
        return observed.content

    def remove_execution_record(self, identity: RootRemovalIdentity) -> None:
        if (
            type(identity) is not RootRemovalIdentity
            or identity.slot not in _ROLLBACK_RECORD_SLOTS
            or identity.execution_id != self._authority_execution_id
        ):
            raise LinuxLiveAdapterError("execution_record_identity_invalid")
        current = self.observe_execution_record(
            identity.slot, identity.content_sha256
        )
        if (
            current.observation.state is not ObservationState.EXACT
            or current.identity != identity
        ):
            raise LinuxLiveAdapterError("execution_record_identity_changed")
        self._filesystem.remove_regular_exact(
            self._slot(identity.slot), identity
        )
        final = self.observe_execution_record(
            identity.slot, identity.content_sha256
        )
        if final.observation.state is not ObservationState.ABSENT:
            raise LinuxLiveAdapterError("execution_record_remove_not_observed")

    def observe_rollback_controller_authority_marker(
        self, expected_content_sha256: str
    ) -> ExactRootRecordObservation:
        return self.observe_execution_record(
            RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER,
            expected_content_sha256,
        )

    def create_rollback_controller_authority_marker(
        self, canonical_document: bytes
    ) -> RootRemovalIdentity:
        return self.create_execution_record(
            RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER,
            canonical_document,
        )

    def read_rollback_controller_authority_marker(
        self, expected_content_sha256: str
    ) -> bytes | None:
        return self.read_execution_record(
            RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER,
            expected_content_sha256,
        )

    def remove_rollback_controller_authority_marker(
        self, identity: RootRemovalIdentity
    ) -> None:
        if identity.slot is not RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER:
            raise LinuxLiveAdapterError("rollback_controller_authority_marker_identity_invalid")
        self.remove_execution_record(identity)

    def observe_rollback_semantic_empty_proof(
        self, expected_content_sha256: str
    ) -> ExactRootRecordObservation:
        return self.observe_execution_record(
            RootFileSlot.ROLLBACK_SEMANTIC_EMPTY_PROOF,
            expected_content_sha256,
        )

    def create_rollback_semantic_empty_proof(
        self, canonical_document: bytes
    ) -> RootRemovalIdentity:
        return self.create_execution_record(
            RootFileSlot.ROLLBACK_SEMANTIC_EMPTY_PROOF,
            canonical_document,
        )

    def read_rollback_semantic_empty_proof(
        self, expected_content_sha256: str
    ) -> bytes | None:
        return self.read_execution_record(
            RootFileSlot.ROLLBACK_SEMANTIC_EMPTY_PROOF,
            expected_content_sha256,
        )

    def remove_rollback_semantic_empty_proof(
        self, identity: RootRemovalIdentity
    ) -> None:
        if identity.slot is not RootFileSlot.ROLLBACK_SEMANTIC_EMPTY_PROOF:
            raise LinuxLiveAdapterError(
                "rollback_semantic_empty_proof_identity_invalid"
            )
        self.remove_execution_record(identity)


def bind_closed_root_files(
    *,
    filesystem: DescriptorSafeRootFilesystem,
    execution_id: str,
    resolved_store_spec_sha256: str,
) -> ClosedRootFileEffects:
    """Bind install and rollback files without exposing a path parameter."""

    return ClosedRootFileEffects(
        filesystem=filesystem,
        execution_id=execution_id,
        resolved_store_spec_sha256=resolved_store_spec_sha256,
    )


@dataclass(frozen=True, slots=True)
class FixedImageIdentity:
    logical_name: str
    image_id: str
    reference: str
    repo_digest: str

    def __post_init__(self) -> None:
        if (
            self.logical_name not in {"postgres", "qdrant"}
            or _IMAGE_ID_RE.fullmatch(self.image_id) is None
            or _IMAGE_REFERENCE_RE.fullmatch(self.reference) is None
            or _REPO_DIGEST_RE.fullmatch(self.repo_digest) is None
            or not self.reference.endswith("@" + self.repo_digest.split("@", 1)[1])
        ):
            raise LinuxLiveAdapterError("fixed_image_identity_invalid")


def _run_exact(
    runner: FixedArgvRunner,
    argv: tuple[str, ...],
    *,
    allow_absent: bool = False,
) -> FixedCommandResult:
    try:
        result = runner.execute(argv)
    except Exception:
        raise LinuxLiveAdapterError("fixed_command_failed") from None
    if type(result) is not FixedCommandResult or result.argv != argv:
        raise LinuxLiveAdapterError("fixed_command_result_mismatch")
    if (
        result.disposition is CommandDisposition.VERIFIED_ABSENT
        and not allow_absent
    ):
        raise LinuxLiveAdapterError("fixed_command_unexpected_absence")
    return result


def _parse_projection(
    stdout: bytes, fields: tuple[str, ...]
) -> tuple[object, ...]:
    if stdout.endswith(b"\n"):
        stdout = stdout[:-1]
    if not stdout or b"\n" in stdout or b"\r" in stdout:
        raise LinuxLiveAdapterError("docker_projection_invalid")
    parts = stdout.split(b"\t")
    if len(parts) != len(fields):
        raise LinuxLiveAdapterError("docker_projection_shape_invalid")
    try:
        return tuple(json.loads(part.decode("utf-8")) for part in parts)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LinuxLiveAdapterError("docker_projection_json_invalid") from None


class ClosedDockerEffects:
    """Exact resolved-spec Docker operations over one fixed command runner."""

    def __init__(
        self,
        *,
        runner: FixedArgvRunner,
        resolved_store_spec: Mapping[str, object],
        images: tuple[FixedImageIdentity, FixedImageIdentity],
    ) -> None:
        spec = validate_store_spec(resolved_store_spec, allow_placeholders=False)
        if (
            not callable(getattr(runner, "execute", None))
            or type(images) is not tuple
            or tuple(item.logical_name for item in images) != ("postgres", "qdrant")
        ):
            raise LinuxLiveAdapterError("closed_docker_dependencies_invalid")
        resources = spec["resources"]
        if (
            resources["network"]["name"] != DOCKER_NETWORK
            or resources["volumes"]["postgres"]["name"] != POSTGRES_VOLUME
            or resources["volumes"]["qdrant"]["name"] != QDRANT_VOLUME
            or resources["containers"]["postgres"]["name"] != POSTGRES_CONTAINER
            or resources["containers"]["qdrant"]["name"] != QDRANT_CONTAINER
        ):
            raise LinuxLiveAdapterError("closed_docker_resource_identity_invalid")
        for image in images:
            expected = resources["containers"][image.logical_name]["image"]
            if (
                image.reference != expected["reference"]
                or image.repo_digest != expected["repo_digest"]
            ):
                raise LinuxLiveAdapterError("closed_docker_image_binding_invalid")
        self._runner = runner
        self._spec = spec
        self._images = {item.logical_name: item for item in images}
        create_plan = build_store_create_plan(spec)
        self._create_argv = MappingProxyType(
            {step.step_id: step.argv for step in create_plan}
        )

    def _observe_values(self, request_id: DockerRequestId) -> tuple[object, ...] | None:
        request = DOCKER_REQUESTS[request_id]
        result = _run_exact(self._runner, request.argv, allow_absent=True)
        if result.disposition is CommandDisposition.VERIFIED_ABSENT:
            return None
        return _parse_projection(result.stdout, request.projected_fields)

    @staticmethod
    def _labels(
        values: tuple[object, ...], start: int, expected: Mapping[str, str]
    ) -> bool:
        label_values = values[start : start + len(expected)]
        return (
            len(label_values) == len(expected)
            and dict(zip(sorted(expected), label_values, strict=True))
            == dict(sorted(expected.items()))
        )

    def _observe_network(self) -> BoundResourceSnapshot:
        values = self._observe_values(DockerRequestId.OBSERVE_NETWORK)
        if values is None:
            return BoundResourceSnapshot(EffectPresence.ABSENT)
        expected = self._spec["resources"]["network"]
        name, resource_id, driver, internal, attachable, label_count, *labels = values
        exact = (
            name == DOCKER_NETWORK
            and type(resource_id) is str
            and _CONTAINER_ID_RE.fullmatch(resource_id) is not None
            and driver == "bridge"
            and internal is True
            and attachable is False
            and type(label_count) is int
            and label_count == len(expected["labels"])
            and self._labels(tuple(labels), 0, expected["labels"])
        )
        if not exact:
            return BoundResourceSnapshot(EffectPresence.DRIFT)
        return BoundResourceSnapshot(
            EffectPresence.EXACT,
            resource_id,
            canonical_labels_sha256(expected["labels"]),
        )

    def observe_network(self) -> BoundResourceSnapshot:
        return self._observe_network()

    def _observe_volume(self, logical_name: str) -> BoundResourceSnapshot:
        request_id = (
            DockerRequestId.OBSERVE_POSTGRES_VOLUME
            if logical_name == "postgres"
            else DockerRequestId.OBSERVE_QDRANT_VOLUME
        )
        values = self._observe_values(request_id)
        if values is None:
            return BoundResourceSnapshot(EffectPresence.ABSENT)
        expected = self._spec["resources"]["volumes"][logical_name]
        name, driver, mountpoint, scope, option_count, label_count, *labels = values
        exact = (
            name == expected["name"]
            and driver == "local"
            and type(mountpoint) is str
            and mountpoint.startswith("/")
            and "\x00" not in mountpoint
            and scope == "local"
            and type(option_count) is int
            and option_count == 0
            and type(label_count) is int
            and label_count == len(expected["labels"])
            and self._labels(tuple(labels), 0, expected["labels"])
        )
        if not exact:
            return BoundResourceSnapshot(EffectPresence.DRIFT)
        return BoundResourceSnapshot(
            EffectPresence.EXACT,
            name,
            canonical_labels_sha256(expected["labels"]),
        )

    def observe_postgres_volume(self) -> BoundResourceSnapshot:
        return self._observe_volume("postgres")

    def observe_qdrant_volume(self) -> BoundResourceSnapshot:
        return self._observe_volume("qdrant")

    def _observe_container(self, logical_name: str) -> BoundResourceSnapshot:
        request_id = (
            DockerRequestId.OBSERVE_POSTGRES_CONTAINER
            if logical_name == "postgres"
            else DockerRequestId.OBSERVE_QDRANT_CONTAINER
        )
        values = self._observe_values(request_id)
        if values is None:
            return BoundResourceSnapshot(EffectPresence.ABSENT)
        expected = self._spec["resources"]["containers"][logical_name]
        image = self._images[logical_name]
        label_count = len(expected["labels"])
        try:
            (
                name,
                container_id,
                image_id,
                image_reference,
                command,
                health_count,
                health_first,
                network_mode,
                port_count,
                port_binding,
                unrelated_binding,
                cap_add,
                cap_drop,
                security_opt,
                readonly_rootfs,
                restart_name,
                restart_retry,
                pids_limit,
                log_driver,
                log_option_count,
                log_max_file,
                log_max_size,
                tmpfs_count,
                tmpfs_value,
                mount_count,
                mount_type,
                mount_name,
                mount_destination,
                mount_rw,
                status,
                running,
                paused,
                restarting,
                dead,
                oom_killed,
                runtime_port_count,
                runtime_port_binding,
                unrelated_runtime_binding,
                network_count,
                network_id,
                observed_label_count,
                *label_values,
            ) = values
        except ValueError:
            raise LinuxLiveAdapterError("docker_container_projection_invalid") from None
        publish = expected["publish"]
        expected_binding = [
            {"HostIp": publish["host"], "HostPort": str(publish["host_port"])}
        ]
        expected_security = expected["security"]
        expected_logging = expected["logging"]
        expected_command = tuple(expected["command"])
        observed_command = (
            tuple(command or ()) if type(command) in {list, type(None)} else None
        )
        network = self._observe_network()
        exact = (
            name == "/" + expected["name"]
            and type(container_id) is str
            and _CONTAINER_ID_RE.fullmatch(container_id) is not None
            and image_id == image.image_id
            and image_reference == image.reference
            and observed_command == expected_command
            and type(health_count) is int
            and health_count == 1
            and health_first == "NONE"
            and network_mode == DOCKER_NETWORK
            and type(port_count) is int
            and port_count == 1
            and port_binding == expected_binding
            and unrelated_binding is None
            and type(cap_add) in {list, type(None)}
            and tuple(cap_add or ()) == tuple(expected_security["cap_add"])
            and type(cap_drop) in {list, type(None)}
            and tuple(cap_drop or ()) == tuple(expected_security["cap_drop"])
            and type(security_opt) in {list, type(None)}
            and tuple(security_opt or ()) == ("no-new-privileges",)
            and readonly_rootfs is False
            and restart_name == "no"
            and type(restart_retry) is int
            and restart_retry == 0
            and type(pids_limit) is int
            and pids_limit == 256
            and log_driver == "json-file"
            and type(log_option_count) is int
            and log_option_count == 2
            and log_max_file == str(expected_logging["max_file"])
            and log_max_size == expected_logging["max_size"]
            and type(tmpfs_count) is int
            and tmpfs_count == 1
            and tmpfs_value == "rw,nosuid,nodev,noexec,size=64m"
            and type(mount_count) is int
            and mount_count == 1
            and mount_type == "volume"
            and mount_name == expected["volume_name"]
            and mount_destination == expected["volume_target"]
            and mount_rw is True
            and type(running) is bool
            and type(status) is str
            and status == ("running" if running else status)
            and (running or status in {"created", "exited"})
            and paused is False
            and restarting is False
            and dead is False
            and oom_killed is False
            and type(runtime_port_count) is int
            and runtime_port_count == 1
            and runtime_port_binding == expected_binding
            and unrelated_runtime_binding is None
            and type(network_count) is int
            and network_count == 1
            and network.presence is EffectPresence.EXACT
            and network_id == network.resource_id
            and type(observed_label_count) is int
            and observed_label_count == label_count
            and self._labels(tuple(label_values), 0, expected["labels"])
        )
        if not exact:
            return BoundResourceSnapshot(EffectPresence.DRIFT)
        return BoundResourceSnapshot(
            EffectPresence.EXACT,
            container_id,
            canonical_labels_sha256(expected["labels"]),
            image.image_id,
            image.repo_digest,
            running,
        )

    def observe_postgres_container(self) -> BoundResourceSnapshot:
        return self._observe_container("postgres")

    def observe_qdrant_container(self) -> BoundResourceSnapshot:
        return self._observe_container("qdrant")

    def _create(self, step_id: str, observe: object) -> None:
        if not callable(observe):
            raise LinuxLiveAdapterError("docker_create_invalid")
        current = observe()
        if type(current) is not BoundResourceSnapshot:
            raise LinuxLiveAdapterError("docker_create_observation_invalid")
        if current.presence is EffectPresence.EXACT:
            return
        if current.presence is not EffectPresence.ABSENT:
            raise LinuxLiveAdapterError("docker_create_drift_refused")
        _run_exact(self._runner, self._create_argv[step_id])

    def create_network(self) -> None:
        self._create("S01_CREATE_EXACT_NETWORK", self.observe_network)

    def create_postgres_volume(self) -> None:
        self._create(
            "S02_CREATE_EXACT_POSTGRES_VOLUME", self.observe_postgres_volume
        )

    def create_qdrant_volume(self) -> None:
        self._create("S03_CREATE_EXACT_QDRANT_VOLUME", self.observe_qdrant_volume)

    def create_postgres_container(self) -> None:
        self._create(
            "S04_CREATE_EXACT_POSTGRES_CONTAINER", self.observe_postgres_container
        )

    def create_qdrant_container(self) -> None:
        self._create(
            "S05_CREATE_EXACT_QDRANT_CONTAINER", self.observe_qdrant_container
        )

    def _effect(self, request_id: DockerRequestId) -> None:
        _run_exact(self._runner, DOCKER_REQUESTS[request_id].argv)

    def start_postgres_container(self) -> None:
        self._transition_container(
            self.observe_postgres_container,
            DockerRequestId.START_POSTGRES_CONTAINER,
            running=True,
        )

    def start_qdrant_container(self) -> None:
        self._transition_container(
            self.observe_qdrant_container,
            DockerRequestId.START_QDRANT_CONTAINER,
            running=True,
        )

    def stop_postgres_container(self) -> None:
        self._transition_container(
            self.observe_postgres_container,
            DockerRequestId.STOP_POSTGRES_CONTAINER,
            running=False,
        )

    def stop_qdrant_container(self) -> None:
        self._transition_container(
            self.observe_qdrant_container,
            DockerRequestId.STOP_QDRANT_CONTAINER,
            running=False,
        )

    def _transition_container(
        self,
        observe: object,
        request_id: DockerRequestId,
        *,
        running: bool,
    ) -> None:
        if not callable(observe):
            raise LinuxLiveAdapterError("docker_transition_invalid")
        current = observe()
        if type(current) is not BoundResourceSnapshot or current.presence is not EffectPresence.EXACT:
            raise LinuxLiveAdapterError("docker_transition_identity_not_exact")
        if current.running is running:
            return
        self._effect(request_id)

    def _remove_exact(
        self, observe: object, request_id: DockerRequestId
    ) -> None:
        if not callable(observe):
            raise LinuxLiveAdapterError("docker_remove_invalid")
        current = observe()
        if type(current) is not BoundResourceSnapshot:
            raise LinuxLiveAdapterError("docker_remove_observation_invalid")
        if current.presence is EffectPresence.ABSENT:
            return
        if current.presence is not EffectPresence.EXACT:
            raise LinuxLiveAdapterError("docker_remove_identity_not_exact")
        self._effect(request_id)

    def remove_postgres_container(self) -> None:
        self._remove_exact(
            self.observe_postgres_container,
            DockerRequestId.REMOVE_POSTGRES_CONTAINER,
        )

    def remove_qdrant_container(self) -> None:
        self._remove_exact(
            self.observe_qdrant_container,
            DockerRequestId.REMOVE_QDRANT_CONTAINER,
        )

    def remove_postgres_volume(self) -> None:
        self._remove_exact(
            self.observe_postgres_volume,
            DockerRequestId.REMOVE_POSTGRES_VOLUME,
        )

    def remove_qdrant_volume(self) -> None:
        self._remove_exact(
            self.observe_qdrant_volume,
            DockerRequestId.REMOVE_QDRANT_VOLUME,
        )

    def remove_network(self) -> None:
        self._remove_exact(self.observe_network, DockerRequestId.REMOVE_NETWORK)


class ClosedSystemdEffects:
    """Recoverable exact unit/link mutation with bounded daemon reloads."""

    def __init__(
        self, *, runner: FixedArgvRunner, files: ClosedRootFileEffects
    ) -> None:
        if not callable(getattr(runner, "execute", None)) or type(files) is not ClosedRootFileEffects:
            raise LinuxLiveAdapterError("closed_systemd_dependencies_invalid")
        self._runner = runner
        self._files = files

    def _reload(self) -> None:
        _run_exact(self._runner, SYSTEMD_DAEMON_RELOAD_ARGV)

    def _pending_document(self, action: str) -> bytes:
        if action not in {"install", "remove"}:
            raise LinuxLiveAdapterError("systemd_pending_action_invalid")
        return _canonical_bytes(
            {
                "action": action,
                "execution_id": self._files._execution_id,
                "schema_version": (
                    "governed-memory-systemd-daemon-reload-pending-v1"
                ),
            }
        ) + b"\n"

    def _pending_action(self) -> str | None:
        slot = RootFileSlot.SYSTEMD_DAEMON_RELOAD_PENDING
        observed = self._files._observe_regular(slot)
        if observed.kind is DescriptorNodeKind.ABSENT:
            return None
        contract = ROOT_FILE_SLOTS[slot]
        if not self._files._regular_exact(
            observed,
            mode=contract.required_mode,
            max_bytes=contract.max_bytes,
        ):
            raise LinuxLiveAdapterError("systemd_pending_record_drift")
        try:
            value = json.loads(observed.content.decode("ascii"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            raise LinuxLiveAdapterError("systemd_pending_record_invalid") from None
        if (
            type(value) is not dict
            or set(value) != {"action", "execution_id", "schema_version"}
            or value.get("action") not in {"install", "remove"}
            or value.get("execution_id") != self._files._execution_id
            or value.get("schema_version")
            != "governed-memory-systemd-daemon-reload-pending-v1"
            or observed.content != _canonical_bytes(value) + b"\n"
        ):
            raise LinuxLiveAdapterError("systemd_pending_record_invalid")
        return value["action"]

    def _ensure_pending(self, action: str) -> None:
        current = self._pending_action()
        if current is None:
            self._files._create_regular(
                RootFileSlot.SYSTEMD_DAEMON_RELOAD_PENDING,
                self._pending_document(action),
            )
        elif current != action:
            raise LinuxLiveAdapterError("systemd_pending_action_mismatch")

    def _clear_pending(self, action: str) -> None:
        if self._pending_action() != action:
            raise LinuxLiveAdapterError("systemd_pending_action_mismatch")
        slot = RootFileSlot.SYSTEMD_DAEMON_RELOAD_PENDING
        observed = self._files._observe_regular(slot)
        identity = self._files._identity(slot, observed)
        self._files._filesystem.remove_regular_exact(
            self._files._slot(slot), identity
        )
        if self._pending_action() is not None:
            raise LinuxLiveAdapterError("systemd_pending_remove_not_observed")

    def observe_supervisor(self) -> BoundSystemdSupervisorSnapshot:
        unit = self._files._observe_regular(RootFileSlot.SYSTEMD_UNIT)
        link = self._files._filesystem.observe(
            self._files._slot(RootFileSlot.SYSTEMD_ENABLEMENT)
        )
        unit_kind = FilesystemNodeKind.OTHER
        if unit.kind is DescriptorNodeKind.ABSENT:
            unit_kind = FilesystemNodeKind.ABSENT
        elif self._files._regular_exact(
            unit, mode=0o644, max_bytes=64 * 1024
        ):
            unit_kind = FilesystemNodeKind.REGULAR_FILE
        link_kind = FilesystemNodeKind.OTHER
        if link.kind is DescriptorNodeKind.ABSENT:
            link_kind = FilesystemNodeKind.ABSENT
        elif (
            link.kind is DescriptorNodeKind.SYMBOLIC_LINK
            and link.uid == 0
            and link.gid == 0
            and link.link_count == 1
        ):
            link_kind = FilesystemNodeKind.SYMLINK
        return BoundSystemdSupervisorSnapshot(
            SYSTEMD_UNIT_PATH,
            unit_kind,
            unit.content_sha256
            if unit_kind is FilesystemNodeKind.REGULAR_FILE
            else None,
            SYSTEMD_ENABLEMENT_PATH,
            link_kind,
            link.symlink_target
            if link_kind is FilesystemNodeKind.SYMLINK
            else None,
            self._pending_action() is not None,
        )

    def _raw_states(
        self, exact: ExactSystemdSupervisor
    ) -> tuple[DescriptorNodeObservation, DescriptorNodeObservation]:
        unit = self._files._observe_regular(RootFileSlot.SYSTEMD_UNIT)
        link = self._files._filesystem.observe(
            self._files._slot(RootFileSlot.SYSTEMD_ENABLEMENT)
        )
        unit_ok = (
            unit.kind is DescriptorNodeKind.ABSENT
            or (
                self._files._regular_exact(unit, mode=0o644, max_bytes=64 * 1024)
                and unit.content_sha256 == exact.unit_sha256
            )
        )
        link_ok = (
            link.kind is DescriptorNodeKind.ABSENT
            or (
                link.kind is DescriptorNodeKind.SYMBOLIC_LINK
                and link.uid == 0
                and link.gid == 0
                and link.link_count == 1
                and link.symlink_target == SYSTEMD_ENABLEMENT_TARGET
            )
        )
        if not unit_ok or not link_ok:
            raise LinuxLiveAdapterError("systemd_prefix_drift_refused")
        return unit, link

    @staticmethod
    def _validate_exact(exact: ExactSystemdSupervisor) -> None:
        if (
            type(exact) is not ExactSystemdSupervisor
            or exact.unit_path != SYSTEMD_UNIT_PATH
            or exact.enablement_path != SYSTEMD_ENABLEMENT_PATH
            or exact.enablement_target != SYSTEMD_ENABLEMENT_TARGET
            or _sha256(exact.unit_content) != exact.unit_sha256
        ):
            raise LinuxLiveAdapterError("systemd_exact_identity_invalid")

    def install_and_enable_supervisor(
        self, exact: ExactSystemdSupervisor
    ) -> None:
        self._validate_exact(exact)
        unit, link = self._raw_states(exact)
        pending = self._pending_action()
        if (
            unit.kind is DescriptorNodeKind.REGULAR_FILE
            and link.kind is DescriptorNodeKind.SYMBOLIC_LINK
            and pending is None
        ):
            return
        self._ensure_pending("install")
        if unit.kind is DescriptorNodeKind.ABSENT:
            self._files._create_regular(RootFileSlot.SYSTEMD_UNIT, exact.unit_content)
        if link.kind is DescriptorNodeKind.ABSENT:
            observed = self._files._filesystem.create_symlink_exclusive(
                self._files._slot(RootFileSlot.SYSTEMD_ENABLEMENT)
            )
            if (
                type(observed) is not DescriptorNodeObservation
                or observed.kind is not DescriptorNodeKind.SYMBOLIC_LINK
                or observed.symlink_target != SYSTEMD_ENABLEMENT_TARGET
            ):
                raise LinuxLiveAdapterError("systemd_enablement_create_not_observed")
        self._reload()
        self._clear_pending("install")
        if self.observe_supervisor().classify(exact).presence is not EffectPresence.EXACT:
            raise LinuxLiveAdapterError("systemd_install_not_observed")

    def disable_and_remove_supervisor(
        self, exact: ExactSystemdSupervisor
    ) -> None:
        self._validate_exact(exact)
        unit, link = self._raw_states(exact)
        pending = self._pending_action()
        if (
            unit.kind is DescriptorNodeKind.ABSENT
            and link.kind is DescriptorNodeKind.ABSENT
            and pending is None
        ):
            return
        self._ensure_pending("remove")
        if link.kind is DescriptorNodeKind.SYMBOLIC_LINK:
            self._files._filesystem.remove_symlink_exact(
                self._files._slot(RootFileSlot.SYSTEMD_ENABLEMENT),
                expected_device=link.device,
                expected_inode=link.inode,
            )
        if unit.kind is DescriptorNodeKind.REGULAR_FILE:
            identity = self._files._identity(RootFileSlot.SYSTEMD_UNIT, unit)
            self._files._filesystem.remove_regular_exact(
                self._files._slot(RootFileSlot.SYSTEMD_UNIT), identity
            )
        self._reload()
        self._clear_pending("remove")
        final = self.observe_supervisor()
        if (
            final.unit_kind is not FilesystemNodeKind.ABSENT
            or final.enablement_kind is not FilesystemNodeKind.ABSENT
        ):
            raise LinuxLiveAdapterError("systemd_remove_not_observed")


@dataclass(frozen=True, slots=True)
class QdrantHttpResponse:
    request_id: QdrantRequestId
    status: int
    body: bytes

    def __post_init__(self) -> None:
        request = QDRANT_REQUESTS.get(self.request_id)
        if (
            request is None
            or type(self.status) is not int
            or self.status not in request.accepted_statuses
            or type(self.body) is not bytes
            or len(self.body) > MAX_QDRANT_RESPONSE_BYTES
        ):
            raise LinuxLiveAdapterError("qdrant_http_response_invalid")


class BoundQdrantHttpClient(Protocol):
    """Already bound to 127.0.0.1:6343 and its opaque API key."""

    def exchange(self, request: QdrantRequestBytes) -> QdrantHttpResponse: ...


class QdrantApiKeySource(Protocol):
    def read_qdrant_api_key(self) -> bytes: ...


class StdlibQdrantHttpClient:
    """Bound, non-redirecting, size- and time-limited stdlib HTTP client."""

    def __init__(self, *, api_key_source: QdrantApiKeySource) -> None:
        if not callable(getattr(api_key_source, "read_qdrant_api_key", None)):
            raise LinuxLiveAdapterError("qdrant_api_key_source_invalid")
        self._api_key_source = api_key_source

    def exchange(self, request: QdrantRequestBytes) -> QdrantHttpResponse:
        canonical = QDRANT_REQUESTS.get(request.request_id)
        if type(request) is not QdrantRequestBytes or request != canonical:
            raise LinuxLiveAdapterError("qdrant_http_request_not_canonical")
        try:
            api_key = self._api_key_source.read_qdrant_api_key()
        except Exception:
            raise LinuxLiveAdapterError("qdrant_api_key_unavailable") from None
        if type(api_key) is not bytes or _SECRET_RE.fullmatch(api_key) is None:
            raise LinuxLiveAdapterError("qdrant_api_key_invalid")
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "api-key": api_key.decode("ascii"),
        }
        if request.body is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(
            QDRANT_HTTP_HOST,
            QDRANT_HTTP_PORT,
            timeout=QDRANT_HTTP_TIMEOUT_SECONDS,
        )
        try:
            connection.request(
                request.method.decode("ascii"),
                request.target.decode("ascii"),
                body=request.body,
                headers=headers,
            )
            response = connection.getresponse()
            encoding = response.getheader("Content-Encoding")
            length_header = response.getheader("Content-Length")
            if encoding not in {None, "identity"}:
                raise LinuxLiveAdapterError("qdrant_http_encoding_refused")
            if length_header is not None:
                try:
                    declared = int(length_header, 10)
                except ValueError:
                    raise LinuxLiveAdapterError("qdrant_http_length_invalid") from None
                if not 0 <= declared <= MAX_QDRANT_RESPONSE_BYTES:
                    raise LinuxLiveAdapterError("qdrant_http_response_too_large")
            body = response.read(MAX_QDRANT_RESPONSE_BYTES + 1)
            if len(body) > MAX_QDRANT_RESPONSE_BYTES:
                raise LinuxLiveAdapterError("qdrant_http_response_too_large")
            if response.status not in request.accepted_statuses:
                raise LinuxLiveAdapterError("qdrant_http_status_refused")
            return QdrantHttpResponse(request.request_id, response.status, body)
        except (OSError, http.client.HTTPException):
            raise LinuxLiveAdapterError("qdrant_http_exchange_failed") from None
        finally:
            connection.close()


class ClosedQdrantEffects:
    bind = QDRANT_BIND

    def __init__(
        self,
        *,
        client: BoundQdrantHttpClient,
        artifacts: ExactInstallArtifacts,
    ) -> None:
        if (
            not callable(getattr(client, "exchange", None))
            or type(artifacts) is not ExactInstallArtifacts
        ):
            raise LinuxLiveAdapterError("closed_qdrant_dependencies_invalid")
        self._client = client
        self._collection_sha256 = artifacts.qdrant_collection_sha256
        self._alias_sha256 = artifacts.qdrant_alias_sha256

    def _exchange(self, request_id: QdrantRequestId) -> QdrantHttpResponse:
        request = QDRANT_REQUESTS[request_id]
        try:
            response = self._client.exchange(request)
        except Exception:
            raise LinuxLiveAdapterError("qdrant_exchange_failed") from None
        if type(response) is not QdrantHttpResponse or response.request_id is not request_id:
            raise LinuxLiveAdapterError("qdrant_response_binding_mismatch")
        return response

    @staticmethod
    def _unique_json_object(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    @staticmethod
    def _reject_json_constant(unused: str) -> None:
        raise ValueError("non-finite JSON number")

    @staticmethod
    def _json(response: QdrantHttpResponse) -> dict[str, object]:
        try:
            value = json.loads(
                response.body.decode("utf-8"),
                object_pairs_hook=ClosedQdrantEffects._unique_json_object,
                parse_constant=ClosedQdrantEffects._reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise LinuxLiveAdapterError("qdrant_response_json_invalid") from None
        if type(value) is not dict:
            raise LinuxLiveAdapterError("qdrant_response_shape_invalid")
        return value

    @staticmethod
    def _response_result(
        value: dict[str, object],
        *,
        shape_error: str,
    ) -> object:
        if not {"result"} <= set(value) <= {"result", "status", "time"}:
            raise LinuxLiveAdapterError(shape_error)
        if "status" in value and type(value["status"]) is not str:
            raise LinuxLiveAdapterError(shape_error)
        if "time" in value and (
            type(value["time"]) not in {int, float} or value["time"] < 0
        ):
            raise LinuxLiveAdapterError(shape_error)
        return value["result"]

    def _server_version(self) -> str:
        value = self._json(self._exchange(QdrantRequestId.OBSERVE_ROOT))
        if not {"title", "version"} <= set(value) <= {
            "title",
            "version",
            "commit",
        }:
            raise LinuxLiveAdapterError("qdrant_root_shape_invalid")
        if type(value.get("title")) is not str or (
            "commit" in value and type(value["commit"]) is not str
        ):
            raise LinuxLiveAdapterError("qdrant_root_shape_invalid")
        version = value.get("version")
        if version != QDRANT_SERVER_VERSION:
            raise LinuxLiveAdapterError("qdrant_server_version_mismatch")
        return version

    def _collection(self) -> tuple[bool, int, QdrantCollectionConfiguration | None]:
        response = self._exchange(QdrantRequestId.OBSERVE_COLLECTION)
        if response.status == 404:
            return False, 0, None
        value = self._json(response)
        try:
            result = self._response_result(
                value, shape_error="qdrant_collection_shape_invalid"
            )
            if type(result) is not dict or not {
                "config",
                "points_count",
            } <= set(result) <= {
                "config",
                "indexed_vectors_count",
                "optimizer_status",
                "payload_schema",
                "points_count",
                "segments_count",
                "status",
            }:
                raise KeyError
            if type(result["config"]) is not dict or not {
                "params"
            } <= set(result["config"]) <= {
                "hnsw_config",
                "optimizer_config",
                "params",
                "quantization_config",
                "strict_mode_config",
                "wal_config",
            }:
                raise KeyError
            config = result["config"]["params"]
            if type(config) is not dict or not {
                "vectors",
                "on_disk_payload",
                "replication_factor",
            } <= set(config) <= {
                "on_disk_payload",
                "read_fan_out_factor",
                "replication_factor",
                "shard_number",
                "sharding_method",
                "sparse_vectors",
                "vectors",
                "write_consistency_factor",
            }:
                raise KeyError
            vectors = config["vectors"]
            if type(vectors) is not dict or not {
                "distance",
                "size",
            } <= set(vectors) <= {
                "datatype",
                "distance",
                "hnsw_config",
                "multivector_config",
                "on_disk",
                "quantization_config",
                "size",
            }:
                raise KeyError
            points_count = result["points_count"]
            normalized = QdrantCollectionConfiguration(
                vector_size=vectors["size"],
                distance=vectors["distance"],
                on_disk_payload=config["on_disk_payload"],
                replication_factor=config["replication_factor"],
            )
        except (KeyError, TypeError):
            raise LinuxLiveAdapterError("qdrant_collection_shape_invalid") from None
        if type(points_count) is not int or points_count < 0:
            raise LinuxLiveAdapterError("qdrant_collection_point_count_invalid")
        return True, points_count, normalized

    def _alias_target(self) -> str | None:
        response = self._exchange(QdrantRequestId.OBSERVE_ALIAS)
        if response.status == 404:
            return None
        value = self._json(response)
        try:
            result = self._response_result(
                value, shape_error="qdrant_alias_shape_invalid"
            )
            if type(result) is not dict or set(result) != {"aliases"}:
                raise KeyError
            aliases = result["aliases"]
        except (KeyError, TypeError):
            raise LinuxLiveAdapterError("qdrant_alias_shape_invalid") from None
        if type(aliases) is not list:
            raise LinuxLiveAdapterError("qdrant_alias_shape_invalid")
        if not aliases:
            return None
        if (
            len(aliases) != 1
            or type(aliases[0]) is not dict
            or set(aliases[0]) != {"alias_name", "collection_name"}
        ):
            raise LinuxLiveAdapterError("qdrant_alias_shape_invalid")
        if aliases[0].get("alias_name") != "governed_memory_active":
            raise LinuxLiveAdapterError("qdrant_alias_identity_invalid")
        target = aliases[0].get("collection_name")
        if type(target) is not str:
            raise LinuxLiveAdapterError("qdrant_alias_target_invalid")
        return target

    def _collection_names(self) -> tuple[str, ...]:
        value = self._json(self._exchange(QdrantRequestId.OBSERVE_COLLECTIONS))
        try:
            result = self._response_result(
                value, shape_error="qdrant_collections_shape_invalid"
            )
            if type(result) is not dict or set(result) != {"collections"}:
                raise KeyError
            collections = result["collections"]
        except (KeyError, TypeError):
            raise LinuxLiveAdapterError("qdrant_collections_shape_invalid") from None
        if (
            type(collections) is not list
            or len(collections) > 4096
            or any(
                type(item) is not dict
                or set(item) != {"name"}
                or type(item.get("name")) is not str
                or len(item["name"]) > 255
                for item in collections
            )
        ):
            raise LinuxLiveAdapterError("qdrant_collections_shape_invalid")
        names = tuple(sorted(item["name"] for item in collections))
        if len(set(names)) != len(names):
            raise LinuxLiveAdapterError("qdrant_collection_name_collision")
        return names

    def observe_collection(self) -> BoundResourceSnapshot:
        exists, points, config = self._collection()
        if not exists:
            return BoundResourceSnapshot(EffectPresence.ABSENT)
        if points != 0 or config is None:
            return BoundResourceSnapshot(EffectPresence.DRIFT)
        return BoundResourceSnapshot(
            EffectPresence.EXACT, self._collection_sha256
        )

    def observe_alias(self) -> BoundResourceSnapshot:
        target = self._alias_target()
        if target is None:
            return BoundResourceSnapshot(EffectPresence.ABSENT)
        if target != QDRANT_COLLECTION:
            return BoundResourceSnapshot(EffectPresence.DRIFT)
        return BoundResourceSnapshot(EffectPresence.EXACT, self._alias_sha256)

    def _mutate(self, request_id: QdrantRequestId) -> None:
        response = self._exchange(request_id)
        value = self._json(response)
        result = self._response_result(
            value, shape_error="qdrant_mutation_shape_invalid"
        )
        if value.get("status") != "ok" or result is not True:
            raise LinuxLiveAdapterError("qdrant_mutation_not_acknowledged")

    def create_collection(self) -> None:
        current = self.observe_collection()
        if current.presence is EffectPresence.EXACT:
            return
        if current.presence is not EffectPresence.ABSENT:
            raise LinuxLiveAdapterError("qdrant_collection_create_drift_refused")
        self._mutate(QdrantRequestId.CREATE_COLLECTION)

    def create_alias(self) -> None:
        current = self.observe_alias()
        if current.presence is EffectPresence.EXACT:
            return
        if current.presence is not EffectPresence.ABSENT:
            raise LinuxLiveAdapterError("qdrant_alias_create_drift_refused")
        self._mutate(QdrantRequestId.CREATE_ALIAS)

    def remove_alias(self) -> None:
        current = self.observe_alias()
        if current.presence is EffectPresence.ABSENT:
            return
        if current.presence is not EffectPresence.EXACT:
            raise LinuxLiveAdapterError("qdrant_alias_remove_drift_refused")
        self._mutate(QdrantRequestId.REMOVE_ALIAS)

    def remove_collection(self) -> None:
        current = self.observe_collection()
        if current.presence is EffectPresence.ABSENT:
            return
        if current.presence is not EffectPresence.EXACT:
            raise LinuxLiveAdapterError("qdrant_collection_remove_drift_refused")
        self._mutate(QdrantRequestId.REMOVE_COLLECTION)

    def inspect_prebootstrap(self) -> PrebootstrapQdrantSnapshot:
        version = self._server_version()
        exists, points, _ = self._collection()
        names = self._collection_names()
        if exists or points != 0 or names:
            raise LinuxLiveAdapterError("qdrant_prebootstrap_not_empty")
        return PrebootstrapQdrantSnapshot(
            QDRANT_BIND, version, False, 0, 0
        )

    def inspect_terminal(self) -> TerminalQdrantSnapshot:
        version = self._server_version()
        exists, points, config = self._collection()
        target = self._alias_target()
        names = self._collection_names()
        unexpected = sum(name != QDRANT_COLLECTION for name in names)
        if not exists or config is None or target != QDRANT_COLLECTION:
            raise LinuxLiveAdapterError("qdrant_terminal_identity_invalid")
        return TerminalQdrantSnapshot(
            bind=QDRANT_BIND,
            server_version=version,
            collection_exists=True,
            alias_target=target,
            collection_config=config,
            point_count=points,
            unexpected_candidate_collection_count=unexpected,
            source_endpoint_count=0,
        )


@dataclass(frozen=True, slots=True)
class ClosedLinuxPlatformAdapters:
    files: ClosedRootFileEffects
    docker: ClosedDockerEffects
    qdrant: ClosedQdrantEffects
    systemd: ClosedSystemdEffects

    def __post_init__(self) -> None:
        if (
            type(self.files) is not ClosedRootFileEffects
            or type(self.docker) is not ClosedDockerEffects
            or type(self.qdrant) is not ClosedQdrantEffects
            or type(self.systemd) is not ClosedSystemdEffects
        ):
            raise LinuxLiveAdapterError("closed_linux_platform_adapters_invalid")


class ClosedLinuxInvariantEffects:
    """Lock-bound, content-free install preflight over the two fixed ports.

    The port check is an administrative preflight, not a hostile-process
    security boundary.  The held global execution lock excludes cooperating
    installers; an administrator with equivalent host authority remains able
    to race or replace host state and is intentionally outside this claim.
    """

    _PORTS: Final = (55432, 6343)

    def __init__(self, held_lock: HeldExecutionLockCapability) -> None:
        try:
            validate_held_execution_lock(held_lock)
        except Exception:
            raise LinuxLiveAdapterError("closed_linux_execution_lock_invalid") from None
        self._held_lock = held_lock

    def global_execution_lock_held(self) -> bool:
        try:
            validate_held_execution_lock(self._held_lock)
        except Exception:
            raise LinuxLiveAdapterError("closed_linux_execution_lock_not_held") from None
        return True

    @staticmethod
    def _loopback_port_free(port: int) -> bool:
        candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            candidate.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            candidate.close()

    def inspect_live_preflight(self) -> LivePreflightSnapshot:
        self.global_execution_lock_held()
        postgres_free, qdrant_free = tuple(
            self._loopback_port_free(port) for port in self._PORTS
        )
        if not postgres_free or not qdrant_free:
            raise LinuxLiveAdapterError("closed_linux_store_port_not_free")
        return LivePreflightSnapshot(True, True, 0, 0)


class ClosedLinuxPlatformAdapterFactory:
    """Bind all non-PostgreSQL adapters after claim consumption."""

    def __init__(
        self,
        *,
        runner: FixedArgvRunner | None = None,
        filesystem: DescriptorSafeRootFilesystem,
        qdrant_http: BoundQdrantHttpClient,
    ) -> None:
        if (
            (
                runner is not None
                and not callable(getattr(runner, "execute", None))
            )
            or not callable(getattr(filesystem, "observe", None))
            or not callable(getattr(qdrant_http, "exchange", None))
        ):
            raise LinuxLiveAdapterError("closed_linux_platform_factory_invalid")
        self._runner = runner
        self._filesystem = filesystem
        self._qdrant_http = qdrant_http

    def __call__(
        self,
        *,
        execution_id: str,
        attempt_id: str,
        execution_binding_sha256: str,
        resolved_store_spec: Mapping[str, object],
        artifacts: ExactInstallArtifacts,
        prerequisites: object,
    ) -> ClosedLinuxPlatformAdapters:
        spec = validate_store_spec(resolved_store_spec, allow_placeholders=False)
        binding = spec["execution_binding"]
        local_images = getattr(prerequisites, "local_images", None)
        postgres_image = getattr(local_images, "postgres", None)
        qdrant_image = getattr(local_images, "qdrant", None)
        resolved_sha256 = _sha256(_canonical_bytes(spec))
        if (
            _EXECUTION_RE.fullmatch(execution_id) is None
            or _ATTEMPT_RE.fullmatch(attempt_id) is None
            or _HASH_RE.fullmatch(execution_binding_sha256) is None
            or binding.get("execution_id") != execution_id
            or binding.get("binding_sha256") != execution_binding_sha256
            or type(artifacts) is not ExactInstallArtifacts
            or postgres_image is None
            or qdrant_image is None
        ):
            raise LinuxLiveAdapterError("closed_linux_platform_binding_invalid")
        files = bind_closed_root_files(
            filesystem=self._filesystem,
            execution_id=execution_id,
            resolved_store_spec_sha256=resolved_sha256,
        )
        images = (
            FixedImageIdentity(
                "postgres",
                postgres_image.image_id,
                postgres_image.reference,
                postgres_image.repo_digest,
            ),
            FixedImageIdentity(
                "qdrant",
                qdrant_image.image_id,
                qdrant_image.reference,
                qdrant_image.repo_digest,
            ),
        )
        runner = self._runner
        if runner is None:
            runner = BoundedSubprocessFixedArgvRunner(spec)
        docker = ClosedDockerEffects(
            runner=runner,
            resolved_store_spec=spec,
            images=images,
        )
        qdrant = ClosedQdrantEffects(
            client=self._qdrant_http,
            artifacts=artifacts,
        )
        systemd = ClosedSystemdEffects(runner=runner, files=files)
        return ClosedLinuxPlatformAdapters(files, docker, qdrant, systemd)


class ProductionLinuxStoreTransportFactory:
    """Complete closed production factory for the dormant store controller.

    Only opaque authority/runtime capabilities and the fixed PostgreSQL receipt
    sink are accepted.  Filesystem, subprocess, fixed-port HTTP, secret
    readers, invariant checks, and all effect adapters are selected here; a
    caller cannot inject a path, command, endpoint, credential, SQL string, or
    alternate effect implementation.
    """

    def __init__(
        self,
        *,
        held_lock: HeldExecutionLockCapability,
        verified_controller_runtime_capability: object,
        postgres_receipt_sink: PostgreSQLStageReceiptSink,
    ) -> None:
        try:
            validate_held_execution_lock(held_lock)
            psycopg_runtime_capability = verified_psycopg_runtime_capability(
                verified_controller_runtime_capability
            )
        except Exception:
            raise LinuxLiveAdapterError(
                "production_linux_transport_authority_invalid"
            ) from None
        if not all(
            callable(getattr(postgres_receipt_sink, method, None))
            for method in (
                "persist_postgres_stage_receipt",
                "postgres_controller_authority_marker_held",
            )
        ):
            raise LinuxLiveAdapterError(
                "production_linux_postgres_receipt_sink_invalid"
            )
        self._held_lock = held_lock
        self._psycopg_runtime_capability = psycopg_runtime_capability
        self._postgres_receipt_sink = postgres_receipt_sink

    def __call__(
        self,
        *,
        execution_id: str,
        attempt_id: str,
        execution_binding_sha256: str,
        resolved_store_spec: Mapping[str, object],
        artifacts: ExactInstallArtifacts,
        prerequisites: object,
    ) -> BoundLinuxStoreTransports:
        try:
            validate_held_execution_lock(self._held_lock)
        except Exception:
            raise LinuxLiveAdapterError(
                "production_linux_execution_lock_not_held"
            ) from None
        spec = validate_store_spec(
            resolved_store_spec, allow_placeholders=False
        )
        binding = spec["execution_binding"]
        local_images = getattr(prerequisites, "local_images", None)
        postgres_image = getattr(local_images, "postgres", None)
        qdrant_image = getattr(local_images, "qdrant", None)
        resolved_sha256 = _sha256(_canonical_bytes(spec))
        if (
            _EXECUTION_RE.fullmatch(execution_id) is None
            or _ATTEMPT_RE.fullmatch(attempt_id) is None
            or _HASH_RE.fullmatch(execution_binding_sha256) is None
            or binding.get("execution_id") != execution_id
            or binding.get("binding_sha256") != execution_binding_sha256
            or type(artifacts) is not ExactInstallArtifacts
            or postgres_image is None
            or qdrant_image is None
        ):
            raise LinuxLiveAdapterError(
                "production_linux_transport_binding_invalid"
            )
        files = bind_closed_root_files(
            filesystem=PosixDescriptorSafeRootFilesystem(),
            execution_id=execution_id,
            resolved_store_spec_sha256=resolved_sha256,
        )
        runner = BoundedSubprocessFixedArgvRunner(spec)
        images = (
            FixedImageIdentity(
                "postgres",
                postgres_image.image_id,
                postgres_image.reference,
                postgres_image.repo_digest,
            ),
            FixedImageIdentity(
                "qdrant",
                qdrant_image.image_id,
                qdrant_image.reference,
                qdrant_image.repo_digest,
            ),
        )
        docker = ClosedDockerEffects(
            runner=runner,
            resolved_store_spec=spec,
            images=images,
        )
        postgres = PsycopgPostgreSQLAdapter(
            secret_source=files,
            receipt_sink=self._postgres_receipt_sink,
            runtime_capability=self._psycopg_runtime_capability,
        )
        qdrant = ClosedQdrantEffects(
            client=StdlibQdrantHttpClient(api_key_source=files),
            artifacts=artifacts,
        )
        systemd = ClosedSystemdEffects(runner=runner, files=files)
        invariants = ClosedLinuxInvariantEffects(self._held_lock)
        return BoundLinuxStoreTransports(
            invariants,
            files,
            docker,
            postgres,
            qdrant,
            systemd,
        )


__all__ = [
    "BoundQdrantHttpClient",
    "BoundedSubprocessFixedArgvRunner",
    "ClosedDockerEffects",
    "ClosedLinuxPlatformAdapterFactory",
    "ClosedLinuxPlatformAdapters",
    "ClosedLinuxInvariantEffects",
    "ClosedQdrantEffects",
    "ClosedRootFileEffects",
    "ClosedSystemdEffects",
    "CommandDisposition",
    "DescriptorDirectoryObservation",
    "DescriptorNodeKind",
    "DescriptorNodeObservation",
    "DescriptorSafeRootFilesystem",
    "ExactRootRecordObservation",
    "FixedArgvRunner",
    "FixedCommandResult",
    "FixedImageIdentity",
    "LinuxLiveAdapterError",
    "MAX_QDRANT_RESPONSE_BYTES",
    "PosixDescriptorSafeRootFilesystem",
    "ProductionLinuxStoreTransportFactory",
    "QDRANT_HTTP_HOST",
    "QDRANT_HTTP_PORT",
    "QDRANT_HTTP_TIMEOUT_SECONDS",
    "QdrantApiKeySource",
    "QdrantHttpResponse",
    "ROLLBACK_CONTROLLER_AUTHORITY_MARKER_PATH_TEMPLATE",
    "ROLLBACK_SEMANTIC_EMPTY_PROOF_PATH_TEMPLATE",
    "ResolvedRootSlot",
    "RetainedRootDirectory",
    "SYSTEMD_DAEMON_RELOAD_ARGV",
    "StdlibQdrantHttpClient",
    "bind_closed_root_files",
]

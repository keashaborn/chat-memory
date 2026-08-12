from __future__ import annotations

"""Closed subprocess boundary for the dormant store package.

The runner has no shell, caller-controlled environment, working directory, or
timeout.  It deliberately rejects image acquisition and remote endpoint
arguments.  Higher-level adapters must narrow the allowed argv further.
"""

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import PurePath
import re
import selectors
import subprocess
import time
from typing import Final, Protocol, Sequence


DOCKER_BINARY: Final = "/usr/bin/docker"
PERMITTED_EXECUTABLES: Final = frozenset({DOCKER_BINARY})
FIXED_ENVIRONMENT: Final = {
    "HOME": "/",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "TZ": "UTC",
}
DEFAULT_TIMEOUT_SECONDS: Final = 20
MAX_TIMEOUT_SECONDS: Final = 30
DEFAULT_MAX_OUTPUT_BYTES: Final = 64 * 1024
MAX_OUTPUT_BYTES: Final = 256 * 1024

_FORBIDDEN_COMMAND_WORDS = frozenset(
    {
        "build",
        "buildx",
        "compose",
        "cp",
        "exec",
        "export",
        "import",
        "load",
        "login",
        "pull",
        "push",
        "run",
        "save",
    }
)
_FORBIDDEN_ENDPOINT_MARKERS = (
    "api.openai.com",
    ".supabase.co",
    "provider_endpoint",
    "provider-endpoint",
    "source_postgres",
    "source-postgres",
    "source_database",
    "source-database",
)
_URI_RE = re.compile(r"(?:https?|postgres(?:ql)?|qdrant)://", re.IGNORECASE)
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_IMAGE_REFERENCE_RE = re.compile(
    r"[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}\Z",
    re.ASCII,
)
_COMMAND_PROFILES: Final = frozenset({"image_inspect", "store_supervisor"})
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ATTEMPT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_SAFE_RESOURCE_RE = re.compile(
    r"/?[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}\Z", re.ASCII
)
_RESOURCE_KINDS = frozenset(
    {
        "container",
        "database",
        "migration",
        "network",
        "qdrant_alias",
        "qdrant_collection",
        "resolved_store_spec",
        "secret_file",
        "systemd_unit",
        "volume",
    }
)


class HostBoundaryError(RuntimeError):
    """Content-free refusal at the host command boundary."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class HostOperationProfile(str, Enum):
    """The complete stores-only effect/compensation vocabulary."""

    REVERIFY_PRECLAIMED_EXECUTION_LOCK = "reverify_preclaimed_execution_lock"
    VERIFY_CLAIMED_EXECUTION_BINDING = "verify_claimed_execution_binding"
    VERIFY_LIVE_PREFLIGHT = "verify_live_preflight"
    WRITE_RESOLVED_STORE_SPEC_AND_GENERATE_FRESH_STORE_SECRETS = (
        "write_resolved_store_spec_and_generate_fresh_store_secrets"
    )
    CREATE_EXACT_NETWORK = "create_exact_network"
    CREATE_EXACT_POSTGRES_VOLUME = "create_exact_postgres_volume"
    CREATE_EXACT_QDRANT_VOLUME = "create_exact_qdrant_volume"
    CREATE_EXACT_POSTGRES_CONTAINER = "create_exact_postgres_container"
    CREATE_EXACT_QDRANT_CONTAINER = "create_exact_qdrant_container"
    START_AND_VERIFY_EMPTY_STORES = "start_and_verify_empty_stores"
    BOOTSTRAP_CANONICAL_DATABASE = "bootstrap_canonical_database"
    APPLY_FOUNDATION_0001 = "apply_foundation_0001"
    APPLY_OWNER_CLAIM_DETAIL_0003 = "apply_owner_claim_detail_0003"
    APPLY_PILOT_MARKER_0004 = "apply_pilot_marker_0004_without_marker"
    CREATE_EMPTY_QDRANT_COLLECTION = "create_empty_qdrant_collection"
    CREATE_QDRANT_ALIAS = "create_qdrant_alias"
    VERIFY_PRE_SUPERVISOR_RESOURCE_IDENTITIES = (
        "verify_pre_supervisor_resource_identities"
    )
    INSTALL_AND_ENABLE_STORES_SUPERVISOR = "install_and_enable_stores_supervisor"
    COLD_RESTART_AND_VERIFY_TERMINAL_POSTFLIGHT = (
        "cold_restart_and_verify_terminal_postflight"
    )
    REMOVE_RESOLVED_STORE_SPEC_AND_FRESH_STORE_SECRETS = (
        "remove_resolved_store_spec_and_fresh_store_secrets"
    )
    REMOVE_EXACT_UNUSED_NETWORK = "remove_exact_unused_network"
    REMOVE_EXACT_EMPTY_POSTGRES_VOLUME = "remove_exact_empty_postgres_volume"
    REMOVE_EXACT_EMPTY_QDRANT_VOLUME = "remove_exact_empty_qdrant_volume"
    REMOVE_EXACT_POSTGRES_CONTAINER = "remove_exact_postgres_container"
    REMOVE_EXACT_QDRANT_CONTAINER = "remove_exact_qdrant_container"
    STOP_EXACT_STORES = "stop_exact_stores"
    DROP_EMPTY_CANONICAL_DATABASE_AND_ROLES = (
        "drop_empty_canonical_database_and_roles"
    )
    ROLLBACK_EMPTY_FOUNDATION_0001 = "rollback_empty_foundation_0001"
    ROLLBACK_OWNER_CLAIM_DETAIL_0003 = "rollback_owner_claim_detail_0003"
    ROLLBACK_EMPTY_PILOT_MARKER_0004 = "rollback_empty_pilot_marker_0004"
    REMOVE_EXACT_EMPTY_QDRANT_COLLECTION = (
        "remove_exact_empty_qdrant_collection"
    )
    REMOVE_EXACT_QDRANT_ALIAS = "remove_exact_qdrant_alias"
    DISABLE_AND_REMOVE_STORES_SUPERVISOR = (
        "disable_and_remove_stores_supervisor"
    )


@dataclass(frozen=True, slots=True)
class HostResourceTarget:
    resource_kind: str
    resource_name: str
    resource_labels_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            self.resource_kind not in _RESOURCE_KINDS
            or _SAFE_RESOURCE_RE.fullmatch(self.resource_name) is None
            or "//" in self.resource_name
            or any(part in {".", ".."} for part in self.resource_name.split("/"))
            or (
                self.resource_kind in {"container", "network", "volume"}
                and (
                    self.resource_labels_sha256 is None
                    or _HASH_RE.fullmatch(self.resource_labels_sha256) is None
                )
            )
            or (
                self.resource_kind not in {"container", "network", "volume"}
                and self.resource_labels_sha256 is not None
            )
        ):
            raise HostBoundaryError("typed_host_resource_target_invalid")


@dataclass(frozen=True, slots=True)
class HostResourceIdentityReceipt:
    resource_kind: str
    resource_name: str
    resource_id: str
    ownership_sha256: str
    resource_labels_sha256: str | None = None
    image_id: str | None = None
    image_repo_digest: str | None = None

    def __post_init__(self) -> None:
        HostResourceTarget(
            self.resource_kind,
            self.resource_name,
            self.resource_labels_sha256,
        )
        if (
            _SAFE_RESOURCE_RE.fullmatch(self.resource_id) is None
            or "//" in self.resource_id
            or any(part in {".", ".."} for part in self.resource_id.split("/"))
            or _HASH_RE.fullmatch(self.ownership_sha256) is None
            or (
                self.resource_kind in {"container", "network", "volume"}
                and (
                    self.resource_labels_sha256 is None
                    or _HASH_RE.fullmatch(self.resource_labels_sha256) is None
                )
            )
            or (
                self.resource_kind not in {"container", "network", "volume"}
                and self.resource_labels_sha256 is not None
            )
        ):
            raise HostBoundaryError("typed_host_resource_identity_invalid")
        if self.resource_kind == "container":
            if (
                _CONTAINER_ID_RE.fullmatch(self.resource_id) is None
                or self.image_id is None
                or re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_id) is None
                or self.image_repo_digest is None
                or re.fullmatch(
                    r"[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}",
                    self.image_repo_digest,
                )
                is None
            ):
                raise HostBoundaryError("typed_host_container_identity_invalid")
        elif self.image_id is not None or self.image_repo_digest is not None:
            raise HostBoundaryError("typed_host_resource_image_identity_invalid")


@dataclass(frozen=True, slots=True)
class HostApplyResult:
    identities: tuple[HostResourceIdentityReceipt, ...]
    postflight_receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.identities) is not tuple
            or any(
                type(identity) is not HostResourceIdentityReceipt
                for identity in self.identities
            )
            or len(
                {
                    (identity.resource_kind, identity.resource_name)
                    for identity in self.identities
                }
            )
            != len(self.identities)
            or (
                self.postflight_receipt_sha256 is not None
                and _HASH_RE.fullmatch(self.postflight_receipt_sha256) is None
            )
        ):
            raise HostBoundaryError("typed_host_apply_result_invalid")
@dataclass(frozen=True, slots=True)
class HostOperationRequest:
    """Attempt-bound typed request; contains no argv or endpoint."""

    profile: HostOperationProfile
    step_id: str
    execution_id: str
    attempt_id: str
    execution_binding_sha256: str
    package_manifest_sha256: str
    resolved_store_spec_sha256: str
    controller_runtime_receipt_sha256: str
    controller_runtime_root: str
    controller_runtime_tree_sha256: str
    controller_release_root: str
    controller_release_tree_sha256: str
    controller_release_package_manifest_path: str
    controller_runtime_interpreter_path: str
    controller_runtime_interpreter_sha256: str
    controller_runtime_inventory_path: str
    controller_runtime_inventory_sha256: str
    controller_requirements_lock_sha256: str
    supervisor_launcher_path: str
    supervisor_launcher_sha256: str
    resource_targets: tuple[HostResourceTarget, ...]

    def __post_init__(self) -> None:
        if (
            type(self.profile) is not HostOperationProfile
            or not self.step_id.startswith("I")
            or _HASH_RE.fullmatch(self.execution_id) is None
            or _ATTEMPT_RE.fullmatch(self.attempt_id) is None
            or _HASH_RE.fullmatch(self.execution_binding_sha256) is None
            or _HASH_RE.fullmatch(self.package_manifest_sha256) is None
            or any(
                _HASH_RE.fullmatch(value) is None
                for value in (
                    self.resolved_store_spec_sha256,
                    self.controller_runtime_receipt_sha256,
                    self.controller_runtime_tree_sha256,
                    self.controller_release_tree_sha256,
                    self.controller_runtime_interpreter_sha256,
                    self.controller_runtime_inventory_sha256,
                    self.controller_requirements_lock_sha256,
                    self.supervisor_launcher_sha256,
                )
            )
            or self.controller_runtime_root
            != (
                "/opt/governed-memory-controller/runtimes/"
                + self.controller_runtime_receipt_sha256
            )
            or self.controller_runtime_interpreter_path
            != self.controller_runtime_root + "/bin/python"
            or self.controller_runtime_inventory_path
            != self.controller_runtime_root + "/controller-distributions.json"
            or self.controller_release_root
            != (
                "/opt/governed-memory-controller/releases/"
                + self.package_manifest_sha256
            )
            or self.controller_release_package_manifest_path
            != self.controller_release_root
            + "/ops/governed_memory/installation/current/package_manifest.json"
            or self.supervisor_launcher_path
            != self.controller_release_root
            + "/tools/governed_memory_install/store_supervisor_launcher.py"
            or type(self.resource_targets) is not tuple
            or any(type(target) is not HostResourceTarget for target in self.resource_targets)
            or len(set(self.resource_targets)) != len(self.resource_targets)
        ):
            raise HostBoundaryError("typed_host_operation_invalid")


@dataclass(frozen=True, slots=True)
class HostObservation:
    """One revision-stamped observation consumed by a matching mutation."""

    state: str
    revision_sha256: str
    ownership_sha256: str
    identities: tuple[HostResourceIdentityReceipt, ...] = ()
    postflight_receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"before", "after", "recoverable", "drift"} or any(
            _HASH_RE.fullmatch(value) is None
            for value in (self.revision_sha256, self.ownership_sha256)
        ) or type(self.identities) is not tuple or any(
            type(identity) is not HostResourceIdentityReceipt
            for identity in self.identities
        ) or (
            self.postflight_receipt_sha256 is not None
            and _HASH_RE.fullmatch(self.postflight_receipt_sha256) is None
        ):
            raise HostBoundaryError("typed_host_observation_invalid")


class TypedHostOperations(Protocol):
    """Closed adapter boundary used by the inactive install backend."""

    def observe(self, request: HostOperationRequest) -> HostObservation: ...

    def apply(
        self, request: HostOperationRequest, expected: HostObservation
    ) -> HostApplyResult: ...

    def compensate(
        self, request: HostOperationRequest, expected: HostObservation
    ) -> HostApplyResult: ...


def validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise HostBoundaryError("command_argv_invalid")
    normalized = tuple(argv)
    if any(type(item) is not str or not item for item in normalized):
        raise HostBoundaryError("command_argv_invalid")
    if not PurePath(normalized[0]).is_absolute():
        raise HostBoundaryError("command_executable_not_absolute")
    if any("\x00" in item or "\n" in item or "\r" in item for item in normalized):
        raise HostBoundaryError("command_argument_control_character")

    for item in normalized[1:]:
        lowered = item.lower()
        if lowered in _FORBIDDEN_COMMAND_WORDS:
            raise HostBoundaryError("command_operation_forbidden")
        if _URI_RE.search(lowered) is not None:
            raise HostBoundaryError("command_endpoint_forbidden")
        if any(marker in lowered for marker in _FORBIDDEN_ENDPOINT_MARKERS):
            raise HostBoundaryError("command_endpoint_forbidden")
    return normalized


class CommandRunner:
    """Run one bounded absolute argv with a fixed, minimal environment."""

    def __init__(
        self,
        *,
        allowed_executables: frozenset[str] = PERMITTED_EXECUTABLES,
        command_profile: str = "image_inspect",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        if not allowed_executables or not allowed_executables.issubset(
            PERMITTED_EXECUTABLES
        ) or any(
            type(path) is not str or not PurePath(path).is_absolute()
            for path in allowed_executables
        ):
            raise HostBoundaryError("allowed_executables_invalid")
        if type(command_profile) is not str or command_profile not in _COMMAND_PROFILES:
            raise HostBoundaryError("command_profile_invalid")
        if (
            type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise HostBoundaryError("command_timeout_invalid")
        if (
            type(max_output_bytes) is not int
            or not 1 <= max_output_bytes <= MAX_OUTPUT_BYTES
        ):
            raise HostBoundaryError("command_output_limit_invalid")
        self._allowed_executables = allowed_executables
        self._command_profile = command_profile
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes

    def run(self, argv: Sequence[str]) -> CommandResult:
        exact_argv = validate_argv(argv)
        if exact_argv[0] not in self._allowed_executables:
            raise HostBoundaryError("command_executable_forbidden")
        self._validate_profile(exact_argv)
        try:
            process = subprocess.Popen(
                exact_argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env=dict(FIXED_ENVIRONMENT),
                close_fds=True,
            )
        except OSError as error:
            raise HostBoundaryError("command_launch_failed") from error

        if process.stdout is None or process.stderr is None:
            process.kill()
            process.wait()
            raise HostBoundaryError("command_pipe_unavailable")
        streams = {"stdout": bytearray(), "stderr": bytearray()}
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + self._timeout_seconds
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HostBoundaryError("command_timeout")
                events = selector.select(remaining)
                if not events:
                    raise HostBoundaryError("command_timeout")
                for key, _ in events:
                    block = os.read(key.fileobj.fileno(), 8192)
                    if not block:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    streams[key.data].extend(block)
                    if sum(len(value) for value in streams.values()) > self._max_output_bytes:
                        raise HostBoundaryError("command_output_limit_exceeded")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostBoundaryError("command_timeout")
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                raise HostBoundaryError("command_timeout") from error
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            selector.close()
            for stream in (process.stdout, process.stderr):
                stream.close()

        try:
            stdout = bytes(streams["stdout"]).decode("utf-8", errors="strict")
            stderr = bytes(streams["stderr"]).decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise HostBoundaryError("command_output_not_utf8") from error
        result = CommandResult(
            argv=exact_argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        if returncode != os.EX_OK:
            raise HostBoundaryError("command_failed")
        return result

    def _validate_profile(self, argv: tuple[str, ...]) -> None:
        if self._command_profile == "image_inspect":
            if (
                len(argv) != 6
                or argv[1:5] != ("image", "inspect", "--format", "{{json .}}")
                or _IMAGE_REFERENCE_RE.fullmatch(argv[5]) is None
            ):
                raise HostBoundaryError("command_profile_refused")
            return
        if self._command_profile == "store_supervisor":
            inspect = (
                len(argv) == 6
                and argv[1:5]
                == ("container", "inspect", "--format", "{{json .}}")
                and _CONTAINER_ID_RE.fullmatch(argv[5]) is not None
            )
            start = (
                len(argv) == 3
                and argv[1] == "start"
                and _CONTAINER_ID_RE.fullmatch(argv[2]) is not None
            )
            stop = (
                len(argv) == 4
                and argv[1:3] == ("stop", "--time=10")
                and _CONTAINER_ID_RE.fullmatch(argv[3]) is not None
            )
            if not (inspect or start or stop):
                raise HostBoundaryError("command_profile_refused")
            return
        raise HostBoundaryError("command_profile_refused")

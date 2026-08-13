from __future__ import annotations

"""Pure contracts for a future closed Linux live-transport adapter.

This module does not implement a transport.  It contains no process, socket,
HTTP, or filesystem primitive and exposes no method that can create, execute,
or remove anything.  A later live adapter must consume these immutable request
identities and return typed, content-free observations.
"""

from dataclasses import dataclass
from enum import Enum
import re
from types import MappingProxyType
from typing import Final, Mapping


DOCKER_BINARY: Final = "/usr/bin/docker"
POSTGRES_CONTAINER: Final = "governed-memory-postgres-9a54cf123493-000001"
QDRANT_CONTAINER: Final = "governed-memory-qdrant-9a54cf123493-000001"
POSTGRES_VOLUME: Final = "governed-memory-postgres-data-9a54cf123493-000001"
QDRANT_VOLUME: Final = "governed-memory-qdrant-data-9a54cf123493-000001"
DOCKER_NETWORK: Final = "governed-memory-net-9a54cf123493-000001"

QDRANT_BIND: Final = "127.0.0.1:6343"
QDRANT_COLLECTION: Final = "governed_memory_9a54cf123493_000001"
QDRANT_ALIAS: Final = "governed_memory_active"

SYSTEMD_UNIT_PATH: Final = "/etc/systemd/system/governed-memory-stores.service"
SYSTEMD_ENABLEMENT_PATH: Final = (
    "/etc/systemd/system/multi-user.target.wants/governed-memory-stores.service"
)
SYSTEMD_ENABLEMENT_TARGET: Final = SYSTEMD_UNIT_PATH

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{2,63}\Z", re.ASCII)


class LiveTransportContractError(ValueError):
    """A pure live-transport contract value was not closed or canonical."""


class ObservationState(str, Enum):
    ABSENT = "ABSENT"
    EXACT = "EXACT"
    DRIFT = "DRIFT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class TypedObservation:
    """Content-free result returned by a future live observation adapter.

    ``ABSENT`` is an affirmative, hashed absence proof.  It is never inferred
    from an unavailable command, timeout, parse failure, or non-zero status;
    those are ``UNKNOWN``.  Raw command or secret-bearing output is not part of
    this contract.
    """

    state: ObservationState
    reason_code: str
    evidence_sha256: str | None
    observed_identity_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.state) is not ObservationState
            or type(self.reason_code) is not str
            or _CODE_RE.fullmatch(self.reason_code) is None
        ):
            raise LiveTransportContractError("typed_observation_invalid")
        evidence_valid = (
            type(self.evidence_sha256) is str
            and _HASH_RE.fullmatch(self.evidence_sha256) is not None
        )
        identity_valid = (
            type(self.observed_identity_sha256) is str
            and _HASH_RE.fullmatch(self.observed_identity_sha256) is not None
        )
        if self.state is ObservationState.UNKNOWN:
            if self.evidence_sha256 is not None or self.observed_identity_sha256 is not None:
                raise LiveTransportContractError("typed_observation_unknown_has_identity")
        elif self.state is ObservationState.ABSENT:
            if not evidence_valid or self.observed_identity_sha256 is not None:
                raise LiveTransportContractError("typed_observation_absence_proof_invalid")
        elif not evidence_valid or not identity_valid:
            raise LiveTransportContractError("typed_observation_identity_invalid")

    @property
    def manual_review_required(self) -> bool:
        return self.state in {ObservationState.DRIFT, ObservationState.UNKNOWN}


class DockerRequestId(str, Enum):
    OBSERVE_NETWORK = "observe_network"
    OBSERVE_POSTGRES_VOLUME = "observe_postgres_volume"
    OBSERVE_QDRANT_VOLUME = "observe_qdrant_volume"
    OBSERVE_POSTGRES_CONTAINER = "observe_postgres_container"
    OBSERVE_QDRANT_CONTAINER = "observe_qdrant_container"
    START_POSTGRES_CONTAINER = "start_postgres_container"
    START_QDRANT_CONTAINER = "start_qdrant_container"
    STOP_POSTGRES_CONTAINER = "stop_postgres_container"
    STOP_QDRANT_CONTAINER = "stop_qdrant_container"
    REMOVE_POSTGRES_CONTAINER = "remove_postgres_container"
    REMOVE_QDRANT_CONTAINER = "remove_qdrant_container"
    REMOVE_POSTGRES_VOLUME = "remove_postgres_volume"
    REMOVE_QDRANT_VOLUME = "remove_qdrant_volume"
    REMOVE_NETWORK = "remove_network"


@dataclass(frozen=True, slots=True)
class DockerArgvContract:
    request_id: DockerRequestId
    argv: tuple[str, ...]
    projected_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.request_id) is not DockerRequestId
            or type(self.argv) is not tuple
            or len(self.argv) < 2
            or self.argv[0] != DOCKER_BINARY
            or any(type(part) is not str or not part or "\x00" in part for part in self.argv)
            or type(self.projected_fields) is not tuple
            or any(type(field) is not str or not field for field in self.projected_fields)
        ):
            raise LiveTransportContractError("docker_argv_contract_invalid")
        is_observation = self.request_id.value.startswith("observe_")
        if is_observation != bool(self.projected_fields):
            raise LiveTransportContractError("docker_projection_contract_invalid")
        if is_observation:
            if self.argv[1] not in {"network", "volume", "container"}:
                raise LiveTransportContractError("docker_observation_scope_invalid")
            if "inspect" not in self.argv or "--format" not in self.argv:
                raise LiveTransportContractError("docker_observation_format_missing")
            template = self.argv[self.argv.index("--format") + 1]
            if "{{json .}}" in template or ".Config.Env" in template:
                raise LiveTransportContractError("docker_observation_projection_too_broad")
        canonical = globals().get("DOCKER_REQUESTS")
        if canonical is not None:
            expected = canonical.get(self.request_id)
            if (
                expected is None
                or self.argv != expected.argv
                or self.projected_fields != expected.projected_fields
            ):
                raise LiveTransportContractError("docker_request_not_canonical")

    @property
    def is_observation(self) -> bool:
        return bool(self.projected_fields)


_LABEL_KEYS: Final = (
    "lifeswitch.governed-memory.authorization-id",
    "lifeswitch.governed-memory.authorization-nonce-sha256",
    "lifeswitch.governed-memory.candidate",
    "lifeswitch.governed-memory.execution-binding-sha256",
    "lifeswitch.governed-memory.execution-id",
    "lifeswitch.governed-memory.package-generation",
    "lifeswitch.governed-memory.package-manifest-sha256",
)


def _label_fields(owner: str) -> tuple[str, ...]:
    return tuple(f'(index {owner} "{key}")' for key in _LABEL_KEYS)


_NETWORK_FIELDS: Final = (
    ".Name",
    ".Id",
    ".Driver",
    ".Internal",
    ".Attachable",
) + _label_fields(".Labels")
_VOLUME_FIELDS: Final = (
    ".Name",
    ".Driver",
    ".Mountpoint",
    ".Scope",
    "(len .Options)",
) + _label_fields(".Labels")
def _container_fields(port_key: str, unrelated_port_key: str) -> tuple[str, ...]:
    return (
        ".Name",
        ".Id",
        ".Image",
        ".Config.Image",
        ".Config.Cmd",
        "(len .Config.Healthcheck.Test)",
        "(index .Config.Healthcheck.Test 0)",
        ".HostConfig.NetworkMode",
        "(len .HostConfig.PortBindings)",
        f'(index .HostConfig.PortBindings "{port_key}")',
        f'(index .HostConfig.PortBindings "{unrelated_port_key}")',
        ".HostConfig.CapAdd",
        ".HostConfig.CapDrop",
        ".HostConfig.SecurityOpt",
        ".HostConfig.ReadonlyRootfs",
        ".HostConfig.RestartPolicy.Name",
        ".HostConfig.RestartPolicy.MaximumRetryCount",
        ".HostConfig.PidsLimit",
        ".HostConfig.LogConfig.Type",
        "(len .HostConfig.LogConfig.Config)",
        '(index .HostConfig.LogConfig.Config "max-file")',
        '(index .HostConfig.LogConfig.Config "max-size")',
        "(len .HostConfig.Tmpfs)",
        '(index .HostConfig.Tmpfs "/tmp")',
        "(len .Mounts)",
        "(index .Mounts 0).Type",
        "(index .Mounts 0).Name",
        "(index .Mounts 0).Destination",
        "(index .Mounts 0).RW",
        ".State.Status",
        ".State.Running",
        ".State.Paused",
        ".State.Restarting",
        ".State.Dead",
        ".State.OOMKilled",
        "(len .NetworkSettings.Ports)",
        f'(index .NetworkSettings.Ports "{port_key}")',
        f'(index .NetworkSettings.Ports "{unrelated_port_key}")',
        "(len .NetworkSettings.Networks)",
        f'(index .NetworkSettings.Networks "{DOCKER_NETWORK}").NetworkID',
        "(len .Config.Labels)",
    ) + _label_fields(".Config.Labels")


_POSTGRES_CONTAINER_FIELDS: Final = _container_fields(
    "5432/tcp", "6333/tcp"
)
_QDRANT_CONTAINER_FIELDS: Final = _container_fields(
    "6333/tcp", "5432/tcp"
)


def _projection(fields: tuple[str, ...]) -> str:
    return "\t".join(f"{{{{json {field}}}}}" for field in fields)


def _observe_argv(scope: str, name: str, fields: tuple[str, ...]) -> tuple[str, ...]:
    return (DOCKER_BINARY, scope, "inspect", "--format", _projection(fields), name)


DOCKER_REQUESTS: Final[Mapping[DockerRequestId, DockerArgvContract]] = MappingProxyType(
    {
        DockerRequestId.OBSERVE_NETWORK: DockerArgvContract(
            DockerRequestId.OBSERVE_NETWORK,
            _observe_argv("network", DOCKER_NETWORK, _NETWORK_FIELDS),
            _NETWORK_FIELDS,
        ),
        DockerRequestId.OBSERVE_POSTGRES_VOLUME: DockerArgvContract(
            DockerRequestId.OBSERVE_POSTGRES_VOLUME,
            _observe_argv("volume", POSTGRES_VOLUME, _VOLUME_FIELDS),
            _VOLUME_FIELDS,
        ),
        DockerRequestId.OBSERVE_QDRANT_VOLUME: DockerArgvContract(
            DockerRequestId.OBSERVE_QDRANT_VOLUME,
            _observe_argv("volume", QDRANT_VOLUME, _VOLUME_FIELDS),
            _VOLUME_FIELDS,
        ),
        DockerRequestId.OBSERVE_POSTGRES_CONTAINER: DockerArgvContract(
            DockerRequestId.OBSERVE_POSTGRES_CONTAINER,
            _observe_argv(
                "container", POSTGRES_CONTAINER, _POSTGRES_CONTAINER_FIELDS
            ),
            _POSTGRES_CONTAINER_FIELDS,
        ),
        DockerRequestId.OBSERVE_QDRANT_CONTAINER: DockerArgvContract(
            DockerRequestId.OBSERVE_QDRANT_CONTAINER,
            _observe_argv(
                "container", QDRANT_CONTAINER, _QDRANT_CONTAINER_FIELDS
            ),
            _QDRANT_CONTAINER_FIELDS,
        ),
        DockerRequestId.START_POSTGRES_CONTAINER: DockerArgvContract(
            DockerRequestId.START_POSTGRES_CONTAINER,
            (DOCKER_BINARY, "start", POSTGRES_CONTAINER),
        ),
        DockerRequestId.START_QDRANT_CONTAINER: DockerArgvContract(
            DockerRequestId.START_QDRANT_CONTAINER,
            (DOCKER_BINARY, "start", QDRANT_CONTAINER),
        ),
        DockerRequestId.STOP_POSTGRES_CONTAINER: DockerArgvContract(
            DockerRequestId.STOP_POSTGRES_CONTAINER,
            (DOCKER_BINARY, "stop", "--timeout=10", POSTGRES_CONTAINER),
        ),
        DockerRequestId.STOP_QDRANT_CONTAINER: DockerArgvContract(
            DockerRequestId.STOP_QDRANT_CONTAINER,
            (DOCKER_BINARY, "stop", "--timeout=10", QDRANT_CONTAINER),
        ),
        DockerRequestId.REMOVE_POSTGRES_CONTAINER: DockerArgvContract(
            DockerRequestId.REMOVE_POSTGRES_CONTAINER,
            (DOCKER_BINARY, "container", "rm", POSTGRES_CONTAINER),
        ),
        DockerRequestId.REMOVE_QDRANT_CONTAINER: DockerArgvContract(
            DockerRequestId.REMOVE_QDRANT_CONTAINER,
            (DOCKER_BINARY, "container", "rm", QDRANT_CONTAINER),
        ),
        DockerRequestId.REMOVE_POSTGRES_VOLUME: DockerArgvContract(
            DockerRequestId.REMOVE_POSTGRES_VOLUME,
            (DOCKER_BINARY, "volume", "rm", POSTGRES_VOLUME),
        ),
        DockerRequestId.REMOVE_QDRANT_VOLUME: DockerArgvContract(
            DockerRequestId.REMOVE_QDRANT_VOLUME,
            (DOCKER_BINARY, "volume", "rm", QDRANT_VOLUME),
        ),
        DockerRequestId.REMOVE_NETWORK: DockerArgvContract(
            DockerRequestId.REMOVE_NETWORK,
            (DOCKER_BINARY, "network", "rm", DOCKER_NETWORK),
        ),
    }
)


class RootNodeKind(str, Enum):
    REGULAR_FILE = "regular_file"
    SYMBOLIC_LINK = "symbolic_link"


class RootFileSlot(str, Enum):
    RESOLVED_STORE_SPEC = "resolved_store_spec"
    POSTGRES_SECRET = "postgres_secret"
    QDRANT_SECRET = "qdrant_secret"
    TERMINAL_POSTFLIGHT_RECEIPT = "terminal_postflight_receipt"
    SYSTEMD_UNIT = "systemd_unit"
    SYSTEMD_ENABLEMENT = "systemd_enablement"


@dataclass(frozen=True, slots=True)
class SafeAncestorContract:
    path_template: str
    allowed_modes: frozenset[int]
    required_uid: int = 0
    required_gid: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.path_template) is not str
            or not self.path_template.startswith("/")
            or type(self.allowed_modes) is not frozenset
            or not self.allowed_modes
            or any(
                type(mode) is not int or not 0 <= mode <= 0o777 or mode & 0o022
                for mode in self.allowed_modes
            )
            or self.required_uid != 0
            or self.required_gid != 0
        ):
            raise LiveTransportContractError("safe_ancestor_contract_invalid")


@dataclass(frozen=True, slots=True)
class RootFileSlotContract:
    slot: RootFileSlot
    path_template: str
    node_kind: RootNodeKind
    required_mode: int | None
    max_bytes: int
    ancestors: tuple[SafeAncestorContract, ...]
    symlink_target: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.slot) is not RootFileSlot
            or type(self.path_template) is not str
            or not self.path_template.startswith("/")
            or type(self.node_kind) is not RootNodeKind
            or type(self.max_bytes) is not int
            or self.max_bytes < 0
            or type(self.ancestors) is not tuple
            or not self.ancestors
        ):
            raise LiveTransportContractError("root_file_slot_contract_invalid")
        if self.node_kind is RootNodeKind.REGULAR_FILE:
            if self.required_mode not in {0o600, 0o644} or self.symlink_target is not None:
                raise LiveTransportContractError("root_regular_file_contract_invalid")
        elif (
            self.required_mode is not None
            or self.max_bytes != 0
            or self.symlink_target != SYSTEMD_ENABLEMENT_TARGET
        ):
            raise LiveTransportContractError("root_symlink_contract_invalid")


_PUBLIC_ROOTS: Final = (
    SafeAncestorContract("/", frozenset({0o755})),
    SafeAncestorContract("/etc", frozenset({0o755})),
)
_SYSTEMD_ROOTS: Final = _PUBLIC_ROOTS + (
    SafeAncestorContract("/etc/systemd", frozenset({0o755})),
    SafeAncestorContract("/etc/systemd/system", frozenset({0o755})),
)


ROOT_FILE_SLOTS: Final[Mapping[RootFileSlot, RootFileSlotContract]] = MappingProxyType(
    {
        RootFileSlot.RESOLVED_STORE_SPEC: RootFileSlotContract(
            RootFileSlot.RESOLVED_STORE_SPEC,
            "/etc/governed-memory-controller/store_spec.json",
            RootNodeKind.REGULAR_FILE,
            0o600,
            16 * 1024 * 1024,
            _PUBLIC_ROOTS
            + (SafeAncestorContract("/etc/governed-memory-controller", frozenset({0o700})),),
        ),
        RootFileSlot.POSTGRES_SECRET: RootFileSlotContract(
            RootFileSlot.POSTGRES_SECRET,
            "/etc/governed-memory-stores/9a54cf123493-000001/postgres.env",
            RootNodeKind.REGULAR_FILE,
            0o600,
            64 * 1024,
            _PUBLIC_ROOTS
            + (
                SafeAncestorContract("/etc/governed-memory-stores", frozenset({0o755})),
                SafeAncestorContract(
                    "/etc/governed-memory-stores/9a54cf123493-000001",
                    frozenset({0o700}),
                ),
            ),
        ),
        RootFileSlot.QDRANT_SECRET: RootFileSlotContract(
            RootFileSlot.QDRANT_SECRET,
            "/etc/governed-memory-stores/9a54cf123493-000001/qdrant.env",
            RootNodeKind.REGULAR_FILE,
            0o600,
            64 * 1024,
            _PUBLIC_ROOTS
            + (
                SafeAncestorContract("/etc/governed-memory-stores", frozenset({0o755})),
                SafeAncestorContract(
                    "/etc/governed-memory-stores/9a54cf123493-000001",
                    frozenset({0o700}),
                ),
            ),
        ),
        RootFileSlot.TERMINAL_POSTFLIGHT_RECEIPT: RootFileSlotContract(
            RootFileSlot.TERMINAL_POSTFLIGHT_RECEIPT,
            "/var/lib/governed-memory-controller/executions/{execution_id}/terminal-postflight-receipt.json",
            RootNodeKind.REGULAR_FILE,
            0o600,
            64 * 1024,
            (
                SafeAncestorContract("/", frozenset({0o755})),
                SafeAncestorContract("/var", frozenset({0o755})),
                SafeAncestorContract("/var/lib", frozenset({0o755})),
                SafeAncestorContract("/var/lib/governed-memory-controller", frozenset({0o700})),
                SafeAncestorContract(
                    "/var/lib/governed-memory-controller/executions", frozenset({0o700})
                ),
                SafeAncestorContract(
                    "/var/lib/governed-memory-controller/executions/{execution_id}",
                    frozenset({0o700}),
                ),
            ),
        ),
        RootFileSlot.SYSTEMD_UNIT: RootFileSlotContract(
            RootFileSlot.SYSTEMD_UNIT,
            SYSTEMD_UNIT_PATH,
            RootNodeKind.REGULAR_FILE,
            0o644,
            64 * 1024,
            _SYSTEMD_ROOTS,
        ),
        RootFileSlot.SYSTEMD_ENABLEMENT: RootFileSlotContract(
            RootFileSlot.SYSTEMD_ENABLEMENT,
            SYSTEMD_ENABLEMENT_PATH,
            RootNodeKind.SYMBOLIC_LINK,
            None,
            0,
            _SYSTEMD_ROOTS
            + (
                SafeAncestorContract(
                    "/etc/systemd/system/multi-user.target.wants", frozenset({0o755})
                ),
            ),
            SYSTEMD_ENABLEMENT_TARGET,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class RootRemovalIdentity:
    """Exact identity a future adapter must match before unlinking a root slot."""

    slot: RootFileSlot
    device: int
    inode: int
    content_sha256: str
    execution_id: str

    def __post_init__(self) -> None:
        if (
            type(self.slot) is not RootFileSlot
            or type(self.device) is not int
            or self.device <= 0
            or type(self.inode) is not int
            or self.inode <= 0
            or type(self.content_sha256) is not str
            or _HASH_RE.fullmatch(self.content_sha256) is None
            or type(self.execution_id) is not str
            or _HASH_RE.fullmatch(self.execution_id) is None
        ):
            raise LiveTransportContractError("root_removal_identity_invalid")

    @property
    def path(self) -> str:
        return ROOT_FILE_SLOTS[self.slot].path_template.replace(
            "{execution_id}", self.execution_id
        )


class SystemdPrefixState(str, Enum):
    ABSENT = "ABSENT"
    UNIT_ONLY_PARTIAL = "UNIT_ONLY_PARTIAL"
    EXACT = "EXACT"
    NON_PREFIX_PARTIAL = "NON_PREFIX_PARTIAL"
    DRIFT = "DRIFT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SystemdPrefixObservation:
    unit: TypedObservation
    enablement: TypedObservation

    def __post_init__(self) -> None:
        if type(self.unit) is not TypedObservation or type(self.enablement) is not TypedObservation:
            raise LiveTransportContractError("systemd_prefix_observation_invalid")

    @property
    def state(self) -> SystemdPrefixState:
        pair = (self.unit.state, self.enablement.state)
        if ObservationState.UNKNOWN in pair:
            return SystemdPrefixState.UNKNOWN
        if ObservationState.DRIFT in pair:
            return SystemdPrefixState.DRIFT
        if pair == (ObservationState.ABSENT, ObservationState.ABSENT):
            return SystemdPrefixState.ABSENT
        if pair == (ObservationState.EXACT, ObservationState.ABSENT):
            return SystemdPrefixState.UNIT_ONLY_PARTIAL
        if pair == (ObservationState.EXACT, ObservationState.EXACT):
            return SystemdPrefixState.EXACT
        return SystemdPrefixState.NON_PREFIX_PARTIAL

    @property
    def manual_review_required(self) -> bool:
        return self.state not in {SystemdPrefixState.ABSENT, SystemdPrefixState.EXACT}


class QdrantRequestId(str, Enum):
    OBSERVE_ROOT = "observe_root"
    OBSERVE_COLLECTION = "observe_collection"
    OBSERVE_ALIAS = "observe_alias"
    CREATE_COLLECTION = "create_collection"
    CREATE_ALIAS = "create_alias"
    REMOVE_ALIAS = "remove_alias"
    REMOVE_COLLECTION = "remove_collection"


@dataclass(frozen=True, slots=True)
class QdrantRequestBytes:
    request_id: QdrantRequestId
    method: bytes
    target: bytes
    body: bytes | None
    accepted_statuses: frozenset[int]

    def __post_init__(self) -> None:
        if (
            type(self.request_id) is not QdrantRequestId
            or self.method not in {b"GET", b"PUT", b"POST", b"DELETE"}
            or type(self.target) is not bytes
            or not self.target.startswith(b"/")
            or any(byte < 0x21 or byte > 0x7E for byte in self.target)
            or (self.body is not None and type(self.body) is not bytes)
            or type(self.accepted_statuses) is not frozenset
            or not self.accepted_statuses
            or any(type(code) is not int or not 100 <= code <= 599 for code in self.accepted_statuses)
        ):
            raise LiveTransportContractError("qdrant_request_bytes_invalid")
        canonical = globals().get("QDRANT_REQUESTS")
        if canonical is not None:
            expected = canonical.get(self.request_id)
            if (
                expected is None
                or self.method != expected.method
                or self.target != expected.target
                or self.body != expected.body
                or self.accepted_statuses != expected.accepted_statuses
            ):
                raise LiveTransportContractError("qdrant_request_not_canonical")


_COLLECTION_TARGET: Final = f"/collections/{QDRANT_COLLECTION}".encode("ascii")
_ALIAS_TARGET: Final = f"/aliases/{QDRANT_ALIAS}".encode("ascii")
_CREATE_COLLECTION_BODY: Final = (
    b'{"on_disk_payload":true,"replication_factor":1,'
    b'"vectors":{"distance":"Dot","size":3072}}'
)
_CREATE_ALIAS_BODY: Final = (
    b'{"actions":[{"create_alias":{"alias_name":"governed_memory_active",'
    b'"collection_name":"governed_memory_9a54cf123493_000001"}}]}'
)
_REMOVE_ALIAS_BODY: Final = (
    b'{"actions":[{"delete_alias":{"alias_name":"governed_memory_active"}}]}'
)


QDRANT_REQUESTS: Final[Mapping[QdrantRequestId, QdrantRequestBytes]] = MappingProxyType(
    {
        QdrantRequestId.OBSERVE_ROOT: QdrantRequestBytes(
            QdrantRequestId.OBSERVE_ROOT, b"GET", b"/", None, frozenset({200})
        ),
        QdrantRequestId.OBSERVE_COLLECTION: QdrantRequestBytes(
            QdrantRequestId.OBSERVE_COLLECTION,
            b"GET",
            _COLLECTION_TARGET,
            None,
            frozenset({200, 404}),
        ),
        QdrantRequestId.OBSERVE_ALIAS: QdrantRequestBytes(
            QdrantRequestId.OBSERVE_ALIAS,
            b"GET",
            _ALIAS_TARGET,
            None,
            frozenset({200, 404}),
        ),
        QdrantRequestId.CREATE_COLLECTION: QdrantRequestBytes(
            QdrantRequestId.CREATE_COLLECTION,
            b"PUT",
            _COLLECTION_TARGET,
            _CREATE_COLLECTION_BODY,
            frozenset({200}),
        ),
        QdrantRequestId.CREATE_ALIAS: QdrantRequestBytes(
            QdrantRequestId.CREATE_ALIAS,
            b"POST",
            b"/collections/aliases",
            _CREATE_ALIAS_BODY,
            frozenset({200}),
        ),
        QdrantRequestId.REMOVE_ALIAS: QdrantRequestBytes(
            QdrantRequestId.REMOVE_ALIAS,
            b"POST",
            b"/collections/aliases",
            _REMOVE_ALIAS_BODY,
            frozenset({200}),
        ),
        QdrantRequestId.REMOVE_COLLECTION: QdrantRequestBytes(
            QdrantRequestId.REMOVE_COLLECTION,
            b"DELETE",
            _COLLECTION_TARGET,
            None,
            frozenset({200}),
        ),
    }
)


__all__ = [
    "DOCKER_REQUESTS",
    "DockerArgvContract",
    "DockerRequestId",
    "LiveTransportContractError",
    "ObservationState",
    "QDRANT_BIND",
    "QDRANT_COLLECTION",
    "QDRANT_REQUESTS",
    "QdrantRequestBytes",
    "QdrantRequestId",
    "ROOT_FILE_SLOTS",
    "RootFileSlot",
    "RootFileSlotContract",
    "RootNodeKind",
    "RootRemovalIdentity",
    "SYSTEMD_ENABLEMENT_PATH",
    "SYSTEMD_ENABLEMENT_TARGET",
    "SYSTEMD_UNIT_PATH",
    "SafeAncestorContract",
    "SystemdPrefixObservation",
    "SystemdPrefixState",
    "TypedObservation",
]

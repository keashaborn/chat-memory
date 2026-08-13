from __future__ import annotations

"""Closed Linux effects for the claim-bound dormant-store installation.

This module is the concrete step dispatcher.  It never accepts argv, shell
text, SQL selected by a caller, URLs, host names, ports, HTTP paths, or raw
credentials.  Low-level Docker, systemd, PostgreSQL, Qdrant, and root-file
drivers are pre-bound to the exact validated store specification by an
injected platform factory.  Phase 9D intentionally does not select the live
PostgreSQL driver; that separately pinned transport remains a Phase 9E input.
"""

from dataclasses import dataclass
from enum import Enum
import base64
import hashlib
import json
import re
import secrets
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Mapping, Protocol

from .controller import STORES_ONLY_PLAN
from .host_boundary import (
    HostApplyResult,
    HostBoundaryError,
    HostObservation,
    HostOperationProfile,
    HostOperationRequest,
    HostResourceIdentityReceipt,
    HostResourceTarget,
)
from .linux_plan import canonical_labels_sha256, validate_store_spec
from .linux_store_readiness import ClosedStoreReadinessProbe

if TYPE_CHECKING:
    from .install_backend import ClaimBoundInstallDependencies, InstallPrerequisites


MAX_ARTIFACT_BYTES: Final = 16 * 1024 * 1024
MAX_PUBLIC_RECEIPT_BYTES: Final = 64 * 1024
POSTGRES_BIND: Final = "127.0.0.1:55432"
QDRANT_BIND: Final = "127.0.0.1:6343"
SYSTEMD_UNIT_PATH: Final = (
    "/etc/systemd/system/governed-memory-stores.service"
)
SYSTEMD_ENABLEMENT_PATH: Final = (
    "/etc/systemd/system/multi-user.target.wants/"
    "governed-memory-stores.service"
)
SYSTEMD_ENABLEMENT_TARGET: Final = SYSTEMD_UNIT_PATH
CONTROLLER_CONFIG_DIRECTORY: Final = "/etc/governed-memory-controller"
STORE_SECRET_DIRECTORY: Final = (
    "/etc/governed-memory-stores/9a54cf123493-000001"
)
POSTGRES_STORE_SECRET_PATH: Final = STORE_SECRET_DIRECTORY + "/postgres.env"
QDRANT_STORE_SECRET_PATH: Final = STORE_SECRET_DIRECTORY + "/qdrant.env"
POSTGRES_STORE_SECRET_PUBLIC_ID: Final = (
    "path-sha256:"
    + hashlib.sha256(POSTGRES_STORE_SECRET_PATH.encode("ascii")).hexdigest()
)
QDRANT_STORE_SECRET_PUBLIC_ID: Final = (
    "path-sha256:"
    + hashlib.sha256(QDRANT_STORE_SECRET_PATH.encode("ascii")).hexdigest()
)
RETAINED_ROOT_DIRECTORY_PATHS: Final = (
    CONTROLLER_CONFIG_DIRECTORY,
    STORE_SECRET_DIRECTORY,
)
POSTFLIGHT_TEMPLATE: Final = (
    "/var/lib/governed-memory-controller/executions/"
    "{execution_id}/terminal-postflight-receipt.json"
)
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}\Z", re.ASCII)
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_REPO_DIGEST_RE = re.compile(
    r"[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}\Z", re.ASCII
)


class LinuxStoreEffectsError(RuntimeError):
    """Content-free refusal from the exact Linux store effects adapter."""


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
        raise LinuxStoreEffectsError("linux_store_document_invalid") from None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _expected_rollback_resource_names(
    package_manifest_sha256: str,
) -> dict[str, tuple[str, str]]:
    if _HASH_RE.fullmatch(package_manifest_sha256) is None:
        raise LinuxStoreEffectsError("linux_store_package_hash_invalid")
    return {
        "stores_supervisor": (
            "systemd_unit",
            "/etc/systemd/system/governed-memory-stores.service",
        ),
        "qdrant_alias": ("qdrant_alias", "governed_memory_active"),
        "qdrant_collection": (
            "qdrant_collection",
            "governed_memory_9a54cf123493_000001",
        ),
        "migration_0004": ("migration", "0004_pilot_marker"),
        "migration_0003": ("migration", "0003_owner_claim_detail"),
        "migration_0001": ("migration", "0001_foundation"),
        "canonical_database_and_roles": ("database", "governed_memory"),
        "qdrant_container": (
            "container",
            "governed-memory-qdrant-9a54cf123493-000001",
        ),
        "postgres_container": (
            "container",
            "governed-memory-postgres-9a54cf123493-000001",
        ),
        "qdrant_volume": (
            "volume",
            "governed-memory-qdrant-data-9a54cf123493-000001",
        ),
        "postgres_volume": (
            "volume",
            "governed-memory-postgres-data-9a54cf123493-000001",
        ),
        "network": ("network", "governed-memory-net-9a54cf123493-000001"),
        "qdrant_store_secret": (
            "secret_file",
            QDRANT_STORE_SECRET_PATH,
        ),
        "postgres_store_secret": (
            "secret_file",
            POSTGRES_STORE_SECRET_PATH,
        ),
        "resolved_store_spec": (
            "resolved_store_spec",
            "/etc/governed-memory-controller/store_spec.json",
        ),
    }


def _host_observation_ownership(
    request: HostOperationRequest, revision_sha256: str
) -> str:
    return _sha256(
        _canonical_bytes(
            {
                "schema_version": "governed-memory-host-observation-ownership-v1",
                "profile": request.profile.value,
                "step_id": request.step_id,
                "execution_id": request.execution_id,
                "attempt_id": request.attempt_id,
                "execution_binding_sha256": request.execution_binding_sha256,
                "resolved_store_spec_sha256": request.resolved_store_spec_sha256,
                "controller_runtime_receipt_sha256": (
                    request.controller_runtime_receipt_sha256
                ),
                "controller_runtime_tree_sha256": (
                    request.controller_runtime_tree_sha256
                ),
                "controller_release_tree_sha256": (
                    request.controller_release_tree_sha256
                ),
                "supervisor_launcher_sha256": request.supervisor_launcher_sha256,
                "postflight_receipt_path": request.postflight_receipt_path,
                "revision_sha256": revision_sha256,
            }
        )
    )


def _resource_ownership(
    request: HostOperationRequest,
    *,
    resource_kind: str,
    resource_name: str,
    resource_id: str,
    resource_labels_sha256: str | None,
    image_id: str | None,
    image_repo_digest: str | None,
) -> str:
    return _sha256(
        _canonical_bytes(
            {
                "schema_version": "governed-memory-install-resource-ownership-v1",
                "profile": request.profile.value,
                "step_id": request.step_id,
                "execution_id": request.execution_id,
                "attempt_id": request.attempt_id,
                "execution_binding_sha256": request.execution_binding_sha256,
                "package_manifest_sha256": request.package_manifest_sha256,
                "resolved_store_spec_sha256": request.resolved_store_spec_sha256,
                "controller_runtime_receipt_sha256": (
                    request.controller_runtime_receipt_sha256
                ),
                "controller_runtime_tree_sha256": (
                    request.controller_runtime_tree_sha256
                ),
                "controller_release_tree_sha256": (
                    request.controller_release_tree_sha256
                ),
                "supervisor_launcher_sha256": request.supervisor_launcher_sha256,
                "postflight_receipt_path": request.postflight_receipt_path,
                "resource_kind": resource_kind,
                "resource_name": resource_name,
                "resource_id": resource_id,
                "resource_labels_sha256": resource_labels_sha256,
                "image_id": image_id,
                "image_repo_digest": image_repo_digest,
            }
        )
    )


class EffectPresence(str, Enum):
    ABSENT = "absent"
    EXACT = "exact"
    PARTIAL = "partial"
    DRIFT = "drift"


class FilesystemNodeKind(str, Enum):
    ABSENT = "absent"
    REGULAR_FILE = "regular_file"
    SYMLINK = "symlink"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ExactSystemdSupervisor:
    """Fixed unit and enablement identity; parent directories are substrate."""

    unit_path: str
    unit_content: bytes
    unit_sha256: str
    enablement_path: str
    enablement_target: str
    composite_identity_sha256: str

    @classmethod
    def from_rendered_unit(cls, rendered_unit: bytes) -> "ExactSystemdSupervisor":
        if (
            type(rendered_unit) is not bytes
            or not 1 <= len(rendered_unit) <= 64 * 1024
            or b"\x00" in rendered_unit
        ):
            raise LinuxStoreEffectsError("systemd_supervisor_unit_invalid")
        unit_sha256 = _sha256(rendered_unit)
        composite_identity_sha256 = _sha256(
            _canonical_bytes(
                {
                    "schema_version": (
                        "governed-memory-systemd-supervisor-identity-v1"
                    ),
                    "unit_path": SYSTEMD_UNIT_PATH,
                    "unit_sha256": unit_sha256,
                    "enablement_path": SYSTEMD_ENABLEMENT_PATH,
                    "enablement_target": SYSTEMD_ENABLEMENT_TARGET,
                }
            )
        )
        return cls(
            SYSTEMD_UNIT_PATH,
            rendered_unit,
            unit_sha256,
            SYSTEMD_ENABLEMENT_PATH,
            SYSTEMD_ENABLEMENT_TARGET,
            composite_identity_sha256,
        )


@dataclass(frozen=True, slots=True)
class BoundSystemdSupervisorSnapshot:
    """Bound observation of both exact systemd filesystem nodes."""

    unit_path: str
    unit_kind: FilesystemNodeKind
    unit_sha256: str | None
    enablement_path: str
    enablement_kind: FilesystemNodeKind
    enablement_target: str | None

    def __post_init__(self) -> None:
        if (
            self.unit_path != SYSTEMD_UNIT_PATH
            or self.enablement_path != SYSTEMD_ENABLEMENT_PATH
            or type(self.unit_kind) is not FilesystemNodeKind
            or type(self.enablement_kind) is not FilesystemNodeKind
        ):
            raise LinuxStoreEffectsError("systemd_supervisor_snapshot_invalid")
        if self.unit_kind is FilesystemNodeKind.REGULAR_FILE:
            if (
                type(self.unit_sha256) is not str
                or _HASH_RE.fullmatch(self.unit_sha256) is None
            ):
                raise LinuxStoreEffectsError("systemd_supervisor_snapshot_invalid")
        elif self.unit_sha256 is not None:
            raise LinuxStoreEffectsError("systemd_supervisor_snapshot_invalid")
        if self.enablement_kind is FilesystemNodeKind.SYMLINK:
            if (
                type(self.enablement_target) is not str
                or self.enablement_target == ""
                or len(self.enablement_target) > 4096
                or "\x00" in self.enablement_target
            ):
                raise LinuxStoreEffectsError("systemd_supervisor_snapshot_invalid")
        elif self.enablement_target is not None:
            raise LinuxStoreEffectsError("systemd_supervisor_snapshot_invalid")

    @classmethod
    def absent(cls) -> "BoundSystemdSupervisorSnapshot":
        return cls(
            SYSTEMD_UNIT_PATH,
            FilesystemNodeKind.ABSENT,
            None,
            SYSTEMD_ENABLEMENT_PATH,
            FilesystemNodeKind.ABSENT,
            None,
        )

    def classify(
        self, exact: ExactSystemdSupervisor
    ) -> "BoundResourceSnapshot":
        if type(exact) is not ExactSystemdSupervisor:
            raise LinuxStoreEffectsError("systemd_supervisor_identity_invalid")
        if (
            self.unit_kind is FilesystemNodeKind.ABSENT
            and self.enablement_kind is FilesystemNodeKind.ABSENT
        ):
            return BoundResourceSnapshot(EffectPresence.ABSENT)
        unit_exact = (
            self.unit_kind is FilesystemNodeKind.REGULAR_FILE
            and self.unit_sha256 == exact.unit_sha256
        )
        enablement_exact = (
            self.enablement_kind is FilesystemNodeKind.SYMLINK
            and self.enablement_target == exact.enablement_target
        )
        if unit_exact and enablement_exact:
            return BoundResourceSnapshot(
                EffectPresence.EXACT, exact.composite_identity_sha256
            )
        allowed_kinds = (
            self.unit_kind
            in {FilesystemNodeKind.ABSENT, FilesystemNodeKind.REGULAR_FILE}
            and self.enablement_kind
            in {FilesystemNodeKind.ABSENT, FilesystemNodeKind.SYMLINK}
        )
        if (
            allowed_kinds
            and self.unit_kind is FilesystemNodeKind.ABSENT
            and enablement_exact
        ) or (
            allowed_kinds
            and unit_exact
            and self.enablement_kind is FilesystemNodeKind.ABSENT
        ):
            return BoundResourceSnapshot(EffectPresence.PARTIAL)
        return BoundResourceSnapshot(EffectPresence.DRIFT)


@dataclass(frozen=True, slots=True)
class BoundRetainedRootDirectorySnapshot:
    """Identity of preinstalled substrate that install/rollback must retain."""

    path: str
    owner_name: str
    group_name: str
    mode: int
    is_directory: bool
    is_symlink: bool

    def __post_init__(self) -> None:
        if (
            self.path not in RETAINED_ROOT_DIRECTORY_PATHS
            or self.owner_name != "root"
            or self.group_name != "root"
            or type(self.mode) is not int
            or self.mode != 0o700
            or self.is_directory is not True
            or self.is_symlink is not False
        ):
            raise LinuxStoreEffectsError(
                "retained_root_directory_snapshot_invalid"
            )


@dataclass(frozen=True, slots=True)
class BoundResourceSnapshot:
    """Content-free identity returned by one pre-bound platform driver."""

    presence: EffectPresence
    resource_id: str | None = None
    resource_labels_sha256: str | None = None
    image_id: str | None = None
    image_repo_digest: str | None = None
    running: bool | None = None

    def __post_init__(self) -> None:
        if type(self.presence) is not EffectPresence:
            raise LinuxStoreEffectsError("bound_resource_snapshot_invalid")
        if self.presence is EffectPresence.EXACT:
            if (
                type(self.resource_id) is not str
                or _SAFE_ID_RE.fullmatch(self.resource_id) is None
                or type(self.running) not in {bool, type(None)}
                or (
                    self.resource_labels_sha256 is not None
                    and _HASH_RE.fullmatch(self.resource_labels_sha256) is None
                )
                or (
                    self.image_id is not None
                    and _IMAGE_ID_RE.fullmatch(self.image_id) is None
                )
                or (
                    self.image_repo_digest is not None
                    and _REPO_DIGEST_RE.fullmatch(self.image_repo_digest) is None
                )
                or ((self.image_id is None) != (self.image_repo_digest is None))
            ):
                raise LinuxStoreEffectsError("bound_resource_snapshot_invalid")
        elif any(
            value is not None
            for value in (
                self.resource_id,
                self.resource_labels_sha256,
                self.image_id,
                self.image_repo_digest,
                self.running,
            )
        ):
            raise LinuxStoreEffectsError("bound_resource_snapshot_invalid")


@dataclass(frozen=True, slots=True)
class LivePreflightSnapshot:
    postgres_port_free: bool
    qdrant_port_free: bool
    provider_call_count: int
    production_read_count: int

    def __post_init__(self) -> None:
        if (
            self.postgres_port_free is not True
            or self.qdrant_port_free is not True
            or type(self.provider_call_count) is not int
            or self.provider_call_count != 0
            or type(self.production_read_count) is not int
            or self.production_read_count != 0
        ):
            raise LinuxStoreEffectsError("live_preflight_snapshot_invalid")


class FreshSecret:
    """Opaque in-process material; repr, equality, and hashing never expose it."""

    __slots__ = ("_value",)

    def __init__(self, value: bytes) -> None:
        if (
            type(value) is not bytes
            or not 43 <= len(value) <= 128
            or b"\x00" in value
            or b"\n" in value
            or b"\r" in value
            or any(byte < 0x21 or byte > 0x7E for byte in value)
        ):
            raise LinuxStoreEffectsError("fresh_secret_invalid")
        self._value = value

    def __repr__(self) -> str:
        return "FreshSecret(<redacted>)"

    def __eq__(self, other: object) -> bool:
        return self is other

    __hash__ = None  # type: ignore[assignment]

    def _for_root_writer_only(self) -> bytes:
        return self._value


class FreshSecretSource(Protocol):
    def generate_postgres_password(self) -> FreshSecret: ...

    def generate_qdrant_api_key(self) -> FreshSecret: ...


class SystemFreshSecretSource:
    """Generate independent 256-bit URL-safe values without environment input."""

    @staticmethod
    def _generate() -> FreshSecret:
        return FreshSecret(base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"="))

    def generate_postgres_password(self) -> FreshSecret:
        return self._generate()

    def generate_qdrant_api_key(self) -> FreshSecret:
        return self._generate()


class SecretEnvironmentDocument:
    """Typed environment document handed only to the root-file driver."""

    __slots__ = ("logical_name", "_secret")

    def __init__(self, logical_name: str, secret: FreshSecret) -> None:
        if logical_name not in {"postgres", "qdrant"} or type(secret) is not FreshSecret:
            raise LinuxStoreEffectsError("secret_environment_document_invalid")
        self.logical_name = logical_name
        self._secret = secret

    def __repr__(self) -> str:
        return f"SecretEnvironmentDocument({self.logical_name!r}, <redacted>)"

    def _render_for_root_writer_only(self) -> bytes:
        secret = self._secret._for_root_writer_only()
        if self.logical_name == "postgres":
            return (
                b"POSTGRES_DB=postgres\n"
                b"POSTGRES_PASSWORD=" + secret + b"\n"
                b"POSTGRES_USER=governed_memory_bootstrap\n"
            )
        return b"QDRANT__SERVICE__API_KEY=" + secret + b"\n"


@dataclass(frozen=True, slots=True)
class ExactSqlArtifact:
    artifact_path: str
    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        if (
            not self.artifact_path
            or self.artifact_path.startswith("/")
            or ".." in self.artifact_path.split("/")
            or type(self.content) is not bytes
            or not 1 <= len(self.content) <= MAX_ARTIFACT_BYTES
            or b"\x00" in self.content
            or _HASH_RE.fullmatch(self.sha256) is None
            or _sha256(self.content) != self.sha256
        ):
            raise LinuxStoreEffectsError("exact_sql_artifact_invalid")


_ARTIFACT_PATHS: Final = MappingProxyType(
    {
        "canonical_bootstrap": (
            "ops/governed_memory/installation/postgres/canonical_cluster.pgsql.in"
        ),
        "canonical_rollback": (
            "ops/governed_memory/installation/postgres/"
            "canonical_cluster_rollback.pgsql.in"
        ),
        "roles_preflight": (
            "ops/governed_memory/installation/current/postgres/roles_preflight.pgsql"
        ),
        "migration_0001_forward": (
            "governed-memory-migrations/0001_foundation/forward.pgsql"
        ),
        "migration_0001_rollback": (
            "governed-memory-migrations/0001_foundation/rollback.pgsql"
        ),
        "migration_0003_forward": (
            "governed-memory-migrations/0003_owner_claim_detail/forward.pgsql"
        ),
        "migration_0003_rollback": (
            "governed-memory-migrations/0003_owner_claim_detail/rollback.pgsql"
        ),
        "migration_0004_forward": (
            "governed-memory-migrations/0004_pilot_marker/forward.pgsql"
        ),
        "migration_0004_rollback": (
            "governed-memory-migrations/0004_pilot_marker/rollback.pgsql"
        ),
    }
)


@dataclass(frozen=True, slots=True)
class ExactInstallArtifacts:
    canonical_bootstrap: ExactSqlArtifact
    canonical_rollback: ExactSqlArtifact
    roles_preflight: ExactSqlArtifact
    migration_0001_forward: ExactSqlArtifact
    migration_0001_rollback: ExactSqlArtifact
    migration_0003_forward: ExactSqlArtifact
    migration_0003_rollback: ExactSqlArtifact
    migration_0004_forward: ExactSqlArtifact
    migration_0004_rollback: ExactSqlArtifact
    qdrant_collection_document: Mapping[str, object]
    qdrant_collection_sha256: str
    qdrant_alias_document: Mapping[str, object]
    qdrant_alias_sha256: str
    supervisor_unit_template: bytes
    supervisor_unit_template_sha256: str

    @classmethod
    def from_verified_mapping(
        cls, artifacts: Mapping[str, bytes]
    ) -> ExactInstallArtifacts:
        if not isinstance(artifacts, Mapping):
            raise LinuxStoreEffectsError("verified_install_artifacts_invalid")
        sql: dict[str, ExactSqlArtifact] = {}
        for field_name, path in _ARTIFACT_PATHS.items():
            content = artifacts.get(path)
            if type(content) is not bytes:
                raise LinuxStoreEffectsError("verified_install_artifact_absent")
            sql[field_name] = ExactSqlArtifact(path, content, _sha256(content))
        collection_path = "ops/governed_memory/qdrant_collection.create.json"
        alias_path = "ops/governed_memory/qdrant_alias.create.json"
        unit_path = (
            "ops/governed_memory/installation/systemd/"
            "governed-memory-stores.service.in"
        )
        collection_raw = artifacts.get(collection_path)
        alias_raw = artifacts.get(alias_path)
        unit = artifacts.get(unit_path)
        if (
            type(collection_raw) is not bytes
            or type(alias_raw) is not bytes
            or type(unit) is not bytes
            or not 1 <= len(unit) <= 64 * 1024
        ):
            raise LinuxStoreEffectsError("verified_install_artifact_absent")
        try:
            collection = json.loads(collection_raw.decode("ascii"))
            alias = json.loads(alias_raw.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LinuxStoreEffectsError("verified_install_artifact_invalid") from None
        if collection != {
            "vectors": {"size": 3072, "distance": "Dot"},
            "on_disk_payload": True,
            "replication_factor": 1,
        } or alias != {
            "actions": [
                {
                    "create_alias": {
                        "collection_name": "governed_memory_9a54cf123493_000001",
                        "alias_name": "governed_memory_active",
                    }
                }
            ]
        }:
            raise LinuxStoreEffectsError("verified_qdrant_artifact_invalid")
        if unit.count(b"@EXECUTION_ID@") != 3 or unit.count(
            b"@RUNTIME_RECEIPT_SHA256@"
        ) != 3 or unit.count(b"@PACKAGE_MANIFEST_SHA256@") != 6:
            raise LinuxStoreEffectsError("verified_supervisor_template_invalid")
        return cls(
            **sql,
            qdrant_collection_document=MappingProxyType(collection),
            qdrant_collection_sha256=_sha256(_canonical_bytes(collection)),
            qdrant_alias_document=MappingProxyType(alias),
            qdrant_alias_sha256=_sha256(_canonical_bytes(alias)),
            supervisor_unit_template=unit,
            supervisor_unit_template_sha256=_sha256(unit),
        )

    def render_supervisor_unit(
        self,
        *,
        execution_id: str,
        runtime_receipt_sha256: str,
        package_manifest_sha256: str,
    ) -> bytes:
        if any(
            _HASH_RE.fullmatch(value) is None
            for value in (
                execution_id,
                runtime_receipt_sha256,
                package_manifest_sha256,
            )
        ):
            raise LinuxStoreEffectsError("supervisor_render_identity_invalid")
        rendered = self.supervisor_unit_template
        for placeholder, value in (
            (b"@EXECUTION_ID@", execution_id.encode("ascii")),
            (b"@RUNTIME_RECEIPT_SHA256@", runtime_receipt_sha256.encode("ascii")),
            (b"@PACKAGE_MANIFEST_SHA256@", package_manifest_sha256.encode("ascii")),
        ):
            rendered = rendered.replace(placeholder, value)
        if b"@" in rendered or len(rendered) > 64 * 1024:
            raise LinuxStoreEffectsError("supervisor_render_invalid")
        return rendered


class BoundInvariantEffects(Protocol):
    def global_execution_lock_held(self) -> bool: ...

    def inspect_live_preflight(self) -> LivePreflightSnapshot: ...


class BoundRootFileEffects(Protocol):
    """Leaf-file effects over retained root:root 0700 parent substrate."""

    def observe_retained_parent_directories(
        self,
    ) -> tuple[BoundRetainedRootDirectorySnapshot, ...]: ...

    def observe_resolved_store_spec(self) -> BoundResourceSnapshot: ...

    def observe_postgres_secret(self) -> BoundResourceSnapshot: ...

    def observe_qdrant_secret(self) -> BoundResourceSnapshot: ...

    def create_resolved_store_spec(self, canonical_document: bytes) -> None: ...

    def create_postgres_secret(self, document: SecretEnvironmentDocument) -> None: ...

    def create_qdrant_secret(self, document: SecretEnvironmentDocument) -> None: ...

    def remove_resolved_store_spec(self) -> None: ...

    def remove_postgres_secret(self) -> None: ...

    def remove_qdrant_secret(self) -> None: ...

    def read_terminal_postflight_receipt(self) -> bytes | None: ...

    def create_terminal_postflight_receipt(self, canonical_document: bytes) -> None: ...


class BoundDockerEffects(Protocol):
    def observe_network(self) -> BoundResourceSnapshot: ...

    def observe_postgres_volume(self) -> BoundResourceSnapshot: ...

    def observe_qdrant_volume(self) -> BoundResourceSnapshot: ...

    def observe_postgres_container(self) -> BoundResourceSnapshot: ...

    def observe_qdrant_container(self) -> BoundResourceSnapshot: ...

    def create_network(self) -> None: ...

    def create_postgres_volume(self) -> None: ...

    def create_qdrant_volume(self) -> None: ...

    def create_postgres_container(self) -> None: ...

    def create_qdrant_container(self) -> None: ...

    def start_postgres_container(self) -> None: ...

    def start_qdrant_container(self) -> None: ...

    def stop_postgres_container(self) -> None: ...

    def stop_qdrant_container(self) -> None: ...

    def remove_postgres_container(self) -> None: ...

    def remove_qdrant_container(self) -> None: ...

    def remove_postgres_volume(self) -> None: ...

    def remove_qdrant_volume(self) -> None: ...

    def remove_network(self) -> None: ...


class BoundPostgreSQLEffects(Protocol):
    bind: str

    def observe_canonical_bootstrap(self) -> BoundResourceSnapshot: ...

    def observe_migration_0001(self) -> BoundResourceSnapshot: ...

    def observe_migration_0003(self) -> BoundResourceSnapshot: ...

    def observe_migration_0004(self) -> BoundResourceSnapshot: ...

    def apply_canonical_bootstrap(
        self, bootstrap: ExactSqlArtifact, roles_preflight: ExactSqlArtifact
    ) -> None: ...

    def apply_migration_0001(self, artifact: ExactSqlArtifact) -> None: ...

    def apply_migration_0003(self, artifact: ExactSqlArtifact) -> None: ...

    def apply_migration_0004(self, artifact: ExactSqlArtifact) -> None: ...

    def rollback_migration_0004(self, artifact: ExactSqlArtifact) -> None: ...

    def rollback_migration_0003(self, artifact: ExactSqlArtifact) -> None: ...

    def rollback_migration_0001(self, artifact: ExactSqlArtifact) -> None: ...

    def rollback_canonical_bootstrap(self, artifact: ExactSqlArtifact) -> None: ...

    def inspect_prebootstrap(self) -> object: ...

    def inspect_terminal(self) -> object: ...


class BoundQdrantEffects(Protocol):
    bind: str

    def observe_collection(self) -> BoundResourceSnapshot: ...

    def observe_alias(self) -> BoundResourceSnapshot: ...

    def create_collection(self) -> None: ...

    def create_alias(self) -> None: ...

    def remove_alias(self) -> None: ...

    def remove_collection(self) -> None: ...

    def inspect_prebootstrap(self) -> object: ...

    def inspect_terminal(self) -> object: ...


class BoundSystemdEffects(Protocol):
    """Fixed systemd effects; parent directories are never rollback resources."""

    def observe_supervisor(self) -> BoundSystemdSupervisorSnapshot: ...

    def install_and_enable_supervisor(
        self, exact: ExactSystemdSupervisor
    ) -> None:
        """Create the exact unit and exact enablement symlink."""
        ...

    def disable_and_remove_supervisor(
        self, exact: ExactSystemdSupervisor
    ) -> None:
        """Remove both fixed nodes, retaining their preinstalled parents."""
        ...


@dataclass(frozen=True, slots=True)
class BoundLinuxStoreTransports:
    invariants: BoundInvariantEffects
    files: BoundRootFileEffects
    docker: BoundDockerEffects
    postgres: BoundPostgreSQLEffects
    qdrant: BoundQdrantEffects
    systemd: BoundSystemdEffects

    def __post_init__(self) -> None:
        required = {
            "invariants": ("global_execution_lock_held", "inspect_live_preflight"),
            "files": (
                "observe_retained_parent_directories",
                "observe_resolved_store_spec",
                "observe_postgres_secret",
                "observe_qdrant_secret",
                "create_resolved_store_spec",
                "create_postgres_secret",
                "create_qdrant_secret",
                "remove_resolved_store_spec",
                "remove_postgres_secret",
                "remove_qdrant_secret",
                "read_terminal_postflight_receipt",
                "create_terminal_postflight_receipt",
            ),
            "docker": (
                "observe_network",
                "observe_postgres_volume",
                "observe_qdrant_volume",
                "observe_postgres_container",
                "observe_qdrant_container",
                "create_network",
                "create_postgres_volume",
                "create_qdrant_volume",
                "create_postgres_container",
                "create_qdrant_container",
                "start_postgres_container",
                "start_qdrant_container",
                "stop_postgres_container",
                "stop_qdrant_container",
                "remove_postgres_container",
                "remove_qdrant_container",
                "remove_postgres_volume",
                "remove_qdrant_volume",
                "remove_network",
            ),
            "postgres": (
                "observe_canonical_bootstrap",
                "observe_migration_0001",
                "observe_migration_0003",
                "observe_migration_0004",
                "apply_canonical_bootstrap",
                "apply_migration_0001",
                "apply_migration_0003",
                "apply_migration_0004",
                "rollback_migration_0004",
                "rollback_migration_0003",
                "rollback_migration_0001",
                "rollback_canonical_bootstrap",
                "inspect_prebootstrap",
                "inspect_terminal",
            ),
            "qdrant": (
                "observe_collection",
                "observe_alias",
                "create_collection",
                "create_alias",
                "remove_alias",
                "remove_collection",
                "inspect_prebootstrap",
                "inspect_terminal",
            ),
            "systemd": (
                "observe_supervisor",
                "install_and_enable_supervisor",
                "disable_and_remove_supervisor",
            ),
        }
        for field_name, methods in required.items():
            value = getattr(self, field_name)
            if any(not callable(getattr(value, method, None)) for method in methods):
                raise LinuxStoreEffectsError("bound_linux_transport_invalid")
        if (
            getattr(self.postgres, "bind", None) != POSTGRES_BIND
            or getattr(self.qdrant, "bind", None) != QDRANT_BIND
        ):
            raise LinuxStoreEffectsError("bound_linux_transport_endpoint_invalid")


class BoundLinuxStoreTransportFactory(Protocol):
    def __call__(
        self,
        *,
        execution_id: str,
        attempt_id: str,
        execution_binding_sha256: str,
        resolved_store_spec: Mapping[str, object],
        artifacts: ExactInstallArtifacts,
        prerequisites: InstallPrerequisites,
    ) -> BoundLinuxStoreTransports: ...


_EFFECT_PROFILE_BY_STEP: Final = {
    step.step_id: HostOperationProfile(step.effect) for step in STORES_ONLY_PLAN
}
_ROLLBACK_PROFILE_BY_STEP: Final = {
    step.step_id: (
        HostOperationProfile(step.rollback) if step.compensable else None
    )
    for step in STORES_ONLY_PLAN
}
_INVARIANT_PROFILES: Final = frozenset(
    HostOperationProfile(step.effect)
    for step in STORES_ONLY_PLAN
    if step.invariant_only
)
_RESOURCE_KEYS_BY_STEP: Final = {
    "I04_WRITE_RESOLVED_STORE_SPEC_AND_GENERATE_FRESH_STORE_SECRETS": (
        "resolved_store_spec",
        "qdrant_store_secret",
        "postgres_store_secret",
    ),
    "I05_CREATE_EXACT_NETWORK": ("network",),
    "I06_CREATE_EXACT_POSTGRES_VOLUME": ("postgres_volume",),
    "I07_CREATE_EXACT_QDRANT_VOLUME": ("qdrant_volume",),
    "I08_CREATE_EXACT_POSTGRES_CONTAINER": ("postgres_container",),
    "I09_CREATE_EXACT_QDRANT_CONTAINER": ("qdrant_container",),
    "I11_BOOTSTRAP_CANONICAL_DATABASE": ("canonical_database_and_roles",),
    "I12_APPLY_FOUNDATION_0001": ("migration_0001",),
    "I13_APPLY_OWNER_CLAIM_DETAIL_0003": ("migration_0003",),
    "I14_APPLY_PILOT_MARKER_0004": ("migration_0004",),
    "I15_CREATE_EMPTY_QDRANT_COLLECTION": ("qdrant_collection",),
    "I16_CREATE_QDRANT_ALIAS": ("qdrant_alias",),
    "I18_INSTALL_AND_ENABLE_STORES_SUPERVISOR": ("stores_supervisor",),
}


@dataclass(frozen=True, slots=True)
class _ExecutionIdentity:
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


class LinuxStoreHostOperations:
    """Concrete exact-step dispatcher used by ``ClaimBoundInstallBackend``."""

    def __init__(
        self,
        *,
        identity: _ExecutionIdentity,
        resolved_store_spec: Mapping[str, object],
        artifacts: ExactInstallArtifacts,
        prerequisites: InstallPrerequisites,
        transports: BoundLinuxStoreTransports,
        readiness_probe: ClosedStoreReadinessProbe,
        secret_source: FreshSecretSource,
    ) -> None:
        spec = validate_store_spec(resolved_store_spec, allow_placeholders=False)
        if (
            type(identity) is not _ExecutionIdentity
            or type(artifacts) is not ExactInstallArtifacts
            or not hasattr(prerequisites, "local_images")
            or type(transports) is not BoundLinuxStoreTransports
            or type(readiness_probe) is not ClosedStoreReadinessProbe
            or not callable(getattr(secret_source, "generate_postgres_password", None))
            or not callable(getattr(secret_source, "generate_qdrant_api_key", None))
        ):
            raise LinuxStoreEffectsError("linux_store_host_dependencies_invalid")
        self._identity = identity
        self._spec = spec
        self._artifacts = artifacts
        self._prerequisites = prerequisites
        self._transports = transports
        self._readiness = readiness_probe
        self._secrets = secret_source
        self._expected_names = _expected_rollback_resource_names(
            identity.package_manifest_sha256
        )
        resources = spec["resources"]
        network = resources["network"]
        volumes = resources["volumes"]
        containers = resources["containers"]
        self._labels_sha256 = {
            ("network", network["name"]): canonical_labels_sha256(network["labels"]),
            ("volume", volumes["postgres"]["name"]): canonical_labels_sha256(
                volumes["postgres"]["labels"]
            ),
            ("volume", volumes["qdrant"]["name"]): canonical_labels_sha256(
                volumes["qdrant"]["labels"]
            ),
            ("container", containers["postgres"]["name"]): canonical_labels_sha256(
                containers["postgres"]["labels"]
            ),
            ("container", containers["qdrant"]["name"]): canonical_labels_sha256(
                containers["qdrant"]["labels"]
            ),
        }
        self._supervisor = ExactSystemdSupervisor.from_rendered_unit(
            artifacts.render_supervisor_unit(
                execution_id=identity.execution_id,
                runtime_receipt_sha256=identity.controller_runtime_receipt_sha256,
                package_manifest_sha256=identity.package_manifest_sha256,
            )
        )
        self._resolved_store_spec_bytes = _canonical_bytes(spec) + b"\n"
        if _sha256(_canonical_bytes(spec)) != identity.resolved_store_spec_sha256:
            raise LinuxStoreEffectsError("linux_store_spec_identity_mismatch")

    def _expected_targets(self, step_id: str) -> tuple[HostResourceTarget, ...]:
        return tuple(
            HostResourceTarget(
                *self._expected_names[key],
                resource_labels_sha256=self._labels_sha256.get(
                    self._expected_names[key]
                ),
            )
            for key in _RESOURCE_KEYS_BY_STEP.get(step_id, ())
        )

    def _validate_request(self, request: HostOperationRequest) -> bool:
        if type(request) is not HostOperationRequest:
            raise LinuxStoreEffectsError("linux_store_request_invalid")
        effect = _EFFECT_PROFILE_BY_STEP.get(request.step_id)
        rollback = _ROLLBACK_PROFILE_BY_STEP.get(request.step_id)
        if request.profile not in {effect, rollback}:
            raise LinuxStoreEffectsError("linux_store_request_profile_invalid")
        expected_values = (
            self._identity.execution_id,
            self._identity.attempt_id,
            self._identity.execution_binding_sha256,
            self._identity.package_manifest_sha256,
            self._identity.resolved_store_spec_sha256,
            self._identity.controller_runtime_receipt_sha256,
            self._identity.controller_runtime_root,
            self._identity.controller_runtime_tree_sha256,
            self._identity.controller_release_root,
            self._identity.controller_release_tree_sha256,
            self._identity.controller_release_package_manifest_path,
            self._identity.controller_runtime_interpreter_path,
            self._identity.controller_runtime_interpreter_sha256,
            self._identity.controller_runtime_inventory_path,
            self._identity.controller_runtime_inventory_sha256,
            self._identity.controller_requirements_lock_sha256,
            self._identity.supervisor_launcher_path,
            self._identity.supervisor_launcher_sha256,
        )
        observed_values = (
            request.execution_id,
            request.attempt_id,
            request.execution_binding_sha256,
            request.package_manifest_sha256,
            request.resolved_store_spec_sha256,
            request.controller_runtime_receipt_sha256,
            request.controller_runtime_root,
            request.controller_runtime_tree_sha256,
            request.controller_release_root,
            request.controller_release_tree_sha256,
            request.controller_release_package_manifest_path,
            request.controller_runtime_interpreter_path,
            request.controller_runtime_interpreter_sha256,
            request.controller_runtime_inventory_path,
            request.controller_runtime_inventory_sha256,
            request.controller_requirements_lock_sha256,
            request.supervisor_launcher_path,
            request.supervisor_launcher_sha256,
        )
        expected_postflight = POSTFLIGHT_TEMPLATE.format(
            execution_id=self._identity.execution_id
        )
        if (
            observed_values != expected_values
            or request.resource_targets != self._expected_targets(request.step_id)
            or request.postflight_receipt_path != expected_postflight
        ):
            raise LinuxStoreEffectsError("linux_store_request_binding_mismatch")
        return request.profile is rollback

    def _snapshot_for_resource_key(self, key: str) -> BoundResourceSnapshot:
        files = self._transports.files
        docker = self._transports.docker
        postgres = self._transports.postgres
        qdrant = self._transports.qdrant
        if key == "stores_supervisor":
            try:
                supervisor = self._transports.systemd.observe_supervisor()
            except Exception:
                raise LinuxStoreEffectsError(
                    "linux_store_observation_failed"
                ) from None
            if type(supervisor) is not BoundSystemdSupervisorSnapshot:
                raise LinuxStoreEffectsError("linux_store_observation_invalid")
            return supervisor.classify(self._supervisor)
        readers = {
            "resolved_store_spec": files.observe_resolved_store_spec,
            "postgres_store_secret": files.observe_postgres_secret,
            "qdrant_store_secret": files.observe_qdrant_secret,
            "network": docker.observe_network,
            "postgres_volume": docker.observe_postgres_volume,
            "qdrant_volume": docker.observe_qdrant_volume,
            "postgres_container": docker.observe_postgres_container,
            "qdrant_container": docker.observe_qdrant_container,
            "canonical_database_and_roles": postgres.observe_canonical_bootstrap,
            "migration_0001": postgres.observe_migration_0001,
            "migration_0003": postgres.observe_migration_0003,
            "migration_0004": postgres.observe_migration_0004,
            "qdrant_collection": qdrant.observe_collection,
            "qdrant_alias": qdrant.observe_alias,
        }
        reader = readers.get(key)
        if reader is None:
            raise LinuxStoreEffectsError("linux_store_resource_key_invalid")
        try:
            snapshot = reader()
        except Exception:
            raise LinuxStoreEffectsError("linux_store_observation_failed") from None
        if type(snapshot) is not BoundResourceSnapshot:
            raise LinuxStoreEffectsError("linux_store_observation_invalid")
        return snapshot

    @staticmethod
    def _aggregate_presence(
        snapshots: tuple[BoundResourceSnapshot, ...]
    ) -> EffectPresence:
        if not snapshots:
            raise LinuxStoreEffectsError("linux_store_observation_empty")
        presences = tuple(item.presence for item in snapshots)
        if EffectPresence.DRIFT in presences or EffectPresence.PARTIAL in presences:
            return EffectPresence.DRIFT
        if all(item is EffectPresence.ABSENT for item in presences):
            return EffectPresence.ABSENT
        if all(item is EffectPresence.EXACT for item in presences):
            return EffectPresence.EXACT
        return EffectPresence.PARTIAL

    def _all_pre_supervisor_snapshots(self) -> tuple[BoundResourceSnapshot, ...]:
        keys = (
            "resolved_store_spec",
            "qdrant_store_secret",
            "postgres_store_secret",
            "network",
            "postgres_volume",
            "qdrant_volume",
            "postgres_container",
            "qdrant_container",
            "canonical_database_and_roles",
            "migration_0001",
            "migration_0003",
            "migration_0004",
            "qdrant_collection",
            "qdrant_alias",
        )
        return tuple(self._snapshot_for_resource_key(key) for key in keys)

    def _retained_parent_directories_are_exact(self) -> bool:
        try:
            snapshots = (
                self._transports.files.observe_retained_parent_directories()
            )
        except Exception:
            return False
        return (
            type(snapshots) is tuple
            and len(snapshots) == len(RETAINED_ROOT_DIRECTORY_PATHS)
            and all(
                type(item) is BoundRetainedRootDirectorySnapshot
                for item in snapshots
            )
            and tuple(item.path for item in snapshots)
            == RETAINED_ROOT_DIRECTORY_PATHS
        )

    def _observe_invariant(
        self, request: HostOperationRequest
    ) -> tuple[str, tuple[BoundResourceSnapshot, ...], str | None]:
        if request.profile is HostOperationProfile.REVERIFY_PRECLAIMED_EXECUTION_LOCK:
            try:
                held = self._transports.invariants.global_execution_lock_held()
            except Exception:
                held = False
            return ("after" if held is True else "drift"), (), None
        if request.profile is HostOperationProfile.VERIFY_CLAIMED_EXECUTION_BINDING:
            return "after", (), None
        if request.profile is HostOperationProfile.VERIFY_LIVE_PREFLIGHT:
            try:
                preflight = self._transports.invariants.inspect_live_preflight()
                resource_snapshots = self._all_pre_supervisor_snapshots() + (
                    self._snapshot_for_resource_key("stores_supervisor"),
                )
                postflight = self._transports.files.read_terminal_postflight_receipt()
            except Exception:
                return "drift", (), None
            exact = (
                type(preflight) is LivePreflightSnapshot
                and self._retained_parent_directories_are_exact()
                and all(
                    item.presence is EffectPresence.ABSENT
                    for item in resource_snapshots
                )
                and postflight is None
            )
            return ("after" if exact else "drift"), (), None
        if request.profile is HostOperationProfile.VERIFY_PRE_SUPERVISOR_RESOURCE_IDENTITIES:
            snapshots = self._all_pre_supervisor_snapshots()
            containers = (
                self._transports.docker.observe_postgres_container(),
                self._transports.docker.observe_qdrant_container(),
            )
            exact = (
                all(item.presence is EffectPresence.EXACT for item in snapshots)
                and all(
                    type(item) is BoundResourceSnapshot
                    and item.presence is EffectPresence.EXACT
                    and item.running is True
                    for item in containers
                )
            )
            return ("after" if exact else "drift"), (), None
        raise LinuxStoreEffectsError("linux_store_invariant_profile_invalid")

    def _postflight_receipt(
        self,
        *,
        request: HostOperationRequest,
        postgres: BoundResourceSnapshot,
        qdrant: BoundResourceSnapshot,
        terminal_readiness_sha256: str,
    ) -> dict[str, object]:
        unsigned: dict[str, object] = {
            "schema_version": "governed-memory-terminal-postflight-receipt-v1",
            "execution_id": request.execution_id,
            "attempt_id": request.attempt_id,
            "execution_binding_sha256": request.execution_binding_sha256,
            "package_manifest_sha256": request.package_manifest_sha256,
            "resolved_store_spec_sha256": request.resolved_store_spec_sha256,
            "controller_runtime_receipt_sha256": (
                request.controller_runtime_receipt_sha256
            ),
            "controller_release_tree_sha256": request.controller_release_tree_sha256,
            "postgres_container_id": postgres.resource_id,
            "qdrant_container_id": qdrant.resource_id,
            "terminal_store_readiness_sha256": terminal_readiness_sha256,
            "provider_call_count": 0,
            "production_read_count": 0,
        }
        return {**unsigned, "receipt_sha256": _sha256(_canonical_bytes(unsigned))}

    def _verify_postflight_receipt(
        self, request: HostOperationRequest, raw: bytes
    ) -> str | None:
        if type(raw) is not bytes or not 1 <= len(raw) <= MAX_PUBLIC_RECEIPT_BYTES:
            return None
        try:
            value = json.loads(raw.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if type(value) is not dict or set(value) != {
            "schema_version",
            "execution_id",
            "attempt_id",
            "execution_binding_sha256",
            "package_manifest_sha256",
            "resolved_store_spec_sha256",
            "controller_runtime_receipt_sha256",
            "controller_release_tree_sha256",
            "postgres_container_id",
            "qdrant_container_id",
            "terminal_store_readiness_sha256",
            "provider_call_count",
            "production_read_count",
            "receipt_sha256",
        }:
            return None
        postgres = self._transports.docker.observe_postgres_container()
        qdrant = self._transports.docker.observe_qdrant_container()
        if (
            type(postgres) is not BoundResourceSnapshot
            or type(qdrant) is not BoundResourceSnapshot
            or postgres.presence is not EffectPresence.EXACT
            or qdrant.presence is not EffectPresence.EXACT
            or postgres.running is not True
            or qdrant.running is not True
        ):
            return None
        try:
            terminal = self._readiness.verify_terminal_canonical_stores()
        except Exception:
            return None
        expected = self._postflight_receipt(
            request=request,
            postgres=postgres,
            qdrant=qdrant,
            terminal_readiness_sha256=terminal.receipt_sha256,
        )
        if value != expected:
            return None
        canonical = _canonical_bytes(value) + b"\n"
        if raw != canonical:
            return None
        return value["receipt_sha256"]

    def _observe_postflight(
        self, request: HostOperationRequest
    ) -> tuple[str, tuple[BoundResourceSnapshot, ...], str | None]:
        try:
            raw = self._transports.files.read_terminal_postflight_receipt()
        except Exception:
            return "drift", (), None
        if raw is None:
            return "before", (), None
        receipt_sha256 = self._verify_postflight_receipt(request, raw)
        if receipt_sha256 is None:
            return "drift", (), None
        return "after", (), receipt_sha256

    def _observe_profile(
        self, request: HostOperationRequest
    ) -> tuple[str, tuple[BoundResourceSnapshot, ...], str | None]:
        if request.profile in _INVARIANT_PROFILES:
            return self._observe_invariant(request)
        if request.step_id == "I10_START_AND_VERIFY_EMPTY_STORES":
            postgres = self._transports.docker.observe_postgres_container()
            qdrant = self._transports.docker.observe_qdrant_container()
            snapshots = (postgres, qdrant)
            if any(
                type(item) is not BoundResourceSnapshot
                or item.presence is not EffectPresence.EXACT
                for item in snapshots
            ):
                return "drift", (), None
            running = (postgres.running, qdrant.running)
            if running == (False, False):
                return "before", (), None
            if running == (True, True):
                return "after", (), None
            return "recoverable", (), None
        if request.step_id == "I19_COLD_RESTART_AND_VERIFY_TERMINAL_POSTFLIGHT":
            return self._observe_postflight(request)
        if (
            request.step_id
            == "I04_WRITE_RESOLVED_STORE_SPEC_AND_GENERATE_FRESH_STORE_SECRETS"
            and not self._retained_parent_directories_are_exact()
        ):
            return "drift", (), None
        keys = _RESOURCE_KEYS_BY_STEP.get(request.step_id)
        if not keys:
            raise LinuxStoreEffectsError("linux_store_step_resources_invalid")
        snapshots = tuple(self._snapshot_for_resource_key(key) for key in keys)
        presence = self._aggregate_presence(snapshots)
        state = {
            EffectPresence.ABSENT: "before",
            EffectPresence.EXACT: "after",
            EffectPresence.PARTIAL: "recoverable",
            EffectPresence.DRIFT: "drift",
        }[presence]
        return state, snapshots, None

    def _expected_non_docker_resource_id(
        self, resource_kind: str, resource_name: str
    ) -> str | None:
        expected = self._expected_names
        by_identity = {
            expected["resolved_store_spec"]: self._identity.resolved_store_spec_sha256,
            expected["postgres_store_secret"]: POSTGRES_STORE_SECRET_PUBLIC_ID,
            expected["qdrant_store_secret"]: QDRANT_STORE_SECRET_PUBLIC_ID,
            expected["canonical_database_and_roles"]: (
                self._artifacts.canonical_bootstrap.sha256
            ),
            expected["migration_0001"]: self._artifacts.migration_0001_forward.sha256,
            expected["migration_0003"]: self._artifacts.migration_0003_forward.sha256,
            expected["migration_0004"]: self._artifacts.migration_0004_forward.sha256,
            expected["qdrant_collection"]: self._artifacts.qdrant_collection_sha256,
            expected["qdrant_alias"]: self._artifacts.qdrant_alias_sha256,
            expected["stores_supervisor"]: (
                self._supervisor.composite_identity_sha256
            ),
        }
        return by_identity.get((resource_kind, resource_name))

    def _identity_receipt(
        self,
        request: HostOperationRequest,
        target: HostResourceTarget,
        snapshot: BoundResourceSnapshot,
    ) -> HostResourceIdentityReceipt:
        if snapshot.presence is not EffectPresence.EXACT or snapshot.resource_id is None:
            raise LinuxStoreEffectsError("linux_store_exact_identity_absent")
        if target.resource_kind in {"container", "network", "volume"}:
            if snapshot.resource_labels_sha256 != target.resource_labels_sha256:
                raise LinuxStoreEffectsError("linux_store_label_identity_mismatch")
        elif snapshot.resource_labels_sha256 is not None:
            raise LinuxStoreEffectsError("linux_store_label_identity_unexpected")
        expected_id = self._expected_non_docker_resource_id(
            target.resource_kind, target.resource_name
        )
        if expected_id is not None and snapshot.resource_id != expected_id:
            raise LinuxStoreEffectsError("linux_store_resource_identity_mismatch")
        if target.resource_kind == "container":
            expected_image = (
                self._prerequisites.local_images.postgres
                if "postgres" in target.resource_name
                else self._prerequisites.local_images.qdrant
            )
            if (
                _CONTAINER_ID_RE.fullmatch(snapshot.resource_id) is None
                or snapshot.image_id != expected_image.image_id
                or snapshot.image_repo_digest != expected_image.repo_digest
            ):
                raise LinuxStoreEffectsError("linux_store_container_identity_mismatch")
        elif snapshot.image_id is not None or snapshot.image_repo_digest is not None:
            raise LinuxStoreEffectsError("linux_store_image_identity_unexpected")
        ownership = _resource_ownership(
            request,
            resource_kind=target.resource_kind,
            resource_name=target.resource_name,
            resource_id=snapshot.resource_id,
            resource_labels_sha256=snapshot.resource_labels_sha256,
            image_id=snapshot.image_id,
            image_repo_digest=snapshot.image_repo_digest,
        )
        return HostResourceIdentityReceipt(
            resource_kind=target.resource_kind,
            resource_name=target.resource_name,
            resource_id=snapshot.resource_id,
            ownership_sha256=ownership,
            resource_labels_sha256=snapshot.resource_labels_sha256,
            image_id=snapshot.image_id,
            image_repo_digest=snapshot.image_repo_digest,
        )

    def observe(self, request: HostOperationRequest) -> HostObservation:
        self._validate_request(request)
        try:
            state, snapshots, postflight_sha256 = self._observe_profile(request)
            identities: tuple[HostResourceIdentityReceipt, ...] = ()
            if state == "after" and request.resource_targets:
                if len(snapshots) != len(request.resource_targets):
                    raise LinuxStoreEffectsError(
                        "linux_store_observation_identity_incomplete"
                    )
                identities = tuple(
                    self._identity_receipt(request, target, snapshot)
                    for target, snapshot in zip(
                        request.resource_targets, snapshots, strict=True
                    )
                )
            revision = _sha256(
                _canonical_bytes(
                    {
                        "schema_version": "governed-memory-linux-observation-v1",
                        "profile": request.profile.value,
                        "step_id": request.step_id,
                        "state": state,
                        "resources": [
                            {
                                "image_id": item.image_id,
                                "image_repo_digest": item.image_repo_digest,
                                "presence": item.presence.value,
                                "resource_id": item.resource_id,
                                "resource_labels_sha256": (
                                    item.resource_labels_sha256
                                ),
                                "running": item.running,
                            }
                            for item in snapshots
                        ],
                        "postflight_receipt_sha256": postflight_sha256,
                    }
                )
            )
            ownership = _host_observation_ownership(request, revision)
            return HostObservation(
                state,
                revision,
                ownership,
                identities,
                postflight_sha256,
            )
        except (LinuxStoreEffectsError, HostBoundaryError):
            raise
        except Exception:
            raise LinuxStoreEffectsError("linux_store_observation_failed") from None

    def _create_files_if_absent(self) -> None:
        files = self._transports.files
        observed = files.observe_resolved_store_spec()
        if observed.presence is EffectPresence.ABSENT:
            files.create_resolved_store_spec(self._resolved_store_spec_bytes)
        elif observed.presence is not EffectPresence.EXACT:
            raise LinuxStoreEffectsError("resolved_store_spec_not_recoverable")
        observed = files.observe_qdrant_secret()
        if observed.presence is EffectPresence.ABSENT:
            secret = self._secrets.generate_qdrant_api_key()
            files.create_qdrant_secret(SecretEnvironmentDocument("qdrant", secret))
        elif observed.presence is not EffectPresence.EXACT:
            raise LinuxStoreEffectsError("qdrant_secret_not_recoverable")
        observed = files.observe_postgres_secret()
        if observed.presence is EffectPresence.ABSENT:
            secret = self._secrets.generate_postgres_password()
            files.create_postgres_secret(
                SecretEnvironmentDocument("postgres", secret)
            )
        elif observed.presence is not EffectPresence.EXACT:
            raise LinuxStoreEffectsError("postgres_secret_not_recoverable")

    @staticmethod
    def _run_if_absent(
        observe: object,
        mutate: object,
    ) -> None:
        if not callable(observe) or not callable(mutate):
            raise LinuxStoreEffectsError("linux_store_bound_effect_invalid")
        snapshot = observe()
        if type(snapshot) is not BoundResourceSnapshot:
            raise LinuxStoreEffectsError("linux_store_bound_effect_invalid")
        if snapshot.presence is EffectPresence.ABSENT:
            mutate()
        elif snapshot.presence is not EffectPresence.EXACT:
            raise LinuxStoreEffectsError("linux_store_effect_not_recoverable")

    def _apply_postflight(self, request: HostOperationRequest) -> None:
        docker = self._transports.docker
        postgres = docker.observe_postgres_container()
        qdrant = docker.observe_qdrant_container()
        if any(
            type(item) is not BoundResourceSnapshot
            or item.presence is not EffectPresence.EXACT
            for item in (postgres, qdrant)
        ):
            raise LinuxStoreEffectsError("postflight_container_identity_invalid")
        if qdrant.running is True:
            docker.stop_qdrant_container()
        if postgres.running is True:
            docker.stop_postgres_container()
        stopped = (
            docker.observe_postgres_container(),
            docker.observe_qdrant_container(),
        )
        if any(item.running is not False for item in stopped):
            raise LinuxStoreEffectsError("postflight_stop_failed")
        docker.start_postgres_container()
        docker.start_qdrant_container()
        started = (
            docker.observe_postgres_container(),
            docker.observe_qdrant_container(),
        )
        if any(item.running is not True for item in started):
            raise LinuxStoreEffectsError("postflight_start_failed")
        terminal = self._readiness.verify_terminal_canonical_stores()
        receipt = self._postflight_receipt(
            request=request,
            postgres=started[0],
            qdrant=started[1],
            terminal_readiness_sha256=terminal.receipt_sha256,
        )
        self._transports.files.create_terminal_postflight_receipt(
            _canonical_bytes(receipt) + b"\n"
        )

    def _apply_effect(self, request: HostOperationRequest) -> None:
        profile = request.profile
        t = self._transports
        a = self._artifacts
        if profile in _INVARIANT_PROFILES:
            return
        if profile is HostOperationProfile.WRITE_RESOLVED_STORE_SPEC_AND_GENERATE_FRESH_STORE_SECRETS:
            self._create_files_if_absent()
        elif profile is HostOperationProfile.CREATE_EXACT_NETWORK:
            self._run_if_absent(t.docker.observe_network, t.docker.create_network)
        elif profile is HostOperationProfile.CREATE_EXACT_POSTGRES_VOLUME:
            self._run_if_absent(
                t.docker.observe_postgres_volume, t.docker.create_postgres_volume
            )
        elif profile is HostOperationProfile.CREATE_EXACT_QDRANT_VOLUME:
            self._run_if_absent(
                t.docker.observe_qdrant_volume, t.docker.create_qdrant_volume
            )
        elif profile is HostOperationProfile.CREATE_EXACT_POSTGRES_CONTAINER:
            self._run_if_absent(
                t.docker.observe_postgres_container,
                t.docker.create_postgres_container,
            )
        elif profile is HostOperationProfile.CREATE_EXACT_QDRANT_CONTAINER:
            self._run_if_absent(
                t.docker.observe_qdrant_container,
                t.docker.create_qdrant_container,
            )
        elif profile is HostOperationProfile.START_AND_VERIFY_EMPTY_STORES:
            postgres = t.docker.observe_postgres_container()
            qdrant = t.docker.observe_qdrant_container()
            if postgres.running is not True:
                t.docker.start_postgres_container()
            if qdrant.running is not True:
                t.docker.start_qdrant_container()
        elif profile is HostOperationProfile.BOOTSTRAP_CANONICAL_DATABASE:
            self._run_if_absent(
                t.postgres.observe_canonical_bootstrap,
                lambda: t.postgres.apply_canonical_bootstrap(
                    a.canonical_bootstrap, a.roles_preflight
                ),
            )
        elif profile is HostOperationProfile.APPLY_FOUNDATION_0001:
            self._run_if_absent(
                t.postgres.observe_migration_0001,
                lambda: t.postgres.apply_migration_0001(
                    a.migration_0001_forward
                ),
            )
        elif profile is HostOperationProfile.APPLY_OWNER_CLAIM_DETAIL_0003:
            self._run_if_absent(
                t.postgres.observe_migration_0003,
                lambda: t.postgres.apply_migration_0003(
                    a.migration_0003_forward
                ),
            )
        elif profile is HostOperationProfile.APPLY_PILOT_MARKER_0004:
            self._run_if_absent(
                t.postgres.observe_migration_0004,
                lambda: t.postgres.apply_migration_0004(
                    a.migration_0004_forward
                ),
            )
        elif profile is HostOperationProfile.CREATE_EMPTY_QDRANT_COLLECTION:
            self._run_if_absent(
                t.qdrant.observe_collection, t.qdrant.create_collection
            )
        elif profile is HostOperationProfile.CREATE_QDRANT_ALIAS:
            self._run_if_absent(t.qdrant.observe_alias, t.qdrant.create_alias)
        elif profile is HostOperationProfile.INSTALL_AND_ENABLE_STORES_SUPERVISOR:
            self._run_if_absent(
                lambda: t.systemd.observe_supervisor().classify(self._supervisor),
                lambda: t.systemd.install_and_enable_supervisor(
                    self._supervisor
                ),
            )
        elif profile is HostOperationProfile.COLD_RESTART_AND_VERIFY_TERMINAL_POSTFLIGHT:
            self._apply_postflight(request)
        else:
            raise LinuxStoreEffectsError("linux_store_effect_profile_refused")

    def apply(
        self, request: HostOperationRequest, expected: HostObservation
    ) -> HostApplyResult:
        compensation = self._validate_request(request)
        if compensation:
            raise LinuxStoreEffectsError("linux_store_effect_direction_invalid")
        current = self.observe(request)
        if type(expected) is not HostObservation or current != expected:
            raise LinuxStoreEffectsError("linux_store_observation_changed")
        allowed = {"after"} if request.profile in _INVARIANT_PROFILES else {
            "before",
            "recoverable",
        }
        if current.state not in allowed:
            raise LinuxStoreEffectsError("linux_store_apply_state_invalid")
        try:
            self._apply_effect(request)
        except LinuxStoreEffectsError:
            raise
        except Exception:
            raise LinuxStoreEffectsError("linux_store_effect_failed") from None
        final = self.observe(request)
        if final.state != "after":
            raise LinuxStoreEffectsError("linux_store_effect_not_observed")
        return HostApplyResult(final.identities, final.postflight_receipt_sha256)

    def _compensate_effect(self, request: HostOperationRequest) -> None:
        profile = request.profile
        t = self._transports
        a = self._artifacts
        if profile is HostOperationProfile.REMOVE_RESOLVED_STORE_SPEC_AND_FRESH_STORE_SECRETS:
            t.files.remove_qdrant_secret()
            t.files.remove_postgres_secret()
            t.files.remove_resolved_store_spec()
        elif profile is HostOperationProfile.REMOVE_EXACT_UNUSED_NETWORK:
            t.docker.remove_network()
        elif profile is HostOperationProfile.REMOVE_EXACT_EMPTY_POSTGRES_VOLUME:
            t.docker.remove_postgres_volume()
        elif profile is HostOperationProfile.REMOVE_EXACT_EMPTY_QDRANT_VOLUME:
            t.docker.remove_qdrant_volume()
        elif profile is HostOperationProfile.REMOVE_EXACT_POSTGRES_CONTAINER:
            t.docker.remove_postgres_container()
        elif profile is HostOperationProfile.REMOVE_EXACT_QDRANT_CONTAINER:
            t.docker.remove_qdrant_container()
        elif profile is HostOperationProfile.STOP_EXACT_STORES:
            postgres = t.docker.observe_postgres_container()
            qdrant = t.docker.observe_qdrant_container()
            if qdrant.running is True:
                t.docker.stop_qdrant_container()
            if postgres.running is True:
                t.docker.stop_postgres_container()
        elif profile is HostOperationProfile.DROP_EMPTY_CANONICAL_DATABASE_AND_ROLES:
            t.postgres.rollback_canonical_bootstrap(a.canonical_rollback)
        elif profile is HostOperationProfile.ROLLBACK_EMPTY_FOUNDATION_0001:
            t.postgres.rollback_migration_0001(a.migration_0001_rollback)
        elif profile is HostOperationProfile.ROLLBACK_OWNER_CLAIM_DETAIL_0003:
            t.postgres.rollback_migration_0003(a.migration_0003_rollback)
        elif profile is HostOperationProfile.ROLLBACK_EMPTY_PILOT_MARKER_0004:
            t.postgres.rollback_migration_0004(a.migration_0004_rollback)
        elif profile is HostOperationProfile.REMOVE_EXACT_EMPTY_QDRANT_COLLECTION:
            t.qdrant.remove_collection()
        elif profile is HostOperationProfile.REMOVE_EXACT_QDRANT_ALIAS:
            t.qdrant.remove_alias()
        elif profile is HostOperationProfile.DISABLE_AND_REMOVE_STORES_SUPERVISOR:
            t.systemd.disable_and_remove_supervisor(self._supervisor)
        else:
            raise LinuxStoreEffectsError("linux_store_compensation_profile_refused")

    def compensate(
        self, request: HostOperationRequest, expected: HostObservation
    ) -> HostApplyResult:
        compensation = self._validate_request(request)
        if not compensation:
            raise LinuxStoreEffectsError("linux_store_effect_direction_invalid")
        current = self.observe(request)
        if (
            type(expected) is not HostObservation
            or current != expected
            or current.state not in {"after", "recoverable"}
        ):
            raise LinuxStoreEffectsError("linux_store_compensation_state_invalid")
        try:
            self._compensate_effect(request)
        except LinuxStoreEffectsError:
            raise
        except Exception:
            raise LinuxStoreEffectsError("linux_store_compensation_failed") from None
        final = self.observe(request)
        if final.state != "before":
            raise LinuxStoreEffectsError(
                "linux_store_compensation_not_observed"
            )
        return HostApplyResult(())


class LinuxInstallDependenciesFactory:
    """Create exact Linux operations only after the signed claim is consumed."""

    def __init__(
        self,
        *,
        transport_factory: BoundLinuxStoreTransportFactory,
        secret_source: FreshSecretSource | None = None,
    ) -> None:
        if not callable(transport_factory):
            raise LinuxStoreEffectsError("linux_transport_factory_invalid")
        source = secret_source if secret_source is not None else SystemFreshSecretSource()
        if (
            not callable(getattr(source, "generate_postgres_password", None))
            or not callable(getattr(source, "generate_qdrant_api_key", None))
        ):
            raise LinuxStoreEffectsError("linux_secret_source_invalid")
        self._transport_factory = transport_factory
        self._secret_source = source

    def __call__(
        self,
        *,
        claimed_execution_binding: object,
        resolved_store_spec: Mapping[str, object],
        verified_artifacts: Mapping[str, bytes],
        prerequisites: InstallPrerequisites,
        journal: object,
        resource_identity_ledger: object,
    ) -> ClaimBoundInstallDependencies:
        del journal, resource_identity_ledger
        try:
            from .execution_capability import _claimed_execution_binding_evidence
            from .install_backend import ClaimBoundInstallDependencies

            evidence = _claimed_execution_binding_evidence(
                claimed_execution_binding
            )
            spec = validate_store_spec(
                resolved_store_spec, allow_placeholders=False
            )
            if (
                _sha256(_canonical_bytes(spec))
                != evidence.resolved_store_spec_sha256
                or spec["execution_binding"]
                != {
                    "authorization_id": evidence.authorization_id,
                    "authorization_nonce_sha256": (
                        evidence.authorization_nonce_sha256
                    ),
                    "binding_sha256": evidence.journal_binding_sha256,
                    "execution_id": evidence.execution_id,
                    "package_manifest_sha256": evidence.package_manifest_sha256,
                }
            ):
                raise LinuxStoreEffectsError(
                    "linux_dependency_factory_binding_mismatch"
                )
            artifacts = ExactInstallArtifacts.from_verified_mapping(
                verified_artifacts
            )
            identity = _ExecutionIdentity(
                execution_id=evidence.execution_id,
                attempt_id=evidence.attempt_id,
                execution_binding_sha256=evidence.journal_binding_sha256,
                package_manifest_sha256=evidence.package_manifest_sha256,
                resolved_store_spec_sha256=evidence.resolved_store_spec_sha256,
                controller_runtime_receipt_sha256=(
                    evidence.controller_runtime_receipt_sha256
                ),
                controller_runtime_root=evidence.controller_runtime_root,
                controller_runtime_tree_sha256=(
                    evidence.controller_runtime_tree_sha256
                ),
                controller_release_root=evidence.controller_release_root,
                controller_release_tree_sha256=(
                    evidence.controller_release_tree_sha256
                ),
                controller_release_package_manifest_path=(
                    evidence.controller_release_package_manifest_path
                ),
                controller_runtime_interpreter_path=(
                    evidence.controller_runtime_interpreter_path
                ),
                controller_runtime_interpreter_sha256=(
                    evidence.controller_runtime_interpreter_sha256
                ),
                controller_runtime_inventory_path=(
                    evidence.controller_runtime_inventory_path
                ),
                controller_runtime_inventory_sha256=(
                    evidence.controller_runtime_inventory_sha256
                ),
                controller_requirements_lock_sha256=(
                    evidence.controller_requirements_lock_sha256
                ),
                supervisor_launcher_path=evidence.supervisor_launcher_path,
                supervisor_launcher_sha256=evidence.supervisor_launcher_sha256,
            )
            transports = self._transport_factory(
                execution_id=evidence.execution_id,
                attempt_id=evidence.attempt_id,
                execution_binding_sha256=evidence.journal_binding_sha256,
                resolved_store_spec=spec,
                artifacts=artifacts,
                prerequisites=prerequisites,
            )
            if type(transports) is not BoundLinuxStoreTransports:
                raise LinuxStoreEffectsError("bound_linux_transport_invalid")
            readiness = ClosedStoreReadinessProbe(
                postgres=transports.postgres,
                qdrant=transports.qdrant,
            )
            operations = LinuxStoreHostOperations(
                identity=identity,
                resolved_store_spec=spec,
                artifacts=artifacts,
                prerequisites=prerequisites,
                transports=transports,
                readiness_probe=readiness,
                secret_source=self._secret_source,
            )
            return ClaimBoundInstallDependencies(operations, readiness)
        except LinuxStoreEffectsError:
            raise
        except Exception:
            raise LinuxStoreEffectsError(
                "linux_dependency_factory_refused"
            ) from None


__all__ = [
    "BoundDockerEffects",
    "BoundInvariantEffects",
    "BoundLinuxStoreTransportFactory",
    "BoundLinuxStoreTransports",
    "BoundPostgreSQLEffects",
    "BoundQdrantEffects",
    "BoundRetainedRootDirectorySnapshot",
    "BoundResourceSnapshot",
    "BoundRootFileEffects",
    "BoundSystemdEffects",
    "BoundSystemdSupervisorSnapshot",
    "EffectPresence",
    "ExactInstallArtifacts",
    "ExactSqlArtifact",
    "ExactSystemdSupervisor",
    "FilesystemNodeKind",
    "FreshSecret",
    "FreshSecretSource",
    "LinuxInstallDependenciesFactory",
    "LinuxStoreEffectsError",
    "LinuxStoreHostOperations",
    "LivePreflightSnapshot",
    "SecretEnvironmentDocument",
    "CONTROLLER_CONFIG_DIRECTORY",
    "RETAINED_ROOT_DIRECTORY_PATHS",
    "STORE_SECRET_DIRECTORY",
    "SYSTEMD_ENABLEMENT_PATH",
    "SYSTEMD_ENABLEMENT_TARGET",
    "SYSTEMD_UNIT_PATH",
    "SystemFreshSecretSource",
]

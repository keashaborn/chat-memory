from __future__ import annotations

"""Closed Linux transport for the controller runtime build plan.

Importing this module performs no filesystem or process operation.  Every
effect is made through the injected primitive interface and every path,
command, and environment is derived here from a validated build plan.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Final, Mapping, Protocol

from tools.governed_memory_install.authority import canonical_json_bytes
from tools.governed_memory_install.controller_runtime import (
    EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256,
    INTERPRETER_RELATIVE_PATH,
    INVENTORY_RELATIVE_PATH,
    LAUNCHER_RELATIVE_PATH,
    PACKAGE_MANIFEST_RELATIVE_PATH,
    REQUIREMENTS_LOCK_RELATIVE_PATH,
    RELEASE_ROOT_PREFIX,
    RUNTIME_RECEIPT_RESULT,
    RUNTIME_RECEIPT_SCHEMA,
    RUNTIME_ROOT_PREFIX,
    _INVENTORY_PROBE,
)
from tools.governed_memory_release.controller_runtime_builder import (
    BUILD_RESULT_TYPE,
    OFFLINE_SUBSTRATE_ROOT_PREFIX,
    OFFLINE_WHEELHOUSE_ROOT_PREFIX,
    PUBLICATION_RESULT_TYPE,
    RUNTIME_RECEIPT_ROOT_PREFIX,
    STAGING_ROOT_PREFIX,
    BuildObservation,
    ControllerRuntimeBuildError,
    ControllerRuntimeBuildPlan,
    ControllerRuntimeBuildResult,
    PublicationObservation,
    PublicationTargetObservation,
    StageObservation,
)


_MARKER_NAME: Final = ".governed-memory-build-plan.json"
_INTENT_SUFFIX: Final = ".create-intent.json"
_PUBLICATION_INTENT_SUFFIX: Final = ".publication-intent.json"
_LOCK_NAME: Final = ".controller-requirements.lock"
_PAYLOAD_PROOF_NAME: Final = ".standalone-cpython-payload-proof.json"
_CONFINEMENT_PROOF_NAME: Final = ".standalone-cpython-confinement-proof.json"
_MAX_ARCHIVE_MEMBERS: Final = 100_000
_MAX_ARCHIVE_BYTES: Final = 2 * 1024 * 1024 * 1024
_MAX_PROBE_BYTES: Final = 1024 * 1024
_MAX_RECEIPT_BYTES: Final = 128 * 1024
_MAX_PUBLICATION_INTENT_BYTES: Final = 8 * 1024
_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_PAYLOAD_TREE_SCHEMA: Final = (
    "governed-memory-standalone-cpython-expanded-payload-tree-v1"
)
_RECEIPT_KEYS: Final = frozenset(
    {
        "schema_version",
        "result",
        "package_manifest_sha256",
        "controller_runtime_contract_sha256",
        "controller_requirements_lock_sha256",
        "build_plan_sha256",
        "standalone_cpython_specification_sha256",
        "standalone_cpython_archive_sha256",
        "standalone_cpython_payload_tree_sha256",
        "wheelhouse_tree_sha256",
        "python_implementation",
        "python_version",
        "platform_os",
        "platform_architecture",
        "runtime_tree_sha256",
        "release_tree_sha256",
        "interpreter_sha256",
        "installed_distribution_inventory_sha256",
        "interpreter_path_facts_sha256",
        "postgresql_driver_identity_sha256",
        "supervisor_launcher_sha256",
        "launcher_help_probe_sha256",
        "pip_present",
        "setuptools_present",
        "wheel_present",
        "user_site_enabled",
        "system_site_packages_enabled",
        "network_calls",
        "provider_calls",
        "production_data_read",
        "active_production_state_changed",
        "persistent_controller_substrate_created",
        "persistent_store_resources_created",
    }
)
_PUBLICATION_INTENT_KEYS: Final = frozenset(
    {
        "schema_version",
        "build_plan_sha256",
        "package_manifest_sha256",
        "controller_runtime_contract_sha256",
        "controller_requirements_lock_sha256",
        "publication_intent_path",
        "stage_root",
        "staged_runtime_root",
        "staged_runtime_tree_sha256",
        "staged_release_root",
        "staged_release_tree_sha256",
        "runtime_root",
        "runtime_tree_sha256",
        "release_root",
        "release_tree_sha256",
        "receipt_path",
        "receipt_sha256",
    }
)

_ENVIRONMENT: Final = {
    "HOME": "/nonexistent",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
    "PIP_CONFIG_FILE": "/dev/null",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INDEX": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
}

_SUBSTRATE_CONFINEMENT_PROBE = r"""
import json, platform, site, sys, sysconfig
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
interpreter = Path(sys.executable).resolve(strict=True)

def confined(value, *, allow_missing_stdlib_zip=False):
    candidate = Path(value)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        expected_zip = root / "lib" / (
            f"python{sys.version_info.major}{sys.version_info.minor}.zip"
        )
        if (
            not allow_missing_stdlib_zip
            or not candidate.is_absolute()
            or candidate != expected_zip
            or candidate.is_symlink()
        ):
            raise SystemExit("standalone_cpython_import_path_invalid") from error
        resolved = candidate.parent.resolve(strict=True) / candidate.name
    try:
        relative = resolved.relative_to(root)
    except ValueError as error:
        raise SystemExit("standalone_cpython_path_escape") from error
    rendered = relative.as_posix()
    return rendered if rendered != "." else "."

if interpreter != (root / "bin/python").resolve(strict=True):
    raise SystemExit("standalone_cpython_executable_mismatch")
prefixes = {
    "base_exec_prefix": confined(sys.base_exec_prefix),
    "base_prefix": confined(sys.base_prefix),
    "exec_prefix": confined(sys.exec_prefix),
    "prefix": confined(sys.prefix),
}
if set(prefixes.values()) != {"."}:
    raise SystemExit("standalone_cpython_prefix_mismatch")
if not sys.path or any(not isinstance(value, str) or not value for value in sys.path):
    raise SystemExit("standalone_cpython_import_path_invalid")
import_paths = [
    confined(value, allow_missing_stdlib_zip=True) for value in sys.path
]
if len(import_paths) != len(set(import_paths)):
    raise SystemExit("standalone_cpython_duplicate_import_path")
configured = sysconfig.get_paths()
facts = {
    "base_executable": confined(getattr(sys, "_base_executable", sys.executable)),
    "executable": confined(sys.executable),
    "implementation": platform.python_implementation(),
    "import_paths": import_paths,
    "platform_architecture": platform.machine(),
    "platform_os": sys.platform,
    "prefixes": prefixes,
    "python_version": platform.python_version(),
    "site_package_paths": sorted(
        {confined(value) for value in site.getsitepackages([sys.prefix, sys.exec_prefix])}
    ),
    "stdlib_paths": {
        key: confined(configured[key]) for key in ("stdlib", "platstdlib")
    },
}
print(json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
""".strip()

_REMOVE_BOOTSTRAP = r"""
import importlib.metadata, pathlib, shutil, sys
root = pathlib.Path(sys.argv[1]).resolve(strict=True)
if root != pathlib.Path(sys.prefix).resolve(strict=True):
    raise SystemExit("runtime_prefix_mismatch")
targets = {"pip", "setuptools", "wheel"}
for distribution in tuple(importlib.metadata.distributions()):
    name = distribution.metadata.get("Name", "")
    normalized = __import__("re").sub(r"[-_.]+", "-", name).lower()
    if normalized not in targets:
        continue
    for item in tuple(distribution.files or ()):
        path = pathlib.Path(distribution.locate_file(item)).resolve(strict=False)
        if root not in path.parents:
            raise SystemExit("bootstrap_path_escape")
        if path.is_symlink():
            raise SystemExit("bootstrap_symlink_forbidden")
        if path.is_file():
            path.unlink()
    metadata = pathlib.Path(distribution._path).resolve(strict=False)
    if metadata.exists():
        if root not in metadata.parents or metadata.is_symlink():
            raise SystemExit("bootstrap_metadata_escape")
        shutil.rmtree(metadata)
for name in targets:
    try:
        importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        continue
    raise SystemExit("bootstrap_distribution_present")
""".strip()


@dataclass(frozen=True, slots=True)
class NodeObservation:
    kind: str
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    size: int | None = None
    sha256: str | None = None
    nlink: int | None = None


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    path: str
    kind: str
    mode: int
    size: int
    link_target: str | None = None


@dataclass(frozen=True, slots=True)
class ArchiveExtraction:
    source_member_path: str
    destination_path: str
    mode: int


@dataclass(frozen=True, slots=True)
class TreeMember:
    path: str
    kind: str
    mode: int
    uid: int
    gid: int
    size: int
    sha256: str | None
    nlink: int


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class RuntimePublicationPrimitives(Protocol):
    """Narrow, no-policy Linux operations used by the closed transport."""

    def observe_no_follow(self, path: str) -> NodeObservation: ...
    def read_regular_no_follow(self, path: str, maximum_bytes: int) -> bytes: ...
    def make_directory_no_replace(self, path: str, mode: int) -> None: ...
    def make_parent_directories_no_follow(self, path: str, mode: int) -> None: ...
    def write_regular_no_replace(self, path: str, raw: bytes, mode: int) -> None: ...
    def remove_owned_tree_no_follow(self, path: str) -> None: ...
    def unlink_regular_no_follow(self, path: str, expected_sha256: str) -> None: ...
    def remove_empty_directory_no_follow(self, path: str) -> None: ...
    def directory_device_no_follow(self, path: str) -> int: ...
    def list_archive_members(self, archive_path: str) -> tuple[ArchiveMember, ...]: ...
    def extract_regular_members_no_follow(
        self,
        archive_path: str,
        extractions: tuple[ArchiveExtraction, ...],
    ) -> None: ...
    def observe_tree_no_follow(self, root: str) -> tuple[TreeMember, ...]: ...
    def seal_tree_root_owned(
        self,
        root: str,
        directory_mode: int,
        regular_mode: int,
        executable_mode: int,
    ) -> None: ...
    def fsync_tree(self, root: str) -> None: ...
    def run_exact(
        self, argv: tuple[str, ...], environment: Mapping[str, str], cwd: str
    ) -> ProcessResult: ...
    def rename_no_replace(self, source: str, destination: str) -> None: ...
    def fsync_file(self, path: str) -> None: ...
    def fsync_parent(self, path: str) -> None: ...


def _fail() -> None:
    raise ControllerRuntimeBuildError("controller_runtime_publication_transport_refused")


def _require_plan(plan: ControllerRuntimeBuildPlan) -> None:
    if type(plan) is not ControllerRuntimeBuildPlan:
        _fail()
    plan_hashes = (
        plan.build_plan_sha256,
        plan.package_manifest_sha256,
        plan.controller_runtime_contract_sha256,
        plan.controller_requirements_lock_sha256,
        plan.release_closure_sha256,
        plan.supervisor_launcher_sha256,
        plan.substrate.specification_sha256,
        plan.substrate.archive_sha256,
        plan.substrate.payload_tree_sha256,
        plan.wheelhouse.tree_sha256,
    )
    if any(
        type(value) is not str or _HASH_RE.fullmatch(value) is None
        for value in plan_hashes
    ):
        _fail()
    stage = STAGING_ROOT_PREFIX / plan.build_plan_sha256
    if (
        plan.stage_root != str(stage)
        or plan.staged_runtime_root != str(stage / "runtime")
        or plan.staged_release_root != str(stage / "release")
        or plan.substrate.archive_path
        != str(
            OFFLINE_SUBSTRATE_ROOT_PREFIX
            / plan.substrate.archive_sha256
            / plan.substrate.archive_name
        )
        or plan.wheelhouse_root
        != str(OFFLINE_WHEELHOUSE_ROOT_PREFIX / plan.wheelhouse.tree_sha256)
        or plan.final_release_root
        != str(RELEASE_ROOT_PREFIX / plan.package_manifest_sha256)
    ):
        _fail()


def _marker(plan: ControllerRuntimeBuildPlan) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": "governed-memory-controller-build-stage-marker-v1",
            "build_plan_sha256": plan.build_plan_sha256,
        }
    )


def _intent_path(plan: ControllerRuntimeBuildPlan) -> str:
    return plan.stage_root + _INTENT_SUFFIX


def _publication_intent_path(plan: ControllerRuntimeBuildPlan) -> str:
    return plan.stage_root + _PUBLICATION_INTENT_SUFFIX


def _unique_receipt_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _receipt_document(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > _MAX_RECEIPT_BYTES:
        _fail()
    try:
        document = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_unique_receipt_object,
            parse_constant=lambda unused: _fail(),
        )
    except ControllerRuntimeBuildError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _fail()
    try:
        canonical = canonical_json_bytes(document)
    except Exception:
        _fail()
    if type(document) is not dict or set(document) != _RECEIPT_KEYS or canonical != raw:
        _fail()
    return document


def _publication_intent(
    plan: ControllerRuntimeBuildPlan,
    *,
    runtime_root: str,
    receipt_path: str,
    receipt_raw: bytes,
) -> bytes:
    document = _receipt_document(receipt_raw)
    receipt_sha256 = hashlib.sha256(receipt_raw).hexdigest()
    return canonical_json_bytes(
        {
            "schema_version": "governed-memory-controller-publication-intent-v1",
            "build_plan_sha256": plan.build_plan_sha256,
            "package_manifest_sha256": plan.package_manifest_sha256,
            "controller_runtime_contract_sha256": (
                plan.controller_runtime_contract_sha256
            ),
            "controller_requirements_lock_sha256": (
                plan.controller_requirements_lock_sha256
            ),
            "publication_intent_path": _publication_intent_path(plan),
            "stage_root": plan.stage_root,
            "staged_runtime_root": plan.staged_runtime_root,
            "staged_runtime_tree_sha256": document["runtime_tree_sha256"],
            "staged_release_root": plan.staged_release_root,
            "staged_release_tree_sha256": document["release_tree_sha256"],
            "runtime_root": runtime_root,
            "runtime_tree_sha256": document["runtime_tree_sha256"],
            "release_root": plan.final_release_root,
            "release_tree_sha256": document["release_tree_sha256"],
            "receipt_path": receipt_path,
            "receipt_sha256": receipt_sha256,
        }
    )


def _publication_intent_document(
    plan: ControllerRuntimeBuildPlan,
    raw: bytes,
) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > _MAX_PUBLICATION_INTENT_BYTES:
        _fail()
    try:
        document = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_unique_receipt_object,
            parse_constant=lambda unused: _fail(),
        )
    except ControllerRuntimeBuildError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _fail()
    if (
        type(document) is not dict
        or set(document) != _PUBLICATION_INTENT_KEYS
        or canonical_json_bytes(document) != raw
    ):
        _fail()
    exact = {
        "schema_version": "governed-memory-controller-publication-intent-v1",
        "build_plan_sha256": plan.build_plan_sha256,
        "package_manifest_sha256": plan.package_manifest_sha256,
        "controller_runtime_contract_sha256": (
            plan.controller_runtime_contract_sha256
        ),
        "controller_requirements_lock_sha256": (
            plan.controller_requirements_lock_sha256
        ),
        "publication_intent_path": _publication_intent_path(plan),
        "stage_root": plan.stage_root,
        "staged_runtime_root": plan.staged_runtime_root,
        "staged_release_root": plan.staged_release_root,
        "release_root": plan.final_release_root,
    }
    if any(
        type(document.get(key)) is not str or document[key] != value
        for key, value in exact.items()
    ):
        _fail()
    hash_keys = (
        "staged_runtime_tree_sha256",
        "staged_release_tree_sha256",
        "runtime_tree_sha256",
        "release_tree_sha256",
        "receipt_sha256",
    )
    if any(
        type(document.get(key)) is not str
        or _HASH_RE.fullmatch(str(document[key])) is None
        for key in hash_keys
    ):
        _fail()
    runtime_root = document.get("runtime_root")
    receipt_path = document.get("receipt_path")
    receipt_sha256 = document["receipt_sha256"]
    if (
        type(runtime_root) is not str
        or type(receipt_path) is not str
        or PurePosixPath(runtime_root).parent != RUNTIME_ROOT_PREFIX
        or PurePosixPath(runtime_root).name != receipt_sha256
        or str(PurePosixPath(runtime_root)) != runtime_root
        or PurePosixPath(receipt_path).parent != RUNTIME_RECEIPT_ROOT_PREFIX
        or PurePosixPath(receipt_path).name != receipt_sha256 + ".json"
        or str(PurePosixPath(receipt_path)) != receipt_path
        or document["staged_runtime_tree_sha256"]
        != document["runtime_tree_sha256"]
        or document["staged_release_tree_sha256"]
        != document["release_tree_sha256"]
    ):
        _fail()
    return document


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        _fail()
    return path


@dataclass(frozen=True, slots=True)
class _ExpandedArchiveMember:
    path: str
    kind: str
    mode: int
    size: int
    source_member_path: str | None


def _resolved_relative_link(member_path: str, target: str) -> str:
    if type(target) is not str or not target or "\x00" in target:
        _fail()
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        _fail()
    parts = list(PurePosixPath(member_path).parent.parts)
    for part in target_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if len(parts) <= 1:
                _fail()
            parts.pop()
            continue
        parts.append(part)
    resolved = PurePosixPath(*parts)
    if not parts or parts[0] != "python" or str(resolved) == "python":
        _fail()
    return str(resolved)


def _expanded_archive_members(
    members: tuple[ArchiveMember, ...],
) -> tuple[tuple[_ExpandedArchiveMember, ...], str]:
    """Expand relative in-payload links into a regular-file/directory tree.

    The selected python-build-standalone archive intentionally contains many
    relative links.  No link is ever emitted.  Each link is resolved only
    within the ``python`` payload, cycles and dangling targets are rejected,
    and file aliases are extracted as independent regular-file copies.
    """

    if type(members) is not tuple or not members or len(members) > _MAX_ARCHIVE_MEMBERS:
        _fail()
    records: dict[str, ArchiveMember] = {}
    for member in members:
        if (
            type(member) is not ArchiveMember
            or type(member.path) is not str
            or type(member.kind) is not str
            or type(member.mode) is not int
            or member.mode & ~0o777
            or type(member.size) is not int
            or member.size < 0
        ):
            _fail()
        path = _safe_relative(member.path)
        if (
            path.parts[0] != "python"
            or member.path in records
            or member.kind not in {"directory", "file", "symlink"}
            or (
                member.kind == "symlink"
                and (type(member.link_target) is not str or member.size != 0)
            )
            or (member.kind != "symlink" and member.link_target is not None)
            or (member.kind == "directory" and member.size != 0)
        ):
            _fail()
        records[member.path] = member
    root = records.get("python")
    if root is not None and root.kind != "directory":
        _fail()

    child_index: dict[str, set[str]] = {}
    for member_path in records:
        parts = PurePosixPath(member_path).parts
        for index in range(1, len(parts)):
            parent = str(PurePosixPath(*parts[:index]))
            child = str(PurePosixPath(*parts[: index + 1]))
            child_index.setdefault(parent, set()).add(child)
    for member_path, member in records.items():
        if member.kind != "directory" and child_index.get(member_path):
            _fail()

    def kind_at(path: str) -> str:
        record = records.get(path)
        if record is not None:
            return record.kind
        if child_index.get(path):
            return "directory"
        _fail()
        raise AssertionError

    expanded: dict[str, _ExpandedArchiveMember] = {}
    mappings: list[dict[str, str]] = []

    def add(item: _ExpandedArchiveMember) -> None:
        if item.path in expanded or len(expanded) >= _MAX_ARCHIVE_MEMBERS:
            _fail()
        expanded[item.path] = item

    def expand(source: str, destination: str, stack: tuple[str, ...]) -> None:
        if source in stack:
            _fail()
        kind = kind_at(source)
        record = records.get(source)
        if kind == "symlink":
            if record is None or record.link_target is None:
                _fail()
            target = _resolved_relative_link(source, record.link_target)
            mappings.append(
                {
                    "link_target": record.link_target,
                    "output_path": destination,
                    "symlink_member_path": source,
                    "resolved_target_path": target,
                }
            )
            expand(target, destination, stack + (source,))
            return
        if kind == "file":
            if record is None:
                _fail()
            add(
                _ExpandedArchiveMember(
                    destination,
                    "file",
                    0o555 if record.mode & 0o111 else 0o444,
                    record.size,
                    source,
                )
            )
            return
        add(_ExpandedArchiveMember(destination, "directory", 0o555, 0, None))
        for child in sorted(child_index.get(source, ())):
            child_name = PurePosixPath(child).name
            expand(
                child,
                str(PurePosixPath(destination) / child_name),
                stack + (source,),
            )

    for child in sorted(child_index.get("python", ())):
        expand(child, PurePosixPath(child).name, ("python",))
    if not expanded:
        _fail()
    total = sum(item.size for item in expanded.values() if item.kind == "file")
    if total > _MAX_ARCHIVE_BYTES:
        _fail()
    mapping_sha256 = hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_version": (
                    "governed-memory-standalone-cpython-symlink-expansion-v1"
                ),
                "mappings": sorted(
                    mappings,
                    key=lambda item: (
                        item["output_path"],
                        item["symlink_member_path"],
                        item["link_target"],
                        item["resolved_target_path"],
                    ),
                ),
            }
        )
    ).hexdigest()
    return tuple(sorted(expanded.values(), key=lambda item: item.path)), mapping_sha256


def _substrate_payload_tree_hash(members: tuple[TreeMember, ...]) -> str:
    if type(members) is not tuple or not members or len(members) > _MAX_ARCHIVE_MEMBERS:
        _fail()
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    total = 0
    for item in sorted(members, key=lambda candidate: candidate.path):
        if type(item) is not TreeMember or item.path in seen:
            _fail()
        path = _safe_relative(item.path)
        if (
            item.uid != 0
            or item.gid != 0
            or item.kind not in {"directory", "file"}
            or item.nlink < 1
            or item.mode not in ({0o700} if item.kind == "directory" else {0o600, 0o700})
            or (item.kind == "directory" and (item.sha256 is not None or item.size != 0))
            or (
                item.kind == "file"
                and (
                    item.nlink != 1
                    or type(item.sha256) is not str
                    or _HASH_RE.fullmatch(item.sha256) is None
                    or item.size < 0
                )
            )
        ):
            _fail()
        seen.add(item.path)
        if item.kind == "file":
            total += item.size
            if total > _MAX_ARCHIVE_BYTES:
                _fail()
        entries.append(
            {
                "mode": (
                    0o555
                    if item.kind == "directory" or item.mode == 0o700
                    else 0o444
                ),
                "path": str(path) + ("/" if item.kind == "directory" else ""),
                "sha256": item.sha256 if item.kind == "file" else "directory",
            }
        )
    return hashlib.sha256(
        canonical_json_bytes(
            {"schema_version": _PAYLOAD_TREE_SCHEMA, "entries": entries}
        )
    ).hexdigest()


def _payload_proof(
    plan: ControllerRuntimeBuildPlan, *, link_mapping_sha256: str
) -> bytes:
    if _HASH_RE.fullmatch(link_mapping_sha256) is None:
        _fail()
    return canonical_json_bytes(
        {
            "schema_version": "governed-memory-standalone-cpython-payload-proof-v1",
            "archive_sha256": plan.substrate.archive_sha256,
            "build_plan_sha256": plan.build_plan_sha256,
            "expanded_payload_tree_schema": _PAYLOAD_TREE_SCHEMA,
            "expanded_payload_tree_sha256": plan.substrate.payload_tree_sha256,
            "symlink_expansion_mapping_sha256": link_mapping_sha256,
        }
    )


def _confinement_proof(
    plan: ControllerRuntimeBuildPlan, *, facts: dict[str, object]
) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": "governed-memory-standalone-cpython-confinement-proof-v1",
            "build_plan_sha256": plan.build_plan_sha256,
            "facts_sha256": hashlib.sha256(canonical_json_bytes(facts)).hexdigest(),
            "payload_tree_sha256": plan.substrate.payload_tree_sha256,
            "python_version": plan.substrate.python_version,
        }
    )


def _confinement_proof_document(
    plan: ControllerRuntimeBuildPlan, raw: bytes
) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > 4096:
        _fail()
    try:
        document = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_unique_receipt_object,
            parse_constant=lambda unused: _fail(),
        )
    except ControllerRuntimeBuildError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _fail()
    if (
        type(document) is not dict
        or set(document)
        != {
            "schema_version",
            "build_plan_sha256",
            "facts_sha256",
            "payload_tree_sha256",
            "python_version",
        }
        or canonical_json_bytes(document) != raw
        or document.get("schema_version")
        != "governed-memory-standalone-cpython-confinement-proof-v1"
        or document.get("build_plan_sha256") != plan.build_plan_sha256
        or document.get("payload_tree_sha256")
        != plan.substrate.payload_tree_sha256
        or document.get("python_version") != plan.substrate.python_version
        or type(document.get("facts_sha256")) is not str
        or _HASH_RE.fullmatch(str(document["facts_sha256"])) is None
    ):
        _fail()
    return document


def _tree_hash(members: tuple[TreeMember, ...], schema: str) -> str:
    if type(members) is not tuple or type(schema) is not str or not schema:
        _fail()
    for item in members:
        if (
            type(item) is not TreeMember
            or type(item.path) is not str
            or type(item.kind) is not str
            or type(item.mode) is not int
            or not 0 <= item.mode <= 0o7777
            or type(item.uid) is not int
            or type(item.gid) is not int
            or type(item.size) is not int
            or item.size < 0
            or type(item.nlink) is not int
            or item.nlink < 1
        ):
            _fail()
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in sorted(members, key=lambda candidate: candidate.path):
        path = _safe_relative(item.path)
        if (
            item.path in seen
            or item.uid != 0
            or item.gid != 0
            or item.mode & 0o222
            or item.kind not in {"directory", "file"}
            or (
                item.kind == "file"
                and (
                    item.nlink != 1
                    or type(item.sha256) is not str
                    or _HASH_RE.fullmatch(item.sha256) is None
                )
            )
            or (item.kind == "directory" and item.sha256 is not None)
        ):
            _fail()
        seen.add(item.path)
        rendered = str(path) + ("/" if item.kind == "directory" else "")
        entries.append(
            {
                "path": rendered,
                "mode": item.mode,
                "sha256": item.sha256 if item.kind == "file" else "directory",
            }
        )
    return hashlib.sha256(
        canonical_json_bytes({"schema_version": schema, "entries": entries})
    ).hexdigest()


def _release_tree_hash(
    members: tuple[TreeMember, ...], package_manifest_sha256: str
) -> str:
    # Reuse the common validator even though the release schema binds extra
    # root and manifest facts and therefore has a different final digest.
    _tree_hash(members, "unused-validation-only")
    entries = [
        {
            "path": item.path + ("/" if item.kind == "directory" else ""),
            "mode": item.mode,
            "sha256": item.sha256 if item.kind == "file" else "directory",
        }
        for item in sorted(members, key=lambda candidate: candidate.path)
    ]
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_version": "governed-memory-controller-release-tree-v1",
                "package_manifest_path": str(PACKAGE_MANIFEST_RELATIVE_PATH),
                "package_manifest_sha256": package_manifest_sha256,
                "root_mode": 0o555,
                "entries": entries,
            }
        )
    ).hexdigest()


class ClosedRuntimePublicationTransport:
    """Executable policy transport; effects occur only when a method is called."""

    def __init__(self, primitives: RuntimePublicationPrimitives) -> None:
        self._os = primitives

    def _marker_state(self, path: str, expected: bytes) -> str:
        node = self._os.observe_no_follow(path)
        if node.kind == "absent":
            return "absent"
        if (
            node.kind != "file"
            or node.uid != 0
            or node.gid != 0
            or node.mode != 0o400
            or node.nlink != 1
            or node.size != len(expected)
            or node.sha256 != hashlib.sha256(expected).hexdigest()
        ):
            return "foreign"
        try:
            raw = self._os.read_regular_no_follow(path, 1024)
        except Exception:
            return "foreign"
        return "valid" if raw == expected else "foreign"

    def _publication_intent_state(self, plan: ControllerRuntimeBuildPlan) -> str:
        path = _publication_intent_path(plan)
        node = self._os.observe_no_follow(path)
        if node.kind == "absent":
            return "absent"
        if (
            node.kind != "file"
            or node.uid != 0
            or node.gid != 0
            or node.mode != 0o400
            or node.nlink != 1
            or type(node.size) is not int
            or node.size < 1
            or node.size > _MAX_PUBLICATION_INTENT_BYTES
            or type(node.sha256) is not str
            or _HASH_RE.fullmatch(node.sha256) is None
        ):
            return "foreign"
        try:
            raw = self._os.read_regular_no_follow(
                path, _MAX_PUBLICATION_INTENT_BYTES
            )
            if (
                len(raw) != node.size
                or hashlib.sha256(raw).hexdigest() != node.sha256
            ):
                return "foreign"
            _publication_intent_document(plan, raw)
        except Exception:
            return "foreign"
        return "valid"

    def _publication_document(
        self, plan: ControllerRuntimeBuildPlan
    ) -> tuple[dict[str, object], bytes]:
        path = _publication_intent_path(plan)
        if self._publication_intent_state(plan) != "valid":
            _fail()
        raw = self._os.read_regular_no_follow(
            path, _MAX_PUBLICATION_INTENT_BYTES
        )
        return _publication_intent_document(plan, raw), raw

    def _terminal_publication_result(
        self, plan: ControllerRuntimeBuildPlan
    ) -> ControllerRuntimeBuildResult:
        document, _ = self._publication_document(plan)
        runtime_root = str(document["runtime_root"])
        release_root = str(document["release_root"])
        receipt_path = str(document["receipt_path"])
        receipt_node = self._os.observe_no_follow(receipt_path)
        if (
            self._os.observe_no_follow(plan.stage_root).kind != "absent"
            or self._os.observe_no_follow(_intent_path(plan)).kind != "absent"
            or receipt_node.kind != "file"
            or receipt_node.mode != 0o400
            or receipt_node.uid != 0
            or receipt_node.gid != 0
            or receipt_node.nlink != 1
            or receipt_node.size is None
            or receipt_node.size < 1
            or receipt_node.size > _MAX_RECEIPT_BYTES
            or receipt_node.sha256 != document["receipt_sha256"]
        ):
            _fail()
        receipt_raw = self._os.read_regular_no_follow(
            receipt_path, _MAX_RECEIPT_BYTES
        )
        receipt = _receipt_document(receipt_raw)
        runtime_node = self._os.observe_no_follow(runtime_root)
        release_node = self._os.observe_no_follow(release_root)
        runtime = self._os.observe_tree_no_follow(runtime_root)
        release = self._os.observe_tree_no_follow(release_root)
        runtime_hash = _tree_hash(
            runtime, "governed-memory-controller-runtime-tree-v1"
        )
        release_hash = _release_tree_hash(
            release, plan.package_manifest_sha256
        )
        exact_receipt = {
            "schema_version": RUNTIME_RECEIPT_SCHEMA,
            "result": RUNTIME_RECEIPT_RESULT,
            "package_manifest_sha256": plan.package_manifest_sha256,
            "controller_runtime_contract_sha256": (
                plan.controller_runtime_contract_sha256
            ),
            "controller_requirements_lock_sha256": (
                plan.controller_requirements_lock_sha256
            ),
            "build_plan_sha256": plan.build_plan_sha256,
            "standalone_cpython_specification_sha256": (
                plan.substrate.specification_sha256
            ),
            "standalone_cpython_archive_sha256": plan.substrate.archive_sha256,
            "standalone_cpython_payload_tree_sha256": (
                plan.substrate.payload_tree_sha256
            ),
            "wheelhouse_tree_sha256": plan.wheelhouse.tree_sha256,
            "python_implementation": "CPython",
            "python_version": plan.substrate.python_version,
            "platform_os": "linux",
            "platform_architecture": "x86_64",
            "supervisor_launcher_sha256": plan.supervisor_launcher_sha256,
            "postgresql_driver_identity_sha256": (
                EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256
            ),
        }
        if (
            len(receipt_raw) != receipt_node.size
            or hashlib.sha256(receipt_raw).hexdigest()
            != document["receipt_sha256"]
            or runtime_node.kind != "directory"
            or runtime_node.mode != 0o555
            or runtime_node.uid != 0
            or runtime_node.gid != 0
            or release_node.kind != "directory"
            or release_node.mode != 0o555
            or release_node.uid != 0
            or release_node.gid != 0
            or runtime_hash != document["runtime_tree_sha256"]
            or release_hash != document["release_tree_sha256"]
            or receipt.get("runtime_tree_sha256") != runtime_hash
            or receipt.get("release_tree_sha256") != release_hash
            or any(
                receipt.get(key) != value
                for key, value in exact_receipt.items()
            )
        ):
            _fail()
        return ControllerRuntimeBuildResult(
            build_plan_sha256=plan.build_plan_sha256,
            runtime_build_receipt_json=receipt_raw,
            controller_runtime_receipt_sha256=str(document["receipt_sha256"]),
            runtime_root=runtime_root,
            release_root=release_root,
            receipt_path=receipt_path,
            runtime_tree_sha256=runtime_hash,
            release_tree_sha256=release_hash,
        )

    def recover_completed_publication(
        self, plan: ControllerRuntimeBuildPlan
    ) -> ControllerRuntimeBuildResult:
        _require_plan(plan)
        result = self._terminal_publication_result(plan)
        self._os.fsync_parent(result.runtime_root)
        self._os.fsync_parent(result.release_root)
        self._os.fsync_file(result.receipt_path)
        self._os.fsync_parent(result.receipt_path)
        return self._terminal_publication_result(plan)

    def observe_stage(self, plan: ControllerRuntimeBuildPlan) -> StageObservation:
        _require_plan(plan)
        publication_intent_state = self._publication_intent_state(plan)
        if publication_intent_state == "valid":
            try:
                self._terminal_publication_result(plan)
            except Exception:
                pass
            else:
                return StageObservation(
                    "publication_completed", plan.build_plan_sha256
                )
            return StageObservation("publication_started", plan.build_plan_sha256)
        if publication_intent_state == "foreign":
            return StageObservation("publication_started_foreign", None)
        expected = _marker(plan)
        intent_state = self._marker_state(_intent_path(plan), expected)
        node = self._os.observe_no_follow(plan.stage_root)
        if node.kind == "absent":
            if intent_state == "foreign":
                return StageObservation("foreign", None)
            return StageObservation("absent", None)
        if node.kind != "directory" or node.uid != 0 or node.gid != 0 or node.mode != 0o700:
            return StageObservation("foreign", None)
        marker_path = str(PurePosixPath(plan.stage_root) / _MARKER_NAME)
        marker_state = self._marker_state(marker_path, expected)
        if intent_state == "foreign" or not (
            marker_state == "valid" or intent_state == "valid"
        ):
            return StageObservation("foreign", None)
        return StageObservation(
            "owned_partial",
            plan.build_plan_sha256,
            root_mode=0o700,
            root_uid=0,
            root_gid=0,
            marker_regular_no_follow=True,
            created_no_replace=True,
            parent_fsynced=True,
        )

    def recover_owned_partial_stage(self, plan: ControllerRuntimeBuildPlan) -> StageObservation:
        current = self.observe_stage(plan)
        if current.state != "owned_partial":
            _fail()
        self._os.remove_owned_tree_no_follow(plan.stage_root)
        self._os.fsync_parent(plan.stage_root)
        intent_path = _intent_path(plan)
        intent_state = self._marker_state(intent_path, _marker(plan))
        if intent_state == "foreign":
            _fail()
        if intent_state == "valid":
            self._os.remove_owned_tree_no_follow(intent_path)
            self._os.fsync_parent(intent_path)
        if self._os.observe_no_follow(plan.stage_root).kind != "absent":
            _fail()
        return StageObservation("absent", None)

    def create_stage(self, plan: ControllerRuntimeBuildPlan) -> StageObservation:
        _require_plan(plan)
        current = self.observe_stage(plan)
        if current.state != "absent":
            _fail()
        intent_path = _intent_path(plan)
        intent_state = self._marker_state(intent_path, _marker(plan))
        if intent_state == "foreign":
            _fail()
        if intent_state == "valid":
            # A crash before mkdir leaves only the durable exact-plan intent.
            # Reclaim only that exact create-only record before retrying.
            self._os.remove_owned_tree_no_follow(intent_path)
            self._os.fsync_parent(intent_path)
        self._os.write_regular_no_replace(intent_path, _marker(plan), 0o400)
        self._os.fsync_file(intent_path)
        self._os.fsync_parent(intent_path)
        self._os.make_directory_no_replace(plan.stage_root, 0o700)
        marker_path = str(PurePosixPath(plan.stage_root) / _MARKER_NAME)
        self._os.write_regular_no_replace(marker_path, _marker(plan), 0o400)
        self._os.fsync_file(marker_path)
        self._os.fsync_parent(plan.stage_root)
        self._os.remove_owned_tree_no_follow(intent_path)
        self._os.fsync_parent(intent_path)
        result = self.observe_stage(plan)
        if result.state != "owned_partial":
            _fail()
        return StageObservation(
            "owned_ready",
            plan.build_plan_sha256,
            root_mode=0o700,
            root_uid=0,
            root_gid=0,
            marker_regular_no_follow=True,
            created_no_replace=True,
            parent_fsynced=True,
        )

    def materialize_standalone_substrate(self, plan: ControllerRuntimeBuildPlan) -> None:
        _require_plan(plan)
        if self.observe_stage(plan).state != "owned_partial":
            _fail()
        archive_root = str(PurePosixPath(plan.substrate.archive_path).parent)
        archive_root_node = self._os.observe_no_follow(archive_root)
        archive = self._os.observe_no_follow(plan.substrate.archive_path)
        if (
            archive_root_node.kind != "directory"
            or archive_root_node.uid != 0
            or archive_root_node.gid != 0
            or archive_root_node.mode != 0o555
            or archive.kind != "file"
            or archive.uid != 0
            or archive.gid != 0
            or archive.nlink != 1
            or archive.mode != 0o444
            or archive.sha256 != plan.substrate.archive_sha256
        ):
            _fail()
        members = self._os.list_archive_members(plan.substrate.archive_path)
        expanded, link_mapping_sha256 = _expanded_archive_members(members)
        self._os.make_directory_no_replace(plan.staged_runtime_root, 0o700)
        extractions: list[ArchiveExtraction] = []
        for member in expanded:
            destination = str(PurePosixPath(plan.staged_runtime_root) / member.path)
            if member.kind == "directory":
                self._os.make_parent_directories_no_follow(destination, 0o700)
            else:
                if member.source_member_path is None:
                    _fail()
                self._os.make_parent_directories_no_follow(
                    str(PurePosixPath(destination).parent), 0o700
                )
                mode = 0o700 if member.mode == 0o555 else 0o600
                extractions.append(
                    ArchiveExtraction(
                        member.source_member_path,
                        destination,
                        mode,
                    )
                )
        self._os.extract_regular_members_no_follow(
            plan.substrate.archive_path, tuple(extractions)
        )
        actual_payload_sha256 = _substrate_payload_tree_hash(
            self._os.observe_tree_no_follow(plan.staged_runtime_root)
        )
        if actual_payload_sha256 != plan.substrate.payload_tree_sha256:
            _fail()
        proof_path = str(PurePosixPath(plan.stage_root) / _PAYLOAD_PROOF_NAME)
        proof = _payload_proof(
            plan, link_mapping_sha256=link_mapping_sha256
        )
        self._os.write_regular_no_replace(proof_path, proof, 0o400)
        self._os.fsync_file(proof_path)
        self._os.fsync_parent(proof_path)

    def install_locked_offline_distributions(
        self,
        plan: ControllerRuntimeBuildPlan,
        *,
        controller_requirements_lock: bytes,
    ) -> None:
        _require_plan(plan)
        if (
            type(controller_requirements_lock) is not bytes
            or not controller_requirements_lock
            or hashlib.sha256(controller_requirements_lock).hexdigest()
            != plan.controller_requirements_lock_sha256
            or b"--hash=sha256:" not in controller_requirements_lock
            or b"--index" in controller_requirements_lock
            or b"://" in controller_requirements_lock
        ):
            _fail()
        archive_members = self._os.list_archive_members(plan.substrate.archive_path)
        unused_expanded, link_mapping_sha256 = _expanded_archive_members(
            archive_members
        )
        payload_proof = _payload_proof(
            plan, link_mapping_sha256=link_mapping_sha256
        )
        payload_proof_path = str(
            PurePosixPath(plan.stage_root) / _PAYLOAD_PROOF_NAME
        )
        if self._marker_state(payload_proof_path, payload_proof) != "valid":
            _fail()
        if (
            _substrate_payload_tree_hash(
                self._os.observe_tree_no_follow(plan.staged_runtime_root)
            )
            != plan.substrate.payload_tree_sha256
        ):
            _fail()
        interpreter = str(
            PurePosixPath(plan.staged_runtime_root) / INTERPRETER_RELATIVE_PATH
        )
        confinement = self._os.run_exact(
            (
                interpreter,
                "-I",
                "-B",
                "-c",
                _SUBSTRATE_CONFINEMENT_PROBE,
                plan.staged_runtime_root,
            ),
            _ENVIRONMENT,
            "/",
        )
        if (
            confinement.returncode != 0
            or confinement.stderr
            or not confinement.stdout
            or len(confinement.stdout) > _MAX_PROBE_BYTES
        ):
            _fail()
        try:
            facts = json.loads(
                confinement.stdout.decode("ascii"),
                object_pairs_hook=_unique_receipt_object,
                parse_constant=lambda unused: _fail(),
            )
        except ControllerRuntimeBuildError:
            raise
        except (UnicodeError, ValueError, RecursionError):
            _fail()
        expected_fact_keys = {
            "base_executable",
            "executable",
            "implementation",
            "import_paths",
            "platform_architecture",
            "platform_os",
            "prefixes",
            "python_version",
            "site_package_paths",
            "stdlib_paths",
        }
        if (
            type(facts) is not dict
            or set(facts) != expected_fact_keys
            or canonical_json_bytes(facts) + b"\n" != confinement.stdout
            or facts.get("implementation") != "CPython"
            or facts.get("python_version") != plan.substrate.python_version
            or facts.get("platform_os") != "linux"
            or facts.get("platform_architecture") != "x86_64"
            or facts.get("executable") != str(INTERPRETER_RELATIVE_PATH)
            or facts.get("base_executable") != str(INTERPRETER_RELATIVE_PATH)
            or facts.get("prefixes")
            != {
                "base_exec_prefix": ".",
                "base_prefix": ".",
                "exec_prefix": ".",
                "prefix": ".",
            }
            or type(facts.get("import_paths")) is not list
            or not facts["import_paths"]
            or len(facts["import_paths"]) != len(set(facts["import_paths"]))
        ):
            _fail()
        confinement_proof = _confinement_proof(plan, facts=facts)
        confinement_proof_path = str(
            PurePosixPath(plan.stage_root) / _CONFINEMENT_PROOF_NAME
        )
        self._os.write_regular_no_replace(
            confinement_proof_path, confinement_proof, 0o400
        )
        self._os.fsync_file(confinement_proof_path)
        self._os.fsync_parent(confinement_proof_path)
        wheel_nodes = self._os.observe_tree_no_follow(plan.wheelhouse_root)
        wheelhouse_root = self._os.observe_no_follow(plan.wheelhouse_root)
        expected = {member.filename: member for member in plan.wheelhouse.members}
        files = {node.path: node for node in wheel_nodes if node.kind == "file"}
        if (
            wheelhouse_root.kind != "directory"
            or wheelhouse_root.uid != 0
            or wheelhouse_root.gid != 0
            or wheelhouse_root.mode != 0o555
            or len(wheel_nodes) != len(expected)
            or set(files) != set(expected)
        ):
            _fail()
        for name, selected in expected.items():
            node = files[name]
            if (
                node.uid != 0
                or node.gid != 0
                or node.mode & 0o222
                or node.nlink != 1
                or node.size != selected.size
                or node.sha256 != selected.sha256
            ):
                _fail()
        lock_path = str(PurePosixPath(plan.stage_root) / _LOCK_NAME)
        self._os.write_regular_no_replace(lock_path, controller_requirements_lock, 0o400)
        self._os.fsync_file(lock_path)
        result = self._os.run_exact(
            (
                interpreter,
                "-I",
                "-B",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--disable-pip-version-check",
                "--no-compile",
                "--no-deps",
                "--require-hashes",
                "--only-binary=:all:",
                "--find-links",
                plan.wheelhouse_root,
                "--requirement",
                lock_path,
            ),
            _ENVIRONMENT,
            "/",
        )
        if (
            result.returncode != 0
            or len(result.stdout) > _MAX_PROBE_BYTES
            or len(result.stderr) > _MAX_PROBE_BYTES
        ):
            _fail()

    def remove_bootstrap_packaging_tools(self, plan: ControllerRuntimeBuildPlan) -> None:
        _require_plan(plan)
        interpreter = str(PurePosixPath(plan.staged_runtime_root) / INTERPRETER_RELATIVE_PATH)
        result = self._os.run_exact(
            (interpreter, "-I", "-B", "-c", _REMOVE_BOOTSTRAP, plan.staged_runtime_root),
            _ENVIRONMENT,
            "/",
        )
        if result.returncode != 0 or result.stdout or result.stderr:
            _fail()

    def stage_exact_release(
        self,
        plan: ControllerRuntimeBuildPlan,
        *,
        package_manifest_json: bytes,
        package_artifacts: Mapping[str, bytes],
    ) -> None:
        _require_plan(plan)
        if (
            type(package_manifest_json) is not bytes
            or hashlib.sha256(package_manifest_json).hexdigest() != plan.package_manifest_sha256
            or type(package_artifacts) is not dict
            or len(package_artifacts) != plan.release_artifact_count
        ):
            _fail()
        self._os.make_directory_no_replace(plan.staged_release_root, 0o700)
        material = dict(package_artifacts)
        material[str(PACKAGE_MANIFEST_RELATIVE_PATH)] = package_manifest_json
        for relative, raw in sorted(material.items()):
            path = _safe_relative(relative)
            if type(raw) is not bytes:
                _fail()
            destination = str(PurePosixPath(plan.staged_release_root) / path)
            self._os.make_parent_directories_no_follow(
                str(PurePosixPath(destination).parent), 0o700
            )
            mode = 0o500 if relative == str(LAUNCHER_RELATIVE_PATH) else 0o400
            self._os.write_regular_no_replace(destination, raw, mode)

    def seal_and_observe(self, plan: ControllerRuntimeBuildPlan) -> BuildObservation:
        _require_plan(plan)
        interpreter = str(PurePosixPath(plan.staged_runtime_root) / INTERPRETER_RELATIVE_PATH)
        launcher = str(PurePosixPath(plan.staged_release_root) / LAUNCHER_RELATIVE_PATH)
        confinement_path = str(
            PurePosixPath(plan.stage_root) / _CONFINEMENT_PROOF_NAME
        )
        confinement_node = self._os.observe_no_follow(confinement_path)
        if (
            confinement_node.kind != "file"
            or confinement_node.uid != 0
            or confinement_node.gid != 0
            or confinement_node.mode != 0o400
            or confinement_node.nlink != 1
            or type(confinement_node.size) is not int
            or not 1 <= confinement_node.size <= 4096
        ):
            _fail()
        confinement_raw = self._os.read_regular_no_follow(
            confinement_path, 4096
        )
        confinement_document = _confinement_proof_document(
            plan, confinement_raw
        )
        inventory = self._os.run_exact(
            (interpreter, "-I", "-B", "-c", _INVENTORY_PROBE, plan.staged_runtime_root),
            _ENVIRONMENT,
            "/",
        )
        help_result = self._os.run_exact(
            (
                interpreter,
                "-I",
                "-B",
                launcher,
                "--package-manifest-sha256",
                plan.package_manifest_sha256,
                "--help",
            ),
            _ENVIRONMENT,
            "/",
        )
        if (
            inventory.returncode != 0
            or help_result.returncode != 0
            or len(inventory.stdout) > _MAX_PROBE_BYTES
            or len(inventory.stderr) > _MAX_PROBE_BYTES
            or len(help_result.stdout) > _MAX_PROBE_BYTES
            or len(help_result.stderr) > _MAX_PROBE_BYTES
        ):
            _fail()
        try:
            facts = json.loads(inventory.stdout.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError):
            _fail()
        if (
            type(facts) is not dict
            or facts.get("distributions") != dict(plan.required_distributions)
            or facts.get("pip_present") is not False
            or facts.get("setuptools_present") is not False
            or facts.get("wheel_present") is not False
            or facts.get("user_site_enabled") is not False
            or facts.get("system_site_packages_enabled") is not False
            or facts.get("python_implementation") != "CPython"
            or facts.get("python_version") != plan.substrate.python_version
            or facts.get("platform_os") != "linux"
            or facts.get("platform_architecture") not in {"x86_64", "amd64"}
            or type(facts.get("interpreter_path_facts")) is not dict
            or facts["interpreter_path_facts"].get("prefixes")
            != {
                "base_exec_prefix": ".",
                "base_prefix": ".",
                "exec_prefix": ".",
                "prefix": ".",
            }
            or hashlib.sha256(
                canonical_json_bytes(facts["interpreter_path_facts"])
            ).hexdigest()
            != facts.get("interpreter_path_facts_sha256")
            or facts.get("postgresql_driver_identity_sha256")
            != EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256
        ):
            _fail()
        inventory_path = str(PurePosixPath(plan.staged_runtime_root) / INVENTORY_RELATIVE_PATH)
        self._os.write_regular_no_replace(inventory_path, inventory.stdout, 0o400)
        self._os.fsync_file(inventory_path)
        # Stage-only proof records gate the build but do not become mutable
        # or path-dependent members of the published immutable runtime.
        for proof_path, proof_hash in (
            (
                confinement_path,
                hashlib.sha256(confinement_raw).hexdigest(),
            ),
            (
                str(PurePosixPath(plan.stage_root) / _PAYLOAD_PROOF_NAME),
                self._os.observe_no_follow(
                    str(PurePosixPath(plan.stage_root) / _PAYLOAD_PROOF_NAME)
                ).sha256,
            ),
        ):
            if type(proof_hash) is not str or _HASH_RE.fullmatch(proof_hash) is None:
                _fail()
            self._os.unlink_regular_no_follow(proof_path, proof_hash)
        self._os.seal_tree_root_owned(
            plan.staged_runtime_root, 0o555, 0o444, 0o555
        )
        self._os.seal_tree_root_owned(
            plan.staged_release_root, 0o555, 0o444, 0o555
        )
        self._os.fsync_tree(plan.staged_runtime_root)
        self._os.fsync_tree(plan.staged_release_root)
        runtime = self._os.observe_tree_no_follow(plan.staged_runtime_root)
        release = self._os.observe_tree_no_follow(plan.staged_release_root)
        runtime_files = {item.path: item for item in runtime if item.kind == "file"}
        release_files = {item.path: item for item in release if item.kind == "file"}
        if (
            str(INTERPRETER_RELATIVE_PATH) not in runtime_files
            or str(LAUNCHER_RELATIVE_PATH) not in release_files
        ):
            _fail()
        runtime_hash = _tree_hash(runtime, "governed-memory-controller-runtime-tree-v1")
        release_hash = _release_tree_hash(release, plan.package_manifest_sha256)
        help_hash = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema_version": "governed-memory-supervisor-help-probe-v1",
                    "exit_code": help_result.returncode,
                    "stdout_sha256": hashlib.sha256(help_result.stdout).hexdigest(),
                    "stderr_sha256": hashlib.sha256(help_result.stderr).hexdigest(),
                }
            )
        ).hexdigest()
        return BuildObservation(
            result_type=BUILD_RESULT_TYPE,
            build_plan_sha256=plan.build_plan_sha256,
            substrate_specification_sha256=plan.substrate.specification_sha256,
            substrate_archive_sha256=plan.substrate.archive_sha256,
            substrate_payload_tree_sha256=plan.substrate.payload_tree_sha256,
            wheelhouse_tree_sha256=plan.wheelhouse.tree_sha256,
            release_closure_sha256=plan.release_closure_sha256,
            release_artifact_count=plan.release_artifact_count,
            python_implementation=str(facts.get("python_implementation")),
            python_version=str(facts.get("python_version")),
            platform_os=(
                "linux"
                if facts.get("platform_os") == "linux"
                else str(facts.get("platform_os"))
            ),
            platform_architecture=(
                "x86_64"
                if facts.get("platform_architecture") in {"x86_64", "amd64"}
                else str(facts.get("platform_architecture"))
            ),
            runtime_tree_sha256=runtime_hash,
            release_tree_sha256=release_hash,
            interpreter_sha256=str(runtime_files[str(INTERPRETER_RELATIVE_PATH)].sha256),
            installed_distribution_inventory_sha256=hashlib.sha256(inventory.stdout).hexdigest(),
            interpreter_path_facts_sha256=str(facts.get("interpreter_path_facts_sha256")),
            postgresql_driver_identity_sha256=str(
                facts.get("postgresql_driver_identity_sha256")
            ),
            supervisor_launcher_sha256=str(release_files[str(LAUNCHER_RELATIVE_PATH)].sha256),
            launcher_help_probe_sha256=help_hash,
            pip_present=False,
            setuptools_present=False,
            wheel_present=False,
            user_site_enabled=False,
            system_site_packages_enabled=False,
            normal_venv_created=False,
            offline_substrate_regular_root_owned_nonwritable=True,
            offline_wheelhouse_regular_root_owned_nonwritable=True,
            runtime_root_mode=0o555,
            release_root_mode=0o555,
            all_members_root_owned=True,
            writable_member_count=0,
            symlink_count=0,
            hardlink_count=0,
            special_file_count=0,
            network_calls=0,
            provider_calls=0,
            production_data_read=False,
            active_production_state_changed=False,
        )

    def _validate_publication_receipt(
        self,
        plan: ControllerRuntimeBuildPlan,
        *,
        runtime_root: str,
        receipt_path: str,
        receipt_raw: bytes,
    ) -> None:
        _require_plan(plan)
        if type(runtime_root) is not str or type(receipt_path) is not str:
            _fail()
        runtime_path = PurePosixPath(runtime_root)
        receipt = PurePosixPath(receipt_path)
        runtime_name = PurePosixPath(runtime_root).name
        if (
            str(runtime_path) != runtime_root
            or str(receipt) != receipt_path
            or runtime_path.parent != RUNTIME_ROOT_PREFIX
            or _HASH_RE.fullmatch(runtime_name) is None
            or receipt.parent != RUNTIME_RECEIPT_ROOT_PREFIX
            or receipt.name != runtime_name + ".json"
        ):
            _fail()
        document = _receipt_document(receipt_raw)
        if hashlib.sha256(receipt_raw).hexdigest() != runtime_name:
            _fail()
        exact_strings = {
            "schema_version": RUNTIME_RECEIPT_SCHEMA,
            "result": RUNTIME_RECEIPT_RESULT,
            "package_manifest_sha256": plan.package_manifest_sha256,
            "controller_runtime_contract_sha256": (
                plan.controller_runtime_contract_sha256
            ),
            "controller_requirements_lock_sha256": (
                plan.controller_requirements_lock_sha256
            ),
            "build_plan_sha256": plan.build_plan_sha256,
            "standalone_cpython_specification_sha256": (
                plan.substrate.specification_sha256
            ),
            "standalone_cpython_archive_sha256": plan.substrate.archive_sha256,
            "standalone_cpython_payload_tree_sha256": (
                plan.substrate.payload_tree_sha256
            ),
            "wheelhouse_tree_sha256": plan.wheelhouse.tree_sha256,
            "python_implementation": "CPython",
            "python_version": plan.substrate.python_version,
            "platform_os": "linux",
            "platform_architecture": "x86_64",
            "supervisor_launcher_sha256": plan.supervisor_launcher_sha256,
            "postgresql_driver_identity_sha256": (
                EXPECTED_POSTGRESQL_DRIVER_IDENTITY_SHA256
            ),
        }
        false_keys = (
            "pip_present",
            "setuptools_present",
            "wheel_present",
            "user_site_enabled",
            "system_site_packages_enabled",
            "production_data_read",
            "active_production_state_changed",
            "persistent_store_resources_created",
        )
        if (
            any(
                type(document.get(key)) is not str
                or document[key] != value
                for key, value in exact_strings.items()
            )
            or any(document.get(key) is not False for key in false_keys)
            or document.get("persistent_controller_substrate_created") is not True
            or any(
                type(document.get(key)) is not int or document[key] != 0
                for key in ("network_calls", "provider_calls")
            )
        ):
            _fail()
        hash_keys = (
            "runtime_tree_sha256",
            "release_tree_sha256",
            "interpreter_sha256",
            "installed_distribution_inventory_sha256",
            "interpreter_path_facts_sha256",
            "postgresql_driver_identity_sha256",
            "launcher_help_probe_sha256",
        )
        if any(
            type(document.get(key)) is not str
            or _HASH_RE.fullmatch(str(document[key])) is None
            for key in hash_keys
        ):
            _fail()

        runtime = self._os.observe_tree_no_follow(plan.staged_runtime_root)
        release = self._os.observe_tree_no_follow(plan.staged_release_root)
        runtime_hash = _tree_hash(
            runtime, "governed-memory-controller-runtime-tree-v1"
        )
        release_hash = _release_tree_hash(release, plan.package_manifest_sha256)
        runtime_files = {item.path: item for item in runtime if item.kind == "file"}
        release_files = {item.path: item for item in release if item.kind == "file"}
        interpreter = runtime_files.get(str(INTERPRETER_RELATIVE_PATH))
        inventory = runtime_files.get(str(INVENTORY_RELATIVE_PATH))
        launcher = release_files.get(str(LAUNCHER_RELATIVE_PATH))
        manifest = release_files.get(str(PACKAGE_MANIFEST_RELATIVE_PATH))
        if (
            document["runtime_tree_sha256"] != runtime_hash
            or document["release_tree_sha256"] != release_hash
            or interpreter is None
            or document["interpreter_sha256"] != interpreter.sha256
            or inventory is None
            or document["installed_distribution_inventory_sha256"]
            != inventory.sha256
            or launcher is None
            or document["supervisor_launcher_sha256"] != launcher.sha256
            or manifest is None
            or manifest.sha256 != plan.package_manifest_sha256
        ):
            _fail()

    def observe_publication_targets(
        self, plan: ControllerRuntimeBuildPlan, *, runtime_root: str, receipt_path: str
    ) -> PublicationTargetObservation:
        _require_plan(plan)
        if type(runtime_root) is not str or type(receipt_path) is not str:
            _fail()
        runtime = PurePosixPath(runtime_root)
        receipt = PurePosixPath(receipt_path)
        if (
            str(runtime) != runtime_root
            or str(receipt) != receipt_path
            or runtime.parent != RUNTIME_ROOT_PREFIX
            or _HASH_RE.fullmatch(runtime.name) is None
            or receipt.parent != RUNTIME_RECEIPT_ROOT_PREFIX
            or receipt.name != runtime.name + ".json"
        ):
            _fail()
        states = tuple(
            self._os.observe_no_follow(path).kind
            for path in (runtime_root, plan.final_release_root, receipt_path)
        )
        return PublicationTargetObservation(
            *("absent" if state == "absent" else "present" for state in states),
            secure_no_follow_observation=True,
        )

    def publish_no_replace(
        self,
        plan: ControllerRuntimeBuildPlan,
        *,
        runtime_root: str,
        receipt_path: str,
        runtime_build_receipt_json: bytes,
    ) -> PublicationObservation:
        self._validate_publication_receipt(
            plan,
            runtime_root=runtime_root,
            receipt_path=receipt_path,
            receipt_raw=runtime_build_receipt_json,
        )
        targets = self.observe_publication_targets(
            plan, runtime_root=runtime_root, receipt_path=receipt_path
        )
        if targets != PublicationTargetObservation("absent", "absent", "absent", True):
            _fail()
        marker_path = str(PurePosixPath(plan.stage_root) / _MARKER_NAME)
        lock_path = str(PurePosixPath(plan.stage_root) / _LOCK_NAME)
        lock = self._os.observe_no_follow(lock_path)
        if (
            self._marker_state(marker_path, _marker(plan)) != "valid"
            or lock.kind != "file"
            or lock.mode != 0o400
            or lock.uid != 0
            or lock.gid != 0
            or lock.nlink != 1
            or type(lock.size) is not int
            or lock.size < 1
            or lock.sha256 != plan.controller_requirements_lock_sha256
        ):
            _fail()
        devices = (
            self._os.directory_device_no_follow(plan.staged_runtime_root),
            self._os.directory_device_no_follow(str(PurePosixPath(runtime_root).parent)),
            self._os.directory_device_no_follow(plan.staged_release_root),
            self._os.directory_device_no_follow(
                str(PurePosixPath(plan.final_release_root).parent)
            ),
        )
        if (
            any(type(device) is not int or device <= 0 for device in devices)
            or devices[0] != devices[1]
            or devices[2] != devices[3]
        ):
            _fail()
        publication_intent_path = _publication_intent_path(plan)
        if self._publication_intent_state(plan) != "absent":
            _fail()
        publication_intent = _publication_intent(
            plan,
            runtime_root=runtime_root,
            receipt_path=receipt_path,
            receipt_raw=runtime_build_receipt_json,
        )
        self._os.write_regular_no_replace(
            publication_intent_path, publication_intent, 0o400
        )
        self._os.fsync_file(publication_intent_path)
        self._os.fsync_parent(publication_intent_path)
        if self._publication_intent_state(plan) != "valid":
            _fail()
        self._os.rename_no_replace(plan.staged_runtime_root, runtime_root)
        self._os.fsync_parent(runtime_root)
        self._os.rename_no_replace(plan.staged_release_root, plan.final_release_root)
        self._os.fsync_parent(plan.final_release_root)
        self._os.unlink_regular_no_follow(
            lock_path, plan.controller_requirements_lock_sha256
        )
        self._os.unlink_regular_no_follow(
            marker_path, hashlib.sha256(_marker(plan)).hexdigest()
        )
        self._os.remove_empty_directory_no_follow(plan.stage_root)
        self._os.fsync_parent(plan.stage_root)
        self._os.write_regular_no_replace(receipt_path, runtime_build_receipt_json, 0o400)
        self._os.fsync_file(receipt_path)
        self._os.fsync_parent(receipt_path)
        terminal = self._terminal_publication_result(plan)
        if (
            terminal.runtime_root != runtime_root
            or terminal.release_root != plan.final_release_root
            or terminal.receipt_path != receipt_path
            or terminal.runtime_build_receipt_json != runtime_build_receipt_json
        ):
            _fail()
        return PublicationObservation(
            result_type=PUBLICATION_RESULT_TYPE,
            runtime_root=runtime_root,
            release_root=plan.final_release_root,
            receipt_path=receipt_path,
            runtime_rename_no_replace=True,
            release_rename_no_replace=True,
            receipt_create_no_replace=True,
            runtime_root_mode=0o555,
            release_root_mode=0o555,
            receipt_mode=0o400,
            root_uid=0,
            root_gid=0,
            runtime_parent_fsynced=True,
            release_parent_fsynced=True,
            receipt_file_fsynced=True,
            receipt_parent_fsynced=True,
            stage_absent=True,
            publication_intent_retained=True,
            terminal_state_observed=True,
            same_device_rename_preconditions_observed=True,
        )

    def abandon_owned_stage(self, plan: ControllerRuntimeBuildPlan) -> StageObservation:
        return self.recover_owned_partial_stage(plan)


__all__ = [
    "ArchiveExtraction",
    "ArchiveMember",
    "ClosedRuntimePublicationTransport",
    "NodeObservation",
    "ProcessResult",
    "RuntimePublicationPrimitives",
    "TreeMember",
]

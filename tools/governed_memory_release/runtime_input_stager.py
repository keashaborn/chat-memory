from __future__ import annotations

"""Create-once staging of exact controller-runtime input artifacts.

The caller cannot select a source or destination path.  Both are derived from
the closed runtime build plan.  Incoming roots are separate, root-owned,
non-writable drop locations; verified material is copied into an immutable
temporary directory and published with no-replace rename. Exact completed
destinations replay, while only exact owned partial staging prefixes recover.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Protocol

from .controller_runtime_builder import ControllerRuntimeBuildPlan
from .runtime_publication_transport import NodeObservation, TreeMember, _require_plan


class RuntimeInputStagingError(RuntimeError):
    """Content-free refusal from exact create-only input staging."""


_INCOMING_CPYTHON_ROOT = PurePosixPath(
    "/var/lib/governed-memory-controller/incoming/cpython"
)
_INCOMING_WHEELHOUSE_ROOT = PurePosixPath(
    "/var/lib/governed-memory-controller/incoming/wheelhouses"
)
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class RuntimeInputStageObservation:
    input_kind: str
    identity_sha256: str
    final_root: str
    member_count: int
    total_bytes: int
    create_only: bool
    no_replace_publication: bool
    root_owned_nonwritable: bool
    replayed: bool


class RuntimeInputStagingPrimitives(Protocol):
    def observe_no_follow(self, path: str) -> NodeObservation: ...
    def observe_tree_no_follow(self, root: str) -> tuple[TreeMember, ...]: ...
    def make_directory_no_replace(self, path: str, mode: int) -> None: ...
    def copy_regular_no_replace(
        self,
        source: str,
        destination: str,
        expected_sha256: str,
        expected_size: int,
        mode: int,
    ) -> None: ...
    def seal_tree_root_owned(
        self,
        root: str,
        directory_mode: int,
        regular_mode: int,
        executable_mode: int,
    ) -> None: ...
    def remove_owned_tree_no_follow(self, path: str) -> None: ...
    def fsync_tree(self, root: str) -> None: ...
    def rename_no_replace(self, source: str, destination: str) -> None: ...
    def fsync_parent(self, path: str) -> None: ...


def _fail() -> None:
    raise RuntimeInputStagingError("runtime_input_staging_refused")


def _root_is_exact(node: NodeObservation, mode: int) -> bool:
    return (
        type(node) is NodeObservation
        and node.kind == "directory"
        and node.mode == mode
        and node.uid == 0
        and node.gid == 0
    )


def _file_is_exact(
    node: NodeObservation,
    *,
    mode: int,
    size: int,
    sha256: str,
) -> bool:
    return (
        type(node) is NodeObservation
        and node.kind == "file"
        and node.mode == mode
        and node.uid == 0
        and node.gid == 0
        and node.nlink == 1
        and node.size == size
        and node.sha256 == sha256
    )


class ClosedRuntimeInputStager:
    """Stage only the exact archive and wheelhouse selected by one plan."""

    def __init__(self, primitives: RuntimeInputStagingPrimitives) -> None:
        self._os = primitives

    def stage_selected_cpython_archive(
        self, plan: ControllerRuntimeBuildPlan
    ) -> RuntimeInputStageObservation:
        _require_plan(plan)
        source_root = _INCOMING_CPYTHON_ROOT / plan.substrate.archive_sha256
        source = source_root / plan.substrate.archive_name
        final_root = PurePosixPath(plan.substrate.archive_path).parent
        stage_root = final_root.parent / f".{final_root.name}.staging"
        if (
            _HASH_RE.fullmatch(final_root.name) is None
            or not _root_is_exact(
                self._os.observe_no_follow(str(source_root)), 0o555
            )
        ):
            _fail()
        source_node = self._os.observe_no_follow(str(source))
        if (
            source_node.kind != "file"
            or source_node.mode != 0o444
            or source_node.uid != 0
            or source_node.gid != 0
            or source_node.nlink != 1
            or type(source_node.size) is not int
            or source_node.size < 1
            or source_node.sha256 != plan.substrate.archive_sha256
        ):
            _fail()
        final_node = self._os.observe_no_follow(plan.substrate.archive_path)
        final_root_node = self._os.observe_no_follow(str(final_root))
        stage_root_node = self._os.observe_no_follow(str(stage_root))
        if final_root_node.kind != "absent":
            if (
                stage_root_node.kind != "absent"
                or not _root_is_exact(final_root_node, 0o555)
                or not _file_is_exact(
                    final_node,
                    mode=0o444,
                    size=source_node.size,
                    sha256=plan.substrate.archive_sha256,
                )
                or self._os.observe_tree_no_follow(str(final_root))
                != (
                    TreeMember(
                        plan.substrate.archive_name,
                        "file",
                        0o444,
                        0,
                        0,
                        source_node.size,
                        plan.substrate.archive_sha256,
                        1,
                    ),
                )
            ):
                _fail()
            return RuntimeInputStageObservation(
                "standalone_cpython_archive",
                plan.substrate.archive_sha256,
                str(final_root),
                1,
                source_node.size,
                True,
                True,
                True,
                True,
            )
        if stage_root_node.kind != "absent":
            partial = self._os.observe_tree_no_follow(str(stage_root))
            allowed = {
                plan.substrate.archive_name: (
                    source_node.size,
                    plan.substrate.archive_sha256,
                )
            }
            if (
                stage_root_node.mode not in {0o700, 0o555}
                or stage_root_node.kind != "directory"
                or stage_root_node.uid != 0
                or stage_root_node.gid != 0
                or len(partial) > 1
                or any(
                    member.path not in allowed
                    or member.kind != "file"
                    or member.mode not in {0o400, 0o444}
                    or member.uid != 0
                    or member.gid != 0
                    or member.nlink != 1
                    or (member.size, member.sha256) != allowed[member.path]
                    for member in partial
                )
            ):
                _fail()
            self._os.remove_owned_tree_no_follow(str(stage_root))
            self._os.fsync_parent(str(stage_root))
            if self._os.observe_no_follow(str(stage_root)).kind != "absent":
                _fail()
        self._os.make_directory_no_replace(str(stage_root), 0o700)
        staged_archive = stage_root / plan.substrate.archive_name
        self._os.copy_regular_no_replace(
            str(source),
            str(staged_archive),
            plan.substrate.archive_sha256,
            source_node.size,
            0o400,
        )
        self._os.seal_tree_root_owned(str(stage_root), 0o555, 0o444, 0o555)
        self._os.fsync_tree(str(stage_root))
        self._os.rename_no_replace(str(stage_root), str(final_root))
        self._os.fsync_parent(str(final_root))
        final_node = self._os.observe_no_follow(plan.substrate.archive_path)
        if not _root_is_exact(
            self._os.observe_no_follow(str(final_root)), 0o555
        ) or not _file_is_exact(
            final_node,
            mode=0o444,
            size=source_node.size,
            sha256=plan.substrate.archive_sha256,
        ):
            _fail()
        return RuntimeInputStageObservation(
            "standalone_cpython_archive",
            plan.substrate.archive_sha256,
            str(final_root),
            1,
            source_node.size,
            True,
            True,
            True,
            False,
        )

    def stage_exact_wheelhouse(
        self, plan: ControllerRuntimeBuildPlan
    ) -> RuntimeInputStageObservation:
        _require_plan(plan)
        source_root = _INCOMING_WHEELHOUSE_ROOT / plan.wheelhouse.tree_sha256
        final_root = PurePosixPath(plan.wheelhouse_root)
        stage_root = final_root.parent / f".{final_root.name}.staging"
        if (
            _HASH_RE.fullmatch(final_root.name) is None
            or not _root_is_exact(
                self._os.observe_no_follow(str(source_root)), 0o555
            )
        ):
            _fail()
        observed = self._os.observe_tree_no_follow(str(source_root))
        expected = {member.filename: member for member in plan.wheelhouse.members}
        files = {member.path: member for member in observed if member.kind == "file"}
        if len(observed) != len(expected) or set(files) != set(expected):
            _fail()
        for filename, selected in expected.items():
            member = files[filename]
            if (
                member.mode != 0o444
                or member.uid != 0
                or member.gid != 0
                or member.nlink != 1
                or member.size != selected.size
                or member.sha256 != selected.sha256
            ):
                _fail()
        final_root_node = self._os.observe_no_follow(str(final_root))
        stage_root_node = self._os.observe_no_follow(str(stage_root))
        if final_root_node.kind != "absent":
            final = self._os.observe_tree_no_follow(str(final_root))
            final_files = {
                member.path: member for member in final if member.kind == "file"
            }
            if (
                stage_root_node.kind != "absent"
                or not _root_is_exact(final_root_node, 0o555)
                or len(final) != len(expected)
                or set(final_files) != set(expected)
                or any(
                    member.mode != 0o444
                    or member.uid != 0
                    or member.gid != 0
                    or member.nlink != 1
                    or member.size != expected[name].size
                    or member.sha256 != expected[name].sha256
                    for name, member in final_files.items()
                )
            ):
                _fail()
            return RuntimeInputStageObservation(
                "offline_wheelhouse",
                plan.wheelhouse.tree_sha256,
                str(final_root),
                len(expected),
                sum(member.size for member in expected.values()),
                True,
                True,
                True,
                True,
            )
        if stage_root_node.kind != "absent":
            partial = self._os.observe_tree_no_follow(str(stage_root))
            if (
                stage_root_node.mode not in {0o700, 0o555}
                or stage_root_node.kind != "directory"
                or stage_root_node.uid != 0
                or stage_root_node.gid != 0
                or len(partial) > len(expected)
                or any(
                    member.path not in expected
                    or member.kind != "file"
                    or member.mode not in {0o400, 0o444}
                    or member.uid != 0
                    or member.gid != 0
                    or member.nlink != 1
                    or member.size != expected[member.path].size
                    or member.sha256 != expected[member.path].sha256
                    for member in partial
                )
            ):
                _fail()
            self._os.remove_owned_tree_no_follow(str(stage_root))
            self._os.fsync_parent(str(stage_root))
            if self._os.observe_no_follow(str(stage_root)).kind != "absent":
                _fail()
        self._os.make_directory_no_replace(str(stage_root), 0o700)
        for filename, selected in sorted(expected.items()):
            self._os.copy_regular_no_replace(
                str(source_root / filename),
                str(stage_root / filename),
                selected.sha256,
                selected.size,
                0o400,
            )
        self._os.seal_tree_root_owned(str(stage_root), 0o555, 0o444, 0o555)
        self._os.fsync_tree(str(stage_root))
        self._os.rename_no_replace(str(stage_root), str(final_root))
        self._os.fsync_parent(str(final_root))
        final = self._os.observe_tree_no_follow(str(final_root))
        final_files = {member.path: member for member in final if member.kind == "file"}
        if (
            not _root_is_exact(
                self._os.observe_no_follow(str(final_root)), 0o555
            )
            or len(final) != len(expected)
            or set(final_files) != set(expected)
            or any(
                member.mode != 0o444
                or member.uid != 0
                or member.gid != 0
                or member.nlink != 1
                or member.size != expected[name].size
                or member.sha256 != expected[name].sha256
                for name, member in final_files.items()
            )
        ):
            _fail()
        return RuntimeInputStageObservation(
            "offline_wheelhouse",
            plan.wheelhouse.tree_sha256,
            str(final_root),
            len(expected),
            sum(member.size for member in expected.values()),
            True,
            True,
            True,
            False,
        )


__all__ = [
    "ClosedRuntimeInputStager",
    "RuntimeInputStageObservation",
    "RuntimeInputStagingError",
    "RuntimeInputStagingPrimitives",
]

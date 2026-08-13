from __future__ import annotations

from pathlib import PurePosixPath
import unittest

from tests.memory.test_runtime_publication_transport import _plan
from tools.governed_memory_release.runtime_input_stager import (
    ClosedRuntimeInputStager,
    RuntimeInputStagingError,
)
from tools.governed_memory_release.runtime_publication_transport import (
    NodeObservation,
    TreeMember,
)


class _Fake:
    def __init__(self, plan):
        archive_root = (
            "/var/lib/governed-memory-controller/incoming/cpython/"
            + plan.substrate.archive_sha256
        )
        wheel_root = (
            "/var/lib/governed-memory-controller/incoming/wheelhouses/"
            + plan.wheelhouse.tree_sha256
        )
        member = plan.wheelhouse.members[0]
        self.nodes = {
            archive_root: NodeObservation("directory", 0o555, 0, 0),
            archive_root + "/" + plan.substrate.archive_name: NodeObservation(
                "file", 0o444, 0, 0, 100, plan.substrate.archive_sha256, 1
            ),
            wheel_root: NodeObservation("directory", 0o555, 0, 0),
            wheel_root + "/" + member.filename: NodeObservation(
                "file", 0o444, 0, 0, member.size, member.sha256, 1
            ),
        }
        self.trees = {
            wheel_root: (
                TreeMember(
                    member.filename,
                    "file",
                    0o444,
                    0,
                    0,
                    member.size,
                    member.sha256,
                    1,
                ),
            )
        }

    def observe_no_follow(self, path):
        return self.nodes.get(path, NodeObservation("absent"))

    def observe_tree_no_follow(self, root):
        return self.trees.get(root, ())

    def make_directory_no_replace(self, path, mode):
        if path in self.nodes:
            raise FileExistsError
        self.nodes[path] = NodeObservation("directory", mode, 0, 0)

    def copy_regular_no_replace(
        self, source, destination, expected_sha256, expected_size, mode
    ):
        node = self.nodes[source]
        if node.sha256 != expected_sha256 or node.size != expected_size:
            raise OSError
        self.nodes[destination] = NodeObservation(
            "file", mode, 0, 0, expected_size, expected_sha256, 1
        )

    def seal_tree_root_owned(self, root, directory_mode, regular_mode, executable_mode):
        self.nodes[root] = NodeObservation("directory", directory_mode, 0, 0)
        files = []
        for path, node in list(self.nodes.items()):
            if path.startswith(root + "/") and node.kind == "file":
                self.nodes[path] = NodeObservation(
                    "file", regular_mode, 0, 0, node.size, node.sha256, 1
                )
                files.append(
                    TreeMember(
                        PurePosixPath(path).relative_to(root).as_posix(),
                        "file",
                        regular_mode,
                        0,
                        0,
                        node.size,
                        node.sha256,
                        1,
                    )
                )
        self.trees[root] = tuple(files)

    def fsync_tree(self, root):
        return None

    def rename_no_replace(self, source, destination):
        if destination in self.nodes:
            raise FileExistsError
        for path, node in list(self.nodes.items()):
            if path == source or path.startswith(source + "/"):
                suffix = path[len(source):]
                self.nodes[destination + suffix] = node
                del self.nodes[path]
        self.trees[destination] = self.trees.pop(source, ())

    def fsync_parent(self, path):
        return None

    def remove_owned_tree_no_follow(self, root):
        for path in sorted(
            tuple(self.nodes), key=lambda value: value.count("/"), reverse=True
        ):
            if path == root or path.startswith(root + "/"):
                del self.nodes[path]
        self.trees.pop(root, None)


class RuntimeInputStagerTests(unittest.TestCase):
    def test_archive_and_wheelhouse_are_create_only_and_exact(self):
        plan = _plan()
        primitives = _Fake(plan)
        stager = ClosedRuntimeInputStager(primitives)
        archive = stager.stage_selected_cpython_archive(plan)
        wheelhouse = stager.stage_exact_wheelhouse(plan)
        self.assertTrue(archive.create_only)
        self.assertTrue(archive.root_owned_nonwritable)
        self.assertEqual(wheelhouse.member_count, 1)
        self.assertTrue(wheelhouse.no_replace_publication)
        replayed_archive = stager.stage_selected_cpython_archive(plan)
        replayed_wheelhouse = stager.stage_exact_wheelhouse(plan)
        self.assertTrue(replayed_archive.replayed)
        self.assertTrue(replayed_wheelhouse.replayed)

    def test_exact_owned_partial_input_stages_are_recovered(self):
        plan = _plan()
        primitives = _Fake(plan)
        archive_stage = str(
            PurePosixPath(plan.substrate.archive_path).parent.parent
            / ("." + plan.substrate.archive_sha256 + ".staging")
        )
        archive_source = (
            "/var/lib/governed-memory-controller/incoming/cpython/"
            + plan.substrate.archive_sha256
            + "/"
            + plan.substrate.archive_name
        )
        primitives.make_directory_no_replace(archive_stage, 0o700)
        primitives.copy_regular_no_replace(
            archive_source,
            archive_stage + "/" + plan.substrate.archive_name,
            plan.substrate.archive_sha256,
            100,
            0o400,
        )
        primitives.trees[archive_stage] = (
            TreeMember(
                plan.substrate.archive_name,
                "file",
                0o400,
                0,
                0,
                100,
                plan.substrate.archive_sha256,
                1,
            ),
        )
        result = ClosedRuntimeInputStager(
            primitives
        ).stage_selected_cpython_archive(plan)
        self.assertFalse(result.replayed)
        self.assertEqual(primitives.observe_no_follow(archive_stage).kind, "absent")

    def test_exact_sealed_partial_input_stage_is_recovered(self):
        plan = _plan()
        primitives = _Fake(plan)
        stage = str(
            PurePosixPath(plan.substrate.archive_path).parent.parent
            / ("." + plan.substrate.archive_sha256 + ".staging")
        )
        source = (
            "/var/lib/governed-memory-controller/incoming/cpython/"
            + plan.substrate.archive_sha256
            + "/"
            + plan.substrate.archive_name
        )
        primitives.make_directory_no_replace(stage, 0o700)
        primitives.copy_regular_no_replace(
            source,
            stage + "/" + plan.substrate.archive_name,
            plan.substrate.archive_sha256,
            100,
            0o400,
        )
        primitives.seal_tree_root_owned(stage, 0o555, 0o444, 0o555)
        result = ClosedRuntimeInputStager(
            primitives
        ).stage_selected_cpython_archive(plan)
        self.assertFalse(result.replayed)
        self.assertEqual(primitives.observe_no_follow(stage).kind, "absent")

    def test_source_drift_refuses_before_destination_creation(self):
        plan = _plan()
        primitives = _Fake(plan)
        source = (
            "/var/lib/governed-memory-controller/incoming/cpython/"
            + plan.substrate.archive_sha256
            + "/"
            + plan.substrate.archive_name
        )
        primitives.nodes[source] = NodeObservation(
            "file", 0o444, 0, 0, 100, "0" * 64, 1
        )
        with self.assertRaises(RuntimeInputStagingError):
            ClosedRuntimeInputStager(primitives).stage_selected_cpython_archive(plan)
        self.assertEqual(
            primitives.observe_no_follow(
                str(PurePosixPath(plan.substrate.archive_path).parent)
            ).kind,
            "absent",
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from types import MappingProxyType
import unittest

from tools.governed_memory_install.authority import canonical_json_bytes
from tools.governed_memory_install.controller_runtime import (
    INTERPRETER_RELATIVE_PATH,
    INVENTORY_RELATIVE_PATH,
    LAUNCHER_RELATIVE_PATH,
    PACKAGE_MANIFEST_RELATIVE_PATH,
    RUNTIME_RECEIPT_RESULT,
    RUNTIME_RECEIPT_SCHEMA,
)
from tools.governed_memory_release.controller_runtime_builder import (
    ControllerRuntimeBuildError,
    ControllerRuntimeBuildPlan,
    ExactOfflineWheelhouse,
    PublicationTargetObservation,
    StandaloneCPythonSubstrate,
    WheelhouseMember,
)
from tools.governed_memory_release.runtime_publication_transport import (
    ArchiveMember,
    ClosedRuntimePublicationTransport,
    NodeObservation,
    ProcessResult,
    TreeMember,
)


def _plan() -> ControllerRuntimeBuildPlan:
    plan_hash = "1" * 64
    archive_hash = "2" * 64
    wheel_hash = "3" * 64
    package_hash = "4" * 64
    member_hash = "5" * 64
    stage = f"/var/lib/governed-memory-controller/build-staging/{plan_hash}"
    return ControllerRuntimeBuildPlan(
        schema_version="governed-memory-controller-runtime-build-plan-v1",
        build_plan_sha256=plan_hash,
        build_nonce="6" * 64,
        package_manifest_sha256=package_hash,
        controller_runtime_contract_sha256="7" * 64,
        controller_requirements_lock_sha256="8" * 64,
        release_artifact_count=1,
        release_closure_sha256="9" * 64,
        supervisor_launcher_sha256="a" * 64,
        required_distributions=MappingProxyType({"psycopg": "3.2.9"}),
        substrate=StandaloneCPythonSubstrate(
            specification_sha256="b" * 64,
            python_version="3.12.13",
            archive_name="python.tar.gz",
            archive_sha256=archive_hash,
            payload_tree_sha256="c" * 64,
            archive_path=(
                "/var/lib/governed-memory-controller/offline/cpython/"
                f"{archive_hash}/python.tar.gz"
            ),
        ),
        wheelhouse=ExactOfflineWheelhouse(
            schema_version="governed-memory-controller-offline-wheelhouse-tree-v1",
            tree_sha256=wheel_hash,
            member_count=1,
            total_bytes=10,
            members=(
                WheelhouseMember(
                    distribution="psycopg",
                    version="3.2.9",
                    filename="psycopg-3.2.9-cp312.whl",
                    size=10,
                    sha256=member_hash,
                ),
            ),
        ),
        wheelhouse_root=(
            "/var/lib/governed-memory-controller/offline/wheelhouses/"
            + wheel_hash
        ),
        stage_root=stage,
        staged_runtime_root=stage + "/runtime",
        staged_release_root=stage + "/release",
        final_release_root=(
            "/opt/governed-memory-controller/releases/" + package_hash
        ),
        operations=(),
    )


def _runtime_tree() -> tuple[TreeMember, ...]:
    return (
        TreeMember("bin", "directory", 0o555, 0, 0, 0, None, 1),
        TreeMember(
            str(INTERPRETER_RELATIVE_PATH),
            "file",
            0o555,
            0,
            0,
            20,
            "d" * 64,
            1,
        ),
        TreeMember(
            str(INVENTORY_RELATIVE_PATH),
            "file",
            0o444,
            0,
            0,
            20,
            "e" * 64,
            1,
        ),
    )


def _release_tree(plan: ControllerRuntimeBuildPlan) -> tuple[TreeMember, ...]:
    return (
        TreeMember("bin", "directory", 0o555, 0, 0, 0, None, 1),
        TreeMember(
            str(LAUNCHER_RELATIVE_PATH),
            "file",
            0o555,
            0,
            0,
            20,
            plan.supervisor_launcher_sha256,
            1,
        ),
        TreeMember(
            str(PACKAGE_MANIFEST_RELATIVE_PATH),
            "file",
            0o444,
            0,
            0,
            20,
            plan.package_manifest_sha256,
            1,
        ),
    )


def _tree_hash(members: tuple[TreeMember, ...], schema: str) -> str:
    entries = [
        {
            "path": item.path + ("/" if item.kind == "directory" else ""),
            "mode": item.mode,
            "sha256": item.sha256 if item.kind == "file" else "directory",
        }
        for item in sorted(members, key=lambda candidate: candidate.path)
    ]
    return hashlib.sha256(
        canonical_json_bytes({"schema_version": schema, "entries": entries})
    ).hexdigest()


def _release_tree_hash(
    plan: ControllerRuntimeBuildPlan, members: tuple[TreeMember, ...]
) -> str:
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
                "package_manifest_sha256": plan.package_manifest_sha256,
                "root_mode": 0o555,
                "entries": entries,
            }
        )
    ).hexdigest()


def _receipt(
    plan: ControllerRuntimeBuildPlan,
    *,
    overrides: dict[str, object] | None = None,
) -> bytes:
    runtime = _runtime_tree()
    release = _release_tree(plan)
    document: dict[str, object] = {
        "schema_version": RUNTIME_RECEIPT_SCHEMA,
        "result": RUNTIME_RECEIPT_RESULT,
        "package_manifest_sha256": plan.package_manifest_sha256,
        "controller_runtime_contract_sha256": plan.controller_runtime_contract_sha256,
        "controller_requirements_lock_sha256": plan.controller_requirements_lock_sha256,
        "build_plan_sha256": plan.build_plan_sha256,
        "standalone_cpython_specification_sha256": plan.substrate.specification_sha256,
        "standalone_cpython_archive_sha256": plan.substrate.archive_sha256,
        "standalone_cpython_payload_tree_sha256": plan.substrate.payload_tree_sha256,
        "wheelhouse_tree_sha256": plan.wheelhouse.tree_sha256,
        "python_implementation": "CPython",
        "python_version": plan.substrate.python_version,
        "platform_os": "linux",
        "platform_architecture": "x86_64",
        "runtime_tree_sha256": _tree_hash(
            runtime, "governed-memory-controller-runtime-tree-v1"
        ),
        "release_tree_sha256": _release_tree_hash(plan, release),
        "interpreter_sha256": "d" * 64,
        "installed_distribution_inventory_sha256": "e" * 64,
        "interpreter_path_facts_sha256": "f" * 64,
        "supervisor_launcher_sha256": plan.supervisor_launcher_sha256,
        "launcher_help_probe_sha256": "b" * 64,
        "pip_present": False,
        "setuptools_present": False,
        "wheel_present": False,
        "user_site_enabled": False,
        "system_site_packages_enabled": False,
        "network_calls": 0,
        "provider_calls": 0,
        "production_data_read": False,
        "active_production_state_changed": False,
        "persistent_controller_substrate_created": True,
        "persistent_store_resources_created": False,
    }
    document.update(overrides or {})
    return canonical_json_bytes(document)


class _FakePrimitives:
    def __init__(self, plan: ControllerRuntimeBuildPlan) -> None:
        self.plan = plan
        self.nodes: dict[str, NodeObservation] = {
            plan.substrate.archive_path: NodeObservation(
                "file", 0o444, 0, 0, 100, plan.substrate.archive_sha256, 1
            )
        }
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[object, ...]] = []
        self.archive_members = (
            ArchiveMember("python/bin", "directory", 0o755, 0),
            ArchiveMember("python/bin/python", "file", 0o755, 20),
        )
        member = plan.wheelhouse.members[0]
        self.wheel_tree = (
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
        self.trees: dict[str, tuple[TreeMember, ...]] = {}
        self.fail_receipt = False
        self.fail_stage_marker = False
        self.fail_stage_mkdir = False
        self.crash_after: str | None = None
        self.devices: dict[str, int] = {}
        self.fraud_terminal_runtime_mode = False
        self.fail_terminal_tree_read = False

    def _maybe_crash(self, prefix: str) -> None:
        if self.crash_after == prefix:
            self.crash_after = None
            raise OSError(f"synthetic crash after {prefix}")

    def observe_no_follow(self, path):
        return self.nodes.get(path, NodeObservation("absent"))

    def read_regular_no_follow(self, path, maximum_bytes):
        raw = self.files[path]
        if len(raw) > maximum_bytes:
            raise OSError
        return raw

    def make_directory_no_replace(self, path, mode):
        if self.fail_stage_mkdir and path == self.plan.stage_root:
            raise OSError("synthetic mkdir failure")
        if self.observe_no_follow(path).kind != "absent":
            raise FileExistsError
        self.nodes[path] = NodeObservation("directory", mode, 0, 0)
        self.calls.append(("mkdir", path, mode))

    def make_parent_directories_no_follow(self, path, mode):
        self.calls.append(("parents", path, mode))

    def write_regular_no_replace(self, path, raw, mode):
        if self.fail_receipt and path.endswith(".json") and "/runtime-receipts/" in path:
            raise OSError("synthetic receipt failure")
        if self.fail_stage_marker and path.endswith(
            "/.governed-memory-build-plan.json"
        ):
            partial = raw[:1]
            self.files[path] = partial
            self.nodes[path] = NodeObservation(
                "file",
                mode,
                0,
                0,
                len(partial),
                hashlib.sha256(partial).hexdigest(),
                1,
            )
            raise OSError("synthetic stage marker failure")
        if self.observe_no_follow(path).kind != "absent":
            raise FileExistsError
        self.files[path] = raw
        self.nodes[path] = NodeObservation(
            "file", mode, 0, 0, len(raw), hashlib.sha256(raw).hexdigest(), 1
        )
        self.calls.append(("write", path, mode))
        if path.endswith(".publication-intent.json"):
            self._maybe_crash("intent_write")
        if "/runtime-receipts/" in path:
            self._maybe_crash("receipt_write")

    def remove_owned_tree_no_follow(self, path):
        for candidate in tuple(self.nodes):
            if candidate == path or candidate.startswith(path + "/"):
                del self.nodes[candidate]
                self.files.pop(candidate, None)
        self.trees.pop(path, None)
        self.calls.append(("remove", path))
        if path == self.plan.stage_root and self.observe_no_follow(
            self.plan.stage_root + ".publication-intent.json"
        ).kind == "file":
            self._maybe_crash("stage_cleanup")

    def unlink_regular_no_follow(self, path, expected_sha256):
        node = self.observe_no_follow(path)
        if (
            node.kind != "file"
            or node.mode != 0o400
            or node.uid != 0
            or node.gid != 0
            or node.nlink != 1
            or node.sha256 != expected_sha256
        ):
            raise OSError("synthetic exact unlink refusal")
        del self.nodes[path]
        self.files.pop(path, None)
        self.calls.append(("unlink", path, expected_sha256))
        if path.endswith("/.controller-requirements.lock"):
            self._maybe_crash("lock_unlink")
        if path.endswith("/.governed-memory-build-plan.json"):
            self._maybe_crash("marker_unlink")

    def remove_empty_directory_no_follow(self, path):
        if self.observe_no_follow(path).kind != "directory" or any(
            candidate.startswith(path + "/") for candidate in self.nodes
        ):
            raise OSError("synthetic nonempty directory refusal")
        del self.nodes[path]
        self.calls.append(("rmdir", path))
        self._maybe_crash("stage_empty_remove")

    def directory_device_no_follow(self, path):
        self.calls.append(("device", path))
        return self.devices.get(path, 1)

    def list_archive_members(self, archive_path):
        return self.archive_members

    def extract_regular_member_no_follow(self, archive_path, member_path, destination_path, mode):
        self.nodes[destination_path] = NodeObservation("file", mode, 0, 0, 20, "d" * 64, 1)
        self.calls.append(("extract", archive_path, member_path, destination_path, mode))

    def observe_tree_no_follow(self, root):
        if self.fail_terminal_tree_read and root.startswith(
            "/opt/governed-memory-controller/"
        ):
            raise OSError("synthetic terminal tree read failure")
        if root == self.plan.wheelhouse_root:
            return self.wheel_tree
        return self.trees.get(root, ())

    def seal_tree_root_owned(
        self, root, directory_mode, regular_mode, executable_mode
    ):
        self.calls.append(
            ("seal", root, directory_mode, regular_mode, executable_mode)
        )

    def fsync_tree(self, root):
        self.calls.append(("fsync_tree", root))

    def run_exact(self, argv, environment, cwd):
        self.calls.append(("run", argv, dict(environment), cwd))
        return ProcessResult(0, b"", b"")

    def rename_no_replace(self, source, destination):
        if self.observe_no_follow(destination).kind != "absent":
            raise FileExistsError
        source_node = self.nodes.pop(source)
        self.nodes[destination] = source_node
        if source in self.trees:
            self.trees[destination] = self.trees.pop(source)
        self.calls.append(("rename", source, destination))
        if source == self.plan.staged_runtime_root:
            self._maybe_crash("runtime_rename")
        if source == self.plan.staged_release_root:
            self._maybe_crash("release_rename")

    def fsync_file(self, path):
        self.calls.append(("fsync_file", path))
        if path.endswith(".publication-intent.json"):
            self._maybe_crash("intent_file_fsync")
        if "/runtime-receipts/" in path:
            self._maybe_crash("receipt_file_fsync")

    def fsync_parent(self, path):
        self.calls.append(("fsync_parent", path))
        if path.endswith(".publication-intent.json"):
            self._maybe_crash("intent_parent_fsync")
        if path.startswith("/opt/governed-memory-controller/runtimes/"):
            self._maybe_crash("runtime_parent_fsync")
        if path == self.plan.final_release_root:
            self._maybe_crash("release_parent_fsync")
        if "/runtime-receipts/" in path:
            if self.fraud_terminal_runtime_mode:
                receipt_sha256 = PurePosixPath(path).name.removesuffix(".json")
                runtime = "/opt/governed-memory-controller/runtimes/" + receipt_sha256
                current = self.nodes[runtime]
                self.nodes[runtime] = NodeObservation(
                    current.kind,
                    0o755,
                    current.uid,
                    current.gid,
                    current.size,
                    current.sha256,
                    current.nlink,
                )
            self._maybe_crash("receipt_parent_fsync")


def _stage(transport, primitives, plan):
    created = transport.create_stage(plan)
    assert created.state == "owned_ready"
    primitives.nodes[plan.staged_runtime_root] = NodeObservation("directory", 0o555, 0, 0)
    primitives.nodes[plan.staged_release_root] = NodeObservation("directory", 0o555, 0, 0)
    primitives.trees[plan.staged_runtime_root] = _runtime_tree()
    primitives.trees[plan.staged_release_root] = _release_tree(plan)
    lock_path = plan.stage_root + "/.controller-requirements.lock"
    primitives.nodes[lock_path] = NodeObservation(
        "file",
        0o400,
        0,
        0,
        1,
        plan.controller_requirements_lock_sha256,
        1,
    )


def _publication_paths(plan, receipt_raw):
    receipt_sha256 = hashlib.sha256(receipt_raw).hexdigest()
    return (
        "/opt/governed-memory-controller/runtimes/" + receipt_sha256,
        "/var/lib/governed-memory-controller/runtime-receipts/"
        + receipt_sha256
        + ".json",
        plan.stage_root + ".publication-intent.json",
    )


class RuntimePublicationTransportTests(unittest.TestCase):
    def test_stage_is_create_only_bound_and_recoverable(self):
        plan = _plan()
        primitives = _FakePrimitives(plan)
        transport = ClosedRuntimePublicationTransport(primitives)
        self.assertEqual(transport.observe_stage(plan).state, "absent")
        created = transport.create_stage(plan)
        self.assertEqual(created.state, "owned_ready")
        self.assertEqual(transport.observe_stage(plan).state, "owned_partial")
        recovered = transport.recover_owned_partial_stage(plan)
        self.assertEqual(recovered.state, "absent")
        self.assertEqual(primitives.observe_no_follow(plan.stage_root).kind, "absent")

    def test_stage_marker_crash_is_authenticated_by_durable_intent(self):
        plan = _plan()
        primitives = _FakePrimitives(plan)
        primitives.fail_stage_marker = True
        transport = ClosedRuntimePublicationTransport(primitives)
        with self.assertRaisesRegex(OSError, "synthetic stage marker failure"):
            transport.create_stage(plan)
        self.assertEqual(transport.observe_stage(plan).state, "owned_partial")
        primitives.fail_stage_marker = False
        self.assertEqual(
            transport.recover_owned_partial_stage(plan).state,
            "absent",
        )
        self.assertEqual(primitives.observe_no_follow(plan.stage_root).kind, "absent")

    def test_pre_mkdir_intent_is_reclaimed_only_when_exact(self):
        plan = _plan()
        primitives = _FakePrimitives(plan)
        primitives.fail_stage_mkdir = True
        transport = ClosedRuntimePublicationTransport(primitives)
        with self.assertRaisesRegex(OSError, "synthetic mkdir failure"):
            transport.create_stage(plan)
        self.assertEqual(transport.observe_stage(plan).state, "absent")
        primitives.fail_stage_mkdir = False
        self.assertEqual(transport.create_stage(plan).state, "owned_ready")

    def test_archive_rejects_traversal_before_any_extraction(self):
        plan = _plan()
        primitives = _FakePrimitives(plan)
        primitives.archive_members = (
            ArchiveMember("python/../escape", "file", 0o644, 1),
        )
        transport = ClosedRuntimePublicationTransport(primitives)
        transport.create_stage(plan)
        with self.assertRaisesRegex(
            ControllerRuntimeBuildError,
            "controller_runtime_publication_transport_refused",
        ):
            transport.materialize_standalone_substrate(plan)
        self.assertFalse(any(call[0] == "extract" for call in primitives.calls))

    def test_offline_pip_argv_and_environment_are_closed(self):
        plan = _plan()
        lock = b"psycopg==3.2.9 --hash=sha256:" + b"0" * 64 + b"\n"
        plan = ControllerRuntimeBuildPlan(
            **{
                **{field: getattr(plan, field) for field in plan.__dataclass_fields__},
                "controller_requirements_lock_sha256": hashlib.sha256(lock).hexdigest(),
            }
        )
        primitives = _FakePrimitives(plan)
        transport = ClosedRuntimePublicationTransport(primitives)
        transport.create_stage(plan)
        transport.install_locked_offline_distributions(
            plan, controller_requirements_lock=lock
        )
        run = next(call for call in primitives.calls if call[0] == "run")
        argv, environment, cwd = run[1], run[2], run[3]
        self.assertEqual(argv[0], plan.staged_runtime_root + "/bin/python")
        self.assertIn("--no-index", argv)
        self.assertIn("--require-hashes", argv)
        self.assertEqual(argv[argv.index("--find-links") + 1], plan.wheelhouse_root)
        self.assertEqual(environment["PIP_NO_INDEX"], "1")
        self.assertEqual(environment["PIP_CONFIG_FILE"], "/dev/null")
        self.assertEqual(cwd, "/")

    def test_publication_is_no_replace_fsynced_and_partial_failure_is_preserved(self):
        plan = _plan()
        primitives = _FakePrimitives(plan)
        transport = ClosedRuntimePublicationTransport(primitives)
        _stage(transport, primitives, plan)
        receipt_raw = _receipt(plan)
        receipt_sha256 = hashlib.sha256(receipt_raw).hexdigest()
        runtime = "/opt/governed-memory-controller/runtimes/" + receipt_sha256
        receipt = (
            "/var/lib/governed-memory-controller/runtime-receipts/"
            + receipt_sha256
            + ".json"
        )
        self.assertEqual(
            transport.observe_publication_targets(
                plan, runtime_root=runtime, receipt_path=receipt
            ),
            PublicationTargetObservation("absent", "absent", "absent", True),
        )
        primitives.fail_receipt = True
        with self.assertRaises(OSError):
            transport.publish_no_replace(
                plan,
                runtime_root=runtime,
                receipt_path=receipt,
                runtime_build_receipt_json=receipt_raw,
            )
        self.assertEqual(primitives.observe_no_follow(runtime).kind, "directory")
        self.assertEqual(
            primitives.observe_no_follow(plan.final_release_root).kind,
            "directory",
        )
        self.assertEqual(primitives.observe_no_follow(receipt).kind, "absent")
        self.assertEqual(primitives.observe_no_follow(plan.stage_root).kind, "absent")
        self.assertEqual(transport.observe_stage(plan).state, "publication_started")
        before = (
            dict(primitives.nodes),
            dict(primitives.files),
            dict(primitives.trees),
        )
        with self.assertRaises(ControllerRuntimeBuildError):
            transport.recover_owned_partial_stage(plan)
        with self.assertRaises(ControllerRuntimeBuildError):
            transport.abandon_owned_stage(plan)
        self.assertEqual(
            before,
            (
                primitives.nodes,
                primitives.files,
                primitives.trees,
            ),
        )

    def test_success_is_exactly_observed_and_replays_from_durable_intent(self):
        plan = _plan()
        primitives = _FakePrimitives(plan)
        transport = ClosedRuntimePublicationTransport(primitives)
        _stage(transport, primitives, plan)
        receipt_raw = _receipt(plan)
        runtime, receipt, intent = _publication_paths(plan, receipt_raw)

        publication = transport.publish_no_replace(
            plan,
            runtime_root=runtime,
            receipt_path=receipt,
            runtime_build_receipt_json=receipt_raw,
        )

        self.assertTrue(publication.publication_intent_retained)
        self.assertTrue(publication.terminal_state_observed)
        self.assertTrue(publication.same_device_rename_preconditions_observed)
        self.assertEqual(transport.observe_stage(plan).state, "publication_completed")
        self.assertEqual(primitives.observe_no_follow(intent).kind, "file")
        self.assertNotIn(("remove", plan.stage_root), primitives.calls)
        completed = transport.recover_completed_publication(plan)
        self.assertEqual(completed.runtime_build_receipt_json, receipt_raw)
        self.assertEqual(completed.runtime_root, runtime)
        self.assertEqual(completed.release_root, plan.final_release_root)
        self.assertEqual(completed.receipt_path, receipt)

    def test_cross_device_refuses_before_publication_intent_or_mutation(self):
        plan = _plan()
        primitives = _FakePrimitives(plan)
        transport = ClosedRuntimePublicationTransport(primitives)
        _stage(transport, primitives, plan)
        receipt_raw = _receipt(plan)
        runtime, receipt, intent = _publication_paths(plan, receipt_raw)
        primitives.devices[str(PurePosixPath(runtime).parent)] = 2
        before = (
            dict(primitives.nodes),
            dict(primitives.files),
            dict(primitives.trees),
            len(primitives.calls),
        )

        with self.assertRaises(ControllerRuntimeBuildError):
            transport.publish_no_replace(
                plan,
                runtime_root=runtime,
                receipt_path=receipt,
                runtime_build_receipt_json=receipt_raw,
            )

        self.assertEqual(primitives.observe_no_follow(intent).kind, "absent")
        self.assertEqual(before[:3], (
            primitives.nodes,
            primitives.files,
            primitives.trees,
        ))
        effects = {"write", "rename", "unlink", "rmdir", "remove"}
        self.assertFalse(
            any(call[0] in effects for call in primitives.calls[before[3]:])
        )

    def test_terminal_postcondition_fraud_never_returns_success(self):
        plan = _plan()
        primitives = _FakePrimitives(plan)
        transport = ClosedRuntimePublicationTransport(primitives)
        _stage(transport, primitives, plan)
        receipt_raw = _receipt(plan)
        runtime, receipt, unused_intent = _publication_paths(plan, receipt_raw)
        primitives.fraud_terminal_runtime_mode = True

        with self.assertRaises(ControllerRuntimeBuildError):
            transport.publish_no_replace(
                plan,
                runtime_root=runtime,
                receipt_path=receipt,
                runtime_build_receipt_json=receipt_raw,
            )
        self.assertEqual(transport.observe_stage(plan).state, "publication_started")

    def test_terminal_observation_io_failure_is_manual_review_not_escape(self):
        plan = _plan()
        primitives = _FakePrimitives(plan)
        transport = ClosedRuntimePublicationTransport(primitives)
        _stage(transport, primitives, plan)
        receipt_raw = _receipt(plan)
        runtime, receipt, unused_intent = _publication_paths(plan, receipt_raw)
        transport.publish_no_replace(
            plan,
            runtime_root=runtime,
            receipt_path=receipt,
            runtime_build_receipt_json=receipt_raw,
        )
        primitives.fail_terminal_tree_read = True
        self.assertEqual(transport.observe_stage(plan).state, "publication_started")

    def test_every_publication_crash_prefix_is_durably_classified(self):
        crash_prefixes = (
            "intent_write",
            "intent_file_fsync",
            "intent_parent_fsync",
            "runtime_rename",
            "runtime_parent_fsync",
            "release_rename",
            "release_parent_fsync",
            "lock_unlink",
            "marker_unlink",
            "stage_empty_remove",
            "receipt_write",
            "receipt_file_fsync",
            "receipt_parent_fsync",
        )
        for crash_prefix in crash_prefixes:
            with self.subTest(crash_prefix=crash_prefix):
                plan = _plan()
                primitives = _FakePrimitives(plan)
                transport = ClosedRuntimePublicationTransport(primitives)
                _stage(transport, primitives, plan)
                receipt_raw = _receipt(plan)
                runtime, receipt, intent = _publication_paths(plan, receipt_raw)
                primitives.crash_after = crash_prefix

                with self.assertRaisesRegex(
                    OSError, f"synthetic crash after {crash_prefix}"
                ):
                    transport.publish_no_replace(
                        plan,
                        runtime_root=runtime,
                        receipt_path=receipt,
                        runtime_build_receipt_json=receipt_raw,
                    )

                intent_node = primitives.observe_no_follow(intent)
                self.assertEqual(intent_node.kind, "file")
                self.assertEqual(intent_node.mode, 0o400)
                intent_raw = primitives.read_regular_no_follow(intent, 8192)
                intent_document = json.loads(intent_raw.decode("ascii"))
                self.assertEqual(
                    intent_raw, canonical_json_bytes(intent_document)
                )
                self.assertEqual(
                    intent_document["schema_version"],
                    "governed-memory-controller-publication-intent-v1",
                )
                self.assertEqual(
                    intent_document["build_plan_sha256"],
                    plan.build_plan_sha256,
                )
                self.assertEqual(
                    intent_document["publication_intent_path"], intent
                )
                self.assertEqual(intent_document["stage_root"], plan.stage_root)
                self.assertEqual(
                    intent_document["staged_runtime_root"],
                    plan.staged_runtime_root,
                )
                self.assertEqual(
                    intent_document["staged_release_root"],
                    plan.staged_release_root,
                )
                self.assertEqual(intent_document["runtime_root"], runtime)
                self.assertEqual(
                    intent_document["release_root"], plan.final_release_root
                )
                self.assertEqual(intent_document["receipt_path"], receipt)
                self.assertEqual(
                    intent_document["receipt_sha256"],
                    hashlib.sha256(receipt_raw).hexdigest(),
                )
                expected_presence = {
                    "intent_write": ("absent", "absent", "absent", "directory"),
                    "intent_file_fsync": ("absent", "absent", "absent", "directory"),
                    "intent_parent_fsync": ("absent", "absent", "absent", "directory"),
                    "runtime_rename": ("directory", "absent", "absent", "directory"),
                    "runtime_parent_fsync": ("directory", "absent", "absent", "directory"),
                    "release_rename": ("directory", "directory", "absent", "directory"),
                    "release_parent_fsync": ("directory", "directory", "absent", "directory"),
                    "lock_unlink": ("directory", "directory", "absent", "directory"),
                    "marker_unlink": ("directory", "directory", "absent", "directory"),
                    "stage_empty_remove": ("directory", "directory", "absent", "absent"),
                    "receipt_write": ("directory", "directory", "file", "absent"),
                    "receipt_file_fsync": ("directory", "directory", "file", "absent"),
                    "receipt_parent_fsync": ("directory", "directory", "file", "absent"),
                }[crash_prefix]
                self.assertEqual(
                    (
                        primitives.observe_no_follow(runtime).kind,
                        primitives.observe_no_follow(plan.final_release_root).kind,
                        primitives.observe_no_follow(receipt).kind,
                        primitives.observe_no_follow(plan.stage_root).kind,
                    ),
                    expected_presence,
                )
                intent_write = primitives.calls.index(("write", intent, 0o400))
                if crash_prefix != "intent_write":
                    intent_file_fsync = primitives.calls.index(
                        ("fsync_file", intent)
                    )
                    self.assertLess(intent_write, intent_file_fsync)
                if crash_prefix not in {"intent_write", "intent_file_fsync"}:
                    intent_parent_fsync = primitives.calls.index(
                        ("fsync_parent", intent)
                    )
                    self.assertLess(intent_file_fsync, intent_parent_fsync)
                    renames = [
                        index
                        for index, call in enumerate(primitives.calls)
                        if call[0] == "rename"
                    ]
                    if renames:
                        self.assertLess(intent_parent_fsync, renames[0])

                restarted = ClosedRuntimePublicationTransport(primitives)
                expected_state = (
                    "publication_completed"
                    if crash_prefix in {
                        "receipt_write",
                        "receipt_file_fsync",
                        "receipt_parent_fsync",
                    }
                    else "publication_started"
                )
                self.assertEqual(
                    restarted.observe_stage(plan).state,
                    expected_state,
                )
                if expected_state == "publication_completed":
                    completed = restarted.recover_completed_publication(plan)
                    self.assertEqual(
                        completed.runtime_build_receipt_json, receipt_raw
                    )
                before = (
                    dict(primitives.nodes),
                    dict(primitives.files),
                    dict(primitives.trees),
                )
                with self.assertRaises(ControllerRuntimeBuildError):
                    restarted.recover_owned_partial_stage(plan)
                with self.assertRaises(ControllerRuntimeBuildError):
                    restarted.abandon_owned_stage(plan)
                self.assertEqual(
                    before,
                    (
                        primitives.nodes,
                        primitives.files,
                        primitives.trees,
                    ),
                )

    def test_publication_intent_is_closed_canonical_and_exactly_bound(self):
        plan = _plan()
        primitives = _FakePrimitives(plan)
        transport = ClosedRuntimePublicationTransport(primitives)
        _stage(transport, primitives, plan)
        receipt_raw = _receipt(plan)
        runtime, receipt, intent = _publication_paths(plan, receipt_raw)
        primitives.crash_after = "runtime_rename"
        with self.assertRaises(OSError):
            transport.publish_no_replace(
                plan,
                runtime_root=runtime,
                receipt_path=receipt,
                runtime_build_receipt_json=receipt_raw,
            )
        exact = primitives.files[intent]
        wrong_plan = json.loads(exact.decode("ascii"))
        wrong_plan["build_plan_sha256"] = "0" * 64
        wrong_runtime = json.loads(exact.decode("ascii"))
        wrong_runtime["runtime_root"] = (
            "/opt/governed-memory-controller/runtimes/" + "0" * 64
        )
        wrong_tree = json.loads(exact.decode("ascii"))
        wrong_tree["runtime_tree_sha256"] = "0" * 64

        extra = json.loads(exact.decode("ascii"))
        extra["unexpected"] = True
        wrong_path = json.loads(exact.decode("ascii"))
        wrong_path["receipt_path"] = (
            "/var/lib/governed-memory-controller/runtime-receipts/"
            + "0" * 64
            + ".json"
        )
        duplicate = (
            exact[:-1]
            + b',"build_plan_sha256":"'
            + b"0" * 64
            + b'"}'
        )
        for candidate in (
            canonical_json_bytes(extra),
            canonical_json_bytes(wrong_plan),
            canonical_json_bytes(wrong_runtime),
            canonical_json_bytes(wrong_tree),
            canonical_json_bytes(wrong_path),
            duplicate,
            exact + b"\n",
        ):
            with self.subTest(candidate=candidate[-80:]):
                primitives.files[intent] = candidate
                primitives.nodes[intent] = NodeObservation(
                    "file",
                    0o400,
                    0,
                    0,
                    len(candidate),
                    hashlib.sha256(candidate).hexdigest(),
                    1,
                )
                self.assertEqual(
                    transport.observe_stage(plan).state,
                    "publication_started_foreign",
                )

    def test_receipt_is_canonical_and_duplicate_free_before_any_mutation(self):
        plan = _plan()
        primitives = _FakePrimitives(plan)
        transport = ClosedRuntimePublicationTransport(primitives)
        _stage(transport, primitives, plan)
        malformed = b'{"a":1,"a":2}'
        digest = hashlib.sha256(malformed).hexdigest()
        before = tuple(primitives.calls)
        with self.assertRaises(ControllerRuntimeBuildError):
            transport.publish_no_replace(
                plan,
                runtime_root=f"/opt/governed-memory-controller/runtimes/{digest}",
                receipt_path=(
                    "/var/lib/governed-memory-controller/runtime-receipts/"
                    f"{digest}.json"
                ),
                runtime_build_receipt_json=malformed,
            )
        self.assertEqual(tuple(primitives.calls), before)

        noncanonical = b" " + _receipt(plan)
        digest = hashlib.sha256(noncanonical).hexdigest()
        with self.assertRaises(ControllerRuntimeBuildError):
            transport.publish_no_replace(
                plan,
                runtime_root=f"/opt/governed-memory-controller/runtimes/{digest}",
                receipt_path=(
                    "/var/lib/governed-memory-controller/runtime-receipts/"
                    f"{digest}.json"
                ),
                runtime_build_receipt_json=noncanonical,
            )
        self.assertEqual(tuple(primitives.calls), before)

    def test_receipt_hash_plan_identity_and_staged_tree_are_bound_before_mutation(self):
        plan = _plan()
        primitives = _FakePrimitives(plan)
        transport = ClosedRuntimePublicationTransport(primitives)
        _stage(transport, primitives, plan)
        valid = _receipt(plan)
        before = tuple(primitives.calls)
        with self.assertRaises(ControllerRuntimeBuildError):
            transport.publish_no_replace(
                plan,
                runtime_root="/opt/governed-memory-controller/runtimes/" + "f" * 64,
                receipt_path=(
                    "/var/lib/governed-memory-controller/runtime-receipts/"
                    + "f" * 64
                    + ".json"
                ),
                runtime_build_receipt_json=valid,
            )
        self.assertEqual(tuple(primitives.calls), before)

        wrong_identity = _receipt(plan, overrides={"build_plan_sha256": "0" * 64})
        wrong_identity_digest = hashlib.sha256(wrong_identity).hexdigest()
        with self.assertRaises(ControllerRuntimeBuildError):
            transport.publish_no_replace(
                plan,
                runtime_root=(
                    "/opt/governed-memory-controller/runtimes/"
                    + wrong_identity_digest
                ),
                receipt_path=(
                    "/var/lib/governed-memory-controller/runtime-receipts/"
                    + wrong_identity_digest
                    + ".json"
                ),
                runtime_build_receipt_json=wrong_identity,
            )
        self.assertEqual(tuple(primitives.calls), before)

        wrong_tree = _receipt(plan, overrides={"runtime_tree_sha256": "0" * 64})
        wrong_tree_digest = hashlib.sha256(wrong_tree).hexdigest()
        with self.assertRaises(ControllerRuntimeBuildError):
            transport.publish_no_replace(
                plan,
                runtime_root=(
                    "/opt/governed-memory-controller/runtimes/"
                    + wrong_tree_digest
                ),
                receipt_path=(
                    "/var/lib/governed-memory-controller/runtime-receipts/"
                    + wrong_tree_digest
                    + ".json"
                ),
                runtime_build_receipt_json=wrong_tree,
            )
        self.assertEqual(tuple(primitives.calls), before)

    def test_forged_plan_path_is_refused_before_effect(self):
        plan = _plan()
        primitives = _FakePrimitives(plan)
        forged = ControllerRuntimeBuildPlan(
            **{
                **{field: getattr(plan, field) for field in plan.__dataclass_fields__},
                "wheelhouse_root": "/tmp/caller-wheelhouse",
            }
        )
        transport = ClosedRuntimePublicationTransport(primitives)
        with self.assertRaises(ControllerRuntimeBuildError):
            transport.observe_stage(forged)
        self.assertEqual(primitives.calls, [])


if __name__ == "__main__":
    unittest.main()

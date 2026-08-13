from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from tools.governed_memory_install.linux_live_adapters import (
    BoundedSubprocessFixedArgvRunner,
    ClosedDockerEffects,
    ClosedLinuxPlatformAdapterFactory,
    ClosedQdrantEffects,
    ClosedSystemdEffects,
    CommandDisposition,
    DescriptorDirectoryObservation,
    DescriptorNodeKind,
    DescriptorNodeObservation,
    FixedCommandResult,
    FixedImageIdentity,
    LinuxLiveAdapterError,
    PosixDescriptorSafeRootFilesystem,
    QdrantHttpResponse,
    ROLLBACK_SEMANTIC_EMPTY_PROOF_PATH_TEMPLATE,
    ROLLBACK_CONTROLLER_AUTHORITY_MARKER_PATH_TEMPLATE,
    ResolvedRootSlot,
    RetainedRootDirectory,
    SYSTEMD_DAEMON_RELOAD_ARGV,
    bind_closed_root_files,
)
from tools.governed_memory_install.linux_live_transports import (
    DOCKER_REQUESTS,
    QDRANT_REQUESTS,
    ROOT_FILE_SLOTS,
    DockerRequestId,
    ObservationState,
    QdrantRequestId,
    RootFileSlot,
)
from tools.governed_memory_install.linux_plan import (
    ExecutionBinding,
    bind_store_spec,
    build_store_create_plan,
)
from tools.governed_memory_install.linux_store_readiness import (
    LinuxStoreReadinessError,
)
from tools.governed_memory_install.linux_store_effects import (
    EffectPresence,
    ExactInstallArtifacts,
    ExactSystemdSupervisor,
)


ROOT = Path(__file__).resolve().parents[2]
EXECUTION_ID = "e" * 64
BINDING_SHA256 = "b" * 64
PACKAGE_SHA256 = "c" * 64
POSTGRES_IMAGE_ID = "sha256:" + "1" * 64
QDRANT_IMAGE_ID = "sha256:" + "2" * 64


def _bound_spec() -> dict[str, object]:
    static = json.loads(
        (ROOT / "ops/governed_memory/installation/store_spec.json").read_text(
            encoding="ascii"
        )
    )
    return bind_store_spec(
        static,
        ExecutionBinding(
            BINDING_SHA256,
            "phase9h-test",
            "a" * 64,
            EXECUTION_ID,
            PACKAGE_SHA256,
        ),
    )


def _artifacts() -> ExactInstallArtifacts:
    paths = (
        "ops/governed_memory/installation/postgres/canonical_cluster.pgsql.in",
        "ops/governed_memory/installation/postgres/canonical_cluster_rollback.pgsql.in",
        "ops/governed_memory/installation/current/postgres/roles_preflight.pgsql",
        "governed-memory-migrations/0001_foundation/forward.pgsql",
        "governed-memory-migrations/0001_foundation/rollback.pgsql",
        "governed-memory-migrations/0003_owner_claim_detail/forward.pgsql",
        "governed-memory-migrations/0003_owner_claim_detail/rollback.pgsql",
        "governed-memory-migrations/0004_pilot_marker/forward.pgsql",
        "governed-memory-migrations/0004_pilot_marker/rollback.pgsql",
        "ops/governed_memory/qdrant_collection.create.json",
        "ops/governed_memory/qdrant_alias.create.json",
        "ops/governed_memory/installation/systemd/governed-memory-stores.service.in",
    )
    return ExactInstallArtifacts.from_verified_mapping(
        {path: (ROOT / path).read_bytes() for path in paths}
    )


def _images(
    spec: dict[str, object],
) -> tuple[FixedImageIdentity, FixedImageIdentity]:
    containers = spec["resources"]["containers"]
    return (
        FixedImageIdentity(
            "postgres",
            POSTGRES_IMAGE_ID,
            containers["postgres"]["image"]["reference"],
            containers["postgres"]["image"]["repo_digest"],
        ),
        FixedImageIdentity(
            "qdrant",
            QDRANT_IMAGE_ID,
            containers["qdrant"]["image"]["reference"],
            containers["qdrant"]["image"]["repo_digest"],
        ),
    )


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.results: dict[tuple[str, ...], deque[FixedCommandResult]] = defaultdict(deque)
        self.failures: dict[tuple[str, ...], int] = defaultdict(int)

    def queue(
        self,
        argv: tuple[str, ...],
        disposition: CommandDisposition,
        stdout: bytes = b"",
    ) -> None:
        self.results[argv].append(FixedCommandResult(argv, disposition, stdout))

    def execute(self, argv: tuple[str, ...]) -> FixedCommandResult:
        self.calls.append(argv)
        if self.failures[argv] > 0:
            self.failures[argv] -= 1
            raise RuntimeError("synthetic command failure")
        queued = self.results.get(argv)
        if queued:
            return queued.popleft()
        return FixedCommandResult(argv, CommandDisposition.SUCCESS, b"")


class FakeRootFilesystem:
    def __init__(self) -> None:
        self.nodes: dict[str, DescriptorNodeObservation] = {}
        self.calls: list[tuple[object, ...]] = []
        self._inode = 100

    @staticmethod
    def _absent() -> DescriptorNodeObservation:
        return DescriptorNodeObservation(DescriptorNodeKind.ABSENT)

    def observe(self, slot: ResolvedRootSlot) -> DescriptorNodeObservation:
        self.calls.append(("observe", slot.slot, slot.path))
        return self.nodes.get(slot.path, self._absent())

    def create_regular_exclusive(
        self, slot: ResolvedRootSlot, content: bytes
    ) -> DescriptorNodeObservation:
        self.calls.append(("create_regular_exclusive_fsync", slot.slot, slot.path))
        if slot.path in self.nodes:
            raise RuntimeError("exclusive create collision")
        self._inode += 1
        contract = ROOT_FILE_SLOTS[slot.slot]
        observed = DescriptorNodeObservation(
            DescriptorNodeKind.REGULAR_FILE,
            device=7,
            inode=self._inode,
            uid=0,
            gid=0,
            mode=contract.required_mode,
            link_count=1,
            content=content,
        )
        self.nodes[slot.path] = observed
        return observed

    def create_symlink_exclusive(
        self, slot: ResolvedRootSlot
    ) -> DescriptorNodeObservation:
        self.calls.append(("create_symlink_exclusive_fsync", slot.slot, slot.path))
        if slot.path in self.nodes:
            raise RuntimeError("exclusive symlink collision")
        self._inode += 1
        observed = DescriptorNodeObservation(
            DescriptorNodeKind.SYMBOLIC_LINK,
            device=7,
            inode=self._inode,
            uid=0,
            gid=0,
            mode=0o777,
            link_count=1,
            symlink_target=ROOT_FILE_SLOTS[slot.slot].symlink_target,
        )
        self.nodes[slot.path] = observed
        return observed

    def remove_regular_exact(self, slot, identity) -> None:
        self.calls.append(("remove_regular_exact_fsync", slot.slot, slot.path))
        current = self.nodes[slot.path]
        if (current.device, current.inode, current.content_sha256) != (
            identity.device,
            identity.inode,
            identity.content_sha256,
        ):
            raise RuntimeError("identity changed")
        del self.nodes[slot.path]

    def remove_symlink_exact(
        self, slot, *, expected_device: int, expected_inode: int
    ) -> None:
        self.calls.append(("remove_symlink_exact_fsync", slot.slot, slot.path))
        current = self.nodes[slot.path]
        if (current.device, current.inode) != (expected_device, expected_inode):
            raise RuntimeError("identity changed")
        del self.nodes[slot.path]

    def observe_retained_directory(
        self, slot: RetainedRootDirectory
    ) -> DescriptorDirectoryObservation:
        path = {
            RetainedRootDirectory.CONTROLLER_CONFIG: "/etc/governed-memory-controller",
            RetainedRootDirectory.STORE_SECRET: "/etc/governed-memory-stores/9a54cf123493-000001",
        }[slot]
        self.calls.append(("observe_retained_directory", slot, path))
        return DescriptorDirectoryObservation(slot, path, 7, 10, 0, 0, 0o700)


class FakeQdrantClient:
    def __init__(self) -> None:
        self.calls: list[QdrantRequestId] = []
        self.responses: dict[QdrantRequestId, deque[QdrantHttpResponse]] = defaultdict(deque)

    def queue(self, request_id: QdrantRequestId, status: int, value: object) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("ascii")
        self.responses[request_id].append(
            QdrantHttpResponse(request_id, status, body)
        )

    def queue_raw(
        self,
        request_id: QdrantRequestId,
        status: int,
        body: bytes,
    ) -> None:
        self.responses[request_id].append(
            QdrantHttpResponse(request_id, status, body)
        )

    def exchange(self, request) -> QdrantHttpResponse:
        self.calls.append(request.request_id)
        return self.responses[request.request_id].popleft()


class RootFileAndMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fs = FakeRootFilesystem()
        self.files = bind_closed_root_files(
            filesystem=self.fs,
            execution_id=EXECUTION_ID,
            resolved_store_spec_sha256="d" * 64,
        )

    def test_marker_path_and_lifecycle_are_fixed_and_identity_bound(self) -> None:
        document = {
            "execution_id": EXECUTION_ID,
            "controller_authority_marker_sha256": "f" * 64,
            "plan_sha256": "9" * 64,
            "schema_version": "governed-memory-empty-rollback-controller-authority-marker-v1",
        }
        raw = json.dumps(
            document, sort_keys=True, separators=(",", ":")
        ).encode("ascii") + b"\n"
        self.assertEqual(
            self.files.rollback_controller_authority_marker_path,
            ROLLBACK_CONTROLLER_AUTHORITY_MARKER_PATH_TEMPLATE.replace(
                "{execution_id}", EXECUTION_ID
            ),
        )
        identity = self.files.create_rollback_controller_authority_marker(raw)
        observed = self.files.observe_rollback_controller_authority_marker(identity.content_sha256)
        self.assertIs(observed.observation.state, ObservationState.EXACT)
        self.assertEqual(observed.identity, identity)
        self.files.remove_rollback_controller_authority_marker(identity)
        self.assertIs(
            self.files.observe_rollback_controller_authority_marker(identity.content_sha256).observation.state,
            ObservationState.ABSENT,
        )
        self.assertIn(
            (
                "create_regular_exclusive_fsync",
                RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER,
                self.files.rollback_controller_authority_marker_path,
            ),
            self.fs.calls,
        )
        self.assertIn(
            (
                "remove_regular_exact_fsync",
                RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER,
                self.files.rollback_controller_authority_marker_path,
            ),
            self.fs.calls,
        )

    def test_marker_rejects_wrong_execution_and_changed_inode(self) -> None:
        wrong = json.dumps(
            {"execution_id": "a" * 64}, sort_keys=True, separators=(",", ":")
        ).encode("ascii") + b"\n"
        with self.assertRaisesRegex(
            LinuxLiveAdapterError, "execution_mismatch"
        ):
            self.files.create_rollback_controller_authority_marker(wrong)

        raw = json.dumps(
            {"execution_id": EXECUTION_ID},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        identity = self.files.create_rollback_controller_authority_marker(raw)
        path = self.files.rollback_controller_authority_marker_path
        current = self.fs.nodes[path]
        self.fs.nodes[path] = DescriptorNodeObservation(
            DescriptorNodeKind.REGULAR_FILE,
            device=current.device,
            inode=current.inode + 1,
            uid=0,
            gid=0,
            mode=ROOT_FILE_SLOTS[
                RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER
            ].required_mode,
            link_count=1,
            content=current.content,
        )
        with self.assertRaisesRegex(
            LinuxLiveAdapterError, "identity_changed"
        ):
            self.files.remove_rollback_controller_authority_marker(identity)

    def test_semantic_empty_proof_is_a_distinct_read_only_exact_record(self) -> None:
        document = {
            "execution_id": EXECUTION_ID,
            "schema_version": (
                "governed-memory-empty-rollback-semantic-empty-proof-v1"
            ),
            "semantic_empty_observation_sha256": "8" * 64,
        }
        raw = json.dumps(
            document, sort_keys=True, separators=(",", ":")
        ).encode("ascii") + b"\n"
        identity = self.files.create_rollback_semantic_empty_proof(raw)
        self.assertIs(identity.slot, RootFileSlot.ROLLBACK_SEMANTIC_EMPTY_PROOF)
        self.assertEqual(
            self.files.rollback_semantic_empty_proof_path,
            ROLLBACK_SEMANTIC_EMPTY_PROOF_PATH_TEMPLATE.replace(
                "{execution_id}", EXECUTION_ID
            ),
        )
        self.assertEqual(
            ROOT_FILE_SLOTS[RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER].required_mode,
            0o400,
        )
        self.assertEqual(
            ROOT_FILE_SLOTS[
                RootFileSlot.ROLLBACK_SEMANTIC_EMPTY_PROOF
            ].required_mode,
            0o400,
        )
        self.assertEqual(
            self.files.read_rollback_semantic_empty_proof(
                identity.content_sha256
            ),
            raw,
        )
        self.files.remove_rollback_semantic_empty_proof(identity)
        self.assertIsNone(
            self.files.read_rollback_semantic_empty_proof(
                identity.content_sha256
            )
        )

    def test_posix_implementation_contains_exclusive_and_durable_primitives(self) -> None:
        source = (
            ROOT / "tools/governed_memory_install/linux_live_adapters.py"
        ).read_text(encoding="utf-8")
        self.assertIn("os.O_EXCL", source)
        self.assertIn("O_NOFOLLOW", source)
        self.assertIn("os.fsync(file_fd)", source)
        self.assertIn("os.fsync(parent_fd)", source)
        self.assertTrue(callable(PosixDescriptorSafeRootFilesystem))


class DockerAdapterTests(unittest.TestCase):
    def test_concrete_runner_is_bound_at_construction_and_refuses_other_argv(
        self,
    ) -> None:
        runner = BoundedSubprocessFixedArgvRunner(_bound_spec())
        with self.assertRaisesRegex(
            LinuxLiveAdapterError, "fixed_command_argv_refused"
        ):
            runner.execute(("/bin/sh", "-c", "true"))
        observe = DOCKER_REQUESTS[DockerRequestId.OBSERVE_NETWORK].argv
        self.assertTrue(
            runner._verified_absence(
                observe,
                b"[]\n",
                (
                    "Error response from daemon: network "
                    + observe[-1]
                    + " not found\n"
                ).encode("utf-8"),
            )
        )
        self.assertFalse(
            runner._verified_absence(
                observe,
                b"[]\n",
                b"permission denied\n",
            )
        )

    def test_construction_is_effect_free_and_create_uses_only_bound_plan(self) -> None:
        spec = _bound_spec()
        runner = FakeRunner()
        adapter = ClosedDockerEffects(
            runner=runner, resolved_store_spec=spec, images=_images(spec)
        )
        self.assertEqual(runner.calls, [])
        observe = DOCKER_REQUESTS[DockerRequestId.OBSERVE_NETWORK].argv
        runner.queue(observe, CommandDisposition.VERIFIED_ABSENT)
        adapter.create_network()
        expected_create = build_store_create_plan(spec)[0].argv
        self.assertEqual(runner.calls, [observe, expected_create])
        self.assertEqual(expected_create[0], "/usr/bin/docker")
        self.assertIn("--internal", expected_create)
        self.assertNotIn("pull", expected_create)

    def test_verified_absence_is_not_a_delete_instruction(self) -> None:
        spec = _bound_spec()
        runner = FakeRunner()
        adapter = ClosedDockerEffects(
            runner=runner, resolved_store_spec=spec, images=_images(spec)
        )
        observe = DOCKER_REQUESTS[DockerRequestId.OBSERVE_POSTGRES_VOLUME].argv
        runner.queue(observe, CommandDisposition.VERIFIED_ABSENT)
        adapter.remove_postgres_volume()
        self.assertEqual(runner.calls, [observe])

    def test_no_public_adapter_method_accepts_argv_or_resource_name(self) -> None:
        effect_methods = {
            name
            for name in dir(ClosedDockerEffects)
            if name.startswith(("create_", "start_", "stop_", "remove_"))
        }
        self.assertEqual(
            effect_methods,
            {
                "create_network",
                "create_postgres_container",
                "create_postgres_volume",
                "create_qdrant_container",
                "create_qdrant_volume",
                "remove_network",
                "remove_postgres_container",
                "remove_postgres_volume",
                "remove_qdrant_container",
                "remove_qdrant_volume",
                "start_postgres_container",
                "start_qdrant_container",
                "stop_postgres_container",
                "stop_qdrant_container",
            },
        )


class SystemdAdapterTests(unittest.TestCase):
    @staticmethod
    def _installed_supervisor():
        fs = FakeRootFilesystem()
        files = bind_closed_root_files(
            filesystem=fs,
            execution_id=EXECUTION_ID,
            resolved_store_spec_sha256="d" * 64,
        )
        adapter = ClosedSystemdEffects(runner=FakeRunner(), files=files)
        exact = ExactSystemdSupervisor.from_rendered_unit(
            b"[Unit]\nDescription=metadata-bound supervisor\n"
        )
        adapter.install_and_enable_supervisor(exact)
        unit_slot = ResolvedRootSlot(RootFileSlot.SYSTEMD_UNIT, EXECUTION_ID)
        link_slot = ResolvedRootSlot(
            RootFileSlot.SYSTEMD_ENABLEMENT, EXECUTION_ID
        )
        return fs, adapter, exact, unit_slot, link_slot

    def test_observation_refuses_unit_and_link_metadata_drift(self) -> None:
        fs, adapter, exact, unit_slot, link_slot = self._installed_supervisor()
        exact_unit = fs.nodes[unit_slot.path]
        exact_link = fs.nodes[link_slot.path]
        self.assertIs(
            adapter.observe_supervisor().classify(exact).presence,
            EffectPresence.EXACT,
        )
        for unsafe_unit in (
            replace(exact_unit, uid=1000),
            replace(exact_unit, mode=0o666),
            replace(exact_unit, link_count=2),
        ):
            with self.subTest(unit=unsafe_unit):
                fs.nodes[unit_slot.path] = unsafe_unit
                self.assertIs(
                    adapter.observe_supervisor().classify(exact).presence,
                    EffectPresence.DRIFT,
                )
        fs.nodes[unit_slot.path] = exact_unit
        for unsafe_link in (
            replace(exact_link, uid=1000),
            replace(exact_link, link_count=2),
        ):
            with self.subTest(link=unsafe_link):
                fs.nodes[link_slot.path] = unsafe_link
                self.assertIs(
                    adapter.observe_supervisor().classify(exact).presence,
                    EffectPresence.DRIFT,
                )

    def test_effect_refuses_hard_linked_enablement_prefix(self) -> None:
        fs, adapter, exact, _, link_slot = self._installed_supervisor()
        fs.nodes[link_slot.path] = replace(
            fs.nodes[link_slot.path], link_count=2
        )
        with self.assertRaisesRegex(
            LinuxLiveAdapterError, "systemd_prefix_drift_refused"
        ):
            adapter.disable_and_remove_supervisor(exact)
        self.assertIn(link_slot.path, fs.nodes)

    def test_partial_unit_prefix_recovers_with_exact_link_and_reload(self) -> None:
        fs = FakeRootFilesystem()
        files = bind_closed_root_files(
            filesystem=fs,
            execution_id=EXECUTION_ID,
            resolved_store_spec_sha256="d" * 64,
        )
        runner = FakeRunner()
        adapter = ClosedSystemdEffects(runner=runner, files=files)
        exact = ExactSystemdSupervisor.from_rendered_unit(
            b"[Unit]\nDescription=governed memory stores\n"
        )
        fs.create_regular_exclusive(
            ResolvedRootSlot(RootFileSlot.SYSTEMD_UNIT, EXECUTION_ID),
            exact.unit_content,
        )
        fs.calls.clear()
        adapter.install_and_enable_supervisor(exact)
        self.assertEqual(runner.calls, [SYSTEMD_DAEMON_RELOAD_ARGV])
        self.assertTrue(
            any(call[0] == "create_symlink_exclusive_fsync" for call in fs.calls)
        )

    def test_remove_recovers_each_exact_prefix_and_reloads(self) -> None:
        fs = FakeRootFilesystem()
        files = bind_closed_root_files(
            filesystem=fs,
            execution_id=EXECUTION_ID,
            resolved_store_spec_sha256="d" * 64,
        )
        runner = FakeRunner()
        adapter = ClosedSystemdEffects(runner=runner, files=files)
        exact = ExactSystemdSupervisor.from_rendered_unit(b"[Unit]\nDescription=x\n")
        adapter.install_and_enable_supervisor(exact)
        runner.calls.clear()
        adapter.disable_and_remove_supervisor(exact)
        self.assertEqual(
            runner.calls,
            [SYSTEMD_DAEMON_RELOAD_ARGV],
        )
        snapshot = adapter.observe_supervisor()
        self.assertEqual(snapshot.unit_kind.value, "absent")
        self.assertEqual(snapshot.enablement_kind.value, "absent")

    def test_failed_reload_leaves_pending_record_and_retry_recovers(self) -> None:
        fs = FakeRootFilesystem()
        files = bind_closed_root_files(
            filesystem=fs,
            execution_id=EXECUTION_ID,
            resolved_store_spec_sha256="d" * 64,
        )
        runner = FakeRunner()
        runner.failures[SYSTEMD_DAEMON_RELOAD_ARGV] = 1
        adapter = ClosedSystemdEffects(runner=runner, files=files)
        exact = ExactSystemdSupervisor.from_rendered_unit(
            b"[Unit]\nDescription=recover reload\n"
        )
        with self.assertRaisesRegex(
            LinuxLiveAdapterError, "fixed_command_failed"
        ):
            adapter.install_and_enable_supervisor(exact)
        pending = adapter.observe_supervisor()
        self.assertTrue(pending.daemon_reload_pending)
        self.assertIs(
            pending.classify(exact).presence,
            EffectPresence.PARTIAL,
        )
        adapter.install_and_enable_supervisor(exact)
        final = adapter.observe_supervisor()
        self.assertFalse(final.daemon_reload_pending)
        self.assertIs(final.classify(exact).presence, EffectPresence.EXACT)


class QdrantAdapterTests(unittest.TestCase):
    @staticmethod
    def _collection() -> dict[str, object]:
        return {
            "result": {
                "points_count": 0,
                "config": {
                    "params": {
                        "vectors": {"size": 3072, "distance": "Dot"},
                        "on_disk_payload": True,
                        "replication_factor": 1,
                    }
                },
            }
        }

    def test_create_uses_fixed_404_probe_then_fixed_request_bytes(self) -> None:
        client = FakeQdrantClient()
        adapter = ClosedQdrantEffects(client=client, artifacts=_artifacts())
        client.queue(QdrantRequestId.OBSERVE_COLLECTION, 404, {"status": "not_found"})
        client.queue(
            QdrantRequestId.CREATE_COLLECTION,
            200,
            {"result": True, "status": "ok"},
        )
        adapter.create_collection()
        self.assertEqual(
            client.calls,
            [QdrantRequestId.OBSERVE_COLLECTION, QdrantRequestId.CREATE_COLLECTION],
        )
        self.assertEqual(
            QDRANT_REQUESTS[QdrantRequestId.CREATE_COLLECTION].target,
            b"/collections/governed_memory_9a54cf123493_000001",
        )

    def test_create_alias_accepts_exact_empty_collection_alias_result(self) -> None:
        client = FakeQdrantClient()
        adapter = ClosedQdrantEffects(client=client, artifacts=_artifacts())
        client.queue(
            QdrantRequestId.OBSERVE_ALIAS,
            200,
            {"result": {"aliases": []}},
        )
        client.queue(
            QdrantRequestId.CREATE_ALIAS,
            200,
            {"result": True, "status": "ok"},
        )
        adapter.create_alias()
        self.assertEqual(
            client.calls,
            [QdrantRequestId.OBSERVE_ALIAS, QdrantRequestId.CREATE_ALIAS],
        )

    def test_response_parser_refuses_duplicate_and_nonfinite_json(self) -> None:
        cases = (
            b'{"version":"wrong","version":"1.19.0","title":"qdrant"}',
            b'{"version":"1.19.0","title":"qdrant","time":NaN}',
            b'{"version":"1.19.0","title":"qdrant","time":Infinity}',
            b'{"version":"1.19.0","title":"qdrant","time":-Infinity}',
        )
        for raw in cases:
            client = FakeQdrantClient()
            adapter = ClosedQdrantEffects(client=client, artifacts=_artifacts())
            client.queue_raw(QdrantRequestId.OBSERVE_ROOT, 200, raw)
            with self.subTest(raw=raw), self.assertRaisesRegex(
                LinuxLiveAdapterError,
                "qdrant_response_json_invalid",
            ):
                adapter.inspect_prebootstrap()

    def test_alias_and_mutation_responses_are_closed(self) -> None:
        client = FakeQdrantClient()
        adapter = ClosedQdrantEffects(client=client, artifacts=_artifacts())
        client.queue_raw(
            QdrantRequestId.OBSERVE_ALIAS,
            200,
            (
                b'{"result":{"aliases":[{"alias_name":"governed_memory_active",'
                b'"collection_name":"governed_memory_9a54cf123493_000001",'
                b'"unexpected":true}]}}'
            ),
        )
        with self.assertRaisesRegex(
            LinuxLiveAdapterError,
            "qdrant_alias_shape_invalid",
        ):
            adapter.observe_alias()

        client = FakeQdrantClient()
        adapter = ClosedQdrantEffects(client=client, artifacts=_artifacts())
        client.queue(QdrantRequestId.OBSERVE_COLLECTION, 404, {"status": "not_found"})
        client.queue_raw(
            QdrantRequestId.CREATE_COLLECTION,
            200,
            b'{"result":true,"status":"ok","unexpected":true}',
        )
        with self.assertRaisesRegex(
            LinuxLiveAdapterError,
            "qdrant_mutation_shape_invalid",
        ):
            adapter.create_collection()

    def test_nonexact_collection_scalar_types_are_refused(self) -> None:
        for field, value in (("size", 3072.0), ("replication_factor", True)):
            collection = self._collection()
            params = collection["result"]["config"]["params"]
            if field == "size":
                params["vectors"][field] = value
            else:
                params[field] = value
            client = FakeQdrantClient()
            adapter = ClosedQdrantEffects(client=client, artifacts=_artifacts())
            client.queue(QdrantRequestId.OBSERVE_COLLECTION, 200, collection)
            with self.subTest(field=field), self.assertRaisesRegex(
                LinuxStoreReadinessError,
                "terminal_qdrant_collection_config_invalid",
            ):
                adapter.observe_collection()

    def test_fresh_store_refuses_any_unrelated_collection(self) -> None:
        client = FakeQdrantClient()
        adapter = ClosedQdrantEffects(client=client, artifacts=_artifacts())
        client.queue(
            QdrantRequestId.OBSERVE_ROOT,
            200,
            {"version": "1.19.0", "title": "qdrant"},
        )
        client.queue(QdrantRequestId.OBSERVE_COLLECTION, 404, {"status": "not_found"})
        client.queue(
            QdrantRequestId.OBSERVE_COLLECTIONS,
            200,
            {"result": {"collections": [{"name": "unrelated_collection"}]}},
        )
        with self.assertRaisesRegex(
            LinuxLiveAdapterError,
            "qdrant_prebootstrap_not_empty",
        ):
            adapter.inspect_prebootstrap()
        self.assertEqual(
            client.calls,
            [
                QdrantRequestId.OBSERVE_ROOT,
                QdrantRequestId.OBSERVE_COLLECTION,
                QdrantRequestId.OBSERVE_COLLECTIONS,
            ],
        )

    def test_terminal_counts_every_noncanonical_collection_as_contamination(self) -> None:
        client = FakeQdrantClient()
        adapter = ClosedQdrantEffects(client=client, artifacts=_artifacts())
        client.queue(
            QdrantRequestId.OBSERVE_ROOT,
            200,
            {"version": "1.19.0", "title": "qdrant"},
        )
        client.queue(QdrantRequestId.OBSERVE_COLLECTION, 200, self._collection())
        client.queue(
            QdrantRequestId.OBSERVE_ALIAS,
            200,
            {
                "result": {
                    "aliases": [
                        {
                            "alias_name": "governed_memory_active",
                            "collection_name": "governed_memory_9a54cf123493_000001",
                        }
                    ]
                }
            },
        )
        client.queue(
            QdrantRequestId.OBSERVE_COLLECTIONS,
            200,
            {
                "result": {
                    "collections": [
                        {"name": "governed_memory_9a54cf123493_000001"},
                        {"name": "unrelated_collection"},
                    ]
                }
            },
        )
        with self.assertRaisesRegex(
            LinuxStoreReadinessError,
            "terminal_qdrant_snapshot_invalid",
        ):
            adapter.inspect_terminal()

    def test_terminal_probe_is_exact_empty_and_fixed_endpoint_only(self) -> None:
        client = FakeQdrantClient()
        adapter = ClosedQdrantEffects(client=client, artifacts=_artifacts())
        client.queue(
            QdrantRequestId.OBSERVE_ROOT,
            200,
            {"version": "1.19.0", "title": "qdrant"},
        )
        client.queue(QdrantRequestId.OBSERVE_COLLECTION, 200, self._collection())
        client.queue(
            QdrantRequestId.OBSERVE_ALIAS,
            200,
            {
                "result": {
                    "aliases": [
                        {
                            "alias_name": "governed_memory_active",
                            "collection_name": "governed_memory_9a54cf123493_000001",
                        }
                    ]
                }
            },
        )
        client.queue(
            QdrantRequestId.OBSERVE_COLLECTIONS,
            200,
            {
                "result": {
                    "collections": [
                        {"name": "governed_memory_9a54cf123493_000001"}
                    ]
                }
            },
        )
        snapshot = adapter.inspect_terminal()
        self.assertEqual(snapshot.point_count, 0)
        self.assertEqual(snapshot.unexpected_candidate_collection_count, 0)
        self.assertEqual(snapshot.source_endpoint_count, 0)
        self.assertEqual(
            client.calls,
            [
                QdrantRequestId.OBSERVE_ROOT,
                QdrantRequestId.OBSERVE_COLLECTION,
                QdrantRequestId.OBSERVE_ALIAS,
                QdrantRequestId.OBSERVE_COLLECTIONS,
            ],
        )


class FactoryBoundaryTests(unittest.TestCase):
    def test_factory_binds_every_non_postgres_adapter_without_effects(self) -> None:
        spec = _bound_spec()
        artifacts = _artifacts()
        runner = FakeRunner()
        fs = FakeRootFilesystem()
        qdrant = FakeQdrantClient()
        images = _images(spec)
        prerequisites = SimpleNamespace(
            local_images=SimpleNamespace(
                postgres=SimpleNamespace(
                    image_id=images[0].image_id,
                    reference=images[0].reference,
                    repo_digest=images[0].repo_digest,
                ),
                qdrant=SimpleNamespace(
                    image_id=images[1].image_id,
                    reference=images[1].reference,
                    repo_digest=images[1].repo_digest,
                ),
            )
        )
        factory = ClosedLinuxPlatformAdapterFactory(
            runner=runner, filesystem=fs, qdrant_http=qdrant
        )
        adapters = factory(
            execution_id=EXECUTION_ID,
            attempt_id="attempt-1",
            execution_binding_sha256=BINDING_SHA256,
            resolved_store_spec=spec,
            artifacts=artifacts,
            prerequisites=prerequisites,
        )
        self.assertEqual(runner.calls, [])
        self.assertEqual(fs.calls, [])
        self.assertEqual(qdrant.calls, [])
        self.assertEqual(adapters.qdrant.bind, "127.0.0.1:6343")


if __name__ == "__main__":
    unittest.main()

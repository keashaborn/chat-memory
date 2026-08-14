from __future__ import annotations

import hashlib
import json
import os
import copy
import socket
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.governed_memory_install.host_boundary import (
    CommandResult,
    CommandRunner,
    DOCKER_BINARY,
    FIXED_ENVIRONMENT,
    HostBoundaryError,
    IMAGE_INSPECT_TEMPLATE,
    SUPERVISOR_LABEL_KEYS,
    SUPERVISOR_INSPECT_TEMPLATE_BY_LOGICAL,
    SUPERVISOR_NETWORK_NAME,
    SUPERVISOR_INSPECT_TEMPLATES,
    validate_argv,
)
from tools.governed_memory_install.image_preflight import (
    ImageExpectation,
    ImagePreflightError,
    inspect_local_images,
)
from tools.governed_memory_install.linux_plan import (
    ExecutionBinding,
    LinuxPlanError,
    bind_store_spec,
    build_store_create_plan,
    load_store_spec,
    validate_store_spec,
)
from tools.governed_memory_install.resource_identity import (
    ResourceIdentityError,
    resource_ledger_binding_sha256,
    load_ledger,
    parse_ledger_bytes,
)
from tools.governed_memory_install.store_supervisor import (
    ExactContainer,
    StoreSupervisor,
    StoreSupervisorError,
    canonical_labels_sha256,
    exact_containers_from_files,
)
from tests.memory.resource_identity_test_support import (
    append_resource_identity,
    resource_identity_ledger_bytes,
)


ROOT = Path(__file__).resolve().parents[2]
INSTALLATION = ROOT / "ops" / "governed_memory" / "installation"
STORE_SPEC = INSTALLATION / "store_spec-v6.json"
STORE_UNIT = INSTALLATION / "systemd" / "governed-memory-stores-v6.service.in"
BINDING = "a" * 64
NONCE_SHA256 = "b" * 64
EXECUTION_ID = "c" * 64
PACKAGE_MANIFEST_SHA256 = "d" * 64
POSTGRES_ID = "1" * 64
QDRANT_ID = "2" * 64
POSTGRES_IMAGE_ID = "sha256:" + "3" * 64
QDRANT_IMAGE_ID = "sha256:" + "4" * 64
NETWORK_ID = "network-id-9a54cf123493-000006"


class _QueueRunner:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]) -> CommandResult:
        self.calls.append(tuple(argv))
        if not self.outputs:
            raise AssertionError("unexpected fake runner call")
        return CommandResult(tuple(argv), 0, json.dumps(self.outputs.pop(0)), "")


class _SupervisorRunner:
    def __init__(self, containers: tuple[ExactContainer, ExactContainer]) -> None:
        self.containers = {item.container_id: item for item in containers}
        self.running = {item.container_id: False for item in containers}
        self.networks = {
            item.container_id: item.network_name for item in containers
        }
        self.commands = {
            item.container_id: list(item.command) if item.command else None
            for item in containers
        }
        self.restart_names = {
            item.container_id: item.restart for item in containers
        }
        self.cap_drops = {
            item.container_id: list(item.cap_drop) for item in containers
        }
        self.mount_destinations = {
            item.container_id: item.volume_target for item in containers
        }
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]) -> CommandResult:
        exact = tuple(argv)
        self.calls.append(exact)
        operation = exact[1]
        if operation == "container":
            self.assert_inspect_shape(exact)
            container_id = exact[-1]
            container = self.containers[container_id]
            port_binding = [
                {
                    "HostIp": container.host_ip,
                    "HostPort": str(container.host_port),
                }
            ]
            value = [
                container_id,
                "/" + container.name,
                container.image_id,
                container.image_reference,
                self.commands[container_id],
                1,
                "NONE",
                len(container.labels),
                *(container.labels.get(key) for key in SUPERVISOR_LABEL_KEYS),
                self.restart_names[container_id],
                0,
                container.pids_limit,
                list(container.cap_add) or None,
                self.cap_drops[container_id] or None,
                list(container.security_opt),
                False,
                container.network_name,
                1,
                port_binding,
                None,
                container.log_driver,
                len(container.log_options),
                container.log_options["max-file"],
                container.log_options["max-size"],
                len(container.tmpfs),
                container.tmpfs["/tmp"],
                1,
                "volume",
                container.volume_name,
                self.mount_destinations[container_id],
                True,
                1,
                port_binding,
                None,
                1,
                (
                    container.network_id
                    if self.networks[container_id] == SUPERVISOR_NETWORK_NAME
                    else None
                ),
                self.running[container_id],
                "running" if self.running[container_id] else "exited",
                False,
                False,
                False,
                False,
            ]
            return CommandResult(exact, 0, json.dumps(value), "")
        if operation == "start":
            container_id = exact[-1]
            if container_id not in self.containers:
                raise AssertionError("non-recorded container id")
            self.running[container_id] = True
            return CommandResult(exact, 0, container_id + "\n", "")
        if operation == "stop":
            container_id = exact[-1]
            if container_id not in self.containers:
                raise AssertionError("non-recorded container id")
            self.running[container_id] = False
            return CommandResult(exact, 0, container_id + "\n", "")
        raise AssertionError("supervisor attempted forbidden Docker operation")

    @staticmethod
    def assert_inspect_shape(argv: tuple[str, ...]) -> None:
        if (
            argv[1:4] != ("container", "inspect", "--format")
            or argv[4] not in SUPERVISOR_INSPECT_TEMPLATES
        ):
            raise AssertionError("unexpected inspect argv")


class DormantStoreInstallStorePackageTests(unittest.TestCase):
    def _bound_spec(self) -> dict[str, object]:
        static = load_store_spec(STORE_SPEC)
        return bind_store_spec(
            static,
            ExecutionBinding(
                binding_sha256=BINDING,
                authorization_id="dormant_store_install-auth-0001",
                authorization_nonce_sha256=NONCE_SHA256,
                execution_id=EXECUTION_ID,
                package_manifest_sha256=PACKAGE_MANIFEST_SHA256,
            ),
        )

    def _containers(self) -> tuple[ExactContainer, ExactContainer]:
        spec = self._bound_spec()
        items = spec["resources"]["containers"]
        result = []
        for logical_name, container_id, image_id in (
            ("postgres", POSTGRES_ID, POSTGRES_IMAGE_ID),
            ("qdrant", QDRANT_ID, QDRANT_IMAGE_ID),
        ):
            item = items[logical_name]
            labels = item["labels"]
            result.append(
                ExactContainer(
                    logical_name=logical_name,
                    name=item["name"],
                    container_id=container_id,
                    image_id=image_id,
                    image_reference=item["image"]["reference"],
                    image_repo_digest=item["image"]["repo_digest"],
                    labels=labels,
                    labels_sha256=canonical_labels_sha256(labels),
                    command=tuple(item["command"]),
                    environment_required_keys=frozenset(
                        item["environment_contract"]["required_keys"]
                    ),
                    environment_secret_keys=frozenset(
                        item["environment_contract"]["secret_keys"]
                    ),
                    environment_fixed_values=dict(
                        item["environment_contract"]["fixed_values"]
                    ),
                    environment_forbidden_keys=frozenset(
                        item["environment_contract"]["forbidden_keys"]
                    ),
                    network_name=spec["resources"]["network"]["name"],
                    network_id=NETWORK_ID,
                    host_ip=item["publish"]["host"],
                    host_port=item["publish"]["host_port"],
                    container_port=item["publish"]["container_port"],
                    volume_name=item["volume_name"],
                    volume_target=item["volume_target"],
                    restart=item["restart"],
                    pids_limit=item["pids_limit"],
                    cap_add=tuple(item["security"]["cap_add"]),
                    cap_drop=tuple(item["security"]["cap_drop"]),
                    security_opt=("no-new-privileges",),
                    log_driver=item["logging"]["driver"],
                    log_options={
                        "max-file": str(item["logging"]["max_file"]),
                        "max-size": item["logging"]["max_size"],
                    },
                    tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=64m"},
                )
            )
        return result[0], result[1]

    def test_host_boundary_rejects_shell_image_acquisition_and_endpoints(self) -> None:
        with self.assertRaisesRegex(HostBoundaryError, "not_absolute"):
            validate_argv(("docker", "image", "inspect", "x"))
        for forbidden in ("pull", "build", "load", "import"):
            with self.subTest(forbidden=forbidden):
                with self.assertRaisesRegex(HostBoundaryError, "operation_forbidden"):
                    validate_argv((DOCKER_BINARY, forbidden, "anything"))
        for endpoint in (
            "https://api.openai.com/v1",
            "postgresql://source-postgres/database",
            "project.supabase.co",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(HostBoundaryError, "endpoint_forbidden"):
                    validate_argv((DOCKER_BINARY, "inspect", endpoint))

    @mock.patch("tools.governed_memory_install.host_boundary.subprocess.Popen")
    def test_command_runner_has_fixed_bounds_and_no_shell(self, popen: mock.Mock) -> None:
        def fake_process(stdout: bytes) -> mock.Mock:
            stdout_read, stdout_write = socket.socketpair()
            stderr_read, stderr_write = socket.socketpair()
            stdout_write.sendall(stdout)
            stdout_write.close()
            stderr_write.close()
            return mock.Mock(
                stdout=stdout_read,
                stderr=stderr_read,
                poll=mock.Mock(return_value=0),
                wait=mock.Mock(return_value=0),
            )

        first_process = fake_process(b"ok\n")
        popen.return_value = first_process
        reference = "postgres:16-alpine@sha256:" + "5" * 64
        result = CommandRunner().run(
            (
                DOCKER_BINARY,
                "image",
                "inspect",
                "--format",
                IMAGE_INSPECT_TEMPLATE,
                reference,
            )
        )
        self.assertEqual(result.stdout, "ok\n")
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["env"], FIXED_ENVIRONMENT)
        self.assertEqual(kwargs["cwd"], "/")
        self.assertEqual(CommandRunner()._timeout_seconds, 20)
        self.assertEqual(first_process.stdout.fileno(), -1)
        self.assertEqual(first_process.stderr.fileno(), -1)
        second_process = fake_process(b"x" * 17)
        popen.return_value = second_process
        with self.assertRaisesRegex(HostBoundaryError, "output_limit"):
            CommandRunner(max_output_bytes=16).run(
                (
                    DOCKER_BINARY,
                    "image",
                    "inspect",
                    "--format",
                    IMAGE_INSPECT_TEMPLATE,
                    reference,
                )
            )
        self.assertEqual(second_process.stdout.fileno(), -1)
        self.assertEqual(second_process.stderr.fileno(), -1)

    def test_image_preflight_is_inspect_only_and_requires_exact_local_identity(self) -> None:
        digest_one = "sha256:" + "5" * 64
        digest_two = "sha256:" + "6" * 64
        expectations = (
            ImageExpectation(
                "postgres",
                "postgres:16-alpine@" + digest_one,
                "postgres@" + digest_one,
            ),
            ImageExpectation(
                "qdrant",
                "qdrant/qdrant:v1.19.0@" + digest_two,
                "qdrant/qdrant@" + digest_two,
            ),
        )
        runner = _QueueRunner(
            [
                [
                    POSTGRES_IMAGE_ID,
                    ["postgres@" + digest_one],
                    "linux",
                    "amd64",
                ],
                [
                    QDRANT_IMAGE_ID,
                    ["qdrant/qdrant@" + digest_two],
                    "linux",
                    "amd64",
                ],
            ]
        )
        identities = inspect_local_images(runner, expectations)
        self.assertEqual(tuple(item.image_id for item in identities), (
            POSTGRES_IMAGE_ID,
            QDRANT_IMAGE_ID,
        ))
        self.assertEqual(len(runner.calls), 2)
        for call in runner.calls:
            self.assertEqual(call[0:3], (DOCKER_BINARY, "image", "inspect"))
            self.assertNotIn("pull", call)

        wrong = _QueueRunner(
            [[
                POSTGRES_IMAGE_ID,
                ["postgres@sha256:" + "7" * 64],
                "linux",
                "amd64",
            ]]
        )
        with self.assertRaisesRegex(ImagePreflightError, "exact_repo_digest_absent"):
            inspect_local_images(wrong, expectations[:1])

    def test_static_store_spec_and_pure_plan_are_exact_and_stores_only(self) -> None:
        spec = self._bound_spec()
        self.assertEqual(spec["state"], "canonical-dormant-store-spec-v2")
        plan = build_store_create_plan(spec)
        self.assertEqual(len(plan), 5)
        self.assertEqual(len({step.step_id for step in plan}), 5)
        rendered = json.dumps(spec, sort_keys=True) + "\n" + "\n".join(
            " ".join(step.argv) for step in plan
        )
        for forbidden in (
            "openai",
            "supabase",
            "source-postgres",
            "governed-memory-http.service",
            "governed-memory-worker.service",
            "http://",
            "https://",
        ):
            self.assertNotIn(forbidden, rendered.lower())
        self.assertNotIn("${", rendered)
        self.assertEqual(
            spec["candidate_id"], "governed_memory_9a54cf123493_000006"
        )
        self.assertEqual(
            spec["docker_resource_slug"], "governed-memory-9a54cf123493-000006"
        )
        for step in plan:
            self.assertEqual(step.argv[0], DOCKER_BINARY)
            self.assertFalse(any(any(char in arg for char in "*?[") for arg in step.argv))
            if step.argv[1] == "create":
                self.assertIn("--pull=never", step.argv)
                for exact in (
                    "--restart=no",
                    "--pids-limit=256",
                    "--security-opt=no-new-privileges",
                    "--cap-drop=ALL",
                    "--log-driver=json-file",
                    "--log-opt=max-size=10m",
                    "--log-opt=max-file=3",
                    "--no-healthcheck",
                ):
                    self.assertIn(exact, step.argv)
        container_names = {
            spec["resources"]["containers"][name]["name"]
            for name in ("postgres", "qdrant")
        }
        planned_names = {
            step.argv[step.argv.index("--name") + 1]
            for step in plan
            if "--name" in step.argv
        }
        self.assertEqual(planned_names, container_names)

        containers = spec["resources"]["containers"]
        postgres = containers["postgres"]
        qdrant = containers["qdrant"]
        self.assertNotEqual(postgres["environment_file"], qdrant["environment_file"])
        self.assertEqual(
            postgres["environment_contract"],
            {
                "fixed_values": {
                    "POSTGRES_DB": "postgres",
                    "POSTGRES_USER": "governed_memory_bootstrap",
                },
                "forbidden_keys": ["QDRANT__SERVICE__API_KEY"],
                "required_keys": [
                    "POSTGRES_DB",
                    "POSTGRES_PASSWORD",
                    "POSTGRES_USER",
                ],
                "secret_keys": ["POSTGRES_PASSWORD"],
            },
        )
        self.assertEqual(
            qdrant["environment_contract"],
            {
                "fixed_values": {},
                "forbidden_keys": [
                    "POSTGRES_DB",
                    "POSTGRES_PASSWORD",
                    "POSTGRES_USER",
                ],
                "required_keys": ["QDRANT__SERVICE__API_KEY"],
                "secret_keys": ["QDRANT__SERVICE__API_KEY"],
            },
        )
        self.assertEqual(
            {
                item["labels"]["lifeswitch.governed-memory.candidate"]
                for item in (
                    spec["resources"]["network"],
                    spec["resources"]["volumes"]["postgres"],
                    spec["resources"]["volumes"]["qdrant"],
                    postgres,
                    qdrant,
                )
            },
            {spec["candidate_id"]},
        )
        self.assertEqual(
            {
                item["labels"][
                    "lifeswitch.governed-memory.package-generation"
                ]
                for item in (
                    spec["resources"]["network"],
                    spec["resources"]["volumes"]["postgres"],
                    spec["resources"]["volumes"]["qdrant"],
                    postgres,
                    qdrant,
                )
            },
            {"dormant-store-install-v2"},
        )
        self.assertNotIn("lifeswitch.governed-memory.phase", rendered)
        self.assertNotIn('"8B"', rendered)
        for item in (postgres, qdrant):
            self.assertEqual(
                item["healthcheck"],
                {
                    "container_healthcheck": "disabled",
                    "external_readiness_probe": (
                        "closed_fixed_loopback_readiness_probe_v1"
                    ),
                },
            )

    def test_store_spec_refuses_identity_environment_or_hardening_drift(self) -> None:
        static = load_store_spec(STORE_SPEC)
        cases: list[dict[str, object]] = []
        wrong_candidate = copy.deepcopy(static)
        wrong_candidate["candidate_id"] = "governed-memory-9a54cf123493-000006"
        cases.append(wrong_candidate)
        shared_environment = copy.deepcopy(static)
        shared_environment["resources"]["containers"]["qdrant"][
            "environment_file"
        ] = shared_environment["resources"]["containers"]["postgres"][
            "environment_file"
        ]
        cases.append(shared_environment)
        cross_store_key = copy.deepcopy(static)
        cross_store_key["resources"]["containers"]["qdrant"][
            "environment_contract"
        ]["required_keys"].append("POSTGRES_PASSWORD")
        cases.append(cross_store_key)
        restart_drift = copy.deepcopy(static)
        restart_drift["resources"]["containers"]["postgres"]["restart"] = "always"
        cases.append(restart_drift)
        for candidate in cases:
            with self.subTest(candidate=candidate["candidate_id"]):
                with self.assertRaises(LinuxPlanError):
                    validate_store_spec(candidate)

    def test_identity_ledger_is_closed_canonical_and_hash_chained(self) -> None:
        ownership_sha256 = "c" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identities.jsonl"
            first = append_resource_identity(
                path,
                binding_sha256=BINDING,
                event="created",
                resource_kind="container",
                resource_name="governed-memory-postgres-000001",
                resource_id=POSTGRES_ID,
                image_id=POSTGRES_IMAGE_ID,
                image_repo_digest="postgres@sha256:" + "5" * 64,
                ownership_sha256=ownership_sha256,
                resource_labels_sha256="d" * 64,
            )
            second = append_resource_identity(
                path,
                binding_sha256=BINDING,
                event="started",
                resource_kind="container",
                resource_name="governed-memory-postgres-000001",
                resource_id=POSTGRES_ID,
                image_id=POSTGRES_IMAGE_ID,
                image_repo_digest="postgres@sha256:" + "5" * 64,
                ownership_sha256=ownership_sha256,
                resource_labels_sha256="d" * 64,
            )
            self.assertEqual(second.previous_entry_sha256, first.entry_sha256)
            self.assertEqual(load_ledger(path, expected_binding_sha256=BINDING), (
                first,
                second,
            ))
            lines = path.read_bytes().splitlines()
            tampered = json.loads(lines[1])
            tampered["resource_id"] = QDRANT_ID
            lines[1] = json.dumps(
                tampered, sort_keys=True, separators=(",", ":")
            ).encode("ascii")
            with self.assertRaisesRegex(ResourceIdentityError, "hash_mismatch"):
                parse_ledger_bytes(
                    b"\n".join(lines) + b"\n",
                    expected_binding_sha256=BINDING,
                )
            with self.assertRaisesRegex(ResourceIdentityError, "forbidden_content"):
                parse_ledger_bytes(
                    resource_identity_ledger_bytes(
                        (
                            {
                                "event": "observed",
                                "resource_kind": "volume",
                                "resource_name": "password-cache",
                                "resource_id": "volume-0001",
                                "ownership_sha256": ownership_sha256,
                                "resource_labels_sha256": "d" * 64,
                            },
                        ),
                        binding_sha256=BINDING,
                    ),
                    expected_binding_sha256=BINDING,
                )

    def test_supervisor_uses_only_two_recorded_ids_and_three_operations(self) -> None:
        containers = self._containers()
        runner = _SupervisorRunner(containers)
        supervisor = StoreSupervisor(runner=runner, containers=containers)
        started = supervisor.start()
        self.assertTrue(all(state.running for state in started))
        stopped = supervisor.stop()
        self.assertFalse(any(state.running for state in stopped))
        allowed = {"container", "start", "stop"}
        self.assertTrue(all(call[1] in allowed for call in runner.calls))
        exact_ids = {POSTGRES_ID, QDRANT_ID}
        self.assertTrue(all(call[-1] in exact_ids for call in runner.calls))
        rendered = "\n".join(" ".join(call) for call in runner.calls)
        for forbidden in (" create ", " pull ", " remove ", " rm "):
            self.assertNotIn(forbidden, " " + rendered + " ")

    def test_supervisor_refuses_observed_name_or_label_drift(self) -> None:
        containers = self._containers()
        runner = _SupervisorRunner(containers)
        runner.containers[POSTGRES_ID] = ExactContainer(
            logical_name="postgres",
            name="wrong-name",
            container_id=POSTGRES_ID,
            image_id=POSTGRES_IMAGE_ID,
            image_reference=containers[0].image_reference,
            image_repo_digest=containers[0].image_repo_digest,
            labels=containers[0].labels,
            labels_sha256=containers[0].labels_sha256,
            command=containers[0].command,
            environment_required_keys=containers[0].environment_required_keys,
            environment_secret_keys=containers[0].environment_secret_keys,
            environment_fixed_values=containers[0].environment_fixed_values,
            environment_forbidden_keys=containers[0].environment_forbidden_keys,
            network_name=containers[0].network_name,
            network_id=containers[0].network_id,
            host_ip=containers[0].host_ip,
            host_port=containers[0].host_port,
            container_port=containers[0].container_port,
            volume_name=containers[0].volume_name,
            volume_target=containers[0].volume_target,
            restart=containers[0].restart,
            pids_limit=containers[0].pids_limit,
            cap_add=containers[0].cap_add,
            cap_drop=containers[0].cap_drop,
            security_opt=containers[0].security_opt,
            log_driver=containers[0].log_driver,
            log_options=containers[0].log_options,
            tmpfs=containers[0].tmpfs,
        )
        supervisor = StoreSupervisor(runner=runner, containers=containers)
        with self.assertRaisesRegex(StoreSupervisorError, "name_drift"):
            supervisor.inspect()

    def test_supervisor_refuses_network_drift_without_broad_config_reads(self) -> None:
        containers = self._containers()
        runner = _SupervisorRunner(containers)
        runner.networks[POSTGRES_ID] = "unrelated-network"
        with self.assertRaisesRegex(StoreSupervisorError, "network_drift"):
            StoreSupervisor(runner=runner, containers=containers).inspect()

        runner = _SupervisorRunner(containers)
        StoreSupervisor(runner=runner, containers=containers).inspect()
        self.assertTrue(runner.calls)
        self.assertFalse(
            any("Env" in argument for call in runner.calls for argument in call)
        )
        rendered = "\n".join(" ".join(call) for call in runner.calls)
        for broad in (
            "{{json .HostConfig}}",
            "{{json .Mounts}}",
            "{{json .NetworkSettings}}",
            "{{json .State}}",
            "{{json .Config.Labels}}",
            "{{json .Config.Env}}",
        ):
            self.assertNotIn(broad, rendered)

    def test_supervisor_refuses_nonsecret_container_hardening_drift(self) -> None:
        containers = self._containers()
        cases = (
            ("command", lambda runner: runner.commands.__setitem__(
                POSTGRES_ID, ["sh", "-c", "malicious"]
            )),
            ("restart", lambda runner: runner.restart_names.__setitem__(
                POSTGRES_ID, "always"
            )),
            ("cap_drop", lambda runner: runner.cap_drops.__setitem__(
                POSTGRES_ID, []
            )),
            ("mount", lambda runner: runner.mount_destinations.__setitem__(
                POSTGRES_ID, "/tmp/changed"
            )),
        )
        for label, mutate in cases:
            runner = _SupervisorRunner(containers)
            mutate(runner)
            with self.subTest(label=label), self.assertRaises(
                StoreSupervisorError
            ):
                StoreSupervisor(
                    runner=runner, containers=containers
                ).inspect()

    def test_resolved_spec_and_ledger_bind_supervisor_identity(self) -> None:
        spec = self._bound_spec()
        containers = spec["resources"]["containers"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            spec_path = root / "store_spec-v6.json"
            ledger_path = root / "identities.jsonl"
            spec_path.write_text(
                json.dumps(spec, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="ascii",
            )
            spec_path.chmod(0o600)
            ledger_binding = resource_ledger_binding_sha256(BINDING)
            network = spec["resources"]["network"]
            append_resource_identity(
                ledger_path,
                binding_sha256=ledger_binding,
                event="created",
                resource_kind="network",
                resource_name=network["name"],
                resource_id=NETWORK_ID,
                ownership_sha256=hashlib.sha256(
                    b"network:owned"
                ).hexdigest(),
                resource_labels_sha256=canonical_labels_sha256(
                    network["labels"]
                ),
            )
            for logical_name, container_id, image_id in (
                ("postgres", POSTGRES_ID, POSTGRES_IMAGE_ID),
                ("qdrant", QDRANT_ID, QDRANT_IMAGE_ID),
            ):
                item = containers[logical_name]
                append_resource_identity(
                    ledger_path,
                    binding_sha256=ledger_binding,
                    event="created",
                    resource_kind="container",
                    resource_name=item["name"],
                    resource_id=container_id,
                    image_id=image_id,
                    image_repo_digest=item["image"]["repo_digest"],
                    ownership_sha256=hashlib.sha256(
                        (logical_name + ":owned").encode("ascii")
                    ).hexdigest(),
                    resource_labels_sha256=canonical_labels_sha256(
                        item["labels"]
                    ),
                )
            loaded = exact_containers_from_files(
                spec_path=spec_path,
                ledger_path=ledger_path,
                expected_uid=os.geteuid(),
            )
            self.assertEqual(tuple(item.container_id for item in loaded), (
                POSTGRES_ID,
                QDRANT_ID,
            ))

    def test_store_unit_is_root_oneshot_unix_only_without_app_or_secret_loading(self) -> None:
        unit = STORE_UNIT.read_text(encoding="ascii")
        for required in (
            "Type=oneshot",
            "RemainAfterExit=yes",
            "User=root",
            "Group=root",
            "RestrictAddressFamilies=AF_UNIX",
            "IPAddressDeny=any",
            "ExecStart=/opt/governed-memory-controller/runtimes/@RUNTIME_RECEIPT_SHA256@/bin/python -I -B /opt/governed-memory-controller/releases/@PACKAGE_MANIFEST_SHA256@/tools/governed_memory_install/store_supervisor_launcher.py --package-manifest-sha256 @PACKAGE_MANIFEST_SHA256@ start",
            "ExecStop=/opt/governed-memory-controller/runtimes/@RUNTIME_RECEIPT_SHA256@/bin/python -I -B /opt/governed-memory-controller/releases/@PACKAGE_MANIFEST_SHA256@/tools/governed_memory_install/store_supervisor_launcher.py --package-manifest-sha256 @PACKAGE_MANIFEST_SHA256@ stop",
            "WorkingDirectory=/opt/governed-memory-controller/releases/@PACKAGE_MANIFEST_SHA256@",
            "executions-v6/@EXECUTION_ID@/resources.jsonl",
            "ConditionPathExists=/opt/governed-memory-controller/runtimes/@RUNTIME_RECEIPT_SHA256@/bin/python",
        ):
            self.assertIn(required, unit)
        for forbidden in (
            "EnvironmentFile",
            "governed-memory-http",
            "governed-memory-worker",
            "OPENAI",
            "SUPABASE",
            "AF_INET",
            "/var/lib/governed-memory-controller/resource-identities.jsonl",
            "${PACKAGE_MANIFEST_SHA256}",
            "/usr/bin/python3.12",
            " -m tools.governed_memory_install.store_supervisor",
        ):
            self.assertNotIn(forbidden, unit)


if __name__ == "__main__":
    unittest.main()

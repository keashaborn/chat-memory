from __future__ import annotations

"""Exact-ID supervisor for two previously created dormant store containers.

The supervisor is intentionally not an installer.  Its complete Docker surface
is container inspect, start, and stop against identities already sealed in the
resource ledger.
"""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from .host_boundary import CommandResult, CommandRunner, DOCKER_BINARY
from .linux_plan import canonical_labels_sha256, load_store_spec
from .resource_identity import (
    load_ledger,
    latest_exact_resources,
    resource_ledger_binding_sha256,
)


class StoreSupervisorError(RuntimeError):
    """Content-free refusal for identity drift or an invalid transition."""


class Runner(Protocol):
    def run(self, argv: Sequence[str]) -> CommandResult:
        ...


@dataclass(frozen=True, slots=True)
class ExactContainer:
    logical_name: str
    name: str
    container_id: str
    image_id: str
    image_repo_digest: str
    labels: Mapping[str, str]
    labels_sha256: str
    command: tuple[str, ...]
    environment_required_keys: frozenset[str]
    environment_secret_keys: frozenset[str]
    environment_fixed_values: Mapping[str, str]
    environment_forbidden_keys: frozenset[str]
    network_name: str
    host_ip: str
    host_port: int
    container_port: int
    volume_name: str
    volume_target: str
    restart: str
    pids_limit: int
    cap_add: tuple[str, ...]
    cap_drop: tuple[str, ...]
    security_opt: tuple[str, ...]
    log_driver: str
    log_options: Mapping[str, str]
    tmpfs: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ContainerState:
    logical_name: str
    container_id: str
    running: bool
    status: str


def exact_containers_from_files(
    *, spec_path: Path, ledger_path: Path, expected_uid: int = 0
) -> tuple[ExactContainer, ExactContainer]:
    spec = load_store_spec(
        spec_path,
        allow_placeholders=False,
        secure_root_file=True,
        secure_expected_uid=expected_uid,
    )
    execution = spec["execution_binding"]
    binding_sha256 = resource_ledger_binding_sha256(
        execution["binding_sha256"]
    )
    records = load_ledger(
        ledger_path,
        expected_binding_sha256=binding_sha256,
        expected_uid=expected_uid,
    )
    latest = latest_exact_resources(records)
    containers = spec["resources"]["containers"]
    network_name = spec["resources"]["network"]["name"]
    exact: list[ExactContainer] = []
    for logical_name in ("postgres", "qdrant"):
        expected = containers[logical_name]
        name = expected["name"]
        record = latest.get(("container", name))
        if record is None:
            raise StoreSupervisorError("container_identity_absent")
        if record.event == "removed":
            raise StoreSupervisorError("container_identity_removed")
        labels = expected["labels"]
        environment = expected["environment_contract"]
        publish = expected["publish"]
        security = expected["security"]
        logging = expected["logging"]
        labels_sha256 = canonical_labels_sha256(labels)
        if record.resource_labels_sha256 != labels_sha256:
            raise StoreSupervisorError("container_labels_identity_mismatch")
        if record.image_id is None or record.image_repo_digest is None:
            raise StoreSupervisorError("container_image_identity_absent")
        if record.image_repo_digest != expected["image"]["repo_digest"]:
            raise StoreSupervisorError("container_repo_digest_mismatch")
        exact.append(
            ExactContainer(
                logical_name=logical_name,
                name=name,
                container_id=record.resource_id,
                image_id=record.image_id,
                image_repo_digest=record.image_repo_digest,
                labels=labels,
                labels_sha256=labels_sha256,
                command=tuple(expected["command"]),
                environment_required_keys=frozenset(environment["required_keys"]),
                environment_secret_keys=frozenset(environment["secret_keys"]),
                environment_fixed_values=dict(environment["fixed_values"]),
                environment_forbidden_keys=frozenset(environment["forbidden_keys"]),
                network_name=network_name,
                host_ip=publish["host"],
                host_port=publish["host_port"],
                container_port=publish["container_port"],
                volume_name=expected["volume_name"],
                volume_target=expected["volume_target"],
                restart=expected["restart"],
                pids_limit=expected["pids_limit"],
                cap_add=tuple(security["cap_add"]),
                cap_drop=tuple(security["cap_drop"]),
                security_opt=("no-new-privileges",),
                log_driver=logging["driver"],
                log_options={
                    "max-file": str(logging["max_file"]),
                    "max-size": logging["max_size"],
                },
                tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=64m"},
            )
        )
    if exact[0].container_id == exact[1].container_id:
        raise StoreSupervisorError("container_id_collision")
    return exact[0], exact[1]


class StoreSupervisor:
    def __init__(
        self,
        *,
        runner: Runner,
        containers: tuple[ExactContainer, ExactContainer],
    ) -> None:
        if tuple(item.logical_name for item in containers) != (
            "postgres",
            "qdrant",
        ):
            raise StoreSupervisorError("supervisor_container_set_invalid")
        if len({item.container_id for item in containers}) != 2:
            raise StoreSupervisorError("supervisor_container_ids_not_distinct")
        self._runner = runner
        self._containers = containers

    def _inspect(self, expected: ExactContainer) -> ContainerState:
        result = self._runner.run(
            (
                DOCKER_BINARY,
                "container",
                "inspect",
                "--format",
                "{{json .}}",
                expected.container_id,
            )
        )
        try:
            observed = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as error:
            raise StoreSupervisorError("container_inspect_json_invalid") from error
        if type(observed) is not dict:
            raise StoreSupervisorError("container_inspect_shape_invalid")
        if observed.get("Id") != expected.container_id:
            raise StoreSupervisorError("container_id_drift")
        if observed.get("Name") != "/" + expected.name:
            raise StoreSupervisorError("container_name_drift")
        if observed.get("Image") != expected.image_id:
            raise StoreSupervisorError("container_image_drift")
        config = observed.get("Config")
        host_config = observed.get("HostConfig")
        mounts = observed.get("Mounts")
        network_settings = observed.get("NetworkSettings")
        state = observed.get("State")
        if type(config) is not dict or config.get("Labels") != dict(expected.labels):
            raise StoreSupervisorError("container_labels_drift")
        observed_command = config.get("Cmd")
        if expected.command:
            command_matches = observed_command == list(expected.command)
        else:
            command_matches = observed_command in (None, [])
        if not command_matches:
            raise StoreSupervisorError("container_command_drift")
        self._verify_environment(expected, config.get("Env"))
        if config.get("Healthcheck") != {"Test": ["NONE"]}:
            raise StoreSupervisorError("container_healthcheck_drift")
        self._verify_host_config(expected, host_config)
        self._verify_mount(expected, mounts)
        self._verify_network(expected, network_settings)
        if (
            type(state) is not dict
            or type(state.get("Running")) is not bool
            or type(state.get("Status")) is not str
        ):
            raise StoreSupervisorError("container_state_invalid")
        return ContainerState(
            logical_name=expected.logical_name,
            container_id=expected.container_id,
            running=state["Running"],
            status=state["Status"],
        )

    @staticmethod
    def _verify_environment(expected: ExactContainer, value: object) -> None:
        if type(value) is not list or any(type(item) is not str for item in value):
            raise StoreSupervisorError("container_environment_invalid")
        observed: dict[str, str] = {}
        for item in value:
            key, separator, setting = item.partition("=")
            if not separator or not key or key in observed:
                raise StoreSupervisorError("container_environment_invalid")
            observed[key] = setting
        observed_keys = frozenset(observed)
        if not expected.environment_required_keys.issubset(observed_keys):
            raise StoreSupervisorError("container_environment_required_key_drift")
        if expected.environment_forbidden_keys & observed_keys:
            raise StoreSupervisorError("container_environment_forbidden_key_drift")
        if any(
            observed.get(key) != setting
            for key, setting in expected.environment_fixed_values.items()
        ):
            raise StoreSupervisorError("container_environment_fixed_value_drift")
        if any(not observed.get(key) for key in expected.environment_secret_keys):
            raise StoreSupervisorError("container_environment_secret_absent")

    @staticmethod
    def _verify_host_config(expected: ExactContainer, value: object) -> None:
        if type(value) is not dict:
            raise StoreSupervisorError("container_host_config_invalid")
        restart = value.get("RestartPolicy")
        log_config = value.get("LogConfig")
        port_bindings = value.get("PortBindings")
        expected_binding = {
            f"{expected.container_port}/tcp": [
                {"HostIp": expected.host_ip, "HostPort": str(expected.host_port)}
            ]
        }
        if type(restart) is not dict or restart.get("Name") != expected.restart:
            raise StoreSupervisorError("container_restart_drift")
        if value.get("PidsLimit") != expected.pids_limit:
            raise StoreSupervisorError("container_pids_limit_drift")
        if tuple(value.get("CapAdd") or ()) != expected.cap_add:
            raise StoreSupervisorError("container_cap_add_drift")
        if tuple(value.get("CapDrop") or ()) != expected.cap_drop:
            raise StoreSupervisorError("container_cap_drop_drift")
        if tuple(value.get("SecurityOpt") or ()) != expected.security_opt:
            raise StoreSupervisorError("container_security_opt_drift")
        if value.get("Tmpfs") != dict(expected.tmpfs):
            raise StoreSupervisorError("container_tmpfs_drift")
        if (
            type(log_config) is not dict
            or log_config.get("Type") != expected.log_driver
            or log_config.get("Config") != dict(expected.log_options)
        ):
            raise StoreSupervisorError("container_logging_drift")
        if port_bindings != expected_binding:
            raise StoreSupervisorError("container_port_binding_drift")
        if value.get("NetworkMode") != expected.network_name:
            raise StoreSupervisorError("container_network_mode_drift")

    @staticmethod
    def _verify_mount(expected: ExactContainer, value: object) -> None:
        if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
            raise StoreSupervisorError("container_mount_drift")
        mount = value[0]
        if (
            mount.get("Type") != "volume"
            or mount.get("Name") != expected.volume_name
            or mount.get("Destination") != expected.volume_target
            or mount.get("RW") is not True
        ):
            raise StoreSupervisorError("container_mount_drift")

    @staticmethod
    def _verify_network(expected: ExactContainer, value: object) -> None:
        if type(value) is not dict:
            raise StoreSupervisorError("container_network_drift")
        networks = value.get("Networks")
        if type(networks) is not dict or set(networks) != {expected.network_name}:
            raise StoreSupervisorError("container_network_drift")

    def inspect(self) -> tuple[ContainerState, ContainerState]:
        return tuple(self._inspect(item) for item in self._containers)  # type: ignore[return-value]

    def start(self) -> tuple[ContainerState, ContainerState]:
        final: list[ContainerState] = []
        for container in self._containers:
            before = self._inspect(container)
            if not before.running:
                self._runner.run((DOCKER_BINARY, "start", container.container_id))
            after = self._inspect(container)
            if not after.running:
                raise StoreSupervisorError("container_start_not_observed")
            final.append(after)
        return final[0], final[1]

    def stop(self) -> tuple[ContainerState, ContainerState]:
        final: dict[str, ContainerState] = {}
        for container in reversed(self._containers):
            before = self._inspect(container)
            if before.running:
                self._runner.run(
                    (DOCKER_BINARY, "stop", "--time=10", container.container_id)
                )
            after = self._inspect(container)
            if after.running:
                raise StoreSupervisorError("container_stop_not_observed")
            final[container.logical_name] = after
        return final["postgres"], final["qdrant"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("operation", choices=("inspect", "start", "stop"))
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.spec.is_absolute() or not arguments.ledger.is_absolute():
        raise StoreSupervisorError("supervisor_paths_not_absolute")
    containers = exact_containers_from_files(
        spec_path=arguments.spec, ledger_path=arguments.ledger
    )
    supervisor = StoreSupervisor(
        runner=CommandRunner(command_profile="store_supervisor"),
        containers=containers,
    )
    getattr(supervisor, arguments.operation)()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

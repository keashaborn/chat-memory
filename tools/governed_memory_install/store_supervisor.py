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

from .host_boundary import (
    CommandResult,
    CommandRunner,
    DOCKER_BINARY,
    SUPERVISOR_LABEL_KEYS,
    SUPERVISOR_INSPECT_TEMPLATE_BY_LOGICAL,
    SUPERVISOR_NETWORK_NAME,
)
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
    image_reference: str
    image_repo_digest: str
    labels: Mapping[str, str]
    labels_sha256: str
    command: tuple[str, ...]
    environment_required_keys: frozenset[str]
    environment_secret_keys: frozenset[str]
    environment_fixed_values: Mapping[str, str]
    environment_forbidden_keys: frozenset[str]
    network_name: str
    network_id: str
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
    network_spec = spec["resources"]["network"]
    network_name = network_spec["name"]
    network_record = latest.get(("network", network_name))
    if network_record is None:
        raise StoreSupervisorError("container_network_identity_absent")
    if network_record.event == "removed":
        raise StoreSupervisorError("container_network_identity_removed")
    if network_record.resource_labels_sha256 != canonical_labels_sha256(
        network_spec["labels"]
    ):
        raise StoreSupervisorError("container_network_labels_identity_mismatch")
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
                image_reference=expected["image"]["reference"],
                image_repo_digest=record.image_repo_digest,
                labels=labels,
                labels_sha256=labels_sha256,
                command=tuple(expected["command"]),
                environment_required_keys=frozenset(environment["required_keys"]),
                environment_secret_keys=frozenset(environment["secret_keys"]),
                environment_fixed_values=dict(environment["fixed_values"]),
                environment_forbidden_keys=frozenset(environment["forbidden_keys"]),
                network_name=network_name,
                network_id=network_record.resource_id,
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
        template = SUPERVISOR_INSPECT_TEMPLATE_BY_LOGICAL[
            expected.logical_name
        ]
        result = self._runner.run(
            (
                DOCKER_BINARY,
                "container",
                "inspect",
                "--format",
                template,
                expected.container_id,
            )
        )
        try:
            projected = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as error:
            raise StoreSupervisorError(
                "container_inspect_json_invalid"
            ) from error
        if type(projected) is not list:
            raise StoreSupervisorError("container_inspect_json_invalid")
        try:
            (
                container_id,
                name,
                image_id,
                image_reference,
                command,
                healthcheck_count,
                healthcheck_first,
                label_count,
                *tail,
            ) = projected
        except ValueError as error:
            raise StoreSupervisorError(
                "container_inspect_json_invalid"
            ) from error
        label_values = tail[: len(SUPERVISOR_LABEL_KEYS)]
        try:
            (
                restart_name,
                restart_maximum_retry_count,
                pids_limit,
                cap_add,
                cap_drop,
                security_opt,
                readonly_rootfs,
                network_mode,
                host_port_count,
                host_port_binding,
                unrelated_host_port_binding,
                log_driver,
                log_option_count,
                log_max_file,
                log_max_size,
                tmpfs_count,
                tmpfs_value,
                mount_count,
                mount_type,
                mount_name,
                mount_destination,
                mount_read_write,
                runtime_port_count,
                runtime_port_binding,
                unrelated_runtime_port_binding,
                network_count,
                network_id,
                running,
                status,
                paused,
                restarting,
                dead,
                oom_killed,
            ) = tail[len(SUPERVISOR_LABEL_KEYS) :]
        except ValueError as error:
            raise StoreSupervisorError(
                "container_inspect_json_invalid"
            ) from error
        if container_id != expected.container_id:
            raise StoreSupervisorError("container_id_drift")
        if name != "/" + expected.name:
            raise StoreSupervisorError("container_name_drift")
        if image_id != expected.image_id:
            raise StoreSupervisorError("container_image_drift")
        if image_reference != expected.image_reference:
            raise StoreSupervisorError("container_image_reference_drift")
        observed_command = tuple(command or ()) if type(command) in {list, type(None)} else None
        if observed_command != expected.command:
            raise StoreSupervisorError("container_command_drift")
        if (
            type(healthcheck_count) is not int
            or healthcheck_count != 1
            or healthcheck_first != "NONE"
        ):
            raise StoreSupervisorError("container_healthcheck_drift")
        expected_labels = dict(expected.labels)
        observed_labels = dict(zip(SUPERVISOR_LABEL_KEYS, label_values, strict=True))
        if (
            type(label_count) is not int
            or label_count != len(SUPERVISOR_LABEL_KEYS)
            or set(expected_labels) != set(SUPERVISOR_LABEL_KEYS)
            or observed_labels != expected_labels
        ):
            raise StoreSupervisorError("container_labels_drift")
        expected_port_binding = [
            {
                "HostIp": expected.host_ip,
                "HostPort": str(expected.host_port),
            }
        ]
        if (
            restart_name != expected.restart
            or type(restart_maximum_retry_count) is not int
            or restart_maximum_retry_count != 0
        ):
            raise StoreSupervisorError("container_restart_drift")
        if type(pids_limit) is not int or pids_limit != expected.pids_limit:
            raise StoreSupervisorError("container_pids_limit_drift")
        if (
            type(cap_add) not in {list, type(None)}
            or tuple(cap_add or ()) != expected.cap_add
        ):
            raise StoreSupervisorError("container_cap_add_drift")
        if (
            type(cap_drop) not in {list, type(None)}
            or tuple(cap_drop or ()) != expected.cap_drop
        ):
            raise StoreSupervisorError("container_cap_drop_drift")
        if (
            type(security_opt) not in {list, type(None)}
            or tuple(security_opt or ()) != expected.security_opt
            or readonly_rootfs is not False
        ):
            raise StoreSupervisorError("container_security_opt_drift")
        if (
            network_mode != expected.network_name
            or type(host_port_count) is not int
            or host_port_count != 1
            or host_port_binding != expected_port_binding
            or unrelated_host_port_binding is not None
        ):
            raise StoreSupervisorError("container_port_binding_drift")
        if (
            log_driver != expected.log_driver
            or type(log_option_count) is not int
            or log_option_count != len(expected.log_options)
            or {
                "max-file": log_max_file,
                "max-size": log_max_size,
            }
            != dict(expected.log_options)
        ):
            raise StoreSupervisorError("container_logging_drift")
        if (
            type(tmpfs_count) is not int
            or tmpfs_count != len(expected.tmpfs)
            or tmpfs_value != expected.tmpfs.get("/tmp")
        ):
            raise StoreSupervisorError("container_tmpfs_drift")
        if (
            type(mount_count) is not int
            or mount_count != 1
            or mount_type != "volume"
            or mount_name != expected.volume_name
            or mount_destination != expected.volume_target
            or mount_read_write is not True
        ):
            raise StoreSupervisorError("container_mount_drift")
        if (
            type(runtime_port_count) is not int
            or runtime_port_count != 1
            or runtime_port_binding != expected_port_binding
            or unrelated_runtime_port_binding is not None
        ):
            raise StoreSupervisorError("container_runtime_port_drift")
        if (
            expected.network_name != SUPERVISOR_NETWORK_NAME
            or type(network_count) is not int
            or network_count != 1
            or network_id != expected.network_id
        ):
            raise StoreSupervisorError("container_network_drift")
        if (
            type(running) is not bool
            or type(status) is not str
            or paused is not False
            or restarting is not False
            or dead is not False
            or oom_killed is not False
            or (
                running is True
                and status != "running"
            )
            or (
                running is False
                and status not in {"created", "exited"}
            )
        ):
            raise StoreSupervisorError("container_state_invalid")
        return ContainerState(
            logical_name=expected.logical_name,
            container_id=expected.container_id,
            running=running,
            status=status,
        )

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
                    (DOCKER_BINARY, "stop", "--timeout=10", container.container_id)
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

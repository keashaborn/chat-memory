from __future__ import annotations

"""Pure, exact argv planner for fresh dormant stores.

This module does not execute commands or read host state.  It accepts the
closed static store specification, binds execution labels, and returns the
five exact Docker create operations in deterministic order.
"""

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Final, Mapping, Sequence

from .host_boundary import DOCKER_BINARY, validate_argv


SCHEMA_VERSION: Final = "governed-memory-phase8b-store-spec-v1"
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SAFE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z", re.ASCII)
_CANDIDATE_ID_RE = re.compile(
    r"governed_memory_([0-9a-f]{12})_([0-9]{6})\Z", re.ASCII
)
_AUTHORIZATION_ID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII
)
_IMAGE_REFERENCE_RE = re.compile(
    r"[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}\Z",
    re.ASCII,
)
_REPO_DIGEST_RE = re.compile(
    r"[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}\Z", re.ASCII
)
_PLACEHOLDERS = {
    "${EXECUTION_BINDING_SHA256}",
    "${AUTHORIZATION_ID}",
    "${AUTHORIZATION_NONCE_SHA256}",
}


class LinuxPlanError(RuntimeError):
    """Closed refusal for an invalid or inexact store plan."""


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    binding_sha256: str
    authorization_id: str
    authorization_nonce_sha256: str

    def __post_init__(self) -> None:
        if (
            _HASH_RE.fullmatch(self.binding_sha256) is None
            or _HASH_RE.fullmatch(self.authorization_nonce_sha256) is None
            or _AUTHORIZATION_ID_RE.fullmatch(self.authorization_id) is None
        ):
            raise LinuxPlanError("execution_binding_invalid")


@dataclass(frozen=True, slots=True)
class PlanStep:
    step_id: str
    argv: tuple[str, ...]


def _require_closed_keys(
    value: object, expected: set[str], *, code: str
) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise LinuxPlanError(code)
    return value


def validate_store_spec(spec: object, *, allow_placeholders: bool = True) -> dict[str, object]:
    root = _require_closed_keys(
        spec,
        {
            "schema_version",
            "state",
            "candidate_id",
            "docker_resource_slug",
            "host_boundary",
            "docker",
            "execution_binding",
            "resources",
        },
        code="store_spec_keys_invalid",
    )
    if root["schema_version"] != SCHEMA_VERSION:
        raise LinuxPlanError("store_spec_schema_invalid")
    if root["state"] != "inactive_package_only_not_installed_not_authorized":
        raise LinuxPlanError("store_spec_state_invalid")
    candidate_id = root["candidate_id"]
    if type(candidate_id) is not str:
        raise LinuxPlanError("store_spec_candidate_invalid")
    candidate_match = _CANDIDATE_ID_RE.fullmatch(candidate_id)
    if candidate_match is None:
        raise LinuxPlanError("store_spec_candidate_invalid")
    identity, sequence = candidate_match.groups()
    resource_suffix = f"{identity}-{sequence}"
    expected_resource_slug = f"governed-memory-{resource_suffix}"
    if root["docker_resource_slug"] != expected_resource_slug:
        raise LinuxPlanError("store_spec_resource_slug_invalid")
    boundary = _require_closed_keys(
        root["host_boundary"],
        {
            "source_postgres_connections_allowed",
            "provider_endpoints_allowed",
            "application_units_included",
            "legacy_imports_allowed",
        },
        code="store_spec_boundary_invalid",
    )
    if boundary != {
        "source_postgres_connections_allowed": False,
        "provider_endpoints_allowed": False,
        "application_units_included": False,
        "legacy_imports_allowed": False,
    }:
        raise LinuxPlanError("store_spec_boundary_open")
    docker = _require_closed_keys(
        root["docker"],
        {"binary", "platform", "pull_policy"},
        code="store_spec_docker_invalid",
    )
    if docker != {
        "binary": DOCKER_BINARY,
        "platform": "linux/amd64",
        "pull_policy": "never",
    }:
        raise LinuxPlanError("store_spec_docker_policy_invalid")
    execution = _require_closed_keys(
        root["execution_binding"],
        {"authorization_id", "authorization_nonce_sha256", "binding_sha256"},
        code="store_spec_execution_binding_invalid",
    )
    if allow_placeholders:
        if set(execution.values()) != _PLACEHOLDERS:
            raise LinuxPlanError("store_spec_placeholders_invalid")
    else:
        ExecutionBinding(
            binding_sha256=execution["binding_sha256"],  # type: ignore[arg-type]
            authorization_id=execution["authorization_id"],  # type: ignore[arg-type]
            authorization_nonce_sha256=execution["authorization_nonce_sha256"],  # type: ignore[arg-type]
        )

    resources = _require_closed_keys(
        root["resources"],
        {"containers", "network", "volumes"},
        code="store_spec_resources_invalid",
    )
    network = _require_closed_keys(
        resources["network"], {"name", "labels"}, code="store_spec_network_invalid"
    )
    volumes = _require_closed_keys(
        resources["volumes"],
        {"postgres", "qdrant"},
        code="store_spec_volumes_invalid",
    )
    containers = _require_closed_keys(
        resources["containers"],
        {"postgres", "qdrant"},
        code="store_spec_containers_invalid",
    )
    all_names: list[str] = []
    for resource in (network, volumes["postgres"], volumes["qdrant"]):
        item = _require_closed_keys(
            resource, {"name", "labels"}, code="store_spec_named_resource_invalid"
        )
        name = item["name"]
        if type(name) is not str or _SAFE_NAME_RE.fullmatch(name) is None:
            raise LinuxPlanError("store_spec_resource_name_invalid")
        _validate_labels(
            item["labels"],
            allow_placeholders=allow_placeholders,
            candidate_id=candidate_id,
        )
        all_names.append(name)
    expected_resource_names = {
        "network": f"governed-memory-net-{resource_suffix}",
        "postgres_volume": f"governed-memory-postgres-data-{resource_suffix}",
        "qdrant_volume": f"governed-memory-qdrant-data-{resource_suffix}",
    }
    if (
        network["name"] != expected_resource_names["network"]
        or volumes["postgres"]["name"]
        != expected_resource_names["postgres_volume"]
        or volumes["qdrant"]["name"] != expected_resource_names["qdrant_volume"]
    ):
        raise LinuxPlanError("store_spec_resource_name_mismatch")
    for logical_name in ("postgres", "qdrant"):
        item = _require_closed_keys(
            containers[logical_name],
            {
                "name",
                "image",
                "publish",
                "volume_name",
                "volume_target",
                "environment_file",
                "environment_contract",
                "healthcheck",
                "logging",
                "pids_limit",
                "restart",
                "security",
                "tmpfs",
                "command",
                "labels",
            },
            code="store_spec_container_invalid",
        )
        name = item["name"]
        if type(name) is not str or _SAFE_NAME_RE.fullmatch(name) is None:
            raise LinuxPlanError("store_spec_resource_name_invalid")
        if name != f"governed-memory-{logical_name}-{resource_suffix}":
            raise LinuxPlanError("store_spec_resource_name_mismatch")
        all_names.append(name)
        image = _require_closed_keys(
            item["image"],
            {"reference", "repo_digest", "image_id_required"},
            code="store_spec_image_invalid",
        )
        if (
            type(image["reference"]) is not str
            or _IMAGE_REFERENCE_RE.fullmatch(image["reference"]) is None
            or type(image["repo_digest"]) is not str
            or _REPO_DIGEST_RE.fullmatch(image["repo_digest"]) is None
            or not image["reference"].endswith(
                "@" + image["repo_digest"].split("@", 1)[1]
            )
            or image["image_id_required"] is not True
        ):
            raise LinuxPlanError("store_spec_image_invalid")
        publish = _require_closed_keys(
            item["publish"],
            {"container_port", "host", "host_port"},
            code="store_spec_publish_invalid",
        )
        if publish["host"] != "127.0.0.1" or any(
            type(publish[key]) is not int or not 1 <= publish[key] <= 65535
            for key in ("host_port", "container_port")
        ):
            raise LinuxPlanError("store_spec_publish_not_loopback")
        expected_volume = volumes[logical_name]["name"]
        if item["volume_name"] != expected_volume:
            raise LinuxPlanError("store_spec_volume_mismatch")
        for path_key in ("volume_target", "environment_file"):
            value = item[path_key]
            if type(value) is not str or not value.startswith("/") or "*" in value:
                raise LinuxPlanError("store_spec_path_invalid")
        expected_environment_file = (
            f"/etc/governed-memory-stores/{resource_suffix}/{logical_name}.env"
        )
        if item["environment_file"] != expected_environment_file:
            raise LinuxPlanError("store_spec_environment_file_invalid")
        environment_contract = _require_closed_keys(
            item["environment_contract"],
            {"fixed_values", "forbidden_keys", "required_keys", "secret_keys"},
            code="store_spec_environment_contract_invalid",
        )
        expected_environment_contracts = {
            "postgres": {
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
            "qdrant": {
                "fixed_values": {},
                "forbidden_keys": [
                    "POSTGRES_DB",
                    "POSTGRES_PASSWORD",
                    "POSTGRES_USER",
                ],
                "required_keys": ["QDRANT__SERVICE__API_KEY"],
                "secret_keys": ["QDRANT__SERVICE__API_KEY"],
            },
        }
        if environment_contract != expected_environment_contracts[logical_name]:
            raise LinuxPlanError("store_spec_environment_contract_invalid")
        healthcheck = _require_closed_keys(
            item["healthcheck"],
            {"container_healthcheck", "external_readiness_probe"},
            code="store_spec_healthcheck_invalid",
        )
        if healthcheck != {
            "container_healthcheck": "disabled",
            "external_readiness_probe": "unimplemented_disposable_proof_required",
        }:
            raise LinuxPlanError("store_spec_healthcheck_invalid")
        logging = _require_closed_keys(
            item["logging"],
            {"driver", "max_file", "max_size"},
            code="store_spec_logging_invalid",
        )
        if logging != {"driver": "json-file", "max_file": 3, "max_size": "10m"}:
            raise LinuxPlanError("store_spec_logging_invalid")
        security = _require_closed_keys(
            item["security"],
            {"cap_add", "cap_drop", "no_new_privileges"},
            code="store_spec_security_invalid",
        )
        expected_security = {
            "postgres": {
                "cap_add": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"],
                "cap_drop": ["ALL"],
                "no_new_privileges": True,
            },
            "qdrant": {
                "cap_add": [],
                "cap_drop": ["ALL"],
                "no_new_privileges": True,
            },
        }
        if security != expected_security[logical_name]:
            raise LinuxPlanError("store_spec_security_invalid")
        if (
            item["restart"] != "no"
            or type(item["pids_limit"]) is not int
            or item["pids_limit"] != 256
            or item["tmpfs"] != ["/tmp:rw,nosuid,nodev,noexec,size=64m"]
        ):
            raise LinuxPlanError("store_spec_runtime_hardening_invalid")
        command = item["command"]
        if type(command) is not list or any(
            type(argument) is not str or not argument for argument in command
        ):
            raise LinuxPlanError("store_spec_command_invalid")
        _validate_labels(
            item["labels"],
            allow_placeholders=allow_placeholders,
            candidate_id=candidate_id,
        )
    if len(all_names) != len(set(all_names)):
        raise LinuxPlanError("store_spec_resource_names_not_distinct")
    return root


def _validate_labels(
    value: object, *, allow_placeholders: bool, candidate_id: str
) -> dict[str, str]:
    required = {
        "lifeswitch.governed-memory.authorization-id",
        "lifeswitch.governed-memory.authorization-nonce-sha256",
        "lifeswitch.governed-memory.candidate",
        "lifeswitch.governed-memory.execution-binding-sha256",
        "lifeswitch.governed-memory.phase",
    }
    if type(value) is not dict or set(value) != required:
        raise LinuxPlanError("store_spec_labels_invalid")
    if value["lifeswitch.governed-memory.phase"] != "8B":
        raise LinuxPlanError("store_spec_label_phase_invalid")
    if value["lifeswitch.governed-memory.candidate"] != candidate_id:
        raise LinuxPlanError("store_spec_label_candidate_invalid")
    for label_value in value.values():
        if type(label_value) is not str or not label_value:
            raise LinuxPlanError("store_spec_label_value_invalid")
        if not allow_placeholders and label_value in _PLACEHOLDERS:
            raise LinuxPlanError("store_spec_label_unresolved")
    return value  # type: ignore[return-value]


def load_store_spec(
    path: Path,
    *,
    allow_placeholders: bool = True,
    secure_root_file: bool = False,
    secure_expected_uid: int = 0,
) -> dict[str, object]:
    try:
        if secure_root_file:
            from .secure_file import read_root_owned_regular_file

            raw = read_root_owned_regular_file(
                path,
                expected_file_mode=0o600,
                expected_parent_mode=0o700,
                expected_uid=secure_expected_uid,
            )
        else:
            raw = path.read_bytes()
        value = json.loads(raw.decode("ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LinuxPlanError("store_spec_document_invalid") from error
    canonical = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")
    if raw != canonical:
        raise LinuxPlanError("store_spec_document_not_canonical")
    return validate_store_spec(value, allow_placeholders=allow_placeholders)


def bind_store_spec(
    static_spec: Mapping[str, object], binding: ExecutionBinding
) -> dict[str, object]:
    validate_store_spec(static_spec, allow_placeholders=True)
    bound = deepcopy(static_spec)
    replacements = {
        "${EXECUTION_BINDING_SHA256}": binding.binding_sha256,
        "${AUTHORIZATION_ID}": binding.authorization_id,
        "${AUTHORIZATION_NONCE_SHA256}": binding.authorization_nonce_sha256,
    }

    def replace(value: object) -> object:
        if type(value) is dict:
            return {key: replace(item) for key, item in value.items()}
        if type(value) is list:
            return [replace(item) for item in value]
        if type(value) is str:
            return replacements.get(value, value)
        return value

    bound = replace(bound)  # type: ignore[assignment]
    return validate_store_spec(bound, allow_placeholders=False)


def _label_args(labels: Mapping[str, str]) -> tuple[str, ...]:
    result: list[str] = []
    for key in sorted(labels):
        result.extend(("--label", f"{key}={labels[key]}"))
    return tuple(result)


def build_store_create_plan(bound_spec: Mapping[str, object]) -> tuple[PlanStep, ...]:
    spec = validate_store_spec(bound_spec, allow_placeholders=False)
    resources = spec["resources"]  # type: ignore[assignment]
    network = resources["network"]
    volumes = resources["volumes"]
    containers = resources["containers"]
    network_name = network["name"]

    steps: list[PlanStep] = [
        PlanStep(
            "S01_CREATE_EXACT_NETWORK",
            (
                DOCKER_BINARY,
                "network",
                "create",
                "--driver",
                "bridge",
                "--internal",
                *_label_args(network["labels"]),
                network_name,
            ),
        )
    ]
    for index, logical_name in enumerate(("postgres", "qdrant"), start=2):
        volume = volumes[logical_name]
        steps.append(
            PlanStep(
                f"S0{index}_CREATE_EXACT_{logical_name.upper()}_VOLUME",
                (
                    DOCKER_BINARY,
                    "volume",
                    "create",
                    *_label_args(volume["labels"]),
                    volume["name"],
                ),
            )
        )
    for index, logical_name in enumerate(("postgres", "qdrant"), start=4):
        container = containers[logical_name]
        publish = container["publish"]
        argv = (
            DOCKER_BINARY,
            "create",
            "--pull=never",
            "--name",
            container["name"],
            "--platform",
            "linux/amd64",
            "--network",
            network_name,
            "--restart=no",
            "--pids-limit=256",
            "--security-opt=no-new-privileges",
            "--cap-drop=ALL",
            *tuple(
                argument
                for capability in container["security"]["cap_add"]
                for argument in ("--cap-add", capability)
            ),
            "--log-driver=json-file",
            "--log-opt=max-size=10m",
            "--log-opt=max-file=3",
            "--no-healthcheck",
            "--tmpfs",
            container["tmpfs"][0],
            *_label_args(container["labels"]),
            "--publish",
            f"{publish['host']}:{publish['host_port']}:{publish['container_port']}",
            "--mount",
            (
                "type=volume,source="
                f"{container['volume_name']},target={container['volume_target']}"
            ),
            "--env-file",
            container["environment_file"],
            container["image"]["reference"],
            *container["command"],
        )
        steps.append(
            PlanStep(
                f"S0{index}_CREATE_EXACT_{logical_name.upper()}_CONTAINER",
                validate_argv(argv),
            )
        )
    if len({step.step_id for step in steps}) != len(steps):
        raise LinuxPlanError("store_plan_step_ids_not_distinct")
    return tuple(steps)

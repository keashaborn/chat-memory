#!/usr/bin/env python3
from __future__ import annotations

"""Offline verifier for the current inactive Phase 8B execution package.

The verifier reads repository files only. It has no command runner, live
executor, network client, secret reader, installation, rollback, or activation
surface.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from types import ModuleType
from typing import Final


ROOT: Final = Path(__file__).resolve().parents[2]
INSTALLATION: Final = ROOT / "ops" / "governed_memory" / "installation"
PHASE8B: Final = INSTALLATION / "phase8b"
MANIFEST: Path = PHASE8B / "package_manifest.json"
CONTRACT: Final = PHASE8B / "contract.json"
PLAN: Final = PHASE8B / "controller_plan.json"
HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)

MANIFEST_RELATIVE: Final = (
    "ops/governed_memory/installation/phase8b/package_manifest.json"
)
CONTRACT_RELATIVE: Final = "ops/governed_memory/installation/phase8b/contract.json"
PLAN_RELATIVE: Final = (
    "ops/governed_memory/installation/phase8b/controller_plan.json"
)
MIGRATION_MANIFEST_RELATIVE: Final = (
    "ops/governed_memory/installation/phase8b/migration_manifest.json"
)
MIGRATION_BINDINGS_RELATIVE: Final = (
    "ops/governed_memory/installation/phase8b/postgres/migration_bindings.json"
)
MIGRATION_VERIFIER_RELATIVE: Final = (
    "tools/governed_memory_validation/verify_store_migration_manifest.py"
)
CONTROLLER_MODEL_RELATIVE: Final = (
    "tools/governed_memory_install/controller.py"
)
EXECUTION_CONTRACT_RELATIVE: Final = (
    "ops/governed_memory/installation/phase8b/execution_contract.json"
)
CONTROLLER_RUNTIME_CONTRACT_RELATIVE: Final = (
    "ops/governed_memory/installation/phase8b/controller_runtime_contract.json"
)
PROOF_CONTRACT_RELATIVE: Final = (
    "ops/governed_memory/installation/phase8b/disposable_proof_contract.json"
)
PROOF_SCHEMA_RELATIVE: Final = (
    "ops/governed_memory/installation/phase8b/disposable_proof_receipt.schema.json"
)

EXPECTED_CONTRACT_CANONICAL_SHA256: Final = (
    "a6b145e5c6e9f53aabe180d8ca78ee499ed97353c1d9d69677873ae802fa8455"
)
EXPECTED_PLAN_CANONICAL_SHA256: Final = (
    "65f9be1d974bfeb6f047b6c33c3fd5a2c4df1a7eeec3df274e75488168de75e2"
)
EXPECTED_CONTROLLER_SOURCE_SHA256: Final = (
    "41215dd2676c8b8cee8e62ff7c83af83ddadaec4dbe72aae5001635cf231353e"
)
EXPECTED_CONTROLLER_MODEL_SHA256: Final = (
    "601288e31fd1b485e57ef5fd4676f9477942db0c3aa3c911de1d002361c49243"
)
EXPECTED_EXECUTION_CONTRACT_CANONICAL_SHA256: Final = (
    "b092c15dbd713b74226ca08fdd98071e88808141e359960e9e7f1705bca84165"
)
EXPECTED_CONTROLLER_RUNTIME_CONTRACT_CANONICAL_SHA256: Final = (
    "f12c9e52bfd4adacaff9715ea7b9a3b097286df5bcc613344c47cb80ff84a315"
)
EXPECTED_PROOF_CONTRACT_CANONICAL_SHA256: Final = (
    "48b2b6ebd9edf7ed8d60bc52df5df363b583da52c33567b7eaf08db8c7ff4a3f"
)
EXPECTED_PROOF_SCHEMA_CANONICAL_SHA256: Final = (
    "3dbc0e9a3e927ec3a17734fae67f02dd0d1567c0c4220f3ae61954a4ee301d67"
)
EXPECTED_MIGRATION_VERIFIER_SOURCE_SHA256: Final = (
    "a5c21c48d692040fefc13473c37d3a5e8e652455a0484cf7a72f73904bd0eac0"
)
EXPECTED_MIGRATION_BINDINGS_CANONICAL_SHA256: Final = (
    "0068a7b9aca51c35185bad33607574c3cc108c0f3b384aa1742e09f4329102cb"
)
EXPECTED_CONTROLLER_STEP_IDS: Final = (
    "I01_REVERIFY_PRECLAIMED_EXECUTION_LOCK",
    "I02_VERIFY_CLAIMED_EXECUTION_BINDING",
    "I03_VERIFY_LIVE_PREFLIGHT",
    "I04_STAGE_IMMUTABLE_CONTROLLER_RELEASE",
    "I05_GENERATE_FRESH_STORE_SECRETS",
    "I06_CREATE_EXACT_NETWORK",
    "I07_CREATE_EXACT_POSTGRES_VOLUME",
    "I08_CREATE_EXACT_QDRANT_VOLUME",
    "I09_CREATE_EXACT_POSTGRES_CONTAINER",
    "I10_CREATE_EXACT_QDRANT_CONTAINER",
    "I11_START_AND_VERIFY_EMPTY_STORES",
    "I12_BOOTSTRAP_CANONICAL_DATABASE",
    "I13_APPLY_FOUNDATION_0001",
    "I14_APPLY_OWNER_CLAIM_DETAIL_0003",
    "I15_APPLY_PILOT_MARKER_0004",
    "I16_CREATE_EMPTY_QDRANT_COLLECTION",
    "I17_CREATE_QDRANT_ALIAS",
    "I18_SEAL_RESOURCE_IDENTITY_LEDGER",
    "I19_INSTALL_AND_ENABLE_STORES_SUPERVISOR",
    "I20_COLD_RESTART_AND_SEAL_INACTIVE_POSTFLIGHT",
)

EXPECTED_CONTRACT_KEYS: Final = frozenset(
    {
        "schema_version",
        "state",
        "server",
        "candidate_id",
        "scope",
        "fresh_store_policy",
        "exact_targets",
        "filesystem_policy",
        "image_policy",
        "secret_policy",
        "migration_policy",
        "authority_policy",
        "recovery_policy",
        "identity_policy",
        "supervisor_policy",
        "proof_harness_policy",
        "excluded_components",
        "remaining_blockers",
    }
)
EXPECTED_PLAN_KEYS: Final = frozenset(
    {
        "schema_version",
        "state",
        "server",
        "candidate_id",
        "contract",
        "execution_invariants",
        "install_steps",
        "same_attempt_compensation_order",
        "later_rollback",
        "live_execution",
        "synthetic_proof",
    }
)
EXPECTED_LIVE_EXECUTION: Final = {
    "installation_composition_callable_packaged": False,
    "live_install_entrypoint_packaged": False,
    "generic_docker_host_runner_primitive_packaged": True,
    "local_image_inspect_adapter_packaged": True,
    "claim_bound_installation_runner_composition_packaged": False,
    "claim_bound_installation_store_effect_adapters_packaged": False,
    "live_rollback_entrypoint_packaged": False,
    "live_activation_entrypoint_packaged": False,
    "claim_bound_installation_typed_command_boundary_packaged": False,
    "stores_supervisor_cli_packaged": True,
    "stores_supervisor_cli_docker_surface": [
        "container_inspect",
        "container_start",
        "container_stop",
    ],
    "stores_supervisor_is_installer": False,
    "stores_supervisor_requires_preexisting_exact_ledger_container_ids": True,
}
EXPECTED_MIGRATION_ARTIFACTS: Final = frozenset(
    {
        MIGRATION_BINDINGS_RELATIVE,
        "ops/governed_memory/installation/phase8b/postgres/roles_preflight.pgsql",
        "governed-memory-migrations/schema_contract.json",
        "governed-memory-migrations/predicate_catalog.json",
        "governed-memory-migrations/0001_foundation/forward.pgsql",
        "governed-memory-migrations/0001_foundation/rollback.pgsql",
        "governed-memory-migrations/0003_owner_claim_detail/forward.pgsql",
        "governed-memory-migrations/0003_owner_claim_detail/rollback.pgsql",
        "governed-memory-migrations/0004_pilot_marker/forward.pgsql",
        "governed-memory-migrations/0004_pilot_marker/rollback.pgsql",
    }
)

EXPECTED_ARTIFACTS: Final = frozenset(
    {
        "governed-memory-migrations/0001_foundation/forward.pgsql",
        "governed-memory-migrations/0001_foundation/rollback.pgsql",
        "governed-memory-migrations/0003_owner_claim_detail/forward.pgsql",
        "governed-memory-migrations/0003_owner_claim_detail/rollback.pgsql",
        "governed-memory-migrations/0004_pilot_marker/forward.pgsql",
        "governed-memory-migrations/0004_pilot_marker/rollback.pgsql",
        "governed-memory-migrations/predicate_catalog.json",
        "governed-memory-migrations/schema_contract.json",
        "ops/governed_memory/installation/phase8b/contract.json",
        "ops/governed_memory/installation/phase8b/controller_plan.json",
        "ops/governed_memory/installation/phase8b/controller_runtime_contract.json",
        "ops/governed_memory/installation/phase8b/disposable_proof_contract.json",
        "ops/governed_memory/installation/phase8b/disposable_proof_receipt.schema.json",
        "ops/governed_memory/installation/phase8b/execution_contract.json",
        "ops/governed_memory/installation/phase8b/migration_manifest.json",
        "ops/governed_memory/installation/phase8b/postgres/migration_bindings.json",
        "ops/governed_memory/installation/phase8b/postgres/roles_preflight.pgsql",
        "ops/governed_memory/installation/postgres/canonical_cluster.pgsql.in",
        "ops/governed_memory/installation/postgres/canonical_cluster_rollback.pgsql.in",
        "ops/governed_memory/installation/store_spec.json",
        "ops/governed_memory/controller-requirements.lock",
        "ops/governed_memory/installation/systemd/governed-memory-stores.service.in",
        "ops/governed_memory/qdrant_alias.create.json",
        "ops/governed_memory/qdrant_collection.create.json",
        "tools/governed_memory_install/__init__.py",
        "tools/governed_memory_install/authority.py",
        "tools/governed_memory_install/authority_state.py",
        "tools/governed_memory_install/controller.py",
        "tools/governed_memory_install/disposable_proof_harness.py",
        "tools/governed_memory_install/journal.py",
        "tools/governed_memory_install/execution_authority.py",
        "tools/governed_memory_install/execution_capability.py",
        "tools/governed_memory_install/execution_lock.py",
        "tools/governed_memory_install/host_boundary.py",
        "tools/governed_memory_install/image_preflight.py",
        "tools/governed_memory_install/linux_plan.py",
        "tools/governed_memory_install/package.py",
        "tools/governed_memory_install/resource_identity.py",
        "tools/governed_memory_install/secure_file.py",
        "tools/governed_memory_install/store_supervisor.py",
        "tools/governed_memory_install/synthetic_backend.py",
        "tools/governed_memory_validation/generate_phase8b_package_manifest.py",
        "tools/governed_memory_validation/run_phase8b_disposable_proof.py",
        "tools/governed_memory_validation/verify_store_migration_manifest.py",
    }
)

FORBIDDEN_ARTIFACT_MARKERS: Final = (
    "0002_conversation_bridge",
    "legacy_quiescence",
    "source_cluster_roles",
    "http.env",
    "worker.env",
    "pilot.env",
    "governed-memory-http.service",
    "governed-memory-worker.service",
    "runtime_build",
    "runtime-requirements",
    "supabase",
    "phase8a_disposable_proof",
)


class PackageError(ValueError):
    """Closed, content-free package verification refusal."""


class _NonFiniteJsonValue(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PackageError("phase8b_package_duplicate_json_key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise _NonFiniteJsonValue(value)


def _relative_parts(relative: object) -> tuple[str, ...]:
    if type(relative) is not str or not relative or "\\" in relative:
        raise PackageError("phase8b_package_artifact_path_invalid")
    pure = PurePosixPath(relative)
    parts = pure.parts
    if (
        pure.is_absolute()
        or not parts
        or str(pure) != relative
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise PackageError("phase8b_package_artifact_path_invalid")
    return parts


def _nofollow() -> int:
    flag = getattr(os, "O_NOFOLLOW", 0)
    if flag == 0:
        raise PackageError("phase8b_package_nofollow_unavailable")
    return flag


def _read_repository_file(relative: str) -> bytes:
    """Read one regular repository file without following any path symlink."""

    parts = _relative_parts(relative)
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | _nofollow()
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    file_flags = os.O_RDONLY | os.O_CLOEXEC | _nofollow()
    descriptors: list[int] = []
    try:
        directory_fd = os.open(ROOT, directory_flags)
        descriptors.append(directory_fd)
        for part in parts[:-1]:
            directory_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            descriptors.append(directory_fd)
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        descriptors.append(file_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise PackageError("phase8b_package_artifact_path_invalid")
        chunks: list[bytes] = []
        while True:
            block = os.read(file_fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    except OSError as error:
        raise PackageError("phase8b_package_artifact_path_invalid") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_path(path: Path) -> bytes:
    """Use strict repository traversal for production package documents."""

    try:
        relative = path.relative_to(ROOT)
    except ValueError:
        # Test-only injected manifests remain final-component no-follow files.
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | _nofollow())
        except OSError as error:
            raise PackageError("phase8b_package_document_path_invalid") from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise PackageError("phase8b_package_document_path_invalid")
            chunks: list[bytes] = []
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                chunks.append(block)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    return _read_repository_file(relative.as_posix())


def _parse_json(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _NonFiniteJsonValue,
    ) as error:
        raise PackageError("phase8b_package_json_invalid") from error
    if type(value) is not dict:
        raise PackageError("phase8b_package_json_root_invalid")
    return value


def _load(path: Path) -> dict[str, object]:
    return _parse_json(_read_path(path))


def _load_verified_json(relative: str, expected_sha256: str) -> dict[str, object]:
    raw = _read_repository_file(relative)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PackageError("phase8b_package_member_changed_after_hash")
    return _parse_json(raw)


def _canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise PackageError("phase8b_package_json_invalid") from error
    return hashlib.sha256(encoded).hexdigest()


def artifact_sha256(relative: str) -> str:
    return hashlib.sha256(_read_repository_file(relative)).hexdigest()


def _verify_contract(contract: dict[str, object]) -> None:
    if set(contract) != EXPECTED_CONTRACT_KEYS:
        raise PackageError("phase8b_contract_shape_invalid")
    if _canonical_sha256(contract) != EXPECTED_CONTRACT_CANONICAL_SHA256:
        raise PackageError("phase8b_contract_semantics_invalid")
    if (
        contract.get("schema_version")
        != "governed-memory-phase8b-inactive-execution-contract-v2"
        or contract.get("state")
        != (
            "phase8b_inactive_execution_and_synthetic_proof_harness_packaged_"
            "proof_pending_not_staged_not_installed_not_authorized"
        )
        or contract.get("server") != "seebx"
    ):
        raise PackageError("phase8b_contract_identity_invalid")
    scope = contract.get("scope")
    if type(scope) is not dict or any(
        scope.get(key) is not False
        for key in (
            "current_phase_executes_live_steps",
            "current_phase_promotes_disposable_proof_receipt",
            "current_phase_stages_images",
            "current_phase_reads_writes_or_generates_secrets",
            "current_phase_installs_or_activates",
            "current_approval_is_future_install_authority",
            "current_approval_is_future_rollback_authority",
            "current_approval_is_future_live_proof_authority",
        )
    ):
        raise PackageError("phase8b_contract_authority_boundary_invalid")
    migration = contract.get("migration_policy")
    if (
        type(migration) is not dict
        or migration.get("excluded_migrations") != ["0002_conversation_bridge"]
        or migration.get("source_postgresql_connections") != 0
        or migration.get("source_postgresql_reads") != 0
        or migration.get("source_postgresql_writes") != 0
    ):
        raise PackageError("phase8b_contract_migration_boundary_invalid")
    secret = contract.get("secret_policy")
    if (
        type(secret) is not dict
        or secret.get("legacy_pilot_env_may_be_read_stat_hashed_renamed_or_deleted")
        is not False
    ):
        raise PackageError("phase8b_contract_secret_boundary_invalid")
    authority = contract.get("authority_policy")
    recovery = contract.get("recovery_policy")
    blockers = contract.get("remaining_blockers")
    if (
        type(authority) is not dict
        or authority.get("opaque_verified_package_capability_required") is not False
        or authority.get("opaque_claimed_execution_binding_required") is not True
        or type(recovery) is not dict
        or recovery.get("same_open_instance_inode_and_directory_replacement_refused")
        is not True
        or recovery.get(
            "cross_process_same_content_inode_or_directory_replacement_refused"
        )
        is not False
        or type(blockers) is not list
        or (
            "opaque_verified_package_capability_and_claim_boundary_not_implemented"
            not in blockers
        )
        or (
            "cross_process_durable_file_identity_or_equivalent_seal_not_implemented"
            not in blockers
        )
    ):
        raise PackageError("phase8b_contract_package_claim_boundary_invalid")


def _verify_plan(plan: dict[str, object]) -> None:
    if set(plan) != EXPECTED_PLAN_KEYS:
        raise PackageError("phase8b_plan_shape_invalid")
    if _canonical_sha256(plan) != EXPECTED_PLAN_CANONICAL_SHA256:
        raise PackageError("phase8b_plan_semantics_invalid")
    if (
        plan.get("schema_version")
        != "governed-memory-phase8b-stores-controller-plan-v3"
        or plan.get("state")
        != (
            "inactive_execution_components_and_synthetic_harness_packaged_"
            "no_complete_live_executor_not_installed_not_authorized"
        )
        or plan.get("server") != "seebx"
    ):
        raise PackageError("phase8b_plan_identity_invalid")
    steps = plan.get("install_steps")
    if type(steps) is not list or len(steps) != len(EXPECTED_CONTROLLER_STEP_IDS):
        raise PackageError("phase8b_plan_step_count_invalid")
    observed_ids: list[str] = []
    for expected_id, step in zip(EXPECTED_CONTROLLER_STEP_IDS, steps, strict=True):
        if type(step) is not dict or set(step) != {"id", "effect", "rollback"}:
            raise PackageError("phase8b_plan_step_shape_invalid")
        if step.get("id") != expected_id:
            raise PackageError("phase8b_plan_step_order_invalid")
        if any(
            type(step.get(key)) is not str or not step.get(key)
            for key in ("effect", "rollback")
        ):
            raise PackageError("phase8b_plan_step_shape_invalid")
        observed_ids.append(expected_id)
    if tuple(observed_ids) != EXPECTED_CONTROLLER_STEP_IDS:
        raise PackageError("phase8b_plan_step_order_invalid")
    live = plan.get("live_execution")
    if live != EXPECTED_LIVE_EXECUTION:
        raise PackageError("phase8b_plan_live_surface_invalid")


def _verify_extension_contracts(observed: dict[str, str]) -> None:
    execution = _load_verified_json(
        EXECUTION_CONTRACT_RELATIVE,
        observed[EXECUTION_CONTRACT_RELATIVE],
    )
    runtime = _load_verified_json(
        CONTROLLER_RUNTIME_CONTRACT_RELATIVE,
        observed[CONTROLLER_RUNTIME_CONTRACT_RELATIVE],
    )
    proof = _load_verified_json(
        PROOF_CONTRACT_RELATIVE,
        observed[PROOF_CONTRACT_RELATIVE],
    )
    proof_schema = _load_verified_json(
        PROOF_SCHEMA_RELATIVE,
        observed[PROOF_SCHEMA_RELATIVE],
    )
    exact = (
        (execution, EXPECTED_EXECUTION_CONTRACT_CANONICAL_SHA256),
        (runtime, EXPECTED_CONTROLLER_RUNTIME_CONTRACT_CANONICAL_SHA256),
        (proof, EXPECTED_PROOF_CONTRACT_CANONICAL_SHA256),
        (proof_schema, EXPECTED_PROOF_SCHEMA_CANONICAL_SHA256),
    )
    if any(_canonical_sha256(value) != wanted for value, wanted in exact):
        raise PackageError("phase8b_extension_contract_semantics_invalid")

    boundary = execution.get("execution_boundary")
    binding = execution.get("binding_policy")
    durability = execution.get("durability_policy")
    host = execution.get("host_action_policy")
    proof_boundary = execution.get("proof_boundary")
    build = runtime.get("build_policy")
    entrypoint = runtime.get("entrypoint_policy")
    proof_execution = proof.get("execution_boundary")
    proof_claims = proof.get("claims")
    if (
        execution.get("schema_version")
        != "governed-memory-phase8b-inactive-execution-package-v1"
        or type(boundary) is not dict
        or any(
            boundary.get(key) is not False
            for key in (
                "approval_authorizes_execution",
                "installation_executor_implemented",
                "rollback_executor_implemented",
                "activation_executor_implemented",
                "live_entrypoint_callable_but_not_cli_exposed",
            )
        )
        or type(binding) is not dict
        or binding.get("opaque_verified_package_capability_required") is not False
        or binding.get("opaque_claimed_execution_capability_required") is not True
        or type(durability) is not dict
        or durability.get(
            "same_open_instance_inode_and_directory_replacement_refused"
        )
        is not True
        or durability.get(
            "cross_process_same_content_inode_or_directory_replacement_refused"
        )
        is not False
        or type(execution.get("remaining_blockers")) is not list
        or (
            "opaque_verified_package_capability_and_claim_boundary_not_implemented"
            not in execution["remaining_blockers"]
        )
        or (
            "cross_process_durable_file_identity_or_equivalent_seal_not_implemented"
            not in execution["remaining_blockers"]
        )
        or type(host) is not dict
        or host.get("typed_operation_specific_boundaries_only") is not False
        or type(proof_boundary) is not dict
        or proof_boundary.get("harness_type")
        != "guarded_synthetic_only"
        or type(build) is not dict
        or build.get("current_runtime_built") is not False
        or build.get("current_runtime_installed") is not False
        or type(entrypoint) is not dict
        or entrypoint.get("opaque_python_capabilities_resist_hostile_same_process_code")
        is not False
        or type(proof_execution) is not dict
        or any(
            proof_execution.get(key) is not False
            for key in (
                "live_mode_exists",
                "command_runner_exists",
                "host_adapter_exists",
                "docker_adapter_exists",
                "systemd_adapter_exists",
                "network_adapter_exists",
                "secret_adapter_exists",
                "installation_adapter_exists",
                "activation_adapter_exists",
            )
        )
        or type(proof_claims) is not dict
        or any(
            proof_claims.get(key) is not False
            for key in (
                "live_execution_proven",
                "live_installation_proven",
                "live_rollback_proven",
                "durable_process_crash_recovery_proven",
                "composite_crash_recovery_proven",
                "docker_compatibility_proven",
                "systemd_compatibility_proven",
                "external_store_readiness_proven",
                "activation_proven",
            )
        )
        or proof_schema.get("additionalProperties") is not False
    ):
        raise PackageError("phase8b_extension_contract_boundary_invalid")


def _load_verified_module(
    relative: str,
    module_name: str,
    *,
    expected_sha256: str,
) -> ModuleType:
    """Execute exactly the bytes already checked against the package manifest."""

    raw = _read_repository_file(relative)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PackageError("phase8b_verified_module_changed_after_hash")
    module = ModuleType(module_name)
    module.__file__ = str(ROOT / relative)
    module.__package__ = module_name.rpartition(".")[0]
    if module_name in sys.modules:
        raise PackageError("phase8b_verified_module_namespace_collision")
    sys.modules[module_name] = module
    try:
        exec(compile(raw, module.__file__, "exec"), module.__dict__)
    except Exception as error:
        raise PackageError("phase8b_verified_module_invalid") from error
    finally:
        sys.modules.pop(module_name, None)
    return module


def _load_verified_controller_module(
    relative: str,
    *,
    expected_sha256: str,
) -> ModuleType:
    """Load the controller model without importing repository dependencies.

    ``controller`` imports three lock types that are irrelevant to plan
    validation.  Supplying a closed synthetic dependency prevents verification
    from executing an unpinned repository module through that relative import.
    Every callable in the stub refuses use, so the loader cannot accidentally
    become a controller execution surface.
    """

    package_name = "_phase8b_verified_controller_package"
    module_name = package_name + ".controller"
    lock_name = package_name + ".execution_lock"

    class _ExecutionLockError(RuntimeError):
        pass

    class _HeldExecutionLockCapability:
        pass

    def _refuse_lock_use(unused: object) -> None:
        raise _ExecutionLockError("phase8b_verifier_lock_surface_unavailable")

    package = ModuleType(package_name)
    package.__path__ = []  # type: ignore[attr-defined]
    lock_module = ModuleType(lock_name)
    lock_module.ExecutionLockError = _ExecutionLockError
    lock_module.HeldExecutionLockCapability = _HeldExecutionLockCapability
    lock_module.validate_held_execution_lock = _refuse_lock_use
    inserted = {
        package_name: package,
        lock_name: lock_module,
    }
    if any(name in sys.modules for name in inserted):
        raise PackageError("phase8b_verified_module_namespace_collision")
    sys.modules.update(inserted)
    try:
        return _load_verified_module(
            relative,
            module_name,
            expected_sha256=expected_sha256,
        )
    finally:
        sys.modules.pop(module_name, None)
        for name in inserted:
            sys.modules.pop(name, None)


def _verify_controller_binding(
    plan: dict[str, object], module: ModuleType
) -> str:
    try:
        controller_plan = module.STORES_ONLY_PLAN
        controller_hash = module.validate_plan(controller_plan)
        controller_projection = module.plan_install_steps_projection(
            controller_plan
        )
    except Exception as error:
        raise PackageError("phase8b_controller_model_invalid") from error
    if (
        controller_hash != EXPECTED_CONTROLLER_MODEL_SHA256
        or tuple(step["id"] for step in controller_projection)
        != EXPECTED_CONTROLLER_STEP_IDS
        or plan["install_steps"] != controller_projection
    ):
        raise PackageError("phase8b_controller_plan_binding_invalid")
    return controller_hash


def _verify_migration_binding(
    observed: dict[str, str], module: ModuleType
) -> dict[str, object]:
    try:
        receipt = module.verify()
    except Exception as error:
        raise PackageError("phase8b_migration_verifier_failed") from error
    expected_keys = {
        "schema_version",
        "state",
        "file_count",
        "manifest_sha256",
        "migration_bindings_sha256",
        "migration_bindings_canonical_sha256",
        "artifact_sha256",
        "source_bridge_artifact_count",
        "historical_package_descriptor_count",
        "production_state_changed",
    }
    if type(receipt) is not dict or set(receipt) != expected_keys:
        raise PackageError("phase8b_migration_receipt_invalid")
    if (
        receipt.get("schema_version")
        != "governed-memory-phase8b-store-migration-verification-v2"
        or receipt.get("state")
        != (
            "phase8b_stores_only_remediation_proof_pending_not_installed_"
            "not_authorized"
        )
        or receipt.get("file_count") != len(EXPECTED_MIGRATION_ARTIFACTS)
        or receipt.get("source_bridge_artifact_count") != 0
        or receipt.get("historical_package_descriptor_count") != 0
        or receipt.get("production_state_changed") is not False
        or receipt.get("manifest_sha256")
        != observed[MIGRATION_MANIFEST_RELATIVE]
        or receipt.get("migration_bindings_sha256")
        != observed[MIGRATION_BINDINGS_RELATIVE]
        or receipt.get("migration_bindings_canonical_sha256")
        != EXPECTED_MIGRATION_BINDINGS_CANONICAL_SHA256
    ):
        raise PackageError("phase8b_migration_receipt_invalid")
    migration_artifacts = receipt.get("artifact_sha256")
    if type(migration_artifacts) is not dict:
        raise PackageError("phase8b_migration_receipt_invalid")
    package_migration_artifacts: set[str] = set()
    for relative, digest in migration_artifacts.items():
        if type(relative) is not str or type(digest) is not str:
            raise PackageError("phase8b_migration_receipt_invalid")
        package_relative = (
            relative
            if relative.startswith("ops/")
            else "governed-memory-migrations/" + relative
        )
        if observed.get(package_relative) != digest:
            raise PackageError("phase8b_migration_package_binding_invalid")
        package_migration_artifacts.add(package_relative)
    if package_migration_artifacts != EXPECTED_MIGRATION_ARTIFACTS:
        raise PackageError("phase8b_migration_package_binding_invalid")
    return receipt


def _verify_manifest(manifest: dict[str, object]) -> dict[str, str]:
    if set(manifest) != {"schema_version", "state", "artifacts"}:
        raise PackageError("phase8b_package_manifest_shape_invalid")
    if (
        manifest.get("schema_version")
        != "governed-memory-phase8b-inactive-execution-package-manifest-v2"
        or manifest.get("state")
        != (
            "inactive_execution_and_synthetic_proof_harness_packaged_proof_"
            "pending_not_staged_not_installed_not_authorized"
        )
    ):
        raise PackageError("phase8b_package_manifest_identity_invalid")
    artifacts = manifest.get("artifacts")
    if type(artifacts) is not dict or set(artifacts) != EXPECTED_ARTIFACTS:
        raise PackageError("phase8b_package_artifact_set_invalid")
    if any(
        marker in relative
        for relative in artifacts
        for marker in FORBIDDEN_ARTIFACT_MARKERS
    ):
        raise PackageError("phase8b_package_forbidden_artifact")
    observed: dict[str, str] = {}
    for relative, wanted in artifacts.items():
        if (
            type(relative) is not str
            or type(wanted) is not str
            or HASH_RE.fullmatch(wanted) is None
        ):
            raise PackageError("phase8b_package_artifact_entry_invalid")
        actual = artifact_sha256(relative)
        if actual != wanted:
            raise PackageError("phase8b_package_hash_mismatch:" + relative)
        observed[relative] = actual
    return observed


def verify() -> dict[str, object]:
    manifest_raw = _read_path(MANIFEST)
    manifest = _parse_json(manifest_raw)
    observed = _verify_manifest(manifest)
    contract = _load_verified_json(
        CONTRACT_RELATIVE,
        observed[CONTRACT_RELATIVE],
    )
    plan = _load_verified_json(
        PLAN_RELATIVE,
        observed[PLAN_RELATIVE],
    )
    _verify_contract(contract)
    _verify_plan(plan)
    _verify_extension_contracts(observed)

    controller = _load_verified_controller_module(
        CONTROLLER_MODEL_RELATIVE,
        expected_sha256=EXPECTED_CONTROLLER_SOURCE_SHA256,
    )
    controller_model_sha256 = _verify_controller_binding(plan, controller)
    migration_verifier = _load_verified_module(
        MIGRATION_VERIFIER_RELATIVE,
        "_phase8b_verified_migration_manifest",
        expected_sha256=EXPECTED_MIGRATION_VERIFIER_SOURCE_SHA256,
    )
    migration_receipt = _verify_migration_binding(observed, migration_verifier)

    return {
        "schema_version": "governed-memory-phase8b-package-verification-v3",
        "state": str(manifest["state"]),
        "artifact_count": len(observed),
        "artifact_sha256": dict(sorted(observed.items())),
        "package_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "contract_canonical_sha256": _canonical_sha256(contract),
        "plan_canonical_sha256": _canonical_sha256(plan),
        "execution_contract_canonical_sha256": (
            EXPECTED_EXECUTION_CONTRACT_CANONICAL_SHA256
        ),
        "controller_runtime_contract_canonical_sha256": (
            EXPECTED_CONTROLLER_RUNTIME_CONTRACT_CANONICAL_SHA256
        ),
        "proof_contract_canonical_sha256": (
            EXPECTED_PROOF_CONTRACT_CANONICAL_SHA256
        ),
        "proof_schema_canonical_sha256": EXPECTED_PROOF_SCHEMA_CANONICAL_SHA256,
        "controller_source_sha256": observed[CONTROLLER_MODEL_RELATIVE],
        "controller_model_sha256": controller_model_sha256,
        "migration_verifier_source_sha256": observed[MIGRATION_VERIFIER_RELATIVE],
        "migration_manifest_sha256": migration_receipt["manifest_sha256"],
        "durable_journal_adapter_packaged": True,
        "guarded_synthetic_proof_harness_packaged": True,
        "synthetic_proof_executed_by_verifier": False,
        "synthetic_proof_receipt_promoted": False,
        "generic_docker_host_runner_primitive_packaged": True,
        "local_image_inspect_adapter_packaged": True,
        "claim_bound_installation_runner_composition_packaged": False,
        "claim_bound_installation_store_effect_adapters_packaged": False,
        "installation_executor_packaged": False,
        "rollback_executor_packaged": False,
        "activation_executor_packaged": False,
        "stores_supervisor_cli_packaged": True,
        "stores_supervisor_cli_docker_surface": list(
            EXPECTED_LIVE_EXECUTION["stores_supervisor_cli_docker_surface"]
        ),
        "installation_performed_by_verifier": False,
        "images_staged_by_verifier": False,
        "secrets_touched_by_verifier": False,
        "activation_performed_by_verifier": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("verify-package",))
    arguments = parser.parse_args(argv)
    try:
        receipt = verify()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print("PHASE8B_PACKAGE_INVALID=" + str(error))
        return 1
    if arguments.command == "verify-package":
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

"""Offline verifier for the current inactive dormant-store installation execution package.

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
CURRENT: Final = INSTALLATION / "current"
MANIFEST: Path = CURRENT / "package_manifest.json"
CONTRACT: Final = CURRENT / "contract.json"
PLAN: Final = CURRENT / "controller_plan.json"
HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)

MANIFEST_RELATIVE: Final = (
    "ops/governed_memory/installation/current/package_manifest.json"
)
CONTRACT_RELATIVE: Final = "ops/governed_memory/installation/current/contract.json"
PLAN_RELATIVE: Final = (
    "ops/governed_memory/installation/current/controller_plan.json"
)
MIGRATION_MANIFEST_RELATIVE: Final = (
    "ops/governed_memory/installation/current/migration_manifest.json"
)
MIGRATION_BINDINGS_RELATIVE: Final = (
    "ops/governed_memory/installation/current/postgres/migration_bindings.json"
)
MIGRATION_VERIFIER_RELATIVE: Final = (
    "tools/governed_memory_validation/verify_store_migration_manifest.py"
)
CONTROLLER_MODEL_RELATIVE: Final = (
    "tools/governed_memory_install/controller.py"
)
EXECUTION_CONTRACT_RELATIVE: Final = (
    "ops/governed_memory/installation/current/execution_contract.json"
)
CONTROLLER_RUNTIME_CONTRACT_RELATIVE: Final = (
    "ops/governed_memory/installation/current/controller_runtime_contract.json"
)
POSTGRES_SOURCE_CLOSURE_RELATIVE: Final = (
    "ops/governed_memory/installation/current/postgres/source_closure_contract.json"
)
POSTGRES_NATIVE_STAGE_CONTRACT_RELATIVE: Final = (
    "ops/governed_memory/installation/current/postgres/native_stage_contract.json"
)
PROOF_CONTRACT_RELATIVE: Final = (
    "ops/governed_memory/installation/current/disposable_proof_contract.json"
)
PROOF_SCHEMA_RELATIVE: Final = (
    "ops/governed_memory/installation/current/disposable_proof_receipt.schema.json"
)
INSTALL_RECEIPT_SCHEMA_RELATIVE: Final = (
    "ops/governed_memory/installation/current/install_receipt.schema.json"
)
EMPTY_ROLLBACK_RECEIPT_SCHEMA_RELATIVE: Final = (
    "ops/governed_memory/installation/current/empty_rollback_receipt.schema.json"
)

EXPECTED_CONTRACT_CANONICAL_SHA256: Final = (
    "beb415b93d08221ab71917b6a2588c7d8ad3ac2d8db4dde3698ff13b0b8c5bed"
)
EXPECTED_PLAN_CANONICAL_SHA256: Final = (
    "d6846be50c1bab2657f831be699a4653defa933dbaee88e1976d9120e341de9e"
)
EXPECTED_CONTROLLER_SOURCE_SHA256: Final = (
    "5a18628c85aab814360667341f685f809ac240b484a5fe6e10c727f54a752de5"
)
EXPECTED_CONTROLLER_MODEL_SHA256: Final = (
    "d3701a21b827da66122906e1dcc2828ce69f06e1048164df1c4d0ca0321c3de8"
)
EXPECTED_EXECUTION_CONTRACT_CANONICAL_SHA256: Final = (
    "1bd4642cc512367c02c2d9282a93397a2230a8770868dae80bab90519993a53f"
)
EXPECTED_CONTROLLER_RUNTIME_CONTRACT_CANONICAL_SHA256: Final = (
    "c3496ae3baa613870937a542783bdd94b1fe95320c4c461a4a5815f83161c08f"
)
EXPECTED_POSTGRES_SOURCE_CLOSURE_CANONICAL_SHA256: Final = (
    "1d4429f46aecee0ddbc348c952d767b0dbe870ecb9bee8be51819de4e312b295"
)
EXPECTED_POSTGRES_NATIVE_STAGE_CONTRACT_CANONICAL_SHA256: Final = (
    "7ed91be76c6296d8fbb15457f327e2de11c78957c1ba1abd68d9a978c4c4cdf3"
)
EXPECTED_PROOF_CONTRACT_CANONICAL_SHA256: Final = (
    "2722d7b274cdab2a4c8e352277b4d28c0c6f96432d4525d8f990b05e891b548a"
)
EXPECTED_PROOF_SCHEMA_CANONICAL_SHA256: Final = (
    "5fa4b98974b7c1652c931abd7b630bbca11fd92fde15ee4d099ae6132f3dfe1d"
)
EXPECTED_INSTALL_RECEIPT_SCHEMA_CANONICAL_SHA256: Final = (
    "6a76bcf802ba72257bb5fa524010de5182f85c6dbee2b490d46f92faf159a395"
)
EXPECTED_EMPTY_ROLLBACK_RECEIPT_SCHEMA_CANONICAL_SHA256: Final = (
    "add4d34411204c87ea321dfecb0c405c7223e1c01155090b56ea207347188aa6"
)
EXPECTED_MIGRATION_VERIFIER_SOURCE_SHA256: Final = (
    "6cc1b361363b0ef2299310b236bd4cef62a792ff211b43349cfa41fbd59ddd9b"
)
EXPECTED_MIGRATION_BINDINGS_CANONICAL_SHA256: Final = (
    "f6229283ff196e7355294f99321b92f350c4ec7909744544e9b1941e13100b42"
)
EXPECTED_CONTROLLER_STEP_IDS: Final = (
    "I01_REVERIFY_PRECLAIMED_EXECUTION_LOCK",
    "I02_VERIFY_CLAIMED_EXECUTION_BINDING",
    "I03_VERIFY_LIVE_PREFLIGHT",
    "I04_WRITE_RESOLVED_STORE_SPEC_AND_GENERATE_FRESH_STORE_SECRETS",
    "I05_CREATE_EXACT_NETWORK",
    "I06_CREATE_EXACT_POSTGRES_VOLUME",
    "I07_CREATE_EXACT_QDRANT_VOLUME",
    "I08_CREATE_EXACT_POSTGRES_CONTAINER",
    "I09_CREATE_EXACT_QDRANT_CONTAINER",
    "I10_START_AND_VERIFY_EMPTY_STORES",
    "I11_BOOTSTRAP_CANONICAL_DATABASE",
    "I12_APPLY_FOUNDATION_0001",
    "I13_APPLY_OWNER_CLAIM_DETAIL_0003",
    "I14_APPLY_PILOT_MARKER_0004",
    "I15_CREATE_EMPTY_QDRANT_COLLECTION",
    "I16_CREATE_QDRANT_ALIAS",
    "I17_VERIFY_PRE_SUPERVISOR_RESOURCE_IDENTITIES",
    "I18_INSTALL_AND_ENABLE_STORES_SUPERVISOR",
    "I19_COLD_RESTART_AND_VERIFY_TERMINAL_POSTFLIGHT",
)
EXPECTED_EMPTY_ROLLBACK_STEP_IDS: Final = (
    "R01_REVERIFY_GLOBAL_LOCK",
    "R02_VERIFY_CLAIMED_ROLLBACK_AUTHORITY",
    "R03_VERIFY_INSTALL_RECEIPT_AND_LEDGER",
    "R04_ACQUIRE_ROLLBACK_CONTROLLER_AUTHORITY_MARKER",
    "R05_RECHECK_SEMANTIC_EMPTY_UNDER_CONTROLLER_AUTHORITY_MARKER",
    "R06_DISABLE_AND_REMOVE_STORES_SUPERVISOR",
    "R07_STOP_EXACT_STORES",
    "R08_REMOVE_EXACT_QDRANT_CONTAINER",
    "R09_REMOVE_EXACT_POSTGRES_CONTAINER",
    "R10_REMOVE_EXACT_EMPTY_QDRANT_VOLUME",
    "R11_REMOVE_EXACT_EMPTY_POSTGRES_VOLUME",
    "R12_REMOVE_EXACT_UNUSED_NETWORK",
    "R13_REMOVE_FRESH_QDRANT_STORE_SECRET",
    "R14_REMOVE_FRESH_POSTGRES_STORE_SECRET",
    "R15_REMOVE_RESOLVED_STORE_SPEC",
    "R16_VERIFY_QDRANT_ALIAS_PHYSICALLY_ABSENT",
    "R17_VERIFY_QDRANT_COLLECTION_PHYSICALLY_ABSENT",
    "R18_VERIFY_PILOT_MARKER_0004_PHYSICALLY_ABSENT",
    "R19_VERIFY_OWNER_CLAIM_DETAIL_0003_PHYSICALLY_ABSENT",
    "R20_VERIFY_FOUNDATION_0001_PHYSICALLY_ABSENT",
    "R21_VERIFY_CANONICAL_DATABASE_AND_ROLES_PHYSICALLY_ABSENT",
    "R22_VERIFY_EXACT_ABSENCE_AND_RETAIN_AUDIT",
)
EXPECTED_POSTGRES_SOURCE_PHASE_IDS: Final = (
    "F01_CANONICAL_CLUSTER_BOOTSTRAP_TRANSLATION",
    "F02_OWNER_ROLE_PREFLIGHT_AND_MIGRATIONS",
    "T01_INDEPENDENT_TERMINAL_CATALOG",
    "R01_EXACT_EMPTY_ROLLBACK_PREFIX_MACHINE",
)
EXPECTED_POSTGRES_NATIVE_STAGE_IDS: Final = (
    "F01_PREBOOTSTRAP_AND_CREATE_DATABASE",
    "F02_ROLES_PRIVACY_AND_MIGRATIONS",
    "T01_TERMINAL_EXACT_CATALOG",
    "R01_EMPTY_ROLLBACK_PREFIX_RESUME",
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
        "receipt_policy",
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
        "empty_rollback_steps",
        "later_rollback",
        "live_execution",
        "synthetic_proof",
    }
)
EXPECTED_LIVE_EXECUTION: Final = {
    "claim_bound_install_controller_composition_packaged": True,
    "non_cli_install_entrypoint_packaged": True,
    "claim_bound_empty_rollback_controller_composition_packaged": True,
    "non_cli_empty_rollback_entrypoint_packaged": True,
    "operation_specific_install_and_empty_rollback_receipts_packaged": True,
    "install_controller_emits_canonical_receipt": True,
    "empty_rollback_controller_emits_canonical_receipt": True,
    "typed_operation_specific_boundary_packaged": True,
    "generic_store_mutation_argv_surface_packaged": False,
    "closed_install_store_effect_adapter_packaged": True,
    "closed_empty_rollback_store_effect_adapter_packaged": True,
    "closed_live_transport_contracts_packaged": True,
    "complete_closed_live_transport_substrate_set_packaged": False,
    "postgresql_source_closure_contract_packaged": True,
    "driver_native_postgresql_stage_contract_packaged": True,
    "driver_native_postgresql_executable_stage_machine_packaged": True,
    "concrete_psycopg_postgresql_transport_packaged": False,
    "runtime_input_selection_contract_repaired": True,
    "runtime_build_receipt_provenance_v3_packaged": True,
    "complete_closed_live_transport_substrate_set_integrated_into_bound_factory": False,
    "approved_terminal_postgresql_catalog_manifest_selected": False,
    "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required": True,
    "durable_live_empty_rollback_controller_authority_marker_transport_packaged": True,
    "external_direct_writer_exclusion_implemented": False,
    "stopped_store_semantic_empty_recheck_is_valid": False,
    "selected_live_platform_transports_packaged": False,
    "selected_non_postgresql_live_platform_transport_factory_packaged": True,
    "pinned_postgresql_driver_selected": False,
    "durable_create_once_receipt_store_packaged": True,
    "public_entrypoints_require_canonical_root_owned_production_receipt_store": True,
    "synthetic_receipt_stores_are_private_test_only": True,
    "controller_runtime_release_builder_orchestration_packaged": True,
    "controller_runtime_build_transport_packaged": False,
    "controller_runtime_publication_policy_transport_packaged": True,
    "production_runtime_publication_primitives_packaged": False,
    "activation_entrypoint_packaged": False,
    "bounded_image_inspect_runner_primitive_packaged": True,
    "local_image_inspect_adapter_packaged": True,
    "controller_runtime_verification_capability_packaged": True,
    "full_controller_release_tree_verification_packaged": True,
    "exact_locked_controller_distribution_set_verification_packaged": True,
    "full_release_tree_sha256_bound_through_claim_journal_host_ownership_and_install_receipt": True,
    "empty_rollback_full_runtime_and_release_identity_bound_through_authority_claim_journal_requests_observations_controller_authority_marker_and_receipt": True,
    "controller_release_and_runtime_require_separate_future_build_and_install_authority": True,
    "supervisor_launcher_source_packaged": True,
    "controller_runtime_built_or_installed": False,
    "controller_release_staged": False,
    "stores_install_owns_or_removes_controller_substrate": False,
    "stores_supervisor_cli_packaged": True,
    "stores_supervisor_cli_docker_surface": [
        "container_inspect",
        "container_start",
        "container_stop",
    ],
    "stores_supervisor_is_installer_or_rollback_adapter": False,
    "stores_supervisor_requires_preexisting_exact_ledger_container_ids": True,
}
EXPECTED_MIGRATION_ARTIFACTS: Final = frozenset(
    {
        MIGRATION_BINDINGS_RELATIVE,
        "ops/governed_memory/installation/current/postgres/roles_preflight.pgsql",
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
        "ops/governed_memory/installation/current/contract.json",
        "ops/governed_memory/installation/current/controller_plan.json",
        "ops/governed_memory/installation/current/controller_runtime_contract.json",
        "ops/governed_memory/installation/current/disposable_proof_contract.json",
        "ops/governed_memory/installation/current/disposable_proof_receipt.schema.json",
        "ops/governed_memory/installation/current/install_receipt.schema.json",
        "ops/governed_memory/installation/current/empty_rollback_receipt.schema.json",
        "ops/governed_memory/installation/current/execution_contract.json",
        "ops/governed_memory/installation/current/migration_manifest.json",
        "ops/governed_memory/installation/current/postgres/migration_bindings.json",
        POSTGRES_SOURCE_CLOSURE_RELATIVE,
        POSTGRES_NATIVE_STAGE_CONTRACT_RELATIVE,
        "ops/governed_memory/installation/current/postgres/roles_preflight.pgsql",
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
        "tools/governed_memory_install/controller_runtime.py",
        "tools/governed_memory_install/durable_receipts.py",
        "tools/governed_memory_install/disposable_proof_harness.py",
        "tools/governed_memory_install/journal.py",
        "tools/governed_memory_install/execution_authority.py",
        "tools/governed_memory_install/execution_capability.py",
        "tools/governed_memory_install/execution_lock.py",
        "tools/governed_memory_install/host_boundary.py",
        "tools/governed_memory_install/image_preflight.py",
        "tools/governed_memory_install/install_backend.py",
        "tools/governed_memory_install/install_entrypoint.py",
        "tools/governed_memory_install/linux_store_effects.py",
        "tools/governed_memory_install/linux_live_transports.py",
        "tools/governed_memory_install/linux_live_adapters.py",
        "tools/governed_memory_install/linux_store_readiness.py",
        "tools/governed_memory_install/linux_plan.py",
        "tools/governed_memory_install/package.py",
        "tools/governed_memory_install/package_capability.py",
        "tools/governed_memory_install/live_rollback_marker.py",
        "tools/governed_memory_install/postgres_native_stages.py",
        "tools/governed_memory_install/postgres_source_closure.py",
        "tools/governed_memory_install/receipts.py",
        "tools/governed_memory_install/resource_identity.py",
        "tools/governed_memory_install/rollback.py",
        "tools/governed_memory_install/rollback_authority.py",
        "tools/governed_memory_install/rollback_entrypoint.py",
        "tools/governed_memory_install/rollback_journal.py",
        "tools/governed_memory_install/rollback_live_adapter.py",
        "tools/governed_memory_install/secure_file.py",
        "tools/governed_memory_install/store_supervisor.py",
        "tools/governed_memory_install/store_readiness.py",
        "tools/governed_memory_install/store_supervisor_launcher.py",
        "tools/governed_memory_install/synthetic_backend.py",
        "tools/governed_memory_release/__init__.py",
        "tools/governed_memory_release/controller_runtime_builder.py",
        "tools/governed_memory_release/runtime_publication_transport.py",
        "tools/governed_memory_validation/generate_installation_package_manifest.py",
        "tools/governed_memory_validation/run_installation_synthetic_proof.py",
        "tools/governed_memory_validation/verify_store_migration_manifest.py",
    }
)

EXPECTED_INSTALLATION_DIRECTORY_FILES: Final = frozenset(
    {MANIFEST_RELATIVE}
    | {
        relative
        for relative in EXPECTED_ARTIFACTS
        if relative.startswith("ops/governed_memory/installation/")
    }
)
EXPECTED_INSTALL_TOOL_DIRECTORY_FILES: Final = frozenset(
    relative
    for relative in EXPECTED_ARTIFACTS
    if relative.startswith("tools/governed_memory_install/")
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
            raise PackageError("dormant_store_install_package_duplicate_json_key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise _NonFiniteJsonValue(value)


def _relative_parts(relative: object) -> tuple[str, ...]:
    if type(relative) is not str or not relative or "\\" in relative:
        raise PackageError("dormant_store_install_package_artifact_path_invalid")
    pure = PurePosixPath(relative)
    parts = pure.parts
    if (
        pure.is_absolute()
        or not parts
        or str(pure) != relative
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise PackageError("dormant_store_install_package_artifact_path_invalid")
    return parts


def _repository_regular_file_inventory(relative_root: str) -> frozenset[str]:
    """Return a no-symlink, regular-file-only inventory below one directory."""

    parts = _relative_parts(relative_root)
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | _nofollow()
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    descriptors: list[int] = []
    observed: set[str] = set()

    def visit(directory_fd: int, prefix: PurePosixPath) -> None:
        try:
            entries = sorted(os.scandir(directory_fd), key=lambda entry: entry.name)
        except OSError as error:
            raise PackageError(
                "dormant_store_install_package_directory_inventory_invalid"
            ) from error
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise PackageError(
                    "dormant_store_install_package_directory_inventory_invalid"
                ) from error
            member = prefix / entry.name
            if stat.S_ISREG(info.st_mode):
                observed.add(member.as_posix())
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise PackageError(
                    "dormant_store_install_package_directory_inventory_invalid"
                )
            try:
                child_fd = os.open(
                    entry.name,
                    directory_flags,
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise PackageError(
                    "dormant_store_install_package_directory_inventory_invalid"
                ) from error
            try:
                visit(child_fd, member)
            finally:
                os.close(child_fd)

    try:
        directory_fd = os.open(ROOT, directory_flags)
        descriptors.append(directory_fd)
        for part in parts:
            directory_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            descriptors.append(directory_fd)
        visit(directory_fd, PurePosixPath(relative_root))
        return frozenset(observed)
    except OSError as error:
        raise PackageError(
            "dormant_store_install_package_directory_inventory_invalid"
        ) from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _verify_current_directory_closure() -> None:
    if (
        _repository_regular_file_inventory(
            "ops/governed_memory/installation"
        )
        != EXPECTED_INSTALLATION_DIRECTORY_FILES
        or _repository_regular_file_inventory("tools/governed_memory_install")
        != EXPECTED_INSTALL_TOOL_DIRECTORY_FILES
    ):
        raise PackageError(
            "dormant_store_install_package_directory_closure_invalid"
        )


def _nofollow() -> int:
    flag = getattr(os, "O_NOFOLLOW", 0)
    if flag == 0:
        raise PackageError("dormant_store_install_package_nofollow_unavailable")
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
            raise PackageError("dormant_store_install_package_artifact_path_invalid")
        chunks: list[bytes] = []
        while True:
            block = os.read(file_fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    except OSError as error:
        raise PackageError("dormant_store_install_package_artifact_path_invalid") from error
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
            raise PackageError("dormant_store_install_package_document_path_invalid") from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise PackageError("dormant_store_install_package_document_path_invalid")
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
        raise PackageError("dormant_store_install_package_json_invalid") from error
    if type(value) is not dict:
        raise PackageError("dormant_store_install_package_json_root_invalid")
    return value


def _load(path: Path) -> dict[str, object]:
    return _parse_json(_read_path(path))


def _load_verified_json(relative: str, expected_sha256: str) -> dict[str, object]:
    raw = _read_repository_file(relative)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PackageError("dormant_store_install_package_member_changed_after_hash")
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
        raise PackageError("dormant_store_install_package_json_invalid") from error
    return hashlib.sha256(encoded).hexdigest()


def artifact_sha256(relative: str) -> str:
    return hashlib.sha256(_read_repository_file(relative)).hexdigest()


def _verify_contract(contract: dict[str, object]) -> None:
    if set(contract) != EXPECTED_CONTRACT_KEYS:
        raise PackageError("dormant_store_install_contract_shape_invalid")
    if _canonical_sha256(contract) != EXPECTED_CONTRACT_CANONICAL_SHA256:
        raise PackageError("dormant_store_install_contract_semantics_invalid")
    if (
        contract.get("schema_version")
        != "governed-memory-dormant-store-install-inactive-execution-contract-v4"
        or contract.get("state")
        != "phase9h-repository-only-bounded-transports-stage-machines-and-controller-marker-packaged-not-installed-not-activated"
        or contract.get("server") != "seebx"
    ):
        raise PackageError("dormant_store_install_contract_identity_invalid")
    scope = contract.get("scope")
    if (
        type(scope) is not dict
        or scope.get("current_phase_repository_only") is not True
        or any(
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
        )
    ):
        raise PackageError("dormant_store_install_contract_authority_boundary_invalid")
    migration = contract.get("migration_policy")
    if (
        type(migration) is not dict
        or migration.get("excluded_migrations") != ["0002_conversation_bridge"]
        or migration.get("postgresql_source_closure_contract")
        != POSTGRES_SOURCE_CLOSURE_RELATIVE
        or migration.get("postgresql_source_closure_contract_packaged")
        is not True
        or migration.get("driver_native_executable_stage_contract_packaged")
        is not True
        or migration.get("concrete_psycopg_postgresql_transport_packaged")
        is not False
        or migration.get("host_psql_direct_execution_allowed") is not False
        or migration.get("preferred_synchronous_driver_target")
        != "psycopg[binary]==3.3.4"
        or migration.get("preferred_synchronous_driver_locked_staged_and_verified")
        is not False
        or migration.get("approved_terminal_catalog_manifest_selected")
        is not False
        or migration.get("source_postgresql_connections") != 0
        or migration.get("source_postgresql_reads") != 0
        or migration.get("source_postgresql_writes") != 0
    ):
        raise PackageError("dormant_store_install_contract_migration_boundary_invalid")
    secret = contract.get("secret_policy")
    if (
        type(secret) is not dict
        or secret.get(
            "secret_file_public_identity_binds_fixed_path_and_claim_execution_id"
        )
        is not True
        or secret.get(
            "secret_file_first_line_contains_nonsecret_execution_id_marker"
        )
        is not True
        or secret.get("prior_execution_secret_file_is_exact_for_new_execution")
        is not False
        or secret.get("legacy_pilot_env_may_be_read_stat_hashed_renamed_or_deleted")
        is not False
    ):
        raise PackageError("dormant_store_install_contract_secret_boundary_invalid")
    authority = contract.get("authority_policy")
    recovery = contract.get("recovery_policy")
    identity = contract.get("identity_policy")
    supervisor = contract.get("supervisor_policy")
    receipts = contract.get("receipt_policy")
    blockers = contract.get("remaining_blockers")
    if (
        type(authority) is not dict
        or authority.get("opaque_verified_package_capability_required") is not True
        or authority.get(
            "opaque_verified_controller_runtime_capability_required"
        )
        is not True
        or authority.get(
            "signed_scope_binds_controller_runtime_receipt_sha256"
        )
        is not True
        or authority.get(
            "empty_rollback_signed_scope_binds_controller_runtime_receipt_sha256"
        )
        is not True
        or authority.get("opaque_claimed_execution_binding_required") is not True
        or authority.get("exact_controller_process_is_trusted") is not True
        or authority.get("hostile_same_process_capability_forgery_resisted")
        is not False
        or type(recovery) is not dict
        or recovery.get("same_open_instance_inode_and_directory_replacement_refused")
        is not True
        or recovery.get(
            "cross_process_same_content_inode_or_directory_replacement_refused"
        )
        is not True
        or recovery.get("one_final_partial_line_beyond_exact_anchor_may_be_truncated")
        is not True
        or recovery.get(
            "install_receipt_replay_requires_fresh_terminal_store_readiness_probe"
        )
        is not True
        or recovery.get(
            "empty_rollback_resume_revalidates_install_receipt_and_resource_ledger"
        )
        is not True
        or recovery.get(
            "completed_empty_rollback_replay_reproves_exact_resource_absence"
        )
        is not True
        or recovery.get("closed_live_transport_contracts_packaged") is not True
        or recovery.get("complete_closed_live_transport_substrate_set_packaged") is not False
        or recovery.get(
            "complete_closed_live_transport_substrate_set_integrated_into_bound_factory"
        )
        is not False
        or recovery.get(
            "postgresql_source_closure_contract_packaged"
        )
        is not True
        or recovery.get("driver_native_postgresql_stage_contract_packaged")
        is not True
        or recovery.get(
            "driver_native_postgresql_executable_stage_machine_packaged"
        )
        is not True
        or recovery.get("concrete_psycopg_postgresql_transport_packaged")
        is not False
        or recovery.get("controller_runtime_input_selection_contract_repaired")
        is not True
        or recovery.get(
            "controller_runtime_build_receipt_provenance_v3_packaged"
        )
        is not True
        or recovery.get(
            "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required"
        )
        is not True
        or recovery.get(
            "approved_terminal_postgresql_catalog_manifest_selected"
        )
        is not False
        or recovery.get(
            "empty_rollback_controller_authority_marker_held_from_r04_through_receipt"
        )
        is not True
        or recovery.get(
            "fresh_live_semantic_empty_recheck_required_at_r05_under_controller_authority_marker"
        )
        is not True
        or recovery.get(
            "destructive_rollback_steps_require_held_controller_authority_marker_and_persisted_r05_empty_proof"
        )
        is not True
        or recovery.get("stopped_store_semantic_empty_recheck_is_valid")
        is not False
        or recovery.get("durable_live_rollback_controller_authority_marker_transport_packaged")
        is not True
        or recovery.get("external_direct_writer_exclusion_implemented")
        is not False
        or recovery.get(
            "controller_authority_marker_empty_recheck_then_stop_then_physical_removal_order_implemented"
        )
        is not True
        or recovery.get(
            "retained_audit_artifact_hashes_bound_to_rollback_receipt"
        )
        is not True
        or recovery.get(
            "empty_rollback_claim_journal_requests_observations_controller_authority_marker_and_receipt_bind_verified_runtime_and_full_release_tree"
        )
        is not True
        or recovery.get("selected_live_platform_transport_factory_packaged")
        is not False
        or recovery.get(
            "selected_non_postgresql_live_platform_transport_factory_packaged"
        )
        is not True
        or type(identity) is not dict
        or identity.get("resource_identity_ledger_requires_global_lock") is not True
        or identity.get("resource_identity_ledger_has_durable_compare_and_set_anchor")
        is not True
        or identity.get(
            "resource_identity_ledger_has_cross_process_filesystem_identity_guard"
        )
        is not True
        or identity.get("rollback_targets_are_derived_from_verified_ledger_records")
        is not True
        or identity.get(
            "empty_rollback_requires_opaque_install_receipt_and_ledger_capability"
        )
        is not True
        or identity.get(
            "resolved_store_spec_binds_execution_authority_and_exact_docker_labels"
        )
        is not True
        or identity.get(
            "resource_ledger_separates_ownership_and_resource_label_hashes"
        )
        is not True
        or type(supervisor) is not dict
        or supervisor.get(
            "unit_template_requires_exact_package_manifest_runtime_receipt_and_execution_id_rendering"
        )
        is not True
        or supervisor.get("unit_template_uses_isolated_runtime_python") is not True
        or supervisor.get("unit_template_disables_user_site_and_bytecode_writes")
        is not True
        or supervisor.get(
            "unit_template_uses_exact_release_launcher_not_module_search"
        )
        is not True
        or type(receipts) is not dict
        or receipts.get("install_receipt_schema_packaged") is not True
        or receipts.get("empty_rollback_receipt_schema_packaged") is not True
        or receipts.get("empty_rollback_controller_emits_canonical_receipt")
        is not True
        or receipts.get("install_controller_emits_canonical_receipt") is not True
        or receipts.get(
            "install_receipt_binds_fresh_terminal_canonical_store_readiness"
        )
        is not True
        or receipts.get(
            "install_receipt_binds_verified_controller_runtime_identity"
        )
        is not True
        or receipts.get(
            "empty_rollback_receipt_binds_verified_install_receipt_and_exact_ledger"
        )
        is not True
        or receipts.get(
            "empty_rollback_receipt_binds_verified_runtime_and_full_release_tree_identity"
        )
        is not True
        or receipts.get(
            "empty_rollback_authority_claim_precedes_durable_receipt_reads_and_eligibility_persistence"
        )
        is not True
        or receipts.get(
            "empty_rollback_receipt_persisted_create_once_while_controller_authority_marker_held"
        )
        is not True
        or receipts.get(
            "empty_rollback_receipt_persistence_create_once_while_controller_authority_marker_held_required"
        )
        is not True
        or receipts.get("canonical_production_executions_root")
        != "/var/lib/governed-memory-controller/executions"
        or receipts.get(
            "public_install_and_rollback_entrypoints_require_root_owned_production_receipt_store"
        )
        is not True
        or receipts.get(
            "synthetic_receipt_stores_accepted_only_by_private_in_process_test_wrappers"
        )
        is not True
        or type(blockers) is not list
        or "pinned_postgresql_driver_not_selected" not in blockers
        or "concrete_psycopg_postgresql_transport_and_complete_bound_live_transport_factory_not_packaged"
        not in blockers
        or "production_runtime_publication_primitives_not_packaged" not in blockers
        or "external_direct_writer_exclusion_not_implemented_controller_marker_only"
        not in blockers
        or "approved_terminal_postgresql_catalog_manifest_not_selected"
        not in blockers
        or "canonical_install_receipt_emission_not_integrated" in blockers
    ):
        raise PackageError("dormant_store_install_contract_package_claim_boundary_invalid")


def _verify_plan(plan: dict[str, object]) -> None:
    if set(plan) != EXPECTED_PLAN_KEYS:
        raise PackageError("dormant_store_install_plan_shape_invalid")
    if _canonical_sha256(plan) != EXPECTED_PLAN_CANONICAL_SHA256:
        raise PackageError("dormant_store_install_plan_semantics_invalid")
    if (
        plan.get("schema_version")
        != "governed-memory-dormant-store-install-stores-controller-plan-v5"
        or plan.get("state")
        != "phase9h-repository-only-non-postgresql-live-transports-postgresql-stage-machine-runtime-publication-policy-and-rollback-marker-packaged-not-installed-not-authorized"
        or plan.get("server") != "seebx"
    ):
        raise PackageError("dormant_store_install_plan_identity_invalid")
    steps = plan.get("install_steps")
    if type(steps) is not list or len(steps) != len(EXPECTED_CONTROLLER_STEP_IDS):
        raise PackageError("dormant_store_install_plan_step_count_invalid")
    observed_ids: list[str] = []
    for expected_id, step in zip(EXPECTED_CONTROLLER_STEP_IDS, steps, strict=True):
        if type(step) is not dict or set(step) != {"id", "effect", "rollback"}:
            raise PackageError("dormant_store_install_plan_step_shape_invalid")
        if step.get("id") != expected_id:
            raise PackageError("dormant_store_install_plan_step_order_invalid")
        if any(
            type(step.get(key)) is not str or not step.get(key)
            for key in ("effect", "rollback")
        ):
            raise PackageError("dormant_store_install_plan_step_shape_invalid")
        observed_ids.append(expected_id)
    if tuple(observed_ids) != EXPECTED_CONTROLLER_STEP_IDS:
        raise PackageError("dormant_store_install_plan_step_order_invalid")
    rollback_steps = plan.get("empty_rollback_steps")
    if (
        type(rollback_steps) is not list
        or len(rollback_steps) != len(EXPECTED_EMPTY_ROLLBACK_STEP_IDS)
    ):
        raise PackageError("dormant_store_install_rollback_plan_step_count_invalid")
    for expected_id, step in zip(
        EXPECTED_EMPTY_ROLLBACK_STEP_IDS,
        rollback_steps,
        strict=True,
    ):
        if (
            type(step) is not dict
            or set(step) != {"id", "operation", "resource_key", "invariant_only"}
            or step.get("id") != expected_id
            or type(step.get("operation")) is not str
            or not step.get("operation")
            or (
                step.get("resource_key") is not None
                and (
                    type(step.get("resource_key")) is not str
                    or not step.get("resource_key")
                )
            )
            or type(step.get("invariant_only")) is not bool
        ):
            raise PackageError("dormant_store_install_rollback_plan_step_invalid")
    live = plan.get("live_execution")
    if live != EXPECTED_LIVE_EXECUTION:
        raise PackageError("dormant_store_install_plan_live_surface_invalid")
    invariants = plan.get("execution_invariants")
    later_rollback = plan.get("later_rollback")
    if (
        type(invariants) is not dict
        or invariants.get("prebootstrap_fresh_empty_readiness_probe_required")
        is not True
        or invariants.get(
            "fresh_terminal_canonical_store_readiness_probe_required_for_install_receipt_and_replay"
        )
        is not True
        or invariants.get(
            "fresh_live_semantic_empty_recheck_required_at_r05_under_controller_authority_marker"
        )
        is not True
        or invariants.get("closed_live_transport_contracts_packaged") is not True
        or invariants.get("complete_closed_live_transport_substrate_set_packaged") is not False
        or invariants.get(
            "complete_closed_live_transport_substrate_set_integrated_into_bound_factory"
        )
        is not False
        or invariants.get(
            "postgresql_source_closure_contract_packaged"
        )
        is not True
        or invariants.get("driver_native_postgresql_stage_contract_packaged")
        is not True
        or invariants.get(
            "driver_native_postgresql_executable_stage_machine_packaged"
        )
        is not True
        or invariants.get("concrete_psycopg_postgresql_transport_packaged")
        is not False
        or invariants.get(
            "controller_runtime_build_receipt_provenance_v3_packaged"
        )
        is not True
        or invariants.get(
            "approved_terminal_postgresql_catalog_manifest_selected"
        )
        is not False
        or invariants.get("durable_live_rollback_controller_authority_marker_transport_packaged")
        is not True
        or invariants.get("external_direct_writer_exclusion_implemented")
        is not False
        or invariants.get("selected_live_platform_transports_packaged")
        is not False
        or invariants.get(
            "selected_non_postgresql_live_platform_transport_factory_packaged"
        )
        is not True
        or type(later_rollback) is not dict
        or later_rollback.get(
            "opaque_verified_install_receipt_and_ledger_capability_required"
        )
        is not True
        or later_rollback.get(
            "resume_revalidates_verified_install_receipt_and_ledger"
        )
        is not True
        or later_rollback.get(
            "completed_replay_reproves_exact_resource_absence"
        )
        is not True
        or later_rollback.get(
            "rollback_authority_claim_precedes_durable_receipt_reads_and_eligibility_persistence"
        )
        is not True
        or later_rollback.get(
            "fresh_live_semantic_empty_recheck_required_before_first_destructive_step"
        )
        is not True
        or later_rollback.get(
            "controller_authority_marker_held_through_terminal_absence_retained_audit_and_receipt"
        )
        is not True
        or later_rollback.get(
            "destructive_steps_require_held_controller_authority_marker_and_persisted_r05_empty_proof"
        )
        is not True
        or later_rollback.get("stopped_store_semantic_empty_recheck_is_valid")
        is not False
        or later_rollback.get("durable_live_rollback_controller_authority_marker_transport_packaged")
        is not True
        or later_rollback.get("external_direct_writer_exclusion_implemented")
        is not False
        or later_rollback.get("selected_live_platform_driver_packaged")
        is not False
        or later_rollback.get(
            "selected_non_postgresql_live_platform_transport_factory_packaged"
        )
        is not True
    ):
        raise PackageError("dormant_store_install_plan_replay_boundary_invalid")


def _verify_postgres_source_closure(contract: dict[str, object]) -> None:
    if (
        _canonical_sha256(contract)
        != EXPECTED_POSTGRES_SOURCE_CLOSURE_CANONICAL_SHA256
        or contract.get("schema_version")
        != "governed-memory-postgres-source-closure-contract-v1"
        or contract.get("state")
        != "repository-only-source-closed-native-execution-blocked"
        or contract.get("server_version") != "16.14"
    ):
        raise PackageError("dormant_store_postgres_source_closure_contract_invalid")
    driver = contract.get("driver")
    endpoint = contract.get("endpoint")
    gate = contract.get("execution_gate")
    semantics = contract.get("semantic_equivalence_requirements")
    session = contract.get("session_contract")
    effects = contract.get("repository_phase_effect_counts")
    phases = contract.get("native_phases")
    if (
        type(driver) is not dict
        or driver.get("preferred_distribution") != "psycopg[binary]==3.3.4"
        or driver.get("preferred_driver_locked") is not False
        or driver.get("preferred_wheels_staged") is not False
        or driver.get("preferred_native_closure_inspected") is not False
        or type(endpoint) is not dict
        or endpoint.get("bind") != "127.0.0.1:55432"
        or endpoint.get("caller_selected_dsn_endpoint_database_or_path_allowed")
        is not False
        or type(gate) is not dict
        or gate.get("executable_stage_count") != 0
        or gate.get("driver_native_translation_complete") is not False
        or gate.get("rollback_prefix_machine_complete") is not False
        or gate.get("fixed_catalog_queries_and_normalization_complete") is not False
        or gate.get("approved_terminal_catalog_manifest_selected") is not False
        or gate.get("live_execution_must_refuse") is not True
        or gate.get("caller_supplied_sql_stage_tuple_or_expected_catalog_allowed")
        is not False
        or type(session) is not dict
        or session.get("session_user_fixed") != "governed_memory_bootstrap"
        or session.get("set_role_must_be_verified_per_phase") is not True
        or session.get("advisory_lock_acquired_before_first_observation") is not True
        or session.get("advisory_lock_held_through_terminal_or_rollback_receipt")
        is not True
        or type(semantics) is not dict
        or not all(value is True for value in semantics.values())
        or effects
        != {
            "postgresql_calls": 0,
            "provider_calls": 0,
            "production_reads": 0,
            "secret_reads_or_writes": 0,
        }
        or type(phases) is not list
        or tuple(
            phase.get("phase_id") if type(phase) is dict else None
            for phase in phases
        )
        != EXPECTED_POSTGRES_SOURCE_PHASE_IDS
        or any(
            type(phase) is not dict
            or phase.get("translation_complete") is not False
            for phase in phases
        )
    ):
        raise PackageError("dormant_store_postgres_source_closure_contract_invalid")
    unsigned = dict(contract)
    supplied = unsigned.pop("contract_sha256", None)
    if supplied != _canonical_sha256(unsigned):
        raise PackageError("dormant_store_postgres_source_closure_digest_invalid")


def _verify_postgres_native_stage_contract(contract: dict[str, object]) -> None:
    if (
        _canonical_sha256(contract)
        != EXPECTED_POSTGRES_NATIVE_STAGE_CONTRACT_CANONICAL_SHA256
        or contract.get("schema_version")
        != "governed-memory-postgres-native-stage-contract-v2"
        or contract.get("state")
        != "repository-only-orchestration-and-fixed-catalog-query-contract-packaged-concrete-transport-runtime-and-approved-catalog-unready"
        or contract.get("server_version") != "16.14"
    ):
        raise PackageError("dormant_store_postgres_native_stage_contract_invalid")
    endpoint = contract.get("endpoint")
    session = contract.get("session")
    preferred_driver = contract.get("preferred_driver")
    stages = contract.get("stages")
    catalog = contract.get("terminal_catalog")
    rollback_marker = contract.get("rollback_controller_marker")
    gate = contract.get("execution_gate")
    effects = contract.get("repository_phase_effect_counts")
    coverage = (
        catalog.get("security_sensitive_coverage")
        if type(catalog) is dict
        else None
    )
    if (
        type(endpoint) is not dict
        or endpoint.get("host") != "127.0.0.1"
        or endpoint.get("port") != 55432
        or endpoint.get("bootstrap_database") != "postgres"
        or endpoint.get("target_database") != "governed_memory"
        or endpoint.get("caller_dsn_host_port_database_role_or_path_allowed")
        is not False
        or type(session) is not dict
        or session.get(
            "session_lock_precedes_postgresql_endpoint_boundary_prefix_and_catalog_observations"
        )
        is not True
        or session.get("migration_transactions_required_by_adapter_contract")
        is not True
        or session.get("migration_transactions_implemented_by_concrete_adapter")
        is not False
        or session.get("set_role_owner_and_current_user_verification_required")
        is not True
        or session.get("set_role_owner_and_current_user_verification_implemented")
        is not False
        or type(preferred_driver) is not dict
        or preferred_driver.get("selection_state")
        != "preferred-family-and-version-only-no-wheel-selected-or-verified"
        or preferred_driver.get("python_version_target") != "3.12.13"
        or preferred_driver.get("api_style") != "synchronous"
        or preferred_driver.get("preferred_package") != "psycopg"
        or preferred_driver.get("preferred_extra") != "binary"
        or preferred_driver.get("preferred_version") != "3.3.4"
        or preferred_driver.get("required_distributions")
        != ["psycopg", "psycopg-binary"]
        or preferred_driver.get("selected_wheels") != []
        or preferred_driver.get("preference_contract_packaged") is not True
        or preferred_driver.get("exact_driver_identity_contract_packaged")
        is not False
        or preferred_driver.get("exact_wheel_filenames_frozen") is not False
        or preferred_driver.get("wheel_bytes_staged") is not False
        or preferred_driver.get("wheel_bytes_verified") is not False
        or preferred_driver.get("binary_native_library_closure_inspected")
        is not False
        or preferred_driver.get("runtime_receipt_selected") is not False
        or preferred_driver.get("ready") is not False
        or type(stages) is not list
        or tuple(
            stage.get("stage_id") if type(stage) is dict else None
            for stage in stages
        )
        != EXPECTED_POSTGRES_NATIVE_STAGE_IDS
        or type(catalog) is not dict
        or catalog.get("caller_expected_catalog_allowed") is not False
        or catalog.get("postgresql_parse_or_execution_proven") is not False
        or catalog.get("approved_manifest_selected") is not False
        or catalog.get("approved_normalized_catalog_sha256") is not None
        or rollback_marker
        != {
            "controller_authority_marker_required_before_first_observation": True,
            "semantic_empty_proof_required_before_first_destructive_operation": True,
            "empty_proof_bound_across_database_absence_resume": True,
            "controller_authority_marker_held_through_final_receipt_persistence": True,
            "physical_database_writer_exclusion_claimed": False,
            "external_direct_writer_exclusion_implemented": False,
            "exact_prefix_only_resume": True,
        }
        or type(coverage) is not dict
        or not coverage
        or not all(value is True for value in coverage.values())
        or type(gate) is not dict
        or gate.get("fixed_orchestration_machine_packaged") is not True
        or gate.get("fixed_catalog_query_contract_packaged") is not True
        or gate.get("operation_to_sql_translation_packaged") is not False
        or gate.get("concrete_psycopg_adapter_packaged") is not False
        or gate.get("runtime_ready") is not False
        or gate.get("driver_ready") is not False
        or gate.get("catalog_ready") is not False
        or gate.get("current_constructor_refuses_before_primitive_call") is not True
        or gate.get(
            "caller_sql_dsn_endpoint_database_role_path_or_expected_catalog_allowed"
        )
        is not False
        or gate.get("live_execution_allowed") is not False
        or effects
        != {
            "postgresql_calls": 0,
            "provider_calls": 0,
            "production_reads": 0,
            "secret_reads_or_writes": 0,
        }
    ):
        raise PackageError("dormant_store_postgres_native_stage_contract_invalid")
    unsigned = dict(contract)
    supplied = unsigned.pop("contract_sha256", None)
    if supplied != _canonical_sha256(unsigned):
        raise PackageError("dormant_store_postgres_native_stage_digest_invalid")


def _verify_extension_contracts(observed: dict[str, str]) -> None:
    execution = _load_verified_json(
        EXECUTION_CONTRACT_RELATIVE,
        observed[EXECUTION_CONTRACT_RELATIVE],
    )
    runtime = _load_verified_json(
        CONTROLLER_RUNTIME_CONTRACT_RELATIVE,
        observed[CONTROLLER_RUNTIME_CONTRACT_RELATIVE],
    )
    postgres_source_closure = _load_verified_json(
        POSTGRES_SOURCE_CLOSURE_RELATIVE,
        observed[POSTGRES_SOURCE_CLOSURE_RELATIVE],
    )
    postgres_native_stage = _load_verified_json(
        POSTGRES_NATIVE_STAGE_CONTRACT_RELATIVE,
        observed[POSTGRES_NATIVE_STAGE_CONTRACT_RELATIVE],
    )
    proof = _load_verified_json(
        PROOF_CONTRACT_RELATIVE,
        observed[PROOF_CONTRACT_RELATIVE],
    )
    proof_schema = _load_verified_json(
        PROOF_SCHEMA_RELATIVE,
        observed[PROOF_SCHEMA_RELATIVE],
    )
    install_receipt_schema = _load_verified_json(
        INSTALL_RECEIPT_SCHEMA_RELATIVE,
        observed[INSTALL_RECEIPT_SCHEMA_RELATIVE],
    )
    empty_rollback_receipt_schema = _load_verified_json(
        EMPTY_ROLLBACK_RECEIPT_SCHEMA_RELATIVE,
        observed[EMPTY_ROLLBACK_RECEIPT_SCHEMA_RELATIVE],
    )
    exact = (
        (execution, EXPECTED_EXECUTION_CONTRACT_CANONICAL_SHA256),
        (runtime, EXPECTED_CONTROLLER_RUNTIME_CONTRACT_CANONICAL_SHA256),
        (
            postgres_source_closure,
            EXPECTED_POSTGRES_SOURCE_CLOSURE_CANONICAL_SHA256,
        ),
        (
            postgres_native_stage,
            EXPECTED_POSTGRES_NATIVE_STAGE_CONTRACT_CANONICAL_SHA256,
        ),
        (proof, EXPECTED_PROOF_CONTRACT_CANONICAL_SHA256),
        (proof_schema, EXPECTED_PROOF_SCHEMA_CANONICAL_SHA256),
        (
            install_receipt_schema,
            EXPECTED_INSTALL_RECEIPT_SCHEMA_CANONICAL_SHA256,
        ),
        (
            empty_rollback_receipt_schema,
            EXPECTED_EMPTY_ROLLBACK_RECEIPT_SCHEMA_CANONICAL_SHA256,
        ),
    )
    if any(_canonical_sha256(value) != wanted for value, wanted in exact):
        raise PackageError("dormant_store_install_extension_contract_semantics_invalid")
    _verify_postgres_source_closure(postgres_source_closure)
    _verify_postgres_native_stage_contract(postgres_native_stage)

    boundary = execution.get("execution_boundary")
    binding = execution.get("binding_policy")
    durability = execution.get("durability_policy")
    host = execution.get("host_action_policy")
    receipt = execution.get("receipt_policy")
    proof_boundary = execution.get("proof_boundary")
    dependency = runtime.get("dependency_policy")
    selected_inputs = runtime.get("selected_runtime_inputs")
    selected_cpython = (
        selected_inputs.get("standalone_cpython")
        if type(selected_inputs) is dict
        else None
    )
    selected_driver = (
        selected_inputs.get("postgresql_driver")
        if type(selected_inputs) is dict
        else None
    )
    selected_wheelhouse = (
        selected_inputs.get("wheelhouse")
        if type(selected_inputs) is dict
        else None
    )
    build = runtime.get("build_policy")
    verification = runtime.get("verification_policy")
    entrypoint = runtime.get("entrypoint_policy")
    proof_execution = proof.get("execution_boundary")
    proof_claims = proof.get("claims")
    proof_package_context = proof.get("package_context")
    if (
        execution.get("schema_version")
        != "governed-memory-dormant-store-install-inactive-execution-package-v3"
        or type(boundary) is not dict
        or boundary.get("approval_authorizes_execution") is not False
        or boundary.get(
            "claim_bound_install_controller_composition_callable_non_cli"
        )
        is not True
        or boundary.get(
            "claim_bound_empty_rollback_controller_composition_callable_non_cli"
        )
        is not True
        or boundary.get("closed_postclaim_linux_install_adapter_implemented")
        is not True
        or boundary.get(
            "closed_ledger_bound_physical_empty_rollback_adapter_implemented"
        )
        is not True
        or boundary.get("closed_live_transport_contracts_implemented")
        is not True
        or boundary.get("complete_closed_live_transport_substrate_set_implemented")
        is not False
        or boundary.get(
            "complete_closed_live_transport_substrate_set_integrated_into_bound_factory"
        )
        is not False
        or boundary.get(
            "postgresql_source_closure_contract_implemented"
        )
        is not True
        or boundary.get("driver_native_postgresql_stage_contract_implemented")
        is not True
        or boundary.get(
            "driver_native_postgresql_executable_stage_machine_implemented"
        )
        is not True
        or boundary.get("concrete_psycopg_postgresql_transport_implemented")
        is not False
        or boundary.get(
            "controller_runtime_input_selection_contract_repaired"
        )
        is not True
        or boundary.get(
            "controller_runtime_capability_refuses_unready_selected_inputs_before_probe"
        )
        is not True
        or boundary.get(
            "controller_runtime_build_receipt_provenance_v3_implemented"
        )
        is not True
        or boundary.get(
            "approved_terminal_postgresql_catalog_manifest_selected"
        )
        is not False
        or boundary.get("selected_live_linux_platform_transport_factory_implemented")
        is not False
        or boundary.get(
            "selected_non_postgresql_live_linux_platform_transport_factory_implemented"
        )
        is not True
        or boundary.get("pinned_postgresql_live_driver_selected") is not False
        or boundary.get(
            "controller_runtime_release_builder_orchestration_implemented"
        )
        is not True
        or boundary.get("controller_runtime_build_transport_implemented") is not False
        or boundary.get(
            "controller_runtime_publication_policy_transport_implemented"
        )
        is not True
        or boundary.get("production_runtime_publication_primitives_implemented")
        is not False
        or boundary.get("activation_executor_implemented") is not False
        or type(binding) is not dict
        or binding.get("opaque_verified_package_capability_required") is not True
        or binding.get(
            "opaque_verified_controller_runtime_capability_required"
        )
        is not True
        or binding.get("opaque_claimed_install_execution_capability_required")
        is not True
        or binding.get("opaque_claimed_empty_rollback_capability_required")
        is not True
        or binding.get(
            "empty_rollback_requires_opaque_verified_install_receipt_and_ledger_capability"
        )
        is not True
        or binding.get(
            "resolved_store_spec_hash_and_exact_docker_label_hashes_bound"
        )
        is not True
        or binding.get(
            "journal_binds_verified_full_controller_release_tree_sha256"
        )
        is not True
        or binding.get(
            "empty_rollback_claim_journal_requests_observations_and_controller_authority_marker_bind_verified_runtime_and_full_release_tree"
        )
        is not True
        or binding.get("exact_controller_process_is_trusted") is not True
        or binding.get("hostile_same_process_capability_forgery_resisted") is not False
        or type(durability) is not dict
        or durability.get(
            "same_open_instance_inode_and_directory_replacement_refused"
        )
        is not True
        or durability.get(
            "cross_process_same_content_inode_or_directory_replacement_refused"
        )
        is not True
        or durability.get("one_final_partial_json_line_beyond_exact_anchor_recoverable")
        is not True
        or durability.get("per_execution_resource_ledger_anchor_packaged") is not True
        or durability.get(
            "install_receipt_replay_requires_fresh_terminal_store_readiness_probe"
        )
        is not True
        or durability.get(
            "empty_rollback_resume_revalidates_install_receipt_and_resource_ledger"
        )
        is not True
        or durability.get(
            "completed_empty_rollback_replay_reproves_exact_resource_absence"
        )
        is not True
        or durability.get(
            "empty_rollback_controller_authority_marker_held_from_r04_through_receipt"
        )
        is not True
        or durability.get(
            "fresh_live_semantic_empty_recheck_required_at_r05_under_controller_authority_marker"
        )
        is not True
        or durability.get(
            "destructive_rollback_steps_require_held_controller_authority_marker_and_persisted_r05_empty_proof"
        )
        is not True
        or durability.get("stopped_store_semantic_empty_recheck_is_valid")
        is not False
        or durability.get("durable_live_rollback_controller_authority_marker_transport_implemented")
        is not True
        or durability.get("external_direct_writer_exclusion_implemented")
        is not False
        or durability.get(
            "controller_authority_marker_empty_recheck_then_stop_then_physical_removal_order_implemented"
        )
        is not True
        or durability.get(
            "retained_audit_artifact_hashes_bound_to_rollback_receipt"
        )
        is not True
        or durability.get(
            "terminal_postflight_effect_present_blocks_compensation_and_exact_resume_completes"
        )
        is not True
        or type(execution.get("remaining_blockers")) is not list
        or "pinned_postgresql_driver_not_selected"
        not in execution["remaining_blockers"]
        or "concrete_psycopg_postgresql_transport_and_complete_bound_live_transport_factory_not_packaged"
        not in execution["remaining_blockers"]
        or "production_runtime_publication_primitives_not_packaged"
        not in execution["remaining_blockers"]
        or "external_direct_writer_exclusion_not_implemented_controller_marker_only"
        not in execution["remaining_blockers"]
        or "approved_terminal_postgresql_catalog_manifest_not_selected"
        not in execution["remaining_blockers"]
        or (
            "canonical_install_receipt_emission_not_integrated"
            in execution["remaining_blockers"]
        )
        or type(host) is not dict
        or host.get("typed_operation_specific_boundaries_only") is not True
        or host.get("bounded_image_inspection_command_primitive_packaged")
        is not True
        or host.get(
            "image_inspection_projection_is_id_repo_digests_os_architecture_only"
        )
        is not True
        or host.get(
            "store_supervisor_observations_use_narrow_nonsecret_fields_only"
        )
        is not True
        or host.get(
            "store_supervisor_reverifies_exact_nonsecret_command_health_ports_capabilities_restart_logging_tmpfs_mount_network_and_state"
        )
        is not True
        or host.get("store_supervisor_never_reads_config_environment")
        is not True
        or host.get("controller_runtime_secure_verifier_packaged") is not True
        or host.get("exact_release_path_supervisor_launcher_packaged") is not True
        or host.get(
            "each_host_request_and_ownership_receipt_binds_verified_full_release_tree_sha256"
        )
        is not True
        or host.get("closed_store_effect_adapters_packaged") is not True
        or host.get("closed_live_transport_contracts_packaged") is not True
        or host.get("complete_closed_live_transport_substrate_set_packaged") is not False
        or host.get(
            "postgresql_source_closure_contract_packaged"
        )
        is not True
        or host.get("driver_native_postgresql_stage_contract_packaged")
        is not True
        or host.get(
            "driver_native_postgresql_executable_stage_machine_packaged"
        )
        is not True
        or host.get("concrete_psycopg_postgresql_transport_packaged")
        is not False
        or host.get(
            "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required"
        )
        is not True
        or host.get("approved_terminal_postgresql_catalog_manifest_selected")
        is not False
        or host.get("selected_live_platform_transports_packaged") is not False
        or host.get(
            "selected_non_postgresql_live_platform_transport_factory_packaged"
        )
        is not True
        or type(receipt) is not dict
        or receipt.get(
            "install_receipt_binds_verified_runtime_and_full_release_tree_identity"
        )
        is not True
        or receipt.get(
            "empty_rollback_receipt_binds_verified_runtime_and_full_release_tree_identity"
        )
        is not True
        or receipt.get(
            "rollback_authority_claim_precedes_durable_receipt_reads_and_eligibility_persistence"
        )
        is not True
        or receipt.get("canonical_production_executions_root")
        != "/var/lib/governed-memory-controller/executions"
        or receipt.get(
            "public_install_and_rollback_entrypoints_require_root_owned_production_receipt_store"
        )
        is not True
        or receipt.get(
            "synthetic_receipt_stores_accepted_only_by_private_in_process_test_wrappers"
        )
        is not True
        or receipt.get(
            "final_rollback_receipt_persisted_while_controller_authority_marker_held"
        )
        is not True
        or receipt.get(
            "final_rollback_receipt_persistence_while_controller_authority_marker_held_required"
        )
        is not True
        or type(proof_boundary) is not dict
        or proof_boundary.get("harness_type")
        != "guarded-synthetic-only"
        or type(dependency) is not dict
        or dependency.get(
            "locked_distribution_set_must_exactly_equal_installed_normalized_distribution_set"
        )
        is not True
        or dependency.get("current_lock_contains_preferred_postgresql_driver")
        is not False
        or type(selected_cpython) is not dict
        or selected_cpython.get("python_version") != "3.12.13"
        or selected_cpython.get("archive_sha256")
        != "506191be3ee7bd190a8834dcdc1b3bc70aab50608deccc711935aa007239cabd"
        or selected_cpython.get("archive_staged") is not False
        or selected_cpython.get("archive_bytes_sha256_verified_locally")
        is not False
        or selected_cpython.get("archive_member_types_verified") is not False
        or selected_cpython.get("specification_sha256") is not None
        or selected_cpython.get("payload_tree_sha256") is not None
        or type(selected_driver) is not dict
        or selected_driver.get("api_style") != "synchronous"
        or selected_driver.get("preferred_extra") != "binary"
        or selected_driver.get("selection_state")
        != "preferred-family-and-version-only-not-selected-not-in-current-lock-not-staged-not-verified"
        or selected_driver.get("preferred_distributions")
        != [
            {
                "normalized_distribution": "psycopg",
                "version": "3.3.4",
            },
            {
                "normalized_distribution": "psycopg-binary",
                "version": "3.3.4",
            },
        ]
        or selected_driver.get("selected_wheels") != []
        or any(
            selected_driver.get(key) is not False
            for key in (
                "binary_native_library_closure_inspected",
                "current_controller_lock_contains_selection",
                "exact_wheel_filenames_frozen",
                "selection_ready_for_runtime_build",
                "wheel_bytes_sha256_verified_locally",
                "wheel_bytes_staged",
            )
        )
        or selected_driver.get("driver_native_postgresql_stages_packaged")
        is not True
        or selected_driver.get("concrete_psycopg_postgresql_transport_packaged")
        is not False
        or type(selected_wheelhouse) is not dict
        or selected_wheelhouse.get("canonical_tree_sha256") is not None
        or selected_wheelhouse.get("canonical_member_count") is not None
        or selected_wheelhouse.get("canonical_total_bytes") is not None
        or selected_wheelhouse.get("wheelhouse_staged") is not False
        or type(build) is not dict
        or build.get("current_runtime_built") is not False
        or build.get("current_runtime_installed") is not False
        or build.get("current_release_staged") is not False
        or build.get(
            "builder_accepts_only_selected_standalone_cpython_archive_name_and_sha256"
        )
        is not True
        or build.get(
            "builder_reparses_exact_standalone_cpython_specification_before_any_transport_call"
        )
        is not True
        or build.get("builder_derives_wheelhouse_tree_from_exact_member_bytes")
        is not True
        or build.get("builder_accepts_caller_supplied_opaque_wheelhouse_hash")
        is not False
        or build.get(
            "builder_requires_same_device_rename_preconditions_before_publication_intent"
        )
        is not True
        or build.get(
            "builder_persists_exact_publication_intent_before_first_rename"
        )
        is not True
        or build.get(
            "builder_forbids_recursive_stage_cleanup_after_publication_intent"
        )
        is not True
        or build.get(
            "builder_replays_only_exact_terminal_publication_with_renewed_fsyncs"
        )
        is not True
        or build.get(
            "repository_only_runtime_and_release_builder_orchestration_packaged"
        )
        is not True
        or build.get("concrete_runtime_build_transport_packaged") is not False
        or build.get("runtime_publication_policy_transport_packaged") is not True
        or build.get("production_runtime_publication_primitives_packaged")
        is not False
        or build.get("independent_standalone_cpython_payload_tree_proof_packaged")
        is not False
        or build.get(
            "controller_runtime_and_release_are_separately_authorized_preinstall_substrate"
        )
        is not True
        or type(verification) is not dict
        or verification.get(
            "secure_receipt_bound_no_follow_verifier_packaged"
        )
        is not True
        or verification.get(
            "runtime_capability_refuses_unready_substrate_driver_or_wheelhouse_before_probe"
        )
        is not True
        or verification.get(
            "interpreter_prefix_import_stdlib_and_site_paths_confined_to_runtime_root"
        )
        is not True
        or verification.get(
            "full_release_tree_must_exactly_match_package_manifest_with_no_extra_members"
        )
        is not True
        or verification.get(
            "static_launcher_local_module_closure_verification_required"
        )
        is not True
        or verification.get(
            "release_tree_sha256_propagates_through_claim_journal_host_ownership_and_install_receipt"
        )
        is not True
        or verification.get(
            "empty_rollback_runtime_and_release_identity_propagates_through_signed_authority_claim_journal_requests_observations_controller_authority_marker_and_receipt"
        )
        is not True
        or verification.get(
            "controller_runtime_verifier_executed_in_current_phase"
        )
        is not False
        or verification.get(
            "current_runtime_build_receipt_present"
        )
        is not False
        or runtime.get("runtime_build_receipt_policy", {}).get("schema_version")
        != "governed-memory-controller-runtime-build-receipt-v3"
        or runtime.get("runtime_build_receipt_policy", {}).get(
            "canonical_wheelhouse_tree_sha256_required"
        )
        is not True
        or runtime.get("runtime_build_receipt_policy", {}).get(
            "receipt_input_hashes_must_equal_selected_staged_runtime_contract"
        )
        is not True
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
                "claim_bound_install_composition_exercised",
                "claim_bound_empty_rollback_composition_exercised",
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
        or type(proof_package_context) is not dict
        or proof_package_context.get("concrete_live_store_effect_adapters_packaged")
        is not True
        or proof_package_context.get("fixed_loopback_store_readiness_adapter_packaged")
        is not True
        or proof_schema.get("additionalProperties") is not False
        or proof_schema.get("$id")
        != "urn:governed-memory:dormant-store-install:disposable-proof-receipt:v2"
        or install_receipt_schema.get("additionalProperties") is not False
        or empty_rollback_receipt_schema.get("additionalProperties") is not False
        or install_receipt_schema.get("$id")
        != "urn:governed-memory:dormant-store-install-receipt:v1"
        or empty_rollback_receipt_schema.get("$id")
        != "urn:governed-memory:empty-store-rollback-receipt:v3"
        or not {
            "controller_runtime_receipt_sha256",
            "controller_runtime_root",
            "controller_runtime_tree_sha256",
            "controller_release_root",
            "controller_release_tree_sha256",
            "controller_release_package_manifest_path",
            "controller_runtime_interpreter_path",
            "controller_runtime_interpreter_sha256",
            "controller_runtime_inventory_path",
            "controller_runtime_inventory_sha256",
            "controller_requirements_lock_sha256",
            "supervisor_launcher_path",
            "supervisor_launcher_sha256",
        }
        <= set(empty_rollback_receipt_schema.get("required", ()))
    ):
        raise PackageError("dormant_store_install_extension_contract_boundary_invalid")


def _load_verified_module(
    relative: str,
    module_name: str,
    *,
    expected_sha256: str,
) -> ModuleType:
    """Execute exactly the bytes already checked against the package manifest."""

    raw = _read_repository_file(relative)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PackageError("dormant_store_install_verified_module_changed_after_hash")
    module = ModuleType(module_name)
    module.__file__ = str(ROOT / relative)
    module.__package__ = module_name.rpartition(".")[0]
    if module_name in sys.modules:
        raise PackageError("dormant_store_install_verified_module_namespace_collision")
    sys.modules[module_name] = module
    try:
        exec(compile(raw, module.__file__, "exec"), module.__dict__)
    except Exception as error:
        raise PackageError("dormant_store_install_verified_module_invalid") from error
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

    package_name = "_dormant_store_install_verified_controller_package"
    module_name = package_name + ".controller"
    lock_name = package_name + ".execution_lock"

    class _ExecutionLockError(RuntimeError):
        pass

    class _HeldExecutionLockCapability:
        pass

    def _refuse_lock_use(unused: object) -> None:
        raise _ExecutionLockError("dormant_store_install_verifier_lock_surface_unavailable")

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
        raise PackageError("dormant_store_install_verified_module_namespace_collision")
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
        raise PackageError("dormant_store_install_controller_model_invalid") from error
    if (
        controller_hash != EXPECTED_CONTROLLER_MODEL_SHA256
        or tuple(step["id"] for step in controller_projection)
        != EXPECTED_CONTROLLER_STEP_IDS
        or plan["install_steps"] != controller_projection
    ):
        raise PackageError("dormant_store_install_controller_plan_binding_invalid")
    return controller_hash


def _verify_migration_binding(
    observed: dict[str, str], module: ModuleType
) -> dict[str, object]:
    try:
        receipt = module.verify()
    except Exception as error:
        raise PackageError("dormant_store_install_migration_verifier_failed") from error
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
        raise PackageError("dormant_store_install_migration_receipt_invalid")
    if (
        receipt.get("schema_version")
        != "governed-memory-dormant-store-install-store-migration-verification-v3"
        or receipt.get("state")
        != "repository-only-current-stores-only-migration-set-not-installed-not-authorized"
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
        raise PackageError("dormant_store_install_migration_receipt_invalid")
    migration_artifacts = receipt.get("artifact_sha256")
    if type(migration_artifacts) is not dict:
        raise PackageError("dormant_store_install_migration_receipt_invalid")
    package_migration_artifacts: set[str] = set()
    for relative, digest in migration_artifacts.items():
        if type(relative) is not str or type(digest) is not str:
            raise PackageError("dormant_store_install_migration_receipt_invalid")
        package_relative = (
            relative
            if relative.startswith("ops/")
            else "governed-memory-migrations/" + relative
        )
        if observed.get(package_relative) != digest:
            raise PackageError("dormant_store_install_migration_package_binding_invalid")
        package_migration_artifacts.add(package_relative)
    if package_migration_artifacts != EXPECTED_MIGRATION_ARTIFACTS:
        raise PackageError("dormant_store_install_migration_package_binding_invalid")
    return receipt


def _verify_manifest(manifest: dict[str, object]) -> dict[str, str]:
    if set(manifest) != {"schema_version", "state", "artifacts"}:
        raise PackageError("dormant_store_install_package_manifest_shape_invalid")
    if (
        manifest.get("schema_version")
        != "governed-memory-dormant-store-install-inactive-execution-package-manifest-v3"
        or manifest.get("state")
        != "repository-only-claim-bound-install-and-empty-rollback-controllers-packaged-not-installed-not-activated"
    ):
        raise PackageError("dormant_store_install_package_manifest_identity_invalid")
    artifacts = manifest.get("artifacts")
    if type(artifacts) is not dict or set(artifacts) != EXPECTED_ARTIFACTS:
        raise PackageError("dormant_store_install_package_artifact_set_invalid")
    if any(
        marker in relative
        for relative in artifacts
        for marker in FORBIDDEN_ARTIFACT_MARKERS
    ):
        raise PackageError("dormant_store_install_package_forbidden_artifact")
    observed: dict[str, str] = {}
    for relative, wanted in artifacts.items():
        if (
            type(relative) is not str
            or type(wanted) is not str
            or HASH_RE.fullmatch(wanted) is None
        ):
            raise PackageError("dormant_store_install_package_artifact_entry_invalid")
        actual = artifact_sha256(relative)
        if actual != wanted:
            raise PackageError("dormant_store_install_package_hash_mismatch:" + relative)
        observed[relative] = actual
    return observed


def verify() -> dict[str, object]:
    _verify_current_directory_closure()
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
        "_dormant_store_install_verified_migration_manifest",
        expected_sha256=EXPECTED_MIGRATION_VERIFIER_SOURCE_SHA256,
    )
    migration_receipt = _verify_migration_binding(observed, migration_verifier)

    return {
        "schema_version": "governed-memory-dormant-store-install-package-verification-v4",
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
        "postgres_source_closure_contract_canonical_sha256": (
            EXPECTED_POSTGRES_SOURCE_CLOSURE_CANONICAL_SHA256
        ),
        "postgres_native_stage_contract_canonical_sha256": (
            EXPECTED_POSTGRES_NATIVE_STAGE_CONTRACT_CANONICAL_SHA256
        ),
        "proof_contract_canonical_sha256": (
            EXPECTED_PROOF_CONTRACT_CANONICAL_SHA256
        ),
        "proof_schema_canonical_sha256": EXPECTED_PROOF_SCHEMA_CANONICAL_SHA256,
        "install_receipt_schema_canonical_sha256": (
            EXPECTED_INSTALL_RECEIPT_SCHEMA_CANONICAL_SHA256
        ),
        "empty_rollback_receipt_schema_canonical_sha256": (
            EXPECTED_EMPTY_ROLLBACK_RECEIPT_SCHEMA_CANONICAL_SHA256
        ),
        "controller_source_sha256": observed[CONTROLLER_MODEL_RELATIVE],
        "controller_model_sha256": controller_model_sha256,
        "migration_verifier_source_sha256": observed[MIGRATION_VERIFIER_RELATIVE],
        "migration_manifest_sha256": migration_receipt["manifest_sha256"],
        "durable_install_and_rollback_journal_adapters_packaged": True,
        "durable_resource_identity_ledger_and_anchor_packaged": True,
        "guarded_synthetic_proof_harness_packaged": True,
        "synthetic_proof_executed_by_verifier": False,
        "synthetic_proof_receipt_promoted": False,
        "bounded_image_inspect_runner_primitive_packaged": True,
        "local_image_inspect_adapter_packaged": True,
        "controller_runtime_verification_capability_packaged": True,
        "full_controller_release_tree_verification_packaged": True,
        "exact_locked_controller_distribution_set_verification_packaged": True,
        "full_release_tree_sha256_bound_through_claim_journal_host_ownership_and_install_receipt": True,
        "empty_rollback_full_runtime_and_release_identity_bound_through_authority_claim_journal_requests_observations_controller_authority_marker_and_receipt": True,
        "supervisor_launcher_source_packaged": True,
        "controller_runtime_built_or_installed": False,
        "controller_release_staged": False,
        "controller_runtime_and_release_require_separate_future_build_and_install_authority": True,
        "stores_install_owns_or_removes_controller_substrate": False,
        "resolved_store_spec_and_exact_docker_labels_bound": True,
        "resource_identity_ledger_v2_packaged": True,
        "empty_rollback_controller_authority_marker_packaged": True,
        "durable_live_empty_rollback_controller_authority_marker_transport_packaged": True,
        "external_direct_writer_exclusion_implemented": False,
        "empty_rollback_receipt_persisted_create_once_while_controller_authority_marker_held": True,
        "fresh_live_semantic_empty_recheck_required_at_r05_under_controller_authority_marker": True,
        "controller_authority_marker_empty_recheck_then_stop_then_physical_removal_order_implemented": True,
        "closed_live_transport_contracts_packaged": True,
        "complete_closed_live_transport_substrate_set_packaged": False,
        "complete_closed_live_transport_substrate_set_integrated_into_bound_factory": False,
        "postgresql_source_closure_contract_packaged": True,
        "driver_native_postgresql_stage_contract_packaged": True,
        "driver_native_postgresql_executable_stage_machine_packaged": True,
        "concrete_psycopg_postgresql_transport_packaged": False,
        "runtime_input_selection_contract_repaired": True,
        "runtime_build_receipt_provenance_v3_packaged": True,
        "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required": True,
        "approved_terminal_postgresql_catalog_manifest_selected": False,
        "stopped_store_semantic_empty_recheck_is_valid": False,
        "retained_audit_artifact_hashes_bound": True,
        "public_entrypoints_require_canonical_root_owned_production_receipt_store": True,
        "synthetic_receipt_stores_are_private_test_only": True,
        "claim_bound_install_controller_composition_packaged": True,
        "claim_bound_empty_rollback_controller_composition_packaged": True,
        "closed_install_store_effect_adapter_packaged": True,
        "closed_empty_rollback_store_effect_adapter_packaged": True,
        "selected_live_platform_transports_packaged": False,
        "selected_non_postgresql_live_platform_transport_factory_packaged": True,
        "pinned_postgresql_driver_selected": False,
        "durable_create_once_receipt_store_packaged": True,
        "controller_runtime_release_builder_orchestration_packaged": True,
        "controller_runtime_build_transport_packaged": False,
        "controller_runtime_publication_policy_transport_packaged": True,
        "runtime_publication_durable_intent_before_first_rename_packaged": True,
        "runtime_publication_post_intent_generic_cleanup_forbidden": True,
        "runtime_publication_crash_prefix_manual_review_fence_packaged": True,
        "runtime_publication_same_device_rename_precondition_packaged": True,
        "runtime_publication_exact_terminal_replay_with_renewed_fsyncs_packaged": True,
        "production_runtime_publication_primitives_packaged": False,
        "independent_standalone_cpython_payload_tree_proof_packaged": False,
        "operation_specific_install_and_empty_rollback_receipts_packaged": True,
        "install_controller_emits_canonical_receipt": True,
        "empty_rollback_controller_emits_canonical_receipt": True,
        "install_receipt_binds_fresh_terminal_canonical_store_readiness": True,
        "empty_rollback_requires_opaque_verified_install_receipt_and_ledger": True,
        "completed_install_and_empty_rollback_replay_reverification_packaged": True,
        "activation_entrypoint_packaged": False,
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
        print("DORMANT_STORE_INSTALL_PACKAGE_INVALID=" + str(error))
        return 1
    if arguments.command == "verify-package":
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

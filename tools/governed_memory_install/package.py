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
POSTGRES_NATIVE_STAGE_CONTRACT_RELATIVE: Final = (
    "ops/governed_memory/installation/current/postgres/native_stage_contract.json"
)
PROOF_CONTRACT_RELATIVE: Final = (
    "ops/governed_memory/installation/current/disposable_proof_contract.json"
)
SYNTHETIC_PROOF_SCHEMA_RELATIVE: Final = (
    "ops/governed_memory/installation/current/disposable_proof_receipt.schema.json"
)
LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE: Final = (
    "ops/governed_memory/installation/current/live_proof_receipt.schema.json"
)
INSTALL_RECEIPT_SCHEMA_RELATIVE: Final = (
    "ops/governed_memory/installation/current/install_receipt.schema.json"
)
EMPTY_ROLLBACK_RECEIPT_SCHEMA_RELATIVE: Final = (
    "ops/governed_memory/installation/current/empty_rollback_receipt.schema.json"
)

EXPECTED_CONTRACT_CANONICAL_SHA256: Final = (
    "fb2aed56c2447a22af849080dc159b0c46f96693887d44b29ca09f3168380898"
)
EXPECTED_PLAN_CANONICAL_SHA256: Final = (
    "d502d29682dbdf511a07d7534cec6a8f14c73e44492b08f0fad29157639e246c"
)
EXPECTED_CONTROLLER_SOURCE_SHA256: Final = (
    "5a18628c85aab814360667341f685f809ac240b484a5fe6e10c727f54a752de5"
)
EXPECTED_CONTROLLER_MODEL_SHA256: Final = (
    "d3701a21b827da66122906e1dcc2828ce69f06e1048164df1c4d0ca0321c3de8"
)
EXPECTED_EXECUTION_CONTRACT_CANONICAL_SHA256: Final = (
    "975421b4e1fe2ed9e436144f37bdd68979b90a373b48186a5a2aa646b189f09f"
)
EXPECTED_CONTROLLER_RUNTIME_CONTRACT_CANONICAL_SHA256: Final = (
    "60580592361d52b6af3d57d023d7bdd3f0876d677f736e6ee00aeb85841d79ac"
)
EXPECTED_POSTGRES_NATIVE_STAGE_CONTRACT_CANONICAL_SHA256: Final = (
    "bec461c1166775817dc4c3ffce7c09195f7d758942ca3d63ee3a875f8d5b9f2d"
)
EXPECTED_PROOF_CONTRACT_CANONICAL_SHA256: Final = (
    "072ad9a0c4f10ef45e0a5287780b2f4b9334eb8741744b9bc15da74260e07d2f"
)
EXPECTED_SYNTHETIC_PROOF_RECEIPT_SCHEMA_CANONICAL_SHA256: Final = (
    "5fa4b98974b7c1652c931abd7b630bbca11fd92fde15ee4d099ae6132f3dfe1d"
)
EXPECTED_LIVE_PROOF_RECEIPT_SCHEMA_CANONICAL_SHA256: Final = (
    "167ada117aa0a3bb69ce62c1c19469cd7cdfad0e85a338b3c891cfd33be4b3b9"
)
EXPECTED_INSTALL_RECEIPT_SCHEMA_CANONICAL_SHA256: Final = (
    "6a76bcf802ba72257bb5fa524010de5182f85c6dbee2b490d46f92faf159a395"
)
EXPECTED_EMPTY_ROLLBACK_RECEIPT_SCHEMA_CANONICAL_SHA256: Final = (
    "4cf0b822c3b7c18bb1469deb8c044146982214e5ae1d0d8be8bf4a4a79e9292e"
)
EXPECTED_MIGRATION_VERIFIER_SOURCE_SHA256: Final = (
    "4b8ae071fb0f1a8e07e1fd78cef0a428e1a27ee44014cba5db912f05f6403931"
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
    "R05_DISABLE_AND_REMOVE_STORES_SUPERVISOR",
    "R06_ESTABLISH_ADMINISTRATIVE_WRITER_FENCE_AND_RECHECK_SEMANTIC_EMPTY",
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
EXPECTED_POSTGRES_NATIVE_STAGE_IDS: Final = (
    "F01_PREBOOTSTRAP_AND_CREATE_DATABASE",
    "F02_ROLES_PRIVACY_AND_MIGRATIONS",
    "T01_TERMINAL_EXACT_CATALOG",
    "R01_EMPTY_ROLLBACK_PREFIX_RESUME",
)

STALE_ROLLBACK_CLAIM_ORDERING_INVARIANTS: Final = frozenset(
    {
        "empty_rollback_authority_claim_precedes_durable_receipt_reads_and_eligibility_persistence",
        "rollback_authority_claim_precedes_durable_receipt_reads_and_eligibility_persistence",
    }
)
REQUIRED_FINAL_ROLLBACK_ORDERING_INVARIANTS: Final = frozenset(
    {
        "durable_install_receipt_and_anchored_exact_fifteen_resource_ledger_verified_before_final_rollback_nonce_claim",
        "opaque_retained_rollback_evidence_sha256_bound_into_final_rollback_execution_and_claim",
        "final_rollback_nonce_claim_precedes_eligibility_persistence_journal_creation_and_any_rollback_effect",
        "r03_reverifies_exact_retained_install_receipt_and_anchored_ledger_before_first_effect",
    }
)
REQUIRED_ROLLBACK_RECOVERY_INVARIANTS: Final = frozenset(
    {
        "resume_install_and_resume_rollback_never_create_missing_claims",
        "start_reserved_rollback_requires_exact_preclaimed_recovery_reservation_and_is_the_only_post_expiry_first_claim_path",
        "install_recovery_reservation_and_rollback_nonces_are_pairwise_distinct_and_recovery_delegation_is_bound_to_the_exact_derived_install_execution_id",
    }
)
REQUIRED_START_AUTHORITY_PAIR_INVARIANTS: Final = frozenset(
    {
        "recovery_reservation_and_install_authority_claimed_atomically_before_capsule_publication_and_first_install_effect",
        "published_recovery_capsule_requires_exact_preclaimed_start_authority_pair",
        "fresh_first_install_worker_uses_exact_resume_only",
    }
)
REQUIRED_PROOF_FAULT_BARRIER_INVARIANTS: Final = frozenset(
    {
        "proof_fault_barrier_is_process_local_and_fixed_to_first_install_i04_and_first_empty_rollback_r05_workers",
        "proof_fault_worker_self_stops_only_after_durable_journal_append_readback_and_anchor_reconciliation",
        "proof_parent_never_issues_sigstop",
        "process_death_arm_requires_boundary_as_exact_journal_head",
        "process_death_arm_requires_single_worker_task_and_empty_child_process_set",
        "sigkill_and_exact_reap_required_before_stopped_worker_can_resume",
        "public_install_and_rollback_entrypoints_are_unmodified_by_proof_fault_barrier",
    }
)


def _requires_true_invariants(
    section: object,
    required: frozenset[str],
    *,
    reject_stale_ordering: bool = False,
) -> bool:
    if type(section) is not dict:
        return False
    assert isinstance(section, dict)
    if any(section.get(key) is not True for key in required):
        return False
    return not (
        reject_stale_ordering
        and any(key in section for key in STALE_ROLLBACK_CLAIM_ORDERING_INVARIANTS)
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
        "disposable_linux_proof",
    }
)
EXPECTED_LIVE_EXECUTION: Final = {
    "claim_bound_install_controller_composition_packaged": True,
    "non_cli_install_entrypoint_packaged": True,
    "public_install_entrypoint_internally_selects_production_journal_ledger_transports_secret_sources_postgresql_receipt_sink_and_host_operations": True,
    "public_install_entrypoint_accepts_caller_selected_dependencies_or_audit_state": False,
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
    "complete_closed_live_transport_substrate_set_packaged": True,
    "driver_native_postgresql_stage_contract_packaged": True,
    "driver_native_postgresql_executable_stage_machine_packaged": True,
    "concrete_psycopg_postgresql_transport_packaged": True,
    "runtime_input_selection_contract_repaired": True,
    "runtime_build_receipt_provenance_v4_packaged": True,
    "complete_closed_live_transport_substrate_set_integrated_into_bound_factory": True,
    "approved_terminal_postgresql_catalog_manifest_selected": True,
    "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required": True,
    "durable_live_empty_rollback_controller_authority_marker_transport_packaged": True,
    "administrative_cooperative_writer_fence_implemented": True,
    "equivalent_privileged_root_bypass_excluded": False,
    "stopped_store_semantic_empty_recheck_is_valid": False,
    "selected_live_platform_transports_packaged": True,
    "selected_non_postgresql_live_platform_transport_factory_packaged": True,
    "pinned_postgresql_driver_selected": True,
    "durable_create_once_receipt_store_packaged": True,
    "public_entrypoints_require_canonical_root_owned_production_receipt_store": True,
    "synthetic_receipt_stores_are_private_test_only": True,
    "controller_runtime_release_builder_orchestration_packaged": True,
    "controller_runtime_build_transport_packaged": True,
    "controller_runtime_publication_policy_transport_packaged": True,
    "production_runtime_publication_primitives_packaged": True,
    "runtime_input_stager_packaged": True,
    "fixed_controller_runtime_parent_root_bootstrap_packaged": True,
    "fixed_controller_runtime_parent_root_bootstrap_is_parameterless_and_idempotent": True,
    "runtime_input_staging_is_create_only_and_no_replace": True,
    "runtime_input_staging_exact_terminal_replay_packaged": True,
    "runtime_input_staging_exact_owned_partial_recovery_packaged": True,
    "runtime_input_staging_foreign_or_drifted_state_refused": True,
    "runtime_input_staging_durable_intent_or_receipt_packaged": False,
    "independent_standalone_cpython_payload_tree_proof_packaged": True,
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
    "controller_runtime_parent_roots_bootstrapped": False,
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
        LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE,
        "ops/governed_memory/installation/current/install_receipt.schema.json",
        "ops/governed_memory/installation/current/empty_rollback_receipt.schema.json",
        "ops/governed_memory/installation/current/execution_contract.json",
        "ops/governed_memory/installation/current/migration_manifest.json",
        "ops/governed_memory/installation/current/postgres/migration_bindings.json",
        POSTGRES_NATIVE_STAGE_CONTRACT_RELATIVE,
        "ops/governed_memory/installation/current/postgres/roles_preflight.pgsql",
        "ops/governed_memory/installation/postgres/canonical_cluster.pgsql.in",
        "ops/governed_memory/installation/postgres/canonical_cluster_rollback.pgsql.in",
        "ops/governed_memory/installation/store_spec-v3.json",
        "ops/governed_memory/controller-requirements.lock",
        "ops/governed_memory/installation/systemd/governed-memory-stores-v3.service.in",
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
        "tools/governed_memory_install/psycopg_postgres_adapter.py",
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
        "tools/governed_memory_release/inspect_standalone_cpython.py",
        "tools/governed_memory_release/linux_runtime_publication_primitives.py",
        "tools/governed_memory_release/runtime_input_stager.py",
        "tools/governed_memory_release/runtime_publication_transport.py",
        "tools/governed_memory_validation/generate_installation_package_manifest.py",
        "tools/governed_memory_validation/durable_live_proof_receipt.py",
        "tools/governed_memory_validation/process_death_arm_receipt.py",
        "tools/governed_memory_validation/run_disposable_installation_live_proof.py",
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
    "postgres_source_closure",
    "source_closure_contract",
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
        != "governed-memory-dormant-store-install-inactive-execution-contract-v6"
        or contract.get("state")
        != "phase9j-install-ready-closed-runtime-and-store-transports-packaged-not-installed-not-activated"
        or contract.get("server") != "seebx"
    ):
        raise PackageError("dormant_store_install_contract_identity_invalid")
    scope = contract.get("scope")
    if (
        type(scope) is not dict
        or scope.get("current_phase_repository_only") is not False
        or scope.get("current_phase_disposable_catalog_selection_executed")
        is not True
        or scope.get("current_phase_runtime_input_selection_closed") is not True
        or scope.get("current_phase_runtime_incoming_artifacts_staged") is not True
        or scope.get("current_phase_offline_runtime_input_publication_executed")
        is not False
        or scope.get("current_controller_runtime_parent_roots_bootstrapped")
        is not False
        or scope.get("current_phase_production_reads") != 0
        or scope.get("current_phase_provider_calls") != 0
        or scope.get("current_phase_activation_calls") != 0
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
        or migration.get("driver_native_executable_stage_contract_packaged")
        is not True
        or migration.get("concrete_psycopg_postgresql_transport_packaged")
        is not True
        or migration.get("host_psql_direct_execution_allowed") is not False
        or migration.get("preferred_synchronous_driver_target")
        != "psycopg[binary]==3.3.4"
        or migration.get("preferred_synchronous_driver_locked_staged_and_verified")
        is not True
        or migration.get("runtime_postgresql_driver_identity_sha256")
        != "364760713fd35d8d7029c972e9cc23ec69f3b5c4a9a1ce6bab302a525f0ef8fa"
        or migration.get("approved_terminal_catalog_manifest_selected")
        is not True
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
    targets = contract.get("exact_targets")
    filesystem = contract.get("filesystem_policy")
    images = contract.get("image_policy")
    if (
        type(targets) is not dict
        or "image_receipt" in targets
        or "trust_anchor" in targets
        or targets.get("recovery_capsule")
        != "/var/lib/governed-memory-controller/phase9-disposable-proof-recovery-capsule-v4.json"
        or targets.get("nonce_state")
        != "/var/lib/governed-memory-controller/authority-state-v3.sqlite3"
        or targets.get("proof_supervision_lock")
        != "/run/lock/governed-memory-controller/phase9-disposable-live-proof.lock"
        or type(filesystem) is not dict
        or "trust_anchor" in filesystem
        or filesystem.get("recovery_capsule")
        != {
            "owner": "root:root",
            "mode": "0400",
            "external_or_persistent_hard_link_allowed": False,
            "fixed_publication_temp_suffix": ".publishing",
            "transient_same_inode_publication_hard_link_allowed": True,
            "terminal_link_count": 1,
            "interrupted_same_inode_link_count_two_reconciled_before_execution": True,
            "no_replace_publication_required": True,
            "partial_different_inode_or_metadata_drift_refused": True,
            "create_once": True,
            "embeds_trust_bundle": True,
            "symlink_allowed": False,
            "retained_after_terminal_observation": True,
            "file_and_parent_fsync_required": True,
        }
        or filesystem.get("proof_supervision_lock")
        != {
            "owner": "root:root",
            "mode": "0600",
            "hard_link_allowed": False,
            "symlink_allowed": False,
            "link_count": 1,
            "empty_file_required": True,
            "parent_owner": "root:root",
            "parent_mode": "0700",
            "issuer_creates_if_absent_or_securely_reopens": True,
            "never_unlinked": True,
            "nonblocking_exclusive_lock_required": True,
            "held_for_full_run_or_recover_lifecycle": True,
            "inherited_through_sealed_runner_process_group": True,
            "retained_after_terminal_observation": True,
        }
        or filesystem.get(
            "recovery_capsule_is_issuer_created_durable_retained_substrate"
        )
        is not True
        or filesystem.get(
            "proof_runner_may_read_only_fixed_recovery_capsule"
        )
        is not True
        or filesystem.get(
            "issuer_acquires_supervision_lock_before_capsule_inspection_or_key_generation"
        )
        is not True
        or filesystem.get(
            "sealed_runner_requires_inherited_locked_open_file_description"
        )
        is not True
        or filesystem.get("proof_workers_retain_supervision_lock_after_issuer_death")
        is not True
        or filesystem.get("proof_supervision_lock_parent_must_preexist") is not True
        or filesystem.get(
            "proof_supervision_lock_is_issuer_created_or_securely_reopened"
        )
        is not True
        or filesystem.get("persistent_authority_key_file_required") is not False
        or filesystem.get("persistent_image_staging_substrate_required")
        is not False
        or filesystem.get("proof_substrate_directories_must_preexist_execution")
        is not True
        or filesystem.get(
            "proof_runner_validates_but_never_creates_chmods_or_chowns_substrate_directories"
        )
        is not True
        or filesystem.get("package_may_create_recovery_capsule")
        is not False
        or filesystem.get(
            "package_install_and_rollback_may_create_or_remove_proof_supervision_lock"
        )
        is not False
        or filesystem.get(
            "package_may_create_or_read_persistent_authority_key_file"
        )
        is not False
        or filesystem.get("package_may_create_or_read_image_staging_receipt")
        is not False
        or type(images) is not dict
        or images.get("pull_policy") != "never"
        or images.get("verified_local_image_id_required") is not True
        or images.get(
            "exact_local_repo_digest_and_image_id_reinspection_required"
        )
        is not True
        or images.get("canonical_image_identity_set_constructed_in_memory")
        is not True
        or images.get(
            "canonical_image_identity_set_sha256_bound_to_install_receipt"
        )
        is not True
        or images.get(
            "canonical_image_identity_set_sha256_bound_to_live_proof_receipt"
        )
        is not True
        or images.get("persistent_image_staging_receipt_required") is not False
        or images.get("package_or_execution_may_pull_build_load_or_import")
        is not False
    ):
        raise PackageError(
            "dormant_store_install_contract_image_authority_boundary_invalid"
        )
    authority = contract.get("authority_policy")
    recovery = contract.get("recovery_policy")
    identity = contract.get("identity_policy")
    supervisor = contract.get("supervisor_policy")
    proof_policy = contract.get("proof_harness_policy")
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
        or authority.get(
            "canonical_dormant_install_authority_identity_derivation_shared_by_install_and_rollback"
        )
        is not True
        or authority.get("recovery_capsule_path_is_fixed") is not True
        or authority.get("recovery_capsule_root_owned_mode_0400_required")
        is not True
        or authority.get("trust_bundle_is_embedded_in_recovery_capsule")
        is not True
        or authority.get("signed_public_pre_effect_recovery_delegation_required")
        is not True
        or authority.get(
            "durable_recovery_reservation_claim_required_before_first_install_effect"
        )
        is not True
        or not _requires_true_invariants(
            authority,
            frozenset(
                {
                    "recovery_reservation_and_install_authority_claimed_atomically_before_capsule_publication_and_first_install_effect",
                    "published_recovery_capsule_requires_exact_preclaimed_start_authority_pair",
                }
            ),
        )
        or authority.get("ephemeral_private_signer_retained_for_execution")
        is not False
        or authority.get("persistent_trusted_owner_key_file_required") is not False
        or authority.get("host_clock_synchronization_preflight_binary")
        != "/usr/bin/timedatectl"
        or authority.get("host_clock_synchronization_preflight_arguments")
        != ["show", "--property=NTPSynchronized", "--value"]
        or authority.get("host_clock_synchronization_preflight_timeout_seconds")
        != 5
        or authority.get("host_clock_synchronization_preflight_exact_stdout")
        != "yes\n"
        or authority.get(
            "host_clock_synchronization_preflight_empty_stderr_required"
        )
        is not True
        or authority.get(
            "host_clock_synchronization_preflight_runs_before_substrate_validation"
        )
        is not True
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
        or recovery.get("durable_pre_effect_rollback_recovery_reservation_packaged")
        is not True
        or recovery.get("fresh_process_recovery_without_ephemeral_signer_packaged")
        is not True
        or not _requires_true_invariants(
            recovery,
            REQUIRED_PROOF_FAULT_BARRIER_INVARIANTS,
        )
        or recovery.get(
            "expired_recovery_delegation_usable_only_with_matching_preclaimed_reservation"
        )
        is not True
        or recovery.get(
            "recovery_capsule_retained_and_never_removed_by_store_rollback"
        )
        is not True
        or recovery.get(
            "proof_supervision_lock_serializes_full_run_and_recover_lifecycle"
        )
        is not True
        or recovery.get(
            "public_install_entrypoint_internally_selects_production_journal_ledger_transports_secret_sources_postgresql_receipt_sink_and_host_operations"
        )
        is not True
        or recovery.get(
            "public_install_entrypoint_accepts_caller_selected_dependencies_or_audit_state"
        )
        is not False
        or recovery.get("closed_live_transport_contracts_packaged") is not True
        or recovery.get("complete_closed_live_transport_substrate_set_packaged") is not True
        or recovery.get(
            "complete_closed_live_transport_substrate_set_integrated_into_bound_factory"
        )
        is not True
        or recovery.get("driver_native_postgresql_stage_contract_packaged")
        is not True
        or recovery.get(
            "driver_native_postgresql_executable_stage_machine_packaged"
        )
        is not True
        or recovery.get("concrete_psycopg_postgresql_transport_packaged")
        is not True
        or recovery.get("controller_runtime_input_selection_contract_repaired")
        is not True
        or recovery.get("runtime_input_stager_packaged") is not True
        or recovery.get("fixed_controller_runtime_parent_root_bootstrap_packaged")
        is not True
        or recovery.get(
            "fixed_controller_runtime_parent_root_bootstrap_is_parameterless_and_idempotent"
        )
        is not True
        or recovery.get(
            "fixed_controller_runtime_parent_root_bootstrap_requires_root_owned_no_follow_nonwritable_directories"
        )
        is not True
        or recovery.get(
            "fixed_controller_runtime_parent_root_bootstrap_fsyncs_directories_and_parents"
        )
        is not True
        or recovery.get("runtime_input_staging_is_create_only_and_no_replace")
        is not True
        or recovery.get("runtime_input_staging_exact_terminal_replay_packaged")
        is not True
        or recovery.get(
            "runtime_input_staging_exact_owned_partial_recovery_packaged"
        )
        is not True
        or recovery.get("runtime_input_staging_foreign_or_drifted_state_refused")
        is not True
        or recovery.get("runtime_input_staging_durable_intent_or_receipt_packaged")
        is not False
        or recovery.get(
            "controller_runtime_build_receipt_provenance_v4_packaged"
        )
        is not True
        or recovery.get(
            "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required"
        )
        is not True
        or recovery.get(
            "approved_terminal_postgresql_catalog_manifest_selected"
        )
        is not True
        or recovery.get(
            "empty_rollback_controller_authority_marker_held_from_r04_through_receipt"
        )
        is not True
        or recovery.get(
            "fresh_live_semantic_empty_recheck_required_at_r06_under_administrative_writer_fence"
        )
        is not True
        or recovery.get(
            "destructive_rollback_steps_require_held_controller_authority_marker_and_persisted_r06_fenced_empty_proof"
        )
        is not True
        or recovery.get("stopped_store_semantic_empty_recheck_is_valid")
        is not False
        or recovery.get("durable_live_rollback_controller_authority_marker_transport_packaged")
        is not True
        or recovery.get("administrative_cooperative_writer_fence_implemented")
        is not True
        or recovery.get("equivalent_privileged_root_bypass_excluded")
        is not False
        or recovery.get(
            "controller_authority_marker_then_supervisor_removal_then_writer_fence_empty_recheck_then_store_stop_and_physical_removal_order_implemented"
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
        or not _requires_true_invariants(
            recovery,
            REQUIRED_FINAL_ROLLBACK_ORDERING_INVARIANTS
            | REQUIRED_ROLLBACK_RECOVERY_INVARIANTS,
            reject_stale_ordering=True,
        )
        or recovery.get("selected_live_platform_transport_factory_packaged")
        is not True
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
        or identity.get("exact_rollback_resource_count") != 15
        or identity.get(
            "recovery_capsule_and_proof_supervision_lock_are_rollback_resources"
        )
        is not False
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
        or type(proof_policy) is not dict
        or proof_policy.get("scope")
        != "guarded-synthetic-model-and-separately-authority-gated-disposable-linux-proof-runner"
        or proof_policy.get("guarded_synthetic_harness_packaged") is not True
        or proof_policy.get(
            "authority_gated_disposable_linux_proof_runner_packaged"
        )
        is not True
        or proof_policy.get("disposable_linux_proof_runner")
        != "tools/governed_memory_validation/run_disposable_installation_live_proof.py"
        or proof_policy.get(
            "disposable_linux_proof_runner_requires_exact_distinct_authority"
        )
        is not True
        or proof_policy.get(
            "sealed_runner_packages_fixed_capsule_reader_and_signed_delegation_verifier"
        )
        is not True
        or proof_policy.get(
            "sealed_runner_packages_pre_effect_recovery_reservation_claim_and_verifier"
        )
        is not True
        or proof_policy.get(
            "sealed_runner_packages_atomic_start_authority_pair_claim_and_reverification"
        )
        is not True
        or proof_policy.get(
            "sealed_runner_packages_fresh_process_recovery_without_ephemeral_signer"
        )
        is not True
        or proof_policy.get(
            "sealed_runner_packages_process_local_post_fsync_cooperative_sigstop_barrier"
        )
        is not True
        or not _requires_true_invariants(
            proof_policy,
            REQUIRED_PROOF_FAULT_BARRIER_INVARIANTS,
        )
        or proof_policy.get(
            "sealed_runner_packages_inherited_supervision_lock_validator"
        )
        is not True
        or proof_policy.get(
            "repository_only_issuer_creates_capsule_and_acquires_supervision_lock"
        )
        is not True
        or proof_policy.get("repository_only_issuer_is_excluded_from_sealed_release")
        is not True
        or proof_policy.get(
            "recovery_capsule_is_external_retained_state_not_package_artifact"
        )
        is not True
        or proof_policy.get(
            "recovery_capsule_and_supervision_lock_are_not_rollback_resources"
        )
        is not True
        or proof_policy.get(
            "disposable_linux_proof_runner_executed_before_package_sealing"
        )
        is not False
        or proof_policy.get(
            "live_linux_docker_systemd_postgresql_or_qdrant_proof_claimed"
        )
        is not False
        or proof_policy.get(
            "disposable_linux_proof_receipt_present_at_package_sealing"
        )
        is not False
        or proof_policy.get("package_itself_does_not_claim_proof_execution")
        is not True
        or proof_policy.get("proof_receipt_is_external_to_package") is not True
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
            "install_receipt_binds_canonical_in_memory_image_identity_set_sha256"
        )
        is not True
        or receipts.get(
            "live_proof_receipt_binds_canonical_in_memory_image_identity_set_sha256"
        )
        is not True
        or receipts.get(
            "live_proof_receipt_binds_host_clock_synchronization_preflight_passed"
        )
        is not True
        or receipts.get(
            "live_proof_receipt_binds_recovery_capsule_and_pre_effect_reservation"
        )
        is not True
        or receipts.get(
            "live_proof_receipt_binds_install_authority_claim_and_atomic_start_pair"
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
        or not _requires_true_invariants(
            receipts,
            REQUIRED_FINAL_ROLLBACK_ORDERING_INVARIANTS,
            reject_stale_ordering=True,
        )
        or receipts.get(
            "empty_rollback_receipt_persisted_create_once_while_controller_authority_marker_held"
        )
        is not True
        or receipts.get(
            "empty_rollback_receipt_persistence_create_once_while_controller_authority_marker_held_required"
        )
        is not True
        or receipts.get("canonical_production_executions_root")
        != "/var/lib/governed-memory-controller/executions-v3"
        or receipts.get(
            "public_install_and_rollback_entrypoints_require_root_owned_production_receipt_store"
        )
        is not True
        or receipts.get(
            "synthetic_receipt_stores_accepted_only_by_private_in_process_test_wrappers"
        )
        is not True
        or type(blockers) is not list
        or any(
            retired in blockers
            for retired in (
                "pinned_postgresql_driver_not_selected",
                "concrete_psycopg_postgresql_transport_and_complete_bound_live_transport_factory_not_packaged",
                "production_runtime_publication_primitives_not_packaged",
                "external_direct_writer_exclusion_not_implemented_controller_marker_only",
                "trusted_authority_substrate_not_installed",
                "exact_local_image_identity_receipt_not_published",
            )
        )
        or "durable_recovery_capsule_and_pre_effect_reservation_not_live_executed"
        not in blockers
        or "exact_local_image_digest_and_id_reinspection_pending_for_disposable_live_proof"
        not in blockers
        or "approved_terminal_postgresql_catalog_manifest_not_selected"
        in blockers
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
        != "phase9j-install-ready-closed-runtime-and-store-transports-packaged-not-installed-not-activated"
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
    disposable_linux_proof = plan.get("disposable_linux_proof")
    if (
        type(invariants) is not dict
        or invariants.get("prebootstrap_fresh_empty_readiness_probe_required")
        is not True
        or invariants.get(
            "fresh_terminal_canonical_store_readiness_probe_required_for_install_receipt_and_replay"
        )
        is not True
        or invariants.get(
            "fresh_live_semantic_empty_recheck_required_at_r06_under_administrative_writer_fence"
        )
        is not True
        or invariants.get(
            "recovery_capsule_is_retained_authority_evidence_not_a_rollback_resource"
        )
        is not True
        or invariants.get(
            "proof_supervision_lock_is_lifecycle_coordination_not_a_rollback_resource"
        )
        is not True
        or invariants.get(
            "durable_recovery_reservation_claim_required_before_first_install_effect"
        )
        is not True
        or not _requires_true_invariants(
            invariants, REQUIRED_START_AUTHORITY_PAIR_INVARIANTS
        )
        or not _requires_true_invariants(
            invariants,
            REQUIRED_PROOF_FAULT_BARRIER_INVARIANTS,
        )
        or invariants.get(
            "derived_rollback_authority_binds_post_effect_exact_ledger_and_empty_eligibility"
        )
        is not True
        or invariants.get("closed_live_transport_contracts_packaged") is not True
        or invariants.get("complete_closed_live_transport_substrate_set_packaged") is not True
        or invariants.get(
            "complete_closed_live_transport_substrate_set_integrated_into_bound_factory"
        )
        is not True
        or invariants.get("driver_native_postgresql_stage_contract_packaged")
        is not True
        or invariants.get(
            "driver_native_postgresql_executable_stage_machine_packaged"
        )
        is not True
        or invariants.get("concrete_psycopg_postgresql_transport_packaged")
        is not True
        or invariants.get(
            "controller_runtime_build_receipt_provenance_v4_packaged"
        )
        is not True
        or invariants.get(
            "runtime_input_selection_and_incoming_artifacts_verified_for_closed_plan"
        )
        is not True
        or invariants.get("current_offline_runtime_inputs_published") is not False
        or invariants.get(
            "public_install_entrypoint_internally_selects_production_journal_ledger_transports_secret_sources_postgresql_receipt_sink_and_host_operations"
        )
        is not True
        or invariants.get(
            "public_install_entrypoint_accepts_caller_selected_dependencies_or_audit_state"
        )
        is not False
        or invariants.get("fixed_controller_runtime_parent_root_bootstrap_packaged")
        is not True
        or invariants.get(
            "approved_terminal_postgresql_catalog_manifest_selected"
        )
        is not True
        or invariants.get("durable_live_rollback_controller_authority_marker_transport_packaged")
        is not True
        or invariants.get("administrative_cooperative_writer_fence_implemented")
        is not True
        or invariants.get("equivalent_privileged_root_bypass_excluded")
        is not False
        or invariants.get("selected_live_platform_transports_packaged")
        is not True
        or invariants.get(
            "selected_non_postgresql_live_platform_transport_factory_packaged"
        )
        is not True
        or disposable_linux_proof
        != {
            "authority_gated_runner_packaged": True,
            "runner": "tools/governed_memory_validation/run_disposable_installation_live_proof.py",
            "exact_distinct_proof_authority_required": True,
            "sealed_runner_packages_fixed_capsule_reader_and_signed_delegation_verifier": True,
            "sealed_runner_packages_pre_effect_recovery_reservation_claim_and_verifier": True,
            "sealed_runner_packages_atomic_start_authority_pair_claim_and_reverification": True,
            "live_proof_receipt_binds_install_authority_claim_and_atomic_start_pair": True,
            "live_proof_receipt_binds_install_and_rollback_process_death_arm_receipts": True,
            "sealed_runner_packages_fresh_process_recovery_without_ephemeral_signer": True,
            "sealed_runner_packages_worker_parent_death_sigkill_fence": True,
            "sealed_runner_packages_process_local_post_fsync_cooperative_sigstop_barrier": True,
            "proof_fault_barrier_is_process_local_and_fixed_to_first_install_i04_and_first_empty_rollback_r05_workers": True,
            "proof_fault_worker_self_stops_only_after_durable_journal_append_readback_and_anchor_reconciliation": True,
            "proof_parent_never_issues_sigstop": True,
            "process_death_arm_requires_boundary_as_exact_journal_head": True,
            "process_death_arm_requires_single_worker_task_and_empty_child_process_set": True,
            "sigkill_and_exact_reap_required_before_stopped_worker_can_resume": True,
            "public_install_and_rollback_entrypoints_are_unmodified_by_proof_fault_barrier": True,
            "sealed_runner_packages_create_once_process_death_arm_receipts": True,
            "sealed_runner_packages_two_arm_exact_live_receipt_reconstruction": True,
            "recover_only_pair_without_execution_never_initiates_install": True,
            "sealed_runner_packages_inherited_supervision_lock_validator": True,
            "repository_only_issuer_is_excluded_from_sealed_release": True,
            "recovery_capsule_is_external_retained_state_not_package_artifact": True,
            "recovery_capsule_and_supervision_lock_are_not_rollback_resources": True,
            "runner_executed_before_package_sealing": False,
            "disposable_linux_proof_receipt_present_at_package_sealing": False,
            "package_itself_does_not_claim_proof_execution": True,
            "proof_receipt_is_external_to_package": True,
            "live_install_crash_resume_empty_rollback_or_absence_proven": False,
        }
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
            "preclaimed_recovery_reservation_required_for_expired_delegation_recovery"
        )
        is not True
        or later_rollback.get("recovery_capsule_retained_after_empty_rollback")
        is not True
        or later_rollback.get(
            "completed_replay_reproves_exact_resource_absence"
        )
        is not True
        or not _requires_true_invariants(
            later_rollback,
            REQUIRED_FINAL_ROLLBACK_ORDERING_INVARIANTS
            | REQUIRED_ROLLBACK_RECOVERY_INVARIANTS,
            reject_stale_ordering=True,
        )
        or later_rollback.get(
            "fresh_live_semantic_empty_recheck_required_before_first_destructive_step"
        )
        is not True
        or later_rollback.get(
            "controller_authority_marker_held_through_terminal_absence_retained_audit_and_receipt"
        )
        is not True
        or later_rollback.get(
            "destructive_steps_require_held_controller_authority_marker_and_persisted_r06_fenced_empty_proof"
        )
        is not True
        or later_rollback.get("stopped_store_semantic_empty_recheck_is_valid")
        is not False
        or later_rollback.get("durable_live_rollback_controller_authority_marker_transport_packaged")
        is not True
        or later_rollback.get(
            "administrative_cooperative_writer_fence_implemented"
        )
        is not True
        or later_rollback.get("equivalent_privileged_root_bypass_excluded")
        is not False
        or later_rollback.get("selected_live_platform_driver_packaged")
        is not True
        or later_rollback.get(
            "selected_non_postgresql_live_platform_transport_factory_packaged"
        )
        is not True
    ):
        raise PackageError("dormant_store_install_plan_replay_boundary_invalid")


def _verify_postgres_native_stage_contract(contract: dict[str, object]) -> None:
    if (
        _canonical_sha256(contract)
        != EXPECTED_POSTGRES_NATIVE_STAGE_CONTRACT_CANONICAL_SHA256
        or contract.get("schema_version")
        != "governed-memory-postgres-native-stage-contract-v4"
        or contract.get("state")
        != "source-closed-concrete-psycopg-transport-exact-prefix-machine-runtime-bound-terminal-catalog-disposable-selected-inactive"
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
        or endpoint.get("port") != 55434
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
        is not True
        or session.get("set_role_owner_and_current_user_verification_required")
        is not True
        or session.get("set_role_owner_and_current_user_verification_implemented")
        is not True
        or session.get("privacy_settings_verified_before_mutation") is not True
        or session.get("external_client_count_zero_verified_before_mutation")
        is not True
        or type(preferred_driver) is not dict
        or preferred_driver.get("selection_state")
        != "exact-runtime-probed-capability-bound"
        or preferred_driver.get("python_version_target") != "3.12.13"
        or preferred_driver.get("api_style") != "synchronous"
        or preferred_driver.get("preferred_package") != "psycopg"
        or preferred_driver.get("preferred_extra") != "binary"
        or preferred_driver.get("preferred_version") != "3.3.4"
        or preferred_driver.get("required_distributions")
        != ["psycopg", "psycopg-binary"]
        or preferred_driver.get("selected_wheels")
        != [
            "psycopg-3.3.4-py3-none-any.whl",
            "psycopg_binary-3.3.4-cp312-cp312-manylinux2014_x86_64."
            "manylinux_2_17_x86_64.whl",
        ]
        or preferred_driver.get("preference_contract_packaged") is not True
        or preferred_driver.get("exact_driver_identity_contract_packaged")
        is not True
        or preferred_driver.get("runtime_driver_identity_sha256")
        != "364760713fd35d8d7029c972e9cc23ec69f3b5c4a9a1ce6bab302a525f0ef8fa"
        or type(preferred_driver.get("runtime_driver_probe")) is not dict
        or _canonical_sha256(preferred_driver["runtime_driver_probe"])
        != "364760713fd35d8d7029c972e9cc23ec69f3b5c4a9a1ce6bab302a525f0ef8fa"
        or preferred_driver.get("independent_native_audit_identity_sha256")
        != "bb6714cb1f3cead78935ae10f9e2ba630f4e9266c208395c6292ff0667f5f7ee"
        or preferred_driver.get("pq_impl") != "binary"
        or preferred_driver.get("libpq_version") != 180000
        or preferred_driver.get("native_file_count") != 17
        or preferred_driver.get("exact_wheel_filenames_frozen") is not True
        or preferred_driver.get("wheel_bytes_staged") is not True
        or preferred_driver.get("wheel_bytes_verified") is not True
        or preferred_driver.get("binary_native_library_closure_inspected")
        is not True
        or preferred_driver.get("runtime_receipt_selected") is not True
        or preferred_driver.get("ready") is not True
        or type(stages) is not list
        or tuple(
            stage.get("stage_id") if type(stage) is dict else None
            for stage in stages
        )
        != EXPECTED_POSTGRES_NATIVE_STAGE_IDS
        or type(catalog) is not dict
        or catalog.get("caller_expected_catalog_allowed") is not False
        or catalog.get("postgresql_parse_or_execution_proven") is not True
        or catalog.get("approved_manifest_selected") is not True
        or catalog.get("approved_normalized_catalog_sha256")
        != "c37620ecb2d1f9a771ea67ce4a71f1d15700f26dba01d4386d70e801a692e1cc"
        or rollback_marker
        != {
            "controller_authority_marker_required_before_first_observation": True,
            "semantic_empty_proof_required_before_first_destructive_operation": True,
            "empty_proof_bound_across_database_absence_resume": True,
            "controller_authority_marker_held_through_final_receipt_persistence": True,
            "administrative_cooperative_writer_fence_implemented": True,
            "equivalent_privileged_root_bypass_excluded": False,
            "loopback_only_endpoint": True,
            "advisory_lock_and_zero_external_client_observation_required": True,
            "exact_prefix_only_resume": True,
        }
        or type(coverage) is not dict
        or not coverage
        or not all(value is True for value in coverage.values())
        or type(gate) is not dict
        or gate.get("fixed_orchestration_machine_packaged") is not True
        or gate.get("fixed_catalog_query_contract_packaged") is not True
        or gate.get("operation_to_sql_translation_packaged") is not True
        or gate.get("concrete_psycopg_adapter_packaged") is not True
        or gate.get("runtime_ready") is not True
        or gate.get("driver_ready") is not True
        or gate.get("catalog_ready") is not True
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
    postgres_native_stage = _load_verified_json(
        POSTGRES_NATIVE_STAGE_CONTRACT_RELATIVE,
        observed[POSTGRES_NATIVE_STAGE_CONTRACT_RELATIVE],
    )
    proof = _load_verified_json(
        PROOF_CONTRACT_RELATIVE,
        observed[PROOF_CONTRACT_RELATIVE],
    )
    proof_schema = _load_verified_json(
        SYNTHETIC_PROOF_SCHEMA_RELATIVE,
        observed[SYNTHETIC_PROOF_SCHEMA_RELATIVE],
    )
    live_proof_schema = _load_verified_json(
        LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE,
        observed[LIVE_PROOF_RECEIPT_SCHEMA_RELATIVE],
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
            postgres_native_stage,
            EXPECTED_POSTGRES_NATIVE_STAGE_CONTRACT_CANONICAL_SHA256,
        ),
        (proof, EXPECTED_PROOF_CONTRACT_CANONICAL_SHA256),
        (
            proof_schema,
            EXPECTED_SYNTHETIC_PROOF_RECEIPT_SCHEMA_CANONICAL_SHA256,
        ),
        (
            live_proof_schema,
            EXPECTED_LIVE_PROOF_RECEIPT_SCHEMA_CANONICAL_SHA256,
        ),
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
    proof_harnesses = proof.get("harnesses")
    synthetic_proof_harness = (
        proof_harnesses.get("guarded_synthetic_matrix")
        if type(proof_harnesses) is dict
        else None
    )
    live_proof_harness = (
        proof_harnesses.get("authority_gated_disposable_linux")
        if type(proof_harnesses) is dict
        else None
    )
    proof_disposable_claims = proof.get("disposable_linux_runner_claims")
    proof_package_context = proof.get("package_context")
    if (
        execution.get("schema_version")
        != "governed-memory-dormant-store-install-inactive-execution-package-v4"
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
        is not True
        or boundary.get(
            "complete_closed_live_transport_substrate_set_integrated_into_bound_factory"
        )
        is not True
        or boundary.get("driver_native_postgresql_stage_contract_implemented")
        is not True
        or boundary.get(
            "driver_native_postgresql_executable_stage_machine_implemented"
        )
        is not True
        or boundary.get("concrete_psycopg_postgresql_transport_implemented")
        is not True
        or boundary.get(
            "controller_runtime_input_selection_contract_repaired"
        )
        is not True
        or boundary.get(
            "controller_runtime_capability_refuses_unready_selected_inputs_before_probe"
        )
        is not True
        or boundary.get(
            "controller_runtime_build_receipt_provenance_v4_implemented"
        )
        is not True
        or boundary.get(
            "approved_terminal_postgresql_catalog_manifest_selected"
        )
        is not True
        or boundary.get("selected_live_linux_platform_transport_factory_implemented")
        is not True
        or boundary.get(
            "selected_non_postgresql_live_linux_platform_transport_factory_implemented"
        )
        is not True
        or boundary.get("pinned_postgresql_live_driver_selected") is not True
        or boundary.get(
            "controller_runtime_release_builder_orchestration_implemented"
        )
        is not True
        or boundary.get("controller_runtime_build_transport_implemented") is not True
        or boundary.get(
            "controller_runtime_publication_policy_transport_implemented"
        )
        is not True
        or boundary.get("production_runtime_publication_primitives_implemented")
        is not True
        or boundary.get("runtime_input_stager_implemented") is not True
        or boundary.get("standalone_cpython_substrate_inspector_implemented")
        is not True
        or boundary.get("fixed_controller_runtime_parent_root_bootstrap_packaged")
        is not True
        or boundary.get(
            "public_install_entrypoint_internally_selects_production_journal_ledger_transports_secret_sources_postgresql_receipt_sink_and_host_operations"
        )
        is not True
        or boundary.get(
            "public_install_entrypoint_accepts_caller_selected_dependencies_or_audit_state"
        )
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
            "canonical_dormant_install_authority_identity_derivation_shared_by_install_and_rollback"
        )
        is not True
        or binding.get(
            "opaque_retained_rollback_evidence_sha256_bound_into_final_rollback_execution_and_claim"
        )
        is not True
        or binding.get(
            "empty_rollback_requires_opaque_verified_install_receipt_and_ledger_capability"
        )
        is not True
        or binding.get("signed_public_pre_effect_recovery_delegation_required")
        is not True
        or binding.get(
            "durable_recovery_reservation_claim_required_before_first_install_effect"
        )
        is not True
        or not _requires_true_invariants(
            binding,
            frozenset(
                {
                    "recovery_reservation_and_install_authority_claimed_atomically_before_capsule_publication_and_first_install_effect",
                    "published_recovery_capsule_requires_exact_preclaimed_start_authority_pair",
                }
            ),
        )
        or binding.get(
            "derived_rollback_authority_binds_post_effect_exact_ledger_and_empty_eligibility"
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
        or durability.get("fresh_process_recovery_without_ephemeral_signer_packaged")
        is not True
        or not _requires_true_invariants(
            durability,
            REQUIRED_PROOF_FAULT_BARRIER_INVARIANTS,
        )
        or durability.get(
            "expired_recovery_delegation_usable_only_with_matching_preclaimed_reservation"
        )
        is not True
        or durability.get(
            "proof_supervision_lock_serializes_full_run_and_recover_lifecycle"
        )
        is not True
        or durability.get(
            "recovery_capsule_retained_and_never_removed_by_store_rollback"
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
            "fresh_live_semantic_empty_recheck_required_at_r06_under_administrative_writer_fence"
        )
        is not True
        or durability.get(
            "destructive_rollback_steps_require_held_controller_authority_marker_and_persisted_r06_fenced_empty_proof"
        )
        is not True
        or durability.get("stopped_store_semantic_empty_recheck_is_valid")
        is not False
        or durability.get("durable_live_rollback_controller_authority_marker_transport_implemented")
        is not True
        or durability.get("administrative_cooperative_writer_fence_implemented")
        is not True
        or durability.get("equivalent_privileged_root_bypass_excluded")
        is not False
        or durability.get(
            "controller_authority_marker_then_supervisor_removal_then_writer_fence_empty_recheck_then_store_stop_and_physical_removal_order_implemented"
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
        or not _requires_true_invariants(
            durability,
            REQUIRED_FINAL_ROLLBACK_ORDERING_INVARIANTS
            | REQUIRED_ROLLBACK_RECOVERY_INVARIANTS,
            reject_stale_ordering=True,
        )
        or type(execution.get("remaining_blockers")) is not list
        or any(
            retired in execution["remaining_blockers"]
            for retired in (
                "pinned_postgresql_driver_not_selected",
                "concrete_psycopg_postgresql_transport_and_complete_bound_live_transport_factory_not_packaged",
                "production_runtime_publication_primitives_not_packaged",
                "external_direct_writer_exclusion_not_implemented_controller_marker_only",
            )
        )
        or "approved_terminal_postgresql_catalog_manifest_not_selected"
        in execution["remaining_blockers"]
        or any(
            retired in execution["remaining_blockers"]
            for retired in (
                "exact_local_image_identity_receipt_not_published",
                "authority_substrate_and_trusted_clock_not_installed",
                "host_clock_synchronization_preflight_not_implemented",
            )
        )
        or (
            "canonical_install_receipt_emission_not_integrated"
            in execution["remaining_blockers"]
        )
        or type(host) is not dict
        or host.get("typed_operation_specific_boundaries_only") is not True
        or host.get("bounded_image_inspection_command_primitive_packaged")
        is not True
        or host.get("fixed_host_clock_synchronization_preflight_binary")
        != "/usr/bin/timedatectl"
        or host.get("fixed_host_clock_synchronization_preflight_arguments")
        != ["show", "--property=NTPSynchronized", "--value"]
        or host.get("fixed_host_clock_synchronization_preflight_exact_stdout")
        != "yes\n"
        or host.get("caller_selected_clock_command_or_path_allowed") is not False
        or host.get(
            "image_inspection_projection_is_id_repo_digests_os_architecture_only"
        )
        is not True
        or host.get(
            "store_supervisor_observations_use_narrow_nonsecret_fields_only"
        )
        is not True
        or type(receipt) is not dict
        or receipt.get(
            "live_proof_receipt_binds_host_clock_synchronization_preflight_passed"
        )
        is not True
        or receipt.get(
            "live_proof_receipt_binds_recovery_capsule_and_pre_effect_reservation"
        )
        is not True
        or receipt.get(
            "live_proof_receipt_binds_install_authority_claim_and_atomic_start_pair"
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
        or host.get("complete_closed_live_transport_substrate_set_packaged") is not True
        or host.get("driver_native_postgresql_stage_contract_packaged")
        is not True
        or host.get(
            "driver_native_postgresql_executable_stage_machine_packaged"
        )
        is not True
        or host.get("concrete_psycopg_postgresql_transport_packaged")
        is not True
        or host.get(
            "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required"
        )
        is not True
        or host.get("approved_terminal_postgresql_catalog_manifest_selected")
        is not True
        or host.get("selected_live_platform_transports_packaged") is not True
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
        or not _requires_true_invariants(
            receipt,
            REQUIRED_FINAL_ROLLBACK_ORDERING_INVARIANTS,
            reject_stale_ordering=True,
        )
        or receipt.get("canonical_production_executions_root")
        != "/var/lib/governed-memory-controller/executions-v3"
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
        != "guarded-synthetic-and-authority-gated-disposable-linux"
        or proof_boundary.get("guarded_synthetic_harness_packaged") is not True
        or proof_boundary.get(
            "authority_gated_disposable_linux_proof_runner_packaged"
        )
        is not True
        or proof_boundary.get("disposable_linux_proof_runner")
        != "tools/governed_memory_validation/run_disposable_installation_live_proof.py"
        or proof_boundary.get(
            "sealed_runner_packages_process_local_post_fsync_cooperative_sigstop_barrier"
        )
        is not True
        or not _requires_true_invariants(
            proof_boundary,
            REQUIRED_PROOF_FAULT_BARRIER_INVARIANTS,
        )
        or proof_boundary.get(
            "disposable_linux_proof_runner_executed_before_package_sealing"
        )
        is not False
        or proof_boundary.get(
            "disposable_linux_proof_receipt_present_at_package_sealing"
        )
        is not False
        or proof_boundary.get("package_itself_does_not_claim_proof_execution")
        is not True
        or proof_boundary.get("proof_receipt_is_external_to_package") is not True
        or type(dependency) is not dict
        or dependency.get(
            "locked_distribution_set_must_exactly_equal_installed_normalized_distribution_set"
        )
        is not True
        or dependency.get("current_lock_contains_preferred_postgresql_driver")
        is not True
        or type(selected_cpython) is not dict
        or selected_cpython.get("python_version") != "3.12.13"
        or selected_cpython.get("archive_sha256")
        != "506191be3ee7bd190a8834dcdc1b3bc70aab50608deccc711935aa007239cabd"
        or selected_cpython.get("archive_bytes") != 34163738
        or selected_cpython.get("archive_staged") is not True
        or selected_cpython.get("archive_bytes_sha256_verified_locally")
        is not True
        or selected_cpython.get("archive_member_types_verified") is not True
        or selected_cpython.get("archive_symlink_count") != 1048
        or selected_cpython.get("archive_symlink_normalization_verified")
        is not True
        or selected_cpython.get("symlink_expansion_mapping_sha256")
        != "38bd37b4098179fe5997e9e5709eb833d4cfcfa2685cfad89d3f6676a2a87ac7"
        or selected_cpython.get("specification_sha256")
        != "883564be18c159544f8785ace4ad1b46abaa2989bb76ad9f03f7a8299d862a85"
        or selected_cpython.get("expanded_payload_tree_sha256")
        != "9eb6554a9807d955e8f2902d058d81e6d57881a8409fb361884297f80e153db1"
        or type(selected_driver) is not dict
        or selected_driver.get("api_style") != "synchronous"
        or selected_driver.get("preferred_extra") != "binary"
        or selected_driver.get("selection_state")
        != "exact-selected-wheels-staged-verified-and-locked"
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
        or selected_driver.get("selected_wheels")
        != [
            {
                "normalized_distribution": "psycopg",
                "selected_wheel_filename": "psycopg-3.3.4-py3-none-any.whl",
                "selected_wheel_sha256": "b6bbc25ccf05c8fad3b061d9db2ef0909a555171b84b07f29458a447253d679a",
                "version": "3.3.4",
            },
            {
                "normalized_distribution": "psycopg-binary",
                "selected_wheel_filename": "psycopg_binary-3.3.4-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
                "selected_wheel_sha256": "e7510c37550f91a187e3660a8cc50d4b760f8c3b8b2f89ebc5698cd2c7f2c85d",
                "version": "3.3.4",
            },
        ]
        or any(
            selected_driver.get(key) is not True
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
        is not True
        or selected_driver.get("runtime_driver_identity_sha256")
        != "364760713fd35d8d7029c972e9cc23ec69f3b5c4a9a1ce6bab302a525f0ef8fa"
        or type(selected_wheelhouse) is not dict
        or selected_wheelhouse.get("selection_state")
        != "exact-six-wheel-canonical-wheelhouse-staged-and-verified"
        or selected_wheelhouse.get("canonical_tree_sha256")
        != "d802e5000dab609a08d08438b37aace108cd07872246f48fcfe322cd9f028fb4"
        or selected_wheelhouse.get("canonical_member_count") != 6
        or selected_wheelhouse.get("canonical_total_bytes") != 10429916
        or selected_wheelhouse.get("wheelhouse_staged") is not True
        or type(build) is not dict
        or build.get("current_runtime_built") is not False
        or build.get("current_runtime_installed") is not False
        or build.get("current_release_staged") is not False
        or build.get("current_controller_runtime_parent_roots_bootstrapped")
        is not False
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
        or build.get("concrete_runtime_build_transport_packaged") is not True
        or build.get("runtime_publication_policy_transport_packaged") is not True
        or build.get("production_runtime_publication_primitives_packaged")
        is not True
        or build.get("runtime_input_stager_packaged") is not True
        or build.get("fixed_controller_runtime_parent_root_bootstrap_packaged")
        is not True
        or build.get("independent_standalone_cpython_payload_tree_proof_packaged")
        is not True
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
        != "governed-memory-controller-runtime-build-receipt-v4"
        or runtime.get("runtime_build_receipt_policy", {}).get(
            "postgresql_driver_identity_sha256_required"
        )
        is not True
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
        or entrypoint.get(
            "public_install_entrypoint_internally_selects_production_journal_ledger_transports_secret_sources_postgresql_receipt_sink_and_host_operations"
        )
        is not True
        or entrypoint.get(
            "public_install_entrypoint_accepts_caller_selected_dependencies_or_audit_state"
        )
        is not False
        or proof.get("schema_version")
        != "governed-memory-dormant-store-install-disposable-proof-contract-v3"
        or type(proof_harnesses) is not dict
        or type(synthetic_proof_harness) is not dict
        or synthetic_proof_harness.get("packaged") is not True
        or synthetic_proof_harness.get("executed_before_package_sealing")
        is not False
        or synthetic_proof_harness.get(
            "synthetic_proof_receipt_present_at_package_sealing"
        )
        is not False
        or type(live_proof_harness) is not dict
        or live_proof_harness.get("entrypoint")
        != "tools/governed_memory_validation/run_disposable_installation_live_proof.py"
        or live_proof_harness.get("packaged") is not True
        or live_proof_harness.get("requires_exact_distinct_proof_authority")
        is not True
        or live_proof_harness.get(
            "sealed_runner_packages_fixed_capsule_reader_and_signed_delegation_verifier"
        )
        is not True
        or live_proof_harness.get(
            "sealed_runner_packages_pre_effect_recovery_reservation_claim_and_verifier"
        )
        is not True
        or live_proof_harness.get(
            "sealed_runner_packages_atomic_start_authority_pair_claim_and_reverification"
        )
        is not True
        or live_proof_harness.get(
            "sealed_runner_packages_fresh_process_recovery_without_ephemeral_signer"
        )
        is not True
        or live_proof_harness.get(
            "sealed_runner_packages_process_local_post_fsync_cooperative_sigstop_barrier"
        )
        is not True
        or not _requires_true_invariants(
            live_proof_harness,
            REQUIRED_PROOF_FAULT_BARRIER_INVARIANTS,
        )
        or live_proof_harness.get(
            "sealed_runner_packages_inherited_supervision_lock_validator"
        )
        is not True
        or live_proof_harness.get(
            "repository_only_issuer_creates_capsule_and_acquires_supervision_lock"
        )
        is not True
        or live_proof_harness.get(
            "repository_only_issuer_is_excluded_from_sealed_release"
        )
        is not True
        or live_proof_harness.get(
            "recovery_capsule_is_external_retained_state_not_package_artifact"
        )
        is not True
        or live_proof_harness.get(
            "recovery_capsule_retained_after_terminal_observation"
        )
        is not True
        or live_proof_harness.get("host_reboot_recovery_packaged_or_proven")
        is not False
        or live_proof_harness.get("issuer_or_host_death_cleanup_proven")
        is not False
        or live_proof_harness.get("executed_before_package_sealing")
        is not False
        or live_proof_harness.get(
            "disposable_linux_proof_receipt_present_at_package_sealing"
        )
        is not False
        or type(proof_disposable_claims) is not dict
        or proof_disposable_claims.get("runner_packaged") is not True
        or any(
            proof_disposable_claims.get(key) is not False
            for key in (
                "runner_executed_before_package_sealing",
                "disposable_linux_proof_receipt_present_at_package_sealing",
                "live_execution_proven",
                "live_installation_proven",
                "live_rollback_proven",
                "durable_process_crash_recovery_proven",
                "cold_controller_process_restart_proven",
                "fresh_process_recovery_without_ephemeral_signer_proven",
                "recovery_capsule_retained_at_terminal_observation_proven",
                "issuer_or_host_death_cleanup_proven",
                "host_reboot_recovery_proven",
                "persistent_store_boot_recovery_proven",
                "exact_resource_absence_proven",
                "container_and_systemd_compatibility_proven",
            )
        )
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
        or proof_claims.get(
            "sealed_runner_supports_process_local_post_fsync_cooperative_sigstop_barrier"
        )
        is not True
        or proof_claims.get("process_death_arm_receipt_schema_version")
        != "governed-memory-phase9-process-death-arm-receipt-v2"
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
        or proof_package_context.get(
            "sealed_runner_packages_process_local_post_fsync_cooperative_sigstop_barrier"
        )
        is not True
        or not _requires_true_invariants(
            proof_package_context,
            REQUIRED_PROOF_FAULT_BARRIER_INVARIANTS,
        )
        or proof_schema.get("additionalProperties") is not False
        or proof_schema.get("$id")
        != "urn:governed-memory:dormant-store-install:disposable-proof-receipt:v2"
        or live_proof_schema.get("additionalProperties") is not False
        or live_proof_schema.get("$id")
        != "urn:governed-memory:phase9:disposable-live-proof-receipt:v4"
        or live_proof_schema.get("properties", {}).get(
            "schema_version", {}
        ).get("const")
        != "governed-memory-phase9-live-proof-receipt-v4"
        or live_proof_schema.get("properties", {}).get(
            "host_clock_synchronization_preflight_passed", {}
        ).get("const")
        is not True
        or "host_clock_synchronization_preflight_passed"
        not in live_proof_schema.get("required", ())
        or live_proof_schema.get("properties", {}).get(
            "start_authority_pair_claimed_atomically", {}
        ).get("const")
        is not True
        or live_proof_schema.get("properties", {}).get(
            "install_authority_claim_sha256", {}
        ).get("$ref")
        != "#/$defs/sha256"
        or not {
            "recovery_capsule_sha256",
            "recovery_reservation_claim_sha256",
            "install_authority_claim_sha256",
            "start_authority_pair_claimed_atomically",
            "recovery_capsule_published_before_first_install_effect",
            "recovery_reservation_claimed_before_first_install_effect",
            "ephemeral_private_signer_retained_at_execution_start",
            "recovery_capsule_retained_at_terminal_observation",
            "exact_rollback_resources_absent_count",
            "install_process_death_arm_receipt_sha256",
            "rollback_process_death_arm_receipt_sha256",
        }
        <= set(live_proof_schema.get("required", ()))
        or live_proof_schema.get("properties", {}).get(
            "host_reboot_proven", {}
        ).get("const")
        is not False
        or live_proof_schema.get("properties", {}).get(
            "issuer_or_host_death_durable_cleanup_proven", {}
        ).get("const")
        is not False
        or install_receipt_schema.get("additionalProperties") is not False
        or empty_rollback_receipt_schema.get("additionalProperties") is not False
        or install_receipt_schema.get("$id")
        != "urn:governed-memory:dormant-store-install-receipt:v1"
        or empty_rollback_receipt_schema.get("$id")
        != "urn:governed-memory:empty-store-rollback-receipt:v4"
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
        != "governed-memory-dormant-store-install-inactive-execution-package-manifest-v6"
        or manifest.get("state")
        != "phase9j-install-ready-closed-runtime-and-store-transports-packaged-not-installed-not-activated"
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
        "schema_version": "governed-memory-dormant-store-install-package-verification-v6",
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
        "postgres_native_stage_contract_canonical_sha256": (
            EXPECTED_POSTGRES_NATIVE_STAGE_CONTRACT_CANONICAL_SHA256
        ),
        "proof_contract_canonical_sha256": (
            EXPECTED_PROOF_CONTRACT_CANONICAL_SHA256
        ),
        "synthetic_proof_receipt_schema_canonical_sha256": (
            EXPECTED_SYNTHETIC_PROOF_RECEIPT_SCHEMA_CANONICAL_SHA256
        ),
        "live_proof_receipt_schema_canonical_sha256": (
            EXPECTED_LIVE_PROOF_RECEIPT_SCHEMA_CANONICAL_SHA256
        ),
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
        "authority_gated_disposable_linux_proof_runner_packaged": True,
        "disposable_linux_proof_runner_executed_by_verifier": False,
        "disposable_linux_proof_receipt_present_at_package_sealing": False,
        "proof_receipt_is_external_to_package": True,
        "synthetic_proof_executed_by_verifier": False,
        "synthetic_proof_receipt_present_at_package_sealing": False,
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
        "controller_runtime_parent_roots_bootstrapped": False,
        "offline_runtime_inputs_published": False,
        "controller_runtime_and_release_require_separate_future_build_and_install_authority": True,
        "stores_install_owns_or_removes_controller_substrate": False,
        "resolved_store_spec_and_exact_docker_labels_bound": True,
        "resource_identity_ledger_v2_packaged": True,
        "empty_rollback_controller_authority_marker_packaged": True,
        "durable_live_empty_rollback_controller_authority_marker_transport_packaged": True,
        "administrative_cooperative_writer_fence_implemented": True,
        "equivalent_privileged_root_bypass_excluded": False,
        "empty_rollback_receipt_persisted_create_once_while_controller_authority_marker_held": True,
        "fresh_live_semantic_empty_recheck_required_at_r06_under_administrative_writer_fence": True,
        "controller_authority_marker_then_supervisor_removal_then_writer_fence_empty_recheck_then_store_stop_and_physical_removal_order_implemented": True,
        "closed_live_transport_contracts_packaged": True,
        "complete_closed_live_transport_substrate_set_packaged": True,
        "complete_closed_live_transport_substrate_set_integrated_into_bound_factory": True,
        "driver_native_postgresql_stage_contract_packaged": True,
        "driver_native_postgresql_executable_stage_machine_packaged": True,
        "concrete_psycopg_postgresql_transport_packaged": True,
        "runtime_input_selection_contract_repaired": True,
        "runtime_build_receipt_provenance_v4_packaged": True,
        "exact_postgresql_16_14_and_qdrant_1_19_0_readiness_required": True,
        "approved_terminal_postgresql_catalog_manifest_selected": True,
        "stopped_store_semantic_empty_recheck_is_valid": False,
        "retained_audit_artifact_hashes_bound": True,
        "public_entrypoints_require_canonical_root_owned_production_receipt_store": True,
        "synthetic_receipt_stores_are_private_test_only": True,
        "claim_bound_install_controller_composition_packaged": True,
        "public_install_entrypoint_internally_selects_production_dependencies_and_audit_state": True,
        "public_install_entrypoint_accepts_caller_selected_dependencies_or_audit_state": False,
        "claim_bound_empty_rollback_controller_composition_packaged": True,
        "closed_install_store_effect_adapter_packaged": True,
        "closed_empty_rollback_store_effect_adapter_packaged": True,
        "selected_live_platform_transports_packaged": True,
        "selected_non_postgresql_live_platform_transport_factory_packaged": True,
        "pinned_postgresql_driver_selected": True,
        "durable_create_once_receipt_store_packaged": True,
        "controller_runtime_release_builder_orchestration_packaged": True,
        "controller_runtime_build_transport_packaged": True,
        "controller_runtime_publication_policy_transport_packaged": True,
        "runtime_publication_durable_intent_before_first_rename_packaged": True,
        "runtime_publication_post_intent_generic_cleanup_forbidden": True,
        "runtime_publication_crash_prefix_manual_review_fence_packaged": True,
        "runtime_publication_same_device_rename_precondition_packaged": True,
        "runtime_publication_exact_terminal_replay_with_renewed_fsyncs_packaged": True,
        "production_runtime_publication_primitives_packaged": True,
        "runtime_input_stager_packaged": True,
        "fixed_controller_runtime_parent_root_bootstrap_packaged": True,
        "fixed_controller_runtime_parent_root_bootstrap_is_parameterless_and_idempotent": True,
        "runtime_input_staging_is_create_only_and_no_replace": True,
        "runtime_input_staging_exact_terminal_replay_packaged": True,
        "runtime_input_staging_exact_owned_partial_recovery_packaged": True,
        "runtime_input_staging_foreign_or_drifted_state_refused": True,
        "runtime_input_staging_durable_intent_or_receipt_packaged": False,
        "independent_standalone_cpython_payload_tree_proof_packaged": True,
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

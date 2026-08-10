"""Source-inventory and installed candidate-runtime identity checks."""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import subprocess
import tomllib
import unicodedata
import unittest

from rag_engine.governed_memory.contracts import ContractViolation, canonical_sha256
from rag_engine.governed_memory.auth import ActorRole
from rag_engine.governed_memory.api import OWNER_ROUTE_SPECIFICATIONS
from rag_engine.governed_memory.repository import GovernedMemoryRepository
from tools.governed_memory_release.build_candidate_runtime import (
    PROVIDER_ASSET_SOURCE_PATHS,
    _package_source_material,
    _package_source_tree_sha256,
    _source_tree_sha256,
)


FIXTURE_PROVENANCE = "synthetic-governed-memory-successor"
ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "rag_engine" / "governed_memory"
RUNTIME_PACKAGE = PACKAGE / "runtime"
PROVIDER_ASSETS = PACKAGE / "provider_assets"
MANIFEST = ROOT / "ops" / "governed_memory" / "runtime_manifest.json"
RUNTIME_LOCK = ROOT / "ops" / "governed_memory" / "runtime-requirements.lock"
BUILD_LOCK = ROOT / "ops" / "governed_memory" / "build-requirements.lock"
SUCCESSOR_README = ROOT / "docs" / "memory" / "clean_successor" / "README.md"
SCHEMA_CONTRACT = ROOT / "governed-memory-migrations" / "schema_contract.json"
INTEGRATION_TESTS = ROOT / "tests" / "memory_integration"
VALIDATION_TOOLS = ROOT / "tools" / "governed_memory_validation"
RELEASE_TOOLS = ROOT / "tools" / "governed_memory_release"
CLEAN_SUCCESSOR_DOCS = ROOT / "docs" / "memory" / "clean_successor"
RUNTIME_PACKAGES = VALIDATION_TOOLS / "runtime_packages.json"
RUNTIME_BUILD_RECEIPT = ROOT / "ops" / "governed_memory" / "runtime_build_receipt.json"

EXPECTED_PACKAGE_FILES = {
    "__init__.py",
    "admission.py",
    "api.py",
    "auth.py",
    "contracts.py",
    "conversation_capture.py",
    "conversation_source.py",
    "eligibility.py",
    "exclusive_cutover.py",
    "extraction.py",
    "http_api.py",
    "http_auth.py",
    "http_runtime.py",
    "http_service.py",
    "http_store.py",
    "lifecycle.py",
    "postgres_adapter.py",
    "projection.py",
    "repository.py",
    "response_postgres.py",
    "response_provider.py",
    "response_runtime.py",
    "retrieval.py",
    "successor_live_authority.py",
    "worker.py",
}

EXPECTED_RUNTIME_PACKAGE_FILES = {
    "__init__.py",
    "__main__.py",
    "application.py",
    "calibration.py",
    "environment.py",
    "once_worker.py",
    "pilot_marker.py",
    "qdrant_adapter.py",
    "live_supabase.py",
    "https_transport.py",
    "openai_adapters.py",
}

EXPECTED_PROVIDER_ASSET_FILES = {
    "__init__.py",
    "extraction_instructions.txt",
    "extraction_output.schema.json",
}

EXPECTED_TEST_FILES = {
    "test_admission.py",
    "test_chat_memory_e2e.py",
    "test_conversation_bridge.py",
    "test_conversation_capture.py",
    "test_eligibility.py",
    "test_exclusive_cutover.py",
    "test_extraction.py",
    "test_https_transport.py",
    "test_http_api.py",
    "test_http_auth.py",
    "test_http_live_auth_mapping.py",
    "test_http_runtime.py",
    "test_http_service.py",
    "test_http_store.py",
    "test_intake_boundary.py",
    "test_lifecycle.py",
    "test_openai_adapters.py",
    "test_once_worker.py",
    "test_phase5_auth_claim_artifacts.py",
    "test_phase5_build_provenance.py",
    "test_pilot_marker.py",
    "test_projection.py",
    "test_prompt_and_binding.py",
    "test_qdrant_adapter.py",
    "test_release_contracts.py",
    "test_response_provider.py",
    "test_response_runtime.py",
    "test_retrieval.py",
    "test_retrieval_calibration.py",
    "test_runtime_inventory.py",
    "test_runtime_release.py",
    "test_successor_live_authority.py",
    "test_schema_and_rls.py",
    "test_worker_recovery.py",
}

EXPECTED_INTEGRATION_FILES = {
    "__init__.py",
    "local_jwks_server.py",
    "test_governed_memory_http_vertical_slice.py",
}

EXPECTED_VALIDATION_TOOL_FILES = {
    "postgres_bootstrap.pgsql",
    "run_disposable_successor.sh",
    "runtime_packages.json",
    "verify_migration_manifest.py",
}

EXPECTED_RELEASE_TOOL_FILES = {
    "__init__.py",
    "build_candidate_runtime.py",
    "release_guard.py",
}

EXPECTED_CLEAN_SUCCESSOR_DOC_FILES = {
    "ACTIVATION.md",
    "README.md",
    "VALIDATION.md",
}

EXPECTED_RUNTIME_PACKAGES = {
    "schema_version": "governed-memory-validation-runtime-v2",
    "python_implementation": "CPython",
    "python_version": "3.12.3",
    "runtime_lock_path": "ops/governed_memory/runtime-requirements.lock",
    "runtime_lock_sha256": "94ca231656579ce3b8f09c308e34dc8a03b8d1cf445f7a3681193767cd7db365",
    "candidate_project": {
        "name": "governed-memory-successor",
        "version": "0.0.0",
    },
    "packages": {
        "annotated-doc": "0.0.5",
        "annotated-types": "0.7.0",
        "anyio": "4.11.0",
        "asyncpg": "0.30.0",
        "cffi": "2.1.0",
        "click": "8.3.0",
        "cryptography": "49.0.0",
        "fastapi": "0.120.4",
        "h11": "0.16.0",
        "idna": "3.11",
        "pycparser": "3.0",
        "pydantic": "2.12.3",
        "pydantic-core": "2.41.4",
        "PyJWT": "2.13.0",
        "sniffio": "1.3.1",
        "starlette": "0.49.2",
        "typing-extensions": "4.15.0",
        "typing-inspection": "0.4.2",
        "uvicorn": "0.38.0",
    },
}

EXPECTED_RUNTIME_LOCK_SHA256 = (
    "94ca231656579ce3b8f09c308e34dc8a03b8d1cf445f7a3681193767cd7db365"
)
EXPECTED_BUILD_LOCK_SHA256 = (
    "138427d8971322f844edef21946cccb55944cfe8b8f322770a051b6642d401dc"
)
EXPECTED_PACKAGE_SOURCE_TREE_SHA256 = _package_source_tree_sha256(PACKAGE)
EXPECTED_SOURCE_TREE_SHA256 = _source_tree_sha256()
EXPECTED_CANDIDATE_PYTHON = (
    "/tmp/governed-memory-phase5-runtime-"
    f"{EXPECTED_RUNTIME_LOCK_SHA256}-{EXPECTED_SOURCE_TREE_SHA256}/bin/python"
)
RECORDED_SOURCE_TREE_SHA256 = (
    "af2fc1255476724200397651c6c0fab9c70d7b7720035788410f1846b937f60b"
)
RECORDED_CANDIDATE_PYTHON = (
    "/tmp/governed-memory-phase5-runtime-"
    f"{EXPECTED_RUNTIME_LOCK_SHA256}-{RECORDED_SOURCE_TREE_SHA256}/bin/python"
)
EXPECTED_PROJECT_WHEEL_SHA256 = (
    "58146af4097400097b1312011c591d1878904f7ac5709b0fdecd57da3fc0f8e4"
)

EXPECTED_ROUTES = [
    "GET /memory/status",
    "GET /memory/claims",
    "GET /memory/claims/{claim_id}",
    "GET /memory/proposals",
    "POST /memory/proposals/{proposal_id}/review",
    "POST /memory/claims/{claim_id}/correct",
    "POST /memory/claims/{claim_id}/retract",
    "DELETE /memory/claims/{claim_id}",
    "GET /memory/operations/{operation_id}",
]


class RuntimeManifestTests(unittest.TestCase):
    def load_manifest(self) -> dict[str, object]:
        raw = MANIFEST.read_bytes()
        self.assertLessEqual(len(raw), 64 * 1024)
        return json.loads(raw.decode("utf-8"))

    def test_candidate_is_explicitly_uninstalled_and_content_free(self) -> None:
        manifest = self.load_manifest()
        self.assertEqual(
            manifest["schema_version"], "governed-memory-runtime-manifest-v5"
        )
        self.assertEqual(
            manifest["phase"],
            "phase5_disposable_validated_activation_blocked",
        )
        self.assertFalse(manifest["production_state_changed"])
        self.assertEqual(
            manifest["authority"],
            {
                "database": "governed_memory",
                "database_target": "127.0.0.1:55432",
                "conversation_database": "memory",
                "conversation_application_login": "brains_app",
                "conversation_bridge": "memory_ingest_private.memory_ingest_outbox",
                "qdrant_target": "127.0.0.1:6343",
                "qdrant_collection": "governed_memory_9a54cf123493_000001",
                "qdrant_alias": "governed_memory_active",
            },
        )
        self.assertEqual(
            manifest["ingestion"],
            {
                "capture_mode_default": "off",
                "capture_owner_allowlist_in_repository": False,
                "writer_membership_granted_in_production": False,
                "historical_import": False,
                "historical_backfill": False,
                "attachment_content_release_1": False,
                "requires_post_cutover_user_message": True,
                "worker_base_conversation_table_select": False,
            },
        )
        self.assertEqual(
            set(manifest),
            {
                "schema_version", "phase", "python_runtime", "validation_runtime",
                "authority", "infrastructure", "http_runtime", "activation",
                "release_guard", "ingestion", "worker_adapters", "provider_policy",
                "disposable_validation", "owner_routes", "prohibited_routes",
                "calibration", "frontend_candidate", "legacy_imports_allowed",
                "production_state_changed",
            },
        )

    def test_no_service_or_timer_is_claimed_installed(self) -> None:
        activation = self.load_manifest()["activation"]
        self.assertEqual(activation["installed_services"], [])
        self.assertEqual(activation["enabled_services"], [])
        self.assertEqual(activation["running_services"], [])
        self.assertEqual(activation["installed_timers"], [])
        self.assertEqual(activation["enabled_timers"], [])
        self.assertEqual(
            activation["future_target_services"],
            [
                "governed-memory-http.service",
                "governed-memory-worker.service",
            ],
        )
        self.assertEqual(
            activation["shipped_inactive_templates"],
            [
                "ops/governed_memory/systemd/governed-memory-http.service.in",
                "ops/governed_memory/systemd/governed-memory-worker.service.in",
            ],
        )
        worker_unit = (
            ROOT / "ops" / "governed_memory" / "systemd"
            / "governed-memory-worker.service.in"
        ).read_text(encoding="utf-8")
        self.assertIn("GOVERNED_MEMORY_WORKER_MODE=off", worker_unit)
        self.assertIn("Type=oneshot", worker_unit)
        self.assertNotIn("[Install]", worker_unit)
        self.assertNotIn("WantedBy=", worker_unit)

    def test_provider_policy_records_zero_disposable_external_calls(self) -> None:
        policy = self.load_manifest()["provider_policy"]
        self.assertFalse(policy["import_time_calls"])
        self.assertFalse(policy["production_calls_authorized"])
        self.assertEqual(policy["disposable_external_calls"], 0)
        self.assertEqual(policy["generation_calls_per_exact_attempt"], 1)
        adapters = self.load_manifest()["worker_adapters"]
        self.assertEqual(adapters["provider"], "strict_fake_tested_zero_real_calls")
        self.assertEqual(
            adapters["embedding"],
            "strict_3072_fake_tested_zero_real_calls",
        )
        self.assertEqual(
            adapters["qdrant"],
            "exact_fake_and_real_disposable_v1_19_0_validated_not_persistent_approved",
        )
        self.assertEqual(adapters["algorithm"], "implemented_fake_tested")
        self.assertEqual(adapters["cli_composition"], "activation_blocker_unwired")
        self.assertEqual(
            adapters["cross_process_singleton"],
            "activation_blocker_not_implemented",
        )

    def test_successor_validation_is_disposable_and_not_activation_proof(self) -> None:
        manifest = self.load_manifest()
        validation = manifest["disposable_validation"]
        self.assertEqual(validation["scope"], "successor_disposable_only")
        self.assertFalse(validation["production_data_read"])
        self.assertEqual(validation["provider_external_calls"], 0)
        self.assertEqual(
            validation["evidence_status"],
            "current_phase5_disposable_proof_passed_not_production_activation",
        )
        self.assertTrue(validation["current_phase5_full_proof_complete"])
        self.assertFalse(validation["prior_receipt_reusable_for_current_source"])
        self.assertTrue(validation["final_resources_absent"])
        self.assertTrue(validation["resource_cleanup_complete"])
        self.assertTrue(validation["all_owner_routes_invoked"])
        self.assertTrue(validation["alternating_owner_pool_isolation"])
        self.assertEqual(validation["owner_pool_max_size"], 1)
        self.assertTrue(
            validation["qdrant_v1_19_0_real_disposable_compatibility_verified"]
        )
        self.assertTrue(validation["pilot_marker_disposable_proof_complete"])
        self.assertFalse(validation["worker_runtime_composition_validated"])
        self.assertFalse(validation["worker_cross_process_singleton_validated"])
        self.assertFalse(validation["semantic_threshold_calibrated"])
        self.assertFalse(validation["persistent_resources_created"])
        self.assertEqual(
            validation["proof_receipt"],
            {
                "schema_version": "governed-memory-successor-disposable-run-v4",
                "result": "passed",
                "attested_candidate_head": (
                    "699c80761065d19832d2c0f3b2b50342a5a8350c"
                ),
                "attested_candidate_tree": (
                    "c5c13579ffa54da4f30fc198254c98b662c7029b"
                ),
                "attested_pre_promotion_manifest_sha256": (
                    "2174711255ba55eeb2233703a0e3813a7d9e191b275cadc5959f7aaee3ab9b45"
                ),
                "invocation_id": "ac6e240b-1b9e-4442-a592-2b4d1c2e8492",
                "postgres_server_version": "16.14",
                "qdrant_server_version": "1.19.0",
                "connect_trace_sha256": (
                    "b054ede64b1f16ce694d110e5d69e4635db41d0f6f2348102d880df8f54338c3"
                ),
                "foundation_logical_dump_sha256": (
                    "852c37925e3a4fc424d4f06456d053bafff3ba8461261303da66b312111c6923"
                ),
                "bridge_logical_dump_sha256": (
                    "c4f802917f69244d6d27cf9bd9e55947e312f22aa9e34fcffe52d4486f09539d"
                ),
                "integration_receipt_sha256": (
                    "6479f3f0feb8f155754edd3467b8c80f043ae47a19f11ddc459f5c3896fe4c37"
                ),
                "provider_external_calls": 0,
                "semantic_threshold_calibrated": False,
                "resources_removed": True,
            },
        )
        self.assertEqual(
            validation["preproof_corrections"],
            [
                "pilot_marker_on_conflict_out_variable_ambiguity_fixed_with_named_constraint",
                "validation_schema_inventory_staleness_fixed_before_passing_run",
                "qdrant_invalid_alias_endpoint_fixed_and_disposable_v1_19_0_verified_only",
            ],
        )
        self.assertFalse(manifest["activation"]["production_authorized"])
        self.assertTrue(
            manifest["infrastructure"][
                "qdrant_real_disposable_compatibility_verified"
            ]
        )
        self.assertIsNone(
            manifest["infrastructure"]["qdrant_persistent_pilot_image"]
        )
        self.assertFalse(
            manifest["infrastructure"]["qdrant_persistent_resource_created"]
        )
        http_runtime = manifest["http_runtime"]
        self.assertTrue(http_runtime["session_id_required"])
        self.assertEqual(
            http_runtime["supabase_auth_sessions_rpc_status"],
            "staged_candidate_not_installed",
        )
        self.assertFalse(http_runtime["supabase_auth_sessions_rpc_live_verified"])
        self.assertTrue(http_runtime["owner_claim_fact_detail_implemented"])
        self.assertTrue(
            http_runtime["owner_claim_fact_detail_disposable_proof_complete"]
        )
        self.assertIn(
            "semantic_calibration_artifact_unapproved_retrieval_off",
            manifest["activation"]["blockers"],
        )
        self.assertIn(
            "supabase_auth_sessions_rpc_not_installed_or_live_verified",
            manifest["activation"]["blockers"],
        )
        self.assertNotIn(
            "qdrant_v1_19_0_real_disposable_compatibility_pending",
            manifest["activation"]["blockers"],
        )
        self.assertNotIn(
            "durable_pilot_marker_candidate_not_applied_or_disposable_proved",
            manifest["activation"]["blockers"],
        )
        self.assertNotIn(
            "owner_claim_fact_detail_api_not_implemented",
            manifest["activation"]["blockers"],
        )
        self.assertIn(
            "worker_cross_process_singleton_not_implemented",
            manifest["activation"]["blockers"],
        )
        self.assertEqual(
            manifest["calibration"],
            {
                "approval_binding": (
                    "independent_expected_artifact_and_approval_receipt_sha256"
                ),
                "artifact_status": "unapproved",
                "retrieval_enabled": False,
            },
        )
        self.assertEqual(
            manifest["frontend_candidate"],
            {
                "git_commit_short": "35a684",
                "built": True,
                "deployed": False,
                "authenticated_visual_qa_complete": False,
            },
        )
        self.assertIn(
            "legacy_memory_owner_scoped_read_write_shadow_quiescence_not_proved",
            manifest["activation"]["blockers"],
        )
        self.assertEqual(
            manifest["release_guard"],
            {
                "create_allowed": False,
                "create_refusal_code": "activation_blockers_open",
                "cleanup_allowed": False,
                "cleanup_refusal_code": "authorization_missing",
                "commands_executed": 0,
            },
        )

    def test_python_runtime_is_exact_candidate_contract(self) -> None:
        manifest = self.load_manifest()
        runtime = manifest["python_runtime"]
        self.assertEqual(
            runtime,
            {
                "implementation": "CPython",
                "required": "3.12.*",
                "validated_candidate": "3.12.3",
                "platform": "linux_x86_64",
            },
        )
        readme = SUCCESSOR_README.read_text(encoding="utf-8")
        normalized_readme = " ".join(readme.split())
        self.assertIn("requires CPython 3.12.x", normalized_readme)
        self.assertIn("CPython 3.12.3", normalized_readme)

    def test_validation_runtime_is_candidate_owned_and_hash_locked(self) -> None:
        runtime_packages = json.loads(RUNTIME_PACKAGES.read_text(encoding="utf-8"))
        self.assertEqual(runtime_packages, EXPECTED_RUNTIME_PACKAGES)
        manifest = self.load_manifest()
        self.assertEqual(
            manifest["validation_runtime"],
            {
                "manifest": "tools/governed_memory_validation/runtime_packages.json",
                "runtime_lock": "ops/governed_memory/runtime-requirements.lock",
                "runtime_lock_sha256": EXPECTED_RUNTIME_LOCK_SHA256,
                "source_tree_sha256": RECORDED_SOURCE_TREE_SHA256,
                "build_lock": "ops/governed_memory/build-requirements.lock",
                "build_lock_sha256": EXPECTED_BUILD_LOCK_SHA256,
                "candidate_python": RECORDED_CANDIDATE_PYTHON,
                "receipt_scope": "prior_phase5_source_bound_disposable_proof_historical_stale",
                "current_phase5_source_bound": False,
                "final_phase6b_runtime_rebuild_pending": True,
                "candidate_owned_environment": True,
                "install_lock": True,
                "build_lock_verified": True,
                "isolated_wheel_build_verified": True,
                "isolated_project_install_verified": True,
                "runtime_package_count": 19,
                "candidate_python_is_symlink": False,
                "pip_present": False,
                "setuptools_present": False,
                "wheel_present": False,
                "user_site_enabled": False,
                "legacy_environment_imported": False,
                "final_build_receipt": "ops/governed_memory/runtime_build_receipt.json",
            },
        )
        self.assertNotIn(
            "candidate_owned_runtime_environment_not_built",
            manifest["activation"]["blockers"],
        )
        self.assertNotIn(
            "final_phase5_runtime_rebuild_and_receipt_pending",
            manifest["activation"]["blockers"],
        )
        self.assertEqual(hashlib.sha256(RUNTIME_LOCK.read_bytes()).hexdigest(), EXPECTED_RUNTIME_LOCK_SHA256)
        self.assertEqual(hashlib.sha256(BUILD_LOCK.read_bytes()).hexdigest(), EXPECTED_BUILD_LOCK_SHA256)
        self.assertTrue(RUNTIME_BUILD_RECEIPT.is_file())
        self.assertFalse(RUNTIME_BUILD_RECEIPT.is_symlink())
        build_receipt = json.loads(RUNTIME_BUILD_RECEIPT.read_text(encoding="ascii"))
        self.assertEqual(
            build_receipt["candidate_python"],
            RECORDED_CANDIDATE_PYTHON,
        )
        self.assertEqual(
            build_receipt["source_tree_sha256"],
            RECORDED_SOURCE_TREE_SHA256,
        )
        self.assertEqual(
            build_receipt["project_wheel_sha256"],
            EXPECTED_PROJECT_WHEEL_SHA256,
        )
        self.assertNotEqual(
            RECORDED_SOURCE_TREE_SHA256,
            EXPECTED_SOURCE_TREE_SHA256,
        )

    def test_schema_validation_scope_is_versionless_and_exact(self) -> None:
        contract = json.loads(SCHEMA_CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(
            contract["validation_scope"],
            {
                "scope": "successor_disposable_only",
                "environment": "disposable_only",
                "production_data_read": False,
                "provider_external_calls": 0,
                "production_state_changed": False,
            },
        )
        self.assertEqual(
            contract["hard_requirements"]["production_activation_blockers"],
            self.load_manifest()["activation"]["blockers"],
        )

    def test_route_surface_is_exact_and_owner_is_not_a_path_parameter(self) -> None:
        manifest = self.load_manifest()
        self.assertEqual(manifest["owner_routes"], EXPECTED_ROUTES)
        self.assertEqual(
            manifest["prohibited_routes"],
            ["/cards", "/vantage", "/memory/evidence", "/memory/retrieve"],
        )
        self.assertTrue(manifest["legacy_imports_allowed"] is False)
        self.assertNotIn("user_id", "\n".join(manifest["owner_routes"]))


class SourceInventoryTests(unittest.TestCase):
    def test_release_one_authority_has_only_owner_and_worker_roles(self) -> None:
        self.assertEqual(
            tuple((role.name, role.value) for role in ActorRole),
            (("OWNER", "owner"), ("WORKER", "worker")),
        )
        for path in sorted(PACKAGE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("ActorRole.ADMIN", source)
                self.assertNotIn("explicit_admin_review", source)

    def test_repository_surface_is_closed_and_has_no_generic_plan_escape_hatch(self) -> None:
        expected_methods = {
            "persist_ingest_decision",
            "read_ingest_receipt",
            "fail_ingest",
            "mark_provider_dispatched",
            "complete_provider_call",
            "fail_provider_call",
            "apply_proposal_review",
            "correct_claim",
            "retract_claim",
            "request_claim_deletion",
            "finalize_claim_deletion",
            "finish_projection",
            "persist_answer_binding",
            "read_claims",
            "read_operation",
        }
        observed = {
            name
            for name, value in vars(GovernedMemoryRepository).items()
            if inspect.isfunction(value) and not name.startswith("_")
        }
        self.assertEqual(observed, expected_methods)
        source = (PACKAGE / "repository.py").read_text(encoding="utf-8")
        for forbidden in ("apply_plan", "effect_names", "free_form_sql"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

        for name in expected_methods:
            signature = inspect.signature(getattr(GovernedMemoryRepository, name))
            parameters = signature.parameters
            self.assertEqual(next(iter(parameters)), "self")
            for parameter in tuple(parameters.values())[1:]:
                with self.subTest(method=name, parameter=parameter.name):
                    self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                    self.assertNotIn(
                        parameter.name,
                        {
                            "now",
                            "occurred_at",
                            "reviewed_at",
                            "transaction_time",
                            "sql",
                            "effects",
                            "plan",
                        },
                    )

    def test_repository_reason_and_answer_binding_signatures_are_exact(self) -> None:
        review = inspect.signature(
            GovernedMemoryRepository.apply_proposal_review
        ).parameters
        self.assertIn("reason_codes", review)
        self.assertNotIn("reason_code", review)
        for method_name in (
            "correct_claim",
            "retract_claim",
            "request_claim_deletion",
        ):
            with self.subTest(method=method_name):
                parameters = inspect.signature(
                    getattr(GovernedMemoryRepository, method_name)
                ).parameters
                self.assertNotIn("reason_code", parameters)
                self.assertNotIn("reason_codes", parameters)

        answer = tuple(
            inspect.signature(
                GovernedMemoryRepository.persist_answer_binding
            ).parameters
        )
        self.assertEqual(
            answer,
            (
                "self",
                "operation_id",
                "owner_user_id",
                "response_id",
                "thread_id",
                "query_sha256",
                "policy_sha256",
                "allowed_predicates",
                "domains",
                "intents",
                "max_records",
                "policy_revision",
                "renderer_sha256",
                "prompt_sha256",
                "explicit_recall",
                "selected_claim_ids",
                "injected_claim_ids",
                "outcome",
                "expected_selection_manifest_sha256",
                "expected_injection_manifest_sha256",
                "memory_block",
                "outbound_request",
            ),
        )

    def test_retract_and_delete_routes_require_state_and_revision_compare_and_swap(self) -> None:
        by_operation = {
            specification.operation: specification
            for specification in OWNER_ROUTE_SPECIFICATIONS
        }
        for operation in ("retract_claim", "delete_claim"):
            specification = by_operation[operation]
            with self.subTest(operation=operation):
                self.assertIn(
                    "expected_state_sha256", specification.required_body_fields
                )
                self.assertIn(
                    "expected_revision_sha256", specification.required_body_fields
                )
                self.assertEqual(specification.optional_body_fields, ())
                self.assertNotIn(
                    "pending_correction_override",
                    specification.required_body_fields
                    + specification.optional_body_fields,
                )
                self.assertTrue(specification.server_time_owned)

    def test_every_module_all_symbol_exists_and_wildcard_import_succeeds(self) -> None:
        for path in sorted(PACKAGE.glob("*.py")):
            module_name = (
                "rag_engine.governed_memory"
                if path.name == "__init__.py"
                else f"rag_engine.governed_memory.{path.stem}"
            )
            module = importlib.import_module(module_name)
            exports = getattr(module, "__all__", None)
            with self.subTest(module=module_name):
                self.assertIsInstance(exports, (list, tuple))
                self.assertEqual(len(exports), len(set(exports)))
                missing = [name for name in exports if not hasattr(module, name)]
                self.assertEqual(missing, [])
                namespace: dict[str, object] = {}
                exec(f"from {module_name} import *", namespace, namespace)
                self.assertEqual(
                    {name for name in exports if name in namespace}, set(exports)
                )

    def test_contract_hash_normalizes_nfc_recursively(self) -> None:
        composed = {
            "outer": ["caf\u00e9", {"label": "r\u00e9sum\u00e9"}],
        }
        decomposed = {
            "outer": [
                unicodedata.normalize("NFD", "caf\u00e9"),
                {"label": unicodedata.normalize("NFD", "r\u00e9sum\u00e9")},
            ],
        }
        self.assertNotEqual(composed, decomposed)
        composed_hash = canonical_sha256("governed_memory.nfc_probe", composed)
        self.assertRegex(composed_hash, r"^[0-9a-f]{64}$")
        with self.assertRaises(ContractViolation):
            canonical_sha256("governed_memory.nfc_probe", decomposed)

    def test_versionless_package_file_set_is_exact(self) -> None:
        observed = {path.name for path in PACKAGE.glob("*.py")}
        self.assertEqual(observed, EXPECTED_PACKAGE_FILES)

    def test_runtime_adapter_file_set_and_exports_are_exact(self) -> None:
        observed = {path.name for path in RUNTIME_PACKAGE.glob("*.py")}
        self.assertEqual(observed, EXPECTED_RUNTIME_PACKAGE_FILES)
        for path in sorted(RUNTIME_PACKAGE.glob("*.py")):
            module_name = f"rag_engine.governed_memory.runtime.{path.stem}"
            module = importlib.import_module(module_name)
            exports = getattr(module, "__all__", None)
            with self.subTest(module=module_name):
                self.assertIsInstance(exports, (list, tuple))
                self.assertEqual(len(exports), len(set(exports)))
                self.assertEqual(
                    [name for name in exports if not hasattr(module, name)],
                    [],
                )

    def test_provider_asset_file_set_and_non_python_allowlist_are_exact(self) -> None:
        observed = {
            path.name for path in PROVIDER_ASSETS.iterdir() if path.is_file()
        }
        self.assertEqual(observed, EXPECTED_PROVIDER_ASSET_FILES)
        observed_non_python = {
            relative
            for relative, _digest in _package_source_material(PACKAGE)
            if Path(relative).suffix != ".py"
        }
        self.assertEqual(observed_non_python, PROVIDER_ASSET_SOURCE_PATHS)

    def test_test_module_file_set_is_exact(self) -> None:
        observed = {path.name for path in Path(__file__).parent.glob("test_*.py")}
        self.assertEqual(observed, EXPECTED_TEST_FILES)

    def test_integration_module_file_set_is_exact(self) -> None:
        observed = {path.name for path in INTEGRATION_TESTS.iterdir() if path.is_file()}
        self.assertEqual(observed, EXPECTED_INTEGRATION_FILES)

    def test_validation_tool_file_set_is_exact(self) -> None:
        observed = {path.name for path in VALIDATION_TOOLS.iterdir() if path.is_file()}
        self.assertEqual(observed, EXPECTED_VALIDATION_TOOL_FILES)

    def test_disposable_runner_binds_exact_migration_manifest(self) -> None:
        migration_manifest = ROOT / "governed-memory-migrations" / "manifest.json"
        manifest_sha256 = hashlib.sha256(migration_manifest.read_bytes()).hexdigest()
        runner = (VALIDATION_TOOLS / "run_disposable_successor.sh").read_text(
            encoding="utf-8"
        )
        binding = f"readonly EXPECTED_MANIFEST_SHA256='{manifest_sha256}'"
        self.assertEqual(runner.count("readonly EXPECTED_MANIFEST_SHA256="), 1)
        self.assertIn(binding, runner)

    def test_disposable_runner_uses_phase5_and_exact_provider_asset_allowlist(self) -> None:
        runner = (VALIDATION_TOOLS / "run_disposable_successor.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("governed-memory-phase4-", runner)
        self.assertIn("governed-memory-phase5-runtime-", runner)
        self.assertIn("governed-memory-phase5-build-", runner)
        for relative in sorted(PROVIDER_ASSET_SOURCE_PATHS):
            with self.subTest(relative=relative):
                self.assertEqual(runner.count(f'"{relative}"'), 2)

    def test_release_tool_file_set_is_exact(self) -> None:
        observed = {path.name for path in RELEASE_TOOLS.iterdir() if path.is_file()}
        self.assertEqual(observed, EXPECTED_RELEASE_TOOL_FILES)

    def test_clean_successor_document_file_set_is_exact(self) -> None:
        observed = {
            path.name for path in CLEAN_SUCCESSOR_DOCS.iterdir() if path.is_file()
        }
        self.assertEqual(observed, EXPECTED_CLEAN_SUCCESSOR_DOC_FILES)

    def test_phase5_release_docs_and_contracts_have_no_stale_phase4_claims(
        self,
    ) -> None:
        paths = [
            *sorted(CLEAN_SUCCESSOR_DOCS.iterdir()),
            MANIFEST,
            ROOT / "ops" / "governed_memory" / "bootstrap_contract.json",
            ROOT / "ops" / "governed_memory" / "pilot_contract.json",
            ROOT / "ops" / "governed_memory" / "compose.candidate.yaml",
            RELEASE_TOOLS / "release_guard.py",
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("Phase 4", text)
                self.assertNotIn("phase4", text)
                self.assertNotIn("v1.11.0", text)
                self.assertNotIn("owner_claim_fact_detail_api_not_implemented", text)
                self.assertNotIn(
                    "durable_pilot_ever_started_marker_not_implemented",
                    text,
                )

    def test_successor_has_no_legacy_names_or_imports(self) -> None:
        prohibited_text = (
            "memory_" + "v1",
            "memory_" + "raw",
            "vantage",
            "persona_loader",
            "filesystem review",
            "assistant_response_preferences",
        )
        prohibited_import_roots = {"openai", "asyncpg", "qdrant_client"}
        allowed_outer_import_roots = {
            "http_service.py": {"asyncpg"},
        }
        for path in sorted(PACKAGE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            lowered = source.lower()
            for token in prohibited_text:
                with self.subTest(path=path.name, token=token):
                    self.assertNotIn(token, lowered)
            tree = ast.parse(source, filename=str(path))
            roots: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots.update(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots.add(node.module.split(".", 1)[0])
            self.assertEqual(
                roots & prohibited_import_roots,
                allowed_outer_import_roots.get(path.name, set()),
                f"{path.name} has an unclassified outer-adapter import",
            )
        runtime_source = (RUNTIME_PACKAGE / "live_supabase.py").read_text(encoding="utf-8")
        self.assertIn('"User-Agent": "governed-memory-live-user"', runtime_source)
        self.assertNotIn("governed-memory-live-user-v", runtime_source)

    def test_successor_tests_do_not_import_old_helpers(self) -> None:
        for path in sorted(Path(__file__).parent.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
            self.assertFalse(
                any(("memory_" + "v1") in name for name in imported),
                f"{path.name} imports a retired Memory test or module",
            )

    def test_object_kind_has_no_compatibility_aliases(self) -> None:
        compatibility_name = "Value" + "Kind"
        paths = [
            *sorted(PACKAGE.glob("*.py")),
            *sorted(Path(__file__).parent.glob("test_*.py")),
        ]
        definitions: list[tuple[str, int]] = []
        aliases: list[tuple[str, int, str]] = []
        for path in paths:
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn(
                    compatibility_name,
                    source,
                    "the clean successor must not retain retired enum names",
                )
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "ObjectKind":
                    definitions.append((path.name, node.lineno))
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = node.value
                    if not isinstance(value, ast.Name) or value.id != "ObjectKind":
                        continue
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id != "ObjectKind":
                            aliases.append((path.name, node.lineno, target.id))
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for imported in node.names:
                        if imported.name.rsplit(".", 1)[-1] != "ObjectKind":
                            continue
                        if imported.asname not in (None, "ObjectKind"):
                            aliases.append((path.name, node.lineno, imported.asname))

        self.assertEqual(len(definitions), 1)
        self.assertEqual(definitions[0][0], "contracts.py")
        self.assertEqual(aliases, [])

    def test_readme_records_disposable_integration_without_activation(self) -> None:
        readme = SUCCESSOR_README.read_text(encoding="utf-8")
        normalized = " ".join(readme.split())
        for required in (
            "disposable PostgreSQL and Qdrant",
            "zero external provider calls",
            "not production activated",
            "semantic retrieval-score threshold",
        ):
            with self.subTest(required=required):
                self.assertIn(required, normalized)

    def test_pyproject_packages_only_the_successor_and_has_exact_entrypoint(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["build-system"], {
            "requires": ["setuptools==84.0.0"],
            "build-backend": "setuptools.build_meta",
        })
        self.assertEqual(
            project["project"]["scripts"],
            {
                "governed-memory-http": (
                    "rag_engine.governed_memory.runtime.application:main"
                )
            },
        )
        self.assertEqual(
            project["tool"]["setuptools"]["packages"]["find"],
            {
                "include": [
                    "rag_engine.governed_memory",
                    "rag_engine.governed_memory.*",
                ],
                "namespaces": False,
            },
        )
        self.assertEqual(
            project["tool"]["setuptools"]["package-data"],
            {
                "rag_engine.governed_memory.provider_assets": [
                    "*.json",
                    "*.txt",
                ]
            },
        )

    def test_candidate_python_source_exactly_matches_current_successor(self) -> None:
        runtime = json.loads(MANIFEST.read_text(encoding="utf-8"))[
            "validation_runtime"
        ]
        if runtime["final_phase6b_runtime_rebuild_pending"]:
            self.assertFalse(runtime["current_phase5_source_bound"])
            self.assertEqual(
                runtime["source_tree_sha256"],
                RECORDED_SOURCE_TREE_SHA256,
            )
            self.assertNotEqual(
                runtime["source_tree_sha256"],
                EXPECTED_SOURCE_TREE_SHA256,
            )
            return
        probe = """
import hashlib
import json
from pathlib import Path
import rag_engine.governed_memory as package

ALLOWED_NON_PYTHON_SOURCE_PATHS = {
    'provider_assets/extraction_instructions.txt',
    'provider_assets/extraction_output.schema.json',
}

def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)

root = Path(package.__file__).parent
material = []
observed_assets = set()
for path in sorted(root.rglob('*')):
    relative = path.relative_to(root)
    relative_text = relative.as_posix()
    if '__pycache__' in relative.parts:
        continue
    if path.is_symlink():
        raise SystemExit('installed package symlink')
    if path.is_dir():
        continue
    if not path.is_file():
        raise SystemExit('installed package inventory invalid')
    if path.suffix != '.py':
        if relative_text not in ALLOWED_NON_PYTHON_SOURCE_PATHS:
            raise SystemExit('installed package inventory invalid')
        observed_assets.add(relative_text)
    material.append((relative_text, sha256(path)))
if observed_assets != ALLOWED_NON_PYTHON_SOURCE_PATHS:
    raise SystemExit('installed package assets missing')
encoded = json.dumps(material, ensure_ascii=True, separators=(',', ':')).encode('ascii')
print(json.dumps({
    'file': str(package.__file__),
    'material': material,
    'source_tree_sha256': hashlib.sha256(encoded).hexdigest(),
}, sort_keys=True, separators=(',', ':')))
"""
        output = subprocess.run(
            [EXPECTED_CANDIDATE_PYTHON, "-I", "-B", "-c", probe],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        receipt = json.loads(output)
        self.assertIn(
            "site-packages/rag_engine/governed_memory/__init__.py",
            receipt["file"],
        )
        self.assertNotIn("/opt/chat-memory", receipt["file"])
        self.assertEqual(
            receipt["material"],
            [list(item) for item in _package_source_material(PACKAGE)],
        )
        self.assertEqual(
            receipt["source_tree_sha256"],
            EXPECTED_PACKAGE_SOURCE_TREE_SHA256,
        )


if __name__ == "__main__":
    unittest.main()

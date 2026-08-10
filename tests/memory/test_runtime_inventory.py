"""Offline source-inventory checks; this is not installed-runtime proof."""

from __future__ import annotations

import ast
import importlib
import inspect
import json
from pathlib import Path
import unicodedata
import unittest

from rag_engine.governed_memory.contracts import ContractViolation, canonical_sha256
from rag_engine.governed_memory.auth import ActorRole
from rag_engine.governed_memory.api import OWNER_ROUTE_SPECIFICATIONS
from rag_engine.governed_memory.repository import GovernedMemoryRepository


FIXTURE_PROVENANCE = "synthetic-governed-memory-successor"
ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "rag_engine" / "governed_memory"
MANIFEST = ROOT / "ops" / "governed_memory" / "runtime_manifest.json"
SUCCESSOR_README = ROOT / "docs" / "memory" / "clean_successor" / "README.md"
SCHEMA_CONTRACT = ROOT / "governed-memory-migrations" / "schema_contract.json"
INTEGRATION_TESTS = ROOT / "tests" / "memory_integration"
VALIDATION_TOOLS = ROOT / "tools" / "governed_memory_validation"
CLEAN_SUCCESSOR_DOCS = ROOT / "docs" / "memory" / "clean_successor"
RUNTIME_PACKAGES = VALIDATION_TOOLS / "runtime_packages.json"

EXPECTED_PACKAGE_FILES = {
    "__init__.py",
    "admission.py",
    "api.py",
    "auth.py",
    "contracts.py",
    "eligibility.py",
    "extraction.py",
    "http_api.py",
    "http_auth.py",
    "http_runtime.py",
    "http_store.py",
    "lifecycle.py",
    "postgres_adapter.py",
    "projection.py",
    "repository.py",
    "retrieval.py",
    "worker.py",
}

EXPECTED_TEST_FILES = {
    "test_admission.py",
    "test_chat_memory_e2e.py",
    "test_eligibility.py",
    "test_extraction.py",
    "test_http_api.py",
    "test_http_auth.py",
    "test_http_runtime.py",
    "test_http_store.py",
    "test_intake_boundary.py",
    "test_lifecycle.py",
    "test_projection.py",
    "test_prompt_and_binding.py",
    "test_retrieval.py",
    "test_runtime_inventory.py",
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

EXPECTED_CLEAN_SUCCESSOR_DOC_FILES = {
    "README.md",
    "VALIDATION.md",
}

EXPECTED_RUNTIME_PACKAGES = {
    "schema_version": "governed-memory-validation-runtime-v1",
    "python_version": "3.12.3",
    "packages": {
        "anyio": "4.11.0",
        "asyncpg": "0.30.0",
        "cryptography": "49.0.0",
        "fastapi": "0.120.4",
        "h11": "0.16.0",
        "PyJWT": "2.13.0",
        "pydantic": "2.12.3",
        "pydantic_core": "2.41.4",
        "starlette": "0.49.2",
        "typing_extensions": "4.15.0",
        "uvicorn": "0.38.0",
    },
}

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
            manifest["schema_version"], "governed-memory-runtime-manifest-v3"
        )
        self.assertEqual(manifest["phase"], "candidate_uninstalled")
        self.assertFalse(manifest["production_state_changed"])
        self.assertEqual(
            manifest["authority"],
            {
                "database": "governed_memory",
                "conversation_bridge": "public.memory_ingest_outbox",
                "qdrant_alias": "governed_memory_active",
            },
        )
        self.assertEqual(
            manifest["ingestion"],
            {
                "historical_import": False,
                "historical_backfill": False,
                "attachment_content_release_1": False,
                "requires_post_cutover_message": True,
            },
        )

    def test_no_service_or_timer_is_claimed_installed(self) -> None:
        activation = self.load_manifest()["activation"]
        self.assertEqual(activation["installed_services"], [])
        self.assertEqual(activation["enabled_services"], [])
        self.assertEqual(activation["installed_timers"], [])
        self.assertEqual(activation["enabled_timers"], [])
        self.assertEqual(
            activation["future_target_service"],
            "governed-memory-worker.service",
        )
        self.assertFalse(
            (ROOT / "ops" / "systemd" / "governed-memory-worker.service").exists(),
            "The candidate must not ship a non-runnable systemd unit",
        )

    def test_provider_policy_records_zero_disposable_external_calls(self) -> None:
        policy = self.load_manifest()["provider_policy"]
        self.assertFalse(policy["import_time_calls"])
        self.assertFalse(policy["production_calls_authorized"])
        self.assertEqual(policy["disposable_external_calls"], 0)
        self.assertEqual(policy["generation_calls_per_exact_attempt"], 1)

    def test_successor_validation_is_disposable_and_not_activation_proof(self) -> None:
        manifest = self.load_manifest()
        validation = manifest["disposable_validation"]
        self.assertEqual(validation["scope"], "successor_disposable_only")
        self.assertFalse(validation["production_data_read"])
        self.assertEqual(validation["provider_external_calls"], 0)
        self.assertTrue(validation["migration_forward_rollback_reapply"])
        self.assertTrue(validation["final_resources_absent"])
        self.assertTrue(validation["all_owner_routes_invoked"])
        self.assertTrue(validation["alternating_owner_pool_isolation"])
        self.assertEqual(validation["owner_pool_max_size"], 1)
        self.assertFalse(manifest["activation"]["production_authorized"])
        self.assertIn(
            "semantic_score_threshold_not_calibrated",
            manifest["activation"]["blockers"],
        )
        self.assertIn(
            "durable_auth_session_provenance_policy_not_decided",
            manifest["activation"]["blockers"],
        )

    def test_python_runtime_floor_is_explicit_and_at_least_3_11(self) -> None:
        manifest = self.load_manifest()
        runtime = manifest["python_runtime"]
        self.assertEqual(set(runtime), {"minimum", "validated_candidate"})
        minimum = tuple(int(part) for part in runtime["minimum"].split("."))
        validated = tuple(
            int(part) for part in runtime["validated_candidate"].split(".")
        )
        self.assertGreaterEqual(minimum, (3, 11))
        self.assertGreaterEqual(validated, minimum)
        readme = SUCCESSOR_README.read_text(encoding="utf-8")
        normalized_readme = " ".join(readme.split())
        self.assertIn("requires Python 3.11 or newer", normalized_readme)
        self.assertIn("Python 3.12.3", normalized_readme)

    def test_validation_runtime_is_exact_evidence_not_an_install_lock(self) -> None:
        runtime_packages = json.loads(RUNTIME_PACKAGES.read_text(encoding="utf-8"))
        self.assertEqual(runtime_packages, EXPECTED_RUNTIME_PACKAGES)
        manifest = self.load_manifest()
        self.assertEqual(
            manifest["validation_runtime"],
            {
                "manifest": "tools/governed_memory_validation/runtime_packages.json",
                "candidate_owned_environment": False,
                "install_lock": False,
            },
        )
        self.assertIn(
            "candidate_owned_runtime_environment_not_built",
            manifest["activation"]["blockers"],
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

    def test_test_module_file_set_is_exact(self) -> None:
        observed = {path.name for path in Path(__file__).parent.glob("test_*.py")}
        self.assertEqual(observed, EXPECTED_TEST_FILES)

    def test_integration_module_file_set_is_exact(self) -> None:
        observed = {path.name for path in INTEGRATION_TESTS.iterdir() if path.is_file()}
        self.assertEqual(observed, EXPECTED_INTEGRATION_FILES)

    def test_validation_tool_file_set_is_exact(self) -> None:
        observed = {path.name for path in VALIDATION_TOOLS.iterdir() if path.is_file()}
        self.assertEqual(observed, EXPECTED_VALIDATION_TOOL_FILES)

    def test_clean_successor_document_file_set_is_exact(self) -> None:
        observed = {
            path.name for path in CLEAN_SUCCESSOR_DOCS.iterdir() if path.is_file()
        }
        self.assertEqual(observed, EXPECTED_CLEAN_SUCCESSOR_DOC_FILES)

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
            self.assertFalse(
                roots & prohibited_import_roots,
                f"{path.name} imports a production adapter in the offline core",
            )

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


if __name__ == "__main__":
    unittest.main()

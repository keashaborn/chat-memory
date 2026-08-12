from __future__ import annotations

import base64
from contextlib import nullcontext
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from tools.governed_memory_release import release_guard
from tools.governed_memory_release.build_candidate_runtime import (
    BUILD_LOCK,
    CandidateBuildError,
    DIST_INFO_PREFIX,
    PROJECT_WHEEL,
    PROVIDER_ASSET_SOURCE_PATHS,
    RUNTIME_LOCK,
    SETUPTOOLS_SHA256,
    _package_source_tree_sha256,
    _parse_hash_lock,
    _source_bound_root,
    _verify_project_wheel,
    _verify_runtime_wheelhouse,
)
from tools.governed_memory_release.release_guard import (
    EXACT_TARGETS,
    EXPECTED_ACTIVATION_BLOCKERS,
    ReleaseGuardError,
    evaluate_release_observation,
    verify_candidate_artifacts,
)


ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "ops" / "governed_memory"


def observation(operation: str, *, state: str) -> dict[str, object]:
    return {
        "schema_version": "governed-memory-release-observation-v1",
        "operation": operation,
        "candidate_git_commit": "a" * 40,
        "authorization_scope_sha256": "b" * 64,
        "hostname": "ip-172-31-32-171",
        "api_port_available": True,
        "postgres_port_available": True,
        "qdrant_port_available": True,
        "frontend_firewall_proof_sha256": "c" * 64,
        "targets": {
            key: {"name": value, "state": state}
            for key, value in EXACT_TARGETS.items()
        },
        "pilot_ever_started": False,
        "postgresql_user_row_count": 0,
        "qdrant_point_count": 0,
        "active_client_count": 0,
    }


class Phase8AReleaseArtifactTests(unittest.TestCase):
    def test_phase8a_promoted_proof_artifacts_verify_offline(self) -> None:
        result = verify_candidate_artifacts()
        self.assertEqual(
            result["schema_version"],
            "governed-memory-release-artifact-verification-v2",
        )
        self.assertEqual(
            result["phase"],
            "phase8a_inactive_installation_controller_proof_promoted_"
            "activation_blocked",
        )
        self.assertEqual(result["installation_package_artifact_count"], 59)
        self.assertEqual(result["external_calls"], 0)
        self.assertEqual(result["commands_executed"], 0)
        self.assertFalse(result["production_state_changed"])
        self.assertFalse(result["installation_authorized"])
        self.assertFalse(result["activation_authorized"])
        self.assertTrue(result["disposable_revalidation_required"])
        self.assertTrue(
            result["installation_controller_disposable_proof_complete"]
        )
        self.assertEqual(
            result["installation_controller_proof_receipt"],
            "ops/governed_memory/phase8a_disposable_proof_receipt.json",
        )
        self.assertEqual(
            result["installation_controller_proof_receipt_sha256"],
            release_guard.EXPECTED_PHASE8A_PROOF_RECEIPT_SHA256,
        )
        self.assertEqual(
            result["package_embedded_controller_state"],
            "packaged_proof_pending_not_installed_not_authorized",
        )
        self.assertFalse(result["linux_execution_backend_available"])
        self.assertIn(
            "ops/governed_memory/installation/package_manifest.json",
            result["artifact_sha256"],
        )
        self.assertIn(
            "ops/governed_memory/history/phase6e/disposable_proof_receipt.json",
            result["artifact_sha256"],
        )
        self.assertIn(
            "ops/governed_memory/phase7c_disposable_proof_receipt.json",
            result["artifact_sha256"],
        )
        for path in (
            "ops/governed_memory/phase8a_disposable_proof_receipt.json",
            (
                "ops/governed_memory/history/phase8a/"
                "controller_disposable_proof_receipt.json"
            ),
            (
                "ops/governed_memory/history/phase8a/"
                "postgresql16_disposable_proof_receipt.json"
            ),
            (
                "ops/governed_memory/history/phase8a/"
                "postgresql16_disposable_proof_harness.sh"
            ),
        ):
            self.assertIn(path, result["artifact_sha256"])
        self.assertNotIn(
            "ops/governed_memory/phase6e_disposable_proof_receipt.json",
            result["artifact_sha256"],
        )
        self.assertEqual(
            result["artifact_sha256"][
                "tools/governed_memory_validation/run_disposable_successor.sh"
            ],
            "2acd2fb134d834b04f9b41448a2cfead"
            "7712ce846dab38b001c1a8647eb9796b",
        )
        self.assertEqual(
            result["artifact_sha256"][
                "tools/governed_memory_validation/postgres_bootstrap.pgsql"
            ],
            "0c28d2e444cddea0b61e8ea7ac9f6084"
            "b06e2038beb06bb65e754c4712eeb857",
        )

    def test_owner_preflight_restricted_cluster_read_is_rejected(self) -> None:
        original = (
            ROOT / "governed-memory-migrations" / "roles_preflight.pgsql"
        ).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            migration_root = Path(directory)
            (migration_root / "roles_preflight.pgsql").write_text(
                original
                + "\nSELECT pg_catalog.current_setting('shared_preload_libraries');\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                release_guard, "MIGRATION_ROOT", migration_root
            ), self.assertRaisesRegex(
                ReleaseGuardError, "release_logging_preflight_boundary_invalid"
            ):
                release_guard._verify_installation_text_contracts()

    def test_runtime_manifest_semantic_drift_is_rejected(self) -> None:
        original = json.loads(
            (OPS / "runtime_manifest.json").read_text(encoding="utf-8")
        )
        variants: list[dict[str, object]] = []
        for mutate in (
            lambda value: value["activation"].update(
                {"production_authorized": True}
            ),
            lambda value: value["activation"]["running_services"].append(
                "governed-memory-http.service"
            ),
            lambda value: value["disposable_validation"].update(
                {"current_full_proof_complete": False}
            ),
            lambda value: value["disposable_validation"].update(
                {"disposable_revalidation_required": True}
            ),
            lambda value: value["disposable_validation"].update(
                {"current_proof_receipt": None}
            ),
            lambda value: value["disposable_validation"].update(
                {"production_data_read": True}
            ),
            lambda value: value["disposable_validation"].update(
                {"provider_external_calls": 1}
            ),
            lambda value: value["disposable_validation"].update(
                {"final_resources_absent": False}
            ),
            lambda value: value["disposable_validation"].update(
                {"runner_path": "tools/unsealed-disposable-runner.sh"}
            ),
            lambda value: value["disposable_validation"].update(
                {"runner_sha256": "0" * 64}
            ),
            lambda value: value["disposable_validation"].update(
                {"runner_sealed": False}
            ),
            lambda value: value["installation_controller_validation"].update(
                {"current_disposable_proof_complete": True}
            ),
            lambda value: value["installation_controller_validation"].update(
                {"current_proof_receipt": "forged-phase8a-proof.json"}
            ),
            lambda value: value["installation_controller_validation"].update(
                {"live_backend_packaged": True}
            ),
            lambda value: value["installation_controller_validation"].update(
                {"controller_plan_sha256": "0" * 64}
            ),
            lambda value: value["installation_controller_validation"].update(
                {"required_disposable_scenario_count": 335}
            ),
            lambda value: value["installation_controller_validation"].update(
                {"phase8b_source_connection_count_required": 1}
            ),
            lambda value: value["installation_controller_validation"].update(
                {
                    "installation_decision_receipt_schema_version": (
                        "governed-memory-installation-decision-receipt-v1"
                    )
                }
            ),
            lambda value: value["installation_controller_validation"][
                "phase8b_migration_execution_contract"
            ].update({"source_conversation_bridge_included": True}),
            lambda value: value["installation_controller_validation"].update(
                {"phase8b_store_supervisor_is_application_runtime": True}
            ),
            lambda value: value["installation_controller_validation"][
                "phase8b_installation_blockers"
            ].remove("canonical_global_execution_lock_not_packaged"),
            lambda value: value["http_runtime"].update(
                {"source_logging_parameter_remediation_applied": True}
            ),
            lambda value: value["ingestion"].update(
                {
                    "source_erasure_structured_lifeswitch_data_or_accounts_deleted": True
                }
            ),
        ):
            variant = json.loads(json.dumps(original))
            mutate(variant)
            variants.append(variant)
        for index, variant in enumerate(variants):
            with self.subTest(index=index), self.assertRaisesRegex(
                ReleaseGuardError,
                "release_(runtime_manifest|source_erasure_scope)_invalid",
            ):
                release_guard._verify_runtime_manifest(variant)

    def test_disposable_runner_tamper_fails_release_guard(self) -> None:
        runner = (
            ROOT
            / "tools"
            / "governed_memory_validation"
            / "run_disposable_successor.sh"
        ).resolve()
        original_sha256 = release_guard._sha256

        def altered_sha256(path: Path) -> str:
            if path.resolve() == runner:
                return "0" * 64
            return original_sha256(path)

        with mock.patch.object(
            release_guard, "_sha256", side_effect=altered_sha256
        ), self.assertRaisesRegex(
            ReleaseGuardError, "release_artifact_hash_mismatch"
        ):
            verify_candidate_artifacts()

    def test_postgres_bootstrap_tamper_fails_release_guard(self) -> None:
        bootstrap = (
            ROOT
            / "tools"
            / "governed_memory_validation"
            / "postgres_bootstrap.pgsql"
        ).resolve()
        original_sha256 = release_guard._sha256

        def altered_sha256(path: Path) -> str:
            if path.resolve() == bootstrap:
                return "0" * 64
            return original_sha256(path)

        with mock.patch.object(
            release_guard, "_sha256", side_effect=altered_sha256
        ), self.assertRaisesRegex(
            ReleaseGuardError, "release_artifact_hash_mismatch"
        ):
            verify_candidate_artifacts()

    def test_runtime_receipt_semantic_drift_is_rejected(self) -> None:
        original = json.loads(
            (OPS / "runtime_build_receipt.json").read_text(encoding="ascii")
        )
        variants = []
        for key, value in (
            ("network_calls", 1),
            ("provider_calls", 1),
            ("legacy_environment_imported", True),
            ("source_tree_sha256", "0" * 64),
            ("project_wheel_sha256", "0" * 64),
        ):
            variant = json.loads(json.dumps(original))
            variant[key] = value
            variants.append(variant)
        with tempfile.TemporaryDirectory() as directory:
            for index, variant in enumerate(variants):
                path = Path(directory) / f"receipt-{index}.json"
                path.write_text(json.dumps(variant), encoding="ascii")
                with self.subTest(index=index), mock.patch.object(
                    release_guard, "RUNTIME_BUILD_RECEIPT", path
                ), self.assertRaisesRegex(
                    ReleaseGuardError, "release_runtime_contract_invalid"
                ):
                    verify_candidate_artifacts()

    def test_package_and_migration_identity_drift_are_rejected(self) -> None:
        bad_package = {
            "package_manifest_sha256": "0" * 64,
            "evaluator_mutating_commands_executed": 0,
            "evaluator_provider_calls": 0,
            "evaluator_state_changed": False,
        }
        with mock.patch.object(
            release_guard,
            "verify_installation_package",
            return_value=bad_package,
        ), self.assertRaisesRegex(
            ReleaseGuardError, "release_installation_package_invalid"
        ):
            verify_candidate_artifacts()

        bad_migration = {
            "schema_version": "governed-memory-migration-verification-v4",
            "result": "verified",
            "validation_state": "phase7b_static_unit_validated_disposable_revalidation_required",
            "manifest_sha256": release_guard.EXPECTED_MIGRATION_MANIFEST_SHA256,
            "migration_package_id_sha256": (
                release_guard.EXPECTED_MIGRATION_PACKAGE_ID_SHA256
            ),
            "file_count": 15,
        }
        with mock.patch.object(
            release_guard,
            "verify_migration_manifest",
            return_value=bad_migration,
        ), self.assertRaisesRegex(
            ReleaseGuardError, "release_migration_manifest_invalid"
        ):
            verify_candidate_artifacts()

    def test_release_json_loader_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, payload in (
                ("duplicate.json", b'{"a":1,"a":2}'),
                ("nan.json", b'{"a":NaN}'),
                ("infinite.json", b'{"a":Infinity}'),
            ):
                path = root / name
                path.write_bytes(payload)
                with self.subTest(name=name), self.assertRaisesRegex(
                    ReleaseGuardError, "release_json_invalid"
                ):
                    release_guard._load_json(path)

    def test_phase6e_is_historical_and_retired_current_paths_are_absent(
        self,
    ) -> None:
        self.assertTrue(
            (OPS / "history" / "phase6e" / "runtime_build_receipt.json").is_file()
        )
        self.assertTrue(
            (OPS / "history" / "phase6e" / "disposable_proof_receipt.json").is_file()
        )
        self.assertFalse(
            (OPS / "phase6e_disposable_proof_receipt.json").exists()
        )
        self.assertTrue((OPS / "phase7c_disposable_proof_receipt.json").is_file())
        self.assertFalse((OPS / "history" / "phase7c").exists())
        self.assertFalse(
            (
                OPS
                / "installation"
                / "postgres"
                / "canonical_bootstrap_finalize.pgsql"
            ).exists()
        )

    def test_blockers_and_chat_only_scope_are_aligned(self) -> None:
        runtime = json.loads(
            (OPS / "runtime_manifest.json").read_text(encoding="utf-8")
        )
        bootstrap = json.loads(
            (OPS / "bootstrap_contract.json").read_text(encoding="utf-8")
        )
        pilot = json.loads(
            (OPS / "pilot_contract.json").read_text(encoding="utf-8")
        )
        schema = json.loads(
            (ROOT / "governed-memory-migrations" / "schema_contract.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(EXPECTED_ACTIVATION_BLOCKERS), 33)
        self.assertNotIn(
            "phase7b_disposable_revalidation_required",
            EXPECTED_ACTIVATION_BLOCKERS,
        )
        self.assertEqual(
            runtime["activation"]["blockers"], EXPECTED_ACTIVATION_BLOCKERS
        )
        self.assertEqual(
            bootstrap["create_policy"]["unresolved_creation_prerequisites"],
            EXPECTED_ACTIVATION_BLOCKERS,
        )
        self.assertEqual(pilot["start_blockers"], EXPECTED_ACTIVATION_BLOCKERS)
        self.assertEqual(
            schema["hard_requirements"]["production_activation_blockers"],
            EXPECTED_ACTIVATION_BLOCKERS,
        )
        ingestion = runtime["ingestion"]
        self.assertEqual(
            ingestion["source_erasure_direct_delete_roots"],
            ["public.chat_log", "public.chat_attachments", "public.threads"],
        )
        self.assertFalse(
            ingestion[
                "source_erasure_structured_lifeswitch_data_or_accounts_deleted"
            ]
        )
        self.assertFalse(
            ingestion[
                "source_erasure_memory_only_or_account_wide_memory_selector_allowed"
            ]
        )
        self.assertFalse(pilot["source_erasure"]["accounts_deleted"])
        self.assertFalse(
            pilot["source_erasure"]["structured_lifeswitch_data_deleted"]
        )

    def _load_phase8a_proof_parts(
        self,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        wrapper = json.loads(
            (OPS / "phase8a_disposable_proof_receipt.json").read_text(
                encoding="utf-8"
            )
        )
        history = OPS / "history" / "phase8a"
        controller = json.loads(
            (history / "controller_disposable_proof_receipt.json").read_text(
                encoding="utf-8"
            )
        )
        postgresql = json.loads(
            (history / "postgresql16_disposable_proof_receipt.json").read_text(
                encoding="utf-8"
            )
        )
        return wrapper, controller, postgresql

    def _assert_phase8a_proof_rejected(
        self,
        wrapper: dict[str, object],
        controller: dict[str, object],
        postgresql: dict[str, object],
        **expected_constants: object,
    ) -> None:
        package = release_guard.verify_installation_package()
        context = (
            mock.patch.multiple(release_guard, **expected_constants)
            if expected_constants
            else nullcontext()
        )
        with context:
            with self.assertRaisesRegex(
                ReleaseGuardError,
                "release_phase8a_disposable_proof_invalid",
            ):
                release_guard._verify_phase8a_proof(
                    wrapper, controller, postgresql, package
                )

    def test_phase8a_proof_is_closed_cross_bound_and_self_bound(self) -> None:
        wrapper, controller, postgresql = self._load_phase8a_proof_parts()
        package = release_guard.verify_installation_package()
        release_guard._verify_phase8a_proof(
            wrapper, controller, postgresql, package
        )
        self.assertEqual(
            controller["package_artifact_sha256"], package["artifact_sha256"]
        )
        self.assertEqual(len(controller["package_artifact_sha256"]), 59)
        self.assertEqual(controller["scenario_counts"]["total"], 336)
        self.assertEqual(postgresql["execution"]["positive_scenario_count"], 21)
        self.assertEqual(postgresql["execution"]["refusal_scenario_count"], 5)
        self.assertTrue(postgresql["bindings"]["proof_harness_self_bound"])
        harness = (
            OPS
            / "history"
            / "phase8a"
            / "postgresql16_disposable_proof_harness.sh"
        )
        self.assertTrue(harness.is_file())
        self.assertFalse(harness.is_symlink())
        self.assertEqual(harness.stat().st_mode & 0o777, 0o644)
        self.assertEqual(
            hashlib.sha256(harness.read_bytes()).hexdigest(),
            release_guard.EXPECTED_PHASE8A_POSTGRESQL16_HARNESS_SHA256,
        )

    def test_phase8a_proof_shapes_are_closed(self) -> None:
        original = self._load_phase8a_proof_parts()
        variants: list[
            tuple[str, dict[str, object], dict[str, object], dict[str, object]]
        ] = []
        for label, section, nested in (
            ("wrapper", 0, None),
            ("wrapper_subject", 0, "subject"),
            ("controller", 1, None),
            ("postgresql_candidate", 2, "candidate"),
        ):
            values = json.loads(json.dumps(original))
            target = values[section] if nested is None else values[section][nested]
            target["unexpected"] = True
            variants.append((label, *values))
        for label, wrapper, controller, postgresql in variants:
            with self.subTest(label=label):
                self._assert_phase8a_proof_rejected(
                    wrapper, controller, postgresql
                )

    def test_phase8a_proof_semantic_tamper_is_rejected_after_rebinding(self) -> None:
        mutations = (
            ("controller_total", "controller", ("scenario_counts", "total"), 335),
            (
                "controller_external_effect",
                "controller",
                ("external_effect_counts", "provider_calls"),
                1,
            ),
            (
                "controller_boolean_as_integer",
                "controller",
                ("local_import_guard", "installed_before_local_artifact_imports"),
                1,
            ),
            ("postgresql_candidate", "postgresql", ("candidate", "head"), "a" * 40),
            (
                "postgresql_refusal_state",
                "postgresql",
                ("execution", "refusal_state_unchanged"),
                False,
            ),
            (
                "postgresql_authority",
                "postgresql",
                ("authority", "installation_authorized"),
                True,
            ),
            (
                "postgresql_boolean_as_integer",
                "postgresql",
                ("cleanup", "exact_owned_resources_removed"),
                1,
            ),
            (
                "postgresql_harness_self_bind",
                "postgresql",
                ("bindings", "proof_harness_self_bound"),
                False,
            ),
            (
                "postgresql_harness_hash",
                "postgresql",
                ("bindings", "proof_harness_sha256"),
                "0" * 64,
            ),
        )
        for label, target_name, path, replacement in mutations:
            wrapper, controller, postgresql = json.loads(
                json.dumps(self._load_phase8a_proof_parts())
            )
            target = controller if target_name == "controller" else postgresql
            target[path[0]][path[1]] = replacement
            if target_name == "controller":
                canonical = release_guard._canonical_json_sha256(controller)
                wrapper["evidence"]["controller_receipt_canonical_sha256"] = canonical
                constants = {
                    "EXPECTED_PHASE8A_CONTROLLER_PROOF_CANONICAL_SHA256": canonical
                }
            else:
                canonical = release_guard._canonical_json_sha256(postgresql)
                wrapper["evidence"]["postgresql16_receipt_canonical_sha256"] = canonical
                constants = {
                    "EXPECTED_PHASE8A_POSTGRESQL16_PROOF_CANONICAL_SHA256": canonical
                }
            with self.subTest(label=label):
                self._assert_phase8a_proof_rejected(
                    wrapper,
                    controller,
                    postgresql,
                    **constants,
                )

    def test_phase8a_wrapper_boolean_as_integer_is_rejected(self) -> None:
        wrapper, controller, postgresql = self._load_phase8a_proof_parts()
        wrapper["proof_summary"]["cleanup_complete"] = 1
        self._assert_phase8a_proof_rejected(wrapper, controller, postgresql)

    def _assert_phase7c_proof_rejected(
        self,
        proof: dict[str, object],
        **expected_constants: object,
    ) -> None:
        context = (
            mock.patch.multiple(release_guard, **expected_constants)
            if expected_constants
            else nullcontext()
        )
        with context:
            with self.assertRaisesRegex(
                ReleaseGuardError,
                "release_phase7c_disposable_proof_invalid",
            ):
                release_guard._verify_phase7c_proof(proof)

    def test_phase7c_proof_shapes_are_closed(self) -> None:
        original = json.loads(
            (OPS / "phase7c_disposable_proof_receipt.json").read_text(
                encoding="ascii"
            )
        )
        variants: list[tuple[str, dict[str, object]]] = []
        outer = json.loads(json.dumps(original))
        outer["unexpected"] = True
        variants.append(("outer", outer))
        nested = json.loads(json.dumps(original))
        nested["http_vertical_slice_receipt"]["unexpected"] = True
        variants.append(("nested", nested))
        missing_outer = json.loads(json.dumps(original))
        del missing_outer["phase"]
        variants.append(("missing_outer", missing_outer))
        missing_nested = json.loads(json.dumps(original))
        del missing_nested["deletion_receipt"]["schema"]
        variants.append(("missing_nested", missing_nested))

        for label, variant in variants:
            with self.subTest(label=label):
                self._assert_phase7c_proof_rejected(variant)

    def test_release_json_loader_rejects_duplicate_and_nonfinite_values(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid.json"
            for label, payload in (
                ("duplicate", '{"value":1,"value":2}'),
                ("nan", '{"value":NaN}'),
                ("infinity", '{"value":Infinity}'),
            ):
                path.write_text(payload, encoding="ascii")
                with self.subTest(label=label), self.assertRaisesRegex(
                    ReleaseGuardError,
                    "release_json_invalid",
                ):
                    release_guard._load_json(path)

    def test_phase7c_proof_parent_identity_drift_is_rejected(self) -> None:
        original = json.loads(
            (OPS / "phase7c_disposable_proof_receipt.json").read_text(
                encoding="ascii"
            )
        )
        for field, value in (
            ("candidate_head", "a" * 40),
            ("candidate_tree", "b" * 40),
        ):
            variant = json.loads(json.dumps(original))
            variant["proof_receipt"][field] = value
            proof_sha256 = release_guard._canonical_json_sha256(
                variant["proof_receipt"]
            )
            variant["proof_receipt_canonical_sha256"] = proof_sha256
            with self.subTest(field=field):
                self._assert_phase7c_proof_rejected(
                    variant,
                    EXPECTED_PHASE7C_PROOF_CANONICAL_SHA256=proof_sha256,
                )

    def test_phase7c_nested_receipt_hash_tamper_is_rejected(self) -> None:
        original = json.loads(
            (OPS / "phase7c_disposable_proof_receipt.json").read_text(
                encoding="ascii"
            )
        )
        original["http_vertical_slice_receipt"]["production_data_read"] = True
        self._assert_phase7c_proof_rejected(original)

    def test_phase7c_safety_semantic_drift_is_rejected(self) -> None:
        original = json.loads(
            (OPS / "phase7c_disposable_proof_receipt.json").read_text(
                encoding="ascii"
            )
        )
        variants = (
            ("proof_network_call", "proof_receipt", "external_network_calls", 1),
            (
                "http_production_read",
                "http_vertical_slice_receipt",
                "production_data_read",
                True,
            ),
            (
                "lifeswitch_snapshot",
                "deletion_receipt",
                "lifeswitch_snapshot_bytes",
                2766,
            ),
            (
                "project_conflict_status",
                "deletion_receipt",
                "typed_project_conflict_status",
                200,
            ),
            (
                "resilience_crash_count",
                "deletion_resilience_receipt",
                "crash_boundary_count",
                8,
            ),
        )
        nested_bindings = {
            "http_vertical_slice_receipt": (
                "integration_receipt_sha256",
                "EXPECTED_PHASE7C_HTTP_RECEIPT_SHA256",
            ),
            "deletion_receipt": (
                "deletion_receipt_sha256",
                "EXPECTED_PHASE7C_DELETION_RECEIPT_SHA256",
            ),
            "deletion_resilience_receipt": (
                "deletion_resilience_receipt_sha256",
                "EXPECTED_PHASE7C_RESILIENCE_RECEIPT_SHA256",
            ),
        }
        for label, section, field, value in variants:
            variant = json.loads(json.dumps(original))
            variant[section][field] = value
            constants: dict[str, object] = {}
            if section in nested_bindings:
                proof_field, constant = nested_bindings[section]
                nested_sha256 = release_guard._canonical_json_sha256(
                    variant[section]
                )
                variant["proof_receipt"][proof_field] = nested_sha256
                constants[constant] = nested_sha256
            proof_sha256 = release_guard._canonical_json_sha256(
                variant["proof_receipt"]
            )
            variant["proof_receipt_canonical_sha256"] = proof_sha256
            constants["EXPECTED_PHASE7C_PROOF_CANONICAL_SHA256"] = proof_sha256
            with self.subTest(label=label):
                self._assert_phase7c_proof_rejected(variant, **constants)

    def test_runtime_and_build_locks_are_closed_and_exact(self) -> None:
        runtime = _parse_hash_lock(RUNTIME_LOCK)
        self.assertEqual(len(runtime), 19)
        self.assertEqual(runtime["asyncpg"][0], "0.30.0")
        self.assertEqual(runtime["fastapi"][0], "0.120.4")
        self.assertEqual(runtime["pyjwt"][0], "2.13.0")
        self.assertNotIn("openai", runtime)
        self.assertNotIn("qdrant-client", runtime)
        self.assertEqual(
            _parse_hash_lock(BUILD_LOCK),
            {"setuptools": ("84.0.0", SETUPTOOLS_SHA256)},
        )

    def test_runtime_root_is_bound_to_exact_package_source_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory).resolve() / "governed_memory"
            runtime_package = package / "runtime"
            runtime_package.mkdir(parents=True)
            (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
            (runtime_package / "__init__.py").write_text("", encoding="utf-8")
            for index, relative_text in enumerate(
                sorted(PROVIDER_ASSET_SOURCE_PATHS)
            ):
                asset = package / relative_text
                asset.parent.mkdir(parents=True, exist_ok=True)
                asset.write_text(f"synthetic-asset-{index}\n", encoding="utf-8")
            first = _package_source_tree_sha256(package)
            first_root = _source_bound_root(
                kind="runtime",
                lock_sha256="a" * 64,
                source_tree_sha256=first,
            )

            (package / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
            second = _package_source_tree_sha256(package)
            second_root = _source_bound_root(
                kind="runtime",
                lock_sha256="a" * 64,
                source_tree_sha256=second,
            )
            self.assertNotEqual(first, second)
            self.assertNotEqual(first_root, second_root)
            self.assertEqual(
                str(second_root),
                f"/tmp/governed-memory-successor-runtime-{'a' * 64}-{second}",
            )

            first_asset = package / sorted(PROVIDER_ASSET_SOURCE_PATHS)[0]
            first_asset.write_text("changed-synthetic-asset\n", encoding="utf-8")
            asset_changed = _package_source_tree_sha256(package)
            self.assertNotEqual(second, asset_changed)

            (package / "unexpected.txt").write_text("stale", encoding="utf-8")
            with self.assertRaisesRegex(
                CandidateBuildError, "candidate_source_inventory_invalid"
            ):
                _package_source_tree_sha256(package)
            (package / "unexpected.txt").unlink()

            first_asset.unlink()
            with self.assertRaisesRegex(
                CandidateBuildError, "candidate_source_inventory_invalid"
            ):
                _package_source_tree_sha256(package)

    def test_runtime_wheelhouse_is_owner_private_and_exact_hash_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheelhouse = Path(directory).resolve() / "wheelhouse"
            wheelhouse.mkdir(mode=0o700)
            first = wheelhouse / "alpha-1.0-py3-none-any.whl"
            second = wheelhouse / "beta-2.0-py3-none-any.whl"
            first.write_bytes(b"synthetic-alpha-wheel")
            second.write_bytes(b"synthetic-beta-wheel")
            first.chmod(0o600)
            second.chmod(0o600)
            packages = {
                "alpha": ("1.0", hashlib.sha256(first.read_bytes()).hexdigest()),
                "beta": ("2.0", hashlib.sha256(second.read_bytes()).hexdigest()),
            }
            observed = _verify_runtime_wheelhouse(wheelhouse, packages)
            self.assertEqual(set(observed), {first.name, second.name})

            stale = wheelhouse / "alpha-0.9-py3-none-any.whl"
            stale.write_bytes(b"stale")
            stale.chmod(0o600)
            with self.assertRaisesRegex(
                CandidateBuildError, "candidate_runtime_wheelhouse_invalid"
            ):
                _verify_runtime_wheelhouse(wheelhouse, packages)
            stale.unlink()

            os.chmod(wheelhouse, 0o775)
            with self.assertRaisesRegex(
                CandidateBuildError, "candidate_runtime_wheelhouse_invalid"
            ):
                _verify_runtime_wheelhouse(wheelhouse, packages)
            os.chmod(wheelhouse, 0o700)

            first.chmod(0o660)
            with self.assertRaisesRegex(
                CandidateBuildError, "candidate_runtime_wheelhouse_invalid"
            ):
                _verify_runtime_wheelhouse(wheelhouse, packages)

    def test_project_wheel_contains_only_exact_source_and_provider_asset_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            package = root / "governed_memory"
            (package / "provider_assets").mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "provider_assets" / "__init__.py").write_text(
                "", encoding="utf-8"
            )
            for index, relative_text in enumerate(
                sorted(PROVIDER_ASSET_SOURCE_PATHS)
            ):
                (package / relative_text).write_bytes(
                    f"synthetic-provider-asset-{index}\n".encode("ascii")
                )

            wheel = root / PROJECT_WHEEL
            metadata = (
                "Metadata-Version: 2.4\n"
                "Name: governed-memory-successor\n"
                "Version: 0.0.0\n"
                "Requires-Python: ==3.12.*\n"
                "Requires-Dist: asyncpg==0.30.0\n"
                "Requires-Dist: cryptography==49.0.0\n"
                "Requires-Dist: fastapi==0.120.4\n"
                "Requires-Dist: PyJWT==2.13.0\n"
                "Requires-Dist: uvicorn==0.38.0\n"
            )
            wheel_metadata = (
                "Wheel-Version: 1.0\n"
                "Generator: setuptools (84.0.0)\n"
                "Root-Is-Purelib: true\n"
                "Tag: py3-none-any\n"
            )
            entry_points = (
                "[console_scripts]\n"
                "governed-memory-http = "
                "rag_engine.governed_memory.runtime.application:main\n"
            )

            def record_hash(value: bytes) -> str:
                encoded = base64.urlsafe_b64encode(hashlib.sha256(value).digest())
                return "sha256=" + encoded.rstrip(b"=").decode("ascii")

            def write_wheel(*, corrupt_asset: bool = False) -> None:
                members: dict[str, bytes] = {}
                for path in sorted(package.rglob("*")):
                    if not path.is_file():
                        continue
                    relative = path.relative_to(package).as_posix()
                    data = path.read_bytes()
                    if corrupt_asset and relative.endswith(
                        "extraction_output.schema.json"
                    ):
                        data = b"corrupt-provider-asset\n"
                    members["rag_engine/governed_memory/" + relative] = data
                members.update(
                    {
                        f"{DIST_INFO_PREFIX}METADATA": metadata.encode("ascii"),
                        f"{DIST_INFO_PREFIX}WHEEL": wheel_metadata.encode("ascii"),
                        f"{DIST_INFO_PREFIX}entry_points.txt": entry_points.encode(
                            "ascii"
                        ),
                        f"{DIST_INFO_PREFIX}top_level.txt": b"rag_engine\n",
                    }
                )
                record_name = f"{DIST_INFO_PREFIX}RECORD"
                output = io.StringIO(newline="")
                writer = csv.writer(output, lineterminator="\n")
                for name in sorted(members):
                    value = members[name]
                    writer.writerow((name, record_hash(value), str(len(value))))
                writer.writerow((record_name, "", ""))
                members[record_name] = output.getvalue().encode("utf-8")
                with zipfile.ZipFile(wheel, "w") as archive:
                    for name in sorted(members):
                        archive.writestr(name, members[name])

            write_wheel()
            _verify_project_wheel(wheel, expected_package_root=package)
            write_wheel(corrupt_asset=True)
            with self.assertRaisesRegex(
                CandidateBuildError, "candidate_project_wheel_invalid"
            ):
                _verify_project_wheel(wheel, expected_package_root=package)

    def test_receipt_schema_is_closed_and_content_free(self) -> None:
        schema = json.loads(
            (OPS / "release_receipt.schema.json").read_text(encoding="utf-8")
        )
        self.assertFalse(schema["additionalProperties"])
        serialized = json.dumps(schema, sort_keys=True).lower()
        for forbidden in (
            "authorization_token",
            "service_token",
            "api_key",
            "message_text",
            "attachment_text",
            "claim_content",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)

    def test_systemd_templates_are_dormant(self) -> None:
        http_unit = (
            OPS / "systemd" / "governed-memory-http.service.in"
        ).read_text(encoding="utf-8")
        self.assertNotIn("[Install]", http_unit)
        self.assertNotIn("WantedBy=", http_unit)
        self.assertIn("GOVERNED_MEMORY_HTTP_MODE=off", http_unit)
        self.assertIn("Restart=no", http_unit)

        worker_unit = (
            OPS / "systemd" / "governed-memory-worker.service.in"
        ).read_text(encoding="utf-8")
        self.assertNotIn("[Install]", worker_unit)
        self.assertNotIn("WantedBy=", worker_unit)
        self.assertIn("GOVERNED_MEMORY_WORKER_MODE=off", worker_unit)
        self.assertIn("Type=oneshot", worker_unit)
        self.assertIn("Restart=no", worker_unit)
        self.assertIn(
            "ConditionPathExists=/etc/governed-memory/runtime/worker.env",
            worker_unit,
        )
        self.assertIn(
            "ConditionPathExists=/etc/governed-memory/runtime/pilot.env",
            worker_unit,
        )
        self.assertFalse(
            (OPS / "systemd" / "governed-memory-worker.timer").exists()
        )


class ReleaseDecisionTests(unittest.TestCase):
    def test_create_refuses_while_activation_blockers_remain(self) -> None:
        result = evaluate_release_observation(observation("create", state="absent"))
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason_code"], "activation_blockers_open")
        self.assertEqual(result["commands_executed"], 0)
        self.assertEqual(result["exact_action_plan"], [])

    def test_create_refuses_any_existing_target(self) -> None:
        document = observation("create", state="absent")
        document["targets"]["database"]["state"] = "present_exact"
        result = evaluate_release_observation(document)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["exact_action_plan"], [])

    def test_cleanup_refuses_without_scoped_authorization(self) -> None:
        result = evaluate_release_observation(
            observation("cleanup", state="present_exact")
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason_code"], "authorization_missing")
        self.assertEqual(result["exact_action_plan"], [])

    def test_observation_counts_cannot_bypass_authority(self) -> None:
        document = observation("cleanup", state="present_exact")
        document["pilot_ever_started"] = True
        document["postgresql_user_row_count"] = 1
        document["qdrant_point_count"] = 1
        document["active_client_count"] = 1
        result = evaluate_release_observation(document)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason_code"], "authorization_missing")


if __name__ == "__main__":
    unittest.main()

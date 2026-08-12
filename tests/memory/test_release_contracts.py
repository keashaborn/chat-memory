from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.governed_memory_release import release_guard
from tools.governed_memory_release.release_guard import (
    CURRENT_CREATE_REFUSAL_CODE,
    EXACT_TARGETS,
    EXPECTED_HISTORICAL_PHASE7C_ARTIFACT_HASHES,
    REQUIRED_ACTIVATION_BLOCKERS,
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


class Phase9BReleaseArtifactTests(unittest.TestCase):
    def test_current_package_and_retained_application_evidence_verify(self) -> None:
        result = verify_candidate_artifacts()
        self.assertEqual(
            result["schema_version"],
            "governed-memory-release-artifact-verification-v6",
        )
        self.assertEqual(
            result["phase"],
            "phase9b_canonical_dormant_store_install_and_empty_rollback_"
            "controllers_packaged_inactive_activation_blocked",
        )
        self.assertTrue(result["artifact_integrity_verified"])
        self.assertTrue(
            result["historical_phase7c_runtime_build_evidence_verified"]
        )
        self.assertTrue(result["current_runtime_build_evidence_verified"])
        self.assertFalse(result["current_runtime_rebuild_required"])
        self.assertTrue(
            result["historical_phase7c_application_proof_verified"]
        )
        self.assertFalse(
            result[
                "historical_phase7c_proof_reusable_for_current_candidate"
            ]
        )
        self.assertFalse(result["historical_phase7c_proof_is_live_proof"])
        self.assertTrue(
            result["current_migration_artifact_integrity_verified"]
        )
        self.assertTrue(
            result["current_migration_disposable_validation_complete"]
        )
        self.assertFalse(
            result["current_migration_disposable_revalidation_required"]
        )
        self.assertTrue(
            result["current_store_package_static_verification_complete"]
        )
        self.assertEqual(result["current_store_package_artifact_count"], 56)
        self.assertTrue(result["synthetic_proof_harness_packaged"])
        self.assertFalse(result["current_store_synthetic_proof_complete"])
        self.assertFalse(
            result[
                "historical_dormant_store_install_synthetic_proof_reusable_for_current_candidate"
            ]
        )
        self.assertFalse(result["synthetic_proof_executed_by_release_guard"])
        self.assertFalse(result["synthetic_proof_receipt_promoted"])
        self.assertTrue(result["current_candidate_disposable_proof_complete"])
        self.assertFalse(result["release_allowed"])
        self.assertEqual(
            result["release_refusal_code"], CURRENT_CREATE_REFUSAL_CODE
        )
        self.assertFalse(result["live_installation_proof_complete"])
        self.assertTrue(result["installation_executor_packaged"])
        self.assertTrue(result["rollback_executor_packaged"])
        for field in (
            "controller_runtime_verification_capability_packaged",
            "full_controller_release_tree_verification_packaged",
            "exact_locked_controller_distribution_set_verification_packaged",
            "full_release_tree_sha256_bound_through_claim_journal_host_ownership_and_install_receipt",
            "empty_rollback_full_runtime_and_release_identity_bound_through_authority_claim_journal_requests_observations_writer_fence_and_receipt",
            "controller_runtime_and_release_require_separate_future_build_and_install_authority",
            "supervisor_launcher_source_packaged",
            "resolved_store_spec_and_exact_docker_labels_bound",
            "resource_identity_ledger_v2_packaged",
            "empty_rollback_writer_fence_packaged",
            "retained_audit_artifact_hashes_bound",
        ):
            self.assertTrue(result[field], field)
        self.assertFalse(result["controller_runtime_built_or_installed"])
        self.assertFalse(result["controller_release_staged"])
        self.assertFalse(result["stores_install_owns_or_removes_controller_substrate"])
        self.assertFalse(result["concrete_install_store_effect_adapters_packaged"])
        self.assertFalse(
            result["concrete_empty_rollback_store_effect_adapters_packaged"]
        )
        self.assertFalse(result["activation_executor_packaged"])
        self.assertFalse(result["installation_authorized"])
        self.assertFalse(result["activation_authorized"])
        self.assertEqual(result["external_calls"], 0)
        self.assertEqual(result["commands_executed"], 0)
        self.assertFalse(result["production_state_changed"])
        self.assertIn(
            "ops/governed_memory/installation/current/package_manifest.json",
            result["artifact_sha256"],
        )
        self.assertIn(
            "ops/governed_memory/current_component_disposition.json",
            result["artifact_sha256"],
        )
        self.assertIn(
            "ops/governed_memory/runtime_build_receipt.json",
            result["artifact_sha256"],
        )
        self.assertEqual(
            {
                path: result["artifact_sha256"][path]
                for path in EXPECTED_HISTORICAL_PHASE7C_ARTIFACT_HASHES
            },
            EXPECTED_HISTORICAL_PHASE7C_ARTIFACT_HASHES,
        )
        self.assertFalse(
            any("history/phase6" in path for path in result["artifact_sha256"])
        )
        self.assertNotIn(
            "ops/governed_memory/installation/package_manifest.json",
            result["artifact_sha256"],
        )

    def test_runtime_manifest_semantic_drift_is_rejected(self) -> None:
        original = json.loads(
            (OPS / "runtime_manifest.json").read_text(encoding="utf-8")
        )
        variants: list[dict[str, object]] = []
        for mutate in (
            lambda value: value["activation"].update(
                {"production_authorized": True}
            ),
            lambda value: value["activation"][
                "retained_phase7a_snapshot_running_services"
            ].append(
                "governed-memory-http.service"
            ),
            lambda value: value["disposable_validation"].update(
                {"reusable_as_current_store_installation_proof": True}
            ),
            lambda value: value["disposable_validation"].update(
                {"reusable_as_live_proof": True}
            ),
            lambda value: value["disposable_validation"].update(
                {"current_proof_complete": False}
            ),
            lambda value: value["validation_runtime"].update(
                {"current_source_bound": False}
            ),
            lambda value: value["validation_runtime"].update(
                {"current_runtime_rebuild_pending": True}
            ),
            lambda value: value["validation_runtime"].update(
                {"current_build_receipt_sha256": "0" * 64}
            ),
            lambda value: value["inactive_store_package"].update(
                {"synthetic_proof_executed_for_current_package": True}
            ),
            lambda value: value["inactive_store_package"].update(
                {"static_package_verification_complete": False}
            ),
            lambda value: value["inactive_store_package"].update(
                {"synthetic_proof_receipt_promoted": True}
            ),
            lambda value: value["inactive_store_package"].update(
                {"live_installation_proof_complete": True}
            ),
            lambda value: value["inactive_store_package"].update(
                {"installation_executor_packaged": False}
            ),
            lambda value: value["inactive_store_package"].update(
                {"package_manifest_sha256": "0" * 64}
            ),
            lambda value: value["inactive_store_package"].update(
                {"controller_runtime_verification_capability_packaged": False}
            ),
            lambda value: value["inactive_store_package"].update(
                {"full_controller_release_tree_verification_packaged": False}
            ),
            lambda value: value["inactive_store_package"].update(
                {"exact_locked_controller_distribution_set_verification_packaged": False}
            ),
            lambda value: value["inactive_store_package"].update(
                {"full_release_tree_sha256_bound_through_claim_journal_host_ownership_and_install_receipt": False}
            ),
            lambda value: value["inactive_store_package"].update(
                {"empty_rollback_full_runtime_and_release_identity_bound_through_authority_claim_journal_requests_observations_writer_fence_and_receipt": False}
            ),
            lambda value: value["inactive_store_package"].update(
                {"supervisor_launcher_source_packaged": False}
            ),
            lambda value: value["inactive_store_package"].update(
                {"controller_runtime_built_or_installed": True}
            ),
            lambda value: value["inactive_store_package"].update(
                {"resolved_store_spec_and_exact_docker_labels_bound": False}
            ),
            lambda value: value["inactive_store_package"].update(
                {"resource_identity_ledger_v2_packaged": False}
            ),
            lambda value: value["inactive_store_package"].update(
                {"empty_rollback_writer_fence_packaged": False}
            ),
            lambda value: value["inactive_store_package"].update(
                {"retained_audit_artifact_hashes_bound": False}
            ),
            lambda value: value["infrastructure"].update(
                {
                    "retained_phase7a_snapshot_postgresql_persistent_resource_created": True
                }
            ),
            lambda value: value["infrastructure"].update(
                {"persistent_composition_packaged": True}
            ),
            lambda value: value["infrastructure"].update(
                {
                    "phase7c_disposable_application_compose_role": (
                        "current_inactive_store_composition"
                    )
                }
            ),
            lambda value: value["http_runtime"].update(
                {"conversation_erasure_route_installed": True}
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
        package_receipt = release_guard.package.verify()
        store_receipt = release_guard.verify_store_migration_manifest.verify()
        for index, variant in enumerate(variants):
            with self.subTest(index=index), self.assertRaisesRegex(
                ReleaseGuardError,
                "release_(runtime_manifest|source_erasure_scope)_invalid",
            ):
                release_guard._verify_runtime_manifest(
                    variant,
                    package_receipt,
                    store_receipt,
                )

    def test_current_package_and_store_receipt_drift_are_rejected(self) -> None:
        current_package = release_guard.package.verify()
        current_store = release_guard.verify_store_migration_manifest.verify()
        package_variants = []
        for key, value in (
            ("artifact_count", current_package["artifact_count"] + 1),
            ("synthetic_proof_executed_by_verifier", True),
            ("synthetic_proof_receipt_promoted", True),
            ("claim_bound_install_controller_composition_packaged", False),
            ("claim_bound_empty_rollback_controller_composition_packaged", False),
            ("controller_runtime_verification_capability_packaged", False),
            ("full_controller_release_tree_verification_packaged", False),
            ("exact_locked_controller_distribution_set_verification_packaged", False),
            (
                "full_release_tree_sha256_bound_through_claim_journal_host_ownership_and_install_receipt",
                False,
            ),
            (
                "empty_rollback_full_runtime_and_release_identity_bound_through_authority_claim_journal_requests_observations_writer_fence_and_receipt",
                False,
            ),
            ("supervisor_launcher_source_packaged", False),
            ("controller_runtime_built_or_installed", True),
            ("controller_release_staged", True),
            (
                "controller_runtime_and_release_require_separate_future_build_and_install_authority",
                False,
            ),
            ("stores_install_owns_or_removes_controller_substrate", True),
            ("resolved_store_spec_and_exact_docker_labels_bound", False),
            ("resource_identity_ledger_v2_packaged", False),
            ("empty_rollback_writer_fence_packaged", False),
            ("retained_audit_artifact_hashes_bound", False),
            (
                "install_receipt_binds_fresh_terminal_canonical_store_readiness",
                False,
            ),
            (
                "empty_rollback_requires_opaque_verified_install_receipt_and_ledger",
                False,
            ),
            (
                "completed_install_and_empty_rollback_replay_reverification_packaged",
                False,
            ),
            ("concrete_install_store_effect_adapters_packaged", True),
            ("concrete_empty_rollback_store_effect_adapters_packaged", True),
            ("images_staged_by_verifier", True),
            ("secrets_touched_by_verifier", True),
        ):
            variant = json.loads(json.dumps(current_package))
            variant[key] = value
            package_variants.append(variant)
        for index, variant in enumerate(package_variants):
            with self.subTest(kind="package", index=index), self.assertRaisesRegex(
                ReleaseGuardError, "release_current_store_package_invalid"
            ):
                release_guard._verify_current_package_receipts(
                    variant, current_store
                )
        bad_store = json.loads(json.dumps(current_store))
        bad_store["source_bridge_artifact_count"] = 1
        with self.assertRaisesRegex(
            ReleaseGuardError, "release_current_store_package_invalid"
        ):
            release_guard._verify_current_package_receipts(
                current_package, bad_store
            )

    def test_current_component_disposition_is_closed_and_exact(self) -> None:
        original = json.loads(
            (OPS / "current_component_disposition.json").read_text(
                encoding="ascii"
            )
        )
        release_guard._verify_current_component_disposition(original)
        mutations = (
            lambda value: value["current_dormant_store_controller"].update(
                {"installation_performed": True}
            ),
            lambda value: value["current_dormant_store_controller"].update(
                {"activation_entrypoint_packaged": True}
            ),
            lambda value: value["current_dormant_store_controller"].update(
                {"production_state_changed": True}
            ),
            lambda value: value["current_dormant_store_controller"].update(
                {"controller_runtime_built_or_installed": True}
            ),
            lambda value: value["historical_only"].update(
                {"may_be_used_as_current_release_authority": True}
            ),
            lambda value: value["safety"].update(
                {"activation_authorized": True}
            ),
            lambda value: value["safety"].update({"provider_calls": 9}),
            lambda value: value["authority"].update(
                {"structured_lifeswitch": "inside_memory_authority"}
            ),
            lambda value: value.update({"unexpected": False}),
        )
        for index, mutate in enumerate(mutations):
            variant = json.loads(json.dumps(original))
            mutate(variant)
            with self.subTest(index=index), self.assertRaisesRegex(
                ReleaseGuardError,
                "release_component_disposition_invalid",
            ):
                release_guard._verify_current_component_disposition(variant)

    def test_release_guard_calls_all_current_static_verifiers(self) -> None:
        current_migration = release_guard.verify_migration_manifest.verify(
            release_guard.MIGRATION_ROOT
        )
        current_package = release_guard.package.verify()
        current_store = release_guard.verify_store_migration_manifest.verify()
        with (
            mock.patch.object(
                release_guard.verify_migration_manifest,
                "verify",
                return_value=current_migration,
            ) as migration_verify,
            mock.patch.object(
                release_guard.package, "verify", return_value=current_package
            ) as package_verify,
            mock.patch.object(
                release_guard.verify_store_migration_manifest,
                "verify",
                return_value=current_store,
            ) as store_verify,
        ):
            verify_candidate_artifacts()
        migration_verify.assert_called_once_with(release_guard.MIGRATION_ROOT)
        package_verify.assert_called_once_with()
        store_verify.assert_called_once_with()

    def test_current_migration_proof_regression_is_rejected(self) -> None:
        current_migration = release_guard.verify_migration_manifest.verify(
            release_guard.MIGRATION_ROOT
        )
        for key, value in (
            ("current_disposable_validation_complete", False),
            ("disposable_revalidation_required", True),
            (
                "historical_phase7c_proof_reusable_for_current_candidate",
                True,
            ),
        ):
            variant = json.loads(json.dumps(current_migration))
            variant[key] = value
            with (
                self.subTest(key=key),
                mock.patch.object(
                    release_guard.verify_migration_manifest,
                    "verify",
                    return_value=variant,
                ),
                self.assertRaisesRegex(
                    ReleaseGuardError,
                    "release_current_migration_artifacts_invalid",
                ),
            ):
                verify_candidate_artifacts()

    def test_current_and_historical_runtime_receipt_drift_are_rejected(self) -> None:
        current = json.loads(
            (OPS / "runtime_build_receipt.json").read_text(encoding="ascii")
        )
        historical = json.loads(
            (
                OPS / "history" / "phase7c" / "runtime_build_receipt.json"
            ).read_text(encoding="ascii")
        )
        verifiers = (
            (
                current,
                release_guard._verify_current_runtime_receipt,
                "release_current_runtime_contract_invalid",
            ),
            (
                historical,
                release_guard._verify_historical_phase7c_runtime_receipt,
                "release_historical_phase7c_runtime_contract_invalid",
            ),
        )
        for original, verifier, error in verifiers:
            for key, value in (
                ("network_calls", 1),
                ("provider_calls", 1),
                ("legacy_environment_imported", True),
                ("source_tree_sha256", "0" * 64),
                ("project_wheel_sha256", "0" * 64),
            ):
                variant = json.loads(json.dumps(original))
                variant[key] = value
                with self.subTest(error=error, key=key), self.assertRaisesRegex(
                    ReleaseGuardError,
                    error,
                ):
                    verifier(variant)

    def test_current_phase8g_proof_is_closed_canonical_and_safety_bound(self) -> None:
        original = json.loads(
            (OPS / "phase8g_disposable_proof_receipt.json").read_text(
                encoding="ascii"
            )
        )
        release_guard._verify_current_phase8g_application_proof(original)
        variants = []
        outer = json.loads(json.dumps(original))
        outer["unexpected"] = True
        variants.append(outer)
        network = json.loads(json.dumps(original))
        network["proof_receipt"]["external_network_calls"] = 1
        variants.append(network)
        lifeswitch = json.loads(json.dumps(original))
        lifeswitch["deletion_receipt"]["lifeswitch_snapshot_bytes"] = 2766
        variants.append(lifeswitch)
        live = json.loads(json.dumps(original))
        live["http_vertical_slice_receipt"]["production_data_read"] = True
        variants.append(live)
        for index, variant in enumerate(variants):
            with self.subTest(index=index), self.assertRaisesRegex(
                ReleaseGuardError, "release_current_phase8g_application_proof_invalid"
            ):
                release_guard._verify_current_phase8g_application_proof(variant)

        canonical = json.loads(json.dumps(original))
        canonical["proof_receipt_canonical_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            ReleaseGuardError, "release_current_phase8g_application_proof_invalid"
        ):
            release_guard._verify_current_phase8g_application_proof(canonical)

    def test_historical_phase7c_proof_is_separate_and_not_current(self) -> None:
        historical = json.loads(
            (OPS / "history" / "phase7c" / "disposable_proof_receipt.json")
            .read_text(encoding="ascii")
        )
        release_guard._verify_historical_phase7c_application_proof(historical)
        self.assertFalse((OPS / "phase7c_disposable_proof_receipt.json").exists())
        self.assertNotEqual(
            historical["proof_receipt_canonical_sha256"],
            json.loads(
                (OPS / "phase8g_disposable_proof_receipt.json").read_text(
                    encoding="ascii"
                )
            )["proof_receipt_canonical_sha256"],
        )

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
                    ReleaseGuardError, "release_artifact_json_invalid"
                ):
                    release_guard._load_json(path)

    def test_phase8g_blockers_and_chat_only_scope_remain_aligned(self) -> None:
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
            (
                ROOT
                / "governed-memory-migrations"
                / "schema_contract.json"
            ).read_text(encoding="utf-8")
        )
        blockers = runtime["activation"]["blockers"]
        self.assertEqual(len(blockers), len(set(blockers)))
        self.assertTrue(REQUIRED_ACTIVATION_BLOCKERS.issubset(blockers))
        self.assertEqual(
            bootstrap["create_policy"]["unresolved_creation_prerequisites"],
            blockers,
        )
        self.assertEqual(pilot["start_blockers"], blockers)
        self.assertEqual(
            schema["hard_requirements"]["production_activation_blockers"],
            blockers,
        )
        bootstrap_candidate = bootstrap["candidate_implementation_status"]
        self.assertEqual(
            bootstrap_candidate["runtime"],
            "phase8g_current_source_bound_runtime_disposable_validated_inactive",
        )
        self.assertFalse(bootstrap_candidate["current_runtime_rebuild_pending"])
        self.assertTrue(
            bootstrap_candidate["current_candidate_disposable_validation_complete"]
        )
        self.assertEqual(
            pilot["candidate_surfaces"]["runtime"],
            bootstrap_candidate["runtime"],
        )
        self.assertFalse(
            pilot["validation_state"]["current_runtime_rebuild_pending"]
        )
        self.assertTrue(
            pilot["validation_state"][
                "current_candidate_disposable_validation_complete"
            ]
        )
        for required in (
            "governed_memory_proxy_group_and_brains_membership_not_provisioned_or_verified",
            "lifeswitch_chat_answer_binding_provenance_and_owner_context_erasure_not_implemented_or_verified",
            "lifeswitch_prior_provenance_view_not_migrated_to_chat_integrity",
            "vantage_answer_trace_erasure_or_retention_disposition_not_decided_or_verified",
            "telemetry_payload_erasure_or_retention_disposition_not_decided_or_verified",
            "conversation_erasure_auxiliary_deleted_object_counts_not_implemented_or_verified",
            "successor_answer_binding_chat_transaction_recovery_not_implemented",
        ):
            with self.subTest(required=required):
                self.assertIn(required, blockers)
        self.assertNotIn("phase8f_current_runtime_rebuild_not_completed", blockers)
        self.assertNotIn(
            "phase8f_current_candidate_disposable_revalidation_not_completed",
            blockers,
        )
        self.assertNotIn(
            "loopback_tcp_endpoint_identity_not_proved_permissioned_unix_socket_or_mtls_required",
            blockers,
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
        self.assertTrue(
            ingestion["source_erasure_lifeswitch_usage_and_audit_records_retained"]
        )
        self.assertFalse(pilot["source_erasure"]["accounts_deleted"])
        self.assertFalse(
            pilot["source_erasure"]["structured_lifeswitch_data_deleted"]
        )
        self.assertTrue(
            pilot["source_erasure"]["lifeswitch_usage_and_audit_records_retained"]
        )

    def test_phase8g_governance_runtime_state_drift_is_rejected(self) -> None:
        runtime = json.loads(
            (OPS / "runtime_manifest.json").read_text(encoding="utf-8")
        )
        original_bootstrap = json.loads(
            (OPS / "bootstrap_contract.json").read_text(encoding="utf-8")
        )
        original_pilot = json.loads(
            (OPS / "pilot_contract.json").read_text(encoding="utf-8")
        )
        schema = json.loads(
            (
                ROOT / "governed-memory-migrations" / "schema_contract.json"
            ).read_text(encoding="utf-8")
        )
        blockers = runtime["activation"]["blockers"]
        variants: list[tuple[dict[str, object], dict[str, object]]] = []
        for target, key, value in (
            ("bootstrap", "runtime", "phase8f_runtime_rebuild_pending"),
            ("bootstrap", "current_runtime_rebuild_pending", True),
            (
                "bootstrap",
                "current_candidate_disposable_validation_complete",
                False,
            ),
            ("pilot_surface", "runtime", "phase8f_runtime_rebuild_pending"),
            ("pilot_validation", "current_runtime_rebuild_pending", True),
            (
                "pilot_validation",
                "current_candidate_disposable_validation_complete",
                False,
            ),
        ):
            bootstrap = json.loads(json.dumps(original_bootstrap))
            pilot = json.loads(json.dumps(original_pilot))
            if target == "bootstrap":
                bootstrap["candidate_implementation_status"][key] = value
            elif target == "pilot_surface":
                pilot["candidate_surfaces"][key] = value
            else:
                pilot["validation_state"][key] = value
            variants.append((bootstrap, pilot))
        for index, (bootstrap, pilot) in enumerate(variants):
            with self.subTest(index=index), self.assertRaisesRegex(
                ReleaseGuardError,
                "release_governance_contract_invalid",
            ):
                release_guard._verify_governance_refusals(
                    bootstrap,
                    pilot,
                    schema,
                    blockers,
                )

    def test_receipt_schema_is_closed_and_content_free(self) -> None:
        schema = json.loads(
            (OPS / "release_receipt.schema.json").read_text(encoding="utf-8")
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("http_transport", schema["required"])
        self.assertNotIn("network", schema["required"])
        transport = schema["properties"]["http_transport"]
        self.assertEqual(
            transport["properties"]["transport"]["const"],
            "permissioned_unix_socket",
        )
        self.assertEqual(
            transport["properties"]["unix_socket_path"]["const"],
            "/run/governed-memory/http.sock",
        )
        self.assertEqual(
            transport["properties"]["unix_socket_group"]["const"],
            "governed-memory-proxy",
        )
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
        for retired_tcp_field in (
            "api_bind",
            "allowed_source_ipv4",
            "firewall_proof_sha256",
        ):
            with self.subTest(retired_tcp_field=retired_tcp_field):
                self.assertNotIn(retired_tcp_field, serialized)

    def test_systemd_templates_remain_dormant(self) -> None:
        http_unit = (
            OPS / "systemd" / "governed-memory-http.service.in"
        ).read_text(encoding="utf-8")
        worker_unit = (
            OPS / "systemd" / "governed-memory-worker.service.in"
        ).read_text(encoding="utf-8")
        for unit in (http_unit, worker_unit):
            self.assertNotIn("[Install]", unit)
            self.assertNotIn("WantedBy=", unit)
            self.assertIn("Restart=no", unit)
        self.assertIn("GOVERNED_MEMORY_HTTP_MODE=off", http_unit)
        self.assertIn("GOVERNED_MEMORY_WORKER_MODE=off", worker_unit)
        self.assertIn(
            "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
            http_unit,
        )
        self.assertFalse(
            (OPS / "systemd" / "governed-memory-worker.timer").exists()
        )


class ReleaseDecisionTests(unittest.TestCase):
    def test_create_refuses_without_installation_authority(self) -> None:
        result = evaluate_release_observation(observation("create", state="absent"))
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason_code"], CURRENT_CREATE_REFUSAL_CODE)
        self.assertTrue(result["current_candidate_disposable_proof_complete"])
        self.assertEqual(result["commands_executed"], 0)
        self.assertEqual(result["exact_action_plan"], [])

    def test_create_refuses_any_existing_target(self) -> None:
        document = observation("create", state="absent")
        document["targets"]["database"]["state"] = "present_exact"
        result = evaluate_release_observation(document)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason_code"], CURRENT_CREATE_REFUSAL_CODE)
        self.assertTrue(result["current_candidate_disposable_proof_complete"])
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

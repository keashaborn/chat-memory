from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.governed_memory_release import release_guard
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


class Phase8DReleaseArtifactTests(unittest.TestCase):
    def test_current_package_and_retained_application_evidence_verify(self) -> None:
        result = verify_candidate_artifacts()
        self.assertEqual(
            result["schema_version"],
            "governed-memory-release-artifact-verification-v3",
        )
        self.assertEqual(
            result["phase"],
            "phase8d_repository_only_phase8a_successor_installation_stack_"
            "retired_"
            "current_inactive_store_package_activation_blocked",
        )
        self.assertTrue(result["runtime_build_evidence_verified"])
        self.assertTrue(result["phase7c_application_proof_verified"])
        self.assertFalse(
            result["phase7c_proof_is_current_store_installation_proof"]
        )
        self.assertFalse(result["phase7c_proof_is_live_proof"])
        self.assertTrue(
            result["current_store_package_static_verification_complete"]
        )
        self.assertEqual(result["current_store_package_artifact_count"], 44)
        self.assertTrue(result["synthetic_proof_harness_packaged"])
        self.assertTrue(
            result["synthetic_proof_executed_separately_for_current_package"]
        )
        self.assertFalse(result["synthetic_proof_executed_by_release_guard"])
        self.assertEqual(result["synthetic_proof_scenario_count"], 131)
        self.assertEqual(
            result["synthetic_proof_receipt_sha256"],
            "ce9a57f6f01c670dfbc030bff812b99bfb13e9782f406d7e576f0d89af1f33c6",
        )
        self.assertFalse(result["synthetic_proof_receipt_promoted"])
        self.assertFalse(result["live_installation_proof_complete"])
        self.assertFalse(result["installation_executor_packaged"])
        self.assertFalse(result["rollback_executor_packaged"])
        self.assertFalse(result["activation_executor_packaged"])
        self.assertFalse(result["installation_authorized"])
        self.assertFalse(result["activation_authorized"])
        self.assertEqual(result["external_calls"], 0)
        self.assertEqual(result["commands_executed"], 0)
        self.assertFalse(result["production_state_changed"])
        self.assertIn(
            "ops/governed_memory/installation/phase8b/package_manifest.json",
            result["artifact_sha256"],
        )
        self.assertIn(
            "ops/governed_memory/history/phase8d/"
            "phase8a_successor_installation_stack_retirement.json",
            result["artifact_sha256"],
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
            lambda value: value["phase7c_application_validation"].update(
                {"reusable_as_current_store_installation_proof": True}
            ),
            lambda value: value["phase7c_application_validation"].update(
                {"reusable_as_live_proof": True}
            ),
            lambda value: value["inactive_store_package"].update(
                {"synthetic_proof_executed_for_current_package": False}
            ),
            lambda value: value["inactive_store_package"].update(
                {"synthetic_proof_receipt_promoted": True}
            ),
            lambda value: value["inactive_store_package"].update(
                {"live_installation_proof_complete": True}
            ),
            lambda value: value["inactive_store_package"].update(
                {"installation_executor_packaged": True}
            ),
            lambda value: value["inactive_store_package"].update(
                {"package_manifest_sha256": "0" * 64}
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
            ("artifact_count", 43),
            ("synthetic_proof_executed_by_verifier", True),
            ("synthetic_proof_receipt_promoted", True),
            ("installation_executor_packaged", True),
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

    def test_release_guard_calls_both_current_verifiers(self) -> None:
        current_package = release_guard.package.verify()
        current_store = release_guard.verify_store_migration_manifest.verify()
        with (
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
        package_verify.assert_called_once_with()
        store_verify.assert_called_once_with()

    def test_runtime_receipt_semantic_drift_is_rejected(self) -> None:
        original = json.loads(
            (OPS / "runtime_build_receipt.json").read_text(encoding="ascii")
        )
        for key, value in (
            ("network_calls", 1),
            ("provider_calls", 1),
            ("legacy_environment_imported", True),
            ("source_tree_sha256", "0" * 64),
            ("project_wheel_sha256", "0" * 64),
        ):
            variant = json.loads(json.dumps(original))
            variant[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(
                ReleaseGuardError, "release_runtime_contract_invalid"
            ):
                release_guard._verify_runtime_receipt(variant)

    def test_phase7c_application_proof_is_closed_and_safety_bound(self) -> None:
        original = json.loads(
            (OPS / "phase7c_disposable_proof_receipt.json").read_text(
                encoding="ascii"
            )
        )
        release_guard._verify_phase7c_application_proof(original)
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
                ReleaseGuardError, "release_phase7c_application_proof_invalid"
            ):
                release_guard._verify_phase7c_application_proof(variant)

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

    def test_blockers_and_chat_only_scope_remain_aligned(self) -> None:
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
        self.assertEqual(len(EXPECTED_ACTIVATION_BLOCKERS), 33)
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
        self.assertFalse(pilot["source_erasure"]["accounts_deleted"])
        self.assertFalse(
            pilot["source_erasure"]["structured_lifeswitch_data_deleted"]
        )

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

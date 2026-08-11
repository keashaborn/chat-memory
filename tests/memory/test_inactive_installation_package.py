from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout

from tools.governed_memory_install import inactive_installation


ROOT = Path(__file__).resolve().parents[2]
INSTALLATION = ROOT / "ops" / "governed_memory" / "installation"
CONTRACT = INSTALLATION / "contract.json"
COMPOSE = INSTALLATION / "compose.persistent.in.yaml"
SECRETS = INSTALLATION / "secrets"
POSTGRES = INSTALLATION / "postgres"

POSTGRES_IMAGE = (
    "postgres:16-alpine@sha256:"
    "57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
)
QDRANT_IMAGE = (
    "qdrant/qdrant:v1.19.0@sha256:"
    "057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc"
)


class InactiveInstallationPackageTests(unittest.TestCase):
    def _canonical_sha256(self, value: object) -> str:
        return hashlib.sha256(
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
        ).hexdigest()

    def _observation(
        self, stage: str, *, prior_receipt_sha256: str | None = None
    ) -> dict[str, object]:
        return {
            "schema_version": "governed-memory-installation-observation-v1",
            "stage": stage,
            "hostname": "ip-172-31-32-171",
            "candidate_git_commit": "b" * 40,
            "candidate_git_tree": "c" * 40,
            "package_manifest_sha256": "a" * 64,
            "prior_decision_receipt_sha256": prior_receipt_sha256,
            "target_states": inactive_installation.TARGET_STATE_PROFILES[stage],
            "port_states": inactive_installation.PORT_STATE_PROFILES[stage],
            "service_account_state": (
                inactive_installation.ACCOUNT_STATE_PROFILES[stage]
            ),
            "unit_states": inactive_installation.UNIT_STATE_PROFILES[stage],
            "source_cluster_states": (
                inactive_installation.SOURCE_CLUSTER_STATE_PROFILES[stage]
            ),
            "successor_store_states": (
                inactive_installation.SUCCESSOR_STORE_STATE_PROFILES[stage]
            ),
            "pilot_ever_started": False,
            "successor_user_memory_row_count": 0,
            "successor_projection_queue_row_count": 0,
            "qdrant_point_count": 0,
            "active_client_count": 0,
            "legacy_import_count": 0,
            "source_application_row_read_count": 0,
        }

    def _assert_closed_receipt_shape(self, receipt: dict[str, object]) -> None:
        schema = json.loads(
            (INSTALLATION / "receipt.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(receipt), set(schema["required"]))
        self.assertEqual(set(receipt), set(schema["properties"]))
        self.assertEqual(set(receipt), inactive_installation.DECISION_RECEIPT_KEYS)
        self.assertEqual(
            receipt["schema_version"],
            "governed-memory-installation-decision-receipt-v2",
        )
        self.assertIn(receipt["stage"], inactive_installation.STAGES)
        self.assertIn(
            receipt["decision"],
            {"refuse", inactive_installation.STRUCTURAL_DECISION},
        )
        for key in ("refusal_codes", "blockers"):
            self.assertIs(type(receipt[key]), list)
            self.assertTrue(all(type(item) is str for item in receipt[key]))
            self.assertEqual(len(receipt[key]), len(set(receipt[key])))
        self.assertIs(receipt["authorization_inferred"], False)
        for key in ("candidate_git_commit", "candidate_git_tree"):
            self.assertRegex(receipt[key], r"[0-9a-f]{40}\Z")
        for key in (
            "package_manifest_sha256",
            "observation_canonical_sha256",
        ):
            self.assertRegex(receipt[key], r"[0-9a-f]{64}\Z")
        prior_hash = receipt["prior_decision_receipt_sha256"]
        self.assertTrue(
            prior_hash is None
            or (
                type(prior_hash) is str
                and re.fullmatch(r"[0-9a-f]{64}", prior_hash)
            )
        )
        self.assertIs(type(receipt["receipt_chain"]), list)
        self.assertIs(type(receipt["exact_target_states"]), dict)
        self.assertIs(type(receipt["exact_source_cluster_states"]), dict)
        self.assertIs(type(receipt["exact_successor_store_states"]), dict)
        self.assertIs(
            type(receipt["phase8b_migration_execution_contract"]), dict
        )
        self.assertIs(type(receipt["fresh_store_counts"]), dict)
        self.assertIs(receipt["pilot_ever_started"], False)
        self.assertIs(type(receipt["evaluator_mutating_commands_executed"]), int)
        self.assertIs(type(receipt["evaluator_provider_calls"]), int)
        self.assertIs(receipt["evaluator_state_changed"], False)

    def test_evaluator_is_structural_only_and_chains_exact_receipts(self) -> None:
        verification = {"package_manifest_sha256": "a" * 64}
        with mock.patch.object(
            inactive_installation, "verify_package", return_value=verification
        ):
            initial = inactive_installation.evaluate_observation(
                self._observation("install_preflight"),
                expected_candidate_git_commit="b" * 40,
                expected_candidate_git_tree="c" * 40,
            )
            self.assertEqual(
                initial["decision"],
                "structurally_valid_candidate_not_authorization",
            )
            self.assertFalse(initial["authorization_inferred"])
            self.assertEqual(
                initial["phase8b_migration_execution_contract"],
                inactive_installation.PHASE8B_MIGRATION_EXECUTION_CONTRACT,
            )
            self.assertEqual(
                initial["schema_version"],
                "governed-memory-installation-decision-receipt-v2",
            )
            self.assertFalse(
                initial["phase8b_migration_execution_contract"][
                    "evaluator_verifies_execution"
                ]
            )
            self.assertEqual(
                initial["phase8b_migration_execution_contract"][
                    "source_postgresql_steps"
                ],
                [],
            )
            self.assertFalse(
                initial["phase8b_migration_execution_contract"][
                    "source_conversation_bridge_included"
                ]
            )
            self.assertIn(
                "separate_future_installation_approval_required",
                initial["blockers"],
            )
            rendered = json.dumps(initial, sort_keys=True)
            self.assertNotIn("allow_separate_execution", rendered)
            self.assertNotIn("verified_complete", rendered)

            with tempfile.TemporaryDirectory() as directory:
                prior_path = Path(directory) / "install-preflight.json"
                prior_bytes = (
                    json.dumps(initial, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode("ascii")
                prior_path.write_bytes(prior_bytes)
                prior_sha256 = self._canonical_sha256(initial)
                postflight = inactive_installation.evaluate_observation(
                    self._observation(
                        "install_postflight",
                        prior_receipt_sha256=prior_sha256,
                    ),
                    expected_candidate_git_commit="b" * 40,
                    expected_candidate_git_tree="c" * 40,
                    prior_receipt_paths=[prior_path],
                )
                self.assertEqual(
                    postflight["decision"],
                    "structurally_valid_candidate_not_authorization",
                )
                self.assertEqual(
                    postflight["receipt_chain"],
                    [
                        {
                            "stage": "install_preflight",
                            "receipt_sha256": prior_sha256,
                        }
                    ],
                )

                postflight_path = Path(directory) / "install-postflight.json"
                postflight_bytes = (
                    json.dumps(
                        postflight, sort_keys=True, separators=(",", ":")
                    )
                    + "\n"
                ).encode("ascii")
                postflight_path.write_bytes(postflight_bytes)
                postflight_sha256 = self._canonical_sha256(postflight)
                rollback = inactive_installation.evaluate_observation(
                    self._observation(
                        "rollback_preflight",
                        prior_receipt_sha256=postflight_sha256,
                    ),
                    expected_candidate_git_commit="b" * 40,
                    expected_candidate_git_tree="c" * 40,
                    prior_receipt_paths=[prior_path, postflight_path],
                )
                self.assertEqual(
                    [item["stage"] for item in rollback["receipt_chain"]],
                    ["install_preflight", "install_postflight"],
                )

                forged_postflight = dict(postflight)
                forged_postflight["prior_decision_receipt_sha256"] = "d" * 64
                forged_postflight["receipt_chain"] = [
                    {
                        "stage": "install_preflight",
                        "receipt_sha256": "d" * 64,
                    }
                ]
                forged_bytes = (
                    json.dumps(
                        forged_postflight,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("ascii")
                postflight_path.write_bytes(forged_bytes)
                forged_sha256 = self._canonical_sha256(forged_postflight)
                forged_chain = inactive_installation.evaluate_observation(
                    self._observation(
                        "rollback_preflight",
                        prior_receipt_sha256=forged_sha256,
                    ),
                    expected_candidate_git_commit="b" * 40,
                    expected_candidate_git_tree="c" * 40,
                    prior_receipt_paths=[prior_path, postflight_path],
                )
                self.assertEqual(forged_chain["decision"], "refuse")
                self.assertIn(
                    "prior_decision_receipt_invalid",
                    forged_chain["refusal_codes"],
                )

                missing_prior = inactive_installation.evaluate_observation(
                    self._observation("install_postflight"),
                    expected_candidate_git_commit="b" * 40,
                    expected_candidate_git_tree="c" * 40,
                )
                self.assertEqual(missing_prior["decision"], "refuse")
                self.assertIn(
                    "prior_decision_receipt_missing",
                    missing_prior["refusal_codes"],
                )

                wrong_tree = inactive_installation.evaluate_observation(
                    self._observation("install_preflight"),
                    expected_candidate_git_commit="b" * 40,
                    expected_candidate_git_tree="d" * 40,
                )
                self.assertEqual(wrong_tree["decision"], "refuse")
                self.assertIn(
                    "candidate_git_tree_mismatch",
                    wrong_tree["refusal_codes"],
                )

    def test_refusal_receipts_never_reflect_untrusted_observation_content(self) -> None:
        attacker = "ATTACKER_SECRET_MUST_NOT_ENTER_RECEIPT"
        verification = {"package_manifest_sha256": "a" * 64}

        bounded = self._observation("install_preflight")
        bounded["target_states"] = {"postgres_container": attacker}
        bounded["source_cluster_states"] = {"preparation_phase": attacker}
        bounded["successor_store_states"] = {"postgresql": attacker}
        bounded["successor_user_memory_row_count"] = attacker
        bounded["pilot_ever_started"] = attacker

        cyclic: dict[str, object] = {}
        cyclic["cycle"] = cyclic
        oversized = self._observation("install_preflight")
        oversized["stage"] = attacker
        oversized["target_states"] = {
            "payload": attacker * 10000,
            "nested": cyclic,
        }
        oversized["prior_decision_receipt_sha256"] = attacker

        malformed: object = [attacker, {"payload": [attacker]}]
        with mock.patch.object(
            inactive_installation, "verify_package", return_value=verification
        ):
            receipts = [
                inactive_installation.evaluate_observation(
                    document,
                    expected_candidate_git_commit="b" * 40,
                    expected_candidate_git_tree="c" * 40,
                )
                for document in (bounded, oversized, malformed)
            ]

        for receipt in receipts:
            with self.subTest(refusal_codes=receipt["refusal_codes"]):
                self.assertEqual(receipt["decision"], "refuse")
                self._assert_closed_receipt_shape(receipt)
                self.assertNotIn(
                    attacker,
                    json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                )
                stage = receipt["stage"]
                self.assertEqual(
                    receipt["exact_target_states"],
                    inactive_installation.TARGET_STATE_PROFILES[stage],
                )
                self.assertEqual(
                    receipt["exact_source_cluster_states"],
                    inactive_installation.SOURCE_CLUSTER_STATE_PROFILES[stage],
                )
                self.assertEqual(
                    receipt["exact_successor_store_states"],
                    inactive_installation.SUCCESSOR_STORE_STATE_PROFILES[stage],
                )
                self.assertEqual(
                    receipt["fresh_store_counts"],
                    inactive_installation.ZERO_FRESH_STORE_COUNTS,
                )

        self.assertIn(
            "exact_target_state_mismatch", receipts[0]["refusal_codes"]
        )
        self.assertIn(
            "source_cluster_state_mismatch", receipts[0]["refusal_codes"]
        )
        self.assertIn(
            "successor_store_state_mismatch", receipts[0]["refusal_codes"]
        )
        self.assertIn(
            "successor_user_memory_row_count_not_zero",
            receipts[0]["refusal_codes"],
        )
        self.assertIn("pilot_already_started", receipts[0]["refusal_codes"])
        self.assertEqual(
            receipts[1]["observation_canonical_sha256"],
            inactive_installation.INVALID_OBSERVATION_CANONICAL_SHA256,
        )
        self.assertIn(
            "observation_value_bounds_exceeded", receipts[1]["refusal_codes"]
        )
        self.assertIn("observation_stage_invalid", receipts[1]["refusal_codes"])
        self.assertIsNone(receipts[1]["prior_decision_receipt_sha256"])
        self.assertIn("observation_not_object", receipts[2]["refusal_codes"])
        self.assertIn("observation_shape_invalid", receipts[2]["refusal_codes"])

    def test_source_profiles_enforce_phase8b_zero_connection_boundary(self) -> None:
        expected = {
            "connection_count": 0,
            "catalog_read_count": 0,
            "application_row_read_count": 0,
            "write_count": 0,
            "preparation_phase": "8C_separate_authorization_required",
        }
        for stage, profile in (
            inactive_installation.SOURCE_CLUSTER_STATE_PROFILES.items()
        ):
            with self.subTest(stage=stage):
                self.assertEqual(profile, expected)

    def test_decision_receipt_v2_schema_matches_closed_runtime_shape(self) -> None:
        schema = json.loads(
            (INSTALLATION / "receipt.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            "governed-memory-installation-decision-receipt-v2",
        )
        self.assertEqual(
            set(schema["required"]),
            inactive_installation.DECISION_RECEIPT_KEYS,
        )
        self.assertEqual(
            set(schema["properties"]),
            inactive_installation.DECISION_RECEIPT_KEYS,
        )
        source = schema["properties"]["exact_source_cluster_states"]
        self.assertFalse(source["additionalProperties"])
        self.assertEqual(
            set(source["required"]),
            set(inactive_installation._PHASE8B_SOURCE_UNTOUCHED),
        )
        migration = schema["properties"][
            "phase8b_migration_execution_contract"
        ]
        self.assertEqual(
            set(migration["required"]),
            set(inactive_installation.PHASE8B_MIGRATION_EXECUTION_CONTRACT),
        )
        self.assertEqual(
            migration["properties"]["canonical_migrations"]["const"],
            inactive_installation.PHASE8B_MIGRATION_EXECUTION_CONTRACT[
                "canonical_migrations"
            ],
        )

    def test_controller_runner_is_package_artifact_59_and_tamper_fails(self) -> None:
        result = inactive_installation.verify_package()
        runner_relative = (
            "tools/governed_memory_validation/"
            "run_disposable_installation_controller.py"
        )
        runner = ROOT / runner_relative
        self.assertEqual(len(result["artifact_sha256"]), 59)
        self.assertEqual(
            result["artifact_sha256"][runner_relative],
            hashlib.sha256(runner.read_bytes()).hexdigest(),
        )

        original_read = inactive_installation._read_bytes

        def tampered_read(path: Path) -> bytes:
            content = original_read(path)
            if path.resolve() == runner.resolve():
                return content + b"\n# synthetic tamper\n"
            return content

        with mock.patch.object(
            inactive_installation,
            "_read_bytes",
            side_effect=tampered_read,
        ), self.assertRaisesRegex(
            inactive_installation.InstallationPackageError,
            "installation_artifact_hash_mismatch",
        ):
            inactive_installation.verify_package()

    def test_json_loader_and_manifest_paths_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            root = temporary_root / "repo"
            root.mkdir()
            duplicate = root / "duplicate.json"
            duplicate.write_text('{"key":1,"key":2}\n', encoding="utf-8")
            with self.assertRaisesRegex(
                inactive_installation.InstallationPackageError,
                "installation_artifact_duplicate_key",
            ):
                inactive_installation._load_json(duplicate)

            artifact = root / "artifact.json"
            artifact.write_text("{}\n", encoding="utf-8")
            outside = temporary_root / "outside.json"
            outside.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(inactive_installation, "ROOT", root):
                self.assertEqual(
                    inactive_installation._manifest_artifact_path(
                        "artifact.json"
                    ),
                    artifact.resolve(),
                )
                for relative in (
                    "../outside.json",
                    "/etc/passwd",
                    "nested//artifact.json",
                    "nested\\artifact.json",
                ):
                    with self.subTest(relative=relative):
                        with self.assertRaises(
                            inactive_installation.InstallationPackageError
                        ):
                            inactive_installation._manifest_artifact_path(relative)

    def test_phase8a_contract_is_exact_pinned_and_has_no_live_executor(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        plan = json.loads(
            (INSTALLATION / "controller_plan.json").read_text(
                encoding="utf-8"
            )
        )
        compose = COMPOSE.read_text(encoding="utf-8")

        self.assertEqual(
            contract["state"],
            "phase8a_controller_packaged_no_live_executor_not_installed_not_authorized",
        )
        self.assertFalse(contract["tooling"]["live_backend_packaged"])
        self.assertFalse(contract["tooling"]["live_install_cli_exposed"])
        self.assertFalse(contract["tooling"]["live_rollback_cli_exposed"])
        self.assertFalse(contract["tooling"]["executes_live_commands"])
        self.assertFalse(contract["tooling"]["creates_live_resources"])
        self.assertFalse(
            contract["tooling"]["can_infer_installation_authority"]
        )
        self.assertFalse(contract["evaluator_state_changed"])
        split = contract["phase_split"]
        self.assertEqual(split["phase8b_source_postgresql_connection_count"], 0)
        self.assertEqual(split["phase8b_source_application_row_read_count"], 0)
        self.assertEqual(split["phase8b_source_application_row_write_count"], 0)
        self.assertEqual(
            contract["source_cluster_policy"]["source_roles_and_bridge_deferred_to"],
            "phase8c",
        )
        self.assertFalse(
            contract["source_cluster_policy"]["phase8b_connection_allowed"]
        )
        self.assertIn(
            "source_logging_parameter_remediation_not_authorized_or_applied",
            contract["phase8c_source_preparation_blockers"],
        )
        controller = contract["controller"]
        self.assertEqual(
            controller["linux_backend_state"],
            "hard_disabled_no_live_execution_surface",
        )
        self.assertEqual(
            controller["phase8a_forbidden_cli_commands"],
            ["install", "rollback", "activate", "cleanup"],
        )
        self.assertEqual(
            controller["phase8a_cli_commands"],
            ["verify-package", "evaluate-observation"],
        )
        self.assertEqual(
            controller["phase8a_disposable_proof_entrypoint"],
            "tools/governed_memory_validation/"
            "run_disposable_installation_controller.py",
        )
        self.assertFalse(
            contract["authority"][
                "phase8a_approval_is_phase8b_execution_authority"
            ]
        )
        credential_policy = contract["dormant_install_credential_policy"]
        self.assertTrue(
            credential_policy["fresh_postgresql_bootstrap_password_required"]
        )
        self.assertTrue(credential_policy["fresh_qdrant_api_key_required"])
        for key in (
            "runtime_database_login_credentials_created",
            "provider_credentials_created",
            "supabase_credentials_created",
            "service_token_created",
            "pilot_marker_values_created",
        ):
            self.assertFalse(credential_policy[key], key)
        self.assertFalse(
            contract["legacy_secret_transition_policy"][
                "value_may_be_read_copied_hashed_or_logged"
            ]
        )
        self.assertFalse(
            contract["legacy_secret_transition_policy"][
                "restore_to_unit_loadable_path_during_compensation"
            ]
        )
        self.assertFalse(
            contract["legacy_secret_transition_policy"][
                "same_filesystem_preflight_adapter_packaged"
            ]
        )
        self.assertFalse(
            controller["external_journal_seal_anchor_packaged"]
        )
        self.assertFalse(
            controller["canonical_global_execution_lock_packaged"]
        )
        self.assertFalse(controller["exact_live_probe_adapter_packaged"])
        authority = contract["authority"]
        self.assertTrue(authority["signature_scope_verifier_packaged"])
        self.assertFalse(authority["trusted_clock_adapter_packaged"])
        self.assertFalse(
            authority["atomic_persistent_nonce_claim_adapter_packaged"]
        )
        self.assertFalse(
            authority["valid_verification_result_is_execution_capability"]
        )
        self.assertFalse(
            contract["rollback_policy"][
                "canonical_cluster_rollback_disposable_postgresql_executed"
            ]
        )
        self.assertIn(
            "runtime_wheel_and_offline_dependency_wheelhouse_not_packaged",
            contract["phase8b_installation_blockers"],
        )
        required_phase8b_blockers = {
            "trusted_clock_and_atomic_single_use_nonce_claim_not_packaged",
            "canonical_global_execution_lock_not_packaged",
            "external_journal_seal_anchor_not_packaged",
            "exact_live_probe_adapter_not_packaged",
            "same_filesystem_quarantine_preflight_adapter_not_packaged",
            "canonical_cluster_rollback_not_disposable_postgresql_executed",
            "linux_execution_backend_hard_disabled",
        }
        self.assertLessEqual(
            required_phase8b_blockers,
            set(contract["phase8b_installation_blockers"]),
        )
        self.assertEqual(
            {
                entry["typed_blocker"]
                for entry in plan[
                    "required_adapter_capabilities_without_packaged_artifacts"
                ]
            },
            {
                "store_supervisor_artifact_not_packaged",
                "encrypted_backup_restore_adapter_artifact_not_packaged",
                "trusted_clock_and_atomic_single_use_nonce_claim_not_packaged",
                "canonical_global_execution_lock_not_packaged",
                "external_journal_seal_anchor_not_packaged",
                "exact_live_probe_adapter_not_packaged",
                "same_filesystem_quarantine_preflight_adapter_not_packaged",
                "canonical_cluster_rollback_not_disposable_postgresql_executed",
                "linux_execution_backend_hard_disabled",
            },
        )
        self.assertFalse(
            contract["images"]["postgresql"]["installation_authorized"]
        )
        self.assertEqual(
            contract["images"]["postgresql"]["reference"], POSTGRES_IMAGE
        )
        self.assertEqual(contract["images"]["qdrant"]["reference"], QDRANT_IMAGE)
        self.assertFalse(
            contract["images"]["qdrant"][
                "approved_for_persistent_installation"
            ]
        )
        self.assertEqual(plan["exact_targets"], contract["exact_targets"])
        self.assertEqual(
            set(plan["exact_targets"]),
            {
                "postgres_container",
                "qdrant_container",
                "postgres_volume",
                "qdrant_volume",
                "network",
                "database",
                "collection",
                "alias",
                "install_root",
                "environment_root",
                "runtime_environment_root",
                "state_root",
                "legacy_secret_quarantine_path_template",
                "backup_root",
                "http_unit",
                "worker_unit",
                "store_supervisor_unit",
            },
        )
        for stage, profile in inactive_installation.TARGET_STATE_PROFILES.items():
            with self.subTest(target_profile_stage=stage):
                self.assertEqual(set(profile), set(plan["exact_targets"]))
        rollback_postflight = inactive_installation.TARGET_STATE_PROFILES[
            "rollback_postflight"
        ]
        for retained_root in (
            "install_root",
            "environment_root",
            "runtime_environment_root",
            "state_root",
            "backup_root",
            "legacy_secret_quarantine_path_template",
        ):
            self.assertTrue(
                rollback_postflight[retained_root].startswith("retained_exact_"),
                retained_root,
            )
        self.assertEqual(
            inactive_installation.UNIT_STATE_PROFILES["install_postflight"],
            {
                "http": "installed_disabled_inactive",
                "worker": "installed_disabled_inactive",
                "store_supervisor": "installed_enabled_active_store_only",
            },
        )
        self.assertEqual(
            inactive_installation.UNIT_STATE_PROFILES["rollback_postflight"],
            {
                "http": "absent",
                "worker": "absent",
                "store_supervisor": "absent",
            },
        )
        self.assertRegex(
            self._canonical_sha256(plan["exact_targets"]),
            r"[0-9a-f]{64}\Z",
        )
        self.assertEqual(
            contract["images"]["qdrant"]["typed_blocker"],
            "persistent_qdrant_digest_not_authorized",
        )
        self.assertIn(POSTGRES_IMAGE, compose)
        self.assertIn(QDRANT_IMAGE, compose)
        self.assertNotIn(":latest", compose)
        self.assertIn(
            "x-governed-memory-scope: "
            "persistent-installation-candidate-inactive",
            compose,
        )
        self.assertIn(
            "blocked-persistent-qdrant-digest-not-authorized", compose
        )
        self.assertIn('restart: "no"', compose)
        self.assertNotIn("/opt/chat-memory", compose)

    def test_controller_plan_and_cluster_rollback_are_closed_and_exact(self) -> None:
        plan = json.loads(
            (INSTALLATION / "controller_plan.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            tuple(step["id"] for step in plan["install_steps"]),
            inactive_installation.PHASE8A_INSTALL_STEPS,
        )
        self.assertEqual(
            tuple(step["id"] for step in plan["rollback_steps"]),
            inactive_installation.PHASE8A_ROLLBACK_STEPS,
        )
        i29 = next(
            step
            for step in plan["install_steps"]
            if step["id"] == "I29_SEAL_INACTIVE_POSTFLIGHT"
        )
        self.assertEqual(
            i29["artifact_refs"],
            [
                "ops/governed_memory/installation/authority/"
                "installation_execution_receipt.schema.json",
                "ops/governed_memory/installation/receipt.schema.json",
            ],
        )
        rendered_plan = json.dumps(
            plan, sort_keys=True, separators=(",", ":")
        )
        for forbidden in (
            "0002_conversation_bridge",
            "source_cluster_roles.pgsql",
        ):
            self.assertNotIn(forbidden, rendered_plan.lower())
        self.assertEqual(plan["scope"]["supabase_steps"], [])
        self.assertEqual(plan["scope"]["provider_steps"], [])
        self.assertFalse(
            plan["authority_boundary"]["phase8a_makes_provider_calls"]
        )
        blockers = {
            item["typed_blocker"]
            for item in plan[
                "required_adapter_capabilities_without_packaged_artifacts"
            ]
        }
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(
            blockers,
            {
                "store_supervisor_artifact_not_packaged",
                "encrypted_backup_restore_adapter_artifact_not_packaged",
                "trusted_clock_and_atomic_single_use_nonce_claim_not_packaged",
                "canonical_global_execution_lock_not_packaged",
                "external_journal_seal_anchor_not_packaged",
                "exact_live_probe_adapter_not_packaged",
                "same_filesystem_quarantine_preflight_adapter_not_packaged",
                "canonical_cluster_rollback_not_disposable_postgresql_executed",
                "linux_execution_backend_hard_disabled",
            },
        )
        self.assertLessEqual(
            blockers,
            set(contract["phase8b_installation_blockers"]),
        )

        transition = contract["legacy_secret_transition_policy"]
        quarantine_template = (
            "/etc/governed-memory/"
            ".legacy-pilot.quarantine-{authorization_nonce_sha256}"
        )
        self.assertEqual(
            transition["source_path"], "/etc/governed-memory/pilot.env"
        )
        self.assertEqual(
            transition["destination_path_template"], quarantine_template
        )
        self.assertEqual(
            plan["exact_targets"][
                "legacy_secret_quarantine_path_template"
            ],
            quarantine_template,
        )
        self.assertEqual(
            Path(transition["source_path"]).parent,
            Path(transition["destination_path_template"]).parent,
        )
        self.assertEqual(
            transition["source_and_destination_parent"],
            "/etc/governed-memory",
        )
        self.assertTrue(transition["parent_must_preexist_i04"])
        self.assertEqual(
            transition["destination_nonce_derivation"],
            "lowercase_hex_sha256_of_exact_utf8_authorization_nonce",
        )
        self.assertEqual(
            transition["destination_nonce_sha256_pattern"],
            "[0-9a-f]{64}",
        )
        self.assertTrue(transition["destination_must_not_preexist"])
        self.assertFalse(transition["destination_is_unit_loadable"])
        self.assertFalse(quarantine_template.endswith(".env"))
        self.assertLess(
            inactive_installation.PHASE8A_INSTALL_STEPS.index(
                "I04_QUARANTINE_LEGACY_SECRET"
            ),
            inactive_installation.PHASE8A_INSTALL_STEPS.index(
                "I06_CREATE_OWNED_ROOTS"
            ),
        )

        lifecycle = contract["directory_lifecycle"]
        self.assertEqual(
            plan["directory_lifecycle"]["create_or_verify_step"],
            lifecycle["create_or_verify_step"],
        )
        expected_roots = {
            "/opt/governed-memory",
            "/etc/governed-memory",
            "/etc/governed-memory/runtime",
            "/var/lib/governed-memory",
            "/var/backups/governed-memory",
        }
        self.assertEqual(
            {
                profile["path"]
                for profile in lifecycle["exact_roots"].values()
            },
            expected_roots,
        )
        self.assertTrue(
            all(
                profile["empty_rollback_disposition"].startswith(
                    "retain_exact_root"
                )
                for profile in lifecycle["exact_roots"].values()
            )
        )
        self.assertEqual(
            set(
                plan["directory_lifecycle"][
                    "roots_retained_after_empty_rollback"
                ]
            ),
            expected_roots,
        )
        self.assertFalse(lifecycle["empty_rollback_removes_named_roots"])
        self.assertFalse(
            plan["directory_lifecycle"][
                "empty_rollback_removes_named_roots"
            ]
        )
        backup = contract["postgres_backup_policy"]
        self.assertEqual(
            backup["backup_root_create_or_verify_step"],
            "I06_CREATE_OWNED_ROOTS",
        )
        self.assertTrue(backup["backup_root_retained_after_empty_rollback"])
        self.assertIn(
            contract["exact_targets"]["backup_root"],
            plan["rollback_retained_resources"],
        )
        rollback_effects = {
            step["id"]: step["effect"] for step in plan["rollback_steps"]
        }
        self.assertIn(
            "retaining_/var/backups/governed-memory",
            rollback_effects["R16_REMOVE_BACKUP_ATTEMPT_ARTIFACTS"],
        )
        self.assertIn(
            "retain_the_named_install_environment_runtime_state_and_backup_roots",
            rollback_effects[
                "R19_REMOVE_ATTEMPT_CHILDREN_RETAIN_NAMED_ROOTS"
            ],
        )

        rollback_sql = (
            POSTGRES / "canonical_cluster_rollback.pgsql.in"
        ).read_text(encoding="utf-8")
        rollback_upper = rollback_sql.upper()
        self.assertNotIn("CASCADE", rollback_upper)
        self.assertNotIn("DROP OWNED", rollback_upper)
        self.assertEqual(
            rollback_upper.count("DROP DATABASE GOVERNED_MEMORY;"), 1
        )
        self.assertIn(
            "GOVERNED_MEMORY_ROLES_ONLY_RECOVERY_PREFLIGHT",
            rollback_upper,
        )
        self.assertIn("PG_CATALOG.PG_SHDEPEND", rollback_upper)
        self.assertIn("BEGIN;", rollback_upper)
        self.assertIn("COMMIT;", rollback_upper)

    def test_secret_templates_contain_names_and_no_values(self) -> None:
        expected = {
            "bootstrap.env.example": {
                "GOVERNED_MEMORY_BOOTSTRAP_PASSWORD",
                "GOVERNED_MEMORY_QDRANT_API_KEY",
            },
            "http.env.example": {
                "GOVERNED_MEMORY_HTTP_MODE",
                "GOVERNED_MEMORY_POSTGRES_DSN",
                "GOVERNED_MEMORY_CONVERSATION_POSTGRES_DSN",
                "GOVERNED_MEMORY_CONVERSATION_BRIDGE_CATALOG_SHA256",
                "GOVERNED_MEMORY_SUPABASE_ISSUER",
                "GOVERNED_MEMORY_SUPABASE_JWKS_URL",
                "GOVERNED_MEMORY_SUPABASE_API_KEY",
                "GOVERNED_MEMORY_SERVICE_TOKEN",
            },
            "worker.env.example": {
                "GOVERNED_MEMORY_WORKER_MODE",
                "GOVERNED_MEMORY_EXCLUSIVE_MODE",
                "GOVERNED_MEMORY_POSTGRES_DSN",
                "GOVERNED_MEMORY_CONVERSATION_POSTGRES_DSN",
                "GOVERNED_MEMORY_OPENAI_API_KEY",
                "GOVERNED_MEMORY_OPENAI_EXTRACTION_MODEL",
                "GOVERNED_MEMORY_QDRANT_URL",
                "GOVERNED_MEMORY_QDRANT_API_KEY",
            },
            "pilot.env.example": {
                "GOVERNED_MEMORY_EXPECTED_PILOT_ID",
                "GOVERNED_MEMORY_EXPECTED_PILOT_CONTRACT_SHA256",
                "GOVERNED_MEMORY_EXPECTED_AUTHORIZATION_RECEIPT_SHA256",
            },
        }
        self.assertEqual({path.name for path in SECRETS.iterdir()}, set(expected))
        for name, names in expected.items():
            assignments = []
            for line in (SECRETS / name).read_text(encoding="utf-8").splitlines():
                if not line or line.startswith("#"):
                    continue
                match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
                self.assertIsNotNone(match, line)
                assert match is not None
                self.assertEqual(match.group(2), "", line)
                assignments.append(match.group(1))
            self.assertEqual(set(assignments), names)
            self.assertEqual(len(assignments), len(names))

    def test_role_bootstraps_are_split_and_source_is_narrowly_additive(self) -> None:
        compose = COMPOSE.read_text(encoding="utf-8")
        canonical = (POSTGRES / "canonical_cluster.pgsql.in").read_text(
            encoding="utf-8"
        )
        source = (POSTGRES / "source_cluster_roles.pgsql.in").read_text(
            encoding="utf-8"
        )
        roles_preflight = (
            ROOT / "governed-memory-migrations" / "roles_preflight.pgsql"
        ).read_text(encoding="utf-8")
        for role in (
            "governed_memory_owner",
            "governed_memory_api",
            "governed_memory_worker",
            "memory_ingest_writer",
            "memory_erasure_requester",
        ):
            self.assertIn(f"CREATE ROLE {role}", canonical)
        canonical_postflight = canonical.split("DO $role_postflight$", 1)[1]
        for role in (
            "governed_memory_owner",
            "governed_memory_api",
            "governed_memory_worker",
            "memory_ingest_writer",
            "memory_erasure_requester",
        ):
            with self.subTest(canonical_postflight_role=role):
                self.assertIn(f"('{role}'::text)", canonical_postflight)
        self.assertIn(
            "NOT role.rolcanlogin AND NOT role.rolinherit",
            canonical_postflight,
        )
        self.assertIn(
            "FROM pg_catalog.pg_auth_members AS membership",
            canonical_postflight,
        )
        self.assertIn(
            "granted_role.rolname = 'governed_memory_owner'",
            canonical_postflight,
        )
        self.assertIn(
            "member_role.rolname = 'governed_memory_bootstrap'",
            canonical_postflight,
        )
        canonical_principals = {
            "governed_memory_bootstrap",
            "governed_memory_owner",
            "governed_memory_api",
            "governed_memory_worker",
            "memory_ingest_writer",
            "memory_erasure_requester",
        }
        runtime_principals = canonical_principals - {
            "governed_memory_bootstrap",
            "governed_memory_owner",
        }

        def membership_role_sets(sql: str) -> list[tuple[set[str], set[str]]]:
            return [
                (
                    set(re.findall(r"'([^']+)'", granted)),
                    set(re.findall(r"'([^']+)'", member)),
                )
                for granted, member in re.findall(
                    r"WHERE\s+(?:\(\s*)?granted_role\.rolname IN \((.*?)\)"
                    r"\s+OR member_role\.rolname IN \((.*?)\)",
                    sql,
                    flags=re.DOTALL,
                )
            ]

        for label, sql in (
            ("canonical bootstrap", canonical_postflight),
            ("canonical roles preflight", roles_preflight),
        ):
            matches = membership_role_sets(sql)
            with self.subTest(label=label):
                self.assertEqual(matches, [(canonical_principals, canonical_principals)])
                self.assertIn(
                    "granted_role.rolname = 'governed_memory_owner'",
                    sql,
                )
                self.assertIn(
                    "member_role.rolname = 'governed_memory_bootstrap'",
                    sql,
                )
        self.assertIn("CREATE DATABASE governed_memory", canonical)
        for setting in (
            "log_statement=none",
            "log_duration=off",
            "log_min_duration_statement=-1",
            "log_min_duration_sample=-1",
            "log_transaction_sample_rate=0",
            "log_parameter_max_length=0",
            "log_parameter_max_length_on_error=0",
        ):
            with self.subTest(compose_logging_setting=setting):
                self.assertIn(f"- {setting}", compose)
        self.assertIn("current_setting('log_statement') = 'none'", canonical)
        self.assertIn("current_setting('log_duration') = 'off'", canonical)
        self.assertIn("shared_preload_libraries", canonical)
        self.assertNotIn(
            "current_setting('shared_preload_libraries')", roles_preflight
        )
        self.assertIn(
            "privileged canonical cluster bootstrap verifies",
            roles_preflight,
        )
        self.assertIn("PostgreSQL 16", roles_preflight)
        self.assertIn("auto_explain.log_parameter_max_length", canonical)
        self.assertNotIn("PASSWORD", canonical)
        self.assertNotIn("SCRAM-SHA-256", canonical)
        self.assertRegex(
            canonical,
            r"CREATE ROLE governed_memory_api\n  NOLOGIN NOINHERIT",
        )
        self.assertRegex(
            canonical,
            r"CREATE ROLE governed_memory_worker\n  NOLOGIN NOINHERIT",
        )

        for role in (
            "governed_memory_api",
            "governed_memory_worker",
            "memory_ingest_writer",
            "memory_erasure_requester",
        ):
            self.assertIn(f"CREATE ROLE {role}", source)
        self.assertNotIn("CREATE DATABASE", source)
        self.assertNotIn("CREATE SCHEMA", source)
        self.assertNotRegex(source, r"(?im)^GRANT\s+memory_erasure_requester")
        self.assertNotRegex(source, r"(?im)^GRANT\s+memory_ingest_writer")
        self.assertNotIn("PASSWORD", source)
        self.assertNotIn("SCRAM-SHA-256", source)
        self.assertIn("('governed_memory_api'::text, false)", source)
        self.assertIn("('governed_memory_worker'::text, false)", source)
        self.assertEqual(source.count("BEGIN;"), 1)
        self.assertEqual(source.count("COMMIT;"), 1)
        self.assertLess(source.index("BEGIN;"), source.index("CREATE ROLE"))
        self.assertGreater(source.index("COMMIT;"), source.rindex("$catalog_postflight$"))
        self.assertIn("current_database() <> 'memory'", source)
        self.assertIn("current_user <> 'sage'", source)
        self.assertIn("relation.relforcerowsecurity", source)
        self.assertIn("runtime_role_count NOT IN (0, 4)", source)
        postflight = source.split("DO $catalog_postflight$", 1)[1]
        self.assertIn("rolname = 'brains_app'", postflight)
        self.assertIn("rolcanlogin AND rolinherit", postflight)
        self.assertIn("FROM pg_catalog.pg_auth_members AS membership", postflight)
        source_membership_sets = membership_role_sets(source)
        self.assertEqual(len(source_membership_sets), 2)
        for direction_sets in source_membership_sets:
            self.assertEqual(direction_sets, (runtime_principals, runtime_principals))
        for role in runtime_principals:
            with self.subTest(source_runtime_role=role):
                self.assertTrue(
                    any(role in granted for granted, _ in source_membership_sets)
                )
                self.assertTrue(
                    any(role in member for _, member in source_membership_sets)
                )
        self.assertIn("log_parameter_max_length_on_error", source)
        self.assertIn("current_setting('log_duration') <> 'off'", source)
        self.assertRegex(
            source,
            r"current_setting\(\s*'log_parameter_max_length'\s*\)"
            r"::integer\s*<>\s*0",
        )
        self.assertNotIn("NOT IN (-1, 0)", source)
        self.assertIn("log_min_duration_statement", source)
        self.assertIn("log_min_duration_sample", source)
        self.assertIn("log_transaction_sample_rate", source)
        self.assertIn("shared_preload_libraries", source)
        self.assertIn("pg_catalog.string_to_array(", source)
        self.assertIn("pg_catalog.btrim(configured.library_name)", source)
        self.assertNotIn(r"\\s*pgaudit", source)
        self.assertIn("auto_explain.log_parameter_max_length", source)
        self.assertFalse(
            (POSTGRES / "canonical_bootstrap_finalize.pgsql").exists()
        )

    def test_service_account_is_unprivileged_and_not_created(self) -> None:
        account = json.loads(
            (INSTALLATION / "service-account.json").read_text(encoding="utf-8")
        )
        self.assertEqual(account["state"], "candidate_only_account_not_created")
        self.assertEqual(account["user"], "governed-memory")
        self.assertEqual(account["shell"], "/usr/sbin/nologin")
        self.assertEqual(account["supplementary_groups"], [])
        self.assertFalse(account["docker_group_member"])
        self.assertFalse(account["sudo_rule"])
        self.assertEqual(
            account["runtime_environment_root"],
            "/etc/governed-memory/runtime",
        )
        self.assertEqual(
            account["runtime_environment_root_owner"],
            "root:governed-memory",
        )
        self.assertEqual(account["runtime_environment_root_mode"], "0750")
        profiles = account["secret_file_profiles"]
        self.assertEqual(
            profiles["bootstrap.env"],
            {
                "path": "/etc/governed-memory/bootstrap.env",
                "owner": "root:root",
                "mode": "0600",
                "service_account_direct_read": False,
            },
        )
        for runtime_file in ("http.env", "worker.env", "pilot.env"):
            self.assertEqual(
                profiles[runtime_file]["path"],
                f"/etc/governed-memory/runtime/{runtime_file}",
            )
            self.assertEqual(
                profiles[runtime_file]["owner"],
                "root:governed-memory",
            )
            self.assertEqual(profiles[runtime_file]["mode"], "0640")
        self.assertFalse(account["evaluator_state_changed"])

    def test_successor_units_load_only_runtime_environment_files(self) -> None:
        expected = {
            "governed-memory-http.service.in": {
                "/etc/governed-memory/runtime/http.env"
            },
            "governed-memory-worker.service.in": {
                "/etc/governed-memory/runtime/worker.env",
                "/etc/governed-memory/runtime/pilot.env",
            },
        }
        unit_root = ROOT / "ops" / "governed_memory" / "systemd"
        for unit_name, expected_paths in expected.items():
            text = (unit_root / unit_name).read_text(encoding="utf-8")
            loaded_paths = {
                line.split("=", 1)[1]
                for line in text.splitlines()
                if line.startswith("EnvironmentFile=")
            }
            conditions = {
                line.split("=", 1)[1]
                for line in text.splitlines()
                if line.startswith("ConditionPathExists=")
            }
            self.assertEqual(loaded_paths, expected_paths, unit_name)
            self.assertEqual(conditions, expected_paths, unit_name)
            for path in loaded_paths | conditions:
                self.assertTrue(
                    path.startswith("/etc/governed-memory/runtime/"), path
                )
            self.assertNotIn(
                "EnvironmentFile=/etc/governed-memory/pilot.env", text
            )
            self.assertNotIn(
                "ConditionPathExists=/etc/governed-memory/pilot.env", text
            )

    def test_cli_exit_codes_never_authorize_an_observation(self) -> None:
        output = io.StringIO()
        with mock.patch.object(
            inactive_installation,
            "verify_package",
            return_value={"schema_version": "verified"},
        ), redirect_stdout(output):
            self.assertEqual(inactive_installation.main(["verify-package"]), 0)

        structurally_valid = {
            "decision": "structurally_valid_candidate_not_authorization"
        }
        with mock.patch.object(
            inactive_installation,
            "_load_json",
            return_value={},
        ), mock.patch.object(
            inactive_installation,
            "evaluate_observation",
            return_value=structurally_valid,
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(
                inactive_installation.main(
                    [
                        "evaluate-observation",
                        "observation.json",
                        "--expected-candidate-git-commit",
                        "b" * 40,
                        "--expected-candidate-git-tree",
                        "c" * 40,
                    ]
                ),
                3,
            )

        with mock.patch.object(
            inactive_installation,
            "_load_json",
            return_value={},
        ), mock.patch.object(
            inactive_installation,
            "evaluate_observation",
            return_value={"decision": "refuse"},
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(
                inactive_installation.main(
                    [
                        "evaluate-observation",
                        "observation.json",
                        "--expected-candidate-git-commit",
                        "b" * 40,
                        "--expected-candidate-git-tree",
                        "c" * 40,
                    ]
                ),
                2,
            )

    def test_quiescence_manifest_names_exact_memory_writers(self) -> None:
        manifest = json.loads(
            (INSTALLATION / "legacy_quiescence_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        timers = {item["timer"] for item in manifest["memory_timer_stop_set"]}
        self.assertEqual(
            timers,
            {
                "memory-v1-deferred-reconciliation-scan.timer",
                "memory-v1-evidence-intake-dispatcher.timer",
                "memory-v1-projection.timer",
                "memory-v1-v5-2-local-packet-router.timer",
                "memory-v1-v5-chat-capture.timer",
                "memory-v1-v5-local-auto-resolution.timer",
                "memory-v1-v5-local-auto-stage.timer",
                "memory-v1-v5-local-claim-projection.timer",
                "memory-v1-v5-local-legacy-reintake-audit.timer",
                "memory-v1-v5-local-packet-router.timer",
            },
        )
        preserve = manifest["inventory_preserve_set"]
        self.assertEqual(len(preserve), 1)
        self.assertEqual(preserve[0]["timer"], "chat-memory-git-sync.timer")
        self.assertEqual(preserve[0]["action"], "preserve_unless_separately_authorized")
        self.assertEqual(
            manifest["preserved_runtime"]["unit"], "brains.service"
        )
        self.assertEqual(
            manifest["preserved_runtime"]["action"],
            "preserve_running_for_chat_and_lifeswitch",
        )
        self.assertFalse(
            manifest["future_quiescence_requirements"][
                "timer_and_cron_quiescence_alone_establishes_exclusivity"
            ]
        )
        self.assertTrue(
            manifest["future_quiescence_requirements"][
                "zero_legacy_memory_readers_required"
            ]
        )
        cron = manifest["cron_stop_set"]
        self.assertEqual(len(cron), 1)
        self.assertEqual(cron[0]["owner"], "ubuntu")
        self.assertEqual(cron[0]["executable"], "/opt/chat-memory/eval_all_users.sh")
        self.assertFalse(manifest["quiescence_performed"])
        self.assertFalse(manifest["production_state_changed"])


if __name__ == "__main__":
    unittest.main()

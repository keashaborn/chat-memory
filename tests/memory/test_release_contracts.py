from __future__ import annotations

import base64
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


class ReleaseArtifactTests(unittest.TestCase):
    def test_machine_readable_artifacts_verify_offline(self) -> None:
        result = verify_candidate_artifacts()
        self.assertEqual(
            result["schema_version"],
            "governed-memory-release-artifact-verification-v1",
        )
        self.assertEqual(result["external_calls"], 0)
        self.assertFalse(result["production_state_changed"])
        self.assertEqual(len(result["artifact_sha256"]), 11)
        self.assertIn(
            "ops/governed_memory/systemd/governed-memory-worker.service.in",
            result["artifact_sha256"],
        )
        self.assertIn(
            "ops/governed_memory/runtime_build_receipt.json",
            result["artifact_sha256"],
        )
        self.assertIn(
            "ops/governed_memory/runtime_manifest.json",
            result["artifact_sha256"],
        )
        self.assertIn(
            "ops/governed_memory/runtime-requirements.lock",
            result["artifact_sha256"],
        )
        self.assertIn(
            "ops/governed_memory/build-requirements.lock",
            result["artifact_sha256"],
        )
        self.assertIn(
            "tools/governed_memory_validation/runtime_packages.json",
            result["artifact_sha256"],
        )

    def test_runtime_manifest_or_lock_drift_is_rejected(self) -> None:
        original = json.loads(
            (OPS / "runtime_manifest.json").read_text(encoding="utf-8")
        )
        variants = []
        redirected = json.loads(json.dumps(original))
        redirected["validation_runtime"]["current_build_receipt"] = (
            "ops/governed_memory/history/phase5/runtime_build_receipt.json"
        )
        variants.append(redirected)
        activated = json.loads(json.dumps(original))
        activated["activation"]["production_authorized"] = True
        activated["activation"]["running_services"] = [
            "governed-memory-worker.service"
        ]
        activated["release_guard"]["create_allowed"] = True
        activated["production_state_changed"] = True
        variants.append(activated)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, variant in enumerate(variants):
                path = root / f"runtime-manifest-{index}.json"
                path.write_text(
                    json.dumps(variant, sort_keys=True),
                    encoding="utf-8",
                )
                with self.subTest(index=index), mock.patch.object(
                    release_guard, "RUNTIME_MANIFEST", path
                ):
                    with self.assertRaisesRegex(
                        ReleaseGuardError,
                        "release_runtime_contract_invalid",
                    ):
                        verify_candidate_artifacts()

            lock = root / "runtime-requirements.lock"
            lock.write_text("drift\n", encoding="utf-8")
            with mock.patch.object(release_guard, "RUNTIME_LOCK", lock):
                with self.assertRaisesRegex(
                    ReleaseGuardError,
                    "release_runtime_dependency_invalid",
                ):
                    verify_candidate_artifacts()

    def test_historical_or_semantically_malformed_runtime_receipt_is_rejected(
        self,
    ) -> None:
        historical = OPS / "history" / "phase5" / "runtime_build_receipt.json"
        with mock.patch.object(
            release_guard, "RUNTIME_BUILD_RECEIPT", historical
        ):
            with self.assertRaisesRegex(
                ReleaseGuardError,
                "release_runtime_contract_invalid",
            ):
                verify_candidate_artifacts()

        original_receipt = json.loads(
            (OPS / "runtime_build_receipt.json").read_text(encoding="ascii")
        )
        original_manifest = json.loads(
            (OPS / "runtime_manifest.json").read_text(encoding="utf-8")
        )
        variants: list[tuple[str, dict[str, object]]] = []
        for label, key, value in (
            ("python", "python_version", "3.12.2"),
            ("platform", "platform", "linux_aarch64"),
            ("runtime_lock", "runtime_lock", "history/phase5/runtime.lock"),
            ("build_lock", "build_lock", "history/phase5/build.lock"),
            (
                "project",
                "project_distribution",
                {"name": "legacy-memory", "version": "0.0.0"},
            ),
        ):
            variant = json.loads(json.dumps(original_receipt))
            variant[key] = value
            variants.append((label, variant))
        packages = json.loads(json.dumps(original_receipt))
        packages["runtime_packages"]["fastapi"] = "0.1.0"
        variants.append(("packages", packages))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for label, receipt in variants:
                receipt_path = root / f"runtime-receipt-{label}.json"
                receipt_path.write_text(
                    json.dumps(receipt, sort_keys=True),
                    encoding="ascii",
                )
                receipt_sha256 = hashlib.sha256(
                    receipt_path.read_bytes()
                ).hexdigest()
                manifest = json.loads(json.dumps(original_manifest))
                manifest["validation_runtime"][
                    "current_build_receipt_sha256"
                ] = receipt_sha256
                manifest_path = root / f"runtime-manifest-{label}.json"
                manifest_path.write_text(
                    json.dumps(manifest, sort_keys=True),
                    encoding="utf-8",
                )
                manifest_sha256 = hashlib.sha256(
                    manifest_path.read_bytes()
                ).hexdigest()
                patches = (
                    mock.patch.object(
                        release_guard,
                        "RUNTIME_BUILD_RECEIPT",
                        receipt_path,
                    ),
                    mock.patch.object(
                        release_guard,
                        "EXPECTED_RUNTIME_RECEIPT_SHA256",
                        receipt_sha256,
                    ),
                    mock.patch.object(
                        release_guard,
                        "RUNTIME_MANIFEST",
                        manifest_path,
                    ),
                    mock.patch.object(
                        release_guard,
                        "EXPECTED_RUNTIME_MANIFEST_SHA256",
                        manifest_sha256,
                    ),
                )
                with self.subTest(label=label), patches[0], patches[1], patches[
                    2
                ], patches[3]:
                    with self.assertRaisesRegex(
                        ReleaseGuardError,
                        "release_runtime_contract_invalid",
                    ):
                        verify_candidate_artifacts()

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
                CandidateBuildError,
                "candidate_source_inventory_invalid",
            ):
                _package_source_tree_sha256(package)
            (package / "unexpected.txt").unlink()

            first_asset.unlink()
            with self.assertRaisesRegex(
                CandidateBuildError,
                "candidate_source_inventory_invalid",
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
                CandidateBuildError,
                "candidate_runtime_wheelhouse_invalid",
            ):
                _verify_runtime_wheelhouse(wheelhouse, packages)
            stale.unlink()

            os.chmod(wheelhouse, 0o775)
            with self.assertRaisesRegex(
                CandidateBuildError,
                "candidate_runtime_wheelhouse_invalid",
            ):
                _verify_runtime_wheelhouse(wheelhouse, packages)
            os.chmod(wheelhouse, 0o700)

            first.chmod(0o660)
            with self.assertRaisesRegex(
                CandidateBuildError,
                "candidate_runtime_wheelhouse_invalid",
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
                encoded = base64.urlsafe_b64encode(
                    hashlib.sha256(value).digest()
                )
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
                        f"{DIST_INFO_PREFIX}WHEEL": wheel_metadata.encode(
                            "ascii"
                        ),
                        f"{DIST_INFO_PREFIX}entry_points.txt": (
                            entry_points.encode("ascii")
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
                CandidateBuildError,
                "candidate_project_wheel_invalid",
            ):
                _verify_project_wheel(wheel, expected_package_root=package)

    def test_bootstrap_uses_separate_fresh_exact_stores(self) -> None:
        contract = json.loads(
            (OPS / "bootstrap_contract.json").read_text(encoding="utf-8")
        )
        self.assertEqual(contract["state"], "inactive_candidate_no_resources_created")
        self.assertEqual(contract["api"]["bind"], "172.31.32.171:8091")
        self.assertEqual(
            contract["api"]["allowed_source_ipv4"],
            ["172.31.43.160/32"],
        )
        self.assertFalse(contract["isolation"]["reuse_existing_postgres_daemon"])
        self.assertFalse(contract["isolation"]["reuse_existing_qdrant_daemon"])
        self.assertEqual(
            contract["postgresql"]["required_settings"],
            {
                "log_parameter_max_length": "0",
                "log_parameter_max_length_on_error": "0",
            },
        )
        self.assertEqual(
            contract["conversation_bridge"]["outbox"],
            "memory_ingest_private.memory_ingest_outbox",
        )
        self.assertFalse(contract["conversation_bridge"]["historical_scan_allowed"])
        self.assertFalse(contract["conversation_bridge"]["base_table_select_for_worker_allowed"])
        self.assertEqual(
            contract["conversation_bridge"][
                "pilot_capture_limit_per_owner_rolling_24h"
            ],
            20,
        )
        self.assertEqual(
            contract["conversation_bridge"]["pilot_capture_limit_result"],
            "pilot_limit_reached_null_outbox_id",
        )
        self.assertEqual(
            contract["candidate_implementation_status"][
                "owner_claim_fact_detail"
            ],
            "implemented_candidate_phase6b_disposable_revalidation_pending_not_production_applied",
        )
        self.assertEqual(
            contract["candidate_implementation_status"]["runtime"],
            "phase6b_source_bound_receipt_present_disposable_execution_pending",
        )
        self.assertEqual(
            contract["candidate_implementation_status"]["qdrant_adapter"],
            "exact_fake_and_real_disposable_v1_19_0_validated_not_persistent_approved",
        )
        self.assertEqual(
            contract["candidate_implementation_status"]["pilot_marker"],
            "implemented_phase6b_disposable_revalidation_pending_not_production_applied",
        )
        self.assertTrue(contract["qdrant"]["real_disposable_compatibility_verified"])
        self.assertFalse(contract["qdrant"]["persistent_pilot_approved"])
        self.assertNotIn(
            "owner_claim_fact_detail_api_not_implemented",
            contract["create_policy"]["unresolved_creation_prerequisites"],
        )
        self.assertNotIn(
            "final_phase6b_runtime_rebuild_and_receipt_pending",
            contract["create_policy"]["unresolved_creation_prerequisites"],
        )
        self.assertNotIn(
            "qdrant_v1_19_0_real_disposable_compatibility_pending",
            contract["create_policy"]["unresolved_creation_prerequisites"],
        )
        self.assertNotIn(
            "pilot_marker_disposable_proof_pending",
            contract["create_policy"]["unresolved_creation_prerequisites"],
        )
        self.assertEqual(
            contract["cleanup_policy"]["unresolved_cleanup_prerequisites"],
            [],
        )
        self.assertFalse(
            contract["cleanup_policy"]["current_cleanup_authorized"]
        )
        self.assertFalse(contract["production_state_changed"])

    def test_pilot_is_bounded_blocked_and_attachment_free(self) -> None:
        pilot = json.loads(
            (OPS / "pilot_contract.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            pilot["state"],
            "inactive_candidate_blocked_not_authorized",
        )
        self.assertEqual(pilot["limits"]["maximum_owner_accounts"], 1)
        self.assertEqual(pilot["limits"]["maximum_post_cutover_user_messages"], 20)
        self.assertEqual(
            pilot["limits"]["maximum_post_cutover_user_messages_enforcement"],
            "owner_locked_all_state_outbox_count_rolling_24h_focused_static_and_adapter_tested_inactive_disposable_proof_pending",
        )
        self.assertFalse(pilot["eligible_input"]["old_conversations"])
        self.assertFalse(pilot["eligible_input"]["historical_backfill"])
        self.assertFalse(pilot["eligible_input"]["attachment_content"])
        self.assertEqual(pilot["provider_policy"]["provider_calls_before_pilot_authorization"], 0)
        self.assertFalse(
            pilot["authentication"][
                "fresh_user_check_claimed_as_immediate_signout_revocation"
            ]
        )
        self.assertTrue(pilot["authentication"]["session_id_required"])
        self.assertFalse(
            pilot["authentication"][
                "supabase_auth_sessions_rpc_live_verified"
            ]
        )
        self.assertIn(
            "supabase_auth_sessions_rpc_not_installed_or_live_verified",
            pilot["start_blockers"],
        )
        self.assertNotIn(
            "qdrant_v1_19_0_real_disposable_compatibility_pending",
            pilot["start_blockers"],
        )
        self.assertNotIn(
            "durable_pilot_marker_candidate_not_applied_or_disposable_proved",
            pilot["start_blockers"],
        )
        self.assertNotIn(
            "owner_claim_fact_detail_api_not_implemented",
            pilot["start_blockers"],
        )
        self.assertNotIn(
            "final_phase6b_runtime_rebuild_and_receipt_pending",
            pilot["start_blockers"],
        )
        self.assertIn(
            "projection_reconciliation_and_sequence_safe_qdrant_repair_not_implemented",
            pilot["start_blockers"],
        )
        self.assertEqual(
            pilot["provider_policy"]["provider_adapter_status"],
            "strict_fake_tested_zero_real_calls",
        )
        self.assertEqual(
            pilot["provider_policy"]["embedding_adapter_status"],
            "strict_3072_fake_tested_durable_request_dispatch_marker_implemented_disposable_proof_pending_zero_real_calls",
        )
        self.assertEqual(
            pilot["provider_policy"]["qdrant_adapter_status"],
            "exact_fake_and_real_disposable_v1_19_0_validated_not_persistent_approved",
        )
        self.assertEqual(
            pilot["candidate_surfaces"]["owner_claim_fact_detail"],
            "implemented_candidate_phase6b_disposable_revalidation_pending_not_production_applied",
        )
        self.assertEqual(
            pilot["candidate_surfaces"]["runtime"],
            "phase6b_source_bound_receipt_present_disposable_execution_pending",
        )
        self.assertEqual(
            pilot["candidate_surfaces"]["pilot_marker"],
            "implemented_phase6b_disposable_revalidation_pending_not_production_applied",
        )
        self.assertEqual(
            pilot["candidate_surfaces"]["frontend"],
            "6d80ba_built_undeployed_visual_qa_pending",
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

    def test_systemd_template_cannot_be_enabled_from_repository(self) -> None:
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
            "ConditionPathExists=/etc/governed-memory/worker.env",
            worker_unit,
        )
        self.assertIn(
            "ConditionPathExists=/etc/governed-memory/pilot.env",
            worker_unit,
        )
        self.assertFalse(
            (OPS / "systemd" / "governed-memory-worker.timer").exists()
        )


class ReleaseDecisionTests(unittest.TestCase):
    def test_create_refuses_while_production_activation_blockers_remain(self) -> None:
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
        document = observation("cleanup", state="present_exact")
        result = evaluate_release_observation(document)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason_code"], "authorization_missing")
        self.assertEqual(result["exact_action_plan"], [])

    def test_cleanup_observation_counts_cannot_bypass_authorization(self) -> None:
        document = observation("cleanup", state="present_exact")
        document["pilot_ever_started"] = True
        document["postgresql_user_row_count"] = 1
        document["qdrant_point_count"] = 1
        document["active_client_count"] = 1
        result = evaluate_release_observation(document)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason_code"], "authorization_missing")
        self.assertEqual(result["exact_action_plan"], [])


if __name__ == "__main__":
    unittest.main()

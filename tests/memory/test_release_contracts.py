from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from tools.governed_memory_release.build_candidate_runtime import (
    BUILD_LOCK,
    CandidateBuildError,
    RUNTIME_LOCK,
    SETUPTOOLS_SHA256,
    _package_source_tree_sha256,
    _parse_hash_lock,
    _source_bound_root,
    _verify_runtime_wheelhouse,
)
from tools.governed_memory_release.release_guard import (
    EXACT_TARGETS,
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
        self.assertEqual(len(result["artifact_sha256"]), 5)

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
                f"/tmp/governed-memory-phase4-runtime-{'a' * 64}-{second}",
            )

            (package / "unexpected.txt").write_text("stale", encoding="utf-8")
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
        self.assertIn(
            "owner_claim_fact_detail_api_not_implemented",
            contract["create_policy"]["unresolved_activation_blockers"],
        )
        self.assertFalse(contract["production_state_changed"])

    def test_pilot_is_bounded_blocked_and_attachment_free(self) -> None:
        pilot = json.loads(
            (OPS / "pilot_contract.json").read_text(encoding="utf-8")
        )
        self.assertEqual(pilot["state"], "blocked_not_authorized")
        self.assertEqual(pilot["limits"]["maximum_owner_accounts"], 1)
        self.assertEqual(pilot["limits"]["maximum_post_cutover_user_messages"], 20)
        self.assertFalse(pilot["eligible_input"]["old_conversations"])
        self.assertFalse(pilot["eligible_input"]["historical_backfill"])
        self.assertFalse(pilot["eligible_input"]["attachment_content"])
        self.assertEqual(pilot["provider_policy"]["provider_calls_before_pilot_authorization"], 0)
        self.assertFalse(
            pilot["authentication"][
                "fresh_user_check_claimed_as_immediate_signout_revocation"
            ]
        )
        self.assertIn(
            "strict_session_revocation_policy_not_decided",
            pilot["start_blockers"],
        )
        self.assertIn(
            "current_qdrant_immutable_digest_security_review_and_compatibility_validation_required",
            pilot["start_blockers"],
        )
        self.assertIn(
            "durable_pilot_ever_started_marker_not_implemented",
            pilot["start_blockers"],
        )
        self.assertIn(
            "owner_claim_fact_detail_api_not_implemented",
            pilot["start_blockers"],
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
        unit = (
            OPS / "systemd" / "governed-memory-http.service.in"
        ).read_text(encoding="utf-8")
        self.assertNotIn("[Install]", unit)
        self.assertNotIn("WantedBy=", unit)
        self.assertIn("GOVERNED_MEMORY_HTTP_MODE=off", unit)
        self.assertIn("Restart=no", unit)
        self.assertFalse(
            (OPS / "systemd" / "governed-memory-worker.service.in").exists()
        )


class ReleaseDecisionTests(unittest.TestCase):
    def test_create_refuses_until_current_qdrant_digest_is_approved(self) -> None:
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

    def test_cleanup_refuses_without_durable_monotonic_pilot_marker(self) -> None:
        document = observation("cleanup", state="present_exact")
        result = evaluate_release_observation(document)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason_code"], "durable_pilot_marker_unavailable")
        self.assertEqual(result["exact_action_plan"], [])

    def test_cleanup_observation_counts_cannot_bypass_marker_blocker(self) -> None:
        document = observation("cleanup", state="present_exact")
        document["pilot_ever_started"] = True
        document["postgresql_user_row_count"] = 1
        document["qdrant_point_count"] = 1
        document["active_client_count"] = 1
        result = evaluate_release_observation(document)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason_code"], "durable_pilot_marker_unavailable")
        self.assertEqual(result["exact_action_plan"], [])


if __name__ == "__main__":
    unittest.main()

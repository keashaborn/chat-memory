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
                initial["inactive_migration_execution_contract"],
                inactive_installation.INACTIVE_MIGRATION_EXECUTION_CONTRACT,
            )
            self.assertFalse(
                initial["inactive_migration_execution_contract"][
                    "evaluator_verifies_execution"
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

    def test_source_profiles_close_all_runtime_membership_edges(self) -> None:
        for stage, profile in (
            inactive_installation.SOURCE_CLUSTER_STATE_PROFILES.items()
        ):
            with self.subTest(stage=stage):
                self.assertEqual(
                    profile["inactive_source_runtime_membership_graph"], []
                )
                self.assertEqual(profile["brains_app_ingest_membership"], "absent")
                self.assertEqual(profile["brains_app_erasure_membership"], "absent")
                self.assertEqual(profile["api_ingest_membership"], "absent")
                self.assertEqual(profile["api_erasure_membership"], "absent")
                self.assertEqual(profile["worker_ingest_membership"], "absent")
                self.assertEqual(profile["worker_erasure_membership"], "absent")
                self.assertEqual(profile["source_log_duration"], "off")
                self.assertEqual(
                    profile["source_log_parameter_max_length"],
                    "0_bind_logging_disabled",
                )

    def test_disposable_runner_is_package_artifact_46_and_tamper_fails(self) -> None:
        result = inactive_installation.verify_package()
        runner_relative = (
            "tools/governed_memory_validation/run_disposable_successor.sh"
        )
        runner = ROOT / runner_relative
        self.assertEqual(len(result["artifact_sha256"]), 46)
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

    def test_persistent_descriptor_is_exact_pinned_and_inactive(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        compose = COMPOSE.read_text(encoding="utf-8")

        self.assertEqual(
            contract["state"], "candidate_only_not_installed_not_authorized"
        )
        self.assertFalse(contract["tooling"]["executes_commands"])
        self.assertFalse(contract["tooling"]["creates_resources"])
        self.assertFalse(contract["tooling"]["can_authorize_installation"])
        self.assertFalse(contract["tooling"]["can_authorize_rollback"])
        self.assertFalse(contract["evaluator_state_changed"])
        self.assertEqual(
            contract["source_cluster_policy"][
                "inactive_source_runtime_membership_graph"
            ],
            [],
        )
        receipt_schema = json.loads(
            (INSTALLATION / "receipt.schema.json").read_text(encoding="utf-8")
        )
        source_state_schema = receipt_schema["properties"][
            "exact_source_cluster_states"
        ]
        self.assertIn(
            "inactive_source_runtime_membership_graph",
            source_state_schema["required"],
        )
        self.assertEqual(
            source_state_schema["properties"][
                "inactive_source_runtime_membership_graph"
            ],
            {"type": "array", "maxItems": 0},
        )
        execution = contract["inactive_migration_execution_contract"]
        self.assertEqual(
            execution["psql_variable_name"],
            "governed_memory_inactive_installation",
        )
        self.assertEqual(execution["psql_variable_value"], "on")
        self.assertTrue(execution["roles_preflight_pgsql_requires_variable"])
        self.assertTrue(
            execution["conversation_bridge_forward_pgsql_requires_variable"]
        )
        self.assertTrue(
            execution[
                "omission_defaults_to_active_mode_and_invalidates_inactive_installation"
            ]
        )
        self.assertFalse(execution["evaluator_verifies_execution"])
        logging_policy = contract["source_logging_policy"]
        self.assertEqual(logging_policy["required_log_duration"], "off")
        self.assertEqual(logging_policy["required_log_parameter_max_length"], 0)
        self.assertEqual(
            logging_policy["required_log_parameter_max_length_on_error"], 0
        )
        self.assertEqual(logging_policy["observed_log_parameter_max_length"], -1)
        self.assertFalse(logging_policy["observed_state_safe"])
        self.assertFalse(logging_policy["remediation_authorized"])
        self.assertFalse(logging_policy["remediation_applied"])
        self.assertTrue(
            logging_policy["role_bootstrap_must_refuse_before_first_write"]
        )
        self.assertIn(
            "source_logging_parameter_remediation_not_authorized_or_applied",
            contract["installation_blockers"],
        )
        sequence = contract["inactive_install_sequence"]
        self.assertIn(
            "apply_verified_successor_migrations_after_roles_preflight_with_psql_variable_governed_memory_inactive_installation_on",
            sequence,
        )
        self.assertIn(
            "apply_verified_conversation_bridge_migration_with_psql_variable_governed_memory_inactive_installation_on_without_runtime_memberships",
            sequence,
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
        credential_policy = contract["database_role_credential_policy"]
        self.assertFalse(
            credential_policy[
                "inactive_install_postflight_catalog_hash_is_activation_hash"
            ]
        )
        self.assertTrue(
            credential_policy[
                "post_activation_catalog_hash_must_bind_login_and_membership_state"
            ]
        )
        self.assertLess(
            credential_policy["future_atomic_order"].index(
                "capture_and_seal_post_activation_full_bridge_catalog_hash"
            ),
            credential_policy["future_atomic_order"].index(
                "start_only_the_separately_approved_runtime"
            ),
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
        profiles = account["secret_file_profiles"]
        self.assertEqual(
            profiles["bootstrap.env"],
            {
                "owner": "root:root",
                "mode": "0600",
                "service_account_direct_read": False,
            },
        )
        for runtime_file in ("http.env", "worker.env", "pilot.env"):
            self.assertEqual(
                profiles[runtime_file]["owner"],
                "root:governed-memory",
            )
            self.assertEqual(profiles[runtime_file]["mode"], "0640")
        self.assertFalse(account["evaluator_state_changed"])

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

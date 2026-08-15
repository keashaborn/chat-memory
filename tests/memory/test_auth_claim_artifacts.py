from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "governed-memory-migrations"
FOUNDATION = MIGRATIONS / "0001_foundation"
CLAIM_DETAIL = MIGRATIONS / "0003_owner_claim_detail"
SESSION_AUTHORITY = ROOT / "ops/governed_memory/supabase_session_authority"
RUNNER = ROOT / "tools/governed_memory_validation/run_disposable_successor.sh"
DELETION_INTEGRATION = (
    ROOT / "tests/memory_integration/test_conversation_deletion_disposable.py"
)
HTTP_VERTICAL_SLICE = (
    ROOT / "tests/memory_integration/test_governed_memory_http_vertical_slice.py"
)
CURRENT_STATUS = "isolated_candidate_disposable_validated_not_production_applied"
ROOT_STATUS = CURRENT_STATUS


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
    )
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sql_function(sql: str, qualified_name: str) -> str:
    marker = f"CREATE FUNCTION {qualified_name}("
    if marker not in sql:
        raise AssertionError(f"function missing: {qualified_name}")
    return sql.split(marker, 1)[1].split("$function$;", 1)[0]


class FoundationErasedChatTombstoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.forward = (FOUNDATION / "forward.pgsql").read_text(
            encoding="utf-8"
        )
        cls.rollback = (FOUNDATION / "rollback.pgsql").read_text(
            encoding="utf-8"
        )

    def test_tombstone_is_global_private_permanent_and_manifest_bound(self) -> None:
        table = self.forward.split(
            "CREATE TABLE memory.erased_chat_message_tombstone (", 1
        )[1].split("\n);", 1)[0]
        self.assertIn("message_id uuid PRIMARY KEY", table)
        self.assertIn("owner_user_id uuid NOT NULL", table)
        self.assertIn("erasure_operation_id uuid NOT NULL", table)
        self.assertIn("erased_at timestamptz NOT NULL", table)
        self.assertIn(
            "REFERENCES memory.source_erasure_operation(\n"
            "    owner_user_id, operation_id\n"
            "  ) ON DELETE RESTRICT",
            table,
        )
        self.assertIn(
            "ALTER TABLE memory.erased_chat_message_tombstone "
            "ENABLE ROW LEVEL SECURITY",
            self.forward,
        )
        self.assertIn(
            "ALTER TABLE memory.erased_chat_message_tombstone "
            "FORCE ROW LEVEL SECURITY",
            self.forward,
        )
        self.assertIn(
            "CREATE POLICY owner_internal ON "
            "memory.erased_chat_message_tombstone",
            self.forward,
        )
        self.assertNotIn(
            "CREATE POLICY owner_isolation ON "
            "memory.erased_chat_message_tombstone",
            self.forward,
        )

        seal = _sql_function(
            self.forward, "memory_private.seal_source_erasure"
        )
        ordered = (
            "source erasure target manifest is incomplete",
            "ORDER BY target.message_id",
            "cross_owner_chat_message_lineage",
            "INSERT INTO memory.erased_chat_message_tombstone",
            "ON CONFLICT (message_id) DO NOTHING",
            "erased chat message tombstone lineage conflicts",
        )
        self.assertEqual(sorted(ordered, key=seal.index), list(ordered))
        self.assertRegex(
            seal,
            r"evidence\.owner_user_id\s*<>\s*operation\.owner_user_id",
        )
        self.assertRegex(
            seal,
            r"binding\.owner_user_id\s*<>\s*operation\.owner_user_id",
        )
        self.assertIn("binding.response_id", seal)
        self.assertIn("evidence.context_message_id", seal)

        guard = _sql_function(
            self.forward, "memory_private.assert_chat_messages_not_erased"
        )
        self.assertIn("ORDER BY message_id", guard)
        self.assertIn("pg_advisory_xact_lock", guard)
        self.assertIn("tombstone.message_id IN", guard)
        self.assertNotIn("tombstone.owner_user_id", guard)
        for function_name, exact_call in (
            (
                "memory_private.record_selected_evidence",
                "assert_chat_messages_not_erased(\n"
                "    p_message_id, p_context_message_id",
            ),
            (
                "memory_private.record_answer_binding",
                "assert_chat_messages_not_erased(\n"
                "    p_response_id, NULL::uuid",
            ),
        ):
            with self.subTest(function=function_name):
                self.assertIn(
                    exact_call,
                    _sql_function(self.forward, function_name),
                )
        self.assertEqual(
            self.forward.count(
                "CREATE TRIGGER erased_chat_message_replay\n"
                "  BEFORE INSERT ON memory."
            ),
            2,
        )
        self.assertIn(
            "CREATE TRIGGER erased_chat_message_tombstone_immutable",
            self.forward,
        )
        verifier = _sql_function(
            self.forward,
            "memory_private.assert_source_erasure_tombstones",
        )
        for required in (
            "ORDER BY target.message_id",
            "observed_target_count <> p_target_count",
            "observed_tombstone_count <> p_target_count",
            "tombstone.owner_user_id <> p_owner_user_id",
            "tombstone.erasure_operation_id <> p_operation_id",
            "tombstone.erased_at IS DISTINCT FROM p_sealed_at",
            "evidence.owner_user_id <> p_owner_user_id",
            "binding.owner_user_id <> p_owner_user_id",
        ):
            self.assertIn(required, verifier)

    def test_every_delete_entry_point_locks_then_checks_closed_catalog(self) -> None:
        catalog = _sql_function(
            self.forward,
            "memory_private.assert_source_erasure_deletion_catalog",
        )
        roots = (
            "answer_binding",
            "claim_evidence",
            "projection_outbox",
            "proposal",
            "claim_revision",
            "claim",
            "provider_call",
            "extraction_job",
            "evidence",
            "entity",
            "source_erasure_claim",
            "source_erasure_target",
        )
        for root in roots:
            self.assertIn(f"'memory.{root}'::regclass::oid", catalog)
        for required in (
            "pg_catalog.pg_constraint",
            "constraint_row.confrelid = ANY(deletion_roots)",
            "EXCEPT ALL",
            "pg_catalog.pg_trigger",
            "(trigger_row.tgtype::integer & 8) = 8",
            "pg_catalog.pg_rewrite",
            "rewrite_row.ev_type = '4'",
            "pg_catalog.pg_inherits",
            "LOCK TABLE memory.erased_chat_message_tombstone\n"
            "    IN SHARE ROW EXCLUSIVE MODE;",
            "pg_catalog.pg_policy",
            "pg_catalog.aclexplode",
            "erased_chat_message_tombstone_operation_fk",
            "erased_chat_message_tombstone_owner_nonzero",
            "pg_catalog.pg_get_expr",
            "erased_chat_message_tombstone_immutable",
            "erased_chat_message_replay",
            "erased chat replay or permanent tombstone trigger inventory differs",
        ):
            self.assertIn(required, catalog)
        self.assertNotIn("confdeltype = 'c'", catalog)

        lock_sequence = [
            f"LOCK TABLE memory.{root} IN ROW EXCLUSIVE MODE;"
            for root in roots
        ]
        delete_entry_points = (
            "memory_private.purge_terminal_proposals",
            "memory_private.purge_expired_answer_bindings",
            "memory_private.finalize_claim_deletion",
            "memory_private.finalize_source_erasure_memory",
            "memory_private.ack_source_erasure_conversation_deleted",
        )
        for function_name in delete_entry_points:
            body = _sql_function(self.forward, function_name)
            with self.subTest(function=function_name):
                positions = [body.index(lock) for lock in lock_sequence]
                self.assertEqual(positions, sorted(positions))
                assertion_position = body.index(
                    "PERFORM "
                    "memory_private.assert_source_erasure_deletion_catalog();"
                )
                self.assertLess(positions[-1], assertion_position)
                self.assertLess(assertion_position, body.index("DELETE FROM"))

        register = _sql_function(
            self.forward, "memory_private.register_source_erasure"
        )
        register_locks = [
            lock.replace("ROW EXCLUSIVE", "SHARE ROW EXCLUSIVE")
            for lock in lock_sequence
        ]
        register_positions = [register.index(lock) for lock in register_locks]
        self.assertEqual(register_positions, sorted(register_positions))
        self.assertLess(
            register_positions[-1],
            register.index(
                "PERFORM "
                "memory_private.assert_source_erasure_deletion_catalog();"
            ),
        )
        seal = _sql_function(
            self.forward, "memory_private.seal_source_erasure"
        )
        seal_positions = [seal.index(lock) for lock in lock_sequence]
        self.assertEqual(seal_positions, sorted(seal_positions))
        self.assertLess(
            seal.index(
                "PERFORM "
                "memory_private.assert_source_erasure_deletion_catalog();"
            ),
            seal.index("INSERT INTO memory.erased_chat_message_tombstone"),
        )

        tombstone_assertion = (
            "PERFORM memory_private.assert_source_erasure_tombstones(\n"
            "    operation.operation_id, operation.owner_user_id,\n"
            "    operation.target_count, operation.sealed_at\n"
            "  );"
        )
        for function_name in (
            "memory_private.finalize_source_erasure_memory",
            "memory_private.ack_source_erasure_conversation_deleted",
        ):
            body = _sql_function(self.forward, function_name)
            with self.subTest(tombstone_reverification=function_name):
                self.assertIn(tombstone_assertion, body)
                self.assertLess(
                    body.index(tombstone_assertion), body.index("DELETE FROM")
                )
        ack = _sql_function(
            self.forward,
            "memory_private.ack_source_erasure_conversation_deleted",
        )
        self.assertLess(
            ack.index(tombstone_assertion),
            ack.index("INSERT INTO memory.source_erasure_receipt"),
        )

    def test_metadata_and_empty_only_rollback_preserve_tombstones(self) -> None:
        package = _load_json(FOUNDATION / "package.json")
        self.assertEqual(package["object_contract"]["table_count"], 18)
        self.assertEqual(
            package["object_contract"]["owner_bearing_table_count"], 17
        )
        self.assertTrue(
            package["object_contract"][
                "global_chat_message_id_replay_suppression"
            ]
        )
        self.assertTrue(
            package["object_contract"][
                "tombstone_reverified_before_finalization_and_ack"
            ]
        )
        self.assertTrue(
            package["object_contract"][
                "runtime_tombstone_catalog_attestation"
            ]
        )
        schema = _load_json(MIGRATIONS / "schema_contract.json")
        self.assertIn("erased_chat_message_tombstone", schema["tables"])
        self.assertIn(
            "erased_chat_message_tombstone", schema["owner_bearing_tables"]
        )
        tombstone = schema["erased_chat_message_tombstone"]
        self.assertEqual(tombstone["primary_key"], ["message_id"])
        self.assertTrue(tombstone["global_message_identity"])
        self.assertEqual(
            tombstone["cross_owner_lineage_action"],
            "manual_review_before_tombstone_or_delete",
        )
        self.assertEqual(
            tombstone["retention"], "permanent_no_runtime_delete_path"
        )
        for signature in (
            "memory_private.assert_chat_messages_not_erased(uuid,uuid)",
            "memory_private.guard_erased_chat_message_replay()",
            "memory_private.guard_erased_chat_message_tombstone_immutable()",
            "memory_private.assert_source_erasure_tombstones(uuid,uuid,integer,timestamptz)",
            "memory_private.assert_source_erasure_deletion_catalog()",
        ):
            self.assertIn(signature, schema["internal_functions"])
        self.assertIn(
            "exact_target_count_owner_operation_erased_at",
            tombstone["transition_reverification"],
        )
        self.assertIn(
            "complete_user_trigger_inventory",
            tombstone["runtime_catalog_attestation"],
        )

        self.assertIn(
            "SELECT 1 FROM memory.erased_chat_message_tombstone LIMIT 1",
            self.rollback,
        )
        self.assertLess(
            self.rollback.index(
                "DROP TABLE memory.erased_chat_message_tombstone;"
            ),
            self.rollback.index("DROP TABLE memory.source_erasure_operation;"),
        )
        self.assertNotRegex(
            self.forward + self.rollback,
            re.compile(r"\b(?:DROP|TRUNCATE)\b[^;]*\bCASCADE\b", re.I),
        )


class OwnerClaimDetailMigrationTests(unittest.TestCase):
    def test_package_hashes_and_manifest_report_current_truth(self) -> None:
        package_path = CLAIM_DETAIL / "package.json"
        package = _load_json(package_path)
        forward_path = CLAIM_DETAIL / "forward.pgsql"
        rollback_path = CLAIM_DETAIL / "rollback.pgsql"
        self.assertEqual(package["status"], CURRENT_STATUS)
        self.assertTrue(package["activation"]["disposable_database_validated"])
        self.assertEqual(package["forward"]["sha256"], _sha256(forward_path))
        self.assertEqual(package["rollback"]["sha256"], _sha256(rollback_path))
        self.assertFalse(package["rollback"]["empty_only"])
        self.assertFalse(package["rollback"]["data_mutation"])

        manifest = _load_json(MIGRATIONS / "manifest.json")
        self.assertEqual(manifest["status"], ROOT_STATUS)
        self.assertEqual(package["status"], CURRENT_STATUS)
        self.assertTrue(
            manifest["safety"]["disposable_database_execution_performed"]
        )
        self.assertFalse(
            manifest["authority"]["disposable_validation_authorized"]
        )
        entries = {item["path"]: item["sha256"] for item in manifest["files"]}
        for relative in (
            "0003_owner_claim_detail/package.json",
            "0003_owner_claim_detail/forward.pgsql",
            "0003_owner_claim_detail/rollback.pgsql",
        ):
            self.assertEqual(entries[relative], _sha256(MIGRATIONS / relative))
        self.assertEqual(
            manifest["execution_order"],
            [
                "roles_preflight.pgsql",
                "0001_foundation/forward.pgsql",
                "0003_owner_claim_detail/forward.pgsql",
                "0004_pilot_marker/forward.pgsql",
                "0005_bounded_auto_admission/forward.pgsql",
                "0006_source_erasure_projection_recovery/forward.pgsql",
                "0002_conversation_bridge/forward.pgsql",
            ],
        )
        self.assertEqual(
            manifest["rollback_order"],
            [
                "0002_conversation_bridge/rollback.pgsql",
                "0006_source_erasure_projection_recovery/rollback.pgsql",
                "0005_bounded_auto_admission/rollback.pgsql",
                "0004_pilot_marker/rollback.pgsql",
                "0003_owner_claim_detail/rollback.pgsql",
                "0001_foundation/rollback.pgsql",
            ],
        )

    def test_function_is_owner_scoped_exact_and_list_surface_is_untouched(self) -> None:
        forward = (CLAIM_DETAIL / "forward.pgsql").read_text(encoding="utf-8")
        rollback = (CLAIM_DETAIL / "rollback.pgsql").read_text(encoding="utf-8")
        self.assertIn(
            "CREATE FUNCTION memory_private.read_claim(p_claim_id uuid)",
            forward,
        )
        self.assertIn("SECURITY DEFINER\nSET search_path TO pg_catalog", forward)
        self.assertIn("session_user <> 'governed_memory_api'", forward)
        self.assertIn("actor := memory_private.current_owner_id()", forward)
        self.assertIn("claim.owner_user_id = actor", forward)
        self.assertIn(
            "revision.revision_number = claim.current_revision_number",
            forward,
        )
        self.assertIn("revision.object_literal #>> '{}'", forward)
        self.assertNotIn("list_claims", forward)
        self.assertNotRegex(forward, re.compile(r"\bCASCADE\b", re.IGNORECASE))
        self.assertNotRegex(rollback, re.compile(r"\bCASCADE\b", re.IGNORECASE))
        self.assertIn(
            "GRANT EXECUTE ON FUNCTION memory_private.read_claim(uuid)\n"
            "  TO governed_memory_api",
            forward,
        )

        schema = _load_json(MIGRATIONS / "schema_contract.json")
        package = _load_json(CLAIM_DETAIL / "package.json")
        self.assertEqual(package["status"], CURRENT_STATUS)
        self.assertEqual(schema["status"], ROOT_STATUS)
        self.assertEqual(package["status"], CURRENT_STATUS)
        self.assertIn(
            "memory_private.read_claim(uuid)",
            schema["function_surface"],
        )
        blockers = schema["hard_requirements"]["production_activation_blockers"]
        self.assertIn(
            "supabase_auth_sessions_rpc_not_installed_or_live_verified",
            blockers,
        )
        runtime_blockers = _load_json(
            ROOT / "ops/governed_memory/runtime_manifest.json"
        )["activation"]["blockers"]
        self.assertEqual(
            blockers,
            runtime_blockers,
            "schema and runtime activation blockers must remain exactly synchronized",
        )
        self.assertNotIn(
            "owner_claim_fact_detail_not_disposable_validated",
            blockers,
        )
        self.assertEqual(
            schema["claim_detail"],
            {
                "database": "governed_memory",
                "function": "memory_private.read_claim(uuid)",
                "runtime_role": "governed_memory_api",
                "authority": "memory_private.current_owner_id()",
                "owner_scoped": True,
                "list_claims_content_free": True,
                "detail_literal_max_utf8_bytes": 2000,
                "direct_runtime_table_access": False,
                "migration": "0003_owner_claim_detail/forward.pgsql",
                "disposable_validated": True,
                "historical_phase7c_disposable_validated": True,
                "production_applied": False,
            },
        )

    def test_runner_applies_and_rolls_back_exact_package_order(self) -> None:
        runner = RUNNER.read_text(encoding="utf-8")
        self.assertIn(
            "readonly EXPECTED_BASE='42e81cb9230873c8027cd805b6ebe84608cfea5e'",
            runner,
        )
        self.assertIn(
            "readonly REQUIRED_CANDIDATE_ROOT="
            "'/tmp/chat-memory-governed-phase8d-retirement-20260812'",
            runner,
        )
        self.assertIn(
            "readonly REQUIRED_CANDIDATE_BRANCH="
            "'codex/governed-memory-phase8d-retirement-20260812'",
            runner,
        )
        self.assertIn(
            "readonly EXPECTED_MANIFEST_SHA256="
            "'f1be143940c3d40197a9f959e5b6d476ae70763972baa9e769419bba7c2e5a70'",
            runner,
        )
        postgres_digest = (
            "postgres:16-alpine@sha256:"
            "57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
        )
        self.assertIn(f"readonly POSTGRES_IMAGE='{postgres_digest}'", runner)
        self.assertIn(
            "readonly POSTGRES_IMAGE_DIGEST='postgres@sha256:"
            "57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777'",
            runner,
        )
        self.assertIn(
            "readonly POSTGRES_IMAGE_ID='sha256:"
            "de3a4eab8fdfa507ea92aac488b916b08089e515db49b055fe71dfa271ba3a28'",
            runner,
        )
        qdrant_digest = (
            "qdrant/qdrant@sha256:"
            "057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc"
        )
        self.assertIn(f"readonly QDRANT_IMAGE='{qdrant_digest}'", runner)
        self.assertIn(f"readonly QDRANT_IMAGE_DIGEST='{qdrant_digest}'", runner)
        self.assertIn(
            "readonly QDRANT_IMAGE_ID='sha256:"
            "92c4050629efe895f87dafd2830f1cd4d0532bc9967b777cab979ebda71612b3'",
            runner,
        )
        self.assertIn(
            "[[ \"${QDRANT_SERVER_VERSION}\" == '1.19.0' ]]",
            runner,
        )
        bootstrap = runner.split("bootstrap_postgres() {", 1)[1].split(
            "verify_disposable_pilot_marker_semantics() {", 1
        )[0]
        self.assertIn(
            "SUCCESSOR_POSTGRES_LOGGING_PREFLIGHT=privileged-cluster-safe",
            bootstrap,
        )
        self.assertIn("shared_preload_libraries", bootstrap)
        self.assertIn(
            "postgres_privileged_logging_preflight_failed",
            bootstrap,
        )
        self.assertIn(
            "readonly EXPECTED_POSTGRES_BOOTSTRAP_SHA256="
            "'9cdda41a1056bec45409e13002bcdd5a234b13d6a4cc18306668bd38085ca5eb'",
            runner,
        )
        fixture_verifier = runner.split(
            "verify_postgres_bootstrap_fixture() {", 1
        )[1].split("verify_migration_manifest() {", 1)[0]
        self.assertIn("postgres_bootstrap_fixture_invalid", fixture_verifier)
        self.assertIn("postgres_bootstrap_fixture_sha256_mismatch", fixture_verifier)
        preflight = runner.split("preflight() {", 1)[1].split("full() {", 1)[0]
        self.assertLess(
            preflight.index("verify_postgres_bootstrap_fixture"),
            preflight.index("verify_migration_manifest"),
        )
        full = runner.split("full() {", 1)[1]
        self.assertLess(full.index("preflight"), full.index("create_resources"))
        self.assertLess(
            full.index("bootstrap_postgres"),
            full.index("verify_mixed_catalog_migration_refusal"),
        )
        for exact_membership_binding in (
            "postgres_canonical_membership_preflight_query_failed",
            "postgres_canonical_membership_preflight_failed",
            "SUCCESSOR_POSTGRES_MEMBERSHIP_PREFLIGHT="
            "canonical-bootstrap-owner-only",
            "bootstrap.rolcanlogin",
            "bootstrap.rolsuper",
            "bootstrap.rolcreatedb",
            "bootstrap.rolcreaterole",
            "bootstrap.rolreplication",
            "bootstrap.rolbypassrls",
            "bootstrap.rolinherit",
            "bootstrap.rolpassword IS NULL",
            "granted_role.rolname = 'governed_memory_owner'",
            "member_role.rolname = 'governed_memory_bootstrap'",
            "NOT membership.admin_option",
            "membership.inherit_option",
            "membership.set_option",
        ):
            with self.subTest(exact_membership_binding=exact_membership_binding):
                self.assertIn(exact_membership_binding, bootstrap)
        self.assertNotIn("qdrant/qdrant:v1.11.0", runner)
        self.assertIn("{{json .RepoDigests}}", runner)
        self.assertIn('if sys.argv[2] not in json.loads(sys.argv[1]):', runner)
        apply = runner.split("apply_migrations() {", 1)[1].split(
            "rollback_migrations() {", 1
        )[0]
        bridge_membership = runner.split(
            "verify_disposable_bridge_memberships() {", 1
        )[1].split(
            "verify_mixed_catalog_migration_refusal() {", 1
        )[0]
        exact_grant = (
            "GRANT memory_ingest_writer TO brains_app; "
            "GRANT memory_erasure_requester TO governed_memory_api"
        )
        exact_revoke = (
            "REVOKE memory_ingest_writer FROM brains_app; "
            "REVOKE memory_erasure_requester FROM governed_memory_api"
        )
        self.assertIn(exact_grant, apply)
        self.assertEqual(runner.count(exact_grant), 2)
        self.assertEqual(runner.count(exact_revoke), 2)
        self.assertNotIn(
            "GRANT memory_ingest_writer, memory_erasure_requester TO brains_app",
            runner,
        )
        self.assertNotIn("memory_erasure_requester TO brains_app", runner)
        self.assertNotIn("memory_erasure_requester FROM brains_app", runner)
        self.assertIn(
            "WHERE granted_role = 'memory_ingest_writer'\n"
            "            AND member_role = 'brains_app'\n"
            "            AND NOT admin_option\n"
            "            AND inherit_option\n"
            "            AND set_option",
            bridge_membership,
        )
        self.assertIn(
            "WHERE granted_role = 'memory_erasure_requester'\n"
            "            AND member_role = 'governed_memory_api'\n"
            "            AND NOT admin_option\n"
            "            AND NOT inherit_option\n"
            "            AND set_option",
            bridge_membership,
        )
        for exact_runtime_binding in (
            "disposable_bridge_membership_postflight_query_failed",
            "disposable_bridge_membership_postflight_failed",
            "'governed_memory_api', 'memory', 'CONNECT'",
            "(SELECT pg_catalog.count(*) FROM runtime_membership) = 2",
            "granted_role = 'memory_ingest_writer'",
            "member_role = 'brains_app'",
            "granted_role = 'memory_erasure_requester'",
            "member_role = 'governed_memory_api'",
            "NOT admin_option",
            "inherit_option",
            "NOT inherit_option",
            "set_option",
        ):
            with self.subTest(exact_runtime_binding=exact_runtime_binding):
                self.assertIn(exact_runtime_binding, bridge_membership)
        rollback = runner.split("rollback_migrations() {", 1)[1].split(
            "verify_rollback_refuses_inflight_enqueue() {", 1
        )[0]
        apply_paths = [
            "roles_preflight.pgsql",
            "0001_foundation/forward.pgsql",
            "0003_owner_claim_detail/forward.pgsql",
            "0004_pilot_marker/forward.pgsql",
            "0005_bounded_auto_admission/forward.pgsql",
            "0002_conversation_bridge/forward.pgsql",
        ]
        rollback_paths = [
            "0002_conversation_bridge/rollback.pgsql",
            "0006_source_erasure_projection_recovery/rollback.pgsql",
            "0005_bounded_auto_admission/rollback.pgsql",
            "0004_pilot_marker/rollback.pgsql",
            "0003_owner_claim_detail/rollback.pgsql",
            "0001_foundation/rollback.pgsql",
        ]
        self.assertEqual(
            sorted(apply_paths, key=apply.index),
            apply_paths,
        )
        self.assertEqual(
            sorted(rollback_paths, key=rollback.index),
            rollback_paths,
        )
        self.assertLess(
            rollback.index("SELECT pg_catalog.count(*) FROM memory.pilot_marker"),
            rollback.index("0004_pilot_marker/rollback.pgsql"),
        )
        self.assertIn("pilot_marker_not_empty_before_rollback", rollback)
        self.assertNotIn("--phase6e-disposable-deletion-proof", runner)
        self.assertIn(
            "readonly CURRENT_DELETION_INTEGRATION_READY='true'",
            runner,
        )
        self.assertIn(
            "retired_phase6e_preliminary_proof_mode_refused",
            runner,
        )
        rollback_race = runner.split(
            "verify_rollback_refuses_inflight_enqueue() {", 1
        )[1].split("bootstrap_postgres() {", 1)[0]
        self.assertIn(
            "query LIKE '%LOCK TABLE public.threads IN ACCESS EXCLUSIVE MODE%'",
            rollback_race,
        )
        self.assertNotIn(
            "query LIKE '%LOCK TABLE memory_ingest_private.memory_ingest_outbox%'",
            rollback_race,
        )
        for exact_binding in (
            "test_http_chat_a_to_chat_b_rebuild_and_deletion",
            "test_deletion_resilience_boundaries",
            "test_exact_chat_only_deletion_and_protected_store_retention",
            "SUCCESSOR_DELETION_RECEIPT=",
            "SUCCESSOR_DELETION_RESILIENCE_RECEIPT=",
            "validate_deletion_receipt",
            "validate_deletion_resilience_receipt",
            '"deletion_receipt_sha256":"%s"',
            '"deletion_resilience_receipt_sha256":"%s"',
        ):
            with self.subTest(exact_binding=exact_binding):
                self.assertIn(exact_binding, runner)
        self.assertNotIn("SUCCESSOR_PHASE6E_DELETION_RECEIPT=", runner)
        self.assertNotIn("SUCCESSOR_PHASE6E_DELETION_RESILIENCE_RECEIPT=", runner)
        self.assertIn(
            '"schema_version":"governed-memory-successor-disposable-run-v7"',
            runner,
        )
        absence = runner.split("assert_rollback_absence() {", 1)[1].split(
            "verify_apply_rollback_reapply() {", 1
        )[0]
        for object_name in (
            "memory_private.read_claim(uuid)",
            "memory.pilot_marker",
            "memory_private.pilot_marker_receipt_sha256",
            "memory_private.guard_pilot_marker_append_only()",
            "memory_private.mark_pilot_started",
            "memory_private.read_pilot_marker()",
        ):
            self.assertIn(object_name, absence)

    def test_deletion_integration_keeps_capture_erasure_and_worker_lanes_split(
        self,
    ) -> None:
        integration = DELETION_INTEGRATION.read_text(encoding="utf-8")
        self.assertEqual(
            integration.count("SUCCESSOR_DELETION_RECEIPT="),
            1,
        )
        self.assertEqual(
            integration.count("SUCCESSOR_DELETION_RESILIENCE_RECEIPT="),
            1,
        )
        self.assertNotIn("SUCCESSOR_PHASE6E_DELETION_RECEIPT=", integration)
        self.assertNotIn(
            "SUCCESSOR_PHASE6E_DELETION_RESILIENCE_RECEIPT=",
            integration,
        )
        self.assertIn(
            "self.conversation_api = await self._connect(\n"
            "            \"governed_memory_api\",\n"
            "            \"successor_api_disposable_only\",\n"
            "            \"memory\",\n"
            "        )",
            integration,
        )
        erasure_context = integration.split(
            "async def _erasure_owner_context(", 1
        )[1].split("@staticmethod", 1)[0]
        self.assertIn("self.conversation_api.transaction()", erasure_context)
        self.assertIn("SET LOCAL ROLE memory_erasure_requester", erasure_context)
        self.assertNotIn("self.brains", erasure_context)
        self.assertEqual(
            len(
                re.findall(
                    r"PostgresConversationDeletionRepository\(\s*"
                    r"self\.conversation_api\s*\)",
                    integration,
                )
            ),
            4,
        )
        self.assertNotRegex(
            integration,
            r"PostgresConversationDeletionRepository\(\s*self\.brains\s*\)",
        )
        future_probe = integration.split(
            "with self.assertRaises(asyncpg.PostgresError) as future_failure:",
            1,
        )[1].split(
            "self.assertEqual(future_failure.exception.sqlstate, \"23514\")",
            1,
        )[0]
        self.assertIn("self._erasure_owner_context(FUTURE_OWNER)", future_probe)
        self.assertIn("self.conversation_api.fetchrow", future_probe)
        self.assertGreaterEqual(
            integration.count("async with self._owner_context(self.brains"),
            4,
        )
        self.assertIn(
            "PostgresConversationDeletionRepository(\n"
            "            self.conversation_worker\n"
            "        )",
            integration,
        )

    def test_disposable_worker_invocation_binds_exclusive_successor_mode(
        self,
    ) -> None:
        integration = HTTP_VERTICAL_SLICE.read_text(encoding="utf-8")
        runtime_proof = integration.split(
            "async def prove_inactive_worker_runtime(self) -> str:", 1
        )[1].split(
            "async def assert_bridge_source_snapshot", 1
        )[0]
        self.assertEqual(
            runtime_proof.count(
                '"GOVERNED_MEMORY_EXCLUSIVE_MODE": "successor_pilot"'
            ),
            1,
        )
        self.assertEqual(
            runtime_proof.count('"GOVERNED_MEMORY_WORKER_MODE": "on"'),
            1,
        )

    def test_runner_proves_marker_semantics_only_after_reapply(self) -> None:
        runner = RUNNER.read_text(encoding="utf-8")
        proof = runner.split("verify_disposable_pilot_marker_semantics() {", 1)[
            1
        ].split("verify_apply_rollback_reapply() {", 1)[0]
        cycle = runner.split("verify_apply_rollback_reapply() {", 1)[1].split(
            "validate_integration_receipt() {", 1
        )[0]
        self.assertEqual(cycle.count("apply_migrations"), 2)
        self.assertLess(
            cycle.rindex("apply_migrations"),
            cycle.index("verify_disposable_pilot_marker_semantics"),
        )
        self.assertNotIn(
            "rollback_migrations",
            cycle.split("verify_disposable_pilot_marker_semantics", 1)[1],
        )
        for expected in (
            "SELECT pg_catalog.count(*) FROM memory.pilot_marker",
            "SELECT pg_catalog.count(*) FROM memory_private.read_pilot_marker()",
            "pilot_marker_initial_row_count_not_zero",
            "pilot_marker_absence_did_not_read_false",
            "pilot_marker_first_insert_result_mismatch",
            "pilot_marker_exact_replay_result_mismatch",
            "pilot_marker_conflicting_replay_unexpectedly_succeeded",
            "pilot_marker_conflicting_replay_refusal_missing",
            "pilot_marker_final_row_count_not_one",
            "pilot_marker_final_read_result_mismatch",
            "pilot_marker_database_time_failed",
            "pilot_marker_database_time_invalid",
            "marker_receipt_sha256",
            "SUCCESSOR_PILOT_MARKER_ABSENCE=false rows=0",
            "SUCCESSOR_PILOT_MARKER_SEMANTICS=insert-replay-conflict-refusal-read-true",
        ):
            self.assertIn(expected, proof)
        self.assertIn(
            "--tmpfs /var/lib/postgresql/data:rw,nosuid,noexec,size=1536m",
            runner,
        )
        self.assertIn(
            "--tmpfs /qdrant/storage:rw,nosuid,noexec,size=1536m",
            runner,
        )
        self.assertNotIn("--mount", runner)
        self.assertNotIn("--volume", runner)
        self.assertIn('"provider_external_calls":0', runner)


class SupabaseSessionAuthorityArtifactTests(unittest.TestCase):
    def test_staged_contract_hashes_and_authority_are_exact(self) -> None:
        contract = _load_json(SESSION_AUTHORITY / "contract.json")
        self.assertEqual(contract["status"], "staged_not_applied_not_live_verified")
        self.assertFalse(contract["authority"]["production_apply_authorized"])
        self.assertFalse(contract["authority"]["production_apply_performed"])
        self.assertFalse(contract["authority"]["service_role_secret_required"])
        for direction in ("forward", "rollback"):
            artifact = contract["artifacts"][direction]
            self.assertEqual(
                artifact["sha256"],
                _sha256(SESSION_AUTHORITY / artifact["path"]),
            )
        self.assertEqual(
            contract["runtime_contract"]["request_order"],
            [
                "/auth/v1/user",
                "/rest/v1/rpc/governed_memory_current_session_v1",
                "governed_memory_owner_store",
            ],
        )
        self.assertFalse(contract["runtime_contract"]["fallback_allowed"])

    def test_sql_uses_private_definer_and_public_invoker_without_id_arguments(
        self,
    ) -> None:
        forward = (SESSION_AUTHORITY / "forward.pgsql").read_text(
            encoding="utf-8"
        )
        rollback = (SESSION_AUTHORITY / "rollback.pgsql").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "CREATE FUNCTION governed_memory_auth_private.current_session_v1()",
            forward,
        )
        self.assertIn(
            "CREATE FUNCTION public.governed_memory_current_session_v1()",
            forward,
        )
        self.assertEqual(forward.count("SET search_path TO ''"), 2)
        self.assertIn("SECURITY DEFINER", forward)
        self.assertIn("SECURITY INVOKER", forward)
        self.assertIn("owner_id := auth.uid()", forward)
        self.assertIn("claims := auth.jwt()", forward)
        self.assertIn("FROM auth.sessions AS session", forward)
        self.assertIn("session.id = checked_session_id", forward)
        self.assertIn("session.user_id = owner_id", forward)
        self.assertIn(
            "GRANT EXECUTE ON FUNCTION public.governed_memory_current_session_v1()\n"
            "TO authenticated",
            forward,
        )
        self.assertNotRegex(
            forward,
            re.compile(r"GRANT EXECUTE[\s\S]*?TO (?:PUBLIC|anon|service_role);"),
        )
        self.assertNotRegex(forward, re.compile(r"\bCASCADE\b", re.IGNORECASE))
        self.assertNotRegex(rollback, re.compile(r"\bCASCADE\b", re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()

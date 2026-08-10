from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "governed-memory-migrations"
CLAIM_DETAIL = MIGRATIONS / "0003_owner_claim_detail"
SESSION_AUTHORITY = ROOT / "ops/governed_memory/supabase_session_authority"
RUNNER = ROOT / "tools/governed_memory_validation/run_disposable_successor.sh"
VALIDATED_STATUS = "isolated_candidate_disposable_validated_not_production_applied"


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


class OwnerClaimDetailMigrationTests(unittest.TestCase):
    def test_package_hashes_and_manifest_report_current_truth(self) -> None:
        package_path = CLAIM_DETAIL / "package.json"
        package = _load_json(package_path)
        forward_path = CLAIM_DETAIL / "forward.pgsql"
        rollback_path = CLAIM_DETAIL / "rollback.pgsql"
        self.assertEqual(package["status"], VALIDATED_STATUS)
        self.assertTrue(package["activation"]["disposable_database_validated"])
        self.assertEqual(package["forward"]["sha256"], _sha256(forward_path))
        self.assertEqual(package["rollback"]["sha256"], _sha256(rollback_path))
        self.assertFalse(package["rollback"]["empty_only"])
        self.assertFalse(package["rollback"]["data_mutation"])

        manifest = _load_json(MIGRATIONS / "manifest.json")
        self.assertEqual(manifest["status"], package["status"])
        self.assertTrue(
            manifest["safety"]["disposable_database_execution_performed"]
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
                "0002_conversation_bridge/forward.pgsql",
            ],
        )
        self.assertEqual(
            manifest["rollback_order"],
            [
                "0002_conversation_bridge/rollback.pgsql",
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
        self.assertEqual(package["status"], VALIDATED_STATUS)
        self.assertEqual(schema["status"], package["status"])
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
                "production_applied": False,
            },
        )

    def test_runner_applies_and_rolls_back_exact_package_order(self) -> None:
        runner = RUNNER.read_text(encoding="utf-8")
        self.assertIn(
            "readonly EXPECTED_BASE='43ba1839233781f231195c4ff5051794494148c6'",
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
        self.assertNotIn("qdrant/qdrant:v1.11.0", runner)
        self.assertIn("{{json .RepoDigests}}", runner)
        self.assertIn('if sys.argv[2] not in json.loads(sys.argv[1]):', runner)
        apply = runner.split("apply_migrations() {", 1)[1].split(
            "rollback_migrations() {", 1
        )[0]
        rollback = runner.split("rollback_migrations() {", 1)[1].split(
            "verify_rollback_refuses_inflight_enqueue() {", 1
        )[0]
        apply_paths = [
            "roles_preflight.pgsql",
            "0001_foundation/forward.pgsql",
            "0003_owner_claim_detail/forward.pgsql",
            "0004_pilot_marker/forward.pgsql",
            "0002_conversation_bridge/forward.pgsql",
        ]
        rollback_paths = [
            "0002_conversation_bridge/rollback.pgsql",
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
        self.assertIn("--preliminary-disposable-proof", runner)
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
            "5aa7051078f7f619c4f705fd79d6817d9d6fc45005b6f8c1271d826b77227e91",
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

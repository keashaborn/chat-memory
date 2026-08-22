from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "build_platform_clean_baseline_v1.py"
if not SCRIPT.is_file():
    SCRIPT = ROOT.parent / "scripts" / "build_platform_clean_baseline_v1.py"
SPEC = importlib.util.spec_from_file_location("platform_clean_baseline", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def disposition_fixture() -> dict[str, object]:
    objects: list[dict[str, object]] = []
    retained_relations = [
        "ai_operations.monitor_incident_v1",
        "chat_history_private.clear_receipt",
        "chat_integrity.assistant_transcript_attestation_v1",
        "lifeswitch_usage.ai_usage_event_v1",
        "public.active_thread_selection",
        "public.chat_attachments",
        "public.chat_log",
        "public.telemetry_event",
        "public.threads",
        "public.voice_session_lease",
        "trusted_web.retrieval_monitor_hourly_v1",
        "user_settings.assistant_response_preference_v1",
    ]
    while len(retained_relations) < 21:
        retained_relations.append(f"trusted_web.synthetic_relation_{len(retained_relations)}")
    retained_functions = [
        "ai_operations.list_monitor_incidents_v1(p_state text, p_limit integer)",
        "chat_history_private.clear_history(p_operation_id uuid, p_scope text, p_thread_id uuid, p_recent_window_seconds integer)",
        "chat_integrity.synthetic_integrity_function_v1()",
        "lifeswitch_usage.current_actor_user_id()",
        "public.guard_canonical_owner()",
        "public.guard_chat_log_immutable()",
    ]
    while len(retained_functions) < 17:
        retained_functions.append(f"trusted_web.synthetic_function_{len(retained_functions)}()")
    for identity in retained_relations:
        objects.append({
            "baseline_action": "include",
            "classification": "application_direct",
            "disposition": "retained_active",
            "extension": None,
            "identity": identity,
            "object_type": "relation",
            "reason_code": "runtime_consumer_verified",
        })
    for identity in retained_functions:
        objects.append({
            "baseline_action": "include",
            "classification": "application_direct",
            "disposition": "retained_active",
            "extension": None,
            "identity": identity,
            "object_type": "function",
            "reason_code": "runtime_consumer_verified",
        })
    for index in range(36):
        objects.append({
            "baseline_action": "include",
            "classification": "extension_owned",
            "disposition": "retained_extension",
            "extension": "pgcrypto",
            "identity": f"public.synthetic_extension_function_{index}()",
            "object_type": "function",
            "reason_code": "retained_ai_operations_digest_dependency",
        })
    roles = []
    for name, expected in module.ROLE_TARGETS.items():
        disposition, action, target, reason = expected
        roles.append({
            "action": action,
            "can_login": name in {"brains_app", "sage"},
            "disposition": disposition,
            "name": name,
            "reason_code": reason,
            "source_bypass_rls": name == "sage",
            "source_superuser": name == "sage",
            "target_role": target or None,
        })
    extensions = [
        {
            "action": expected[1],
            "disposition": expected[0],
            "name": name,
            "reason_code": expected[2],
            "version": {"citext": "1.6", "pg_trgm": "1.6", "pgcrypto": "1.3", "plpgsql": "1.0", "unaccent": "1.1"}[name],
        }
        for name, expected in module.EXTENSION_DECISIONS.items()
    ]
    return {
        "baseline_blockers": [],
        "baseline_generation_allowed": True,
        "deletion_authority": False,
        "extensions": extensions,
        "memberships": [{
            "action": "rebuild",
            "admin_option": False,
            "granted": "lifeswitch_usage_writer_v1",
            "member": "brains_app",
            "target_granted": "seebx_usage_writer_v1",
            "target_member": "seebx_platform_app_v1",
        }],
        "object_count": len(objects),
        "objects": objects,
        "production_change_authority": False,
        "roles": roles,
        "schema_version": module.DISPOSITION_VERSION,
        "status": "candidate_baseline_ready",
    }


class PlatformCleanBaselineV1Tests(unittest.TestCase):
    def test_exact_disposition_derives_canonical_targets(self):
        plan = module.validate_disposition(disposition_fixture())
        self.assertEqual(len(plan["targets"]), 38)
        target_ids = {item["target_identity"] for item in plan["targets"]}
        self.assertIn("conversation.threads", target_ids)
        self.assertIn("conversation_private.clear_receipt", target_ids)
        self.assertIn("conversation_integrity.assistant_transcript_attestation_v1", target_ids)
        self.assertIn("usage.ai_usage_event_v1", target_ids)
        self.assertIn("telemetry.telemetry_event", target_ids)
        self.assertIn("voice.voice_session_lease", target_ids)
        self.assertEqual(plan["target_memberships"], [{
            "granted": "seebx_usage_writer_v1",
            "member": "seebx_platform_app_v1",
        }])

    def test_disposition_fails_closed_on_blocker_or_authority(self):
        manifest = disposition_fixture()
        manifest["baseline_blockers"] = [{"code": "drift"}]
        with self.assertRaisesRegex(module.BaselineContractError, "blockers_present"):
            module.validate_disposition(manifest)
        manifest = disposition_fixture()
        manifest["production_change_authority"] = True
        with self.assertRaisesRegex(module.BaselineContractError, "production_change_authority"):
            module.validate_disposition(manifest)

    def test_disposition_rejects_recorded_target_drift(self):
        manifest = disposition_fixture()
        manifest["objects"][0]["target_identity"] = "public.wrong"
        with self.assertRaisesRegex(module.BaselineContractError, "recorded_target_identity_changed"):
            module.validate_disposition(manifest)

    def test_restore_filter_keeps_only_retained_product_objects(self):
        plan = module.validate_disposition(disposition_fixture())
        lines = []
        number = 1
        for identity in plan["source_relations"]:
            schema, name = identity.split(".", 1)
            lines.append(f"{number}; 1259 {number} TABLE {schema} {name} owner")
            number += 1
            lines.append(f"{number}; 2606 {number} CONSTRAINT {schema} {name} {name}_pkey owner")
            number += 1
        for identity in plan["source_functions"]:
            schema, remainder = identity.split(".", 1)
            name = remainder.split("(", 1)[0]
            lines.append(f"{number}; 1255 {number} FUNCTION {schema} {name}() owner")
            number += 1
        lines.append(f"{number}; 1259 {number} TABLE memory retired owner")
        filtered, summary = module.filter_restore_list("\n".join(lines), plan)
        self.assertNotIn("memory retired", filtered)
        self.assertEqual(summary["ignored_toc_entries"], 1)
        self.assertEqual(summary["retained_toc_entries"], 21 * 2 + 17)

    def test_restore_filter_rejects_data_acl_or_missing_object(self):
        plan = module.validate_disposition(disposition_fixture())
        with self.assertRaisesRegex(module.BaselineContractError, "row_data"):
            module.filter_restore_list("1; 0 0 TABLE DATA public threads owner", plan)
        with self.assertRaisesRegex(module.BaselineContractError, "privileges"):
            module.filter_restore_list("1; 0 0 ACL public TABLE threads owner", plan)
        with self.assertRaisesRegex(module.BaselineContractError, "relation_missing"):
            module.filter_restore_list("1; 1259 1 TABLE public threads owner", plan)

    def test_schema_canonicalization_rewrites_namespaces_and_roles(self):
        plan = module.validate_disposition(disposition_fixture())
        source = []
        for identity in plan["source_relations"]:
            schema, name = identity.split(".", 1)
            if identity == "trusted_web.retrieval_monitor_hourly_v1":
                source.append(f"CREATE VIEW {schema}.{name} AS SELECT 1 AS value;")
            else:
                source.append(f"CREATE TABLE {schema}.{name} (owner_user_id uuid);")
        for index, identity in enumerate(plan["source_functions"]):
            base = identity.split("(", 1)[0]
            function_body = (
                "INSERT INTO trusted_web.synthetic_relation_12 DEFAULT VALUES;\n"
                if index == 0
                else ""
            )
            source.append(
                f"CREATE FUNCTION {base}() RETURNS text LANGUAGE sql AS $$\n"
                f"{function_body}SELECT 'brains_app'::text\n$$;"
            )
        canonical, kinds = module.canonicalize_schema_sql("\n".join(source), plan)
        self.assertIn("CREATE TABLE conversation.threads", canonical)
        self.assertIn("CREATE TABLE usage.ai_usage_event_v1", canonical)
        self.assertIn("CREATE TABLE telemetry.telemetry_event", canonical)
        self.assertIn("CREATE TABLE voice.voice_session_lease", canonical)
        self.assertIn("'seebx_platform_app_v1'", canonical)
        self.assertIn("INSERT INTO trusted_web.synthetic_relation_12", canonical)
        self.assertNotIn("chat_history_private.", canonical)
        self.assertEqual(kinds["trusted_web.retrieval_monitor_hourly_v1"], "VIEW")

    def test_schema_canonicalization_rejects_data_or_excluded_content(self):
        plan = module.validate_disposition(disposition_fixture())
        with self.assertRaisesRegex(module.BaselineContractError, "row_data"):
            module.canonicalize_schema_sql("COPY public.threads FROM stdin;", plan)
        with self.assertRaisesRegex(module.BaselineContractError, "excluded_public_object"):
            module.canonicalize_schema_sql("CREATE TABLE public.vs_profiles ();", plan)

    def test_bootstrap_has_six_least_privilege_roles_and_writer_only_membership(self):
        sql = module.build_roles_sql()
        self.assertEqual(sql.count("CREATE ROLE"), 6)
        self.assertNotIn("SUPERUSER", sql.replace("NOSUPERUSER", ""))
        self.assertNotIn("BYPASSRLS", sql.replace("NOBYPASSRLS", ""))
        self.assertNotIn("PASSWORD", sql)
        self.assertIn("GRANT seebx_usage_writer_v1 TO seebx_platform_app_v1;", sql)
        self.assertNotIn("GRANT seebx_usage_admin_v1 TO seebx_platform_app_v1;", sql)

    def test_namespaces_retain_only_pgcrypto_and_plpgsql_contract(self):
        sql = module.build_namespaces_sql()
        self.assertIn("CREATE EXTENSION pgcrypto", sql)
        self.assertIn("extname='plpgsql' AND extversion='1.0'", sql)
        self.assertNotIn("citext", sql)
        self.assertNotIn("pg_trgm", sql)
        self.assertNotIn("unaccent", sql)
        for item in module.TARGET_SCHEMA_CONTRACTS:
            self.assertIn(f"CREATE SCHEMA {item['name']} AUTHORIZATION {item['owner']};", sql)


if __name__ == "__main__":
    unittest.main()

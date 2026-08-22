from __future__ import annotations

import importlib.util
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_platform_database_disposition_v1.py"
SPEC = importlib.util.spec_from_file_location("platform_database_disposition", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def object_entry(
    identity: str,
    classification: str,
    *,
    extension: str = "",
    object_type: str = "relation",
) -> dict[str, object]:
    return {
        "classification": classification,
        "extension": extension,
        "identity": identity,
        "object_type": object_type,
        "relation_kind": "r" if object_type == "relation" else "",
    }


def audit_fixture() -> dict[str, object]:
    objects = [
        object_entry("public.chat_log", "application_direct"),
        object_entry("chat_history_private.helper()", "database_internal_reachable", object_type="function"),
        object_entry("memory.old_table", "unproven"),
        object_entry("memory_ingest_private.old_outbox", "database_internal_reachable"),
        object_entry("catalog_dev.exercise", "migration_only"),
        object_entry("public.gen_random_uuid()", "extension_owned", extension="pgcrypto", object_type="function"),
        object_entry("public.citextin(cstring)", "extension_owned", extension="citext", object_type="function"),
    ]
    for identity in sorted(module.ACTIVE_OPERATIONAL_OBJECTS):
        objects.append(object_entry(identity, "operational_reference_only", object_type="function"))
    for identity in sorted(module.FORMS_OBJECTS):
        objects.append(object_entry(identity, "operational_reference_only"))
    for identity in sorted(module.LEGACY_PUBLIC_OBJECTS):
        objects.append(object_entry(identity, "unproven"))
    for identity in sorted(module.USAGE_REGISTRY_OBJECTS):
        objects.append(object_entry(
            identity,
            "migration_only",
            object_type="function" if identity.endswith("()") else "relation",
        ))
    table_counts = []
    for index, item in enumerate(objects, 1):
        if item["object_type"] != "relation" or str(item["identity"]).endswith("_seq"):
            continue
        schema, relation = str(item["identity"]).split(".", 1)
        table_counts.append({"schema": schema, "relation": relation, "row_count": index})
    roles = [
        {"name": "brains_app", "can_login": True, "superuser": False, "bypass_rls": False},
        {"name": "ai_operations_store_v1", "can_login": False, "superuser": False, "bypass_rls": False},
        {"name": "lifeswitch_usage_admin_v1", "can_login": False, "superuser": False, "bypass_rls": False},
        {"name": "lifeswitch_usage_writer_v1", "can_login": False, "superuser": False, "bypass_rls": False},
        {"name": "sage", "can_login": True, "superuser": True, "bypass_rls": True},
        {"name": "memory_ingest_writer", "can_login": False, "superuser": False, "bypass_rls": False},
        {"name": "lifeswitch_chat_reader_v1", "can_login": False, "superuser": False, "bypass_rls": False},
    ]
    memberships = [
        {"member": "brains_app", "granted": "lifeswitch_usage_writer_v1", "admin_option": False},
        {"member": "brains_app", "granted": "memory_ingest_writer", "admin_option": False},
    ]
    sections = {
        "columns": [],
        "constraints": [],
        "extensions": [
            {"name": name, "version": "1.0"}
            for name in sorted(module.EXTENSION_DECISIONS)
        ],
        "functions": [],
        "indexes": [],
        "memberships": memberships,
        "policies": [],
        "relations": [],
        "roles": roles,
        "schemas": [],
        "sequences": [],
        "table_counts": table_counts,
        "triggers": [],
    }
    governance_body = {
        "database": {"container": "fixture", "name": "fixture"},
        "schema_version": module.GOVERNANCE_SCHEMA_VERSION,
        "scope": {"row_content_included": False},
        "section_hashes": {
            name: module.sha256_bytes(module.canonical_bytes(rows))
            for name, rows in sorted(sections.items())
        },
        "sections": sections,
    }
    governance = {
        **governance_body,
        "manifest_sha256": module.sha256_bytes(module.canonical_bytes(governance_body)),
    }
    return {
        "candidate_commit": "a" * 40,
        "governance_manifest": governance,
        "object_catalog_sha256": "b" * 64,
        "object_count": len(objects),
        "objects": objects,
        "schema_version": module.AUDIT_SCHEMA_VERSION,
        "scope": {
            "deletion_authority": False,
            "governance_manifest_included": True,
            "operational_references_are_retention_seeds": False,
        },
        "status": "pass",
    }


class PlatformDatabaseDispositionV1Tests(unittest.TestCase):
    def test_manifest_is_complete_and_remains_fail_closed(self) -> None:
        manifest = module.build_manifest(audit_fixture(), "c" * 64)
        self.assertFalse(manifest["baseline_generation_allowed"])
        self.assertFalse(manifest["deletion_authority"])
        self.assertFalse(manifest["production_change_authority"])
        self.assertEqual(manifest["status"], "candidate_review_gate")
        blocker_codes = {item["code"] for item in manifest["baseline_blockers"]}
        self.assertEqual(blocker_codes, {"legacy_ingest_dependencies_present"})
        dispositions = {item["identity"]: item["disposition"] for item in manifest["objects"]}
        self.assertEqual(dispositions["public.chat_log"], "retained_active")
        self.assertEqual(
            dispositions["ai_operations.enforce_telemetry_retention_v1()"],
            "retained_operations",
        )
        self.assertEqual(dispositions["memory.old_table"], "archive_legacy_memory")
        self.assertEqual(
            dispositions["memory_ingest_private.old_outbox"],
            "archive_legacy_ingest",
        )
        self.assertEqual(dispositions["public.vb_form_entries"], "migrate_lifeswitch_forms")
        objects = {item["identity"]: item for item in manifest["objects"]}
        self.assertEqual(objects["public.gen_random_uuid()"]["extension"], "pgcrypto")
        self.assertEqual(objects["public.gen_random_uuid()"]["baseline_action"], "include")
        self.assertEqual(objects["public.citextin(cstring)"]["extension"], "citext")
        self.assertEqual(objects["public.citextin(cstring)"]["baseline_action"], "exclude")

        extensions = {item["name"]: item for item in manifest["extensions"]}
        self.assertEqual(extensions["pgcrypto"]["action"], "include")
        self.assertEqual(extensions["plpgsql"]["action"], "include")
        self.assertEqual(extensions["citext"]["action"], "exclude")
        self.assertEqual(extensions["pg_trgm"]["action"], "exclude")
        self.assertEqual(extensions["unaccent"]["action"], "exclude")
        self.assertNotIn("hold", {item["action"] for item in manifest["extensions"]})

    def test_unknown_object_and_role_fail_closed(self) -> None:
        audit = audit_fixture()
        audit["objects"].append(object_entry("public.unknown", "unproven"))
        audit["object_count"] = len(audit["objects"])
        with self.assertRaisesRegex(
            module.DispositionError,
            "unresolved_object_without_exact_disposition",
        ):
            module.build_manifest(audit, "c" * 64)
        audit = audit_fixture()
        roles = audit["governance_manifest"]["sections"]["roles"]
        roles.append({"name": "unexpected", "can_login": False})
        audit["governance_manifest"]["section_hashes"]["roles"] = module.sha256_bytes(
            module.canonical_bytes(roles)
        )
        body = {
            key: value
            for key, value in audit["governance_manifest"].items()
            if key != "manifest_sha256"
        }
        audit["governance_manifest"]["manifest_sha256"] = module.sha256_bytes(
            module.canonical_bytes(body)
        )
        with self.assertRaisesRegex(
            module.DispositionError,
            "unknown_role_without_exact_disposition",
        ):
            module.build_manifest(audit, "c" * 64)

    def test_governance_hash_and_audit_hash_are_exact(self) -> None:
        audit = audit_fixture()
        audit["governance_manifest"]["sections"]["roles"][0]["can_login"] = False
        with self.assertRaisesRegex(module.DispositionError, "governance_sha256_mismatch"):
            module.build_manifest(audit, "c" * 64)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "audit.json"
            path.write_text(json.dumps(audit_fixture()), encoding="utf-8")
            with self.assertRaisesRegex(module.DispositionError, "audit_sha256_mismatch"):
                module.read_audit(path, "d" * 64)

    def test_output_is_private_and_exclusive(self) -> None:
        manifest = module.build_manifest(audit_fixture(), "c" * 64)
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "manifest.json"
            module.write_exclusive(output, manifest)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            with self.assertRaisesRegex(module.DispositionError, "output_create_failed"):
                module.write_exclusive(output, manifest)


if __name__ == "__main__":
    unittest.main()

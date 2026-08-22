from __future__ import annotations

import importlib.util
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_lifeswitch_database_disposition_v1.py"
SPEC = importlib.util.spec_from_file_location("database_disposition", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def object_entry(identity: str, classification: str) -> dict[str, object]:
    return {
        "classification": classification,
        "identity": identity,
        "object_type": "relation",
    }


def audit_fixture() -> dict[str, object]:
    objects = [
        object_entry("lifeswitch_plan.plan_profile", "application_direct"),
        object_entry("lifeswitch_plan.plan_version", "database_internal_reachable"),
        object_entry("public.spatial_ref_sys", "extension_owned"),
    ]
    for identity, decision in module.EXPLICIT_DISPOSITIONS.items():
        objects.append(object_entry(identity, decision[0]))
    return {
        "candidate_commit": "a" * 40,
        "object_catalog_sha256": "b" * 64,
        "object_count": len(objects),
        "objects": objects,
        "schema_version": module.AUDIT_SCHEMA_VERSION,
        "scope": {
            "deletion_authority": False,
            "operational_references_are_retention_seeds": False,
        },
        "source_manifest_sha256": "c" * 64,
        "status": "pass",
    }


class LifeSwitchDatabaseDispositionV1Tests(unittest.TestCase):
    def test_exact_explicit_objects_and_proven_objects_are_partitioned(self) -> None:
        manifest = module.build_manifest(audit_fixture(), "d" * 64)
        self.assertTrue(manifest["baseline_generation_allowed"])
        self.assertFalse(manifest["deletion_authority"])
        self.assertFalse(manifest["production_change_authority"])
        self.assertEqual(
            manifest["disposition_counts"],
            {
                "archive_candidate": 6,
                "recovery_only": 2,
                "retained_active": 1,
                "retained_canonical_product_data": 3,
                "retained_dependency": 1,
                "retained_extension": 1,
            },
        )
        self.assertEqual(manifest["status"], "candidate_baseline_ready")

    def test_unknown_unresolved_object_fails_closed(self) -> None:
        audit = audit_fixture()
        audit["objects"].append(object_entry("lifeswitch_plan.unknown", "unproven"))
        audit["object_count"] = len(audit["objects"])
        with self.assertRaisesRegex(
            module.DispositionError, "unresolved_object_without_exact_disposition"
        ):
            module.build_manifest(audit, "d" * 64)

    def test_operational_references_must_not_seed_retention(self) -> None:
        audit = audit_fixture()
        audit["scope"]["operational_references_are_retention_seeds"] = True
        with self.assertRaisesRegex(
            module.DispositionError, "audit_operational_seed_contract_invalid"
        ):
            module.build_manifest(audit, "d" * 64)

    def test_audit_hash_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "audit.json"
            path.write_text(json.dumps(audit_fixture()), encoding="utf-8")
            with self.assertRaisesRegex(module.DispositionError, "audit_sha256_mismatch"):
                module.read_audit(path, "d" * 64)

    def test_output_is_private_and_exclusive(self) -> None:
        manifest = module.build_manifest(audit_fixture(), "d" * 64)
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "manifest.json"
            module.write_exclusive(output, manifest)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            with self.assertRaisesRegex(module.DispositionError, "output_create_failed"):
                module.write_exclusive(output, manifest)


if __name__ == "__main__":
    unittest.main()

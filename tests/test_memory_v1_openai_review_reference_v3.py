from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


class OpenAIReviewReferenceV3Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.package_root = cls.root / "governed-function-migrations" / "memory_openai_review_reference_authority_v3"
        cls.package_bytes = (cls.package_root / "package.json").read_bytes()
        cls.package = json.loads(cls.package_bytes)
        cls.forward = "\n".join(
            path.read_text() for path in sorted(cls.package_root.glob("*-forward.pgsql"))
        )
        cls.rollback = "\n".join(
            path.read_text() for path in sorted(cls.package_root.glob("*-rollback.pgsql"))
        )

    def test_package_is_registered_and_hash_bound(self) -> None:
        registry = json.loads(
            (self.root / "governed-migration-ci/registry/governed-function-migrations-v1.json").read_text()
        )
        record = next(item for item in registry["packages"] if item["migration_id"] == "memory_openai_review_reference_authority_v3")
        material = {
            "manifest_sha256": hashlib.sha256(self.package_bytes).hexdigest(),
            "relation_forward_sha256": self.package["relations"]["forward_sha256"],
            "relation_rollback_sha256": self.package["relations"]["rollback_sha256"],
            "function_forward_sha256": [
                function["forward_sha256"] for function in self.package["functions"]
            ],
            "function_rollback_sha256": [
                function["rollback_sha256"] for function in self.package["functions"]
            ],
            "forward_recovery_sha256": self.package["recovery"]["forward_recovery_sha256"],
        }
        package_sha256 = hashlib.sha256(
            (json.dumps(material, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest()
        self.assertEqual(record["package_sha256"], package_sha256)
        self.assertEqual(len(self.package["functions"]), 3)
        for function in self.package["functions"]:
            self.assertNotEqual(
                function["prior_definition_sha256"], function["expected_definition_sha256"]
            )

    def test_forward_uses_immutable_packet_references(self) -> None:
        self.assertIn("observation_ref:=observation->>'observation_ref'", self.forward)
        self.assertIn("subject_entity_ref:=observation->>'subject_entity_ref'", self.forward)
        self.assertIn("ARRAY['observation_ids',observation_ref]", self.forward)
        self.assertIn("ARRAY['resolution_ids',subject_entity_ref]", self.forward)
        self.assertNotIn("{observation_ids,o01}", self.forward)
        self.assertNotIn("{resolution_ids,e01}", self.forward)
        self.assertIn("observation_ref !~ '^o[0-9]{2}$'", self.forward)
        self.assertIn("subject_entity_ref !~ '^e[0-9]{2}$'", self.forward)

    def test_scope_preserves_security_and_has_no_external_or_destructive_capability(self) -> None:
        self.assertEqual(self.forward.count("SECURITY DEFINER"), 3)
        self.assertEqual(self.forward.count("SET search_path TO pg_catalog"), 3)
        self.assertEqual(self.forward.count("GRANT EXECUTE ON FUNCTION"), 6)
        self.assertEqual(self.forward.count(" FROM brains_app;"), 3)
        self.assertEqual(self.forward.count(" FROM memory_v5_writer;"), 3)
        for forbidden in (
            "DELETE FROM",
            "TRUNCATE",
            "openai.responses",
            "http://",
            "https://",
            "memory_raw",
            "qdrant_client",
        ):
            self.assertNotIn(forbidden, self.forward.lower() if forbidden.islower() else self.forward)

    def test_rollback_restores_exact_original_reference_boundary(self) -> None:
        self.assertEqual(self.rollback.count("CREATE OR REPLACE FUNCTION"), 3)
        self.assertIn("observation->>'observation_ref'<>'o01'", self.rollback)
        self.assertIn("{observation_ids,o01}", self.rollback)
        self.assertIn("{resolution_ids,e01}", self.rollback)
        self.assertNotIn("ARRAY['observation_ids',observation_ref]", self.rollback)


if __name__ == "__main__":
    unittest.main()

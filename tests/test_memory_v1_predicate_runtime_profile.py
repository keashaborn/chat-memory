from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.memory_v1_predicate_registry_v5_1 import provider_registry
from scripts.memory_v1_predicate_runtime_profile import load_runtime_profile


class PredicateRuntimeProfileTest(unittest.TestCase):
    def test_exact_profiles_are_hash_bound(self) -> None:
        v5 = load_runtime_profile(ROOT, "v5")
        v5_1 = load_runtime_profile(ROOT, "v5_1")
        self.assertEqual(v5.registry_version, "memory_predicate_registry_v5")
        self.assertEqual(
            v5.contract_version,
            "memory_v1_relational_extraction_v5",
        )
        self.assertEqual(
            v5_1.registry_version,
            "memory_predicate_registry_v5_1",
        )
        self.assertEqual(
            v5_1.contract_version,
            "memory_v1_relational_extraction_v5_1",
        )
        self.assertEqual(v5.lifecycle, "legacy_replay")
        self.assertEqual(v5_1.lifecycle, "shadow_review_staging")

    def test_generated_provider_registry_matches_bound_artifact(self) -> None:
        expected = provider_registry(
            ROOT / "specs/memory_v1_predicate_registry_v5_1_integration.json"
        )
        artifact = json.loads(
            (ROOT / "specs/memory_v1_predicate_registry_v5_1.json").read_text()
        )
        self.assertEqual(artifact, expected)
        self.assertEqual(len(artifact["predicates"]), 63)
        self.assertEqual(len(artifact["legacy_compatibility"]), 19)

    def test_unknown_profile_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            load_runtime_profile(ROOT, "latest")

    def test_registry_tamper_fails_before_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copy_root = Path(temporary)
            (copy_root / "specs").mkdir()
            for path in (ROOT / "specs").iterdir():
                if path.is_file():
                    (copy_root / "specs" / path.name).write_bytes(path.read_bytes())
            target = copy_root / "specs/memory_v1_predicate_registry_v5_1.json"
            target.write_bytes(target.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "registry SHA-256 mismatch"):
                load_runtime_profile(copy_root, "v5_1")


if __name__ == "__main__":
    unittest.main()


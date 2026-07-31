from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.memory_v1_predicate_runtime_profile import load_runtime_profile
from scripts.memory_v1_predicate_runtime_profile_v2 import load_runtime_profile_v2


class PredicateRuntimeProfileV2Test(unittest.TestCase):
    def test_v5_2_is_bound_for_private_shadow_staging(self) -> None:
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        self.assertEqual(profile.lifecycle, "shadow_review_staging")
        self.assertEqual(
            profile.contract_version,
            "memory_v1_relational_extraction_v5_2",
        )
        self.assertEqual(
            profile.registry_version,
            "memory_predicate_registry_v5_2",
        )
        self.assertFalse(profile.review_only)

    def test_existing_runtime_loader_cannot_select_v5_2(self) -> None:
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            load_runtime_profile(ROOT, "v5_2")

    def test_v2_manifest_preserves_active_scheduler_target(self) -> None:
        v5 = load_runtime_profile_v2(ROOT, "v5")
        v5_1 = load_runtime_profile_v2(ROOT, "v5_1")
        self.assertFalse(v5.review_only)
        self.assertFalse(v5_1.review_only)
        self.assertEqual(v5.lifecycle, "legacy_replay")
        self.assertEqual(v5_1.lifecycle, "shadow_review_staging")

    def test_v5_2_registry_tamper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copy_root = Path(temporary)
            shutil.copytree(ROOT / "specs", copy_root / "specs")
            target = copy_root / (
                "specs/memory_v1_predicate_registry_v5_2_compiler_v12.json"
            )
            target.write_bytes(target.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "registry SHA-256 mismatch"):
                load_runtime_profile_v2(copy_root, "v5_2")

    def test_unknown_profile_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            load_runtime_profile_v2(ROOT, "latest")


if __name__ == "__main__":
    unittest.main()

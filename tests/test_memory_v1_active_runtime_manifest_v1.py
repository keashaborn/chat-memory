from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "ops/systemd/memory-v1-active-runtime-manifest-v1.json"


class ActiveRuntimeManifestTests(unittest.TestCase):
    def test_manifest_is_closed_inactive_and_postgres_authoritative(self) -> None:
        value = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(
            value["contract_version"], "memory_v1_active_runtime_manifest_v1"
        )
        self.assertEqual(value["activation_state"], "candidate_only_inactive")
        self.assertEqual(value["canonical_authority"]["postgresql"], "authoritative")
        self.assertEqual(value["canonical_authority"]["qdrant"], "derived_rebuildable")
        self.assertFalse(value["first_vertical_slice"]["backlog_drain"])
        self.assertFalse(
            value["first_vertical_slice"]["automatic_claim_promotion"]
        )
        self.assertEqual(value["first_vertical_slice"]["jobs_maximum"], 1)
        self.assertEqual(
            value["first_vertical_slice"]["external_provider_calls_maximum"], 1
        )

    def test_every_declared_entrypoint_and_unit_exists_and_is_inactive(self) -> None:
        value = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for service in value["services"]:
            self.assertFalse(service["installed"])
            self.assertTrue((ROOT / service["entrypoint"]).is_file())
            unit = ROOT / "ops/systemd" / service["unit"]
            timer = ROOT / "ops/systemd" / service["timer"]
            self.assertTrue(unit.is_file())
            self.assertTrue(timer.is_file())
            timer_source = timer.read_text(encoding="utf-8")
            self.assertIn(
                f"ConditionPathExists={service['timer_enable_sentinel']}",
                timer_source,
            )

    def test_new_runtime_has_no_legacy_worker_or_review_filesystem_dependency(self) -> None:
        extraction = (
            ROOT / "scripts/memory_v1_openai_extraction_worker_v1.py"
        ).read_text(encoding="utf-8")
        router = (
            ROOT / "scripts/memory_v1_openai_v5_2_packet_router_v1.py"
        ).read_text(encoding="utf-8")
        router_unit = (
            ROOT / "ops/systemd/memory-v1-openai-v5-2-packet-router.service"
        ).read_text(encoding="utf-8")
        self.assertNotIn("memory_v1_v5_bounded_extraction_worker", extraction)
        self.assertNotIn("memory-v1-reviews", router)
        self.assertNotIn("ReadWritePaths=", router_unit)


if __name__ == "__main__":
    unittest.main()

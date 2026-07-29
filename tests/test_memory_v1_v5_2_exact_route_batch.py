from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.memory_v1_v5_2_exact_route_batch import load_manifest


class ExactRouteBatchTest(unittest.TestCase):
    def write_manifest(self, value: dict) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "manifest.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def valid_manifest(self) -> dict:
        return {
            "contract_version": "memory_v1_contextual_exact_23_route_v1",
            "owner_user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
            "expected_counts": {
                "packets": 1,
                "route_events": 1,
                "model_calls": 0,
            },
            "packets": [
                {
                    "packet_id": "9ba9905c-a05a-5c63-9a33-a327c11e42cf",
                    "job_id": "05a39cdd-9234-4836-91c8-2e2eb9b3115c",
                    "evidence_id": "5e3359f6-f742-5895-8f40-e5c6be16b865",
                    "packet_storage_sha256": "a" * 64,
                }
            ],
        }

    def test_load_manifest_accepts_hash_bound_unique_packet(self) -> None:
        owner, packets, expected = load_manifest(
            self.write_manifest(self.valid_manifest())
        )
        self.assertEqual(str(owner), "1240822d-ac9a-4096-95aa-e2b24d36ef50")
        self.assertEqual(len(packets), 1)
        self.assertEqual(expected["model_calls"], 0)

    def test_load_manifest_rejects_duplicate_packet(self) -> None:
        value = self.valid_manifest()
        value["packets"].append(dict(value["packets"][0]))
        value["expected_counts"]["packets"] = 2
        value["expected_counts"]["route_events"] = 2
        with self.assertRaisesRegex(RuntimeError, "duplicated"):
            load_manifest(self.write_manifest(value))


if __name__ == "__main__":
    unittest.main()

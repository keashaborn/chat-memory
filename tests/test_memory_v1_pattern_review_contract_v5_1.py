from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "specs/memory_v1_pattern_review_v5_1.schema.json"


def walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


class PatternReviewContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    def test_every_object_is_closed(self) -> None:
        for node in walk(self.schema):
            if isinstance(node, dict) and node.get("type") == "object":
                self.assertIs(node.get("additionalProperties"), False)

    def test_owner_identity_is_not_caller_supplied(self) -> None:
        property_names: set[str] = set()
        for node in walk(self.schema):
            if isinstance(node, dict) and isinstance(node.get("properties"), dict):
                property_names.update(node["properties"])
        self.assertNotIn("owner_user_id", property_names)
        self.assertNotIn("vantage_id", property_names)

    def test_truth_and_scalar_salience_are_not_canonical_fields(self) -> None:
        property_names: set[str] = set()
        for node in walk(self.schema):
            if isinstance(node, dict) and isinstance(node.get("properties"), dict):
                property_names.update(node["properties"])
        self.assertTrue(
            property_names.isdisjoint(
                {"truth", "truth_score", "salience", "overall_score", "final_score"}
            )
        )

    def test_identity_and_causality_inference_remain_forbidden(self) -> None:
        properties = self.schema["properties"]
        self.assertIs(properties["identity_inference_forbidden"]["const"], True)
        self.assertIs(properties["causal_inference_forbidden"]["const"], True)

    def test_observation_items_are_exact_and_hash_locked(self) -> None:
        item = self.schema["$defs"]["observation_item"]
        self.assertEqual(set(item["required"]), set(item["properties"]))
        self.assertEqual(item["additionalProperties"], False)
        self.assertTrue(
            {
                "observation_sha256",
                "evidence_content_sha256",
                "episode_key_sha256",
                "independence_key_sha256",
                "temporal_bucket_sha256",
            }.issubset(item["properties"])
        )


if __name__ == "__main__":
    unittest.main()

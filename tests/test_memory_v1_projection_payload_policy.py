from __future__ import annotations

from datetime import datetime, timezone
import unittest
from uuid import UUID

from rag_engine.memory_v1_projection import projection_payload


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


def snapshot(surface_policy: str) -> dict[str, object]:
    return {
        "claim_id": UUID("f4688838-7193-4e0e-961f-c9bbbf9904c7"),
        "revision_number": 2,
        "status": "supported",
        "predicate": "relationship.caregiver_for",
        "sensitivity": "high",
        "retrieval_policy": {"surface_policy": surface_policy},
        "updated_at": datetime(2026, 7, 25, tzinfo=timezone.utc),
    }


class ProjectionPayloadPolicyTest(unittest.TestCase):
    def test_surface_policy_is_preserved_in_qdrant_payload(self) -> None:
        payload = projection_payload(OWNER, snapshot("direct_or_relevant"))
        self.assertEqual(payload["surface"], "direct_or_relevant")
        self.assertFalse(payload["requires_explicit"])

    def test_explicit_only_policy_sets_explicit_candidate_hint(self) -> None:
        payload = projection_payload(OWNER, snapshot("explicit_recall_only"))
        self.assertEqual(payload["surface"], "explicit_recall_only")
        self.assertTrue(payload["requires_explicit"])


if __name__ == "__main__":
    unittest.main()

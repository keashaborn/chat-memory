from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import uuid

from scripts.memory_v1_relational_extraction_v5_provider import canonical_sha256
from scripts.memory_v1_v5_build_local_review_deferral import CONTRACT, REASON, decision_body
from scripts.memory_v1_v5_local_review_deferral import load_decision, operation_ids


class LocalReviewDeferralTest(unittest.TestCase):
    def decision(self) -> dict:
        value = {
            "contract_version": CONTRACT,
            "decision_sha256": "",
            "owner_user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
            "packet_id": "7357397a-26a3-5d19-aee4-f7284509cf3f",
            "packet_storage_sha256": "a" * 64,
            "evidence_content_sha256": "b" * 64,
            "validator_packet_sha256": "c" * 64,
            "review_decision": "deferred",
            "reason_code": REASON,
            "promotion_eligible": False,
            "reviewer_type": "owner_authorized_operator",
            "reviewer_ref": "user_authorized_2026-07-21",
            "source_prose_included": False,
            "external_model_calls": 0,
            "database_writes": 0,
            "qdrant_writes": 0,
            "prompt_influence": 0,
        }
        value["decision_sha256"] = canonical_sha256(decision_body(value))
        return value

    def test_exact_decision_and_identities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decision.json"
            path.write_text(json.dumps(self.decision(), sort_keys=True) + "\n")
            path.chmod(0o600)
            value, file_sha = load_decision(str(path))
            self.assertEqual(value["reason_code"], REASON)
            self.assertEqual(len(file_sha), 64)
            owner = uuid.UUID(value["owner_user_id"])
            packet = uuid.UUID(value["packet_id"])
            first = operation_ids(owner, packet, value["decision_sha256"])
            second = operation_ids(owner, packet, value["decision_sha256"])
            self.assertEqual(first, second)
            self.assertNotEqual(first[0], first[1])

    def test_promotion_or_source_prose_fails_closed(self) -> None:
        for key, replacement in (
            ("promotion_eligible", True),
            ("source_prose_included", True),
            ("reason_code", "unpopular_belief"),
        ):
            value = self.decision()
            value[key] = replacement
            value["decision_sha256"] = canonical_sha256(decision_body(value))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "decision.json"
                path.write_text(json.dumps(value, sort_keys=True) + "\n")
                path.chmod(0o600)
                with self.assertRaises(RuntimeError):
                    load_decision(str(path))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
import unittest
import uuid

from scripts.memory_v1_v5_2_canary_rubric_evaluate import _failures


ROOT = Path(__file__).resolve().parents[1]
RUBRIC = json.loads(
    (
        ROOT / "evals" / "memory_v1_v5_2_nuanced_belief_canary_20260722.json"
    ).read_text(encoding="utf-8")
)
OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


def stance(ref: str) -> dict:
    return {
        "observation_ref": ref,
        "predicate": "stance.reported",
        "modality": "reported_belief",
        "projection_class": "reported_stance",
        "surface_policy": "relevant_recall_or_explicit_recall",
        "sensitivity": "medium",
        "object": {
            "kind": "literal",
            "datatype": "json",
            "value": {
                "context": None,
                "orientation": "supports",
                "position": "redacted fixture position",
                "topic_key": "fixture.topic",
                "topic_text": "redacted fixture topic",
            },
        },
    }


def fixture() -> tuple[dict, dict]:
    binding = RUBRIC["source_binding"]
    packet = {
        "contract_version": "memory_v1_relational_extraction_v5_2",
        "predicate_registry_version": "memory_predicate_registry_v5_2",
        "source_envelope": {
            "job_id": binding["job_id"],
            "source_sha256": binding["evidence_content_sha256"],
        },
        "entity_mentions": [{"mention_kind": "self_reference"}],
        "observations": [stance("o01"), stance("o02")],
        "comparison_hints": [],
        "deferrals": [],
    }
    row = {
        "job_id": uuid.UUID(binding["job_id"]),
        "evidence_id": uuid.UUID(binding["evidence_id"]),
        "evidence_content_sha256": binding["evidence_content_sha256"],
        "evidence_authority_sha256": binding["evidence_content_sha256"],
        "evidence_status": "active",
        "storage_integrity_verified": True,
        "local_model_calls": 1,
        "external_model_calls": 0,
        "exact_stage_batch_count": 0,
        "evidence_stage_batch_count": 0,
        "entity_mention_count": 1,
        "observation_count": 2,
        "comparison_hint_count": 0,
        "deferral_count": 0,
    }
    return row, packet


class CanaryRubricEvaluatorTest(unittest.TestCase):
    def test_expected_packet_passes_without_prose_output(self) -> None:
        row, packet = fixture()
        failures, predicates, deferrals = _failures(
            rubric=RUBRIC, owner=OWNER, row=row, packet=packet
        )
        self.assertEqual(failures, [])
        self.assertEqual(predicates, {"stance.reported": 2})
        self.assertEqual(deferrals, {})

    def test_health_fact_and_transcription_deferral_fail_closed(self) -> None:
        row, packet = fixture()
        packet["observations"].append(
            {**stance("o03"), "predicate": "health.user_reported_observation"}
        )
        packet["deferrals"].append({"reason_code": "ambiguous_transcription"})
        row["observation_count"] = 3
        row["deferral_count"] = 1
        failures, _, _ = _failures(
            rubric=RUBRIC, owner=OWNER, row=row, packet=packet
        )
        self.assertIn("unexpected_predicate", failures)
        self.assertIn("forbidden_predicate", failures)
        self.assertIn("forbidden_deferral", failures)
        self.assertIn("unexpected_deferral", failures)

    def test_unlisted_predicate_and_low_sensitivity_fail_closed(self) -> None:
        row, packet = fixture()
        packet["observations"][0]["predicate"] = "identity.name"
        packet["observations"][1]["sensitivity"] = "low"
        failures, _, _ = _failures(
            rubric=RUBRIC, owner=OWNER, row=row, packet=packet
        )
        self.assertIn("unexpected_predicate", failures)
        self.assertIn("predicate_count_failed:stance.reported", failures)
        self.assertIn("stance_sensitivity_failed", failures)


if __name__ == "__main__":
    unittest.main()

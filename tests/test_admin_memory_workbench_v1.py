from datetime import datetime, timezone
import unittest
from uuid import UUID

from rag_engine.admin_memory_workbench_v1 import _workbench_item


NOW = datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc)


class AdminMemoryWorkbenchTests(unittest.TestCase):
    def test_item_exposes_source_and_plain_language_gpu_interpretation(self):
        item = _workbench_item(
            {
                "packet_id": UUID("11111111-1111-4111-8111-111111111111"),
                "packet_storage_sha256": "a" * 64,
                "source_content": "My name is Avery.",
                "source_recorded_at": NOW,
                "source_context_content": (
                    "Before the target. My name is Avery. After the target."
                ),
                "source_char_start": 19,
                "source_char_end": 36,
                "packet_created_at": NOW,
                "manual_review_required": True,
                "normalized_packet": {
                    "entity_mentions": [
                        {
                            "entity_ref": "e00",
                            "entity_type": "self",
                            "name_text": None,
                            "reason_codes": ["deterministic_self_link"],
                        }
                    ],
                    "observations": [
                        {
                            "subject_entity_ref": "e00",
                            "predicate": "identity.name_canonical",
                            "object": {"kind": "literal", "value": "Avery"},
                            "polarity": "affirmed",
                            "extraction_confidence": 0.99,
                            "reason_codes": ["explicit_self_name"],
                        }
                    ],
                    "deferrals": [],
                },
                "route": "manual_review_artifact_ready",
                "route_reason_code": "reviewable_relational_packet_v5_2",
                "feedback_id": None,
                "feedback_decision": None,
                "feedback_category": None,
                "feedback_note": None,
                "feedback_created_at": None,
            }
        )

        self.assertEqual(item["source"]["text"], "My name is Avery.")
        self.assertEqual(
            item["gpu"]["interpretations"][0]["summary"],
            "Your canonical name is Avery.",
        )
        self.assertNotIn("owner_user_id", str(item))
        self.assertNotIn("normalized_packet", str(item))
        self.assertTrue(item["source"]["context"]["available"])
        self.assertEqual(
            item["source"]["context"]["text"],
            "Before the target. My name is Avery. After the target.",
        )
        self.assertEqual(
            item["gpu"]["diagnostic_json"]["observations"][0]["predicate"],
            "identity.name_canonical",
        )
        self.assertNotIn(
            "source_envelope",
            item["gpu"]["diagnostic_json"],
        )

    def test_deferral_is_explained_without_raw_packet_exposure(self):
        item = _workbench_item(
            {
                "packet_id": UUID("22222222-2222-4222-8222-222222222222"),
                "packet_storage_sha256": "b" * 64,
                "source_content": "How are my workouts going?",
                "source_recorded_at": NOW,
                "source_context_content": "How are my workouts going?",
                "source_char_start": 0,
                "source_char_end": 26,
                "packet_created_at": NOW,
                "manual_review_required": False,
                "normalized_packet": {
                    "entity_mentions": [],
                    "observations": [],
                    "deferrals": [{"reason_code": "question_only"}],
                },
                "route": None,
                "route_reason_code": None,
                "feedback_id": None,
                "feedback_decision": None,
                "feedback_category": None,
                "feedback_note": None,
                "feedback_created_at": None,
            }
        )

        self.assertEqual(item["gpu"]["deferral_count"], 1)
        self.assertIn(
            "question",
            item["gpu"]["interpretations"][0]["summary"].lower(),
        )
        self.assertFalse(item["source"]["context"]["available"])

    def test_context_excerpt_is_target_centered_and_bounded(self):
        target = "Target evidence."
        prefix = "a" * 8000
        suffix = "b" * 8000
        item = _workbench_item(
            {
                "packet_id": UUID("33333333-3333-4333-8333-333333333333"),
                "packet_storage_sha256": "c" * 64,
                "source_content": target,
                "source_recorded_at": NOW,
                "source_context_content": prefix + target + suffix,
                "source_char_start": len(prefix),
                "source_char_end": len(prefix) + len(target),
                "packet_created_at": NOW,
                "manual_review_required": True,
                "normalized_packet": {
                    "contract_version": "memory_v1_relational_extraction_v5_2",
                    "predicate_registry_version": "memory_predicate_registry_v5_2",
                    "entity_mentions": [],
                    "observations": [],
                    "deferrals": [],
                    "comparison_hints": [],
                    "packet_findings": [],
                    "source_envelope": {"job_id": "must-not-leak"},
                },
                "route": "manual_review_artifact_ready",
                "route_reason_code": "reviewable_relational_packet_v5_2",
                "feedback_id": None,
                "feedback_decision": None,
                "feedback_category": None,
                "feedback_note": None,
                "feedback_created_at": None,
            }
        )

        context = item["source"]["context"]
        self.assertTrue(context["available"])
        self.assertTrue(context["truncated"])
        self.assertLessEqual(len(context["text"]), 12000)
        self.assertEqual(
            context["text"][context["target_start"] : context["target_end"]],
            target,
        )


if __name__ == "__main__":
    unittest.main()

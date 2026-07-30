from datetime import datetime, timedelta, timezone
import unittest

from rag_engine.admin_memory_health_v1 import _summarize_memory_health_v1


NOW = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)


def _payload(**overrides):
    values = {
        "claim": {"supported": 43, "retracted": 1, "last_claim_at": NOW},
        "preference": {"active": 2},
        "project": {"active": 1},
        "processing": {
            "pending": 0,
            "review_required": 0,
            "skipped": 12,
            "processing": 0,
            "error": 0,
            "pending_older_1d": 0,
            "pending_older_7d": 0,
            "completed_1d": 5,
            "completed_7d": 12,
        },
        "evidence": {"last_evidence_at": NOW},
        "answers": {
            "attested_answers": 20,
            "memory_bound_answers": 4,
            "last_answer_at": NOW,
            "last_memory_bound_at": NOW - timedelta(hours=2),
        },
        "pipeline": {
            "stages": {
                "evidence_rows": 100,
                "extracted_evidence_rows": 80,
                "durable_observations": 40,
                "bound_observations": 32,
                "evaluated_observations": 24,
                "claim_plan_items": 15,
                "reviewed_claim_plan_items": 12,
            },
            "backlog": {
                "waiting_for_binding": 8,
                "waiting_for_entailment": 8,
                "waiting_for_claim_review": 3,
                "ready_for_materialization": 2,
            },
        },
        "vector_points": 43,
        "vector_error": False,
        "governed_active": True,
        "actor_active": True,
        "generated_at": NOW,
        "collection_name": "memory_claim_v1",
    }
    values.update(overrides)
    return _summarize_memory_health_v1(**values)


class AdminMemoryHealthTests(unittest.TestCase):
    def test_healthy_payload_is_sanitized_and_owner_scoped(self):
        payload = _payload()

        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["scope"], "current_actor")
        self.assertIs(payload["index"]["synchronized"], True)
        self.assertEqual(payload["answer_use"]["memory_bound_percent"], 20.0)
        self.assertEqual(
            payload["runtime"]["lanes"]["projects"]["status"],
            "stored_not_response_active",
        )
        self.assertNotIn("owner_user_id", str(payload))
        self.assertEqual(payload["pipeline_schema"], "memory_pipeline_status_v1")
        self.assertEqual(payload["pipeline"]["stages"]["indexed_vectors"], 43)
        self.assertEqual(
            payload["pipeline"]["stages"]["memory_bound_answers_7d"],
            4,
        )
        self.assertEqual(
            payload["pipeline"]["backlog"]["waiting_for_entailment"],
            8,
        )

    def test_backlog_requires_attention_without_declaring_memory_broken(self):
        processing = {
            "pending": 11,
            "review_required": 2,
            "skipped": 0,
            "processing": 0,
            "error": 0,
            "pending_older_1d": 8,
            "pending_older_7d": 3,
        }
        payload = _payload(processing=processing)

        self.assertEqual(payload["status"], "attention")
        self.assertEqual(
            {warning["code"] for warning in payload["warnings"]},
            {"extraction_backlog", "review_queue"},
        )

    def test_index_mismatch_is_critical_and_stale_use_is_only_informational(
        self,
    ):
        answers = {
            "attested_answers": 18,
            "memory_bound_answers": 0,
            "last_answer_at": NOW,
            "last_memory_bound_at": NOW - timedelta(days=5),
        }
        payload = _payload(vector_points=42, answers=answers)

        self.assertEqual(payload["status"], "critical")
        warnings = {warning["code"]: warning for warning in payload["warnings"]}
        self.assertEqual(
            warnings["claim_index_mismatch"]["severity"],
            "critical",
        )
        self.assertEqual(
            warnings["memory_use_canary_recommended"]["severity"],
            "information",
        )


if __name__ == "__main__":
    unittest.main()

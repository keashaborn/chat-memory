from __future__ import annotations

import unittest
import uuid

from scripts.memory_v1_extraction_job_store_v1 import (
    RECEIPT_CONTRACT,
    bind_eligibility_disposition_v2,
    probe_job,
    record_eligibility_disposition,
    reserve_call,
    skip_job,
)


OWNER = uuid.UUID("00000000-0000-4000-8000-000000000001")
JOB = uuid.UUID("00000000-0000-4000-8000-000000000002")
RUN = uuid.UUID("00000000-0000-4000-8000-000000000003")
OPERATION = uuid.UUID("00000000-0000-4000-8000-000000000004")
EVIDENCE = uuid.UUID("00000000-0000-4000-8000-000000000005")
CONTENT_SHA = "a" * 64


class Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class Connection:
    def __init__(self, row: dict) -> None:
        self.row = row
        self.executions: list[tuple] = []
        self.fetches: list[tuple] = []

    def transaction(self, **_kwargs: object) -> Transaction:
        return Transaction()

    async def execute(self, *args: object) -> None:
        self.executions.append(args)

    async def fetchrow(self, *args: object) -> dict:
        self.fetches.append(args)
        return self.row


def common() -> dict:
    return {
        "owner": OWNER,
        "operation_id": OPERATION,
        "run_id": RUN,
        "worker_id": "test-worker",
        "exact_job_id": JOB,
        "expected_content_sha256": CONTENT_SHA,
        "provider_id": "openai_responses",
        "provider_version": "v1",
        "provider_model_sha256": "b" * 64,
        "rolling_window_seconds": 86400,
        "max_reserved_calls": 4,
        "failure_threshold": 2,
    }


def gate_receipt(decision: str, *reasons: str) -> dict:
    return {
        "contract_version": "memory_v1_personal_evidence_disposition_receipt_v1",
        "context_envelope_sha256": None,
        "context_turn_count": 0,
        "decision": decision,
        "gate_contract_version": "memory_v1_personal_evidence_prefilter_v1",
        "gate_result_sha256": "c" * 64,
        "policy_sha256": "d" * 64,
        "policy_version": "memory_v1_personal_evidence_prefilter_20260805_v1",
        "reason_codes": list(reasons),
        "selected_span_count": 1 if decision == "send_external" else 0,
        "selected_spans_sha256": "e" * 64,
        "source_gate_sha256": None,
    }


def bound_disposition(decision: str, *reasons: str) -> dict:
    return bind_eligibility_disposition_v2(
        owner=OWNER,
        evidence_id=EVIDENCE,
        exact_job_id=JOB,
        expected_content_sha256=CONTENT_SHA,
        gate_receipt=gate_receipt(decision, *reasons),
    )


class ExtractionJobStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_is_read_only_mode_and_exact_bound(self) -> None:
        row = {"job_id": JOB, "evidence_content_sha256": CONTENT_SHA}
        conn = Connection(row)
        result = await probe_job(conn, **common())
        self.assertEqual(result, row)
        rendered = repr(conn.executions)
        self.assertIn("memory_v1_openai_prefilter_probe_v1", rendered)
        self.assertIn(str(JOB), rendered)
        self.assertIn(CONTENT_SHA, rendered)
        self.assertIn("claim_owner_v5_bounded_extraction_job_v1", conn.fetches[0][0])

    async def test_skip_is_terminal_and_contains_no_source_value(self) -> None:
        conn = Connection({"status": "skipped", "apply_outcome": "applied"})
        result = await skip_job(
            conn,
            **common(),
            reason_code="not_personal_evidence",
            gate_sha256="c" * 64,
        )
        self.assertEqual(result["status"], "skipped")
        rendered = repr(conn.executions)
        self.assertIn("memory_v1_openai_prefilter_skip_v1", rendered)
        self.assertIn("not_personal_evidence", rendered)
        self.assertIn("external_model_calls", rendered)

    def test_bound_disposition_carries_complete_content_free_lineage(self) -> None:
        receipt = bound_disposition(
            "review_context",
            "context_binding_required",
            "ambiguous_context_fragment",
        )
        self.assertEqual(receipt["owner_user_id"], str(OWNER))
        self.assertEqual(receipt["evidence_id"], str(EVIDENCE))
        self.assertEqual(receipt["job_id"], str(JOB))
        self.assertEqual(
            receipt["reason_codes"],
            ["context_binding_required", "ambiguous_context_fragment"],
        )
        self.assertEqual(len(receipt["lineage_sha256"]), 64)
        rendered = repr(receipt)
        self.assertNotIn("source_text", rendered)
        self.assertNotIn("content\"", rendered)

    async def test_non_send_disposition_is_distinct_and_durable(self) -> None:
        for decision, status in (
            ("skip_zero_call", "skipped"),
            ("review_context", "review_required"),
            ("route_internal", "review_required"),
            ("block_local", "review_required"),
        ):
            with self.subTest(decision=decision):
                conn = Connection({"status": status, "apply_outcome": "applied"})
                receipt = bound_disposition(decision, f"{decision}_reason")
                result = await record_eligibility_disposition(
                    conn,
                    **common(),
                    eligibility_disposition=receipt,
                )
                self.assertEqual(result["status"], status)
                rendered = repr(conn.executions)
                self.assertIn("memory_v1_openai_eligibility_disposition_v2", rendered)
                self.assertIn(f'"decision":"{decision}"', rendered)
                self.assertIn(f'"owner_user_id":"{OWNER}"', rendered)

    async def test_send_external_cannot_use_non_send_transition(self) -> None:
        conn = Connection({"status": "review_required", "apply_outcome": "applied"})
        with self.assertRaisesRegex(RuntimeError, "contract is invalid"):
            await record_eligibility_disposition(
                conn,
                **common(),
                eligibility_disposition=bound_disposition(
                    "send_external", "personal_evidence_selected"
                ),
            )

    async def test_budget_skip_retains_send_external_disposition(self) -> None:
        conn = Connection({"status": "skipped", "apply_outcome": "applied"})
        receipt = bound_disposition("send_external", "personal_evidence_selected")
        await skip_job(
            conn,
            **common(),
            reason_code="request_budget_exceeded",
            gate_sha256=receipt["gate_result_sha256"],
            eligibility_disposition=receipt,
        )
        rendered = repr(conn.executions)
        self.assertIn("memory_v1_prefilter_skip_receipt_v2", rendered)
        self.assertIn('"decision":"send_external"', rendered)

    async def test_reservation_requires_canonical_receipt_and_exact_result(self) -> None:
        conn = Connection(
            {
                "job_id": JOB,
                "evidence_content_sha256": CONTENT_SHA,
                "control_outcome": "reserved",
            }
        )
        receipt = {"contract_version": RECEIPT_CONTRACT}
        result = await reserve_call(conn, **common(), receipt=receipt)
        self.assertEqual(result["control_outcome"], "reserved")
        self.assertIn("memory_v1_openai_request_reservation_v1", repr(conn.executions))
        with self.assertRaisesRegex(RuntimeError, "contract is invalid"):
            await reserve_call(conn, **common(), receipt={})


if __name__ == "__main__":
    unittest.main()

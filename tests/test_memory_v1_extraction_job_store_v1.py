from __future__ import annotations

import unittest
import uuid

from scripts.memory_v1_extraction_job_store_v1 import (
    RECEIPT_CONTRACT,
    probe_job,
    reserve_call,
    skip_job,
)


OWNER = uuid.UUID("00000000-0000-4000-8000-000000000001")
JOB = uuid.UUID("00000000-0000-4000-8000-000000000002")
RUN = uuid.UUID("00000000-0000-4000-8000-000000000003")
OPERATION = uuid.UUID("00000000-0000-4000-8000-000000000004")
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

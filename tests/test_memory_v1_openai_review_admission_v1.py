from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID

from rag_engine.memory_v1_openai_review_admission_v1 import (
    OpenAIReviewAdmissionCommandV1,
    OpenAIReviewAdmissionError,
    VerifiedReviewerContextV1,
    admit_reviewed_openai_observation_v1,
)


OWNER = UUID("11111111-1111-4111-8111-111111111111")
IDS = [UUID(f"00000000-0000-4000-8000-{value:012d}") for value in range(1, 15)]
SHA = "a" * 64


class _Transaction:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn

    async def __aenter__(self) -> None:
        self.conn.in_transaction = True

    async def __aexit__(self, *args: object) -> None:
        self.conn.in_transaction = False


class FakeConnection:
    def __init__(self, rows: list[dict[str, object] | None]) -> None:
        self.rows = list(rows)
        self.queries: list[str] = []
        self.in_transaction = False

    def is_in_transaction(self) -> bool:
        return self.in_transaction

    def transaction(self, **kwargs: object) -> _Transaction:
        if kwargs != {"isolation": "serializable"}:
            raise AssertionError(kwargs)
        return _Transaction(self)

    async def execute(self, query: str, *args: object) -> str:
        self.queries.append(query)
        return "SELECT 1"

    async def fetchval(self, query: str, *args: object) -> object:
        self.queries.append(query)
        return OWNER

    async def fetchrow(
        self, query: str, *args: object
    ) -> dict[str, object] | None:
        self.queries.append(query)
        if not self.rows:
            raise AssertionError(f"unexpected query: {query}")
        return self.rows.pop(0)


class Binder:
    async def bind_verified_reviewer(
        self, conn: FakeConnection, reviewer: VerifiedReviewerContextV1
    ) -> None:
        if reviewer.owner_user_id != OWNER:
            raise AssertionError("owner drift")
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(OWNER))


def reviewer() -> VerifiedReviewerContextV1:
    return VerifiedReviewerContextV1(
        owner_user_id=OWNER,
        reviewer_type="user",
        reviewer_ref="owner-browser-session-v1",
        authentication_manifest_sha256="b" * 64,
    )


def command() -> OpenAIReviewAdmissionCommandV1:
    return OpenAIReviewAdmissionCommandV1(
        entity_resolution_request_id=IDS[2],
        route_event_id=IDS[3],
        expected_stage_bundle_sha256=SHA,
        plan_id=IDS[4],
        entailment_request_id=IDS[5],
        reason="Owner explicitly approved this exact proposition.",
        reason_codes=("explicit_owner_approval", "single_observation_review"),
    )


def source_row() -> dict[str, object]:
    return {
        "owner_user_id": OWNER,
        "evidence_id": IDS[7],
        "observation_id": IDS[8],
        "observation_sha256": "c" * 64,
        "predicate_registry_version": "memory_predicate_registry_v5_2",
        "predicate": "stance.reported",
        "projection_class": "reported_stance",
        "surface_policy": "relevant_recall_or_explicit_recall",
        "modality": "reported_belief",
        "polarity": "affirmed",
        "subject_entity_type": "self",
        "subject_entity_status": "active",
        "evidence_status": "active",
        "object_literal": {"kind": "literal", "value": "test"},
        "project_scope": {"kind": "global"},
        "temporal": {"semantic": "current"},
    }


def database_rows() -> list[dict[str, object] | None]:
    return [
        {"admission_manifest_sha256": "d" * 64},
        {
            "packet_id": IDS[6],
            "evidence_id": IDS[7],
            "batch_id": IDS[9],
            "observation_id": IDS[8],
            "outcome": "applied",
            "rows_written": 8,
        },
        None,
        source_row(),
        {
            "evidence_id": IDS[7],
            "observation_sha256": "c" * 64,
            "observation_ref": "o01",
            "source_spans": [{"start": 0, "end": 4}],
        },
        {"owner_manifest_sha256": "e" * 64},
        {"authorization_manifest_sha256": "f" * 64},
        {"outcome": "applied", "rows_written": 1},
        {"outcome": "applied", "rows_written": 6},
        None,
        {"authorization_manifest_sha256": "1" * 64},
        {"review_id": IDS[10], "outcome": "applied", "rows_written": 1},
        {"lane": "claim", "apply_manifest_sha256": "2" * 64},
        finalized_row(),
    ]


def finalized_row(
    *, outcome: str = "applied", rows_written: int = 1
) -> dict[str, object]:
    owner_sha = hashlib.sha256(str(OWNER).encode()).hexdigest()
    command_sha = "3" * 64
    values = (
        "memory_v1_openai_review_admission_receipt_v1",
        owner_sha,
        command_sha,
        "d" * 64,
        str(IDS[3]),
        str(IDS[0]),
        str(IDS[5]),
        str(IDS[3]),
        str(IDS[6]),
        str(IDS[7]),
        str(IDS[9]),
        str(IDS[8]),
        str(IDS[4]),
        str(IDS[10]),
        "1" * 64,
        "2" * 64,
    )
    return {
        "owner_user_id_sha256": owner_sha,
        "command_manifest_sha256": command_sha,
        "admission_manifest_sha256": "d" * 64,
        "admission_id": IDS[3],
        "operation_id": IDS[0],
        "entailment_request_id": IDS[5],
        "route_event_id": IDS[3],
        "packet_id": IDS[6],
        "evidence_id": IDS[7],
        "batch_id": IDS[9],
        "observation_id": IDS[8],
        "plan_id": IDS[4],
        "projection_review_id": IDS[10],
        "projection_review_authorization_sha256": "1" * 64,
        "projection_apply_manifest_sha256": "2" * 64,
        "receipt_sha256": hashlib.sha256("|".join(values).encode()).hexdigest(),
        "outcome": outcome,
        "rows_written": rows_written,
    }


class AdmissionContractTest(unittest.TestCase):
    def test_command_requires_sorted_unique_reason_codes(self) -> None:
        with self.assertRaises(OpenAIReviewAdmissionError):
            OpenAIReviewAdmissionCommandV1(
                entity_resolution_request_id=IDS[2],
                route_event_id=IDS[3],
                expected_stage_bundle_sha256=SHA,
                plan_id=IDS[4],
                entailment_request_id=IDS[5],
                reason="valid",
                reason_codes=("z", "a"),
            )

    def test_reviewer_rejects_unverified_authority(self) -> None:
        with self.assertRaises(OpenAIReviewAdmissionError):
            VerifiedReviewerContextV1(
                owner_user_id=OWNER,
                reviewer_type="user",
                reviewer_ref="owner",
                authentication_manifest_sha256="not-a-hash",
            )

    def test_top_level_transaction_is_required(self) -> None:
        conn = FakeConnection([])
        conn.in_transaction = True
        with self.assertRaises(OpenAIReviewAdmissionError):
            asyncio.run(
                admit_reviewed_openai_observation_v1(
                    conn, binder=Binder(), reviewer=reviewer(), command=command()
                )
            )

    def test_one_item_chain_returns_content_free_receipt(self) -> None:
        conn = FakeConnection(database_rows())
        packet = {"contract_version": "test", "projections": [{"projection_ref": "p01"}]}
        with (
            patch(
                "rag_engine.memory_v1_openai_review_admission_v1.build_packet",
                return_value=packet,
            ),
            patch(
                "rag_engine.memory_v1_openai_review_admission_v1.validate_packet"
            ),
            patch(
                "rag_engine.memory_v1_openai_review_admission_v1._projection_registry",
                return_value={},
            ),
        ):
            receipt = asyncio.run(
                admit_reviewed_openai_observation_v1(
                    conn, binder=Binder(), reviewer=reviewer(), command=command()
                )
            )
        self.assertEqual(receipt.outcome, "applied")
        self.assertEqual(receipt.packet_id, IDS[6])
        self.assertEqual(receipt.observation_id, IDS[8])
        self.assertEqual(receipt.projection_review_id, IDS[10])
        self.assertEqual(receipt.rows_written, 16)
        self.assertEqual(receipt.material()["external_model_calls"], 0)
        self.assertEqual(receipt.material()["claim_writes"], 0)
        self.assertEqual(receipt.material()["qdrant_writes"], 0)
        self.assertEqual(conn.rows, [])
        joined = "\n".join(conn.queries)
        self.assertIn("preflight_owner_openai_review_admission_v1", joined)
        self.assertIn("apply_owner_openai_review_admission_v1", joined)
        self.assertIn("stage_projection_plan_v5_2", joined)
        self.assertIn("review_projection_v5", joined)
        self.assertNotIn("finalize_owner_openai_review_admission_v1", joined)
        self.assertIn("'system','memory_v1_openai_review_admission_v1'", joined)
        self.assertNotIn("projection_outbox", joined)
        self.assertNotIn("apply_projection_v5", joined)

    def test_finalized_replay_returns_without_new_projection_review(self) -> None:
        rows: list[dict[str, object] | None] = [
            {"admission_manifest_sha256": "d" * 64},
            {
                "packet_id": IDS[6],
                "evidence_id": IDS[7],
                "batch_id": IDS[9],
                "observation_id": IDS[8],
                "outcome": "replayed",
                "rows_written": 0,
            },
            finalized_row(outcome="replayed", rows_written=0),
        ]
        conn = FakeConnection(rows)
        receipt = asyncio.run(
            admit_reviewed_openai_observation_v1(
                conn, binder=Binder(), reviewer=reviewer(), command=command()
            )
        )
        self.assertEqual(receipt.outcome, "replayed")
        self.assertEqual(receipt.rows_written, 0)
        joined = "\n".join(conn.queries)
        self.assertIn("read_owner_openai_review_admission_receipt_v1", joined)
        self.assertNotIn("review_projection_v5", joined)
        self.assertEqual(conn.rows, [])

    def test_non_claim_projection_is_rejected(self) -> None:
        rows = database_rows()
        rows[-2] = {"lane": "preference", "apply_manifest_sha256": "2" * 64}
        conn = FakeConnection(rows)
        with (
            patch(
                "rag_engine.memory_v1_openai_review_admission_v1.build_packet",
                return_value={"projections": [{"projection_ref": "p01"}]},
            ),
            patch(
                "rag_engine.memory_v1_openai_review_admission_v1.validate_packet"
            ),
            patch(
                "rag_engine.memory_v1_openai_review_admission_v1._projection_registry",
                return_value={},
            ),
        ):
            with self.assertRaises(OpenAIReviewAdmissionError):
                asyncio.run(
                    admit_reviewed_openai_observation_v1(
                        conn, binder=Binder(), reviewer=reviewer(), command=command()
                    )
                )


class AdmissionSQLPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        package = (
            cls.root
            / "governed-function-creations"
            / "memory_openai_review_admission_authority_v1"
        )
        cls.forward = "\n".join(
            path.read_text() for path in sorted(package.glob("*-forward.pgsql"))
        )
        cls.rollback = "\n".join(
            path.read_text() for path in sorted(package.glob("*-rollback.pgsql"))
        )

    def test_uses_existing_append_only_authority_without_new_tables(self) -> None:
        self.assertNotIn("CREATE TABLE", self.forward)
        self.assertNotIn("CREATE TRIGGER", self.forward)
        self.assertIn("memory.relational_operation_request", self.forward)
        self.assertIn("memory.projection_review", self.forward)
        self.assertIn("read_owner_openai_review_admission_receipt_v1", self.forward)

    def test_function_is_exact_item_and_provider_free(self) -> None:
        self.assertIn("source.observation_count<>1", self.forward)
        self.assertIn("observation->>'observation_ref'<>'o01'", self.forward)
        self.assertIn("source.blocking_code_count<>0", self.forward)
        self.assertIn("stage_relational_packet_v5_2", self.forward)
        for forbidden in ("http", "openai.responses", "DELETE FROM"):
            self.assertNotIn(forbidden, self.forward)

    def test_rollback_drops_only_new_functions(self) -> None:
        self.assertEqual(self.rollback.count("DROP FUNCTION"), 3)
        self.assertNotIn("DROP TABLE", self.rollback)
        self.assertNotIn("TRUNCATE", self.rollback)


if __name__ == "__main__":
    unittest.main()

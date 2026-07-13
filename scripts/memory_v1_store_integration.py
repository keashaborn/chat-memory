#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg

from rag_engine.memory_v1_retrieval import build_memory_packet
from rag_engine.memory_v1_store import (
    CandidateConflict,
    CandidateNotApproved,
    CandidateNotFound,
    EvidenceConflict,
    ProposalValidationError,
    apply_candidate,
    propose_candidate,
    record_evidence,
    review_candidate,
)


ACTOR_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ACTOR_B = uuid.UUID("22222222-2222-4222-8222-222222222222")


def name_proposal(value: str, *, supersedes_claim_id: str | None = None) -> dict:
    proposal = {
        "subject": {
            "entity_key": "self",
            "entity_type": "person",
            "canonical_name": "The user",
        },
        "predicate": "name.canonical",
        "object_literal": {"type": "str", "v": value},
        "canonical_text": f"The canonical pet name is {value}.",
        "qualifiers": {"entity_role": "pet"},
        "status": "supported",
        "confidence": 0.92,
        "importance": 0.70,
        "salience": 0.80,
        "sensitivity": "low",
        "retrieval_policy": {
            "domains": ["name_correction"],
            "intents": ["personal_recall"],
            "surface": "normalization",
        },
        "evidence_stance": "supports",
        "support_score": 0.92,
        "opposition_score": 0.0,
        "assessment_method": "integration_review",
        "assessment_method_version": "v1",
    }
    if supersedes_claim_id:
        proposal["supersedes_claim_id"] = supersedes_claim_id
    return proposal


async def expect_error(exc_type, awaitable, label: str) -> None:
    try:
        await awaitable
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__}: {label}")


async def main() -> int:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")

    conn = await asyncpg.connect(dsn)
    outer = conn.transaction()
    await outer.start()
    try:
        await conn.execute("SET LOCAL ROLE brains_app")

        nemo_evidence = await record_evidence(
            conn,
            ACTOR_A,
            kind="user_statement",
            source_system="memory_v1_store_integration",
            external_id="nemo-initial",
            content="The pet name is Nemo.",
            directness=1.0,
            sensitivity="low",
        )
        nemo_evidence_repeat = await record_evidence(
            conn,
            ACTOR_A,
            kind="user_statement",
            source_system="memory_v1_store_integration",
            external_id="nemo-initial",
            content="The pet name is Nemo.",
            directness=1.0,
            sensitivity="low",
        )
        if nemo_evidence_repeat != nemo_evidence:
            raise AssertionError("idempotent evidence write returned a different ID")
        await expect_error(
            EvidenceConflict,
            record_evidence(
                conn,
                ACTOR_A,
                kind="user_statement",
                source_system="memory_v1_store_integration",
                external_id="nemo-initial",
                content="Conflicting replacement content.",
                directness=1.0,
                sensitivity="low",
            ),
            "immutable evidence conflict",
        )
        nemo_candidate = await propose_candidate(
            conn,
            ACTOR_A,
            evidence_id=nemo_evidence,
            proposal=name_proposal("Nemo"),
            extractor="integration_manual_v1",
            extractor_version="v1",
        )

        await expect_error(
            CandidateNotApproved,
            apply_candidate(
                conn,
                ACTOR_A,
                candidate_id=nemo_candidate["candidate_id"],
                expected_proposal_hash=nemo_candidate["proposal_hash"],
            ),
            "unapproved candidate",
        )
        await expect_error(
            CandidateConflict,
            review_candidate(
                conn,
                ACTOR_A,
                candidate_id=nemo_candidate["candidate_id"],
                expected_proposal_hash="0" * 64,
                approve=True,
                reviewer_ref="integration",
                reason="wrong hash must fail",
            ),
            "candidate hash mismatch",
        )
        await expect_error(
            CandidateNotFound,
            review_candidate(
                conn,
                ACTOR_B,
                candidate_id=nemo_candidate["candidate_id"],
                expected_proposal_hash=nemo_candidate["proposal_hash"],
                approve=True,
                reviewer_ref="integration",
                reason="cross-user review must fail",
            ),
            "cross-user review",
        )
        await expect_error(
            CandidateNotFound,
            apply_candidate(
                conn,
                ACTOR_B,
                candidate_id=nemo_candidate["candidate_id"],
                expected_proposal_hash=nemo_candidate["proposal_hash"],
            ),
            "cross-user apply",
        )

        await review_candidate(
            conn,
            ACTOR_A,
            candidate_id=nemo_candidate["candidate_id"],
            expected_proposal_hash=nemo_candidate["proposal_hash"],
            approve=True,
            reviewer_ref="integration",
            reason="seed the incorrect name for correction testing",
        )
        nemo_result = await apply_candidate(
            conn,
            ACTOR_A,
            candidate_id=nemo_candidate["candidate_id"],
            expected_proposal_hash=nemo_candidate["proposal_hash"],
            actor_type="admin",
            actor_ref="integration",
        )

        neko_evidence = await record_evidence(
            conn,
            ACTOR_A,
            kind="user_statement",
            source_system="memory_v1_store_integration",
            external_id="neko-correction",
            content="The correct pet name is Neko, not Nemo.",
            directness=1.0,
            sensitivity="low",
        )
        neko_candidate = await propose_candidate(
            conn,
            ACTOR_A,
            evidence_id=neko_evidence,
            proposal=name_proposal(
                "Neko", supersedes_claim_id=nemo_result["claim_id"]
            ),
            extractor="explicit_user_correction_v1",
            extractor_version="v1",
            auto_approve=True,
        )
        neko_result = await apply_candidate(
            conn,
            ACTOR_A,
            candidate_id=neko_candidate["candidate_id"],
            expected_proposal_hash=neko_candidate["proposal_hash"],
            actor_type="system",
            actor_ref="explicit_user_correction_v1",
        )

        repeat_evidence = await record_evidence(
            conn,
            ACTOR_A,
            kind="user_statement",
            source_system="memory_v1_store_integration",
            external_id="neko-repeat",
            content="Neko is the correct spelling.",
            directness=1.0,
            sensitivity="low",
        )
        repeat_candidate = await propose_candidate(
            conn,
            ACTOR_A,
            evidence_id=repeat_evidence,
            proposal=name_proposal(
                "Neko", supersedes_claim_id=nemo_result["claim_id"]
            ),
            extractor="explicit_user_correction_v1",
            extractor_version="v1",
            auto_approve=True,
        )
        repeat_result = await apply_candidate(
            conn,
            ACTOR_A,
            candidate_id=repeat_candidate["candidate_id"],
            expected_proposal_hash=repeat_candidate["proposal_hash"],
            actor_type="system",
            actor_ref="explicit_user_correction_v1",
        )

        if repeat_result["claim_id"] != neko_result["claim_id"]:
            raise AssertionError("repeated evidence created a duplicate claim")

        await conn.execute(
            "SELECT set_config('app.user_id', $1, true)", str(ACTOR_A)
        )
        rows = await conn.fetch(
            """
            SELECT claim_id, status::text, canonical_text
            FROM memory.claim
            ORDER BY canonical_text
            """
        )
        if len(rows) != 2:
            raise AssertionError(f"expected 2 claims, got {len(rows)}")
        statuses = {str(row["claim_id"]): row["status"] for row in rows}
        if statuses[nemo_result["claim_id"]] != "superseded":
            raise AssertionError("Nemo claim was not superseded")
        if statuses[neko_result["claim_id"]] != "supported":
            raise AssertionError("Neko claim is not supported")

        old_assessment_status = await conn.fetchval(
            """
            SELECT status::text
            FROM memory.claim_assessment
            WHERE claim_id=$1
            ORDER BY assessed_at DESC, assessment_id DESC
            LIMIT 1
            """,
            uuid.UUID(nemo_result["claim_id"]),
        )
        if old_assessment_status != "superseded":
            raise AssertionError("Nemo supersession assessment is missing")

        evidence_count = await conn.fetchval(
            """
            SELECT count(*)
            FROM memory.claim_evidence
            WHERE claim_id=$1
            """,
            uuid.UUID(neko_result["claim_id"]),
        )
        if evidence_count != 2:
            raise AssertionError(
                f"expected 2 Neko evidence links, got {evidence_count}"
            )

        relation_count = await conn.fetchval(
            """
            SELECT count(*)
            FROM memory.claim_relation
            WHERE from_claim_id=$1
              AND to_claim_id=$2
              AND relation_type='supersedes'
            """,
            uuid.UUID(neko_result["claim_id"]),
            uuid.UUID(nemo_result["claim_id"]),
        )
        if relation_count != 1:
            raise AssertionError("supersession relation is missing or duplicated")

        outbox_count = await conn.fetchval(
            "SELECT count(*) FROM memory.projection_outbox"
        )
        if outbox_count != 2:
            raise AssertionError(f"expected 2 coalesced outbox rows, got {outbox_count}")

        packet = await build_memory_packet(
            conn,
            ACTOR_A,
            query="Was the pet name Neko or Nemo?",
            intent="personal_recall",
            domain="name_correction",
            candidate_hits=[
                {"claim_id": nemo_result["claim_id"], "semantic_score": 0.99},
                {"claim_id": neko_result["claim_id"], "semantic_score": 0.95},
            ],
            max_claims=4,
            max_tokens=300,
            max_sensitivity="medium",
            explicit_recall=True,
            request_id="memory-v1-integration-recall",
        )
        if [item["claim_id"] for item in packet["claims"]] != [
            neko_result["claim_id"]
        ]:
            raise AssertionError("packet did not select only the active Neko claim")
        if packet["rejected_counts"].get("status:superseded") != 1:
            raise AssertionError("packet did not record the superseded rejection")
        if packet["claims"][0]["use_instruction"] != (
            "normalize_memory_without_unprompted_discussion"
        ):
            raise AssertionError("normalization use instruction is incorrect")
        if packet["claims"][0]["qualifiers"] != {"entity_role": "pet"}:
            raise AssertionError("claim qualifiers did not round-trip")

        technical_packet = await build_memory_packet(
            conn,
            ACTOR_A,
            query="How do I restart the frontend?",
            intent="technical",
            domain="technical",
            candidate_hits=[
                {"claim_id": neko_result["claim_id"], "semantic_score": 0.80}
            ],
            max_claims=4,
            max_tokens=300,
            request_id="memory-v1-integration-technical",
        )
        if technical_packet["claims"]:
            raise AssertionError("technical packet selected personal memory")
        if technical_packet["rejected_counts"].get("domain") != 1:
            raise AssertionError("technical packet did not record domain rejection")

        cross_user_packet = await build_memory_packet(
            conn,
            ACTOR_B,
            query="Try to read another actor's claim",
            intent="personal_recall",
            domain="name_correction",
            candidate_hits=[
                {"claim_id": neko_result["claim_id"], "semantic_score": 1.0}
            ],
            max_claims=4,
            max_tokens=300,
            request_id="memory-v1-integration-cross-user",
        )
        if cross_user_packet["claims"]:
            raise AssertionError("cross-user packet exposed a claim")
        if cross_user_packet["rejected_counts"].get("not_visible") != 1:
            raise AssertionError("cross-user packet did not record invisible candidate")

        await conn.execute(
            "SELECT set_config('app.user_id', $1, true)", str(ACTOR_A)
        )
        traces = await conn.fetch(
            """
            SELECT query_hash, query_preview, selected_count
            FROM memory.retrieval_trace
            ORDER BY created_at
            """
        )
        if len(traces) != 2:
            raise AssertionError(f"expected 2 actor A traces, got {len(traces)}")
        if any(row["query_preview"] is not None for row in traces):
            raise AssertionError("retrieval trace stored raw query preview")
        if any(len(row["query_hash"]) != 64 for row in traces):
            raise AssertionError("retrieval trace query hash is invalid")

        await conn.fetchrow(
            """
            SELECT event_id
            FROM memory.transition_evidence_lifecycle($1, $2, $3, $4)
            """,
            repeat_evidence,
            uuid.UUID("f1000000-0000-4000-8000-000000000001"),
            "redact_content",
            "correction",
        )

        one_source_packet = await build_memory_packet(
            conn,
            ACTOR_A,
            query="What is the corrected pet name?",
            intent="personal_recall",
            domain="name_correction",
            candidate_hits=[
                {"claim_id": neko_result["claim_id"], "semantic_score": 0.95}
            ],
            max_claims=4,
            max_tokens=300,
            request_id="memory-v1-integration-one-active-source",
        )
        if len(one_source_packet["claims"]) != 1:
            raise AssertionError("claim with another active source was suppressed")
        if one_source_packet["claims"][0]["evidence_refs"] != [str(neko_evidence)]:
            raise AssertionError("redacted evidence leaked into packet evidence refs")

        await expect_error(
            EvidenceConflict,
            record_evidence(
                conn,
                ACTOR_A,
                kind="user_statement",
                source_system="memory_v1_store_integration",
                external_id="neko-repeat",
                content="Neko is the correct spelling.",
                directness=1.0,
                sensitivity="low",
            ),
            "tombstoned evidence reinsertion",
        )
        await expect_error(
            ProposalValidationError,
            propose_candidate(
                conn,
                ACTOR_A,
                evidence_id=repeat_evidence,
                proposal=name_proposal("Neko"),
                extractor="integration_manual_v1",
                extractor_version="v1",
            ),
            "candidate from redacted evidence",
        )

        stale_evidence = await record_evidence(
            conn,
            ACTOR_A,
            kind="user_statement",
            source_system="memory_v1_store_integration",
            external_id="stale-approved-candidate",
            content="The temporary candidate value is Nyx.",
            directness=1.0,
            sensitivity="low",
        )
        stale_candidate = await propose_candidate(
            conn,
            ACTOR_A,
            evidence_id=stale_evidence,
            proposal=name_proposal("Nyx"),
            extractor="explicit_user_correction_v1",
            extractor_version="v1",
            auto_approve=True,
        )
        await conn.fetchrow(
            """
            SELECT event_id
            FROM memory.transition_evidence_lifecycle($1, $2, $3, $4)
            """,
            stale_evidence,
            uuid.UUID("f1000000-0000-4000-8000-000000000002"),
            "delete_tombstone",
            "user_request",
        )
        await expect_error(
            CandidateConflict,
            apply_candidate(
                conn,
                ACTOR_A,
                candidate_id=stale_candidate["candidate_id"],
                expected_proposal_hash=stale_candidate["proposal_hash"],
            ),
            "approved candidate after evidence deletion",
        )

        await conn.fetchrow(
            """
            SELECT event_id
            FROM memory.transition_evidence_lifecycle($1, $2, $3, $4)
            """,
            neko_evidence,
            uuid.UUID("f1000000-0000-4000-8000-000000000003"),
            "delete_tombstone",
            "user_request",
        )
        no_source_packet = await build_memory_packet(
            conn,
            ACTOR_A,
            query="What is the corrected pet name?",
            intent="personal_recall",
            domain="name_correction",
            candidate_hits=[
                {"claim_id": neko_result["claim_id"], "semantic_score": 0.95}
            ],
            max_claims=4,
            max_tokens=300,
            request_id="memory-v1-integration-no-active-source",
        )
        if no_source_packet["claims"]:
            raise AssertionError("claim without active evidence entered the packet")
        if no_source_packet["rejected_counts"].get("no_active_evidence") != 1:
            raise AssertionError("missing active-evidence rejection reason")

        print("memory_v1_store_integration: PASS")
        return 0
    finally:
        await outer.rollback()
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

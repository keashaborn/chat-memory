#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

import asyncpg

from rag_engine.memory_v1_store import propose_candidate, record_evidence


OWNER = uuid.UUID("557ea042-cb82-48f8-9429-472e96c957ef")
SOURCE_CANDIDATE = uuid.UUID("66bdd1df-5630-4976-aafc-a18f0e66bb2b")


async def main() -> None:
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        async with conn.transaction(readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(OWNER))
            proposal_raw = await conn.fetchval(
                """
                SELECT proposal FROM memory.candidate
                WHERE owner_user_id=$1 AND candidate_id=$2
                """,
                OWNER,
                SOURCE_CANDIDATE,
            )
        proposal = json.loads(proposal_raw) if isinstance(proposal_raw, str) else dict(proposal_raw)

        duplicate_evidence = await record_evidence(
            conn,
            OWNER,
            kind="user_statement",
            source_system="governance_clone_probe",
            external_id="dedupe-name-v3",
            content="My name is David Baeb.",
            observed_at=datetime.now(timezone.utc),
            directness=1.0,
            source_reliability=1.0,
            independence_key="governance_clone_probe:dedupe-name-v3",
            sensitivity="medium",
            metadata={"test_only": True},
        )
        duplicate = await propose_candidate(
            conn,
            OWNER,
            evidence_id=duplicate_evidence,
            proposal=proposal,
            extractor="reviewed_seed_v1",
            extractor_version="governance_clone_probe_v1",
            comparison={"test": "dedupe"},
            auto_approve=True,
        )

        contradiction_proposal = dict(proposal)
        contradiction_proposal["object_literal"] = "Other Name"
        contradiction_proposal["canonical_text"] = "User's name is Other Name."
        contradiction_proposal["confidence"] = 0.9
        contradiction_proposal["support_score"] = 0.9
        contradiction_evidence = await record_evidence(
            conn,
            OWNER,
            kind="user_statement",
            source_system="governance_clone_probe",
            external_id="contradict-name-v3",
            content="My name is Other Name.",
            observed_at=datetime.now(timezone.utc),
            directness=1.0,
            source_reliability=1.0,
            independence_key="governance_clone_probe:contradict-name-v3",
            sensitivity="medium",
            metadata={"test_only": True},
        )
        contradiction = await propose_candidate(
            conn,
            OWNER,
            evidence_id=contradiction_evidence,
            proposal=contradiction_proposal,
            extractor="reviewed_seed_v1",
            extractor_version="governance_clone_probe_v1",
            comparison={"test": "contradiction"},
            auto_approve=True,
        )
        print(
            json.dumps(
                {
                    "duplicate_candidate_id": str(duplicate["candidate_id"]),
                    "contradiction_candidate_id": str(contradiction["candidate_id"]),
                },
                sort_keys=True,
            )
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

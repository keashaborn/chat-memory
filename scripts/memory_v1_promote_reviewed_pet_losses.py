#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag_engine.memory_v1_pet_review import (
    HELSING_LOSS_CLAIM_ID,
    NEKO_CORRECTION_CLAIM_ID,
    OWNER_USER_ID,
    SOURCE_CHAT_LOG_ID,
    SOURCE_SHA256,
    dahlia_loss_proposal,
    existing_claim_proposal,
    neko_loss_proposal,
    validate_source,
)
from rag_engine.memory_v1_store import (
    apply_candidate,
    proposal_hash,
    propose_candidate,
    record_evidence,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Promote reviewed Neko/Dahlia losses and link supporting Helsing evidence."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-owner", default="")
    parser.add_argument("--confirm-source-id", default="")
    parser.add_argument("--confirm-source-sha256", default="")
    return parser.parse_args()


async def set_actor(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "SELECT set_config('app.user_id', $1, true)", str(OWNER_USER_ID)
    )


async def load_review_context(conn: asyncpg.Connection):
    async with conn.transaction():
        await set_actor(conn)
        source = await conn.fetchrow(
            """
            SELECT id, user_id, source, text, thread_id, request_id, created_at
            FROM public.chat_log
            WHERE id=$1
            """,
            SOURCE_CHAT_LOG_ID,
        )
        if not source:
            raise RuntimeError("reviewed pet-loss source is missing")
        validate_source(source)

        existing = await conn.fetchrow(
            """
            SELECT claim.claim_id, entity.entity_key, entity.entity_type,
                   entity.canonical_name, claim.predicate, claim.object_literal,
                   claim.qualifiers, claim.canonical_text, claim.status::text,
                   claim.confidence, claim.importance, claim.salience,
                   claim.sensitivity::text, claim.valid_from, claim.valid_to,
                   claim.retrieval_policy, claim.metadata
            FROM memory.claim AS claim
            JOIN memory.entity AS entity
              ON entity.owner_user_id=claim.owner_user_id
             AND entity.entity_id=claim.subject_entity_id
            WHERE claim.owner_user_id=$1 AND claim.claim_id=$2
            """,
            OWNER_USER_ID,
            HELSING_LOSS_CLAIM_ID,
        )
        if not existing:
            raise RuntimeError("governed Helsing claim is missing")

        correction = await conn.fetchrow(
            """
            SELECT claim_id, status::text, canonical_text
            FROM memory.claim
            WHERE owner_user_id=$1 AND claim_id=$2
            """,
            OWNER_USER_ID,
            NEKO_CORRECTION_CLAIM_ID,
        )
        if not correction or correction["status"] != "supported":
            raise RuntimeError("supported Neko correction claim is missing")
        if "Neko" not in correction["canonical_text"] or "Nemo" not in correction["canonical_text"]:
            raise RuntimeError("Neko correction claim text changed")
    return dict(source), dict(existing)


async def main() -> int:
    args = arguments()
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")

    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        source, existing = await load_review_context(conn)
        proposals = [
            ("neko_loss", neko_loss_proposal()),
            ("dahlia_loss", dahlia_loss_proposal()),
            ("helsing_support", existing_claim_proposal(existing)),
        ]

        print("=== memory V1 reviewed pet-loss preview ===")
        print("mode:", "APPLY" if args.apply else "DRY_RUN")
        print("owner_user_id:", OWNER_USER_ID)
        print("source_chat_log_id:", SOURCE_CHAT_LOG_ID)
        print("source_sha256:", SOURCE_SHA256)
        for label, proposal in proposals:
            print(
                json.dumps(
                    {
                        "label": label,
                        "canonical_text": proposal["canonical_text"],
                        "qualifiers": proposal["qualifiers"],
                        "retrieval_policy": proposal["retrieval_policy"],
                        "proposal_hash": proposal_hash(proposal),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )

        if not args.apply:
            print("dry_run_no_writes")
            return 0

        if args.confirm_owner != str(OWNER_USER_ID):
            raise SystemExit("REFUSING APPLY: --confirm-owner mismatch")
        if args.confirm_source_id != str(SOURCE_CHAT_LOG_ID):
            raise SystemExit("REFUSING APPLY: --confirm-source-id mismatch")
        if args.confirm_source_sha256 != SOURCE_SHA256:
            raise SystemExit("REFUSING APPLY: --confirm-source-sha256 mismatch")

        results: Dict[str, Dict[str, Any]] = {}
        async with conn.transaction():
            evidence_id = await record_evidence(
                conn,
                OWNER_USER_ID,
                kind="user_statement",
                source_system="public.chat_log",
                external_id=f"chat_log:{SOURCE_CHAT_LOG_ID}",
                content=source["text"],
                observed_at=source["created_at"],
                directness=1.0,
                source_reliability=1.0,
                independence_key=f"chat_log:{SOURCE_CHAT_LOG_ID}",
                sensitivity="medium",
                metadata={
                    "chat_log_id": str(SOURCE_CHAT_LOG_ID),
                    "thread_id": str(source["thread_id"]) if source["thread_id"] else None,
                    "request_id": source["request_id"],
                    "review": "split_multi_pet_source_v1",
                },
            )

            for label, proposal in proposals:
                candidate = await propose_candidate(
                    conn,
                    OWNER_USER_ID,
                    evidence_id=evidence_id,
                    proposal=proposal,
                    extractor="reviewed_seed_v1",
                    extractor_version="pet_loss_split_v1",
                    comparison={
                        "source_chat_log_id": str(SOURCE_CHAT_LOG_ID),
                        "review": "manual_source_split",
                        "label": label,
                    },
                    auto_approve=True,
                )
                if candidate["status"] == "applied":
                    results[label] = {
                        "claim_id": str(candidate["applied_claim_id"]),
                        "existing": True,
                    }
                    continue
                result = await apply_candidate(
                    conn,
                    OWNER_USER_ID,
                    candidate_id=candidate["candidate_id"],
                    expected_proposal_hash=candidate["proposal_hash"],
                    actor_type="admin",
                    actor_ref="reviewed_pet_loss_split_v1",
                )
                results[label] = result

            neko_claim_id = uuid.UUID(results["neko_loss"]["claim_id"])
            await set_actor(conn)
            await conn.execute(
                """
                INSERT INTO memory.claim_relation(
                  owner_user_id, from_claim_id, to_claim_id,
                  relation_type, rationale
                ) VALUES($1, $2, $3, 'depends_on', $4)
                ON CONFLICT DO NOTHING
                """,
                OWNER_USER_ID,
                neko_claim_id,
                NEKO_CORRECTION_CLAIM_ID,
                "Neko event name normalized from source value Nemo by explicit correction claim",
            )

        for label in sorted(results):
            print(json.dumps({"label": label, **results[label]}, sort_keys=True))
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

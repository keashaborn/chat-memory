#!/usr/bin/env python3
"""Probe minimum-useful governed profile recall without model or store writes."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_engine.governed_memory_provider_v1 import (
    LiveGovernedMemoryAssemblyProviderV1,
)
from rag_engine.qdrant_compat import make_qdrant_client
from seebx.capabilities.conversation.snapshot import (
    create_current_only_conversation_snapshot_v1,
)
from seebx.capabilities.conversation.policy import ResponsePolicySignalsV0_2


CASES = (
    ("self_identity", "What is my name?"),
    ("self_profile", "What do you know about me?"),
)


def seed_vector(
    *,
    qdrant_url: str,
    collection: str,
    seed_claim_id: UUID,
) -> list[float]:
    client = make_qdrant_client(url=qdrant_url, timeout=15.0)
    try:
        points = client.retrieve(
            collection_name=collection,
            ids=[str(seed_claim_id)],
            with_vectors=True,
            with_payload=False,
        )
        if len(points) != 1:
            raise RuntimeError("seed governed projection is absent")
        vector = points[0].vector
        if isinstance(vector, dict):
            vector = next(iter(vector.values()))
        return [float(item) for item in vector]
    finally:
        client.close()


async def run(
    *,
    dsn: str,
    qdrant_url: str,
    collection: str,
    owner: UUID,
    seed_claim_id: UUID,
    expect_self_name: bool,
) -> dict[str, object]:
    vector = seed_vector(
        qdrant_url=qdrant_url,
        collection=collection,
        seed_claim_id=seed_claim_id,
    )
    conn = await asyncpg.connect(dsn, command_timeout=30)
    outcomes: list[dict[str, object]] = []
    try:
        for label, query in CASES:
            snapshot = create_current_only_conversation_snapshot_v1(
                authenticated_actor_user_id=owner,
                thread_id=uuid4(),
                current_request_id=f"minimum-useful-{label}-{uuid4()}",
                current_message=query,
            )
            with patch(
                "rag_engine.governed_memory_provider_v1.embed_text",
                return_value=vector,
            ):
                result = await LiveGovernedMemoryAssemblyProviderV1(conn).prepare(
                    authenticated_actor_user_id=owner,
                    conversation_snapshot=snapshot,
                    trusted_policy_signals=ResponsePolicySignalsV0_2(),
                )
            application = result.memory_application
            selected_ids = (
                ()
                if application is None
                else tuple(
                    sorted(
                        str(item.record.record_id)
                        for item in application.injected_records
                    )
                )
            )
            token_count = (
                0 if application is None else application.actual_prompt_tokens
            )
            if label == "self_profile" and (
                not selected_ids or token_count <= 0
            ):
                raise RuntimeError(
                    "broad self profile selected no governed memory"
                )
            if label == "self_identity" and (
                bool(selected_ids) != expect_self_name
            ):
                raise RuntimeError(
                    "self-name projection expectation was not satisfied"
                )
            outcomes.append(
                {
                    "case": label,
                    "selected_count": len(selected_ids),
                    "selected_ids": selected_ids,
                    "actual_prompt_tokens": token_count,
                }
            )
    finally:
        await conn.close()
    return {
        "status": "pass",
        "owner_user_id": str(owner),
        "cases": outcomes,
        "database_writes": 0,
        "external_model_calls": 0,
        "qdrant_writes": 0,
        "answer_generation_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.getenv("POSTGRES_DSN"))
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL"))
    parser.add_argument(
        "--collection",
        default=os.getenv("MEMORY_V1_COLLECTION", "memory_claim_v1"),
    )
    parser.add_argument("--owner-user-id", required=True, type=UUID)
    parser.add_argument("--seed-claim-id", required=True, type=UUID)
    parser.add_argument("--expect-self-name", action="store_true")
    args = parser.parse_args()
    if not args.dsn or not args.qdrant_url:
        raise SystemExit("POSTGRES_DSN and QDRANT_URL are required")
    print(
        json.dumps(
            asyncio.run(
                run(
                    dsn=args.dsn,
                    qdrant_url=args.qdrant_url,
                    collection=args.collection,
                    owner=args.owner_user_id,
                    seed_claim_id=args.seed_claim_id,
                    expect_self_name=args.expect_self_name,
                )
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

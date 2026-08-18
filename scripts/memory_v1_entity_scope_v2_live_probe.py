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
from rag_engine.response_policy_v0_2 import ResponsePolicySignalsV0_2


TARGET_PROJECTIONS = {
    "family_death": UUID("7b5f799a-7461-45e1-b359-0ddb048f3896"),
    "pet_profile": UUID("1f0c47b4-4717-4574-b926-c5800fd88692"),
    "name_correction": UUID("7c1813ff-7569-4713-bbdf-108ba4312e40"),
    "stance": UUID("8fb8b3ab-a627-4555-99c6-fe4dc9b0ca89"),
}
EXPECTED = {
    "family_death": {
        UUID("7b5f799a-7461-45e1-b359-0ddb048f3896"),
    },
    "pet_profile": {
        UUID("1f0c47b4-4717-4574-b926-c5800fd88692"),
        UUID("7c1813ff-7569-4713-bbdf-108ba4312e40"),
        UUID("e4a15ed9-557d-4e16-aa22-342cb34f7f2d"),
        UUID("3a4b2a62-c230-4a77-a674-fe0cfeb55e71"),
    },
    "name_correction": set(),
    "pet_loss": set(),
    "life_context": set(),
    "profession": set(),
    "stance": {
        UUID("8fb8b3ab-a627-4555-99c6-fe4dc9b0ca89"),
    },
}
CASES = (
    ("family_death", "Have I had any deaths in the family?", "family_death"),
    ("pet_profile", "Do you know anything about my pets?", "pet_profile"),
    ("name_correction", "Was it Nemo or Neko?", "name_correction"),
    ("pet_loss", "Do you remember when I lost my pet?", "family_death"),
    (
        "life_context",
        "I’m struggling with caregiving for my wife.",
        "family_death",
    ),
    ("profession", "Do you know what my profession is?", "family_death"),
    (
        "stance",
        "What have I said about mental illness and the concept of hell?",
        "stance",
    ),
)


def projection_vectors(
    qdrant_url: str,
    collection: str,
) -> dict[str, list[float]]:
    client = make_qdrant_client(url=qdrant_url, timeout=15.0)
    try:
        result: dict[str, list[float]] = {}
        for label, point_id in TARGET_PROJECTIONS.items():
            points = client.retrieve(
                collection_name=collection,
                ids=[str(point_id)],
                with_vectors=True,
                with_payload=False,
            )
            if len(points) != 1:
                raise RuntimeError(f"missing target projection: {label}")
            vector = points[0].vector
            if isinstance(vector, dict):
                vector = next(iter(vector.values()))
            result[label] = [float(item) for item in vector]
        return result
    finally:
        client.close()


async def run(
    *,
    dsn: str,
    qdrant_url: str,
    collection: str,
    owner: UUID,
) -> dict[str, object]:
    vectors = projection_vectors(qdrant_url, collection)
    conn = await asyncpg.connect(dsn, command_timeout=30)
    outcomes: list[dict[str, object]] = []
    try:
        for label, query, vector_label in CASES:
            snapshot = create_current_only_conversation_snapshot_v1(
                authenticated_actor_user_id=owner,
                thread_id=uuid4(),
                current_request_id=f"entity-scope-live-{label}-{uuid4()}",
                current_message=query,
            )
            with patch(
                "rag_engine.governed_memory_provider_v1.embed_text",
                return_value=vectors[vector_label],
            ):
                result = await LiveGovernedMemoryAssemblyProviderV1(conn).prepare(
                    authenticated_actor_user_id=owner,
                    conversation_snapshot=snapshot,
                    trusted_policy_signals=ResponsePolicySignalsV0_2(),
                )
            application = result.memory_application
            selected = (
                set()
                if application is None
                else {
                    item.record.record_id
                    for item in application.injected_records
                }
            )
            if selected != EXPECTED[label]:
                raise RuntimeError(f"{label} selected an unexpected governed set")
            outcomes.append(
                {
                    "case": label,
                    "selected_count": len(selected),
                    "selected_ids": sorted(str(item) for item in selected),
                    "actual_prompt_tokens": (
                        0
                        if application is None
                        else application.actual_prompt_tokens
                    ),
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
        "prompt_answer_calls": 0,
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
                )
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

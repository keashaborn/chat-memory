from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import struct
import sys
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_engine.governed_memory_provider_v1 import (  # noqa: E402
    LiveGovernedMemoryAssemblyProviderV1,
)
from rag_engine.memory_v1_intent import classify_memory_intent  # noqa: E402
from rag_engine.memory_v1_projection import ClaimVectorIndex  # noqa: E402
from rag_engine.memory_v1_shadow import governed_activation_allowlisted  # noqa: E402
from rag_engine.qdrant_compat import make_qdrant_client  # noqa: E402
from rag_engine.response_conversation_snapshot_v1 import (  # noqa: E402
    create_current_only_conversation_snapshot_v1,
)
from rag_engine.response_policy_v0_2 import ResponsePolicySignalsV0_2  # noqa: E402


CONTRACT_VERSION = "memory_v1_v5_2_evidence_context_stance_typed_shadow_v1"
TARGET_CLAIM_ID = UUID("2759c735-f629-468c-bbb8-8947ccb5fdbc")
TARGET_PREDICATE = "stance.reported"
TARGET_REVISION = 2
QUERY = "What have I said about how Fractal Monism can help people?"
OTHER_OWNER = UUID("557ea042-cb82-48f8-9429-472e96c957ef")


def _stable_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _vector_sha256(vector: Sequence[float]) -> str:
    values = tuple(float(item) for item in vector)
    return hashlib.sha256(struct.pack(f"!{len(values)}d", *values)).hexdigest()


def _collection_snapshot(
    qdrant_url: str,
    collection: str,
) -> tuple[list[float], str, int]:
    client = make_qdrant_client(url=qdrant_url, timeout=15.0)
    try:
        target = client.retrieve(
            collection_name=collection,
            ids=[str(TARGET_CLAIM_ID)],
            with_vectors=True,
            with_payload=True,
        )
        if len(target) != 1:
            raise RuntimeError("the controlled stance projection is absent")
        point = target[0]
        payload = dict(point.payload or {})
        if UUID(str(point.id)) != TARGET_CLAIM_ID:
            raise RuntimeError("the controlled stance projection ID changed")
        if str(payload.get("owner_user_id") or "") == "":
            raise RuntimeError("the controlled stance projection lacks an owner")
        if str(payload.get("claim_id") or "") != str(TARGET_CLAIM_ID):
            raise RuntimeError("the controlled stance projection claim ID changed")
        if str(payload.get("predicate") or "") != TARGET_PREDICATE:
            raise RuntimeError("the controlled stance projection predicate changed")
        if str(payload.get("status") or "") != "supported":
            raise RuntimeError("the controlled stance projection is not supported")
        if int(payload.get("revision_number") or 0) != TARGET_REVISION:
            raise RuntimeError("the controlled stance projection revision changed")
        vector = point.vector
        if isinstance(vector, dict):
            vector = next(iter(vector.values()))
        target_vector = [float(item) for item in vector]
        if not target_vector:
            raise RuntimeError("the controlled stance projection has no vector")

        rows: list[dict[str, object]] = []
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=collection,
                limit=100,
                offset=offset,
                with_vectors=True,
                with_payload=True,
            )
            for current in points:
                current_vector = current.vector
                if isinstance(current_vector, dict):
                    current_vector = next(iter(current_vector.values()))
                current_payload = dict(current.payload or {})
                rows.append(
                    {
                        "point_id": str(current.id),
                        "owner_user_id": str(
                            current_payload.get("owner_user_id") or ""
                        ),
                        "claim_id": str(current_payload.get("claim_id") or ""),
                        "predicate": str(current_payload.get("predicate") or ""),
                        "status": str(current_payload.get("status") or ""),
                        "revision_number": int(
                            current_payload.get("revision_number") or 0
                        ),
                        "vector_sha256": _vector_sha256(current_vector or ()),
                    }
                )
            if offset is None:
                break
        return (
            target_vector,
            _stable_sha256(sorted(rows, key=lambda item: item["point_id"])),
            len(rows),
        )
    finally:
        client.close()


def _other_owner_target_absent(
    *,
    qdrant_url: str,
    collection: str,
    vector: Sequence[float],
) -> tuple[bool, int]:
    client = make_qdrant_client(url=qdrant_url, timeout=15.0)
    try:
        index = ClaimVectorIndex(
            client,
            collection_name=collection,
            vector_size=len(vector),
        )
        hits = index.search_claims(OTHER_OWNER, vector, limit=100)
    finally:
        client.close()
    ids = {UUID(str(item["claim_id"])) for item in hits}
    return TARGET_CLAIM_ID not in ids, len(ids)


async def run(
    *,
    dsn: str,
    qdrant_url: str,
    collection: str,
    owner: UUID,
) -> dict[str, object]:
    vector, qdrant_before, points_before = _collection_snapshot(
        qdrant_url,
        collection,
    )
    if not governed_activation_allowlisted(owner):
        raise RuntimeError("the target owner is not governed-memory activated")

    intent = classify_memory_intent(QUERY, request_classification="GENERAL")
    claim_context = dict(intent.get("claim_context") or {})
    if not intent.get("routes", {}).get("governed_claims"):
        raise RuntimeError("the stance question did not open the governed claim lane")
    if claim_context.get("allowed_predicates") != [TARGET_PREDICATE]:
        raise RuntimeError("the stance question received an unexpected predicate policy")
    if not claim_context.get("explicit_recall"):
        raise RuntimeError("the stance question was not classified as explicit recall")

    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            snapshot = create_current_only_conversation_snapshot_v1(
                authenticated_actor_user_id=owner,
                thread_id=uuid4(),
                current_request_id=f"stance-typed-shadow-{uuid4()}",
                current_message=QUERY,
            )
            with patch(
                "rag_engine.governed_memory_provider_v1.embed_text",
                return_value=list(vector),
            ):
                result = await LiveGovernedMemoryAssemblyProviderV1(conn).prepare(
                    authenticated_actor_user_id=owner,
                    conversation_snapshot=snapshot,
                    trusted_policy_signals=ResponsePolicySignalsV0_2(),
                )
            if result.memory_input is None:
                raise RuntimeError("the typed selector did not create a memory input")
            application = result.memory_application
            if application is None:
                raise RuntimeError("the typed selector did not create an application")
            selected_ids = tuple(
                item.record.record_id for item in application.injected_records
            )
            if TARGET_CLAIM_ID not in selected_ids:
                raise RuntimeError("the typed selector omitted the target stance claim")
            if not application.memory_content_included:
                raise RuntimeError("the typed selector suppressed the selected stance")
            if application.actual_prompt_tokens <= 0:
                raise RuntimeError("the offline governed render used no tokens")

            envelope_payload = result.memory_input.envelope.model_dump(mode="json")
            assembly_payload = result.memory_input.model_dump(mode="json")
            selected_record_payloads = [
                item.record.model_dump(mode="json")
                for item in application.injected_records
            ]
    finally:
        await conn.close()

    target_absent, other_owner_candidates = _other_owner_target_absent(
        qdrant_url=qdrant_url,
        collection=collection,
        vector=vector,
    )
    if not target_absent:
        raise RuntimeError("the target stance projection crossed the owner boundary")

    _, qdrant_after, points_after = _collection_snapshot(qdrant_url, collection)
    if qdrant_before != qdrant_after or points_before != points_after:
        raise RuntimeError("Qdrant changed during the read-only typed shadow probe")

    return {
        "contract_version": CONTRACT_VERSION,
        "status": "pass",
        "owner_user_id_sha256": hashlib.sha256(str(owner).encode()).hexdigest(),
        "target_claim_present": True,
        "selected_count": len(selected_ids),
        "selected_set_sha256": _stable_sha256(
            sorted(str(item) for item in selected_ids)
        ),
        "selected_records_sha256": _stable_sha256(selected_record_payloads),
        "selection_envelope_sha256": _stable_sha256(envelope_payload),
        "prompt_assembly_input_sha256": _stable_sha256(assembly_payload),
        "memory_included": True,
        "memory_estimated_tokens": application.actual_prompt_tokens,
        "memory_binding": "shadow_only_not_bound_to_answer",
        "intent_version": str(intent.get("version") or ""),
        "intent_domain": str(claim_context.get("domain") or ""),
        "predicate_policy": [TARGET_PREDICATE],
        "explicit_recall": True,
        "other_owner_target_absent": True,
        "other_owner_candidate_count": other_owner_candidates,
        "database_transaction_read_only": True,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "answer_model_calls": 0,
        "live_prompt_influence": False,
        "qdrant_unchanged": True,
        "qdrant_collection_point_count": points_after,
        "qdrant_collection_fingerprint_sha256": qdrant_after,
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

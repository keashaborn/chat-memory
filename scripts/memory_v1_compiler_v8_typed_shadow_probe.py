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
from rag_engine.memory_v1_governed_postgres_loaders_v1 import (  # noqa: E402
    load_governed_entity_scope_snapshot_v2,
    load_governed_v5_claim_rows_v2,
)
from rag_engine.memory_v1_intent import classify_memory_intent  # noqa: E402
from rag_engine.memory_v1_projection import ClaimVectorIndex  # noqa: E402
from rag_engine.memory_v1_shadow import governed_activation_allowlisted  # noqa: E402
from rag_engine.qdrant_compat import make_qdrant_client  # noqa: E402
from seebx.capabilities.conversation.snapshot import (  # noqa: E402
    create_current_only_conversation_snapshot_v1,
)
from rag_engine.response_policy_v0_2 import ResponsePolicySignalsV0_2  # noqa: E402


CLINICAL_PSYCHOLOGIST = UUID("2c3ab91d-c423-43d9-8d55-19e4f1069028")
BCBA = UUID("3073b518-1f12-4fa3-93d6-eaf0b22f864c")
SPOUSE = UUID("b93c565d-6511-4648-b5e4-5c441e3373f8")
CAREGIVER = UUID("f4688838-7193-4e0e-961f-c9bbbf9904c7")
TARGET_IDS = (
    CLINICAL_PSYCHOLOGIST,
    BCBA,
    SPOUSE,
    CAREGIVER,
)
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


def _projection_snapshot(
    qdrant_url: str,
    collection: str,
) -> tuple[dict[UUID, list[float]], str]:
    client = make_qdrant_client(url=qdrant_url, timeout=15.0)
    try:
        points = client.retrieve(
            collection_name=collection,
            ids=[str(item) for item in TARGET_IDS],
            with_vectors=True,
            with_payload=True,
        )
        if len(points) != len(TARGET_IDS):
            raise RuntimeError("one or more controlled projections are absent")
        vectors: dict[UUID, list[float]] = {}
        fingerprint_rows: list[dict[str, object]] = []
        for point in points:
            point_id = UUID(str(point.id))
            if point_id not in TARGET_IDS or point_id in vectors:
                raise RuntimeError("projection retrieval returned an unexpected point")
            vector = point.vector
            if isinstance(vector, dict):
                vector = next(iter(vector.values()))
            values = [float(item) for item in vector]
            vectors[point_id] = values
            payload = dict(point.payload or {})
            fingerprint_rows.append(
                {
                    "point_id": str(point_id),
                    "owner_user_id": str(payload.get("owner_user_id") or ""),
                    "claim_id": str(payload.get("claim_id") or ""),
                    "predicate": str(payload.get("predicate") or ""),
                    "sensitivity": str(payload.get("sensitivity") or ""),
                    "status": str(payload.get("status") or ""),
                    "vector_sha256": _vector_sha256(values),
                }
            )
        return vectors, _stable_sha256(
            sorted(fingerprint_rows, key=lambda item: str(item["point_id"]))
        )
    finally:
        client.close()


async def _select(
    *,
    conn: object,
    owner: UUID,
    query: str,
    vector: Sequence[float],
    expected_ids: set[UUID],
    allow_explicit_high: bool,
    case: str,
) -> dict[str, object]:
    snapshot = create_current_only_conversation_snapshot_v1(
        authenticated_actor_user_id=owner,
        thread_id=uuid4(),
        current_request_id=f"compiler-v8-typed-shadow-{case}-{uuid4()}",
        current_message=query,
    )
    policy_patch = {
        "MEMORY_V1_GOVERNED_EXPLICIT_HIGH_USER_IDS": (
            str(owner) if allow_explicit_high else ""
        ),
        "MEMORY_V1_GOVERNED_EXPLICIT_RECALL_MAX_SENSITIVITY": "high",
    }
    with patch.dict(os.environ, policy_patch, clear=False), patch(
        "rag_engine.governed_memory_provider_v1.embed_text",
        return_value=list(vector),
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
        else {item.record.record_id for item in application.injected_records}
    )
    if selected != expected_ids:
        raise RuntimeError(
            f"{case} selected an unexpected governed set: "
            f"selected={sorted(str(item) for item in selected)} "
            f"expected={sorted(str(item) for item in expected_ids)}"
        )
    return {
        "case": case,
        "memory_included": bool(
            application is not None and application.memory_content_included
        ),
        "selected_count": len(selected),
        "selected_set_sha256": _stable_sha256(
            sorted(str(item) for item in selected)
        ),
        "actual_prompt_tokens": (
            0 if application is None else application.actual_prompt_tokens
        ),
        "allow_explicit_high_test_override": allow_explicit_high,
    }


async def run(
    *,
    dsn: str,
    qdrant_url: str,
    collection: str,
    owner: UUID,
) -> dict[str, object]:
    vectors, qdrant_before = _projection_snapshot(qdrant_url, collection)
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if not governed_activation_allowlisted(owner):
            raise RuntimeError("target owner is not governed-memory activated")
        profession_plan = classify_memory_intent(
            "What professions have I worked in?",
            request_classification="GENERAL",
        )
        if not profession_plan.get("routes", {}).get("governed_claims"):
            raise RuntimeError("profession query did not open the governed claim lane")
        scope_batch = await load_governed_entity_scope_snapshot_v2(conn, owner)
        if scope_batch.get("snapshot") is None:
            raise RuntimeError("target owner has no governed entity-scope snapshot")
        diagnostic_client = make_qdrant_client(url=qdrant_url, timeout=15.0)
        try:
            diagnostic_index = ClaimVectorIndex(
                diagnostic_client,
                collection_name=collection,
                vector_size=len(vectors[CLINICAL_PSYCHOLOGIST]),
            )
            diagnostic_hits = diagnostic_index.search_claims(
                owner,
                vectors[CLINICAL_PSYCHOLOGIST],
                limit=24,
            )
        finally:
            diagnostic_client.close()
        diagnostic_ids = tuple(
            UUID(str(item["claim_id"])) for item in diagnostic_hits
        )
        if not diagnostic_ids:
            raise RuntimeError("owner-filtered Qdrant search returned no candidates")
        diagnostic_rows = await load_governed_v5_claim_rows_v2(
            conn,
            owner,
            diagnostic_ids,
        )
        if not diagnostic_rows.get("records"):
            raise RuntimeError("Postgres revalidation returned no governed rows")
        cases = [
            await _select(
                conn=conn,
                owner=owner,
                query="What professions have I worked in?",
                vector=vectors[CLINICAL_PSYCHOLOGIST],
                expected_ids={CLINICAL_PSYCHOLOGIST, BCBA},
                allow_explicit_high=False,
                case="profession_general",
            ),
            await _select(
                conn=conn,
                owner=owner,
                query="Have I worked as a BCBA?",
                vector=vectors[BCBA],
                expected_ids={BCBA},
                allow_explicit_high=False,
                case="profession_named",
            ),
            await _select(
                conn=conn,
                owner=owner,
                query="Who is my spouse?",
                vector=vectors[SPOUSE],
                expected_ids={SPOUSE},
                allow_explicit_high=False,
                case="spouse",
            ),
            await _select(
                conn=conn,
                owner=owner,
                query="Do you remember who I care for?",
                vector=vectors[CAREGIVER],
                expected_ids=set(),
                allow_explicit_high=False,
                case="caregiver_policy_suppressed",
            ),
            await _select(
                conn=conn,
                owner=owner,
                query="Do you remember who I care for?",
                vector=vectors[CAREGIVER],
                expected_ids={CAREGIVER},
                allow_explicit_high=True,
                case="caregiver_explicit_high_test",
            ),
            await _select(
                conn=conn,
                owner=owner,
                query="Am I a caregiver for Monika?",
                vector=vectors[CAREGIVER],
                expected_ids={CAREGIVER},
                allow_explicit_high=True,
                case="caregiver_named_match",
            ),
            await _select(
                conn=conn,
                owner=owner,
                query="Am I a caregiver for Cindy?",
                vector=vectors[CAREGIVER],
                expected_ids=set(),
                allow_explicit_high=True,
                case="caregiver_named_nonmatch",
            ),
            await _select(
                conn=conn,
                owner=OTHER_OWNER,
                query="What professions have I worked in?",
                vector=vectors[CLINICAL_PSYCHOLOGIST],
                expected_ids=set(),
                allow_explicit_high=False,
                case="inactive_other_owner",
            ),
        ]
    finally:
        await conn.close()
    _, qdrant_after = _projection_snapshot(qdrant_url, collection)
    if qdrant_before != qdrant_after:
        raise RuntimeError("Qdrant changed during the read-only probe")
    return {
        "contract_version": "memory_v1_compiler_v8_typed_shadow_probe_v1",
        "status": "pass",
        "owner_user_id_sha256": hashlib.sha256(str(owner).encode()).hexdigest(),
        "cases": cases,
        "case_count": len(cases),
        "diagnostics": {
            "activation": True,
            "profession_route": True,
            "entity_scope_present": True,
            "qdrant_candidate_count": len(diagnostic_ids),
            "postgres_revalidated_count": len(diagnostic_rows["records"]),
        },
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "answer_model_calls": 0,
        "prompt_influence": False,
        "qdrant_unchanged": True,
        "qdrant_target_fingerprint_sha256": qdrant_after,
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

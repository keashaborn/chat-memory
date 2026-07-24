from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_engine.memory_v1_entity_scope_resolver_v2 import (
    resolve_memory_claim_selector_context_v2,
)
from rag_engine.memory_v1_governed_postgres_loaders_v1 import (
    load_governed_entity_scope_snapshot_v2,
)
from rag_engine.memory_v1_intent import classify_memory_intent
from rag_engine.memory_v1_selection_envelope import (
    MemoryLane,
    MemorySelectionBudgetPolicyV1,
    MemorySelectionRequestV1,
    QueryEmbeddingArtifactV1,
    QueryEmbeddingSource,
    SelectionDirective,
    Sensitivity,
    SourceContractVersionV1,
)


SOURCE = SourceContractVersionV1(
    name="claim_projection",
    version="memory_projection_v5",
)


def request(owner: UUID, query: str) -> MemorySelectionRequestV1:
    vector = (0.125, -0.25, 0.5)
    return MemorySelectionRequestV1.create(
        intent_adapter_version="memory_intent_adapter_v14",
        source_contract_versions=(SOURCE,),
        selection_trace_id=uuid4(),
        authenticated_actor_user_id=owner,
        owner_user_id=owner,
        request_id=f"entity-scope-clone-{uuid4()}",
        thread_id=uuid4(),
        query_text=query,
        query_vector=vector,
        query_embedding=QueryEmbeddingArtifactV1.from_vector(
            source=QueryEmbeddingSource.PRIVATE_LOCAL,
            model_version="synthetic-clone-vector",
            vector=vector,
        ),
        memory_intent="personal_recall",
        domains=("personal",),
        requested_lanes=(MemoryLane.CLAIM,),
        selection_directive=SelectionDirective.EVALUATE,
        explicit_recall=True,
        project_key=None,
        component_key=None,
        max_sensitivity=Sensitivity.HIGH,
        selected_at=datetime.now(timezone.utc),
        budget_policy=MemorySelectionBudgetPolicyV1.standard(),
    )


async def run(dsn: str, owner: UUID) -> dict[str, object]:
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        batch = await load_governed_entity_scope_snapshot_v2(conn, owner)
    finally:
        await conn.close()
    snapshot = batch["snapshot"]
    names = {item.entity_id: item.normalized_name for item in snapshot.entities}
    cases = (
        ("pet_profile", "Do you know anything about my pets?"),
        ("name_correction", "Was it Nemo or Neko?"),
        ("pet_loss", "Do you remember when I lost my pet?"),
        ("family_death", "Have I had any deaths in the family?"),
        ("stance_recall", "What have I said about evidence?"),
    )
    resolved: dict[str, dict[str, tuple[set[str], set[str]]]] = {}
    for label, query in cases:
        plan = classify_memory_intent(query, request_classification="GENERAL")
        context = resolve_memory_claim_selector_context_v2(
            request=request(owner, query),
            claim_context=plan["claim_context"],
            snapshot=snapshot,
        )
        resolved[label] = {
            rule.predicate: (
                {names[entity_id] for entity_id in rule.subject_entity_ids},
                {names[entity_id] for entity_id in rule.object_entity_ids},
            )
            for rule in context.entity_scope.predicate_rules
        }

    if resolved["pet_profile"]["identity.name"][0] != {"dahlia"}:
        raise RuntimeError("generic pet profile scope changed")
    if resolved["pet_profile"]["relationship.has_pet"] != (
        {"self"},
        {"dahlia"},
    ):
        raise RuntimeError("generic pet relationship scope changed")
    if resolved["name_correction"]["identity.name_canonical"][0] != {"neko"}:
        raise RuntimeError("name correction did not resolve only Neko")
    if resolved["pet_loss"]["life_event.died"][0] != {"dahlia"}:
        raise RuntimeError("generic pet loss crossed the governed pet edge")
    if not {"mother", "dad"}.issubset(
        resolved["family_death"]["life_event.died"][0]
    ):
        raise RuntimeError("family death scope lost governed relatives")
    if resolved["stance_recall"]["stance.reported"][0] != {"self"}:
        raise RuntimeError("stance scope is not owner-self only")
    return {
        "status": "pass",
        "snapshot_sha256": snapshot.snapshot_sha256,
        "entity_count": len(snapshot.entities),
        "edge_count": len(snapshot.edges),
        "database_writes": batch["database_writes"],
        "external_calls": 0,
        "qdrant_reads": 0,
        "qdrant_writes": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--owner-user-id", required=True, type=UUID)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(run(args.dsn, args.owner_user_id)),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio

import pytest

from rag_engine.memory_v1_governed_lane_adapter_common import (
    GovernedLaneAdapterError,
)
from rag_engine.memory_v1_selection_envelope import MemoryLane
from tests.test_memory_v1_governed_lane_adapters import (
    CLAIM_SOURCE,
    claim_adapter,
    enriched_claim_row,
    lane_limit,
    request,
    uid,
)


def test_context_overlap_preserves_epistemic_stance() -> None:
    row = enriched_claim_row(uid(1))
    row["evidence_by_stance"] = {
        "context": [str(uid(101))],
        "opposes": [],
        "qualifies": [],
        "supports": [str(uid(101))],
    }
    result = asyncio.run(
        claim_adapter(
            (row,),
            predicate_permissions=("relationship.has_pet",),
        ).select(
            request(
                MemoryLane.CLAIM,
                CLAIM_SOURCE,
                memory_intent="specific_recall",
            ),
            lane_limit(MemoryLane.CLAIM),
        )
    )
    assert len(result.records) == 1
    evidence = result.records[0].evidence_by_stance
    assert evidence.supports == (uid(101),)
    assert evidence.context == ()
    assert evidence.all_evidence() == (uid(101),)


@pytest.mark.parametrize(
    ("first", "second"),
    (
        ("supports", "opposes"),
        ("supports", "qualifies"),
        ("opposes", "qualifies"),
    ),
)
def test_multiple_epistemic_stances_fail_closed(
    first: str,
    second: str,
) -> None:
    row = enriched_claim_row(uid(1))
    row["evidence_by_stance"] = {
        "context": [],
        "opposes": [],
        "qualifies": [],
        "supports": [],
    }
    row["evidence_by_stance"][first] = [str(uid(101))]
    row["evidence_by_stance"][second] = [str(uid(101))]
    with pytest.raises(
        GovernedLaneAdapterError,
        match="multiple epistemic stances",
    ):
        asyncio.run(
            claim_adapter(
                (row,),
                predicate_permissions=("relationship.has_pet",),
            ).select(
                request(
                    MemoryLane.CLAIM,
                    CLAIM_SOURCE,
                    memory_intent="specific_recall",
                ),
                lane_limit(MemoryLane.CLAIM),
            )
        )

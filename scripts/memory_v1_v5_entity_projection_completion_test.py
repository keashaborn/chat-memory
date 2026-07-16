#!/usr/bin/env python3
from __future__ import annotations

from memory_v1_v5_entity_projection_completion import evaluate


def snapshot() -> tuple[dict, dict]:
    database = {
        "occupation": {
            "observation_rows": 1,
            "self_resolution_apply_rows": 1,
            "trainer_resolution_apply_rows": 1,
            "binding_rows": 1,
            "projection_plan_rows": 1,
            "projection_item_rows": 1,
            "projection_review_rows": 1,
            "projection_apply_rows": 1,
            "projection_dispatch_rows": 1,
            "candidate_claim_rows": 1,
        },
        "correction": {
            "observation_rows": 1,
            "deferred_plan_rows": 1,
            "resolution_apply_rows": 0,
            "binding_rows": 0,
            "projection_input_rows": 0,
        },
        "isolation": {"other_owner_v5_rows": 0},
        "registry": {
            "status": "proposed",
            "runtime_active": False,
            "contract_rows": 44,
        },
    }
    qdrant = {
        "point_count": 26,
        "payload_snapshot_sha256": "a" * 64,
        "v5_candidate_claim_present": False,
    }
    return database, qdrant


def main() -> None:
    database, qdrant = snapshot()
    result = evaluate(database, qdrant)
    assert result["phase_complete"] is True
    assert result["next_boundary"] == (
        "build_read_only_v5_shadow_retrieval_without_prompt_exposure"
    )

    leaked_database, leaked_qdrant = snapshot()
    leaked_database["isolation"]["other_owner_v5_rows"] = 1
    assert evaluate(leaked_database, leaked_qdrant)["phase_complete"] is False

    projected_database, projected_qdrant = snapshot()
    projected_qdrant["v5_candidate_claim_present"] = True
    assert evaluate(projected_database, projected_qdrant)["phase_complete"] is False

    guessed_database, guessed_qdrant = snapshot()
    guessed_database["correction"]["resolution_apply_rows"] = 1
    assert evaluate(guessed_database, guessed_qdrant)["phase_complete"] is False

    active_database, active_qdrant = snapshot()
    active_database["registry"]["runtime_active"] = True
    assert evaluate(active_database, active_qdrant)["phase_complete"] is False

    print("memory_v1_v5_entity_projection_completion: PASS")


if __name__ == "__main__":
    main()

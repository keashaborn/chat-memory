#!/usr/bin/env python3
from __future__ import annotations

import asyncio

import memory_v1_v5_2_compiler_v8_claim_apply_manifest as materialize


materialize.REVIEW_MANIFEST_CONTRACT = (
    "memory_v1_v5_2_canonical_name_claim_review_manifest_v1"
)
materialize.REVIEW_RESULT_CONTRACT = (
    "memory_v1_v5_2_canonical_name_claim_review_result_v1"
)
materialize.REQUEST_NAMESPACE = (
    "memory-v1-v5-2-canonical-name-claim-materialization"
)
materialize.ASSESSMENT = {
    "action": "promote_supported",
    "support_score": "1.000",
    "opposition_score": "0.000",
    "claim_confidence": "0.980",
    "assessment_confidence": "0.990",
    "reason_codes": [
        "accepted_observation_entailment_reused",
        "authorized_projection_review",
        "correction_target_reconciled",
        "canonical_name_normalized",
        "no_active_opposition",
    ],
    "rationale": (
        "The owner-authored correction has an accepted entailment, an exact "
        "reconciled animal entity, and an authorized canonical-name review."
    ),
    "reviewer_type": "system",
    "reviewer_ref": (
        "memory_v1_v5_2_canonical_name_claim_materialization_20260725"
    ),
}
materialize.TARGETS = {
    "be564d98-8dbc-5b92-90c3-a10cebfd982b": {
        "observation_id": "93024235-89a8-49d5-88fa-7e4a143b68f3",
        "predicate": "identity.name_canonical",
        "canonical_text": "Neko's canonical name is Neko.",
    },
}


if __name__ == "__main__":
    raise SystemExit(asyncio.run(materialize.run()))

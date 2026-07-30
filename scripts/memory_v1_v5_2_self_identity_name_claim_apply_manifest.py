#!/usr/bin/env python3
from __future__ import annotations

import asyncio

import memory_v1_v5_2_compiler_v8_claim_apply_manifest as materialize


materialize.REVIEW_MANIFEST_CONTRACT = (
    "memory_v1_v5_2_self_identity_name_claim_review_manifest_v1"
)
materialize.REVIEW_RESULT_CONTRACT = (
    "memory_v1_v5_2_self_identity_name_claim_review_result_v1"
)
materialize.REQUEST_NAMESPACE = (
    "memory-v1-v5-2-self-identity-name-claim-materialization"
)
materialize.ASSESSMENT = {
    "action": "promote_supported",
    "support_score": "1.000",
    "opposition_score": "0.000",
    "claim_confidence": "0.990",
    "assessment_confidence": "0.990",
    "reason_codes": [
        "accepted_observation_entailment",
        "authorized_projection_review",
        "owner_authored_identity",
        "trusted_owner_self_binding",
        "no_active_opposition",
    ],
    "rationale": (
        "Direct owner-authored evidence has an accepted identity-name "
        "entailment, a trusted owner-self entity binding, an authorized "
        "projection review, and no active opposition."
    ),
    "reviewer_type": "system",
    "reviewer_ref": (
        "memory_v1_v5_2_self_identity_name_claim_materialization_20260730"
    ),
}
materialize.TARGETS = {
    "0b1a280d-6170-5f6a-a738-adc342188c89": {
        "observation_id": "fc86c43e-3fa6-465e-b348-8656e3a896c0",
        "predicate": "identity.name",
        "canonical_text": "The user's name is Eric.",
    },
}


if __name__ == "__main__":
    raise SystemExit(asyncio.run(materialize.run()))

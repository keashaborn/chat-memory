#!/usr/bin/env python3
from __future__ import annotations

import asyncio

import memory_v1_v5_2_compiler_v8_claim_apply_manifest as base


base.REQUEST_NAMESPACE = "memory-v1-v5-2-evidence-context-stance-claim"
base.TARGETS = {
    "0685b77e-de79-5216-b58d-6d7e464166e6": {
        "observation_id": "87ce1a11-01ae-4d6f-80ea-8e62b5b43cff",
        "predicate": "stance.reported",
        "canonical_text": (
            'The user reports this position: '
            '"Fractal Monism will help people in life."'
        ),
    },
}
base.ASSESSMENT = {
    "action": "promote_supported",
    "support_score": "1.000",
    "opposition_score": "0.000",
    "claim_confidence": "0.920",
    "assessment_confidence": "0.950",
    "reason_codes": [
        "accepted_reported_stance_observation",
        "attributed_belief_only",
        "authorized_projection_review",
        "no_active_opposition_to_attribution",
    ],
    "rationale": (
        "Direct owner-authored evidence supports the attributed claim that the "
        "user reported this belief. It does not establish the belief's object "
        "as an external fact."
    ),
    "reviewer_type": "system",
    "reviewer_ref": "memory_v1_v5_2_evidence_context_stance_20260726",
}


if __name__ == "__main__":
    raise SystemExit(asyncio.run(base.run()))

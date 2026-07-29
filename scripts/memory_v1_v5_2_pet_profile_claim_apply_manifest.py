#!/usr/bin/env python3
from __future__ import annotations

import asyncio

from scripts import memory_v1_v5_2_compiler_v8_claim_apply_manifest as apply_manifest
from scripts import memory_v1_v5_2_compiler_v8_claim_stage as base_stage
from scripts.memory_v1_v5_2_pet_profile_claim_stage import OWNER, TARGETS


DEFERRED_OBSERVATION = "bc8866ad-95e8-4413-832e-813f601eece6"


def plan_targets() -> dict[str, dict[str, str]]:
    targets: dict[str, dict[str, str]] = {}
    for observation_id, target in TARGETS.items():
        plan_id = base_stage.stable_id(
            "plan",
            OWNER,
            target["evidence_id"],
            observation_id,
        )
        targets[plan_id] = {
            "observation_id": observation_id,
            "predicate": target["predicate"],
            "canonical_text": target["canonical_text"],
        }
    return targets


def configure() -> None:
    apply_manifest.REVIEW_MANIFEST_CONTRACT = (
        "memory_v1_v5_2_pet_profile_claim_review_manifest_v1"
    )
    apply_manifest.REVIEW_RESULT_CONTRACT = (
        "memory_v1_v5_2_pet_profile_claim_review_result_v1"
    )
    apply_manifest.REQUEST_NAMESPACE = "memory-v1-v5-2-pet-profile-claim"
    apply_manifest.ASSESSMENT = {
        **apply_manifest.ASSESSMENT,
        "reason_codes": [
            "accepted_observation_entailment",
            "active_owner_evidence",
            "authorized_projection_review",
            "no_active_opposition",
            "pet_profile_semantics_reviewed",
        ],
        "rationale": (
            "Direct owner-authored evidence has accepted atomic pet-profile "
            "entailment, reviewed entity bindings and temporal semantics, and "
            "no active opposition for this exact claim identity."
        ),
        "reviewer_ref": "memory_v1_v5_2_pet_profile_claim_materialization_20260729",
    }
    apply_manifest.TARGETS = plan_targets()


if __name__ == "__main__":
    configure()
    raise SystemExit(asyncio.run(apply_manifest.run()))

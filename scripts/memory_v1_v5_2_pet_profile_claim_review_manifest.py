#!/usr/bin/env python3
from __future__ import annotations

import asyncio

from scripts import memory_v1_v5_2_compiler_v8_claim_review_manifest as review


def configure() -> None:
    review.CONTRACT = "memory_v1_v5_2_pet_profile_claim_review_manifest_v1"
    review.DECISION_CONTRACT = (
        "memory_v1_v5_2_pet_profile_claim_review_decisions_v1"
    )
    review.STAGE_CONTRACT = "memory_v1_v5_2_pet_profile_claim_stage_manifest_v1"
    review.REVIEWER_REF = "memory_v1_v5_2_pet_profile_claim_review_20260729"


if __name__ == "__main__":
    configure()
    raise SystemExit(asyncio.run(review.run()))

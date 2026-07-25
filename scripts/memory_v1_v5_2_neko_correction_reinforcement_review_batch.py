#!/usr/bin/env python3
from __future__ import annotations

import asyncio

from scripts import memory_v1_v5_2_compiler_v8_claim_review_batch as review


review.MANIFEST_CONTRACT = (
    "memory_v1_v5_2_neko_correction_reinforcement_review_manifest_v1"
)
review.RESULT_CONTRACT = (
    "memory_v1_v5_2_neko_correction_reinforcement_review_result_v1"
)
review.EXPECTED_EVIDENCE_COUNT = 1
review.REVIEWER_REF = (
    "controlled_v5_2_neko_correction_reinforcement_review_20260725"
)
review.APPLY_ENV = (
    "MEMORY_V1_V5_2_NEKO_CORRECTION_REINFORCEMENT_REVIEW_APPLY"
)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(review.run()))

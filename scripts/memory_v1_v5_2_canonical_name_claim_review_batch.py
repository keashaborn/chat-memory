#!/usr/bin/env python3
from __future__ import annotations

import asyncio

from scripts import memory_v1_v5_2_compiler_v8_claim_review_batch as review


review.MANIFEST_CONTRACT = (
    "memory_v1_v5_2_canonical_name_claim_review_manifest_v1"
)
review.RESULT_CONTRACT = (
    "memory_v1_v5_2_canonical_name_claim_review_result_v1"
)
review.EXPECTED_EVIDENCE_COUNT = 1
review.REVIEWER_REF = (
    "memory_v1_v5_2_canonical_name_claim_review_20260725"
)
review.APPLY_ENV = "MEMORY_V1_V5_2_CANONICAL_NAME_CLAIM_REVIEW_APPLY"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(review.run()))

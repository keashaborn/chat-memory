#!/usr/bin/env python3
"""One-evidence adapter for the governed V5.2 projection-review executor."""

from __future__ import annotations

import asyncio

from scripts import memory_v1_v5_2_compiler_v8_claim_review_batch as review


review.EXPECTED_EVIDENCE_COUNT = 1
review.APPLY_ENV = "MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STANCE_CLAIM_REVIEW_APPLY"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(review.run()))

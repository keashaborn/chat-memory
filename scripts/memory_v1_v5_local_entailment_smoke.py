#!/usr/bin/env python3
"""One synthetic, private, zero-write entailment transport smoke test."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LOCAL_CALL_ENABLE_TOKEN,
    LlamaCppSecureTransport,
)
from scripts.memory_v1_v5_local_entailment_provider import assess
from scripts.memory_v1_v5_local_inference_canary import PINNED_MODEL_ALIAS


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--endpoint", default="http://127.0.0.1:18080/v1/chat/completions"
    )
    args = parser.parse_args()
    credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not credential_dir:
        raise RuntimeError("systemd credentials are required")
    api_key = (Path(credential_dir) / "local_api_key").read_text().strip()
    transport = LlamaCppSecureTransport(
        endpoint=args.endpoint,
        enable_token=LOCAL_CALL_ENABLE_TOKEN,
        api_key=api_key,
        allow_loopback_http=True,
    )
    assessment = assess(
        transport,
        model=PINNED_MODEL_ALIAS,
        evidence_content="My name is Avery.",
        observation_payload={
            "predicate": "identity.name",
            "polarity": "positive",
            "modality": "asserted",
            "projection_class": "direct_claim",
            "subject": {
                "entity_type": "self",
                "mention_kind": "self_reference",
            },
            "object": {"type": "text", "value": "Avery"},
            "source_spans": [],
        },
        timeout_seconds=300,
        max_output_tokens=128,
    )
    print(json.dumps({
        "contract_version": "memory_v1_v5_local_entailment_smoke_v1",
        "decision": assessment.decision,
        "confidence": assessment.confidence,
        "local_model_calls": assessment.local_model_calls,
        "external_model_calls": 0,
        "database_writes": 0,
        "qdrant_writes": 0,
        "prompt_influence": 0,
        "request_sha256": assessment.request_sha256,
        "response_sha256": assessment.response_sha256,
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

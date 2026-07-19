#!/usr/bin/env python3
"""One synthetic, private, zero-write entity-validation smoke."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LOCAL_CALL_ENABLE_TOKEN,
    LlamaCppSecureTransport,
)
from scripts.memory_v1_v5_local_entity_validation_provider import assess
from scripts.memory_v1_v5_local_inference_canary import PINNED_MODEL_ALIAS


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--endpoint", default="http://127.0.0.1:18080/v1/chat/completions"
    )
    parser.add_argument("--credential-name", default="local_api_key")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise RuntimeError("systemd credential directory is unavailable")
    key = (Path(directory) / args.credential_name).read_text().strip()
    transport = LlamaCppSecureTransport(
        endpoint=args.endpoint,
        enable_token=LOCAL_CALL_ENABLE_TOKEN,
        api_key=key,
        allow_loopback_http=True,
    )
    result = assess(
        transport,
        model=PINNED_MODEL_ALIAS,
        evidence_content="My sister Avery called this morning.",
        entity_mention={
            "entity_type": "person",
            "mention_kind": "named",
            "name_text": "Avery",
            "relationship_role": "sister",
            "source_spans": [{"start": 0, "end": 37}],
        },
    )
    if result.decision != "supported" or result.confidence != "high":
        raise RuntimeError("synthetic entity validation was not supported/high")
    print(
        json.dumps(
            {
                "decision": result.decision,
                "confidence": result.confidence,
                "local_model_calls": result.local_model_calls,
                "external_model_calls": 0,
                "database_writes": 0,
                "qdrant_writes": 0,
                "prompt_influence": 0,
                "request_sha256": result.request_sha256,
                "response_sha256": result.response_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

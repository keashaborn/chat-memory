#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LOCAL_CALL_ENABLE_TOKEN,
    LOCAL_PROVIDER_ID,
    LOCAL_PROVIDER_VERSION,
    LlamaCppSecureTransport,
    LocalLlamaCppProvider,
)
from scripts.memory_v1_relational_extraction_v5_observable_provider import (
    CapturingProvider,
    sanitize_provider_packet,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    load_registry,
    load_schema,
    sha256_text,
    validate_and_normalize,
)


REGISTRY_SHA256 = (
    "4837cc66f8ef41d5b091528c02e06add267586cb170dc0eb4b57fc207bd0f3d8"
)
SCHEMA_SHA256 = (
    "744ce1d466dfe502fb78fd0ba0a996dd723d1593f34bc456c849c20811f99e70"
)
MODEL_FILE_SHA256 = (
    "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
)
CONTENT = "My name is Avery."


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one synthetic, zero-write local V5 extraction smoke test."
    )
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:18080/v1/chat/completions",
    )
    parser.add_argument("--model", default="qwen3-8b-local-extractor")
    parser.add_argument(
        "--model-file-sha256",
        default=MODEL_FILE_SHA256,
    )
    parser.add_argument("--content", default=CONTENT)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    return parser.parse_args()


def main() -> int:
    args = arguments()
    if not isinstance(args.content, str) or not 1 <= len(args.content) <= 5000:
        raise RuntimeError("synthetic content must contain 1 to 5000 characters")
    if "\x00" in args.content:
        raise RuntimeError("synthetic content contains a null byte")
    root = Path(__file__).resolve().parents[1]
    registry = load_registry(
        root / "specs" / "memory_v1_predicate_registry_v5.json",
        REGISTRY_SHA256,
    )
    schema = load_schema(
        root / "specs" / "memory_v1_relational_extraction_v5.schema.json",
        SCHEMA_SHA256,
    )
    source = TrustedExtractionSource.create(
        job_id="00000000-0000-4000-8000-000000000011",
        source_system="public.chat_log",
        source_external_id="00000000-0000-4000-8000-000000000012",
        source_sha256=sha256_text(args.content),
        source_recorded_at="2026-07-17T12:00:00+00:00",
        content=args.content,
    )
    transport = LlamaCppSecureTransport(
        endpoint=args.endpoint,
        enable_token=LOCAL_CALL_ENABLE_TOKEN,
        api_key=os.getenv("MEMORY_V1_LOCAL_INFERENCE_API_KEY"),
        allow_loopback_http=True,
        allow_unauthenticated_loopback=False,
    )
    provider = LocalLlamaCppProvider(
        model=args.model,
        model_file_sha256=args.model_file_sha256,
        runtime_revision="llama.cpp-b10066-86a9c79f8",
        registry=registry,
        transport=transport,
        max_output_tokens=4096,
        timeout_seconds=args.timeout_seconds,
    )
    capturing = CapturingProvider(provider)
    try:
        validated = validate_and_normalize(
            capturing,
            source=source,
            registry=registry,
            schema=schema,
            allowed_provider_versions={
                LOCAL_PROVIDER_ID: LOCAL_PROVIDER_VERSION,
            },
            max_external_model_calls=0,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "contract_version": "memory_v1_v5_local_smoke_v1",
                    "passed": False,
                    "source_sha256": source.source_sha256,
                    "provider_id": provider.provider_id,
                    "provider_version": provider.provider_version,
                    "external_model_calls": provider.external_model_calls,
                    "local_model_calls": provider.local_model_calls,
                    "rejection": {
                        "class": type(exc).__name__,
                        "message_sha256": sha256_text(str(exc)),
                    },
                    "sanitized_provider_packet": (
                        sanitize_provider_packet(capturing.last_packet)
                        if capturing.last_packet is not None
                        else None
                    ),
                    "audit": provider.last_audit,
                    "effects": {
                        "database_writes": 0,
                        "qdrant_writes": 0,
                        "redis_writes": 0,
                        "staging_writes": 0,
                        "claim_writes": 0,
                        "prompt_influence_changes": 0,
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    if validated.external_model_calls != 0 or provider.local_model_calls != 1:
        raise RuntimeError("local/external call accounting changed")
    print(
        json.dumps(
            {
                "contract_version": "memory_v1_v5_local_smoke_v1",
                "passed": True,
                "source_sha256": source.source_sha256,
                "provider_id": validated.provider_id,
                "provider_version": validated.provider_version,
                "external_model_calls": validated.external_model_calls,
                "local_model_calls": provider.local_model_calls,
                "provider_output_sha256": validated.provider_output_sha256,
                "normalized_packet_sha256": (
                    validated.normalized_packet_sha256
                ),
                "manual_review_required": (
                    validated.manual_review_required
                ),
                "counts": {
                    "entity_mentions": len(
                        validated.normalized_packet["entity_mentions"]
                    ),
                    "observations": len(
                        validated.normalized_packet["observations"]
                    ),
                    "comparison_hints": len(
                        validated.normalized_packet["comparison_hints"]
                    ),
                    "deferrals": len(
                        validated.normalized_packet["deferrals"]
                    ),
                },
                "audit": provider.last_audit,
                "effects": {
                    "database_writes": 0,
                    "qdrant_writes": 0,
                    "redis_writes": 0,
                    "staging_writes": 0,
                    "claim_writes": 0,
                    "prompt_influence_changes": 0,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

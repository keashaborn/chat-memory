#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import asyncpg

from rag_engine.memory_v1_evidence_context_loader_v1 import (
    load_memory_evidence_context_v1,
)
from scripts.memory_v1_predicate_runtime_profile_v2 import (
    load_runtime_profile_v2,
)
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LlamaCppSecureTransport,
    LocalLlamaCppProvider,
    LocalProviderAdapterError,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    canonical_json,
    canonical_sha256,
)
from scripts.memory_v1_v5_local_inference_canary import (
    PINNED_MODEL_ALIAS,
    PINNED_MODEL_FILE_SHA256,
    PINNED_RUNTIME_REVISION,
    loopback_dsn,
)


ENABLE_TOKEN = "memory_v1_evidence_context_zero_write_canary_v1"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one private zero-write contextual extraction canary."
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--target-evidence-id", required=True)
    parser.add_argument("--expected-target-content-sha256", required=True)
    parser.add_argument("--packet-output", required=True)
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:18080/v1/chat/completions",
    )
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    return parser.parse_args()


async def load_context(args: argparse.Namespace):
    dsn = loopback_dsn(os.environ["POSTGRES_DSN"])
    conn = await asyncpg.connect(dsn)
    try:
        return await load_memory_evidence_context_v1(
            conn,
            expected_owner_user_id=args.owner_user_id,
            target_evidence_id=args.target_evidence_id,
            expected_target_content_sha256=(
                args.expected_target_content_sha256
            ),
        )
    finally:
        await conn.close()


def main() -> int:
    args = arguments()
    if os.getenv("MEMORY_V1_EVIDENCE_CONTEXT_CANARY") != ENABLE_TOKEN:
        raise RuntimeError("evidence-context canary capability is absent")
    api_key = os.getenv("MEMORY_V1_LOCAL_INFERENCE_API_KEY")
    if not api_key:
        raise RuntimeError("private local inference credential is absent")
    if not 1000 <= args.max_output_tokens <= 8192:
        raise RuntimeError("max output tokens must be between 1000 and 8192")
    if not 30 <= args.timeout_seconds <= 900:
        raise RuntimeError("timeout must be between 30 and 900 seconds")

    root = Path(__file__).resolve().parents[1]
    packet_path = Path(args.packet_output)
    if packet_path.exists():
        raise RuntimeError("packet output already exists")
    context = asyncio.run(load_context(args))
    target = next(
        span for span in context.spans if span.context_role == "target"
    )
    source = TrustedExtractionSource.create(
        job_id="00000000-0000-4000-8000-000000000001",
        source_system="public.chat_log",
        source_external_id=context.target_evidence_id,
        source_sha256=context.target_content_sha256,
        source_recorded_at=context.source.source_recorded_at,
        content=target.content,
    )
    profile = load_runtime_profile_v2(root, "v5_2")
    registry = json.loads(
        profile.registry_path.read_text(encoding="utf-8")
    )
    transport = LlamaCppSecureTransport(
        endpoint=args.endpoint,
        enable_token="memory_v1_local_v5_inference_v1",
        api_key=api_key,
        allow_loopback_http=True,
    )
    provider = LocalLlamaCppProvider(
        model=PINNED_MODEL_ALIAS,
        model_file_sha256=PINNED_MODEL_FILE_SHA256,
        runtime_revision=PINNED_RUNTIME_REVISION,
        registry=registry,
        transport=transport,
        max_output_tokens=args.max_output_tokens,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        packet = provider.extract(
            source,
            evidence_context=context,
        )
    except LocalProviderAdapterError as exc:
        print(
            canonical_json(
                {
                    "contract_version": (
                        "memory_v1_evidence_context_local_canary_report_v1"
                    ),
                    "outcome": "rejected",
                    "error_code": exc.code,
                    "retryable": exc.retryable,
                    "context_envelope_sha256": context.envelope_sha256,
                    "external_model_calls": provider.external_model_calls,
                    "local_model_calls": provider.local_model_calls,
                    "provider_audit": provider.last_audit,
                    "write_counts": {
                        "database": 0,
                        "qdrant": 0,
                        "retrieval": 0,
                        "prompt_influence": 0,
                    },
                }
            )
        )
        return 1

    packet_value = packet.model_dump(mode="json")
    packet_path.write_text(
        canonical_json(packet_value) + "\n",
        encoding="utf-8",
    )
    packet_path.chmod(0o600)
    print(
        canonical_json(
            {
                "contract_version": (
                    "memory_v1_evidence_context_local_canary_report_v1"
                ),
                "outcome": "accepted",
                "context_envelope_sha256": context.envelope_sha256,
                "packet_sha256": canonical_sha256(packet_value),
                "entity_count": len(packet.entity_mentions),
                "observation_count": len(packet.observations),
                "deferral_count": len(packet.deferrals),
                "predicates": sorted(
                    {item.predicate for item in packet.observations}
                ),
                "projection_classes": sorted(
                    {
                        item.projection_class
                        for item in packet.observations
                    }
                ),
                "external_model_calls": provider.external_model_calls,
                "local_model_calls": provider.local_model_calls,
                "provider_audit": provider.last_audit,
                "write_counts": {
                    "database": 0,
                    "qdrant": 0,
                    "retrieval": 0,
                    "prompt_influence": 0,
                },
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    _apply_context_coreference_bindings,
    _unresolved_context_coreferences,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    TrustedExtractionSource,
    canonical_json,
    canonical_sha256,
)
from scripts.memory_v1_v5_local_inference_canary import loopback_dsn


ENABLE_TOKEN = "memory_v1_evidence_context_packet_replay_v1"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay one retained contextual packet without inference."
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--target-evidence-id", required=True)
    parser.add_argument("--expected-target-content-sha256", required=True)
    parser.add_argument("--packet-input", required=True)
    parser.add_argument("--packet-output", required=True)
    return parser.parse_args()


async def load_context(args: argparse.Namespace):
    conn = await asyncpg.connect(loopback_dsn(os.environ["POSTGRES_DSN"]))
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
    if os.getenv("MEMORY_V1_EVIDENCE_CONTEXT_REPLAY") != ENABLE_TOKEN:
        raise RuntimeError("evidence-context replay capability is absent")
    packet_input = Path(args.packet_input)
    packet_output = Path(args.packet_output)
    if not packet_input.is_file():
        raise RuntimeError("packet input is absent")
    if packet_output.exists():
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
    original = ProviderPacket.model_validate(
        json.loads(packet_input.read_text(encoding="utf-8"))
    )
    normalized, repairs = _apply_context_coreference_bindings(
        source,
        context,
        original,
    )
    unresolved = _unresolved_context_coreferences(
        source,
        context,
        normalized,
    )
    if unresolved:
        raise RuntimeError("context coreference remains unresolved")
    normalized_value = normalized.model_dump(mode="json")
    packet_output.write_text(
        canonical_json(normalized_value) + "\n",
        encoding="utf-8",
    )
    packet_output.chmod(0o600)
    print(
        canonical_json(
            {
                "contract_version": (
                    "memory_v1_evidence_context_packet_replay_report_v1"
                ),
                "context_envelope_sha256": context.envelope_sha256,
                "input_packet_sha256": canonical_sha256(
                    original.model_dump(mode="json")
                ),
                "output_packet_sha256": canonical_sha256(
                    normalized_value
                ),
                "repairs": list(repairs),
                "entity_count": len(normalized.entity_mentions),
                "observation_count": len(normalized.observations),
                "deferral_count": len(normalized.deferrals),
                "predicates": sorted(
                    {
                        observation.predicate
                        for observation in normalized.observations
                    }
                ),
                "local_model_calls": 0,
                "external_model_calls": 0,
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

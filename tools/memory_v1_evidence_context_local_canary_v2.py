#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import asyncpg

from rag_engine.memory_v1_evidence_context_loader_v2 import (
    load_memory_evidence_context_v2,
)
from scripts.memory_v1_predicate_runtime_profile_v2 import (
    load_runtime_profile_v2,
)
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LOCAL_CALL_ENABLE_TOKEN,
    LOCAL_PROVIDER_ID,
    LOCAL_PROVIDER_VERSION,
    LlamaCppSecureTransport,
    LocalLlamaCppProvider,
    LocalProviderAdapterError,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    TrustedExtractionSource,
    canonical_json,
    canonical_sha256,
    load_registry,
    load_schema,
    validate_and_normalize,
)
from scripts.memory_v1_v5_local_inference_canary import (
    PINNED_MODEL_ALIAS,
    PINNED_MODEL_FILE_SHA256,
    PINNED_RUNTIME_REVISION,
    loopback_dsn,
)


ENABLE_TOKEN = "memory_v1_evidence_context_zero_write_canary_v2"


class ContextBoundProvider:
    def __init__(self, delegate, context) -> None:
        self.delegate = delegate
        self.context = context

    @property
    def provider_id(self) -> str:
        return self.delegate.provider_id

    @property
    def provider_version(self) -> str:
        return self.delegate.provider_version

    @property
    def external_model_calls(self) -> int:
        return self.delegate.external_model_calls

    @property
    def external_call_capability(self) -> bool:
        return self.delegate.external_call_capability

    def extract(self, source: TrustedExtractionSource) -> ProviderPacket:
        return self.delegate.extract(
            source,
            evidence_context=self.context,
        )


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
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
    conn = await asyncpg.connect(loopback_dsn(os.environ["POSTGRES_DSN"]))
    try:
        return await load_memory_evidence_context_v2(
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
        raise RuntimeError("evidence-context V2 canary capability is absent")
    api_key = os.getenv("MEMORY_V1_LOCAL_INFERENCE_API_KEY")
    if not api_key:
        raise RuntimeError("private local inference credential is absent")
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
    registry = load_registry(
        profile.registry_path,
        profile.registry_artifact_sha256,
    )
    transport = LlamaCppSecureTransport(
        endpoint=args.endpoint,
        enable_token=LOCAL_CALL_ENABLE_TOKEN,
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
    schema = load_schema(
        profile.schema_path,
        profile.schema_artifact_sha256,
        expected_contract_version=profile.contract_version,
    )
    try:
        validated = validate_and_normalize(
            ContextBoundProvider(provider, context),
            source=source,
            registry=registry,
            schema=schema,
            allowed_provider_versions={
                LOCAL_PROVIDER_ID: LOCAL_PROVIDER_VERSION,
            },
            max_external_model_calls=0,
        )
    except Exception as exc:
        error_code = (
            exc.code
            if isinstance(exc, LocalProviderAdapterError)
            else "local_validation_rejected"
        )
        print(canonical_json({
            "contract_version": (
                "memory_v1_evidence_context_local_canary_report_v2"
            ),
            "outcome": "rejected",
            "error_code": error_code,
            "retryable": (
                exc.retryable
                if isinstance(exc, LocalProviderAdapterError)
                else False
            ),
            "rejection_message_sha256": canonical_sha256(str(exc)),
            "context_envelope_sha256": context.envelope_sha256,
            "prior_turn_count": len(context.prior_turns),
            "external_model_calls": provider.external_model_calls,
            "local_model_calls": provider.local_model_calls,
            "provider_audit": provider.last_audit,
            "write_counts": {
                "database": 0,
                "qdrant": 0,
                "retrieval": 0,
                "prompt_influence": 0,
            },
        }))
        return 1
    packet_value = validated.normalized_packet
    packet_path.write_text(
        canonical_json(packet_value) + "\n",
        encoding="utf-8",
    )
    packet_path.chmod(0o600)
    print(canonical_json({
        "contract_version": (
            "memory_v1_evidence_context_local_canary_report_v2"
        ),
        "outcome": "accepted",
        "context_envelope_sha256": context.envelope_sha256,
        "prior_turn_count": len(context.prior_turns),
        "packet_sha256": canonical_sha256(packet_value),
        "entity_count": len(packet_value.get("entity_mentions") or []),
        "observation_count": len(packet_value.get("observations") or []),
        "deferral_count": len(packet_value.get("deferrals") or []),
        "predicates": sorted(
            {
                str(item.get("predicate"))
                for item in packet_value.get("observations") or []
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
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

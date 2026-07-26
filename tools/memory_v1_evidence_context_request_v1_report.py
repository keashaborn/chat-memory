#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from rag_engine.memory_v1_evidence_context_v1 import (
    build_memory_evidence_context_envelope_v1,
)
from scripts.memory_v1_predicate_runtime_profile_v2 import (
    load_runtime_profile_v2,
)
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LocalLlamaCppProvider,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    canonical_sha256,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a sanitized zero-call contextual provider request."
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--expected-owner-user-id", required=True)
    parser.add_argument("--target-evidence-id", required=True)
    parser.add_argument("--expected-target-content-sha256", required=True)
    parser.add_argument("--max-spans", type=int, default=12)
    return parser.parse_args()


def load_input(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("source"), dict)
        or not isinstance(value.get("evidence"), list)
    ):
        raise RuntimeError("input must contain source object and evidence array")
    return value


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main() -> int:
    args = arguments()
    root = Path(__file__).resolve().parents[1]
    value = load_input(Path(args.input))
    envelope = build_memory_evidence_context_envelope_v1(
        expected_owner_user_id=args.expected_owner_user_id,
        target_evidence_id=args.target_evidence_id,
        expected_target_content_sha256=(
            args.expected_target_content_sha256
        ),
        source_row=value["source"],
        evidence_rows=value["evidence"],
        max_spans=args.max_spans,
    )
    target = next(
        row
        for row in value["evidence"]
        if str(row.get("evidence_id")) == args.target_evidence_id
    )
    source = TrustedExtractionSource.create(
        job_id="00000000-0000-4000-8000-000000000001",
        source_system="public.chat_log",
        source_external_id=args.target_evidence_id,
        source_sha256=args.expected_target_content_sha256,
        source_recorded_at=value["source"]["created_at"],
        content=target["content"],
    )
    profile = load_runtime_profile_v2(root, "v5_2")
    registry = json.loads(
        profile.registry_path.read_text(encoding="utf-8")
    )
    provider = LocalLlamaCppProvider(
        model="qwen3-14b-local-extractor",
        model_file_sha256="5" * 64,
        runtime_revision="llama.cpp-b10066-86a9c79f8",
        registry=registry,
        transport=object(),
    )
    request = provider.request(
        source,
        evidence_context=envelope,
    )
    report = {
        "contract_version": "memory_evidence_context_request_report_v1",
        "context_envelope_sha256": envelope.envelope_sha256,
        "provider_request_sha256": request.request_sha256,
        "instructions_sha256": sha256_text(request.instructions),
        "input_text_sha256": sha256_text(request.input_text),
        "output_schema_sha256": canonical_sha256(request.output_schema),
        "prompt_profile": request.prompt_profile,
        "target_content_sha256": source.source_sha256,
        "context_span_count": len(envelope.spans),
        "assertion_origin_count": len(
            envelope.allowed_assertion_evidence_ids
        ),
        "external_model_calls": 0,
        "local_model_calls": 0,
        "raw_text_retained": False,
        "retrieval_activation": False,
        "prompt_influence": False,
    }
    print(
        json.dumps(
            report,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

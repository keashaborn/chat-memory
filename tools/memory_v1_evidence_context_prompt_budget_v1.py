#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import urllib.request
from pathlib import Path
from typing import Any

import asyncpg

from rag_engine.memory_v1_evidence_context_loader_v1 import (
    load_memory_evidence_context_v1,
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
        description=(
            "Build and tokenize one sanitized, zero-inference contextual "
            "provider request."
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--target-evidence-id", required=True)
    parser.add_argument("--expected-target-content-sha256", required=True)
    parser.add_argument(
        "--endpoint-root",
        default="http://127.0.0.1:18080",
    )
    parser.add_argument(
        "--api-key-file",
        type=Path,
        default=Path("/etc/memory-v1-local-inference/api-key"),
    )
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    return parser.parse_args()


def request_json(
    url: str,
    *,
    api_key: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        method = "POST"
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError("private endpoint returned a non-object response")
    return value


async def load_context(args: argparse.Namespace):
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("prompt-budget probe requires brains_app")
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
    if not 1000 <= args.max_output_tokens <= 8192:
        raise RuntimeError("max output tokens must be between 1000 and 8192")
    api_key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        raise RuntimeError("private endpoint API key is empty")

    root = Path(__file__).resolve().parents[1]
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
    provider = LocalLlamaCppProvider(
        model="qwen3-14b-local-extractor",
        model_file_sha256="5" * 64,
        runtime_revision="llama.cpp-b10066-86a9c79f8",
        registry=registry,
        transport=object(),
        max_output_tokens=args.max_output_tokens,
    )
    structured = provider.request(
        source,
        evidence_context=context,
    )
    endpoint_root = args.endpoint_root.rstrip("/")
    props = request_json(
        f"{endpoint_root}/props",
        api_key=api_key,
    )
    context_tokens = int(
        props["default_generation_settings"]["n_ctx"]
    )
    templated = request_json(
        f"{endpoint_root}/apply-template",
        api_key=api_key,
        payload={
            "messages": structured.body()["messages"],
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )
    prompt = templated.get("prompt")
    if not isinstance(prompt, str):
        raise RuntimeError("private template endpoint omitted prompt")
    tokenized = request_json(
        f"{endpoint_root}/tokenize",
        api_key=api_key,
        payload={"content": prompt, "add_special": False},
    )
    tokens = tokenized.get("tokens")
    if not isinstance(tokens, list):
        raise RuntimeError("private tokenize endpoint omitted tokens")
    prompt_tokens = len(tokens)
    remaining_context_tokens = context_tokens - prompt_tokens
    passed = remaining_context_tokens >= args.max_output_tokens
    report = {
        "contract_version": (
            "memory_v1_evidence_context_prompt_budget_report_v1"
        ),
        "passed": passed,
        "prompt_profile": structured.prompt_profile,
        "prompt_tokens": prompt_tokens,
        "context_tokens": context_tokens,
        "remaining_context_tokens": remaining_context_tokens,
        "max_output_tokens": structured.max_output_tokens,
        "context_span_count": len(context.spans),
        "assertion_origin_count": len(
            context.allowed_assertion_evidence_ids
        ),
        "context_envelope_sha256": context.envelope_sha256,
        "provider_request_sha256": structured.request_sha256,
        "instructions_sha256": hashlib.sha256(
            structured.instructions.encode("utf-8")
        ).hexdigest(),
        "input_text_sha256": hashlib.sha256(
            structured.input_text.encode("utf-8")
        ).hexdigest(),
        "output_schema_sha256": canonical_sha256(
            structured.output_schema
        ),
        "external_model_calls": 0,
        "local_model_calls": 0,
        "raw_text_retained": False,
        "write_counts": {
            "database": 0,
            "qdrant": 0,
            "retrieval": 0,
            "prompt_influence": 0,
        },
    }
    print(
        json.dumps(
            report,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

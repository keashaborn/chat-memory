#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any

from scripts.memory_v1_predicate_runtime_profile_v2 import load_runtime_profile_v2
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LocalLlamaCppProvider,
    SEMANTIC_V5_2_POLICY_COMPILER_VERSION,
)
from scripts.memory_v1_relational_extraction_v5_provider import TrustedExtractionSource


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PROFILE = "semantic_stance_compact_v1"
MAX_PROMPT_TOKENS = 4096
MIN_REMAINING_CONTEXT = 4096


def post_json(url: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def get_json(url: str, api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint-root", default="http://127.0.0.1:18080")
    parser.add_argument(
        "--api-key-file",
        type=Path,
        default=Path("/etc/memory-v1-local-inference/api-key"),
    )
    args = parser.parse_args()
    api_key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        raise RuntimeError("private endpoint API key is empty")

    profile = load_runtime_profile_v2(ROOT, "v5_2")
    registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
    source_text = (
        "I believe that evidence should be separated from popular opinion. "
        "My view is that claims should remain revisable when contrary evidence "
        "appears. This is a reported philosophical position, not a diagnosis. "
    ) * 6
    source = TrustedExtractionSource.create(
        job_id="00000000-0000-4000-8000-000000000201",
        source_system="public.chat_log",
        source_external_id="00000000-0000-4000-8000-000000000202",
        source_sha256=hashlib.sha256(source_text.encode()).hexdigest(),
        source_recorded_at="2026-07-22T00:00:00+00:00",
        content=source_text,
    )
    provider = LocalLlamaCppProvider(
        model="qwen3-14b-local-extractor",
        model_file_sha256="5" * 64,
        runtime_revision="llama.cpp-b10066-86a9c79f8",
        registry=registry,
        transport=object(),
        max_output_tokens=4096,
    )
    structured = provider.request(source)
    if structured.prompt_profile != EXPECTED_PROFILE:
        raise RuntimeError("compact prompt route was not selected")
    if provider._policy_compiler_version != SEMANTIC_V5_2_POLICY_COMPILER_VERSION:
        raise RuntimeError("semantic compiler version changed")

    root = args.endpoint_root.rstrip("/")
    props = get_json(f"{root}/props", api_key)
    context_tokens = int(props["default_generation_settings"]["n_ctx"])
    templated = post_json(
        f"{root}/apply-template",
        {
            "messages": structured.body()["messages"],
            "chat_template_kwargs": {"enable_thinking": False},
        },
        api_key,
    )
    prompt = templated["prompt"]
    tokenized = post_json(
        f"{root}/tokenize",
        {"content": prompt, "add_special": False},
        api_key,
    )
    prompt_tokens = len(tokenized["tokens"])
    remaining = context_tokens - prompt_tokens
    if prompt_tokens > MAX_PROMPT_TOKENS:
        raise RuntimeError("compact prompt exceeds prompt-token ceiling")
    if remaining < MIN_REMAINING_CONTEXT:
        raise RuntimeError("compact prompt leaves insufficient context")

    print(
        json.dumps(
            {
                "contract_version": "memory_v1_v5_2_compact_prompt_budget_v1",
                "passed": True,
                "policy_compiler_version": SEMANTIC_V5_2_POLICY_COMPILER_VERSION,
                "prompt_profile": structured.prompt_profile,
                "prompt_tokens": prompt_tokens,
                "context_tokens": context_tokens,
                "remaining_context_tokens": remaining,
                "max_output_tokens": structured.max_output_tokens,
                "request_sha256": structured.request_sha256,
                "source_sha256": source.source_sha256,
                "model_calls": 0,
                "external_calls": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

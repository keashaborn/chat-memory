#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


VERSION = "memory_v1_v5_local_inference_health_v1"


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the private local inference model without inference."
    )
    parser.add_argument(
        "--endpoint", default="http://127.0.0.1:18080/v1/models"
    )
    parser.add_argument("--credential-name", default="local_api_key")
    parser.add_argument("--expected-model", default="qwen3-14b-local-extractor")
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    return parser.parse_args()


def loopback_endpoint(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http":
        raise RuntimeError("health endpoint must use loopback HTTP")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("health endpoint must be loopback-only")
    if parsed.path != "/v1/models" or parsed.params or parsed.query or parsed.fragment:
        raise RuntimeError("health endpoint path must be /v1/models")
    return value


def load_credential(name: str) -> str:
    directory = os.getenv("CREDENTIALS_DIRECTORY")
    if not directory:
        raise RuntimeError("systemd credential directory is unavailable")
    if not name or "/" in name or "\\" in name:
        raise RuntimeError("credential name is invalid")
    path = Path(directory) / name
    key = path.read_text(encoding="utf-8").strip()
    if not 32 <= len(key) <= 500 or any(character.isspace() for character in key):
        raise RuntimeError("local inference credential is invalid")
    return key


def fetch_models(endpoint: str, api_key: str, timeout_seconds: float) -> list[str]:
    request = urllib.request.Request(
        endpoint,
        headers={
            "authorization": f"Bearer {api_key}",
            "accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        if response.status != 200:
            raise RuntimeError("local inference health returned non-200 status")
        payload = json.load(response)
    values = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(values, list) or len(values) > 100:
        raise RuntimeError("local inference model inventory is invalid")
    models: list[str] = []
    for value in values:
        model_id = value.get("id") if isinstance(value, dict) else None
        if not isinstance(model_id, str) or not model_id or len(model_id) > 200:
            raise RuntimeError("local inference model id is invalid")
        models.append(model_id)
    return sorted(set(models))


def main() -> int:
    args = arguments()
    endpoint = loopback_endpoint(args.endpoint)
    if not 1.0 <= args.timeout_seconds <= 30.0:
        raise RuntimeError("timeout must be between 1 and 30 seconds")
    if not args.expected_model or len(args.expected_model) > 200:
        raise RuntimeError("expected model id is invalid")
    models = fetch_models(
        endpoint,
        load_credential(args.credential_name),
        args.timeout_seconds,
    )
    if args.expected_model not in models:
        raise RuntimeError("expected local inference model is absent")
    print(
        stable_json(
            {
                "version": VERSION,
                "status": "healthy",
                "model_count": len(models),
                "expected_model_sha256": sha256_text(args.expected_model),
                "model_inventory_sha256": sha256_text(stable_json(models)),
                "local_model_calls": 0,
                "external_model_calls": 0,
                "database_writes": 0,
                "qdrant_writes": 0,
                "prompt_influence": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

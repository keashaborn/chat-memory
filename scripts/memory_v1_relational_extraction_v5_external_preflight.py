#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from scripts.memory_v1_relational_extraction_v5_openai_provider import (
    EXTERNAL_CALL_ENABLE_TOKEN,
    OPENAI_PROVIDER_ID,
    OPENAI_PROVIDER_VERSION,
    OpenAIResponsesProvider,
    ResponsesResult,
    StaticResponsesTransport,
    sdk_capability_report,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    TrustedExtractionSource,
    canonical_json,
    canonical_sha256,
    load_registry,
    load_schema,
)


MANIFEST_CONTRACT = (
    "memory_v1_relational_extraction_v5_external_preflight_manifest_v1"
)
REPORT_CONTRACT = (
    "memory_v1_relational_extraction_v5_external_preflight_report_v1"
)
SCOPE = "single_synthetic_source_preflight_only_zero_call"
SHA256_HEX = frozenset("0123456789abcdef")
EXPECTED_ARTIFACT_PATHS = {
    "requirements-ci.txt",
    "scripts/memory_v1_relational_extraction_v5_openai_provider.py",
    "scripts/memory_v1_relational_extraction_v5_provider.py",
    "specs/memory_v1_predicate_registry_v5.json",
    "specs/memory_v1_relational_extraction_v5.schema.json",
}
EXPECTED_ARTIFACT_KINDS = {
    "requirements-ci.txt": "dependency_lock",
    "scripts/memory_v1_relational_extraction_v5_openai_provider.py": (
        "external_provider_adapter"
    ),
    "scripts/memory_v1_relational_extraction_v5_provider.py": (
        "strict_validator"
    ),
    "specs/memory_v1_predicate_registry_v5.json": "predicate_registry",
    "specs/memory_v1_relational_extraction_v5.schema.json": (
        "normalized_schema"
    ),
}
EXPECTED_ZERO_EFFECTS = {
    "external_model_calls": 0,
    "database_writes": 0,
    "qdrant_writes": 0,
    "redis_writes": 0,
    "candidate_writes": 0,
    "claim_writes": 0,
    "staging_writes": 0,
    "prompt_influence_changes": 0,
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Hash-locked, zero-call preflight for one synthetic V5 "
            "Responses API extraction."
        )
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    return parser.parse_args()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in SHA256_HEX for character in value)
    )


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise RuntimeError(f"{label} keys do not match the contract")
    return value


def load_manifest(
    path: Path,
    expected_sha256: str,
) -> tuple[dict[str, Any], str]:
    if not _is_sha256(expected_sha256):
        raise RuntimeError("expected manifest SHA-256 is invalid")
    raw = path.read_bytes()
    actual_sha256 = _sha256_bytes(raw)
    if actual_sha256 != expected_sha256:
        raise RuntimeError("manifest file SHA-256 mismatch")
    manifest = _exact_keys(
        json.loads(raw),
        {
            "contract_version",
            "manifest_id",
            "created_on",
            "scope",
            "external_call_authorized",
            "required_ancestor_commit",
            "provider",
            "source",
            "expected",
            "artifacts",
        },
        "manifest",
    )
    if manifest["contract_version"] != MANIFEST_CONTRACT:
        raise RuntimeError("manifest contract version mismatch")
    if manifest["scope"] != SCOPE:
        raise RuntimeError("manifest is not preflight-only")
    if manifest["external_call_authorized"] is not False:
        raise RuntimeError("preflight manifest must not authorize an external call")
    if manifest["manifest_id"] != (
        "memory-v1-v5-synthetic-openai-preflight-20260716"
    ):
        raise RuntimeError("manifest ID mismatch")
    if manifest["created_on"] != "2026-07-16":
        raise RuntimeError("manifest creation date mismatch")
    ancestor = manifest["required_ancestor_commit"]
    if (
        not isinstance(ancestor, str)
        or len(ancestor) != 40
        or any(character not in SHA256_HEX for character in ancestor)
    ):
        raise RuntimeError("required ancestor commit is invalid")
    return manifest, actual_sha256


def _artifact_map(
    repo_root: Path,
    artifacts: Any,
) -> dict[str, str]:
    if not isinstance(artifacts, list) or len(artifacts) != len(
        EXPECTED_ARTIFACT_PATHS
    ):
        raise RuntimeError("artifact list count mismatch")
    output: dict[str, str] = {}
    for index, item_value in enumerate(artifacts):
        item = _exact_keys(
            item_value,
            {"path", "sha256", "kind"},
            f"artifacts[{index}]",
        )
        path_value = item["path"]
        if (
            not isinstance(path_value, str)
            or not path_value
            or Path(path_value).is_absolute()
        ):
            raise RuntimeError("artifact path is invalid")
        if path_value in output:
            raise RuntimeError("artifact path is duplicated")
        if item["kind"] != EXPECTED_ARTIFACT_KINDS.get(path_value):
            raise RuntimeError(f"artifact kind mismatch: {path_value}")
        expected_sha256 = item["sha256"]
        if not _is_sha256(expected_sha256):
            raise RuntimeError("artifact SHA-256 is invalid")
        resolved = (repo_root / path_value).resolve()
        try:
            resolved.relative_to(repo_root)
        except ValueError as exc:
            raise RuntimeError("artifact escapes the repository") from exc
        if not resolved.is_file():
            raise RuntimeError(f"artifact is missing: {path_value}")
        if _sha256_bytes(resolved.read_bytes()) != expected_sha256:
            raise RuntimeError(f"artifact SHA-256 mismatch: {path_value}")
        output[path_value] = expected_sha256
    if set(output) != EXPECTED_ARTIFACT_PATHS:
        raise RuntimeError("artifact path set mismatch")
    return output


def _require_descendant(repo_root: Path, ancestor: str) -> str:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, head],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("repository HEAD is not descended from required commit")
    return head


def preflight(
    *,
    repo_root: Path,
    manifest: dict[str, Any],
    manifest_sha256: str,
) -> dict[str, Any]:
    artifacts = _artifact_map(repo_root, manifest["artifacts"])
    requirement_lines = {
        line.strip()
        for line in (repo_root / "requirements-ci.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if "openai==2.6.1" not in requirement_lines:
        raise RuntimeError("CI OpenAI SDK pin mismatch")
    if "pydantic==2.12.3" not in requirement_lines:
        raise RuntimeError("CI Pydantic pin mismatch")
    head = _require_descendant(
        repo_root,
        manifest["required_ancestor_commit"],
    )

    provider_value = _exact_keys(
        manifest["provider"],
        {
            "provider_id",
            "provider_version",
            "model",
            "store",
            "max_external_model_calls",
            "max_output_tokens",
            "timeout_seconds",
            "expected_openai_version",
            "expected_pydantic_version",
            "enable_token_sha256",
        },
        "provider",
    )
    if provider_value["provider_id"] != OPENAI_PROVIDER_ID:
        raise RuntimeError("provider ID mismatch")
    if provider_value["provider_version"] != OPENAI_PROVIDER_VERSION:
        raise RuntimeError("provider version mismatch")
    if provider_value["model"] != "gpt-5.6-terra":
        raise RuntimeError("synthetic smoke-test model mismatch")
    if provider_value["store"] is not False:
        raise RuntimeError("provider storage must be disabled")
    if provider_value["max_external_model_calls"] != 1:
        raise RuntimeError("smoke-test call budget must be exactly one")
    if provider_value["enable_token_sha256"] != _sha256_text(
        EXTERNAL_CALL_ENABLE_TOKEN
    ):
        raise RuntimeError("external enable token binding mismatch")

    expected = _exact_keys(
        manifest["expected"],
        {
            *EXPECTED_ZERO_EFFECTS,
            "required_predicates",
            "forbidden_packet_keys",
        },
        "expected",
    )
    for key, value in EXPECTED_ZERO_EFFECTS.items():
        if expected[key] != value:
            raise RuntimeError(f"preflight expected effect is not zero: {key}")
    if expected["required_predicates"] != ["identity.name"]:
        raise RuntimeError("synthetic expected predicate set changed")
    if expected["forbidden_packet_keys"] != [
        "owner_user_id",
        "vantage_id",
    ]:
        raise RuntimeError("forbidden packet key policy changed")

    source_value = _exact_keys(
        manifest["source"],
        {
            "synthetic",
            "job_id",
            "source_system",
            "source_external_id",
            "source_recorded_at",
            "content",
            "content_sha256",
        },
        "source",
    )
    if source_value["synthetic"] is not True:
        raise RuntimeError("external preflight source must be synthetic")
    if source_value["content"] != "My name is Avery.":
        raise RuntimeError("synthetic source content mismatch")
    source = TrustedExtractionSource.create(
        job_id=source_value["job_id"],
        source_system=source_value["source_system"],
        source_external_id=source_value["source_external_id"],
        source_sha256=source_value["content_sha256"],
        source_recorded_at=source_value["source_recorded_at"],
        content=source_value["content"],
    )
    uuid.UUID(source.job_id)
    uuid.UUID(source.source_external_id)

    registry_path = (
        repo_root / "specs/memory_v1_predicate_registry_v5.json"
    )
    schema_path = (
        repo_root / "specs/memory_v1_relational_extraction_v5.schema.json"
    )
    registry = load_registry(
        registry_path,
        artifacts[
            "specs/memory_v1_predicate_registry_v5.json"
        ],
    )
    load_schema(
        schema_path,
        artifacts[
            "specs/memory_v1_relational_extraction_v5.schema.json"
        ],
    )

    capability = sdk_capability_report()
    if capability["supported"] is not True:
        raise RuntimeError("installed OpenAI SDK lacks Responses.parse support")
    if capability["openai_version"] != provider_value["expected_openai_version"]:
        raise RuntimeError("installed OpenAI SDK version mismatch")
    if capability["pydantic_version"] != provider_value[
        "expected_pydantic_version"
    ]:
        raise RuntimeError("installed Pydantic version mismatch")

    static_transport = StaticResponsesTransport(
        result=ResponsesResult(
            response_id=None,
            status="completed",
            parsed=ProviderPacket(
                entity_mentions=[],
                observations=[],
                comparison_hints=[],
                deferrals=[],
                packet_findings=[],
            ),
            refusal=False,
            incomplete_reason=None,
        )
    )
    provider = OpenAIResponsesProvider(
        model=provider_value["model"],
        registry=registry,
        transport=static_transport,
        max_output_tokens=provider_value["max_output_tokens"],
        timeout_seconds=provider_value["timeout_seconds"],
    )
    request = provider.request(source)
    if request.store is not False:
        raise RuntimeError("constructed Responses request is stateful")
    if request.output_schema_sha256 != capability["output_schema_sha256"]:
        raise RuntimeError("constructed output schema fingerprint mismatch")
    if static_transport.requests or static_transport.external_model_calls != 0:
        raise RuntimeError("preflight unexpectedly invoked the static transport")
    serialized_request = canonical_json(
        {
            "model": request.model,
            "instructions": request.instructions,
            "input_text": request.input_text,
            "store": request.store,
            "max_output_tokens": request.max_output_tokens,
            "timeout_seconds": request.timeout_seconds,
            "metadata": request.metadata,
            "output_schema_sha256": request.output_schema_sha256,
        }
    )
    forbidden_values = {
        source.job_id,
        source.source_external_id,
        source.source_sha256,
        "owner_user_id",
        "vantage_id",
    }
    if any(value in serialized_request for value in forbidden_values):
        raise RuntimeError("constructed request contains a forbidden identifier")

    return {
        "contract_version": REPORT_CONTRACT,
        "manifest_id": manifest["manifest_id"],
        "manifest_sha256": manifest_sha256,
        "repository_head": head,
        "scope": SCOPE,
        "external_call_authorized": False,
        "ready_for_authorization": True,
        "provider": {
            "provider_id": provider.provider_id,
            "provider_version": provider.provider_version,
            "model": request.model,
            "store": request.store,
            "max_external_model_calls": 1,
            "output_schema_sha256": request.output_schema_sha256,
            "request_sha256": canonical_sha256(
                {
                    "model": request.model,
                    "instructions_sha256": _sha256_text(
                        request.instructions
                    ),
                    "input_sha256": _sha256_text(request.input_text),
                    "store": request.store,
                    "max_output_tokens": request.max_output_tokens,
                    "timeout_seconds": request.timeout_seconds,
                    "metadata": request.metadata,
                    "output_schema_sha256": request.output_schema_sha256,
                }
            ),
        },
        "sdk": capability,
        "artifact_sha256": artifacts,
        "effects": dict(EXPECTED_ZERO_EFFECTS),
    }


def main() -> int:
    args = arguments()
    repo_root = Path(__file__).resolve().parents[1]
    manifest, manifest_sha256 = load_manifest(
        Path(args.manifest).resolve(),
        args.expected_manifest_sha256,
    )
    report = preflight(
        repo_root=repo_root,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
    )
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

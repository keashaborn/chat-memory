#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from openai import OpenAI

from scripts.memory_v1_relational_extraction_v5_external_preflight import (
    load_manifest as load_preflight_manifest,
    preflight as run_zero_call_preflight,
)
from scripts.memory_v1_relational_extraction_v5_openai_provider import (
    EXTERNAL_CALL_ENABLE_TOKEN,
    OPENAI_PROVIDER_ID,
    OPENAI_PROVIDER_VERSION,
    OpenAIResponsesProvider,
    OpenAIResponsesTransport,
    ProviderAdapterError,
    ResponsesTransport,
    sdk_capability_report,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    canonical_json,
    file_sha256,
    load_registry,
    load_schema,
    sha256_text,
    validate_and_normalize,
)


MANIFEST_CONTRACT = (
    "memory_v1_relational_extraction_v5_external_smoke_manifest_v1"
)
REPORT_CONTRACT = (
    "memory_v1_relational_extraction_v5_external_smoke_report_v1"
)
SCOPE = "single_synthetic_source_store_false_one_call"
MANIFEST_ID = "memory-v1-v5-synthetic-openai-smoke-20260716"
AUDIT_RELATIVE_PATH = (
    "ops/audits/"
    "memory_v1_relational_extraction_v5_external_smoke_20260716.json"
)
AUTHORIZATION_STATEMENT = (
    "I authorize one OpenAI API call using only the synthetic text "
    "\u201cMy name is Avery.\u201d, with store=false, maximum one call, and "
    "zero database, Qdrant, Redis, staging, or prompt-influence writes. "
    "Stop after validating and auditing the response."
)
SHA256_HEX = frozenset("0123456789abcdef")
ZERO_PROHIBITED_EFFECTS = {
    "database_writes": 0,
    "qdrant_writes": 0,
    "redis_writes": 0,
    "candidate_writes": 0,
    "claim_writes": 0,
    "staging_writes": 0,
    "prompt_influence_changes": 0,
}
ARTIFACT_KINDS = {
    "requirements-ci.txt": "dependency_lock",
    "scripts/memory_v1_relational_extraction_v5_openai_provider.py": (
        "external_provider_adapter"
    ),
    "scripts/memory_v1_relational_extraction_v5_provider.py": (
        "strict_validator"
    ),
    "scripts/memory_v1_relational_extraction_v5_external_smoke.py": (
        "one_call_runner"
    ),
    "specs/memory_v1_predicate_registry_v5.json": "predicate_registry",
    "specs/memory_v1_relational_extraction_v5.schema.json": (
        "normalized_schema"
    ),
    "ops/manifests/"
    "memory_v1_relational_extraction_v5_external_preflight_20260716.json": (
        "zero_call_preflight_manifest"
    ),
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Hash-locked, one-call, store=false synthetic V5 extraction "
            "smoke test. The OpenAI SDK is configured with zero retries."
        )
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


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


def _require_clean_repository(repo_root: Path) -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise RuntimeError("one-call smoke test requires a clean worktree")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if len(head) != 40 or any(character not in SHA256_HEX for character in head):
        raise RuntimeError("repository HEAD is invalid")
    return head


def load_smoke_manifest(
    path: Path,
    expected_sha256: str,
) -> tuple[dict[str, Any], str]:
    if not _is_sha256(expected_sha256):
        raise RuntimeError("expected smoke manifest SHA-256 is invalid")
    raw = path.read_bytes()
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise RuntimeError("smoke manifest file SHA-256 mismatch")
    manifest = _exact_keys(
        json.loads(raw),
        {
            "contract_version",
            "manifest_id",
            "created_on",
            "scope",
            "external_call_authorized",
            "authorization",
            "required_ancestor_commit",
            "preflight",
            "provider",
            "source",
            "expected",
            "audit_output",
            "artifacts",
        },
        "manifest",
    )
    if manifest["contract_version"] != MANIFEST_CONTRACT:
        raise RuntimeError("smoke manifest contract version mismatch")
    if manifest["manifest_id"] != MANIFEST_ID:
        raise RuntimeError("smoke manifest ID mismatch")
    if manifest["created_on"] != "2026-07-16":
        raise RuntimeError("smoke manifest creation date mismatch")
    if manifest["scope"] != SCOPE:
        raise RuntimeError("smoke manifest scope mismatch")
    if manifest["external_call_authorized"] is not True:
        raise RuntimeError("smoke manifest does not authorize the external call")
    if manifest["audit_output"] != AUDIT_RELATIVE_PATH:
        raise RuntimeError("smoke audit output path mismatch")

    authorization = _exact_keys(
        manifest["authorization"],
        {
            "statement_sha256",
            "authorized_external_model_calls",
            "store",
            "synthetic_source_only",
            "implicit_http_retries",
        },
        "authorization",
    )
    if authorization["statement_sha256"] != sha256_text(
        AUTHORIZATION_STATEMENT
    ):
        raise RuntimeError("user authorization statement hash mismatch")
    if authorization["authorized_external_model_calls"] != 1:
        raise RuntimeError("authorization call budget must be exactly one")
    if authorization["store"] is not False:
        raise RuntimeError("authorization requires store=false")
    if authorization["synthetic_source_only"] is not True:
        raise RuntimeError("authorization is not synthetic-source-only")
    if authorization["implicit_http_retries"] != 0:
        raise RuntimeError("authorization forbids implicit HTTP retries")

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
    if not isinstance(artifacts, list) or len(artifacts) != len(ARTIFACT_KINDS):
        raise RuntimeError("smoke artifact count mismatch")
    output: dict[str, str] = {}
    for index, value in enumerate(artifacts):
        item = _exact_keys(
            value,
            {"path", "sha256", "kind"},
            f"artifacts[{index}]",
        )
        relative = item["path"]
        if not isinstance(relative, str) or relative not in ARTIFACT_KINDS:
            raise RuntimeError("smoke artifact path is invalid")
        if relative in output:
            raise RuntimeError("smoke artifact path is duplicated")
        if item["kind"] != ARTIFACT_KINDS[relative]:
            raise RuntimeError(f"smoke artifact kind mismatch: {relative}")
        expected_sha256 = item["sha256"]
        if not _is_sha256(expected_sha256):
            raise RuntimeError("smoke artifact SHA-256 is invalid")
        resolved = (repo_root / relative).resolve()
        try:
            resolved.relative_to(repo_root)
        except ValueError as exc:
            raise RuntimeError("smoke artifact escapes repository") from exc
        if not resolved.is_file() or file_sha256(resolved) != expected_sha256:
            raise RuntimeError(f"smoke artifact SHA-256 mismatch: {relative}")
        output[relative] = expected_sha256
    if set(output) != set(ARTIFACT_KINDS):
        raise RuntimeError("smoke artifact path set mismatch")
    return output


def prepare_smoke(
    *,
    repo_root: Path,
    manifest: dict[str, Any],
    manifest_sha256: str,
    enforce_clean_repository: bool = True,
) -> dict[str, Any]:
    artifacts = _artifact_map(repo_root, manifest["artifacts"])
    head = (
        _require_clean_repository(repo_root)
        if enforce_clean_repository
        else subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    ancestor = manifest["required_ancestor_commit"]
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, head],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    ).returncode != 0:
        raise RuntimeError("smoke runner is not descended from authorized commit")

    preflight_value = _exact_keys(
        manifest["preflight"],
        {"path", "manifest_sha256", "request_sha256"},
        "preflight",
    )
    if preflight_value["path"] != (
        "ops/manifests/"
        "memory_v1_relational_extraction_v5_external_preflight_20260716.json"
    ):
        raise RuntimeError("zero-call preflight path mismatch")
    if not _is_sha256(preflight_value["manifest_sha256"]):
        raise RuntimeError("zero-call preflight manifest hash is invalid")
    if not _is_sha256(preflight_value["request_sha256"]):
        raise RuntimeError("zero-call preflight request hash is invalid")
    preflight_manifest, preflight_sha256 = load_preflight_manifest(
        repo_root / preflight_value["path"],
        preflight_value["manifest_sha256"],
    )
    zero_call_report = run_zero_call_preflight(
        repo_root=repo_root,
        manifest=preflight_manifest,
        manifest_sha256=preflight_sha256,
    )
    if zero_call_report["ready_for_authorization"] is not True:
        raise RuntimeError("zero-call preflight is not ready")
    if zero_call_report["effects"]["external_model_calls"] != 0:
        raise RuntimeError("zero-call preflight invoked an external model")
    if (
        zero_call_report["provider"]["request_sha256"]
        != preflight_value["request_sha256"]
    ):
        raise RuntimeError("authorized request differs from zero-call preflight")

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
            "implicit_http_retries",
            "base_url",
        },
        "provider",
    )
    if provider_value != {
        "provider_id": OPENAI_PROVIDER_ID,
        "provider_version": OPENAI_PROVIDER_VERSION,
        "model": "gpt-5.6-terra",
        "store": False,
        "max_external_model_calls": 1,
        "max_output_tokens": 4000,
        "timeout_seconds": 120.0,
        "expected_openai_version": "2.6.1",
        "expected_pydantic_version": "2.12.3",
        "enable_token_sha256": sha256_text(EXTERNAL_CALL_ENABLE_TOKEN),
        "implicit_http_retries": 0,
        "base_url": "https://api.openai.com/v1",
    }:
        raise RuntimeError("smoke provider configuration mismatch")
    capability = sdk_capability_report()
    if capability["supported"] is not True:
        raise RuntimeError("OpenAI Responses.parse capability is unavailable")
    if capability["openai_version"] != provider_value["expected_openai_version"]:
        raise RuntimeError("installed OpenAI SDK version mismatch")
    if capability["pydantic_version"] != provider_value[
        "expected_pydantic_version"
    ]:
        raise RuntimeError("installed Pydantic version mismatch")

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
        raise RuntimeError("smoke source must be synthetic")
    if source_value["content"] != "My name is Avery.":
        raise RuntimeError("smoke source content mismatch")
    source = TrustedExtractionSource.create(
        job_id=source_value["job_id"],
        source_system=source_value["source_system"],
        source_external_id=source_value["source_external_id"],
        source_sha256=source_value["content_sha256"],
        source_recorded_at=source_value["source_recorded_at"],
        content=source_value["content"],
    )

    expected = _exact_keys(
        manifest["expected"],
        {
            "external_model_calls",
            *ZERO_PROHIBITED_EFFECTS,
            "required_predicates",
            "required_literal_value",
            "manual_review_required",
        },
        "expected",
    )
    if expected["external_model_calls"] != 1:
        raise RuntimeError("smoke expected call count must be one")
    for key, value in ZERO_PROHIBITED_EFFECTS.items():
        if expected[key] != value:
            raise RuntimeError(f"smoke expected prohibited effect changed: {key}")
    if expected["required_predicates"] != ["identity.name"]:
        raise RuntimeError("smoke expected predicate changed")
    if expected["required_literal_value"] != "Avery":
        raise RuntimeError("smoke expected literal changed")
    if expected["manual_review_required"] is not False:
        raise RuntimeError("smoke expected review disposition changed")

    registry = load_registry(
        repo_root / "specs/memory_v1_predicate_registry_v5.json",
        artifacts["specs/memory_v1_predicate_registry_v5.json"],
    )
    schema = load_schema(
        repo_root / "specs/memory_v1_relational_extraction_v5.schema.json",
        artifacts["specs/memory_v1_relational_extraction_v5.schema.json"],
    )
    return {
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "repository_head": head,
        "artifacts": artifacts,
        "source": source,
        "registry": registry,
        "schema": schema,
        "provider": provider_value,
        "expected": expected,
        "preflight_report": zero_call_report,
        "sdk": capability,
    }


def _sanitized_provider_audit(value: dict[str, Any] | None) -> dict[str, Any]:
    audit = dict(value or {})
    response_id = audit.pop("response_id", None)
    audit["response_id_sha256"] = (
        sha256_text(str(response_id)) if response_id else None
    )
    return audit


def _semantic_findings(
    normalized: dict[str, Any],
    *,
    expected: dict[str, Any],
) -> list[str]:
    findings: list[str] = []
    mentions = normalized.get("entity_mentions", [])
    observations = normalized.get("observations", [])
    if len(mentions) != 1 or mentions[0].get("entity_type") != "self":
        findings.append("expected_exactly_one_self_entity")
    if len(observations) != 1:
        findings.append("expected_exactly_one_observation")
        return findings
    observation = observations[0]
    if [observation.get("predicate")] != expected["required_predicates"]:
        findings.append("identity_name_predicate_missing")
    object_value = observation.get("object", {})
    if object_value.get("kind") != "literal":
        findings.append("identity_name_object_not_literal")
    if object_value.get("value") != expected["required_literal_value"]:
        findings.append("identity_name_literal_mismatch")
    if observation.get("polarity") != "affirmed":
        findings.append("identity_name_not_affirmed")
    if observation.get("modality") != "asserted":
        findings.append("identity_name_not_asserted")
    if normalized.get("comparison_hints"):
        findings.append("unexpected_comparison_hint")
    if normalized.get("deferrals"):
        findings.append("unexpected_deferral")
    return findings


def execute_once(
    *,
    prepared: dict[str, Any],
    transport: ResponsesTransport,
) -> dict[str, Any]:
    provider_value = prepared["provider"]
    provider = OpenAIResponsesProvider(
        model=provider_value["model"],
        registry=prepared["registry"],
        transport=transport,
        max_output_tokens=provider_value["max_output_tokens"],
        timeout_seconds=provider_value["timeout_seconds"],
    )
    result = None
    error: dict[str, Any] | None = None
    findings: list[str] = []
    try:
        result = validate_and_normalize(
            provider,
            source=prepared["source"],
            registry=prepared["registry"],
            schema=prepared["schema"],
            allowed_provider_versions={
                OPENAI_PROVIDER_ID: OPENAI_PROVIDER_VERSION,
            },
            max_external_model_calls=1,
        )
        findings = _semantic_findings(
            result.normalized_packet,
            expected=prepared["expected"],
        )
        if result.manual_review_required is not prepared["expected"][
            "manual_review_required"
        ]:
            findings.append("manual_review_disposition_mismatch")
    except Exception as exc:
        error = {
            "class": type(exc).__name__,
            "code": (
                exc.code
                if isinstance(exc, ProviderAdapterError)
                else "validation_or_execution_error"
            ),
            "http_status": (
                exc.http_status if isinstance(exc, ProviderAdapterError) else None
            ),
            "retry_attempted": False,
        }

    calls = int(provider.external_model_calls)
    if calls != 1:
        findings.append("external_model_call_count_mismatch")
    passed = result is not None and error is None and not findings and calls == 1
    return {
        "contract_version": REPORT_CONTRACT,
        "manifest_id": prepared["manifest"]["manifest_id"],
        "manifest_sha256": prepared["manifest_sha256"],
        "repository_head": prepared["repository_head"],
        "scope": SCOPE,
        "passed": passed,
        "authorization": {
            "statement_sha256": prepared["manifest"]["authorization"][
                "statement_sha256"
            ],
            "external_call_authorized": True,
            "authorized_external_model_calls": 1,
        },
        "provider": {
            "provider_id": OPENAI_PROVIDER_ID,
            "provider_version": OPENAI_PROVIDER_VERSION,
            "model": provider_value["model"],
            "store": False,
            "implicit_http_retries": 0,
            "request_sha256": prepared["preflight_report"]["provider"][
                "request_sha256"
            ],
            "output_schema_sha256": prepared["preflight_report"]["provider"][
                "output_schema_sha256"
            ],
            "audit": _sanitized_provider_audit(provider.last_audit),
        },
        "sdk": prepared["sdk"],
        "external_model_calls": calls,
        "error": error,
        "findings": sorted(set(findings)),
        "validation": (
            {
                "provider_output_sha256": result.provider_output_sha256,
                "normalized_packet_sha256": result.normalized_packet_sha256,
                "manual_review_required": result.manual_review_required,
                "normalized_packet": result.normalized_packet,
            }
            if result is not None
            else None
        ),
        "effects": {
            "external_model_calls": calls,
            **ZERO_PROHIBITED_EFFECTS,
            "filesystem_audit_writes": 1,
        },
        "artifact_sha256": prepared["artifacts"],
    }


def secure_write_audit(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (canonical_json(report) + "\n").encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise


def main() -> int:
    args = arguments()
    repo_root = Path(__file__).resolve().parents[1]
    manifest, manifest_sha256 = load_smoke_manifest(
        Path(args.manifest).resolve(),
        args.expected_manifest_sha256,
    )
    output_path = Path(args.output).resolve()
    expected_output = (repo_root / AUDIT_RELATIVE_PATH).resolve()
    if output_path != expected_output:
        raise RuntimeError("smoke audit output argument is not manifest-bound")
    if output_path.exists():
        raise RuntimeError("smoke audit already exists; replay is forbidden")
    prepared = prepare_smoke(
        repo_root=repo_root,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
    )
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is unavailable")
    client = OpenAI(
        api_key=api_key,
        base_url=prepared["provider"]["base_url"],
        max_retries=0,
    )
    transport = OpenAIResponsesTransport(
        enable_token=EXTERNAL_CALL_ENABLE_TOKEN,
        client=client,
    )
    report = execute_once(prepared=prepared, transport=transport)
    secure_write_audit(output_path, report)
    print(
        canonical_json(
            {
                "report": AUDIT_RELATIVE_PATH,
                "manifest_sha256": manifest_sha256,
                "passed": report["passed"],
                "external_model_calls": report["external_model_calls"],
                "store": report["provider"]["store"],
                "implicit_http_retries": report["provider"][
                    "implicit_http_retries"
                ],
                "response_status": report["provider"]["audit"].get(
                    "response_status"
                ),
                "findings": report["findings"],
                "error": report["error"],
            }
        )
    )
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

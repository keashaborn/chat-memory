#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from openai import OpenAI

from scripts.memory_v1_consolidation_packet_eval import (
    load_manifest,
    secure_write_json,
    stable_json,
    state_snapshot,
    zero_write_proof,
)
from scripts.memory_v1_relational_v5_live_eval import (
    CONTRACT_VERSION,
    DEFAULT_COLLECTION,
    EXPECTED_MANIFEST_SHA256,
    REGISTRY_VERSION,
    enrich_packet,
    evaluate_case,
    load_jsonl,
    packet_integrity_reasons,
    sha256_bytes,
    validate_inputs,
)
from scripts.memory_v1_relational_v5_specialized import (
    EntityGraphPassPacket,
    ProjectKnowledgePassPacket,
    TemporalContentPassPacket,
)
from scripts.memory_v1_relational_v5_specialized_eval import (
    PIPELINE_VERSION,
    SpecializedExtractionError,
    run_specialized_zero_write,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manifest-bound, zero-write specialized V5 live evaluation"
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--cases", default="evals/memory_v1_relational_extraction_v5_cases.jsonl"
    )
    parser.add_argument(
        "--registry", default="specs/memory_v1_predicate_registry_v5.json"
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--collection", default=os.getenv("MEMORY_V1_COLLECTION", DEFAULT_COLLECTION)
    )
    return parser.parse_args()


def _pass_schema_hashes() -> dict[str, str]:
    return {
        "entity_graph": sha256_bytes(
            stable_json(EntityGraphPassPacket.model_json_schema()).encode("utf-8")
        ),
        "temporal_content": sha256_bytes(
            stable_json(TemporalContentPassPacket.model_json_schema()).encode("utf-8")
        ),
        "project_knowledge": sha256_bytes(
            stable_json(ProjectKnowledgePassPacket.model_json_schema()).encode("utf-8")
        ),
    }


def _attempts(value: list[Any]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in value]


def _repository_commit() -> str:
    root = Path(__file__).resolve().parent.parent
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise RuntimeError("specialized live evaluation requires a clean worktree")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
        raise RuntimeError("specialized live evaluator commit is invalid")
    return head


def _failed_evaluation(findings: list[str]) -> dict[str, Any]:
    return {"passed": False, "findings": findings, "actual": {}}


async def _snapshot(
    *,
    dsn: str,
    qdrant_url: str,
    collection: str,
    owner: uuid.UUID,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from rag_engine.qdrant_compat import make_qdrant_client

    qdrant = make_qdrant_client(url=qdrant_url, timeout=30.0)
    try:
        return await state_snapshot(
            dsn=dsn,
            qdrant=qdrant,
            collection=collection,
            owner=owner,
            manifest=manifest,
        )
    finally:
        qdrant.close()


async def run(args: argparse.Namespace) -> dict[str, Any]:
    manifest, manifest_sha256 = load_manifest(Path(args.manifest))
    cases = load_jsonl(Path(args.cases))
    registry_path = Path(args.registry)
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    validate_inputs(manifest, manifest_sha256, cases, registry)
    if manifest_sha256 != EXPECTED_MANIFEST_SHA256:
        raise RuntimeError("specialized live runner manifest hash mismatch")
    evaluator_commit = _repository_commit()

    dsn = os.getenv("POSTGRES_DSN", "").strip()
    qdrant_url = os.getenv("QDRANT_URL", "").strip()
    if not dsn or not qdrant_url:
        raise RuntimeError("POSTGRES_DSN and QDRANT_URL are required")
    owner = uuid.UUID(manifest["owner_user_id"])
    before, sources = await _snapshot(
        dsn=dsn,
        qdrant_url=qdrant_url,
        collection=args.collection,
        owner=owner,
        manifest=manifest,
    )

    if args.preflight_only:
        after, _ = await _snapshot(
            dsn=dsn,
            qdrant_url=qdrant_url,
            collection=args.collection,
            owner=owner,
            manifest=manifest,
        )
        return {
            "mode": "zero_write_relational_v5_specialized_preflight",
            "pipeline_version": PIPELINE_VERSION,
            "evaluator_commit": evaluator_commit,
            "contract_version": CONTRACT_VERSION,
            "registry_version": REGISTRY_VERSION,
            "registry_runtime_active": False,
            "manifest_sha256": manifest_sha256,
            "owner_user_id": str(owner),
            "source_count": len(sources),
            "external_model_calls": 0,
            "pass_schema_sha256": _pass_schema_hashes(),
            "registry_sha256": sha256_bytes(registry_path.read_bytes()),
            "zero_write_proof": zero_write_proof(before, after),
        }

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for live evaluation")
    client = OpenAI(api_key=api_key)
    model = (
        os.getenv("MEMORY_V1_RELATIONAL_V5_MODEL")
        or os.getenv("MEMORY_V1_CONSOLIDATION_MODEL")
        or os.getenv("VANTAGE_MODEL")
        or "gpt-5.2"
    ).strip()
    reports: list[dict[str, Any]] = []
    for ordinal, (case, manifest_source, live_source) in enumerate(
        zip(cases, manifest["sources"], sources, strict=True), 1
    ):
        text = str(live_source["text"] or "")
        row: dict[str, Any] = {
            "case_id": case["case_id"],
            "ordinal": ordinal,
            "source_external_id": manifest_source["source_external_id"],
            "source_sha256": manifest_source["source_sha256"],
            "source_chars": len(text),
            "attempts": [],
            "model_packet": None,
            "packet": None,
            "deterministic_rejections": [],
            "integrity_reasons": [],
        }
        try:
            specialized = await run_specialized_zero_write(
                client,
                model=model,
                owner_user_id=str(owner),
                source_external_id=manifest_source["source_external_id"],
                source_recorded_at=manifest_source["source_recorded_at"],
                text=text,
                registry=registry,
            )
            row["attempts"] = _attempts(specialized.attempts)
            row["model_packet"] = specialized.packet.model_dump(mode="json")
            packet, rejections = enrich_packet(
                specialized.packet,
                source=manifest_source,
                text=text,
                registry=registry,
            )
            integrity = packet_integrity_reasons(packet, rejections)
            evaluation = evaluate_case(packet, case["expected"], registry)
            if integrity:
                evaluation["passed"] = False
                evaluation["findings"] = sorted(
                    set(
                        list(evaluation["findings"])
                        + [f"integrity:{item}" for item in integrity]
                    )
                )
            row.update(
                {
                    "packet": packet,
                    "deterministic_rejections": rejections,
                    "integrity_reasons": integrity,
                    "evaluation": evaluation,
                }
            )
        except SpecializedExtractionError as exc:
            row["attempts"] = _attempts(exc.attempts)
            row["evaluation"] = _failed_evaluation(
                [f"specialized_extractor_error:{type(exc).__name__}:{exc}"]
            )
        except Exception as exc:
            row["evaluation"] = _failed_evaluation(
                [f"extractor_error:{type(exc).__name__}:{exc}"]
            )
        reports.append(row)

    after, _ = await _snapshot(
        dsn=dsn,
        qdrant_url=qdrant_url,
        collection=args.collection,
        owner=owner,
        manifest=manifest,
    )
    proof = zero_write_proof(before, after)
    passed = sum(int(row["evaluation"]["passed"]) for row in reports)
    attempts = [attempt for row in reports for attempt in row["attempts"]]
    return {
        "mode": "zero_write_relational_v5_specialized_live_evaluation",
        "pipeline_version": PIPELINE_VERSION,
        "evaluator_commit": evaluator_commit,
        "contract_version": CONTRACT_VERSION,
        "registry_version": REGISTRY_VERSION,
        "registry_runtime_active": False,
        "manifest_sha256": manifest_sha256,
        "owner_user_id": str(owner),
        "model": model,
        "store": False,
        "source_count": len(reports),
        "model_call_count": len(attempts),
        "repair_call_count": sum(
            int(attempt["attempt"] == "repair") for attempt in attempts
        ),
        "refusal_count": sum(int(attempt["refusal"]) for attempt in attempts),
        "incomplete_count": sum(
            int(attempt["response_status"] == "incomplete") for attempt in attempts
        ),
        "request_error_count": sum(
            int(attempt["response_status"] == "request_error") for attempt in attempts
        ),
        "passed_case_count": passed,
        "failed_case_count": len(reports) - passed,
        "deterministic_rejection_count": sum(
            len(row["deterministic_rejections"]) for row in reports
        ),
        "integrity_reason_count": sum(len(row["integrity_reasons"]) for row in reports),
        "pass_schema_sha256": _pass_schema_hashes(),
        "registry_sha256": sha256_bytes(registry_path.read_bytes()),
        "zero_write_proof": proof,
        "sources": reports,
    }


async def main() -> int:
    args = arguments()
    report = await run(args)
    secure_write_json(Path(args.output), report)
    summary = {
        "mode": report["mode"],
        "report": args.output,
        "manifest_sha256": report["manifest_sha256"],
        "source_count": report["source_count"],
        "model": report.get("model"),
        "external_model_calls": report.get(
            "model_call_count", report.get("external_model_calls", 0)
        ),
        "passed_case_count": report.get("passed_case_count"),
        "failed_case_count": report.get("failed_case_count"),
        "zero_write_proof": report["zero_write_proof"],
    }
    print(stable_json(summary))
    if not report["zero_write_proof"]["passed"]:
        return 3
    if args.preflight_only:
        return 0
    return 0 if report["failed_case_count"] == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}:{exc}", file=sys.stderr)
        raise SystemExit(1)

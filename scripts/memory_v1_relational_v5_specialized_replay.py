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

from scripts.memory_v1_consolidation_packet_eval import (
    load_manifest,
    secure_write_json,
    stable_json,
    state_snapshot,
    zero_write_proof,
)
from scripts.memory_v1_relational_v5_live_eval import (
    DEFAULT_COLLECTION,
    EXPECTED_MANIFEST_SHA256,
    ModelPacket,
    enrich_packet,
    evaluate_case,
    load_jsonl,
    packet_integrity_reasons,
    sha256_bytes,
    validate_inputs,
)
from scripts.memory_v1_relational_v5_specialized import (
    GRAPH_PREDICATES,
    EntityGraphPassPacket,
)
from scripts.memory_v1_relational_v5_specialized_eval import (
    normalize_repeated_sibling_roles,
)


MODE = "zero_write_relational_v5_specialized_saved_replay"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline deterministic replay of one saved specialized V5 result"
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--saved-report", required=True)
    parser.add_argument("--saved-report-sha256", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument(
        "--cases", default="evals/memory_v1_relational_extraction_v5_cases.jsonl"
    )
    parser.add_argument(
        "--registry", default="specs/memory_v1_predicate_registry_v5.json"
    )
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--collection", default=os.getenv("MEMORY_V1_COLLECTION", DEFAULT_COLLECTION)
    )
    return parser.parse_args()


def repository_commit() -> str:
    root = Path(__file__).resolve().parents[1]
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if status:
        raise RuntimeError("saved replay requires a clean worktree")
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def load_bound_report(path: Path, expected_sha256: str) -> dict[str, Any]:
    actual_sha256 = sha256_bytes(path.read_bytes())
    if actual_sha256 != expected_sha256:
        raise RuntimeError("saved report SHA-256 mismatch")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("mode") != "zero_write_relational_v5_specialized_live_evaluation":
        raise RuntimeError("saved report mode mismatch")
    if report.get("store") is not False:
        raise RuntimeError("saved report was not store=false")
    return report


def exactly_one(rows: list[dict[str, Any]], key: str, value: str) -> dict[str, Any]:
    matches = [row for row in rows if row.get(key) == value]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one {key}={value}")
    return matches[0]


def replay_model_packet(
    *,
    saved_row: dict[str, Any],
    case: dict[str, Any],
    manifest_source: dict[str, Any],
    live_source: dict[str, Any],
    registry: dict[str, Any],
) -> dict[str, Any]:
    if saved_row.get("source_external_id") != manifest_source.get("source_external_id"):
        raise RuntimeError("saved source identity mismatch")
    if saved_row.get("source_sha256") != manifest_source.get("source_sha256"):
        raise RuntimeError("saved source hash mismatch")
    text = str(live_source.get("text") or "")
    if sha256_bytes(text.encode("utf-8")) != manifest_source.get("source_sha256"):
        raise RuntimeError("live source hash mismatch")
    raw_model_packet = saved_row.get("model_packet")
    if not isinstance(raw_model_packet, dict):
        raise RuntimeError("saved model packet is absent")

    model_packet = ModelPacket.model_validate(raw_model_packet)
    original_roles = {
        item.entity_ref: item.relationship_role for item in model_packet.entity_mentions
    }
    graph = EntityGraphPassPacket(
        entity_mentions=[item.model_copy(deep=True) for item in model_packet.entity_mentions],
        relationship_observations=[
            item.model_copy(deep=True)
            for item in model_packet.observations
            if item.predicate in GRAPH_PREDICATES
        ],
        deferrals=[],
        packet_findings=list(model_packet.packet_findings),
    )
    graph = normalize_repeated_sibling_roles(graph, text)
    normalized_roles = {
        item.entity_ref: item.relationship_role for item in graph.entity_mentions
    }
    role_changes = []
    for entity in model_packet.entity_mentions:
        normalized_role = normalized_roles[entity.entity_ref]
        if entity.relationship_role != normalized_role:
            role_changes.append(
                {
                    "entity_ref": entity.entity_ref,
                    "before": entity.relationship_role,
                    "after": normalized_role,
                }
            )
            entity.relationship_role = normalized_role
    for finding in graph.packet_findings:
        if finding not in model_packet.packet_findings:
            model_packet.packet_findings.append(finding)

    packet, rejections = enrich_packet(
        model_packet,
        source=manifest_source,
        text=text,
        registry=registry,
    )
    integrity = packet_integrity_reasons(packet, rejections)
    evaluation = evaluate_case(packet, case["expected"], registry)
    if integrity:
        evaluation["passed"] = False
        evaluation["findings"] = sorted(
            set(list(evaluation["findings"]) + [f"integrity:{item}" for item in integrity])
        )
    return {
        "case_id": case["case_id"],
        "source_external_id": manifest_source["source_external_id"],
        "source_sha256": manifest_source["source_sha256"],
        "role_changes": role_changes,
        "original_entity_roles": sorted(
            role for role in original_roles.values() if role is not None
        ),
        "normalized_entity_roles": evaluation["actual"]["entity_roles"],
        "deterministic_rejections": rejections,
        "integrity_reasons": integrity,
        "evaluation": evaluation,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    manifest, manifest_sha256 = load_manifest(Path(args.manifest))
    if manifest_sha256 != EXPECTED_MANIFEST_SHA256:
        raise RuntimeError("manifest SHA-256 mismatch")
    cases_path = Path(args.cases)
    cases = load_jsonl(cases_path)
    registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    validate_inputs(manifest, manifest_sha256, cases, registry)
    saved_report = load_bound_report(
        Path(args.saved_report), args.saved_report_sha256
    )
    if saved_report.get("manifest_sha256") != manifest_sha256:
        raise RuntimeError("saved report manifest mismatch")

    case = exactly_one(cases, "case_id", args.case_id)
    saved_row = exactly_one(saved_report["sources"], "case_id", args.case_id)
    manifest_source = exactly_one(
        manifest["sources"], "source_external_id", saved_row["source_external_id"]
    )

    dsn = os.getenv("POSTGRES_DSN", "").strip()
    qdrant_url = os.getenv("QDRANT_URL", "").strip()
    if not dsn or not qdrant_url:
        raise RuntimeError("POSTGRES_DSN and QDRANT_URL are required")
    owner = uuid.UUID(manifest["owner_user_id"])
    from rag_engine.qdrant_compat import make_qdrant_client

    qdrant = make_qdrant_client(url=qdrant_url, timeout=30.0)
    try:
        before, sources = await state_snapshot(
            dsn=dsn,
            qdrant=qdrant,
            collection=args.collection,
            owner=owner,
            manifest=manifest,
        )
        live_source = exactly_one(
            sources, "source_external_id", manifest_source["source_external_id"]
        )
        replay = replay_model_packet(
            saved_row=saved_row,
            case=case,
            manifest_source=manifest_source,
            live_source=live_source,
            registry=registry,
        )
        after, _ = await state_snapshot(
            dsn=dsn,
            qdrant=qdrant,
            collection=args.collection,
            owner=owner,
            manifest=manifest,
        )
    finally:
        qdrant.close()

    return {
        "mode": MODE,
        "evaluator_commit": repository_commit(),
        "manifest_sha256": manifest_sha256,
        "case_contract_sha256": sha256_bytes(cases_path.read_bytes()),
        "saved_report_sha256": args.saved_report_sha256,
        "external_model_calls": 0,
        "store": False,
        "replay": replay,
        "zero_write_proof": zero_write_proof(before, after),
    }


async def main() -> int:
    args = arguments()
    report = await run(args)
    secure_write_json(Path(args.output), report)
    print(
        stable_json(
            {
                "mode": report["mode"],
                "report": args.output,
                "case_id": report["replay"]["case_id"],
                "external_model_calls": report["external_model_calls"],
                "role_changes": report["replay"]["role_changes"],
                "evaluation": report["replay"]["evaluation"],
                "zero_write_proof": report["zero_write_proof"],
            }
        )
    )
    if not report["zero_write_proof"]["passed"]:
        return 3
    return 0 if report["replay"]["evaluation"]["passed"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}:{exc}", file=sys.stderr)
        raise SystemExit(1)

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from openai import OpenAI

from rag_engine.memory_v1_consolidation import (
    ConsolidationError,
    ExtractedCandidate,
    _normalize_temporal_candidate,
    _validate_candidate,
    extract_with_openai,
)


INTRO_SOURCE = "2efbb10d-f07a-4936-beab-9e7155e433dd"
WORK_SOURCE = "387c34d3-e4a3-4d47-845f-f6a349fb37ae"
TRANSIENT_SOURCE = "b2b2f1c7-19da-4667-a93c-5eb7fe6aaa20"
JAZZ_SOURCE = "8e22f07c-71cb-49b9-9ac9-e9898668eadb"
ALCOHOL_SOURCE = "8e3e5fba-054c-4013-a317-3ca06ef53958"
RANCH_SOURCE = "3cb6072d-70f4-455e-b2ec-0bd9cb24aa51"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    return parser.parse_args()


def candidate_text(candidate: ExtractedCandidate) -> str:
    return " ".join(
        (
            candidate.canonical_text,
            candidate.predicate,
            candidate.object_literal,
            candidate.preference_domain,
            candidate.preference_key,
        )
    ).casefold()


def policy_findings(
    source_id: str,
    candidates: list[ExtractedCandidate],
) -> list[str]:
    findings: list[str] = []
    blobs = [(candidate, candidate_text(candidate)) for candidate in candidates]
    if source_id == INTRO_SOURCE and len(candidates) < 4:
        findings.append("compound introduction produced fewer than four atomic candidates")
    elif source_id == WORK_SOURCE:
        work_candidates = [
            candidate
            for candidate, blob in blobs
            if "work" in blob or "architect" in blob or "profession" in blob
        ]
        if not work_candidates:
            findings.append("work-history candidate is missing")
        elif not any(candidate.valid_from or candidate.valid_to for candidate in work_candidates):
            findings.append("work-history relative date was not anchored")
    elif source_id == TRANSIENT_SOURCE:
        if any("blocked" in blob or "lately" in blob for _, blob in blobs):
            findings.append("transient blocked/lately state survived validation")
        if not any("walk" in blob for _, blob in blobs):
            findings.append("durable walkabout behavior is missing")
    elif source_id == JAZZ_SOURCE:
        jazz = [candidate for candidate, blob in blobs if "jazz" in blob]
        if not jazz:
            findings.append("jazz preference is missing")
        elif not all(
            candidate.lane == "preference"
            and candidate.preference_class == "life"
            and candidate.surface_policy in {"mention_when_relevant", "explicit_recall_only"}
            for candidate in jazz
        ):
            findings.append("jazz was not routed as a life preference")
    elif source_id == ALCOHOL_SOURCE:
        alcohol = [
            candidate
            for candidate, blob in blobs
            if ("alcohol" in blob or "drink" in blob)
            and ("quit" in blob or "stop" in blob or "ceas" in blob)
        ]
        if not alcohol:
            findings.append("alcohol-stop claim is missing")
        elif not all(candidate.valid_from for candidate in alcohol):
            findings.append("alcohol-stop date was not anchored")
    elif source_id == RANCH_SOURCE:
        terms = ("tractor", "farm", "cattle")
        for term in terms:
            if not any(term in blob for _, blob in blobs):
                findings.append(f"atomic ranch activity is missing: {term}")
        for _, blob in blobs:
            if sum(term in blob for term in terms) > 1:
                findings.append("ranch activities remain compound")
                break
    return findings


async def load_sources(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    manifest_sources: list[dict[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    async with conn.transaction(readonly=True):
        await conn.execute("SELECT set_config('app.user_id', $1, true)", str(owner))
        for item in manifest_sources:
            source_id = uuid.UUID(item["source_external_id"])
            row = await conn.fetchrow(
                """
                SELECT id, text, created_at, source
                FROM public.chat_log
                WHERE owner_user_id=$1 AND id=$2
                """,
                owner,
                source_id,
            )
            if row is None or row["source"] != "frontend/chat:user":
                raise RuntimeError(f"owner-scoped source not found: {source_id}")
            actual = hashlib.sha256(str(row["text"] or "").encode("utf-8")).hexdigest()
            if actual != item["source_sha256"]:
                raise RuntimeError(f"source hash mismatch: {source_id}")
            rows.append(dict(row))
    return rows


async def main() -> int:
    args = arguments()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    owner = uuid.UUID(manifest["owner_user_id"])
    if manifest["pipeline_version"] != "20260714_v2":
        raise RuntimeError("manifest pipeline mismatch")
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is not configured")
    conn = await asyncpg.connect(dsn)
    try:
        if await conn.fetchval("SELECT current_user") != "brains_app":
            raise RuntimeError("evaluation requires brains_app")
        sources = await load_sources(conn, owner, manifest["sources"])
    finally:
        await conn.close()

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    model = (
        os.getenv("MEMORY_V1_CONSOLIDATION_MODEL")
        or os.getenv("VANTAGE_MODEL")
        or "gpt-5.2"
    ).strip()
    report: dict[str, Any] = {
        "mode": "zero_write_extraction_eval",
        "owner_user_id": str(owner),
        "pipeline_version": "20260714_v2",
        "model": model,
        "sources": [],
        "finding_count": 0,
    }
    for source in sources:
        extraction, response_id = await asyncio.to_thread(
            extract_with_openai,
            client,
            model=model,
            owner_user_id=owner,
            source_external_id=str(source["id"]),
            source_observed_at=source["created_at"],
            text=str(source["text"] or ""),
        )
        valid: list[ExtractedCandidate] = []
        rejected: list[dict[str, str]] = []
        for candidate in extraction.candidates:
            candidate = _normalize_temporal_candidate(
                candidate,
                source["created_at"],
            )
            try:
                _validate_candidate(candidate)
            except ConsolidationError as exc:
                rejected.append(
                    {
                        "lane": candidate.lane,
                        "canonical_text": candidate.canonical_text,
                        "reason": str(exc),
                    }
                )
            else:
                valid.append(candidate)
        findings = policy_findings(str(source["id"]), valid)
        report["finding_count"] += len(findings)
        report["sources"].append(
            {
                "source_external_id": str(source["id"]),
                "model_response_id": response_id,
                "model_candidate_count": len(extraction.candidates),
                "valid_candidates": [
                    candidate.model_dump(mode="json") for candidate in valid
                ],
                "validation_rejections": rejected,
                "policy_findings": findings,
            }
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["finding_count"] == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

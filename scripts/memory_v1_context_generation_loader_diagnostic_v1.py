#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from urllib.parse import urlparse
import uuid

import asyncpg

from rag_engine.memory_v1_evidence_context_loader_v1 import (
    load_memory_evidence_context_v1,
)


CONTRACT_VERSION = "memory_v1_context_generation_loader_diagnostic_v1"


def stable_json(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def loopback_dsn(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("context loader diagnostic DSN is invalid")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError(
            "context loader diagnostic DSN must be loopback-only"
        )
    return value


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--selector-version", required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    return parser.parse_args()


async def run() -> int:
    args = arguments()
    owner = uuid.UUID(args.owner_user_id)
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(
        loopback_dsn(dsn),
        command_timeout=60,
        ssl=False,
    )
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError(
                "context loader diagnostic requires brains_app session"
            )
        async with conn.transaction(readonly=True):
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)",
                str(owner),
            )
            jobs = await conn.fetch(
                """
                SELECT job_id,evidence_id,evidence_content_sha256
                FROM memory.evidence_extraction_job
                WHERE owner_user_id=$1
                  AND selector_version=$2
                ORDER BY evidence_id,job_id
                """,
                owner,
                args.selector_version,
            )
        if len(jobs) != args.expected_count:
            raise RuntimeError(
                "context loader diagnostic job count changed"
            )

        origins: Counter[str] = Counter()
        span_counts: Counter[int] = Counter()
        envelope_hashes: list[str] = []
        for job in jobs:
            envelope = await load_memory_evidence_context_v1(
                conn,
                expected_owner_user_id=owner,
                target_evidence_id=job["evidence_id"],
                expected_target_content_sha256=job[
                    "evidence_content_sha256"
                ],
                max_spans=32,
            )
            if envelope.target_evidence_id != str(job["evidence_id"]):
                raise RuntimeError(
                    "context loader diagnostic target changed"
                )
            if envelope.allowed_assertion_evidence_ids != (
                str(job["evidence_id"]),
            ):
                raise RuntimeError(
                    "context loader diagnostic assertion authority changed"
                )
            envelope.validate_hash()
            origins.update(span.span_origin for span in envelope.spans)
            span_counts[len(envelope.spans)] += 1
            envelope_hashes.append(envelope.envelope_sha256)

        report = {
            "contract_version": CONTRACT_VERSION,
            "owner_user_id_sha256": sha256_text(str(owner)),
            "selector_version_sha256": sha256_text(
                args.selector_version
            ),
            "job_count": len(jobs),
            "contexts_loaded": len(envelope_hashes),
            "span_count_distribution": dict(sorted(span_counts.items())),
            "span_origin_counts": dict(sorted(origins.items())),
            "envelope_set_sha256": sha256_text(
                stable_json(sorted(envelope_hashes))
            ),
            "database_writes": 0,
            "model_calls": 0,
            "qdrant_writes": 0,
            "prompt_influence": 0,
            "outcome": "pass",
        }
        print(stable_json(report))
        return 0
    finally:
        await conn.close()


def main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:
        print(
            stable_json(
                {
                    "contract_version": CONTRACT_VERSION,
                    "outcome": "rejected",
                    "error_class": type(exc).__name__,
                    "error_sha256": sha256_text(str(exc)),
                    "database_writes": 0,
                    "model_calls": 0,
                    "qdrant_writes": 0,
                    "prompt_influence": 0,
                }
            ),
            file=__import__("sys").stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

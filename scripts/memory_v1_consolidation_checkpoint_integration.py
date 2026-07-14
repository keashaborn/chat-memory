#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import uuid

import asyncpg
from openai import OpenAI

from rag_engine.memory_v1_consolidation import EXTRACTOR_VERSION, extract_with_openai
from scripts.memory_v1_consolidation_worker import (
    _sha256,
    checkpoint_extraction,
    claim_job,
    fail_job,
    source_record,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--confirm-test-database", required=True)
    return parser.parse_args()


async def main() -> int:
    args = arguments()
    owner = uuid.UUID(args.owner_user_id)
    dsn = os.environ["POSTGRES_DSN"]
    conn = await asyncpg.connect(dsn)
    worker_id = f"{socket.gethostname()}:checkpoint-test:{EXTRACTOR_VERSION}"
    try:
        database = await conn.fetchval("SELECT current_database()")
        if database != args.confirm_test_database or not database.startswith(
            "memory_owner_consolidation_"
        ):
            raise RuntimeError("checkpoint integration test requires the disposable database")
        if await conn.fetchval("SELECT current_user") != "brains_app":
            raise RuntimeError("checkpoint integration test requires brains_app")
        job = await claim_job(
            conn,
            owner=owner,
            worker_id=worker_id,
            lease_seconds=300,
        )
        if job is None:
            raise RuntimeError("no eligible test job")
        source = await source_record(
            conn,
            owner=owner,
            source_external_id=job["source_external_id"],
        )
        if source is None:
            raise RuntimeError("test source is missing")
        text = str(source.get("text") or "")
        if _sha256(text) != job["source_sha256"]:
            raise RuntimeError("test source hash mismatch")
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        model = (
            os.getenv("MEMORY_V1_CONSOLIDATION_MODEL")
            or os.getenv("VANTAGE_MODEL")
            or "gpt-5.2"
        )
        extraction, response_id = await asyncio.to_thread(
            extract_with_openai,
            client,
            model=model,
            owner_user_id=owner,
            source_external_id=str(source["id"]),
            text=text,
        )
        if not await checkpoint_extraction(
            conn,
            owner=owner,
            job=job,
            worker_id=worker_id,
            extraction=extraction,
            response_id=response_id,
        ):
            raise RuntimeError("test extraction exceeded checkpoint size")
        await fail_job(
            conn,
            owner=owner,
            job=job,
            worker_id=worker_id,
            error=RuntimeError("simulated post-checkpoint crash"),
            max_attempts=5,
        )
        print(
            json.dumps(
                {
                    "job_id": str(job["job_id"]),
                    "source_external_id": job["source_external_id"],
                    "checkpoint_candidates": len(extraction.candidates),
                    "simulated_status": "error",
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

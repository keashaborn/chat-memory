#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

from rag_engine.memory_v1_v5_shadow_loader import load_v5_shadow_claims
from rag_engine.memory_v1_v5_shadow_retrieval import evaluate_v5_shadow_claims

VERSION = "memory_v1_v5_shadow_live_probe_v1"
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
CLAIM = "50ebf1af-b072-4bf9-badc-2df7585f12c6"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only production probe of the V5 shadow reader and selector"
    )
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def _secure_write(path: Path, value: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(payload).hexdigest()


async def probe(dsn: str) -> dict:
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        records = await load_v5_shadow_claims(conn, OWNER, [CLAIM])
    finally:
        await conn.close()
    result = evaluate_v5_shadow_claims(
        OWNER,
        query="What kind of work do I do?",
        intent="profile_recall",
        domain="profile",
        allowed_predicate_prefixes=["occupation."],
        candidate_hits=[{"claim_id": CLAIM, "semantic_score": 1.0}],
        records=records,
    )
    if len(records) != 1:
        raise RuntimeError("expected exactly one owner-scoped V5 candidate record")
    if result["selected_count"] != 0:
        raise RuntimeError("candidate V5 claim was unexpectedly selected")
    if result["rejected_counts"] != {"status:candidate": 1}:
        raise RuntimeError("candidate V5 claim did not fail only at status")
    if any(
        result[key]
        for key in (
            "prompt_injection",
            "answer_model_exposure",
            "retrieval_activation",
        )
    ):
        raise RuntimeError("V5 shadow probe unexpectedly activated retrieval")
    return {
        "record_count": len(records),
        "record_status": str(records[0]["status"]),
        "result": result,
    }


def main() -> int:
    args = arguments()
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise SystemExit("POSTGRES_DSN is required")
    value = asyncio.run(probe(dsn))
    report = {
        "contract_version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server": "seebx",
        "mode": "read_only_zero_write",
        "owner_user_id": OWNER,
        "claim_id": CLAIM,
        "probe": value,
        "database_writes": 0,
        "qdrant_reads": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "router_modified": False,
        "prompt_influence": False,
    }
    output = Path(args.output).resolve()
    sha256 = _secure_write(output, report)
    print(
        json.dumps(
            {
                "version": VERSION,
                "output": str(output),
                "sha256": sha256,
                "selected_count": value["result"]["selected_count"],
                "rejected_counts": value["result"]["rejected_counts"],
                "prompt_influence": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

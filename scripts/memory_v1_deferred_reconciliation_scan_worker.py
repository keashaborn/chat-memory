#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import uuid
from pathlib import Path
from typing import Any

import asyncpg


ROSTER_CONTRACT = "memory_v1_deferred_scanner_owner_roster_v1"
WORKER_VERSION = "memory_v1_deferred_reconciliation_scan_worker_v1"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run owner-scoped deferred reconciliation scans in audit-only mode."
    )
    parser.add_argument(
        "--roster",
        default=os.environ.get("MEMORY_V1_DEFERRED_SCANNER_OWNER_ROSTER"),
    )
    parser.add_argument(
        "--expected-roster-sha256",
        default=os.environ.get("MEMORY_V1_DEFERRED_SCANNER_ROSTER_SHA256"),
    )
    parser.add_argument("--limit", type=int, default=25)
    return parser.parse_args()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_roster(path_value: str | None, expected_sha256: str | None) -> tuple[list[uuid.UUID], str]:
    if not path_value:
        raise RuntimeError("owner roster path is required")
    if (
        not expected_sha256
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise RuntimeError("expected owner roster SHA-256 is required")
    path = Path(path_value)
    raw = path.read_bytes()
    actual_sha256 = _sha256(raw)
    if actual_sha256 != expected_sha256:
        raise RuntimeError("owner roster SHA-256 mismatch")
    value = json.loads(raw)
    if set(value) != {"contract_version", "source", "owners"}:
        raise RuntimeError("owner roster keys do not match the contract")
    if value["contract_version"] != ROSTER_CONTRACT:
        raise RuntimeError("owner roster contract mismatch")
    if value["source"] != "user_approved_active_accounts_20260715":
        raise RuntimeError("owner roster authority source mismatch")
    raw_owners = value["owners"]
    if not isinstance(raw_owners, list) or not 1 <= len(raw_owners) <= 100:
        raise RuntimeError("owner roster must contain 1 to 100 UUIDs")
    if raw_owners != sorted(raw_owners) or len(raw_owners) != len(set(raw_owners)):
        raise RuntimeError("owner roster must be sorted and unique")
    owners = [uuid.UUID(item) for item in raw_owners]
    return owners, actual_sha256


async def run_owner(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    roster_sha256: str,
    worker_ref: str,
    limit: int,
    run_id: uuid.UUID,
) -> dict[str, Any]:
    async with conn.transaction():
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)",
            str(owner),
        )
        row = await conn.fetchrow(
            """
            SELECT run_id,outcome,candidate_count,candidates_sha256,
                   run_manifest_sha256,rows_written
            FROM memory.run_deferred_reconciliation_scan_v5(
              $1,$2,$3,$4
            )
            """,
            run_id,
            limit,
            roster_sha256,
            worker_ref,
        )
    if row is None:
        raise RuntimeError(f"scan returned no result for owner {owner}")
    result = dict(row)
    if result["outcome"] != "applied" or int(result["rows_written"]) != 1:
        raise RuntimeError(f"scan was not newly audited for owner {owner}")
    return {
        "run_id": str(result["run_id"]),
        "candidate_count": int(result["candidate_count"]),
        "candidates_sha256": result["candidates_sha256"],
        "run_manifest_sha256": result["run_manifest_sha256"],
    }


async def async_main(args: argparse.Namespace) -> dict[str, Any]:
    if not 1 <= int(args.limit) <= 100:
        raise RuntimeError("limit must be between 1 and 100")
    owners, roster_sha256 = load_roster(
        args.roster,
        args.expected_roster_sha256,
    )
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    worker_ref = (
        f"{WORKER_VERSION}:{socket.gethostname()}:{os.getpid()}:"
        f"{uuid.uuid4().hex[:12]}"
    )
    conn = await asyncpg.connect(dsn, command_timeout=30)
    results: dict[str, dict[str, Any]] = {}
    try:
        for owner in owners:
            results[str(owner)] = await run_owner(
                conn,
                owner=owner,
                roster_sha256=roster_sha256,
                worker_ref=worker_ref,
                limit=int(args.limit),
                run_id=uuid.uuid4(),
            )
    finally:
        await conn.close()
    return {
        "contract_version": "memory_v1_deferred_scan_worker_report_v1",
        "worker_version": WORKER_VERSION,
        "owner_roster_sha256": roster_sha256,
        "owner_count": len(owners),
        "candidate_count": sum(
            item["candidate_count"] for item in results.values()
        ),
        "automatic_apply": False,
        "owners": results,
    }


def main() -> int:
    result = asyncio.run(async_main(arguments()))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
